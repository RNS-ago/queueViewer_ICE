import hashlib
import secrets

from django.db import models
from django.utils import timezone

# Raw keys look like "qt_<43 url-safe chars>". The "qt_" prefix makes them
# easy to spot in logs/config; the first 8 chars are stored in the clear so a
# key can be identified in the admin without revealing it.
_KEY_PREFIX = "qt_"


def hash_key(raw):
    """Deterministic hash used for lookup and storage.

    A plain SHA-256 is appropriate here (not a slow password hash): API keys are
    long, high-entropy random strings, so they aren't vulnerable to the
    brute-force/dictionary attacks that bcrypt/argon2 defend against, and the
    deterministic hash lets us look a key up in one indexed query.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiKey(models.Model):
    """A revocable API key authorizing a client to POST to /api/log.

    Only the hash of the key is stored, so a database leak does not expose
    usable keys. The raw key is shown exactly once, at creation time.
    """

    name = models.CharField(
        max_length=100,
        help_text="Which device or client this key is for, e.g. 'entrance-01'.",
    )
    prefix = models.CharField(max_length=12, editable=False, db_index=True)
    hashed_key = models.CharField(max_length=64, editable=False, unique=True)
    active = models.BooleanField(default=True, help_text="Uncheck to revoke without deleting.")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        state = "active" if self.active else "revoked"
        return f"{self.name} ({self.prefix}…, {state})"

    @classmethod
    def generate(cls, name):
        """Create and save a new key. Returns (instance, raw_key).

        `raw_key` is the only time the full key exists — store it now.
        """
        raw = _KEY_PREFIX + secrets.token_urlsafe(32)
        obj = cls.objects.create(name=name, prefix=raw[:12], hashed_key=hash_key(raw))
        return obj, raw

    @classmethod
    def verify(cls, raw):
        """Return the active ApiKey matching `raw`, or None. Touches last_used_at."""
        if not raw:
            return None
        try:
            key = cls.objects.get(hashed_key=hash_key(raw), active=True)
        except cls.DoesNotExist:
            return None
        # Cheap usage tracking; avoid auto_now fields to keep it to one column.
        cls.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return key


class CountRecord(models.Model):
    """One record as emitted by the ESP32 logger (esp32/src/logger.py).

    The device payload looks like::

        {"device_id": "entrance-01", "boot_id": "a1b2c3...",
         "ts": "2026-06-27T08:01:00Z",   # or "uptime+1234ms" before NTP sync
         "event": "snapshot",            # in | out | snapshot | boot
         "in": 5, "out": 2, "occupancy": 3}

    `in`/`out` are stored as count_in/count_out because `in` is a Python keyword.
    `ts` is kept verbatim as text (it may be a non-datetime uptime marker); a
    best-effort parsed datetime is stored separately for charting.
    """

    EVENT_CHOICES = [
        ("in", "in"),
        ("out", "out"),
        ("snapshot", "snapshot"),
        ("boot", "boot"),
    ]

    device_id = models.CharField(max_length=64, db_index=True)
    boot_id = models.CharField(max_length=64, db_index=True)
    ts = models.CharField(max_length=64)
    ts_parsed = models.DateTimeField(null=True, blank=True, db_index=True)
    event = models.CharField(max_length=16)
    count_in = models.IntegerField(default=0)
    count_out = models.IntegerField(default=0)
    occupancy = models.IntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["device_id", "ts_parsed"]),
        ]

    def __str__(self):
        return f"{self.device_id} {self.event} @ {self.ts} (occ={self.occupancy})"
