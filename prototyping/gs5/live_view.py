"""
live_view.py
============
Real-time visualizer for the GS5 people counter. Two panels:

  TOP  - "cloud": the current frame's points + the clusters detected this frame.
         Watch this to verify calibration/mounting and see people as point blobs.
  BOTTOM - "tracks": position-across vs time, scrolling left. This is the LIVE
         version of the static plots -- each horizontal streak is one person.

A big running count sits on top.

Runs from a built-in SIMULATOR (default, no hardware needed) or from the real
GS5(s). Works for Mode A (single overhead) and Mode B (two-side fusion).

EXAMPLES
  # watch it now, no hardware (simulated stream):
  python3 live_view.py --mode A --source sim
  python3 live_view.py --mode B --source sim

  # save a short preview clip instead of opening a window:
  python3 live_view.py --mode A --source sim --save-gif preview.gif --frames 80

  # live on hardware:
  python3 live_view.py --mode A --source live --port-a /dev/ttyUSB0 --center-a <deg>
  python3 live_view.py --mode B --source live \
      --port-a /dev/ttyUSB0 --center-a <A> --port-b /dev/ttyUSB1 --center-b <B> --width 1.6

VERSIONING: git log --oneline ; __version__ = "3.1.0"
  3.1.0  Live two-panel visualizer (cloud + scrolling tracks) + simulator.
"""

from __future__ import annotations

import argparse
import time
from collections import deque

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from gs5_overhead_counter import (GS5Config, GS5PeopleCounter,
                                  frame_to_cartesian, _make_beams,
                                  _frame_with_people)
from gs5_gate_fusion import (GateConfig, GateFusionCounter, SensorPose,
                             sensor_points_to_gate, _frame as fusion_frame)

try:
    import ydlidar
except ImportError:
    ydlidar = None

__version__ = "3.1.0"

BLUE, PURPLE, GREY = "#2b6cb0", "#6b46c1", "#cbd5e0"


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------
class SimStream:
    """Synthesizes a lively stream of passes for either mode."""
    def __init__(self, mode, cfg, extra, seed=1):
        self.mode, self.cfg, self.extra = mode, cfg, extra
        self.rng = np.random.default_rng(seed)
        self.active = []          # [pos, h0, h1, half, total, elapsed]
        self.cooldown = 0
        self.k = 0

    def _person(self, pos):
        # head height ramps over the pass: rising = nearing the sensor (entry),
        # falling = receding (exit). ~70% enter, ~30% leave.
        h0 = float(self.rng.uniform(1.55, 1.80))
        h1 = h0 + 0.22 if self.rng.random() > 0.3 else h0 - 0.22
        return [pos, h0, h1, 0.13, int(self.rng.integers(6, 11)), 0]

    def _maybe_spawn(self):
        if self.cooldown > 0:
            self.cooldown -= 1
            return
        if self.active or self.rng.random() > 0.14:
            return
        if self.mode == "A":
            lo, hi = -0.30, 0.30
        else:
            lo, hi = 0.25, self.cfg.width_m - 0.25
        if self.rng.random() < 0.4:                 # side-by-side pair
            c = float(self.rng.uniform(lo + 0.20, hi - 0.20))
            self.active = [self._person(c - 0.28), self._person(c + 0.28)]
        else:                                       # single
            self.active = [self._person(float(self.rng.uniform(lo, hi)))]
        self.cooldown = int(self.rng.integers(9, 18))

    def step(self):
        self._maybe_spawn()
        people = []
        for pos, h0, h1, half, total, elapsed in self.active:
            frac = min(1.0, elapsed / max(1, total - 1))
            people.append((pos, h0 + (h1 - h0) * frac, half))   # current head height
        if self.mode == "A":
            ranges = _frame_with_people(self.extra["beams"], people, self.cfg)
            out = ("A", self.extra["beams"], ranges)
        else:
            aa, ra = fusion_frame(people, self.extra["pose_a"], self.cfg)
            ab, rb = fusion_frame(people, self.extra["pose_b"], self.cfg)
            out = ("B", aa, ra, ab, rb)
        for p in self.active:
            p[5] += 1
        self.active = [p for p in self.active if p[5] < p[4]]
        self.k += 1
        return out


class LidarStream:
    """Reads real GS5(s). mode 'A' -> one laser; 'B' -> two."""
    def __init__(self, mode, lasers):
        self.mode = mode
        self.lasers = lasers
        self.scans = [ydlidar.LaserScan() for _ in lasers]
        self._last = None

    @staticmethod
    def _arrays(scan):
        n = scan.points.size()
        a = np.empty(n); r = np.empty(n)
        for i in range(n):
            a[i] = np.degrees(scan.points[i].angle)
            r[i] = scan.points[i].range
        return a, r

    def step(self):
        if self.mode == "A":
            if self.lasers[0].doProcessSimple(self.scans[0]):
                a, r = self._arrays(self.scans[0])
                self._last = ("A", a, r)
        else:
            got = [l.doProcessSimple(s) for l, s in zip(self.lasers, self.scans)]
            if any(got):
                aa, ra = self._arrays(self.scans[0])
                ab, rb = self._arrays(self.scans[1])
                self._last = ("B", aa, ra, ab, rb)
        return self._last


def _make_lidar(port, scan_freq):
    laser = ydlidar.CYdLidar()
    laser.setlidaropt(ydlidar.LidarPropSerialPort, port)
    laser.setlidaropt(ydlidar.LidarPropSerialBaudrate, 921600)
    laser.setlidaropt(ydlidar.LidarPropLidarType, ydlidar.TYPE_GS)
    laser.setlidaropt(ydlidar.LidarPropDeviceType, ydlidar.YDLIDAR_TYPE_SERIAL)
    laser.setlidaropt(ydlidar.LidarPropScanFrequency, float(scan_freq))
    laser.setlidaropt(ydlidar.LidarPropSampleRate, 4)
    laser.setlidaropt(ydlidar.LidarPropSingleChannel, False)
    laser.setlidaropt(ydlidar.LidarPropIntenstiy, False)
    laser.setlidaropt(ydlidar.LidarPropMaxRange, 1.0)
    laser.setlidaropt(ydlidar.LidarPropMinRange, 0.05)
    if not laser.initialize() or not laser.turnOn():
        raise RuntimeError(f"GS5 on {port} failed to start")
    return laser


# ---------------------------------------------------------------------------
# Visualizer
# ---------------------------------------------------------------------------
class LiveView:
    def __init__(self, args):
        self.args = args
        self.mode = args.mode
        self.dt = 1.0 / args.scan_freq
        self.window_s = args.seconds
        self.history = deque()            # (t, pos) of detected clusters
        self.t0 = time.monotonic()

        if self.mode == "A":
            self.cfg = GS5Config(mount_height_m=args.mount_height,
                                 frame_rate_hz=args.scan_freq)
            self.counter = GS5PeopleCounter(self.cfg)
            self.extra = {"beams": _make_beams(self.cfg)}
            self.pos_label = "lateral position across path (m)"
            self.pos_lim = (-0.7, 0.7)
            self.cloud_xlim, self.cloud_ylim = (-0.7, 0.7), (0.0, 2.3)
            self.cloud_xlabel, self.cloud_ylabel = "lateral x (m)", "height above floor (m)"
        else:
            W = args.width
            self.cfg = GateConfig(width_m=W)
            self.pose_a = SensorPose(args.ua, args.va, args.ba,
                                     center_angle_deg=args.center_a, flip=args.flip_a)
            self.pose_b = SensorPose(args.ub if args.ub is not None else W - 0.05,
                                     args.vb, args.bb,
                                     center_angle_deg=args.center_b, flip=args.flip_b)
            self.counter = GateFusionCounter(self.cfg, self.pose_a, self.pose_b)
            self.extra = {"pose_a": self.pose_a, "pose_b": self.pose_b}
            self.pos_label = "position across gate u (m)"
            self.pos_lim = (0.0, W)
            self.cloud_xlim, self.cloud_ylim = (0.0, W), (0.0, 2.0)
            self.cloud_xlabel, self.cloud_ylabel = "across gate u (m)", "height v (m)"

        self.stream = self._make_stream()

        self.fig, (self.ax_cloud, self.ax_track) = plt.subplots(
            2, 1, figsize=(11, 7), gridspec_kw={"height_ratios": [1.1, 1.0]})
        self.fig.subplots_adjust(top=0.90, hspace=0.35)

    def _make_stream(self):
        if self.args.source == "sim":
            return SimStream(self.mode, self.cfg, self.extra, seed=self.args.seed)
        if ydlidar is None:
            raise RuntimeError("`ydlidar` not installed; use --source sim.")
        ydlidar.os_init()
        if self.mode == "A":
            lasers = [_make_lidar(self.args.port_a, self.args.scan_freq)]
        else:
            lasers = [_make_lidar(self.args.port_a, self.args.scan_freq),
                      _make_lidar(self.args.port_b, self.args.scan_freq)]
        return LidarStream(self.mode, lasers)

    def _now(self):
        return self.stream.k * self.dt if self.args.source == "sim" \
            else time.monotonic() - self.t0

    def update(self, _frame):
        f = self.stream.step()
        if f is None:
            return
        t = self._now()

        # --- process + collect points/clusters ---
        if f[0] == "A":
            _, beams, ranges = f
            x, h, valid = frame_to_cartesian(beams, ranges, self.cfg)
            clusters = self.counter.process_frame(beams, ranges, t=t)
            cloud = [(x[valid], h[valid], BLUE)]
            marks = [(c.x, c.height, c.height, c.height) for c in clusters]
        else:
            _, aa, ra, ab, rb = f
            ua, va, oka = sensor_points_to_gate(aa, ra, self.pose_a, self.cfg)
            ub, vb, okb = sensor_points_to_gate(ab, rb, self.pose_b, self.cfg)
            clusters = self.counter.process_dual(aa, ra, ab, rb, t=t)
            cloud = [(ua[oka], va[oka], BLUE), (ub[okb], vb[okb], PURPLE)]
            marks = [(c.u, (c.v_lo + c.v_hi) / 2, c.v_lo, c.v_hi) for c in clusters]

        for m in marks:
            self.history.append((t, m[0]))
        while self.history and self.history[0][0] < t - self.window_s:
            self.history.popleft()

        # --- cloud panel ---
        self.ax_cloud.clear()
        for xs, ys, col in cloud:
            self.ax_cloud.scatter(xs, ys, s=14, color=col, alpha=0.8)
        for (px, py, lo, hi) in marks:
            self.ax_cloud.axvline(px, color="#e53e3e", lw=1.2, alpha=0.7)
            self.ax_cloud.plot([px], [py], marker="o", ms=11, mfc="none",
                               mec="#e53e3e", mew=2)
        self.ax_cloud.set_xlim(*self.cloud_xlim)
        self.ax_cloud.set_ylim(*self.cloud_ylim)
        self.ax_cloud.set_xlabel(self.cloud_xlabel)
        self.ax_cloud.set_ylabel(self.cloud_ylabel)
        self.ax_cloud.set_title("LIVE cloud  —  points this frame (dots) + detected people (red)",
                                fontsize=10)
        self.ax_cloud.grid(alpha=0.2)

        # --- track panel ---
        self.ax_track.clear()
        if self.history:
            ts, ps = zip(*self.history)
            self.ax_track.scatter(ts, ps, s=22, color=PURPLE)
        self.ax_track.set_xlim(max(0, t - self.window_s), max(self.window_s, t))
        self.ax_track.set_ylim(*self.pos_lim)
        self.ax_track.set_xlabel("time (s)  →")
        self.ax_track.set_ylabel(self.pos_label)
        self.ax_track.set_title("each streak = one person passing", fontsize=10)
        self.ax_track.grid(alpha=0.2)

        self.fig.suptitle(f"People counted: {self.counter.count}",
                          fontsize=18, weight="bold")

    def run(self):
        frames = self.args.frames if self.args.save_gif else None
        anim = FuncAnimation(self.fig, self.update, frames=frames,
                             interval=int(self.dt * 1000), blit=False,
                             cache_frame_data=False)
        if self.args.save_gif:
            from matplotlib.animation import PillowWriter
            anim.save(self.args.save_gif, writer=PillowWriter(fps=self.args.scan_freq))
            print(f"[live] saved {self.args.save_gif}")
        else:
            plt.show()
        return anim


def main():
    ap = argparse.ArgumentParser(description="Live visualizer for the GS5 people counter.")
    ap.add_argument("--mode", choices=["A", "B"], default="A")
    ap.add_argument("--source", choices=["sim", "live"], default="sim")
    ap.add_argument("--scan-freq", type=float, default=10.0, dest="scan_freq")
    ap.add_argument("--seconds", type=float, default=8.0, help="track-window length")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--save-gif", default=None, dest="save_gif")
    ap.add_argument("--frames", type=int, default=80, help="frames when saving a gif")
    # Mode A
    ap.add_argument("--mount-height", type=float, default=2.20, dest="mount_height")
    ap.add_argument("--port-a", default="/dev/ttyUSB0", dest="port_a")
    ap.add_argument("--center-a", type=float, default=0.0, dest="center_a")
    ap.add_argument("--flip-a", action="store_true", dest="flip_a")
    # Mode B extras
    ap.add_argument("--width", type=float, default=1.60)
    ap.add_argument("--port-b", default="/dev/ttyUSB1", dest="port_b")
    ap.add_argument("--center-b", type=float, default=0.0, dest="center_b")
    ap.add_argument("--flip-b", action="store_true", dest="flip_b")
    ap.add_argument("--ua", type=float, default=0.05)
    ap.add_argument("--va", type=float, default=0.10)
    ap.add_argument("--ba", type=float, default=55.0)
    ap.add_argument("--ub", type=float, default=None)
    ap.add_argument("--vb", type=float, default=0.10)
    ap.add_argument("--bb", type=float, default=125.0)
    args = ap.parse_args()

    if args.save_gif:
        matplotlib.use("Agg")
    LiveView(args).run()


if __name__ == "__main__":
    main()
