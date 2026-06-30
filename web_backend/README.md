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

## Deployment (production)

The production stack is **gunicorn** (running the Django app on
`127.0.0.1:8000`) behind **Caddy** (reverse proxy + automatic HTTPS), with
**WhiteNoise** serving the admin's static files and data in SQLite. Ready-made
files live in `deploy/`:

| File                       | Purpose                                            |
|----------------------------|----------------------------------------------------|
| `deploy/queuetracker.service` | systemd unit that runs gunicorn                 |
| `deploy/Caddyfile`         | Caddy reverse-proxy + HTTPS config (edit the domain) |
| `deploy/DEPLOY.md`         | full step-by-step VPS walkthrough                  |

Outline (Ubuntu/Debian, repo at `/opt/queueTracker_roskilde`):

```bash
# 1. DNS: point your domain's A/AAAA record at the server
# 2. Python 3.12 (Django 5 needs 3.10+; Ubuntu 20.04 ships 3.8)
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv

# 3. Packages (Caddy isn't in the default Ubuntu repos — add its official repo first)
sudo apt install -y git debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# 4. Code + deps (build the venv with python3.12)
sudo git clone <your-repo-url> /opt/queueTracker_roskilde
cd /opt/queueTracker_roskilde
sudo python3.12 -m venv .venv
sudo .venv/bin/pip install -r web_backend/requirements.txt

# 4. Production .env (copy web_backend/.env.example, set DEBUG=false,
#    a real DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, DJANGO_CSRF_TRUSTED_ORIGINS)
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"  # secret key

# 5. Initialise DB (starts empty — db.sqlite3 is gitignored)
cd web_backend
sudo ../.venv/bin/python manage.py migrate
sudo ../.venv/bin/python manage.py collectstatic --noinput
sudo ../.venv/bin/python manage.py createsuperuser
sudo ../.venv/bin/python manage.py create_apikey entrance-01   # copy the printed key
sudo chown -R www-data:www-data /opt/queueTracker_roskilde/web_backend

# 6. gunicorn under systemd
sudo cp deploy/queuetracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now queuetracker

# 7. Caddy in front (edit the domain first)
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Then point the device at `https://<your-domain>/api/log` with the API key from
step 5. **See `deploy/DEPLOY.md` for the full guide**, including a note on
ESP32 + HTTPS, updating an existing deployment, and SQLite backups.
