"""
People counter — top-level coordinator (MicroPython / ESP32).
====================================================================
Boot order:
  1. Try to bring up WiFi (optional — counting works offline too).
  2. Build the logger (online push + local store-and-forward fallback).
  3. Calibrate the two TF-Luna beams against the empty lane.
  4. Run the loop: read sensors and keep the in/out tallies; once per
     minute log a snapshot (in, out, occupancy) and retry buffered records.

All tunables live in config.py. Drop this whole src/ folder on the device;
MicroPython runs main.py automatically at boot.
"""

import time

import config as cfg
import wifi_manager
from logger import Logger
from counter import PeopleCounter


def boot():
    print("=" * 52)
    print("People counter '{}' starting...".format(cfg.DEVICE_ID))
    print("=" * 52)

    # 1. WiFi (best effort — never blocks counting)
    online = wifi_manager.connect(
        cfg.WIFI_SSID, cfg.WIFI_PASSWORD,
        timeout=cfg.WIFI_CONNECT_TIMEOUT, retries=cfg.WIFI_RETRIES)
    if online and cfg.WIFI_SYNC_TIME:
        wifi_manager.sync_time()
    if not online:
        print("Running OFFLINE — counts buffered locally, sent when WiFi returns.")

    # 2. Logger (handles online push + local fallback)
    logger = Logger(cfg, wifi_manager)

    # 3. Sensors + calibration
    counter = PeopleCounter(cfg)
    print("Calibrating backgrounds (keep the lane clear)...")
    if not counter.calibrate():
        print("ERROR: no valid background. Check sensor wiring/aim, then reset.")
        return None
    print("Background A: {} cm   Background B: {} cm".format(counter.bg_a, counter.bg_b))
    print("Ready. Counting...")
    return logger, counter


def run(logger, counter):
    next_snapshot = time.ticks_add(time.ticks_ms(), cfg.SNAPSHOT_EVERY * 1000)
    next_flush = time.ticks_add(time.ticks_ms(), cfg.FLUSH_EVERY * 1000)
    last_debug = time.ticks_ms()

    while True:
        now = time.ticks_ms()

        # --- counting only (crossings are tallied, not logged per event) ---
        for direction, in_count, out_count in counter.update(now):
            if cfg.DEBUG:
                tag = " IN " if direction == "in" else " OUT"
                print(" >{}  in={} out={} occupancy={}".format(
                    tag, in_count, out_count, in_count - out_count))

        # --- periodic snapshot: log in/out/occupancy once per minute ---
        if time.ticks_diff(now, next_snapshot) >= 0:
            next_snapshot = time.ticks_add(now, cfg.SNAPSHOT_EVERY * 1000)
            logger.record("snapshot", counter.in_count, counter.out_count)

        # --- periodic retry of the offline buffer ---
        if time.ticks_diff(now, next_flush) >= 0:
            next_flush = time.ticks_add(now, cfg.FLUSH_EVERY * 1000)
            logger.flush()

        # --- live debug readout ---
        if cfg.DEBUG and time.ticks_diff(now, last_debug) >= cfg.DEBUG_PERIOD_MS:
            last_debug = now
            print("A={:>4}cm {}   B={:>4}cm {}".format(
                counter.last_a, "[X]" if counter.blocked_a else "[ ]",
                counter.last_b, "[X]" if counter.blocked_b else "[ ]"))


def main():
    started = boot()
    if started is None:
        return
    logger, counter = started
    try:
        run(logger, counter)
    except KeyboardInterrupt:
        # Log a final snapshot on manual stop, regardless of the snapshot timer,
        # so short runs (< SNAPSHOT_EVERY) still leave a record.
        logger.record("snapshot", counter.in_count, counter.out_count)
        print("\nStopped. Final: in={} out={} occupancy={}".format(
            counter.in_count, counter.out_count, counter.occupancy))


if __name__ == "__main__":
    main()
