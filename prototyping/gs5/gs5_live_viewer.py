#!/usr/bin/env python3
"""
gs5_live_viewer.py — Live point-cloud visualizer for the YDLIDAR GS5 on Linux.

Uses the official YDLidar-SDK Python bindings (`import ydlidar`) to stream scans
and plots them live with matplotlib as a top-down Cartesian "fan" — which suits
the GS5's forward-facing ~80-85 deg field of view better than a full polar circle.

Requirements
------------
    # SDK (build with python + swig present BEFORE cmake, or import ydlidar fails):
    sudo apt install cmake pkg-config python3 python3-pip swig g++ git
    git clone https://github.com/YDLIDAR/YDLidar-SDK.git
    cd YDLidar-SDK && mkdir build && cd build && cmake .. && make && sudo make install
    cd .. && pip install .

    # Plotting deps:
    pip install matplotlib numpy

Usage
-----
    python3 gs5_live_viewer.py                 # auto-detect port, GS5 defaults
    python3 gs5_live_viewer.py --port /dev/ttyUSB0
    python3 gs5_live_viewer.py --baudrate 921600 --frequency 8 --max-range 1.2

Tip: if you don't want to run as root, add a udev rule or
     `sudo chmod 666 /dev/ttyUSB0` for a quick test.
"""

import argparse
import math
import signal
import sys
import time
from collections import deque

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation

try:
    import ydlidar
except ImportError:
    sys.exit(
        "ERROR: could not `import ydlidar`.\n"
        "The Python bindings weren't built. Make sure swig + python3-dev were\n"
        "installed BEFORE you ran cmake, then rebuild, or run `pip install .`\n"
        "from inside the YDLidar-SDK directory."
    )


def parse_args():
    p = argparse.ArgumentParser(description="Live YDLIDAR GS5 visualizer")
    p.add_argument("--port", default=None,
                   help="Serial port (default: auto-detect first YDLIDAR found)")
    p.add_argument("--baudrate", type=int, default=921600,
                   help="GS-series default is 921600")
    p.add_argument("--frequency", type=float, default=8.0,
                   help="Scan frequency in Hz (GS default ~8)")
    p.add_argument("--max-range", type=float, default=1.2,
                   help="Plot axis limit in metres (GS5 ranges ~0.07-1.0 m)")
    return p.parse_args()


def pick_port(requested):
    """Return a usable serial port.

    The GS5's CP2102 USB adapter always enumerates as /dev/ttyUSB* (or ttyACM*),
    while /dev/ttyS* are the host's built-in serial ports. lidarPortList() reports
    both, so prefer USB/ACM devices and never auto-pick a bare ttyS* port.
    """
    if requested:
        return requested

    detected = list(ydlidar.lidarPortList().values())  # {description: device_path}
    if not detected:
        print("No YDLIDAR auto-detected; falling back to /dev/ttyUSB0")
        return "/dev/ttyUSB0"

    usb_like = [p for p in detected if "ttyUSB" in p or "ttyACM" in p]
    chosen = usb_like[0] if usb_like else detected[0]
    print(f"Auto-detected port(s): {detected} -> using {chosen}")
    if not usb_like:
        print("  (warning: no ttyUSB*/ttyACM* found; this may be the wrong port. "
              "Pass --port explicitly if it fails.)")
    return chosen


def make_lidar(args):
    """Configure a CYdLidar handle with GS5-appropriate options."""
    laser = ydlidar.CYdLidar()
    port = pick_port(args.port)

    # GS5 is a GS-series device -> use TYPE_GS. We resolve it defensively in case
    # an older SDK build names the constant differently.
    lidar_type = getattr(ydlidar, "TYPE_GS", None)
    if lidar_type is None:
        sys.exit("This SDK build has no TYPE_GS constant. Update YDLidar-SDK; "
                 "GS5 support requires a GS-aware build.")

    laser.setlidaropt(ydlidar.LidarPropSerialPort, port)
    laser.setlidaropt(ydlidar.LidarPropSerialBaudrate, args.baudrate)
    laser.setlidaropt(ydlidar.LidarPropLidarType, lidar_type)
    laser.setlidaropt(ydlidar.LidarPropDeviceType, ydlidar.YDLIDAR_TYPE_SERIAL)
    laser.setlidaropt(ydlidar.LidarPropScanFrequency, args.frequency)
    laser.setlidaropt(ydlidar.LidarPropSampleRate, 20)
    laser.setlidaropt(ydlidar.LidarPropSingleChannel, False)
    return laser, port


def setup_plot(max_range):
    """Top-down Cartesian view: sensor at origin, beam pointing +X."""
    fig, ax = plt.subplots(figsize=(8, 6))
    fig.canvas.manager.set_window_title("YDLIDAR GS5 — Live Point Cloud")

    scatter = ax.scatter([], [], s=8, c=[], cmap="viridis", vmin=0, vmax=255)
    ax.plot(0, 0, marker="o", color="red", markersize=10, label="GS5")

    # Range rings every 0.25 m for quick distance reference.
    for r in np.arange(0.25, max_range + 0.001, 0.25):
        ax.add_patch(plt.Circle((0, 0), r, fill=False,
                                 linestyle="--", linewidth=0.5, alpha=0.4))
        ax.text(0, r, f"{r:.2f} m", fontsize=7, ha="center", alpha=0.6)

    ax.set_xlim(-max_range, max_range)
    ax.set_ylim(-0.1, max_range)
    ax.set_aspect("equal")
    ax.set_xlabel("Lateral (m)")
    ax.set_ylabel("Forward (m)")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper right")
    title = ax.set_title("Starting…")
    fig.colorbar(scatter, ax=ax, label="Intensity")
    return fig, ax, scatter, title


def main():
    args = parse_args()

    ydlidar.os_init()  # installs a SIGINT handler so Ctrl-C shuts down cleanly
    laser, port = make_lidar(args)

    if not laser.initialize():
        sys.exit(f"Failed to initialize GS5 on {port}. "
                 f"Check the cable, power (try the USB_PWR port), and permissions.")
    if not laser.turnOn():
        laser.disconnecting()
        sys.exit("Failed to start scanning (turnOn returned False).")

    print(f"Streaming from {port} @ {args.baudrate} baud. "
          f"Close the window or press Ctrl-C to stop.")

    scan = ydlidar.LaserScan()
    fig, ax, scatter, title = setup_plot(args.max_range)

    # Wall-clock timestamps of recent frames, for a stable smoothed rate readout.
    frame_times = deque(maxlen=20)

    # Clean Ctrl-C: override the SDK's signal handler (installed by os_init) with
    # one that only sets a flag. This stops Python from raising KeyboardInterrupt
    # mid-draw inside Tkinter; the animation closes the window on its next tick.
    shutdown = {"requested": False}

    def request_shutdown(_signum, _frame):
        if not shutdown["requested"]:
            shutdown["requested"] = True
            print("\nCtrl-C received — closing…")

    signal.signal(signal.SIGINT, request_shutdown)

    def update(_frame):
        if shutdown["requested"]:
            plt.close(fig)          # ends plt.show(), letting the finally block clean up
            return scatter, title

        if not (ydlidar.os_isOk() and laser.doProcessSimple(scan)):
            return scatter, title

        n = scan.points.size()
        xs, ys, intensities = [], [], []
        for i in range(n):
            pt = scan.points[i]
            r = pt.range          # metres
            if r <= 0:            # 0 = no return for that beam, skip it
                continue
            # angle is in radians; +X forward, +Y to the left
            xs.append(r * math.cos(pt.angle))
            ys.append(r * math.sin(pt.angle))
            intensities.append(getattr(pt, "intensity", 0))

        if xs:
            scatter.set_offsets(np.column_stack([ys, xs]))  # lateral=Y, forward=X
            scatter.set_array(np.array(intensities))

        # Smoothed frame rate from wall-clock spacing over the last N frames.
        # (The GS5 is solid-state, so this is a data/frame rate, not a rotation freq.)
        frame_times.append(time.perf_counter())
        if len(frame_times) >= 2:
            span = frame_times[-1] - frame_times[0]
            fps = (len(frame_times) - 1) / span if span > 0 else 0.0
        else:
            fps = 0.0

        title.set_text(f"{len(xs)} points   |   {fps:4.1f} frames/s")
        return scatter, title

    # interval is a redraw hint; the SDK call paces the actual data rate.
    _anim = animation.FuncAnimation(fig, update, interval=50,
                                    blit=False, cache_frame_data=False)

    stopped = {"done": False}

    def shutdown_lidar():
        if stopped["done"]:
            return
        stopped["done"] = True
        print("Shutting down GS5…")
        laser.turnOff()
        laser.disconnecting()

    try:
        plt.show()
    except KeyboardInterrupt:
        pass            # belt-and-suspenders: in case a SIGINT slips through
    finally:
        shutdown_lidar()


if __name__ == "__main__":
    main()
