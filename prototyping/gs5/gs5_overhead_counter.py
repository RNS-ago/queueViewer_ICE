"""
gs5_overhead_counter.py
=======================
People counter for an OVERHEAD 2D line-scanning LiDAR (YDLIDAR GS5) mounted
above a doorway/lane, with its 80 deg fan oriented ACROSS the path. Each frame
is a cross-section of whatever is under the sensor. People become point
clusters; counting two people walking side-by-side -- the thing a single beam
cannot do -- works here because they appear as two separate lateral clusters.

GS5 hardware facts that shape this design:
  * Range 0.07 - 1.00 m ONLY  -> floor is out of range (empty = no returns).
    Mount low: for heads P_min..P_max, height H must satisfy
        P_max + 0.07 <= H <= P_min + 1.00
  * 80 deg FOV, 0.54 deg resolution, solid-state (no moving parts).
  * Lateral swath at the head plane ~= 1.68 * (H - head_height). ~0.85 m at
    0.5 m below the sensor -> roughly one doorway wide.

Pipeline:  frame -> cartesian -> foreground -> lateral clustering -> tracking -> count

Plug your real GS5 SDK (YDLidar-SDK) frame callback into `process_frame`,
passing per-point angle (deg, 0 = straight down) and range (metres).

------------------------------------------------------------------------------
VERSIONING
  git log --oneline                                   # history
  git checkout <commit> -- gs5_overhead_counter.py    # restore an old revision
  Snapshot copies kept as *_v<version>.py
  Single-beam predecessor preserved as overhead_lidar_counter.py (v1.0.0).

__version__ = "2.1.0"

CHANGELOG
  2.0.0  Rewrite for the YDLIDAR GS5 2D line scanner. Per-frame cross-section
         geometry, lateral clustering (separates side-by-side people), and a
         centroid tracker that counts distinct crossings. New frame-based API.
  1.0.0  Single-beam distance-over-time counter (see overhead_lidar_counter.py).
------------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

__version__ = "2.1.0"


# ---------------------------------------------------------------------------
# Configuration  (defaults tuned for a GS5 over a ~doorway-width lane)
# ---------------------------------------------------------------------------
@dataclass
class GS5Config:
    # Sensor / mounting
    fov_deg: float = 80.0
    range_min_m: float = 0.07
    range_max_m: float = 1.00
    frame_rate_hz: float = 15.0        # confirm from the SDK; used only for time/speed
    mount_height_m: float = 2.20       # sensor height above floor

    # Foreground gating
    min_person_height_m: float = 1.20  # head must be at least this tall to count
    bg_margin_m: float = 0.08          # point must be this much closer than learned background

    # Lateral clustering (across the path)
    cluster_gap_m: float = 0.10        # split clusters separated by a bigger x-gap
    min_cluster_points: int = 7
    min_cluster_width_m: float = 0.08

    # Tracking
    max_track_jump_m: float = 0.40     # max centroid move between frames to stay same track
    min_track_frames: int = 2          # a track must persist this long to be counted
    max_missing_frames: int = 3        # close a track after this many frames with no match

    # Directional counting (distance to sensor over a track's life)
    min_directional_travel_m: float = 0.06  # min change in distance-to-sensor to assign a direction


@dataclass
class Cluster:
    x: float            # lateral centroid (m, 0 = under sensor)
    height: float       # peak height above floor (m)
    width: float        # lateral extent (m)
    n_points: int
    dist: float = 0.0   # closest distance to the sensor (m); smaller = nearer


@dataclass
class _Track:
    id: int
    x: float
    frames_seen: int = 1
    frames_missing: int = 0
    counted: bool = False
    xs: List[float] = field(default_factory=list)
    ts: List[float] = field(default_factory=list)
    ds: List[float] = field(default_factory=list)   # distance-to-sensor per frame


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def frame_to_cartesian(angles_deg: Sequence[float], ranges_m: Sequence[float],
                       cfg: GS5Config) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (x, height, valid_mask) for one frame.

    angle 0 deg = straight down. x = lateral position, height = above floor.
    """
    a = np.deg2rad(np.asarray(angles_deg, dtype=float))
    r = np.asarray(ranges_m, dtype=float)
    valid = np.isfinite(r) & (r > cfg.range_min_m) & (r < cfg.range_max_m)
    z = r * np.cos(a)                  # depth below sensor
    x = r * np.sin(a)                  # lateral
    height = cfg.mount_height_m - z
    return x, height, valid


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------
class GS5PeopleCounter:
    def __init__(self, cfg: GS5Config = GS5Config()):
        self.cfg = cfg
        self.count = 0        # net = entered - exited
        self.entered = 0      # gross toward-sensor crossings
        self.exited = 0       # gross away-from-sensor crossings
        self._tracks: List[_Track] = []
        self._next_id = 0
        self._frame_idx = 0
        # per-beam background (max valid range seen while "empty"); learned lazily
        self._bg: Optional[np.ndarray] = None

    # -- public --------------------------------------------------------------
    def process_frame(self, angles_deg: Sequence[float], ranges_m: Sequence[float],
                      t: Optional[float] = None) -> List[Cluster]:
        if t is None:
            t = self._frame_idx / self.cfg.frame_rate_hz
        self._frame_idx += 1

        clusters = self._segment(angles_deg, ranges_m)
        self._update_tracks(clusters, t)
        return clusters

    def finalize(self) -> None:
        """Close any open tracks at end of stream (counts long-lived ones)."""
        for tr in self._tracks:
            self._finish_track(tr)
        self._tracks.clear()

    # -- directional counting ------------------------------------------------
    def _finish_track(self, tr: _Track) -> None:
        """Tally a completed track by direction of travel toward the sensor.

        `dist` is the cluster's closest distance to the sensor (smaller = nearer).
        A track that moved from far -> near over its life is an entry (+1);
        near -> far is an exit (-1). Travel below `min_directional_travel_m` has
        no clear direction and is ignored, as is a too-short track.
        """
        cfg = self.cfg
        if tr.counted or tr.frames_seen < cfg.min_track_frames or len(tr.ds) < 2:
            return
        k = max(1, len(tr.ds) // 3)
        first = sum(tr.ds[:k]) / k          # mean distance at the start
        last = sum(tr.ds[-k:]) / k          # mean distance at the end
        travel = first - last               # > 0: got closer (far -> near)
        if abs(travel) < cfg.min_directional_travel_m:
            return                          # ambiguous: no net direction
        tr.counted = True
        if travel > 0:
            self.entered += 1
        else:8tiagO
            self.exited += 1
        self.count = self.entered - self.exited

    # -- segmentation --------------------------------------------------------
    def _segment(self, angles_deg, ranges_m) -> List[Cluster]:
        cfg = self.cfg
        x, height, valid = frame_to_cartesian(angles_deg, ranges_m, cfg)
        r = np.asarray(ranges_m, dtype=float)

        # background model: learn per-beam far range when the frame looks empty
        if self._bg is None:
            self._bg = np.full(len(r), np.inf)
        fg_by_bg = np.ones(len(r), dtype=bool)
        learn = ~valid  # beams with no return contribute nothing to "person"
        # a point is foreground if clearly closer than its learned background
        finite_bg = np.isfinite(self._bg)
        fg_by_bg[finite_bg] = r[finite_bg] < (self._bg[finite_bg] - cfg.bg_margin_m)

        person = valid & (height >= cfg.min_person_height_m) & fg_by_bg

        # update background with valid, non-person points (fixed structure / floor edge)
        upd = valid & ~person
        self._bg[upd] = np.where(np.isfinite(self._bg[upd]),
                                 np.maximum(self._bg[upd], r[upd]), r[upd])
        _ = learn  # (kept for clarity)

        if not person.any():
            return []

        xs = x[person]
        hs = height[person]
        rs = r[person]                     # range = distance to sensor
        order = np.argsort(xs)
        xs, hs, rs = xs[order], hs[order], rs[order]

        # split into clusters wherever the lateral gap is too big
        clusters: List[Cluster] = []
        start = 0
        for i in range(1, len(xs) + 1):
            gap_break = (i == len(xs)) or (xs[i] - xs[i - 1] > cfg.cluster_gap_m)
            if gap_break:
                cx = xs[start:i]
                ch = hs[start:i]
                cr = rs[start:i]
                width = float(cx[-1] - cx[0])
                if len(cx) >= cfg.min_cluster_points and width >= cfg.min_cluster_width_m:
                    clusters.append(Cluster(x=float(cx.mean()),
                                            height=float(ch.max()),
                                            width=width, n_points=len(cx),
                                            dist=float(cr.min())))
                start = i
        return clusters

    # -- tracking ------------------------------------------------------------
    def _update_tracks(self, clusters: List[Cluster], t: float) -> None:
        cfg = self.cfg
        unmatched = set(range(len(clusters)))

        # greedy nearest-neighbour match (tracks -> clusters)
        for tr in self._tracks:
            best, best_d = None, cfg.max_track_jump_m
            for ci in unmatched:
                d = abs(clusters[ci].x - tr.x)
                if d <= best_d:
                    best, best_d = ci, d
            if best is not None:
                tr.x = clusters[best].x
                tr.frames_seen += 1
                tr.frames_missing = 0
                tr.xs.append(tr.x); tr.ts.append(t)
                tr.ds.append(clusters[best].dist)
                unmatched.discard(best)
            else:
                tr.frames_missing += 1

        # new tracks for leftover clusters
        for ci in unmatched:
            tr = _Track(id=self._next_id, x=clusters[ci].x)
            tr.xs.append(tr.x); tr.ts.append(t)
            tr.ds.append(clusters[ci].dist)
            self._tracks.append(tr)
            self._next_id += 1

        # retire tracks that vanished; tally direction for the real ones
        survivors = []
        for tr in self._tracks:
            if tr.frames_missing > cfg.max_missing_frames:
                self._finish_track(tr)
            else:
                survivors.append(tr)
        self._tracks = survivors


# ---------------------------------------------------------------------------
# Simulation + plot
# ---------------------------------------------------------------------------
def _make_beams(cfg: GS5Config) -> np.ndarray:
    n = int(cfg.fov_deg / 0.54) + 1
    return np.linspace(-cfg.fov_deg / 2, cfg.fov_deg / 2, n)


def _frame_with_people(beams_deg, people, cfg, noise=0.004):
    """people: list of (lateral_x_m, head_height_m, half_width_m). Returns ranges."""
    a = np.deg2rad(beams_deg)
    ranges = np.full(len(beams_deg), np.nan)  # no return by default
    for (X, Ph, w) in people:
        z = cfg.mount_height_m - Ph                # depth of head below sensor
        if not (cfg.range_min_m < z < cfg.range_max_m):
            continue
        x_ground = z * np.tan(a)                    # where each beam lands at depth z
        hit = np.abs(x_ground - X) <= w
        r = z / np.cos(a)                           # slant range to a flat head top
        r = r + np.random.normal(0, noise, size=r.shape)
        ranges[hit] = r[hit]
    return ranges


def _demo():
    np.random.seed(3)
    cfg = GS5Config(mount_height_m=2.20, frame_rate_hz=15.0)
    beams = _make_beams(cfg)
    counter = GS5PeopleCounter(cfg)

    frames = []  # (t, [people...])
    fr = 0
    N = 8
    def t_of(f): return f / cfg.frame_rate_hz
    def ramp(i, h0, h1): return h0 + (h1 - h0) * (i / max(1, N - 1))
    # rising head height -> head gets nearer the sensor -> entry; falling -> exit.

    # 5 empty frames (let background settle)
    for _ in range(5):
        frames.append((t_of(fr), [])); fr += 1
    # Person A walks IN (head 1.55 -> 1.85, far -> near)  => +1
    for i in range(N):
        frames.append((t_of(fr), [(-0.05, ramp(i, 1.55, 1.85), 0.11)])); fr += 1
    # gap
    for _ in range(6):
        frames.append((t_of(fr), [])); fr += 1
    # Persons B & C SIDE BY SIDE, both walking IN  => +2
    for i in range(N):
        frames.append((t_of(fr), [(-0.26, ramp(i, 1.55, 1.85), 0.11),
                                   (0.26, ramp(i, 1.52, 1.82), 0.11)])); fr += 1
    # gap
    for _ in range(6):
        frames.append((t_of(fr), [])); fr += 1
    # Person D walks OUT (head 1.85 -> 1.55, near -> far)  => -1
    for i in range(N):
        frames.append((t_of(fr), [(0.0, ramp(i, 1.85, 1.55), 0.11)])); fr += 1
    # trailing empties
    for _ in range(6):
        frames.append((t_of(fr), [])); fr += 1

    max_clusters = 0
    track_log = []  # (t, x) of matched clusters for plotting
    for (t, ppl) in frames:
        ranges = _frame_with_people(beams, ppl, cfg)
        clusters = counter.process_frame(beams, ranges, t=t)
        max_clusters = max(max_clusters, len(clusters))
        for c in clusters:
            track_log.append((t, c.x))
    counter.finalize()

    print(f"gs5_overhead_counter v{__version__}")
    print(f"mount height = {cfg.mount_height_m} m,  FOV = {cfg.fov_deg} deg,  "
          f"range = {cfg.range_min_m}-{cfg.range_max_m} m")
    print(f"max clusters in a single frame = {max_clusters}  (proves side-by-side split)")
    print(f"entered (toward sensor) = {counter.entered}   exited (away) = {counter.exited}")
    print(f"NET PEOPLE COUNT: {counter.count}  (expected +2: 3 in, 1 out)")

    # ---- plot: lateral position vs time, showing 1 then 2 parallel tracks ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if track_log:
            ts, xs = zip(*track_log)
        else:
            ts, xs = [], []
        fig, ax = plt.subplots(figsize=(11, 4.2))
        ax.scatter(ts, xs, s=26, color="#2b6cb0", label="detected person cluster")
        ax.axhline(0, color="#a0aec0", lw=0.8)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("lateral position across path (m)")
        ax.set_title(f"GS5 overhead cross-section  |  net {counter.count:+d} "
                     f"({counter.entered} in, {counter.exited} out)")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper right", fontsize=9)
        fig.tight_layout()
        fig.savefig("gs5_demo.png", dpi=130)
        print("plot written to gs5_demo.png")
    except ImportError:
        print("(matplotlib not installed -> skipping plot)")


if __name__ == "__main__":
    _demo()
