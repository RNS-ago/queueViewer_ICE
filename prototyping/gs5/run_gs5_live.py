"""
run_gs5_live.py
===============
Drive a REAL YDLIDAR GS5 and feed its scans into GS5PeopleCounter.

This is the hardware glue between the YDLidar-SDK Python module (`ydlidar`) and
the counter in gs5_overhead_counter.py.

--------------------------------------------------------------------------
INSTALL (Ubuntu; do this once)
--------------------------------------------------------------------------
  sudo apt install cmake swig python3-dev build-essential git
  git clone https://github.com/YDLIDAR/YDLidar-SDK.git
  cd YDLidar-SDK && mkdir build && cd build
  cmake ..            # add -DCMAKE_BUILD_TYPE=Release if you like
  make -j
  sudo make install    # this ALSO installs the python `ydlidar` module
  # (alt: from YDLidar-SDK root:  pip install .   )

  # serial permissions for the USB adapter board:
  sudo usermod -aG dialout $USER      # then log out/in
  # the SDK also ships startup/initenv.sh to create a /dev/ydlidar udev symlink

  python3 -c "import ydlidar; print('ydlidar OK')"

--------------------------------------------------------------------------
WIRING / MOUNTING
--------------------------------------------------------------------------
  * GS5 -> USB Type-C adapter board -> host. It enumerates as /dev/ttyUSB0
    (or /dev/ydlidar if you ran the udev script). Baud = 921600 for GS.
  * Mount the sensor OVERHEAD with its 80 deg fan ACROSS the walking path.
  * Mount height H must satisfy:  P_max + 0.07 <= H <= P_min + 1.00
    (GS5 only sees 0.07-1.00 m, so the floor is invisible -- that's fine).

--------------------------------------------------------------------------
ALIGNING THE ANGLE FRAME (do this once, with --calibrate)
--------------------------------------------------------------------------
  The SDK reports each point's angle in radians in the SENSOR frame. The
  counter wants angle 0 = straight down, +/- across the path. Run:
      python3 run_gs5_live.py --calibrate
  ...place a small object directly under the sensor; the printed CENTER_ANGLE
  is the SDK angle of the nearest point. Put that into --center-angle. If
  "left" people show up on the wrong side, add --flip.

--------------------------------------------------------------------------
VERSIONING
  git log --oneline ; git checkout <commit> -- run_gs5_live.py
  __version__ = "2.1.0"
  CHANGELOG
    2.1.0  Add live GS5 hardware runner (YDLidar-SDK glue) + angle calibration.
    2.0.0  GS5 2D counter core (gs5_overhead_counter.py).
    1.0.0  Single-beam counter (overhead_lidar_counter.py).
--------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import time

import numpy as np

try:
    import ydlidar
except ImportError:
    ydlidar = None  # so the file still imports/reads without the SDK present

from gs5_overhead_counter import GS5Config, GS5PeopleCounter

__version__ = "2.1.0"

GS_BAUDRATE = 921600


# ---------------------------------------------------------------------------
# Sensor setup
# ---------------------------------------------------------------------------
def make_lidar(port: str | None, scan_freq_hz: float):
    if ydlidar is None:
        raise RuntimeError("`ydlidar` module not found -- build & install YDLidar-SDK first.")
    ydlidar.os_init()
    laser = ydlidar.CYdLidar()

    if port is None:
        ports = ydlidar.lidarPortList()
        port = "/dev/ttyUSB0"
        for _, value in ports.items():
            port = value  # take the last enumerated YDLidar port
    print(f"[gs5] using port {port}")

    laser.setlidaropt(ydlidar.LidarPropSerialPort, port)
    laser.setlidaropt(ydlidar.LidarPropSerialBaudrate, GS_BAUDRATE)
    laser.setlidaropt(ydlidar.LidarPropLidarType, ydlidar.TYPE_GS)        # <-- GS family
    laser.setlidaropt(ydlidar.LidarPropDeviceType, ydlidar.YDLIDAR_TYPE_SERIAL)
    laser.setlidaropt(ydlidar.LidarPropScanFrequency, float(scan_freq_hz))
    laser.setlidaropt(ydlidar.LidarPropSampleRate, 4)
    laser.setlidaropt(ydlidar.LidarPropSingleChannel, False)
    laser.setlidaropt(ydlidar.LidarPropIntenstiy, False)
    laser.setlidaropt(ydlidar.LidarPropMaxRange, 1.0)
    laser.setlidaropt(ydlidar.LidarPropMinRange, 0.05)
    return laser, port


def scan_to_arrays(scan, center_angle_deg: float, flip: bool):
    """Convert a LaserScan into (counter_angles_deg, ranges_m) for the counter."""
    pts = scan.points
    n = pts.size()
    ang = np.empty(n, dtype=float)
    rng = np.empty(n, dtype=float)
    for i in range(n):
        p = pts[i]
        ang[i] = p.angle      # radians, sensor frame
        rng[i] = p.range      # metres (0.0 == no return)
    ang_deg = np.degrees(ang) - center_angle_deg
    if flip:
        ang_deg = -ang_deg
    return ang_deg, rng


# ---------------------------------------------------------------------------
# Calibration helper
# ---------------------------------------------------------------------------
def calibrate(port, scan_freq_hz):
    laser, _ = make_lidar(port, scan_freq_hz)
    if not laser.initialize() or not laser.turnOn():
        print("[gs5] init/turnOn failed"); return
    scan = ydlidar.LaserScan()
    print("[gs5] place an object directly under the sensor. Reading 30 frames...")
    centers = []
    for _ in range(30):
        if laser.doProcessSimple(scan):
            n = scan.points.size()
            if n:
                best_i = min(range(n), key=lambda i: scan.points[i].range or 1e9)
                centers.append(np.degrees(scan.points[best_i].angle))
        time.sleep(0.03)
    laser.turnOff(); laser.disconnecting()
    if centers:
        print(f"[gs5] CENTER_ANGLE = {np.median(centers):.2f} deg  "
              f"-> pass with --center-angle {np.median(centers):.2f}")
    else:
        print("[gs5] no points received -- check wiring/permissions.")


# ---------------------------------------------------------------------------
# Live counting loop
# ---------------------------------------------------------------------------
def run(args):
    cfg = GS5Config(
        mount_height_m=args.mount_height,
        frame_rate_hz=args.scan_freq,
        min_person_height_m=args.min_height,
    )
    counter = GS5PeopleCounter(cfg)
    laser, _ = make_lidar(args.port, args.scan_freq)

    if not laser.initialize():
        print("[gs5] initialize() failed -- wrong port/baud or no permissions."); return
    if not laser.turnOn():
        print("[gs5] turnOn() failed."); return

    scan = ydlidar.LaserScan()
    last_count = 0
    print("[gs5] counting... Ctrl+C to stop.")
    try:
        while ydlidar.os_isOk():
            if not laser.doProcessSimple(scan):
                time.sleep(0.005)
                continue
            angles_deg, ranges_m = scan_to_arrays(scan, args.center_angle, args.flip)
            counter.process_frame(angles_deg, ranges_m, t=time.monotonic())
            if counter.count != last_count:
                last_count = counter.count
                print(f"[gs5] people so far: {counter.count}")
    except KeyboardInterrupt:
        pass
    finally:
        counter.finalize()
        laser.turnOff()
        laser.disconnecting()
        print(f"[gs5] FINAL COUNT: {counter.count}")


def main():
    ap = argparse.ArgumentParser(description="Live people counter on a YDLIDAR GS5.")
    ap.add_argument("--port", default=None, help="serial port, e.g. /dev/ttyUSB0 (auto if omitted)")
    ap.add_argument("--scan-freq", type=float, default=10.0, dest="scan_freq")
    ap.add_argument("--mount-height", type=float, default=2.20, dest="mount_height")
    ap.add_argument("--min-height", type=float, default=1.20, dest="min_height")
    ap.add_argument("--center-angle", type=float, default=0.0, dest="center_angle",
                    help="SDK angle (deg) that points straight down (see --calibrate)")
    ap.add_argument("--flip", action="store_true", help="mirror the lateral axis")
    ap.add_argument("--calibrate", action="store_true", help="find CENTER_ANGLE and exit")
    args = ap.parse_args()

    if args.calibrate:
        calibrate(args.port, args.scan_freq)
    else:
        run(args)


if __name__ == "__main__":
    main()
