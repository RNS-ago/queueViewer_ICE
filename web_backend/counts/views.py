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
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiKey, CountRecord, SensorZone, Zone

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


def _resolve_times(records):
    """Map each record's pk -> an x-axis datetime.

    ISO-8601 stamps are used as-is. Pre-NTP "uptime+<ms>" markers have no
    absolute reference, so we anchor each boot's uptime stream to that boot's
    first wall-clock time — or, if a whole run never synced, to that run's
    server-receipt time (received_at). Anything else falls back to received_at.
    """
    by_boot = defaultdict(list)
    for r in records:
        by_boot[r.boot_id].append(r)

    times = {}
    # A run that never NTP-synced carries only "uptime+<ms>" markers — no wall
    # clock. We lay it on the real timeline starting at its earliest receipt time
    # (received_at), spread out by the uptime deltas. (It used to be parked on the
    # 1970 epoch; once mixed with NTP-synced runs that blew the x-axis out to a
    # ~56-year span and squashed the live data into a sliver.) Successive unsynced
    # runs are nudged forward so they don't overlap. Uptime resets per boot, so
    # receipt time is the only cross-run ordering signal — walk boots in that
    # order to keep the sequence chronological.
    unsynced_cursor = None
    run_gap = timedelta(minutes=5)
    boots_in_order = sorted(by_boot.values(),
                            key=lambda rows: min(r.received_at for r in rows))
    for rows in boots_in_order:
        walls = {r.pk: r.ts_parsed for r in rows if r.ts_parsed}
        ups = {}
        for r in rows:
            if not r.ts_parsed:
                m = _UPTIME_RE.match(r.ts or "")
                if m:
                    ups[r.pk] = int(m.group(1))
        first_wall = min(walls.values()) if walls else None
        max_up = max(ups.values()) if ups else None
        min_up = min(ups.values()) if ups else None
        # Anchor for a wholly-unsynced run: its own receipt time, but never
        # before the previous unsynced run ended, so they don't pile up.
        anchor = None
        if first_wall is None and ups:
            anchor = min(r.received_at for r in rows)
            if unsynced_cursor is not None and anchor < unsynced_cursor:
                anchor = unsynced_cursor
        for r in rows:
            if r.pk in walls:
                times[r.pk] = walls[r.pk]
            elif r.pk in ups:
                if first_wall is not None:
                    times[r.pk] = (first_wall
                                   - timedelta(milliseconds=max_up - ups[r.pk])
                                   - timedelta(seconds=1))
                else:
                    times[r.pk] = anchor + timedelta(milliseconds=ups[r.pk] - min_up)
            else:
                times[r.pk] = r.received_at
        # Advance the cursor past the unsynced span we just laid down.
        if anchor is not None:
            unsynced_cursor = anchor + timedelta(milliseconds=max_up - min_up) + run_gap
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

    # Counts reset to 0 at every boot, so a single continuous line would draw a
    # misleading plunge from the end of one run to the start of the next. Insert
    # a null row at each boot boundary to lift the pen between runs (records are
    # time-sorted, so each run is a contiguous block).
    series = []
    prev_boot = None
    for r in records:
        if prev_boot is not None and r.boot_id != prev_boot:
            series.append({"x": None, "event": "gap",
                           "in": None, "out": None, "occupancy": None})
        series.append({
            "x": times[r.pk].isoformat(),
            "event": r.event,
            "in": r.count_in,
            "out": r.count_out,
            "occupancy": r.occupancy,
        })
        prev_boot = r.boot_id

    boots = []
    for n, r in enumerate((r for r in records if r.event == "boot"), start=1):
        boots.append({"x": times[r.pk].isoformat(), "label": f"⏻ boot {n}"})

    cmax = max((max(r.count_in, r.count_out, r.occupancy) for r in records), default=0)
    return series, boots, cmax


def _devices():
    return list(
        CountRecord.objects.values_list("device_id", flat=True).distinct().order_by("device_id")
    )


# A sensor sends a heartbeat snapshot every SNAPSHOT_EVERY=60s (esp32/src/config.py),
# so anything silent for a few cycles is effectively offline. The buffer can flush
# late after a network blip, so we allow some slack.
_ONLINE_AFTER = timedelta(minutes=3)


def _sensor_statuses():
    """One row per sensor: when it last reported and whether it's online.

    `last_seen` is the most recent `received_at` (server receipt time, the only
    clock we trust for liveness — device `ts` may be a pre-NTP uptime marker).
    `online` is True if that was within _ONLINE_AFTER; `ago_seconds` is how long
    ago, for a human-friendly "x ago" label rendered client-side.
    """
    now = datetime.now(timezone.utc)
    rows = (
        CountRecord.objects.values("device_id")
        .annotate(last_seen=Max("received_at"))
        .order_by("device_id")
    )
    statuses = []
    for r in rows:
        last_seen = r["last_seen"]
        statuses.append({
            "device_id": r["device_id"],
            "last_seen": last_seen.isoformat() if last_seen else None,
            "online": last_seen is not None and (now - last_seen) <= _ONLINE_AFTER,
        })
    return statuses


def _latest(device_id):
    r = CountRecord.objects.filter(device_id=device_id).order_by("-received_at", "-pk").first()
    if r is None:
        return None
    return {"occupancy": r.occupancy, "in": r.count_in, "out": r.count_out, "ts": r.ts}


def _zone_series(device_ids, limit=2000):
    """Combine several sensors' occupancy/in/out into one time series.

    Each member device has its own snapshot/boot records (and its own boot
    timeline). We resolve every device's x-axis times with the same logic as a
    single-device chart, then walk the merged set of all timestamps and, at each
    one, sum every device's *most recent* value at-or-before that instant
    (carry-forward; a device contributes 0 before its first reading). The result
    is a continuous combined line.

    Members boot independently, so we emit one boot divider per member reboot
    (labelled with the device that rebooted) rather than a single shared one.
    They matter here because `in`/`out` are cumulative-per-boot on each device,
    so the combined line steps down when a member reboots; the marker explains
    the drop. `occupancy` is the headline metric and combines cleanly.
    """
    per_device = []           # list of [(time, in, out, occ), ...] sorted by time
    timeline = set()
    boots = []                # (time, device_id) for every member boot
    for did in device_ids:
        recs = list(
            CountRecord.objects.filter(device_id=did, event__in=["snapshot", "boot"])
            .order_by("-received_at")[:limit]
        )
        times = _resolve_times(recs)
        recs.sort(key=lambda r: (times[r.pk], r.pk))
        pts = [(times[r.pk], r.count_in, r.count_out, r.occupancy) for r in recs]
        per_device.append(pts)
        timeline.update(t for t, _, _, _ in pts)
        boots.extend((times[r.pk], did) for r in recs if r.event == "boot")

    series = []
    idx = [0] * len(per_device)
    last = [(0, 0, 0)] * len(per_device)   # (in, out, occ) carried forward per device
    for t in sorted(timeline):
        s_in = s_out = s_occ = 0
        for i, pts in enumerate(per_device):
            j = idx[i]
            while j < len(pts) and pts[j][0] <= t:
                last[i] = (pts[j][1], pts[j][2], pts[j][3])
                j += 1
            idx[i] = j
            s_in += last[i][0]
            s_out += last[i][1]
            s_occ += last[i][2]
        series.append({"x": t.isoformat(), "event": "snapshot",
                       "in": s_in, "out": s_out, "occupancy": s_occ})

    boot_markers = [{"x": t.isoformat(), "label": f"⏻ {did}"}
                    for t, did in sorted(boots)]
    cmax = max((max(r["in"], r["out"], r["occupancy"]) for r in series), default=0)
    return series, boot_markers, cmax


def _zone_latest(device_ids):
    """Latest combined occupancy/in/out for a zone: sum of each member's latest."""
    occ = cin = cout = 0
    latest_ts = None
    for did in device_ids:
        r = _latest(did)
        if r is None:
            continue
        occ += r["occupancy"]
        cin += r["in"]
        cout += r["out"]
        latest_ts = r["ts"]  # representative; members aren't perfectly synced
    if latest_ts is None:
        return None
    return {"occupancy": occ, "in": cin, "out": cout, "ts": latest_ts}


def _dashboard_context(request):
    devices = _devices()
    zones = list(Zone.objects.prefetch_related("sensors"))

    # A single selector mixes zones and sensors; its value is "zone:<slug>" or
    # "device:<id>". Default to the first zone, else the first sensor.
    target = request.GET.get("target")
    if not target:
        if zones:
            target = f"zone:{zones[0].slug}"
        elif devices:
            target = f"device:{devices[0]}"

    kind, _, ident = (target or "").partition(":")
    zone = next((z for z in zones if z.slug == ident), None) if kind == "zone" else None

    if zone is not None:
        series, boots, cmax = _zone_series(zone.device_ids)
        latest = _zone_latest(zone.device_ids)
        title = zone.name
    else:
        # Treat anything not resolving to a zone as a device selection.
        device = ident if kind == "device" else None
        if device not in devices:
            device = devices[0] if devices else None
        series, boots, cmax = _chart_data(device) if device else ([], [], 0)
        latest = _latest(device) if device else None
        title = device
        target = f"device:{device}" if device else target

    return {
        "devices": devices,
        "zones": zones,
        "target": target,
        "title": title,
        "series_json": json.dumps(series),
        "boots_json": json.dumps(boots),
        "cmax": cmax,
        "latest": latest,
        "total_records": CountRecord.objects.count(),
        "sensor_statuses": _sensor_statuses(),
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
def advanced_dashboard_data(request):
    """JSON feed powering the dashboard's in-place refresh.

    The page polls this instead of reloading itself, so the ~4.5 MB Plotly
    bundle is parsed once and kept in memory rather than re-executed every 30s.
    """
    ctx = _dashboard_context(request)
    return JsonResponse({
        "series": json.loads(ctx["series_json"]),
        "boots": json.loads(ctx["boots_json"]),
        "cmax": ctx["cmax"],
        "latest": ctx["latest"],
        "total_records": ctx["total_records"],
        "sensor_statuses": ctx["sensor_statuses"],
        "title": ctx["title"],
    })


@login_required
@staff_required
@require_http_methods(["GET", "POST"])
def manage_zones(request):
    """Staff-only tab: create/delete zones and assign sensors to them.

    Sensors are the distinct device_ids seen in CountRecord. Each sensor belongs
    to at most one zone (SensorZone.device_id is unique), so assigning a sensor
    already in another zone moves it.
    """
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = (request.POST.get("name") or "").strip()
            if not name:
                messages.error(request, "Please enter a name for the zone.")
            elif Zone.objects.filter(name=name).exists():
                messages.error(request, f"A zone named “{name}” already exists.")
            else:
                Zone.objects.create(name=name)
                messages.success(request, f"Created zone “{name}”.")
        elif action == "delete":
            zone = Zone.objects.filter(pk=request.POST.get("pk")).first()
            if zone is None:
                messages.error(request, "That zone no longer exists.")
            else:
                name = zone.name
                zone.delete()  # cascades to its SensorZone rows
                messages.success(request, f"Deleted zone “{name}”.")
        elif action == "assign":
            zone = Zone.objects.filter(pk=request.POST.get("zone")).first()
            device_id = (request.POST.get("device_id") or "").strip()
            if zone is None or not device_id:
                messages.error(request, "Pick a sensor and a zone.")
            else:
                SensorZone.objects.update_or_create(
                    device_id=device_id, defaults={"zone": zone}
                )
                messages.success(request, f"Assigned “{device_id}” to “{zone.name}”.")
        elif action == "unassign":
            SensorZone.objects.filter(device_id=request.POST.get("device_id")).delete()
            messages.success(request, "Removed the sensor from its zone.")
        return redirect("manage_zones")  # PRG: avoid resubmit on refresh

    devices = _devices()
    assigned = {sz.device_id: sz.zone_id for sz in SensorZone.objects.all()}
    return render(request, "manage_zones.html", {
        "zones": list(Zone.objects.prefetch_related("sensors")),
        "unassigned": [d for d in devices if d not in assigned],
    })


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
