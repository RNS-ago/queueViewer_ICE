"""
TF-Luna LiDAR - UART Reader for MicroPython
===========================================
Reads distance, signal strength, and chip temperature from a
Benewake TF-Luna single-point LiDAR over UART.

Version history
---------------
v1.0.0  (2026-06-24)  Initial release.
                      - Frame sync on 0x59 0x59 header
                      - Checksum validation
                      - Parses distance (cm), strength, temperature (C)

Wiring (Raspberry Pi Pico example)
----------------------------------
  TF-Luna          Pico
  -------          ----
  5V / Vin   --->  VBUS (5V)   (TF-Luna accepts 3.7-5.2 V)
  GND        --->  GND
  TXD        --->  GP1  (UART0 RX)   <- sensor talks, Pico listens
  RXD        --->  GP0  (UART0 TX)   <- optional, only needed to configure

Default sensor settings: 115200 baud, 9-byte frame @ 100 Hz.

Frame layout (9 bytes):
  [0] 0x59  header
  [1] 0x59  header
  [2] Dist_L      [3] Dist_H      -> distance in cm
  [4] Amp_L       [5] Amp_H       -> signal strength
  [6] Temp_L      [7] Temp_H      -> chip temperature (raw)
  [8] Checksum    -> low 8 bits of sum of bytes 0..7
"""

from machine import UART, Pin
import time

# ---- Configuration --------------------------------------------------
UART_ID   = 0       # UART peripheral (0 or 1 on the Pico)
TX_PIN    = 17       # GP0 -> sensor RXD
RX_PIN    = 18       # GP1 -> sensor TXD
BAUD      = 115200  # TF-Luna default
FRAME_LEN = 9       # standard output frame length
HEADER    = 0x59    # frame header byte (appears twice)

uart = UART(UART_ID, baudrate=BAUD, tx=Pin(TX_PIN), rx=Pin(RX_PIN))


def read_frame():
    """Return one validated 9-byte frame, or None.

    Synchronises on the 0x59 0x59 header and verifies the checksum.
    """
    # Find the first header byte
    b = uart.read(1)
    if not b or b[0] != HEADER:
        return None

    # Confirm the second header byte
    b = uart.read(1)
    if not b or b[0] != HEADER:
        return None

    # Read the remaining 7 bytes of the frame
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
    strength = frame[4] | (frame[5] << 8)      # signal amplitude
    temp_raw = frame[6] | (frame[7] << 8)
    temp_c   = temp_raw / 8.0 - 256.0          # C (chip temperature)

    return {
        "distance_cm": dist,
        "strength": strength,
        "temp_c": temp_c,
    }


def main():
    print("TF-Luna UART reader v1.0.0 - starting...")
    while True:
        frame = read_frame()
        if frame is None:
            time.sleep_ms(1)
            continue

        data = parse_frame(frame)

        # Signal-quality notes (per Benewake guidance):
        #   strength < 100   -> reading is unreliable
        #   strength == 65535 -> saturated, ignore the distance
        if data["strength"] < 100 or data["strength"] == 65535:
            status = "weak/invalid"
        else:
            status = "ok"

        print("dist={:>4} cm  strength={:>5}  temp={:.1f} C  [{}]".format(
            data["distance_cm"], data["strength"], data["temp_c"], status))


if __name__ == "__main__":
    main()
