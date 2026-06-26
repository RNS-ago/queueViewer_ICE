"""
TF-Luna LiDAR - Dual-sensor directional people counter (MicroPython)
====================================================================
Counts people IN/OUT through an entrance using TWO Benewake TF-Luna
single-point LiDARs mounted side by side ALONG the walking path.

Each sensor is a "beam": when a person passes, the measured distance
drops well below the calibrated background. Two beams separated along
the path let us resolve direction from the ORDER the beams break:

        walking direction  ----------->
             [Sensor A]      [Sensor B]
                 |               |
        A breaks first, then B  ->  IN
        B breaks first, then A  ->  OUT

This is a beam-break direction counter, not a literal queue: we never
block the loop waiting for the other sensor. We timestamp each beam's
clear->blocked edge and compare the order within a time window. A
person who trips only one beam (reaches in, retreats) is discarded.

Sources of truth:
  - Waveshare wiki: https://www.waveshare.com/wiki/TF-Luna_LiDAR_Range_Sensor
  - Benewake "SJ-PM-TF-Luna A05" Product Manual

Version history
---------------
v1.0.0  (2026-06-24)  Initial single-sensor reader.
v1.1.0  (2026-06-24)  Verified against Waveshare wiki + Benewake manual.
v2.0.0  (2026-06-25)  Dual-sensor directional people counter.
                      - Two independent UART peripherals (was a port collision:
                        both sensors shared UART2 / the same pins).
                      - Hysteretic beam-break detection vs calibrated background.
                      - First-break direction state machine with debounce.
                      - Removed unused import from v1.

Sensor protocol (from manual, default config)
---------------------------------------------
  Interface : UART, 115200 baud, 8 data bits, 1 stop bit, no parity
  Output    : 9-byte/cm frame, default 100 Hz (1-250 Hz adjustable)
  Frame     : [0]0x59 [1]0x59 [2]Dist_L [3]Dist_H [4]Amp_L [5]Amp_H
              [6]Temp_L [7]Temp_H [8]Checksum
  Distance  : Dist_L | Dist_H<<8   -> centimetres
  Strength  : Amp_L  | Amp_H<<8    -> signal amplitude (Amp)
  Temp (C)  : (Temp_L | Temp_H<<8) / 8 - 256
  Checksum  : low 8 bits of the sum of bytes 0..7

Validity rules (from manual):
  - Amp < 100             -> distance unreliable
  - Amp == 65535 (0xFFFF) -> overexposure, distance invalid
  - Amp  > 32768          -> ambient-light overexposure detected
  - Distance < 20 cm      -> inside the 20 cm blind zone, unreliable

Wiring - Raspberry Pi Pico (RP2040 has TWO UARTs: UART0 and UART1)
-----------------------------------------------------------------
  IMPORTANT: edit the pin/UART config below to match YOUR wiring.
  An RP2040 only has UART0 and UART1 - there is no "UART2".

  Sensor A (UART0): Pico GP0 (TX) -> A pin2 RXD,  GP1 (RX) <- A pin3 TXD
  Sensor B (UART1): Pico GP4 (TX) -> B pin2 RXD,  GP5 (RX) <- B pin3 TXD
  Both sensors: pin1 +5V -> VBUS (3.7-5.2 V, NOT 3.3 V), pin4 GND -> GND.
  Pin 5 (config): leave open / tie 3.3 V for UART mode (GND selects I2C).
"""

from machine import UART, Pin
import time

# ---- Sensor UART configuration --------------------------------------
# Two SEPARATE UART peripherals. Adjust pins to match your wiring.
BAUD      = 115200  # TF-Luna default
FRAME_LEN = 9       # 9-byte/cm default output frame
HEADER    = 0x59    # frame header byte (appears twice)

UART_A_ID, TX_A_PIN, RX_A_PIN = 1, 17, 18   # Sensor A -> UART0, GP0/GP1
UART_B_ID, TX_B_PIN, RX_B_PIN = 2, 4, 5   # Sensor B -> UART1, GP4/GP5

# ---- Reading-validity thresholds (from manual) ----------------------
BLIND_ZONE_CM   = 20     # distances below this are unreliable
AMP_MIN         = 100    # below this -> unreliable
AMP_OVEREXPOSE  = 65535  # 0xFFFF -> overexposure / invalid
AMP_AMBIENT     = 32768  # above this -> ambient light overexposure

# ---- People-counting tuning -----------------------------------------
# A beam is "blocked" when distance drops at least BLOCK_MARGIN_CM below
# the calibrated background. Hysteresis: once blocked, it stays blocked
# until the distance recovers to within RELEASE_MARGIN_CM of background.
BLOCK_MARGIN_CM     = 40     # drop needed to call a beam blocked
RELEASE_MARGIN_CM   = 25     # recovery needed to call it clear again
MATCH_WINDOW_MS     = 1200   # max time between a person's two beam breaks;
                             # a pulse unmatched this long is discarded
FRAME_PERIOD_MS     = 10     # sensor output period (100 Hz default); used to
                             # back-date edges by their position in the stream

# Direction mapping: which first-broken beam means "IN".
FIRST_TO_IN = "A"            # A-then-B -> IN; B-then-A -> OUT

# Debug: print live distance + blocked state so you can see beams break.
# Set False once counting works to quiet the shell.
DEBUG = True
DEBUG_PERIOD_MS = 250        # how often to print the live readout

# ---- ANSI colour output ---------------------------------------------
# Works in mpremote / picocom / screen on Linux. Set False for plain text
# (e.g. Thonny's shell, which doesn't render ANSI escapes).
USE_COLOR = True

_ANSI = {
    "reset": "\x1b[0m", "bold": "\x1b[1m", "dim": "\x1b[2m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "cyan": "\x1b[36m", "grey": "\x1b[90m",
}


def col(text, *styles):
    """Wrap text in ANSI styles when USE_COLOR is on, else return as-is."""
    if not USE_COLOR:
        return text
    return "".join(_ANSI[s] for s in styles) + text + _ANSI["reset"]


uart_a = UART(UART_A_ID, baudrate=BAUD, tx=Pin(TX_A_PIN), rx=Pin(RX_A_PIN))
uart_b = UART(UART_B_ID, baudrate=BAUD, tx=Pin(TX_B_PIN), rx=Pin(RX_B_PIN))


def read_frame(uart):
    """Return one validated 9-byte frame, or None.

    Synchronises on the 0x59 0x59 header and verifies the checksum.
    Returns None when no complete/valid frame is available right now.
    """
    b = uart.read(1)
    if not b or b[0] != HEADER:
        return None

    b = uart.read(1)
    if not b or b[0] != HEADER:
        return None

    rest = uart.read(FRAME_LEN - 2)
    if not rest or len(rest) != FRAME_LEN - 2:
        return None

    frame = bytes((HEADER, HEADER)) + rest

    # Checksum = low 8 bits of the sum of the first 8 bytes
    if (sum(frame[0:8]) & 0xFF) != frame[8]:
        return None

    return frame


def parse_frame(frame):
    """Decode a validated frame into a dict of readings."""
    dist     = frame[2] | (frame[3] << 8)      # cm
    strength = frame[4] | (frame[5] << 8)      # signal amplitude (Amp)
    temp_raw = frame[6] | (frame[7] << 8)
    temp_c   = temp_raw / 8.0 - 256.0          # C (chip temperature)

    return {
        "distance_cm": dist,
        "strength": strength,
        "temp_c": temp_c,
    }


def reading_status(data):
    """Classify a reading per the manual's validity rules."""
    amp  = data["strength"]
    dist = data["distance_cm"]

    if amp == AMP_OVEREXPOSE:
        return "invalid (overexposed)"
    if amp < AMP_MIN:
        return "weak (amp<100)"
    if dist < BLIND_ZONE_CM:
        return "blind zone (<20cm)"
    if amp > AMP_AMBIENT:
        return "ok (ambient glare)"
    return "ok"


def read_reading(uart):
    """Read one frame and return a usable reading dict, or None.

    None means "no fresh, trustworthy distance right now" - the caller
    should keep the sensor's previous state rather than guess.
    """
    frame = read_frame(uart)
    if frame is None:
        return None
    data = parse_frame(frame)
    if not reading_status(data).startswith("ok"):
        return None
    return data


def read_edges(uart, buf, background, blocked, now):
    """Drain the UART buffer and return every beam-state transition, TIMESTAMPED.

    The TF-Luna streams ~100 frames/sec (one per FRAME_PERIOD_MS). If we only
    looked at the newest frame, two beams that break within one loop pass would
    be ordered by the loop's read order, not by reality - so a fast crossing
    gets the wrong direction. Instead we parse every buffered frame IN ORDER and
    estimate when each transition happened from its position in the stream
    (the last frame ~= now, each earlier frame ~FRAME_PERIOD_MS older). Sorting
    A's and B's edges by these timestamps recovers the true crossing order.

    Returns (new_blocked, edges, last_dist, buf):
      new_blocked : beam's blocked state after processing the buffer
      edges       : list of (est_time_ms, is_blocked) transitions, in order
      last_dist   : most recent valid distance (for the debug readout), or None
      buf         : leftover trailing bytes for the next call
    """
    chunk = uart.read()           # read ALL available bytes (None if empty)
    if chunk:
        buf += chunk
    if len(buf) > 256:
        buf = buf[-256:]

    # Collect complete, valid frames in arrival order.
    frames = []
    i = 0
    n = len(buf)
    while i + FRAME_LEN <= n:
        if (buf[i] == HEADER and buf[i + 1] == HEADER and
                (sum(buf[i:i + 8]) & 0xFF) == buf[i + 8]):
            frames.append(buf[i:i + FRAME_LEN])
            i += FRAME_LEN
        else:
            i += 1
    buf = buf[i:]                 # trailing partial frame -> next call

    edges = []
    last_dist = None
    k = len(frames)
    for idx, frame in enumerate(frames):
        data = parse_frame(frame)
        if not reading_status(data).startswith("ok"):
            continue
        last_dist = data["distance_cm"]
        nb = beam_blocked(data, background, blocked)
        if nb is not None and nb != blocked:
            # Newest frame is ~now; each earlier frame is ~FRAME_PERIOD_MS older.
            est = time.ticks_add(now, -(k - 1 - idx) * FRAME_PERIOD_MS)
            edges.append((est, nb))
            blocked = nb

    return blocked, edges, last_dist, buf


def calibrate_background(uart, samples=100):
    """Average valid 'ok' readings to estimate the empty-lane distance."""
    total = 0
    valid = 0
    for _ in range(samples):
        data = read_reading(uart)
        if data is not None:
            total += data["distance_cm"]
            valid += 1
        time.sleep_ms(10)
    return (total / valid) if valid else None


def beam_blocked(data, background, currently_blocked):
    """Hysteretic beam-break test.

    Returns the new blocked state, or None when there's no fresh reading
    (keep the previous state). Once blocked, a beam stays blocked until the
    distance recovers to within RELEASE_MARGIN_CM of background - this keeps
    a stationary person from flickering between counted/not-counted.
    """
    if data is None:
        return None
    dist = data["distance_cm"]
    if currently_blocked:
        return dist < background - RELEASE_MARGIN_CM
    return dist < background - BLOCK_MARGIN_CM


class DirectionCounter:
    """Pulse-pairing directional counter for two beams 'A' and 'B'.

    Each person crossing makes one pulse (clear->blocked edge) on each beam.
    We keep a FIFO of unmatched pulse timestamps per beam. When a beam
    breaks, we try to match the oldest unmatched pulse on the OTHER beam:
    a match = one person, and the already-pending beam is the one that
    broke FIRST, which gives direction. Unlike a single-crossing state
    machine, this counts MULTIPLE people in flight at once - two people
    side by side that each break both beams produce two pairs = two counts.

    A pulse that never finds a partner within MATCH_WINDOW_MS (someone who
    broke one beam but not the other) is expired and discarded.
    """

    def __init__(self):
        self.in_count = 0
        self.out_count = 0
        self._blocked = {"A": False, "B": False}
        self._pending = {"A": [], "B": []}   # unmatched pulse timestamps

    def update_beam(self, name, blocked, now):
        """Apply a beam state. On a rising edge, pair or enqueue a pulse.

        Returns "IN"/"OUT" when a pair completes (one person), else None.
        """
        if blocked is None:
            return None
        rising = blocked and not self._blocked[name]
        self._blocked[name] = blocked
        if not rising:
            return None

        self._expire(now)
        other = "B" if name == "A" else "A"
        if self._pending[other]:
            # The other beam already broke -> it broke first -> direction.
            self._pending[other].pop(0)
            if other == FIRST_TO_IN:
                self.in_count += 1
                return "IN"
            self.out_count += 1
            return "OUT"

        # No partner yet; wait for the other beam to break.
        self._pending[name].append(now)
        return None

    def poll(self, now):
        """Call each loop to discard stale single-beam pulses."""
        self._expire(now)
        return None

    def _expire(self, now):
        for beam in ("A", "B"):
            q = self._pending[beam]
            while q and time.ticks_diff(now, q[0]) > MATCH_WINDOW_MS:
                q.pop(0)


def beam_cell(name, dist, blocked):
    """Coloured beam cell. Blocked: neutral text + a bold-red [X] that pops.
    Clear: all grey so it recedes. Only the marker carries the state."""
    if blocked:
        return "{}={:>4}cm {}".format(name, dist, col("[X]", "red", "bold"))
    return col("{}={:>4}cm [ ]".format(name, dist), "grey")


def main():
    print(col("TF-Luna dual-sensor people counter v2.0.0 - starting...",
              "bold", "cyan"))

    for label in ("A", "B"):
        for n in (3, 2, 1):
            print(col("Calibrating sensor {} background in {}...".format(label, n),
                      "grey"))
            time.sleep(1)

    bg_a = calibrate_background(uart_a)
    bg_b = calibrate_background(uart_b)
    print(col("Background A: {} cm   Background B: {} cm".format(bg_a, bg_b),
              "cyan"))
    if bg_a is None or bg_b is None:
        print(col("ERROR: no valid background. Check wiring/aim and retry.",
                  "bold", "red"))
        return

    counter = DirectionCounter()
    blocked_a = False
    blocked_b = False
    last_a = -1            # most recent distance seen, for debug
    last_b = -1
    buf_a = bytearray()   # leftover UART bytes between loops
    buf_b = bytearray()
    last_debug = time.ticks_ms()

    while True:
        now = time.ticks_ms()

        # Drain both sensors into timestamped edges.
        blocked_a, edges_a, da, buf_a = read_edges(uart_a, buf_a, bg_a, blocked_a, now)
        blocked_b, edges_b, db, buf_b = read_edges(uart_b, buf_b, bg_b, blocked_b, now)
        if da is not None:
            last_a = da
        if db is not None:
            last_b = db

        # Merge both beams' edges and process them in TRUE chronological order,
        # so a fast crossing is counted in the direction it physically happened
        # rather than in the loop's fixed A-before-B read order. Sort by signed
        # offset from now (ticks_diff) so it stays correct across timer wrap.
        edges = [(t, "A", b) for (t, b) in edges_a]
        edges += [(t, "B", b) for (t, b) in edges_b]
        edges.sort(key=lambda e: time.ticks_diff(e[0], now))

        for (t, name, blk) in edges:
            ev = counter.update_beam(name, blk, t)
            if DEBUG:
                edge = col("BLOCKED", "red", "bold") if blk else col("clear", "grey")
                print("  {} {} ({} cm)".format(
                    name, edge, last_a if name == "A" else last_b))
            if ev is not None:
                occupancy = counter.in_count - counter.out_count
                if ev == "IN":
                    tag = col(" ▶ IN  ", "bold", "green")
                else:
                    tag = col(" ◀ OUT ", "bold", "blue")
                print("{}  {}".format(tag, col(
                    "in={} out={} occupancy={}".format(
                        counter.in_count, counter.out_count, occupancy), "grey")))

        counter.poll(now)   # expire stale single-beam pulses

        if DEBUG and time.ticks_diff(now, last_debug) >= DEBUG_PERIOD_MS:
            last_debug = now
            print("{}   {}".format(
                beam_cell("A", last_a, blocked_a),
                beam_cell("B", last_b, blocked_b)))


if __name__ == "__main__":
    main()
