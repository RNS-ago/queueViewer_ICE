"""
gs5_gate_fusion.py
==================
ALTERNATIVE MODE: fuse TWO YDLIDAR GS5 sensors mounted on a gate into one
shared "gate frame", then cluster + track the merged cross-section. This
widens coverage past a single GS5's 1m range and reduces occlusion.

Why fuse (vs two independent counters): a person in the overlap zone is seen
by both sensors. Merging into ONE frame first means the overlap densifies the
SAME cluster instead of being counted twice.

GATE FRAME (2D, the vertical cross-section people walk through):
    u = horizontal across the gate (0 = left post .. width = right post)
    v = vertical (0 = floor, up positive)

Each sensor has a pose (u0, v0, boresight_deg) in that frame. A raw point
(SDK fan-angle a_deg, range r) maps to:
    theta = boresight_deg + (a_deg - center_angle_deg)     # +/- flip
    u = u0 + r*cos(theta)
    v = v0 + r*sin(theta)

REACH NOTE: GS5 range is 0.07-1.00 m, so fusion tops out at roughly a ~2 m
gate; the CENTRE is the most range-marginal zone, not the edges.

Typical bottom-corner layout for a width-W gate, sensors angled 40 deg up so
one fan edge is vertical:
    A = SensorPose(u0=0.05,    v0=0.10, boresight_deg= 55)   # bottom-left, up-right
    B = SensorPose(u0=W-0.05,  v0=0.10, boresight_deg=125)   # bottom-right, up-left
(Overhead-pair layout also works: give both poses near the ceiling pointing
down, e.g. boresight ~ -80 and -100.)

------------------------------------------------------------------------------
VERSIONING
  git log --oneline ; git checkout <commit> -- gs5_gate_fusion.py
  __version__ = "3.0.0"
  CHANGELOG
    3.0.0  Add dual-GS5 gate-fusion mode: shared-frame transform, merge,
           u-clustering with vertical-spread gating, centroid tracking.
    2.1.0  Live single-GS5 hardware runner.
    2.0.0  Single-GS5 overhead counter core.
    1.0.0  Single-beam counter.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

__version__ = "3.0.0"


# ---------------------------------------------------------------------------
# Geometry / configuration
# ---------------------------------------------------------------------------
@dataclass
class SensorPose:
    u0: float                      # gate-frame position (m)
    v0: float
    boresight_deg: float           # direction of fan centre in gate frame
    center_angle_deg: float = 0.0  # SDK angle that == fan-angle 0 (from --calibrate)
    flip: bool = False             # mirror the fan if the sensor reads reversed


@dataclass
class GateConfig:
    width_m: float = 1.60
    v_max_m: float = 2.30
    range_min_m: float = 0.07
    range_max_m: float = 1.00
    floor_margin_m: float = 0.12       # drop points at/below the floor
    fov_half_deg: float = 40.0         # 80 deg / 2

    # clustering across the gate (on u)
    # Side-mounting sees only the near silhouette EDGES, so one body = two
    # vertical streaks ~body-width apart. We first split into fine fragments,
    # then greedily group fragments spanning <= max_body_width into one person.
    fragment_gap_m: float = 0.10       # split raw points into edge fragments
    max_body_width_m: float = 0.48     # a single person spans at most this in u
    min_cluster_points: int = 4
    min_vertical_spread_m: float = 0.22  # a person is a tall-ish vertical streak

    # tracking
    max_track_jump_m: float = 0.45
    min_track_frames: int = 2
    max_missing_frames: int = 3


@dataclass
class GateCluster:
    u: float          # horizontal centroid across the gate (m)
    v_lo: float
    v_hi: float
    n_points: int

    @property
    def v_spread(self) -> float:
        return self.v_hi - self.v_lo


@dataclass
class _Track:
    id: int
    u: float
    frames_seen: int = 1
    frames_missing: int = 0
    counted: bool = False


def sensor_points_to_gate(angles_deg: Sequence[float], ranges_m: Sequence[float],
                          pose: SensorPose, cfg: GateConfig
                          ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map one sensor's (fan-angle, range) into gate-frame (u, v, valid)."""
    a = np.asarray(angles_deg, dtype=float)
    if pose.flip:
        a = -a
    fan = a - pose.center_angle_deg
    r = np.asarray(ranges_m, dtype=float)
    in_fov = np.abs(fan) <= cfg.fov_half_deg
    in_rng = np.isfinite(r) & (r > cfg.range_min_m) & (r < cfg.range_max_m)
    theta = np.deg2rad(pose.boresight_deg + fan)
    u = pose.u0 + r * np.cos(theta)
    v = pose.v0 + r * np.sin(theta)
    return u, v, (in_fov & in_rng)


# ---------------------------------------------------------------------------
# Fusion counter
# ---------------------------------------------------------------------------
class GateFusionCounter:
    def __init__(self, cfg: GateConfig, pose_a: SensorPose, pose_b: SensorPose):
        self.cfg = cfg
        self.pose_a = pose_a
        self.pose_b = pose_b
        self.count = 0
        self._tracks: List[_Track] = []
        self._next_id = 0
        self._frame_idx = 0

    def process_dual(self,
                     angles_a, ranges_a,
                     angles_b, ranges_b,
                     t: Optional[float] = None) -> List[GateCluster]:
        self._frame_idx += 1
        cfg = self.cfg

        ua, va, oka = sensor_points_to_gate(angles_a, ranges_a, self.pose_a, cfg)
        ub, vb, okb = sensor_points_to_gate(angles_b, ranges_b, self.pose_b, cfg)

        u = np.concatenate([ua[oka], ub[okb]])
        v = np.concatenate([va[oka], vb[okb]])

        # keep points inside the opening and above the floor
        m = (u >= 0) & (u <= cfg.width_m) & (v >= cfg.floor_margin_m) & (v <= cfg.v_max_m)
        u, v = u[m], v[m]

        clusters = self._cluster(u, v)
        self._track(clusters, t)
        return clusters

    def finalize(self) -> None:
        for tr in self._tracks:
            if not tr.counted and tr.frames_seen >= self.cfg.min_track_frames:
                self.count += 1
                tr.counted = True
        self._tracks.clear()

    # -- clustering: fine fragments -> width-bounded body grouping ----------
    def _cluster(self, u: np.ndarray, v: np.ndarray) -> List[GateCluster]:
        cfg = self.cfg
        if len(u) == 0:
            return []
        order = np.argsort(u)
        u, v = u[order], v[order]

        # 1) split into fine fragments (each is a silhouette edge / piece)
        frags: List[Tuple[np.ndarray, np.ndarray]] = []
        start = 0
        for i in range(1, len(u) + 1):
            if (i == len(u)) or (u[i] - u[i - 1] > cfg.fragment_gap_m):
                frags.append((u[start:i], v[start:i]))
                start = i

        # 2) greedily group fragments into bodies spanning <= max_body_width
        clusters: List[GateCluster] = []
        gu: List[np.ndarray] = []
        gv: List[np.ndarray] = []
        body_start_u = None

        def flush():
            if not gu:
                return
            cu = np.concatenate(gu)
            cv = np.concatenate(gv)
            if len(cu) >= cfg.min_cluster_points and \
               (cv.max() - cv.min()) >= cfg.min_vertical_spread_m:
                clusters.append(GateCluster(u=float(cu.mean()),
                                            v_lo=float(cv.min()),
                                            v_hi=float(cv.max()),
                                            n_points=len(cu)))

        for fu, fv in frags:
            f_left = fu.min()
            if body_start_u is None:
                body_start_u = f_left
            elif (fu.max() - body_start_u) > cfg.max_body_width_m:
                flush()
                gu, gv = [], []
                body_start_u = f_left
            gu.append(fu); gv.append(fv)
        flush()
        return clusters

    # -- tracking on u centroid ---------------------------------------------
    def _track(self, clusters: List[GateCluster], t) -> None:
        cfg = self.cfg
        unmatched = set(range(len(clusters)))
        for tr in self._tracks:
            best, best_d = None, cfg.max_track_jump_m
            for ci in unmatched:
                d = abs(clusters[ci].u - tr.u)
                if d <= best_d:
                    best, best_d = ci, d
            if best is not None:
                tr.u = clusters[best].u
                tr.frames_seen += 1
                tr.frames_missing = 0
                unmatched.discard(best)
            else:
                tr.frames_missing += 1
        for ci in unmatched:
            self._tracks.append(_Track(id=self._next_id, u=clusters[ci].u))
            self._next_id += 1
        survivors = []
        for tr in self._tracks:
            if tr.frames_missing > cfg.max_missing_frames:
                if not tr.counted and tr.frames_seen >= cfg.min_track_frames:
                    self.count += 1
                    tr.counted = True
            else:
                survivors.append(tr)
        self._tracks = survivors


# ---------------------------------------------------------------------------
# Simulation + plot
# ---------------------------------------------------------------------------
def _person_returns(U, head_h, half_w, pose: SensorPose, cfg: GateConfig, dv=0.02, noise=0.004):
    """Raw (fan-angle_deg, range) a sensor would get from a person's near face."""
    face_u = U - half_w if pose.u0 < U else U + half_w   # face that points at this sensor
    out_a, out_r = [], []
    v = 0.0
    while v <= head_h:
        du, dvv = face_u - pose.u0, v - pose.v0
        r = math.hypot(du, dvv)
        if cfg.range_min_m < r < cfg.range_max_m:
            theta = math.degrees(math.atan2(dvv, du))
            fan = theta - pose.boresight_deg
            if abs(fan) <= cfg.fov_half_deg:
                a = fan + pose.center_angle_deg
                if pose.flip:
                    a = -a
                out_a.append(a)
                out_r.append(r + np.random.normal(0, noise))
        v += dv
    return np.asarray(out_a), np.asarray(out_r)


def _frame(people, pose, cfg):
    """Concatenate raw returns from all present people for one sensor."""
    A, R = [], []
    for (U, h, w) in people:
        a, r = _person_returns(U, h, w, pose, cfg)
        A.append(a); R.append(r)
    if A:
        return np.concatenate(A), np.concatenate(R)
    return np.array([]), np.array([])


def _demo():
    np.random.seed(5)
    W = 1.60
    cfg = GateConfig(width_m=W)
    A = SensorPose(u0=0.05,     v0=0.10, boresight_deg=55.0)
    B = SensorPose(u0=W - 0.05, v0=0.10, boresight_deg=125.0)
    counter = GateFusionCounter(cfg, A, B)

    seq = ([[]] * 3
           + [[(0.80, 1.70, 0.13)]] * 8          # one person, gate centre
           + [[]] * 5
           + [[(0.55, 1.70, 0.13), (1.05, 1.66, 0.13)]] * 8  # two side-by-side
           + [[]] * 5)

    max_clusters = 0
    log = []  # (t, u)
    for k, ppl in enumerate(seq):
        t = k / 10.0
        aa, ra = _frame(ppl, A, cfg)
        ab, rb = _frame(ppl, B, cfg)
        clusters = counter.process_dual(aa, ra, ab, rb, t=t)
        max_clusters = max(max_clusters, len(clusters))
        for c in clusters:
            log.append((t, c.u))
    counter.finalize()

    print(f"gs5_gate_fusion v{__version__}")
    print(f"gate width = {W} m, two sensors at bottom corners (40 deg up)")
    print(f"max fused clusters in a frame = {max_clusters}  (2 => side-by-side resolved)")
    print(f"TOTAL PEOPLE COUNTED: {counter.count}  (expected 3)")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ts, us = zip(*log) if log else ([], [])
        fig, ax = plt.subplots(figsize=(11, 4.2))
        ax.scatter(ts, us, s=26, color="#6b46c1", label="fused person cluster")
        ax.axhline(0, color="#cbd5e0", lw=0.8); ax.axhline(W, color="#cbd5e0", lw=0.8)
        ax.set_ylim(-0.1, W + 0.1)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("position across gate u (m)")
        ax.set_title(f"Two-GS5 gate fusion  |  counted {counter.count} "
                     f"(1 centre + 1 side-by-side pair)")
        ax.grid(alpha=0.25); ax.legend(loc="upper right", fontsize=9)
        fig.tight_layout(); fig.savefig("gate_fusion_demo.png", dpi=130)
        print("plot written to gate_fusion_demo.png")
    except ImportError:
        print("(matplotlib not installed -> skipping plot)")


if __name__ == "__main__":
    _demo()
