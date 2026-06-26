"""
TF-Luna LiDAR - I2C Diagnostic Reader for MicroPython
=====================================================
Talks to the Benewake TF-Luna over I2C instead of UART. Intended as a
diagnostic: if this works but UART didn't, the sensor is fine and the
problem is on the UART side (wiring, pins, baud, or pin-5 mode select).

Sources of truth:
  - Waveshare wiki: https://www.waveshare.com/wiki/TF-Luna_LiDAR_Range_Sensor
  - Benewake "SJ-PM-TF-Luna A05" Product Manual (Appendix III, I2C registers)

Version history
---------------
i2c v1.0.0  (2026-06-24)  Initial I2C diagnostic.
                          - I2C bus scan
                          - SIGNATURE check (reads "LUNA" from 0x3C-0x3F)
                          - Reads dist / amp / temp from registers
                          (UART lineage v1/v2 left untouched.)

IMPORTANT - mode is latched at power-on
---------------------------------------
  Pin 5 = GND  before power-up -> I2C mode  (required for this script)
  Pin 5 = NC / 3.3V            -> UART mode
  You MUST power-cycle the sensor after moving pin 5. It will not switch
  modes while running.

Wiring - Raspberry Pi Pico (Waveshare reference, I2C mode)
----------------------------------------------------------
  TF-Luna pin        Pico
  -----------        ----
  1  +5V       --->  VBUS (5V)        (supply MUST be 3.7-5.2 V, NOT 3.3 V)
  4  GND       --->  GND
  2  SDA       --->  GP8  (I2C0 SDA)  <- same wire as UART; no need to move
  3  SCL       --->  GP9  (I2C0 SCL)  <- same wire as UART; no need to move
  5  Config    --->  GND              <- grounded to select I2C mode

  If the bus scan finds nothing, add ~4.7k pull-up resistors from SDA and
  SCL to 3.3V - some setups need them for reliable I2C.

I2C register map (from manual, Appendix III)
--------------------------------------------
  0x00 DIST_LOW   0x01 DIST_HIGH   -> distance in cm
  0x02 AMP_LOW    0x03 AMP_HIGH    -> signal strength (Amp)
  0x04 TEMP_LOW   0x05 TEMP_HIGH   -> chip temp, unit 0.01 C (see note)
  0x3C..0x3F SIGNATURE             -> 'L' 'U' 'N' 'A'
"""

from machine import I2C, Pin
import time

# ---- Configuration --------------------------------------------------
I2C_ID   = 0        # I2C0 on the Pico
SDA_PIN  = 21        # GP8 -> sensor SDA (pin 2)
SCL_PIN  = 22        # GP9 -> sensor SCL (pin 3)
FREQ     = 100000   # 100 kHz for robust first contact (sensor max 400 kHz)
ADDR     = 0x10     # default 7-bit slave address

# Register addresses
REG_DIST = 0x00
REG_AMP  = 0x02
REG_TEMP = 0x04
REG_SIG  = 0x3C

AMP_MIN        = 100
AMP_OVEREXPOSE = 65535
BLIND_ZONE_CM  = 20

i2c = I2C(I2C_ID, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=FREQ)


def scan_bus():
    """List every device that ACKs on the bus."""
    found = i2c.scan()
    if found:
        print("I2C scan found:", [hex(a) for a in found])
    else:
        print("I2C scan found NOTHING.")
        print("  -> check power (3.7-5.2V), GND, that pin 5 is grounded,")
        print("     that you power-cycled the sensor, and SDA/SCL wiring.")
    return found


def check_signature():
    """Read 0x3C-0x3F and confirm the sensor identifies as 'LUNA'."""
    try:
        sig = i2c.readfrom_mem(ADDR, REG_SIG, 4)
    except OSError as e:
        print("Signature read failed:", e)
        return False
    text = "".join(chr(b) for b in sig)
    ok = (text == "LUNA")
    print("Signature register: {!r}  ->  {}".format(
        text, "OK, this is a TF-Luna" if ok else "unexpected"))
    return ok


def read_u16(reg):
    """Read a little-endian 16-bit value starting at reg."""
    data = i2c.readfrom_mem(ADDR, reg, 2)
    return data[0] | (data[1] << 8)


def read_measurement():
    """Return dist (cm), amp, temp (C), or None on bus error."""
    try:
        dist = read_u16(REG_DIST)
        amp  = read_u16(REG_AMP)
        # Per the I2C register table the temp unit is 0.01 C.
        # (Note: the UART frame instead uses temp/8 - 256. If this value
        #  looks wrong on your unit, that scaling discrepancy is why.)
        temp = read_u16(REG_TEMP) * 0.01
    except OSError as e:
        print("Read error:", e)
        return None
    return {"distance_cm": dist, "strength": amp, "temp_c": temp}


def reading_status(data):
    amp, dist = data["strength"], data["distance_cm"]
    if amp == AMP_OVEREXPOSE:
        return "invalid (overexposed)"
    if amp < AMP_MIN:
        return "weak (amp<100)"
    if dist < BLIND_ZONE_CM:
        return "blind zone (<20cm)"
    return "ok"


def main():
    print("TF-Luna I2C diagnostic i2c-v1.0.0 - starting...\n")

    # Step 1: is anything on the bus?
    found = scan_bus()
    if ADDR not in found:
        print("\nDefault address 0x10 not present - stopping.")
        print("If the scan found a DIFFERENT address, set ADDR to it.")
        return

    # Step 2: confirm it is really a TF-Luna
    print()
    check_signature()

    # Step 3: stream readings
    print("\nStreaming measurements (Ctrl-C to stop):")
    while True:
        data = read_measurement()
        if data is not None:
            print("dist={:>4} cm  amp={:>5}  temp={:.1f} C  [{}]".format(
                data["distance_cm"], data["strength"],
                data["temp_c"], reading_status(data)))
        time.sleep_ms(100)


if __name__ == "__main__":
    main()
