"""
Central configuration for the TF-Luna people counter (MicroPython / ESP32).
====================================================================
Every tunable parameter lives here so the logic modules never need editing.
Copy this file to the device alongside the other src/ modules. For a real
deployment, put secrets (WiFi password, auth token) here and keep it off git.
"""

# ---- Device identity -------------------------------------------------
DEVICE_ID = "entrance-01"        # identifies this counter in the logs

# ---- WiFi ------------------------------------------------------------
WIFI_SSID            = "67"
WIFI_PASSWORD        = "PumaSandy1203@_"
WIFI_CONNECT_TIMEOUT = 15        # seconds to wait for a connection per attempt
WIFI_RETRIES         = 3         # connection attempts at startup before giving up
WIFI_SYNC_TIME       = True      # try NTP time sync once connected (for log timestamps)
# Some APs (notably iPhone hotspots) hand out a DNS server MicroPython can't
# use, so name lookups fail with OSError -202. If resolution doesn't work after
# connecting, fall back to this public resolver (keeps the DHCP IP/gateway).
# Set to "" to always trust the DHCP-assigned DNS.
WIFI_FALLBACK_DNS    = "8.8.8.8"

# ---- Online logging --------------------------------------------------
# Where to push counts. Point this at the dashboard server's /api/log endpoint
# (use the server machine's LAN IP, not localhost).
LOG_URL          = "https://queueview.ago.sh/api/log"
AUTH_TOKEN       = "qt_k_Tu0OxEEV0PnJNPWMWgdzTub5Ma89yI5hrEGdN_jNY"            # must match the server's AUTH_TOKEN, or "" to disable
ONLINE_TIMEOUT   = 5             # seconds per HTTP request before giving up
SNAPSHOT_EVERY   = 60            # also log a heartbeat snapshot every N seconds
FLUSH_EVERY      = 10            # try to flush the offline buffer every N seconds

# ---- TLS / HTTPS -----------------------------------------------------
# When LOG_URL is https, the server certificate is verified against this CA
# bundle (PEM), which must be uploaded to the device alongside the src/ files.
# For queueview.ago.sh it holds the Let's Encrypt roots (ISRG Root X1 + X2).
# Set to "" to skip verification (still encrypted, but unauthenticated).
# NOTE: verification needs a correct clock, so keep WIFI_SYNC_TIME = True.
CA_CERT_FILE     = "ca_certs.pem"

# ---- Local fallback (store-and-forward) ------------------------------
# When the network is down, records are kept here and pushed once back online,
# so nothing is lost offline. A permanent CSV is also always written locally.
BUFFER_FILE      = "buffer.jsonl"   # pending records waiting to be sent
LOCAL_LOG_FILE   = "counts.csv"     # permanent on-device record (append-only)
LOCAL_LOG_ENABLE = True             # write the permanent CSV in addition to pushing
MAX_BUFFER_LINES = 5000             # cap the offline buffer; oldest dropped beyond this

# ---- Sensor UART wiring ----------------------------------------------
# Two SEPARATE UART peripherals. ESP32 has UART1 and UART2 free (UART0 = USB).
# Adjust the pins to match your wiring.
BAUD       = 115200              # TF-Luna default
FRAME_LEN  = 9                  # 9-byte/cm default output frame
HEADER     = 0x59               # frame header byte (appears twice)

UART_A_ID, TX_A_PIN, RX_A_PIN = 1, 17, 18   # Sensor A -> UART0, GP0/GP1
UART_B_ID, TX_B_PIN, RX_B_PIN = 2, 4, 5   # Sensor B -> UART1, GP4/GP5

# ---- Reading-validity thresholds (from the TF-Luna manual) -----------
BLIND_ZONE_CM  = 20             # distances below this are unreliable
AMP_MIN        = 100            # below this -> unreliable
AMP_OVEREXPOSE = 65535          # 0xFFFF -> overexposure / invalid
AMP_AMBIENT    = 32768          # above this -> ambient light overexposure

# ---- People-counting tuning ------------------------------------------
BLOCK_MARGIN_CM   = 40          # drop below background needed to call a beam blocked
RELEASE_MARGIN_CM = 25          # recovery needed to call it clear again (hysteresis)
MATCH_WINDOW_MS   = 1200        # max time between a person's two beam breaks
FRAME_PERIOD_MS   = 10          # sensor output period (100 Hz default)
FIRST_TO_IN       = "A"         # which first-broken beam means IN (A-then-B -> IN)
CALIB_SAMPLES     = 100         # samples averaged to learn the empty-lane background

# ---- Debug -----------------------------------------------------------
DEBUG           = True          # print live beam state + events to the USB console
DEBUG_PERIOD_MS = 250           # how often to print the live readout
USE_COLOR       = True         # ANSI colour (False is safe for Thonny's shell)
