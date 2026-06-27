"""Views for the three endpoints.

  * api_log            — POST ingest from the ESP32 logger
  * public_dashboard   — minimal, no auth (replace the template with your own)
  * advanced_dashboard — richer view, login required
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiKey, CountRecord

# Managing keys is an admin-level action; restrict it to staff users.
staff_required = user_passes_test(lambda u: u.is_staff)

# Fields we accept from a device record.
_REQUIRED = ("device_id", "boot_id", "ts", "event")


def _parse_ts(ts):
    """Best-effort: return a datetime for ISO-8601 strings, else None.

    The device sends 'uptime+1234ms' before NTP sync — that has no datetime,
    so we keep the raw string in `ts` and leave `ts_parsed` null.
    """
    if not isinstance(ts, str):
        return None
    return parse_datetime(ts)


def _record_from_payload(item):
    """Build (unsaved) CountRecord from one dict, or raise ValueError."""
    if not isinstance(item, dict):
        raise ValueError("each record must be a JSON object")
    missing = [k for k in _REQUIRED if k not in item]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    return CountRecord(
        device_id=str(item["device_id"])[:64],
        boot_id=str(item["boot_id"])[:64],
        ts=str(item["ts"])[:64],
        ts_parsed=_parse_ts(item["ts"]),
        event=str(item["event"])[:16],
        count_in=int(item.get("in", 0)),
        count_out=int(item.get("out", 0)),
        occupancy=int(item.get("occupancy", 0)),
    )


@csrf_exempt
@require_http_methods(["POST"])
def api_log(request):
    """Accept a single record object or a JSON array of them.

    Mirrors what esp32/src/logger.py POSTs. Requires a valid API key in the
    `x-auth-token` header — create one with `manage.py create_apikey` or in the
    admin, and set the same value as AUTH_TOKEN in the device config.
    """
    if ApiKey.verify(request.headers.get("x-auth-token", "")) is None:
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "invalid JSON"}, status=400)

    items = data if isinstance(data, list) else [data]
    try:
        records = [_record_from_payload(item) for item in items]
    except (ValueError, TypeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    CountRecord.objects.bulk_create(records)
    return JsonResponse({"status": "ok", "saved": len(records)}, status=201)


_UPTIME_RE = re.compile(r"uptime\+(\d+)ms")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _resolve_times(records):
    """Map each record's pk -> an x-axis datetime.

    ISO-8601 stamps are used as-is. Pre-NTP "uptime+<ms>" markers have no
    absolute reference, so (mirroring visualize_counts.parse_timestamps) we
    anchor each boot's uptime stream to that boot's first wall-clock time —
    or, if a whole run never synced, lay it on the epoch so its shape still
    shows. Anything else falls back to received_at.
    """
    by_boot = defaultdict(list)
    for r in records:
        by_boot[r.boot_id].append(r)

    times = {}
    for rows in by_boot.values():
        walls = {r.pk: r.ts_parsed for r in rows if r.ts_parsed}
        ups = {}
        for r in rows:
            if not r.ts_parsed:
                m = _UPTIME_RE.match(r.ts or "")
                if m:
                    ups[r.pk] = int(m.group(1))
        first_wall = min(walls.values()) if walls else None
        max_up = max(ups.values()) if ups else None
        for r in rows:
            if r.pk in walls:
                times[r.pk] = walls[r.pk]
            elif r.pk in ups:
                if first_wall is not None:
                    times[r.pk] = (first_wall
                                   - timedelta(milliseconds=max_up - ups[r.pk])
                                   - timedelta(seconds=1))
                else:
                    times[r.pk] = _EPOCH + timedelta(milliseconds=ups[r.pk])
            else:
                times[r.pk] = r.received_at
    return times


def _chart_data(device_id, limit=2000):
    """Build (series, boots, cmax) for the dashboard chart.

    `series` is the snapshot+boot records (oldest-first) with a resolved x;
    `boots` are the boot markers (vertical dividers); `cmax` is the largest
    count value, used to decide integer tick spacing.
    """
    qs = (
        CountRecord.objects.filter(device_id=device_id, event__in=["snapshot", "boot"])
        .order_by("-received_at")[:limit]
    )
    records = list(qs)
    times = _resolve_times(records)
    records.sort(key=lambda r: (times[r.pk], r.pk))

    series = [
        {
            "x": times[r.pk].isoformat(),
            "event": r.event,
            "in": r.count_in,
            "out": r.count_out,
            "occupancy": r.occupancy,
        }
        for r in records
    ]
    boots = []
    for n, r in enumerate((r for r in records if r.event == "boot"), start=1):
        boots.append({"x": times[r.pk].isoformat(), "label": f"⏻ boot {n}"})

    cmax = max((max(s["in"], s["out"], s["occupancy"]) for s in series), default=0)
    return series, boots, cmax


def _devices():
    return list(
        CountRecord.objects.values_list("device_id", flat=True).distinct().order_by("device_id")
    )


def _latest(device_id):
    r = CountRecord.objects.filter(device_id=device_id).order_by("-received_at", "-pk").first()
    if r is None:
        return None
    return {"occupancy": r.occupancy, "in": r.count_in, "out": r.count_out, "ts": r.ts}


def _dashboard_context(request):
    devices = _devices()
    device = request.GET.get("device") or (devices[0] if devices else None)
    series, boots, cmax = _chart_data(device) if device else ([], [], 0)
    return {
        "devices": devices,
        "device": device,
        "series_json": json.dumps(series),
        "boots_json": json.dumps(boots),
        "cmax": cmax,
        "latest": _latest(device) if device else None,
        "total_records": CountRecord.objects.count(),
    }


def public_dashboard(request):
    """Minimal public dashboard. Replace templates/public_dashboard.html with
    your own minimal page — the `series_json`/`latest` context is available to it."""
    return render(request, "public_dashboard.html", _dashboard_context(request))


@login_required
def advanced_dashboard(request):
    """Richer dashboard, gated behind login."""
    return render(request, "advanced_dashboard.html", _dashboard_context(request))


@login_required
@staff_required
@require_http_methods(["GET", "POST"])
def manage_keys(request):
    """Staff-only tab of the advanced dashboard: create/revoke/delete API keys."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = (request.POST.get("name") or "").strip()
            if name:
                obj, raw = ApiKey.generate(name)
                messages.warning(
                    request,
                    f"New API key for “{obj.name}”:  {raw}  — copy it now, "
                    "it will not be shown again.",
                )
            else:
                messages.error(request, "Please enter a name for the key.")
        elif action in {"revoke", "activate", "delete"}:
            key = ApiKey.objects.filter(pk=request.POST.get("pk")).first()
            if key is None:
                messages.error(request, "That key no longer exists.")
            elif action == "revoke":
                key.active = False
                key.save(update_fields=["active"])
                messages.success(request, f"Revoked the key for “{key.name}”.")
            elif action == "activate":
                key.active = True
                key.save(update_fields=["active"])
                messages.success(request, f"Re-activated the key for “{key.name}”.")
            else:  # delete
                name = key.name
                key.delete()
                messages.success(request, f"Deleted the key for “{name}”.")
        return redirect("manage_keys")  # PRG: avoid resubmit on refresh

    return render(request, "manage_keys.html", {"keys": ApiKey.objects.all()})
