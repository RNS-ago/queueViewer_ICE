"""
Dedicated online logging with local fallback (MicroPython / ESP32).
====================================================================
Strategy = local-first, store-and-forward:

  1. Every record is appended to an on-device send buffer (BUFFER_FILE) and,
     optionally, to a permanent local CSV (LOCAL_LOG_FILE) — so a record
     survives even with no network and even across a reboot.
  2. When WiFi is up, the buffer is POSTed to the server (LOG_URL) as a JSON
     batch. On success the buffer is cleared; on failure it stays put and is
     retried later. Nothing is lost while offline.

The server endpoint (/api/log) accepts either a single record object or a
JSON array of them.
"""

import time

try:
    import ujson as json
except ImportError:
    import json

try:
    import uos as os
except ImportError:
    import os

try:
    import usocket as socket
except ImportError:
    import socket


def _file_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _file_empty(path):
    """True if the file is missing or has zero bytes (no header yet)."""
    try:
        return os.stat(path)[6] == 0
    except OSError:
        return True


class Logger:
    def __init__(self, cfg, wifi):
        self.cfg = cfg
        self.wifi = wifi                 # module exposing is_connected()/time_synced()
        # Build the TLS context once: verifies the server against the embedded
        # CA bundle (cfg.CA_CERT_FILE). _tls_verified is False if we fell back
        # to unverified TLS (bundle missing) or the build has no TLS at all.
        self._ssl_ctx, self._tls_verified = self._build_ssl_context()
        # A fresh id every boot so records from different runs can be told apart.
        self.boot_id = self._new_boot_id()
        print("logger: boot_id =", self.boot_id)
        # Emit an explicit marker so each start-up stands out in the logs.
        self.record("boot", 0, 0)

    # ---- boot / session id -------------------------------------------
    def _new_boot_id(self):
        """Short id unique to this power-up (random + uptime, no NTP needed)."""
        try:
            import urandom
            rnd = urandom.getrandbits(24)
        except ImportError:
            import random
            rnd = random.getrandbits(24)
        return "{:06x}{:08x}".format(rnd & 0xFFFFFF, time.ticks_ms() & 0xFFFFFFFF)

    # ---- timestamps --------------------------------------------------
    def _timestamp(self):
        """ISO-8601 UTC if NTP synced, else an uptime marker."""
        if self.wifi.time_synced():
            t = time.gmtime()
            return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
                t[0], t[1], t[2], t[3], t[4], t[5])
        return "uptime+{}ms".format(time.ticks_ms())

    # ---- public API --------------------------------------------------
    def record(self, event, in_count, out_count):
        """Persist one record locally and try to ship it online.

        `event` is "in", "out", "snapshot" (periodic heartbeat), or "boot"
        (a one-shot marker written at start-up to delimit runs).
        """
        payload = {
            "device_id": self.cfg.DEVICE_ID,
            "boot_id": self.boot_id,
            "ts": self._timestamp(),
            "event": event,
            "in": in_count,
            "out": out_count,
            "occupancy": in_count - out_count,
        }
        line = json.dumps(payload)
        self._enqueue(line)
        if self.cfg.LOCAL_LOG_ENABLE:
            self._append_csv(payload)
        self.flush()
        return payload

    def flush(self):
        """Push the offline buffer to the server. Returns True if buffer is empty."""
        if not self.wifi.is_connected():
            return False
        lines = self._read_buffer()
        if not lines:
            return True
        body = "[" + ",".join(lines) + "]"
        if self._post(body):
            self._clear_buffer()
            return True
        return False

    # ---- offline buffer (store-and-forward) --------------------------
    def _enqueue(self, line):
        try:
            with open(self.cfg.BUFFER_FILE, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            print("logger: could not write buffer:", e)
            return
        self._trim_buffer()

    def _read_buffer(self):
        if not _file_exists(self.cfg.BUFFER_FILE):
            return []
        try:
            with open(self.cfg.BUFFER_FILE) as f:
                return [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return []

    def _clear_buffer(self):
        try:
            os.remove(self.cfg.BUFFER_FILE)
        except OSError:
            pass

    def _trim_buffer(self):
        """Keep the buffer from growing without bound if offline for a long time."""
        lines = self._read_buffer()
        if len(lines) <= self.cfg.MAX_BUFFER_LINES:
            return
        keep = lines[-self.cfg.MAX_BUFFER_LINES:]
        try:
            with open(self.cfg.BUFFER_FILE, "w") as f:
                f.write("\n".join(keep) + "\n")
        except OSError:
            pass

    # ---- permanent local record -------------------------------------
    def _append_csv(self, payload):
        new = _file_empty(self.cfg.LOCAL_LOG_FILE)
        try:
            with open(self.cfg.LOCAL_LOG_FILE, "a") as f:
                if new:
                    f.write("ts,device_id,boot_id,event,in,out,occupancy\n")
                f.write("{},{},{},{},{},{},{}\n".format(
                    payload["ts"], payload["device_id"], payload["boot_id"],
                    payload["event"], payload["in"], payload["out"],
                    payload["occupancy"]))
        except OSError as e:
            print("logger: could not write CSV:", e)

    # ---- network -----------------------------------------------------
    def _build_ssl_context(self):
        """Make a TLS context that verifies the server against cfg.CA_CERT_FILE.

        Returns (context_or_None, verified). If the CA bundle is missing we fall
        back to UNVERIFIED TLS (encrypted but not authenticated) so the device
        still reports rather than going dark — a warning is printed so it shows.
        Returns (None, False) on builds with no TLS at all (http stays usable).
        """
        try:
            import tls as ssl
        except ImportError:
            try:
                import ssl
            except ImportError:
                return None, False

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ca_file = getattr(self.cfg, "CA_CERT_FILE", "")
        if ca_file and _file_exists(ca_file):
            try:
                # Read as bytes and NUL-terminate: mbedtls treats a buffer whose
                # last byte is '\0' as PEM, otherwise as DER. MicroPython passes
                # `cadata` positionally (no keyword), unlike CPython.
                with open(ca_file, "rb") as f:
                    cadata = f.read()
                if not cadata.endswith(b"\x00"):
                    cadata += b"\x00"
                ctx.load_verify_locations(cadata)
                ctx.verify_mode = ssl.CERT_REQUIRED
                print("logger: TLS verifying server against", ca_file)
                return ctx, True
            except Exception as e:
                print("logger: could not load CA ({}); TLS UNVERIFIED".format(e))
        else:
            print("logger: no CA bundle (CA_CERT_FILE); TLS UNVERIFIED")
        ctx.verify_mode = ssl.CERT_NONE
        return ctx, False

    def _post(self, body):
        """POST `body` (JSON text) to cfg.LOG_URL. Returns True on a 2xx reply.

        Done over a raw socket rather than urequests so we can (a) verify the
        server certificate against the embedded CA and (b) honour ONLINE_TIMEOUT
        — the bundled urequests does neither.
        """
        try:
            scheme, _, rest = self.cfg.LOG_URL.partition("://")
            hostport, _, path = rest.partition("/")
            use_tls = scheme == "https"
            if ":" in hostport:
                host, port = hostport.split(":")
                port = int(port)
            else:
                host, port = hostport, (443 if use_tls else 80)
        except Exception as e:
            print("logger: bad LOG_URL:", e)
            return False

        body_bytes = body.encode() if isinstance(body, str) else body
        req = "POST /{} HTTP/1.0\r\nHost: {}\r\n".format(path, host)
        req += "Content-Type: application/json\r\n"
        if self.cfg.AUTH_TOKEN:
            req += "x-auth-token: {}\r\n".format(self.cfg.AUTH_TOKEN)
        req += "Content-Length: {}\r\nConnection: close\r\n\r\n".format(len(body_bytes))

        s = None
        try:
            addr = socket.getaddrinfo(host, port)[0][-1]
            s = socket.socket()
            s.settimeout(self.cfg.ONLINE_TIMEOUT)
            s.connect(addr)
            if use_tls:
                if self._ssl_ctx is None:
                    raise OSError("https requested but this build has no TLS")
                s = self._ssl_ctx.wrap_socket(s, server_hostname=host)
            s.write(req.encode())
            s.write(body_bytes)
            status_line = s.readline()          # e.g. b"HTTP/1.1 201 Created\r\n"
            status = int(status_line.split(b" ")[1])
            ok = 200 <= status < 300
            if not ok:
                print("logger: server returned", status)
            return ok
        except Exception as e:
            # offline / DNS / timeout / cert failure — keep the buffer for later
            print("logger: push failed ({}), keeping records local".format(e))
            return False
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
