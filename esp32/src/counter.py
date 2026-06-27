"""
TF-Luna dual-sensor directional people counter — counting core.
====================================================================
Refactored from tf_luna_uart_v2.py (v2.0.0) into a reusable class so the
coordinator (main.py) can drive it and route events to the logger. All the
detection logic is unchanged; parameters now come from config.py.

Two TF-Luna beams along the walking path resolve direction from the ORDER
the beams break:

        walking direction  ----------->
             [Sensor A]      [Sensor B]
        A breaks first, then B  ->  IN
        B breaks first, then A  ->  OUT
"""

from machine import UART, Pin
import time


class DirectionCounter:
    """Pulse-pairing directional counter for two beams 'A' and 'B'.

    Each crossing makes one clear->blocked pulse on each beam. We keep a FIFO
    of unmatched pulses per beam; when a beam breaks we match the oldest pulse
    on the OTHER beam — the already-pending beam broke FIRST, giving direction.
    Counts several people in flight at once. A pulse with no partner within
    MATCH_WINDOW_MS (someone who broke one beam only) is expired.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.in_count = 0
        self.out_count = 0
        self._blocked = {"A": False, "B": False}
        self._pending = {"A": [], "B": []}

    def update_beam(self, name, blocked, now):
        if blocked is None:
            return None
        rising = blocked and not self._blocked[name]
        self._blocked[name] = blocked
        if not rising:
            return None

        self._expire(now)
        other = "B" if name == "A" else "A"
        if self._pending[other]:
            self._pending[other].pop(0)
            if other == self.cfg.FIRST_TO_IN:
                self.in_count += 1
                return "in"
            self.out_count += 1
            return "out"

        self._pending[name].append(now)
        return None

    def poll(self, now):
        self._expire(now)

    def _expire(self, now):
        for beam in ("A", "B"):
            q = self._pending[beam]
            while q and time.ticks_diff(now, q[0]) > self.cfg.MATCH_WINDOW_MS:
                q.pop(0)


class PeopleCounter:
    """Owns the two sensor UARTs, calibration, and the DirectionCounter.

    Lifecycle:  c = PeopleCounter(cfg); c.calibrate(); loop: c.update(now)
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.uart_a = UART(cfg.UART_A_ID, baudrate=cfg.BAUD,
                           tx=Pin(cfg.TX_A_PIN), rx=Pin(cfg.RX_A_PIN))
        self.uart_b = UART(cfg.UART_B_ID, baudrate=cfg.BAUD,
                           tx=Pin(cfg.TX_B_PIN), rx=Pin(cfg.RX_B_PIN))
        self._dir = DirectionCounter(cfg)
        self.bg_a = None
        self.bg_b = None
        self.blocked_a = False
        self.blocked_b = False
        self.buf_a = bytearray()
        self.buf_b = bytearray()
        self.last_a = -1
        self.last_b = -1

    # ---- counts ------------------------------------------------------
    @property
    def in_count(self):
        return self._dir.in_count

    @property
    def out_count(self):
        return self._dir.out_count

    @property
    def occupancy(self):
        return self._dir.in_count - self._dir.out_count

    # ---- frame parsing ----------------------------------------------
    def _parse_frame(self, frame):
        dist = frame[2] | (frame[3] << 8)
        strength = frame[4] | (frame[5] << 8)
        temp_raw = frame[6] | (frame[7] << 8)
        return {
            "distance_cm": dist,
            "strength": strength,
            "temp_c": temp_raw / 8.0 - 256.0,
        }

    def _ok(self, data):
        amp, dist = data["strength"], data["distance_cm"]
        if amp == self.cfg.AMP_OVEREXPOSE:
            return False
        if amp < self.cfg.AMP_MIN:
            return False
        if dist < self.cfg.BLIND_ZONE_CM:
            return False
        return True

    def _read_frame(self, uart):
        h = self.cfg.HEADER
        b = uart.read(1)
        if not b or b[0] != h:
            return None
        b = uart.read(1)
        if not b or b[0] != h:
            return None
        rest = uart.read(self.cfg.FRAME_LEN - 2)
        if not rest or len(rest) != self.cfg.FRAME_LEN - 2:
            return None
        frame = bytes((h, h)) + rest
        if (sum(frame[0:8]) & 0xFF) != frame[8]:
            return None
        return frame

    def _read_reading(self, uart):
        frame = self._read_frame(uart)
        if frame is None:
            return None
        data = self._parse_frame(frame)
        return data if self._ok(data) else None

    def _beam_blocked(self, data, background, currently_blocked):
        if data is None:
            return None
        dist = data["distance_cm"]
        if currently_blocked:
            return dist < background - self.cfg.RELEASE_MARGIN_CM
        return dist < background - self.cfg.BLOCK_MARGIN_CM

    # ---- calibration -------------------------------------------------
    def _calibrate_one(self, uart):
        total = 0
        valid = 0
        for _ in range(self.cfg.CALIB_SAMPLES):
            data = self._read_reading(uart)
            if data is not None:
                total += data["distance_cm"]
                valid += 1
            time.sleep_ms(10)
        return (total / valid) if valid else None

    def calibrate(self):
        """Learn each empty-lane background. Returns True if both succeeded."""
        self.bg_a = self._calibrate_one(self.uart_a)
        self.bg_b = self._calibrate_one(self.uart_b)
        return self.bg_a is not None and self.bg_b is not None

    # ---- edge draining ----------------------------------------------
    def _read_edges(self, uart, buf, background, blocked, now):
        h, flen = self.cfg.HEADER, self.cfg.FRAME_LEN
        chunk = uart.read()
        if chunk:
            buf += chunk
        if len(buf) > 256:
            buf = buf[-256:]

        frames = []
        i = 0
        n = len(buf)
        while i + flen <= n:
            if buf[i] == h and buf[i + 1] == h and (sum(buf[i:i + 8]) & 0xFF) == buf[i + 8]:
                frames.append(buf[i:i + flen])
                i += flen
            else:
                i += 1
        buf = buf[i:]

        edges = []
        last_dist = None
        k = len(frames)
        for idx, frame in enumerate(frames):
            data = self._parse_frame(frame)
            if not self._ok(data):
                continue
            last_dist = data["distance_cm"]
            nb = self._beam_blocked(data, background, blocked)
            if nb is not None and nb != blocked:
                est = time.ticks_add(now, -(k - 1 - idx) * self.cfg.FRAME_PERIOD_MS)
                edges.append((est, nb))
                blocked = nb
        return blocked, edges, last_dist, buf

    # ---- main step ---------------------------------------------------
    def update(self, now):
        """Drain both sensors and return a list of events for this step.

        Each event is (direction, in_count, out_count) where direction is
        "in" or "out". Returns [] when nothing crossed.
        """
        self.blocked_a, edges_a, da, self.buf_a = self._read_edges(
            self.uart_a, self.buf_a, self.bg_a, self.blocked_a, now)
        self.blocked_b, edges_b, db, self.buf_b = self._read_edges(
            self.uart_b, self.buf_b, self.bg_b, self.blocked_b, now)
        if da is not None:
            self.last_a = da
        if db is not None:
            self.last_b = db

        # Merge both beams' edges and process in TRUE chronological order so a
        # fast crossing is counted in the direction it physically happened.
        edges = [(t, "A", b) for (t, b) in edges_a]
        edges += [(t, "B", b) for (t, b) in edges_b]
        edges.sort(key=lambda e: time.ticks_diff(e[0], now))

        events = []
        for (t, name, blk) in edges:
            ev = self._dir.update_beam(name, blk, t)
            if ev is not None:
                events.append((ev, self.in_count, self.out_count))
        self._dir.poll(now)
        return events
