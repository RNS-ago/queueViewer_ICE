"""
WiFi connection + time sync for the people counter (MicroPython / ESP32).
====================================================================
Tries to join the configured network at startup, with a timeout and a few
retries. Connection is OPTIONAL by design: if it fails, the counter still
runs and logs locally (store-and-forward in logger.py), then catches up
once WiFi returns. Also does a best-effort NTP sync so log timestamps are
real wall-clock time rather than just uptime.
"""

import time

try:
    import network
except ImportError:
    network = None   # allows importing this module off-device for linting


_wlan = None
_time_synced = False


def _ensure_dns(fallback):
    """Make sure name resolution works; if not, install a fallback resolver.

    iPhone hotspots (and some APs) advertise a DNS server MicroPython can't use,
    so getaddrinfo fails with OSError -202 even though we're associated. If a
    test lookup fails and a fallback is configured, swap only the DNS field of
    ifconfig (keeping the DHCP IP/mask/gateway). No-op if DNS already works.
    """
    if not fallback or _wlan is None:
        return
    import socket
    try:
        socket.getaddrinfo("pool.ntp.org", 123)
        return                              # DNS already works — leave it alone
    except Exception:
        pass
    try:
        ip, mask, gw, _ = _wlan.ifconfig()
        _wlan.ifconfig((ip, mask, gw, fallback))
        socket.getaddrinfo("pool.ntp.org", 123)   # verify the fallback resolves
        print("wifi: DHCP DNS failed; using fallback DNS", fallback)
    except Exception as e:
        print("wifi: fallback DNS {} also failed: {}".format(fallback, e))


def connect(ssid, password, timeout=15, retries=3, fallback_dns=""):
    """Attempt to join WiFi. Returns True if connected, False otherwise.

    Never raises on failure — the caller decides what to do offline.
    """
    global _wlan
    if network is None:
        print("wifi: network module unavailable (not on device?)")
        return False

    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(True)

    if _wlan.isconnected():
        print("wifi: already connected, IP =", _wlan.ifconfig()[0])
        _ensure_dns(fallback_dns)
        return True

    for attempt in range(1, retries + 1):
        print("wifi: connecting to '{}' (attempt {}/{})...".format(ssid, attempt, retries))
        try:
            _wlan.connect(ssid, password)
        except OSError as e:
            print("wifi: connect error:", e)

        deadline = time.ticks_add(time.ticks_ms(), timeout * 1000)
        while not _wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
            time.sleep_ms(200)

        if _wlan.isconnected():
            print("wifi: connected, IP =", _wlan.ifconfig()[0])
            _ensure_dns(fallback_dns)
            return True
        print("wifi: attempt {} timed out".format(attempt))

    print("wifi: could not connect — continuing OFFLINE (logging locally)")
    return False


def is_connected():
    """True if WiFi is currently associated. Cheap to call in the main loop."""
    return _wlan is not None and _wlan.isconnected()


def get_ip():
    return _wlan.ifconfig()[0] if is_connected() else None


def sync_time():
    """Best-effort NTP sync so logs carry real timestamps. Returns True on success."""
    global _time_synced
    if not is_connected():
        return False
    try:
        import ntptime
        ntptime.settime()
        _time_synced = True
        print("wifi: time synced via NTP")
        return True
    except Exception as e:
        print("wifi: NTP sync failed:", e)
        return False


def time_synced():
    return _time_synced
