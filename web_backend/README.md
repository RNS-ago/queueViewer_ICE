# queueTracker dashboard server

Minimal Django server with three endpoints:

| Method & path   | Auth          | Purpose                                             |
|-----------------|---------------|-----------------------------------------------------|
| `POST /api/log` | API key       | Ingest records from `esp32/src/logger.py`           |
| `GET  /`        | public        | Minimal public dashboard (replace with your own)    |
| `GET  /advanced/` | login required | Richer staff dashboard (occupancy chart + stats)  |

The advanced dashboard has an **API keys** tab (`/advanced/keys/`, staff only) for
creating, revoking, and deleting keys. Plus Django admin at `/admin/` and
login/logout at `/login/`, `/logout/`.

## Run (local dev)

```bash
cd web_backend
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py createsuperuser        # for the advanced dashboard + admin
../.venv/bin/python manage.py create_apikey entrance-01   # key for the device (shown once)
# Port 3000 matches LOG_URL in esp32/src/config.py. 0.0.0.0 so the ESP32 can reach it.
../.venv/bin/python manage.py runserver 0.0.0.0:3000
```

Open http://localhost:3000/ (public) and http://localhost:3000/advanced/ (login).

Point the device at the server by setting `LOG_URL` in `esp32/src/config.py` to
`http://<this-machine-LAN-IP>:3000/api/log`.

## The /api/log endpoint

Accepts either a single JSON record or a JSON array of them, exactly as the
device sends:

```json
{"device_id":"entrance-01","boot_id":"a1b2c3","ts":"2026-06-27T08:01:00Z",
 "event":"snapshot","in":5,"out":2,"occupancy":3}
```

Every request must carry a valid API key in the `x-auth-token` header. Create
keys with `manage.py create_apikey <name>` or in the advanced dashboard's
**API keys** tab, then set the value as `AUTH_TOKEN` in the device config. Keys
are stored hashed (only a SHA-256 is kept) and can be revoked or deleted at any
time. Requests with a missing, unknown, or revoked key get `401`.

Quick test (replace with a real key):

```bash
curl -X POST http://localhost:3000/api/log \
  -H 'Content-Type: application/json' -H 'x-auth-token: qt_yourkeyhere' \
  -d '{"device_id":"entrance-01","boot_id":"test","ts":"2026-06-27T08:01:00Z","event":"snapshot","in":5,"out":2,"occupancy":3}'
```

## Loading existing data

To populate the database from a `counts.csv` exported by the device (or the
bundled sample), use the `load_counts` command. `--clear` wipes existing count
records first (API keys and users are kept):

```bash
../.venv/bin/python manage.py load_counts ../visualization/sample_counts.csv --clear
```

## Dashboards

- **Public** (`templates/public_dashboard.html`) is a placeholder showing current
  occupancy. Replace it with the minimal page you want to provide; the view hands
  the template `device`, `latest`, and `series_json` (recent records as JSON).
- **Advanced** (`templates/advanced_dashboard.html`) is login-gated and draws an
  occupancy / in / out chart with Plotly, with a device selector.

## Configuration

Everything is environment-driven with safe dev defaults — see `.env.example`.
For production set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, and
`DJANGO_ALLOWED_HOSTS`.
