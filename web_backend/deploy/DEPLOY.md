# Deploying the queueTracker backend to a VPS (gunicorn + Caddy)

Production stack: **gunicorn** runs the Django app on `127.0.0.1:8000`,
**Caddy** sits in front as a reverse proxy and provides automatic HTTPS,
**WhiteNoise** serves the admin's static files, and the data lives in SQLite.

These steps assume Ubuntu/Debian and the repo cloned to
`/opt/queueTracker_roskilde`. Adjust paths to taste (and match
`deploy/queuetracker.service` if you change them).

## 1. DNS

Point your domain's `A` record (and `AAAA` if you have IPv6) at the server's IP.
Caddy can't get a certificate until DNS resolves to the box.

## 2. System packages

Caddy isn't in the default Ubuntu repositories, so add its official
repo before installing it.

```bash
# Base packages
sudo apt update
sudo apt install -y python3 python3-venv git debian-keyring debian-archive-keyring apt-transport-https curl

# Add Caddy's official repository
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list

# Install Caddy
sudo apt update
sudo apt install -y caddy
```

## 3. Get the code and install deps

```bash
sudo git clone <your-repo-url> /opt/queueTracker_roskilde
cd /opt/queueTracker_roskilde
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r web_backend/requirements.txt
```

## 4. Production environment file

Create `/opt/queueTracker_roskilde/web_backend/.env` (copy `.env.example` and
edit). The systemd unit loads it. Minimum for production:

```bash
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<paste a long random string>
DJANGO_ALLOWED_HOSTS=counter.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://counter.example.com
```

Generate a secret key:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
```

## 5. Initialise the database (starts empty — no dev/sample data)

`db.sqlite3` is gitignored, so the server starts with a clean database.

```bash
cd /opt/queueTracker_roskilde/web_backend
sudo ../.venv/bin/python manage.py migrate
sudo ../.venv/bin/python manage.py collectstatic --noinput
sudo ../.venv/bin/python manage.py createsuperuser
sudo ../.venv/bin/python manage.py create_apikey entrance-01   # copy the key it prints
```

Make sure the service user can read/write the DB and static dir:

```bash
sudo chown -R www-data:www-data /opt/queueTracker_roskilde/web_backend
```

## 6. Run gunicorn under systemd

```bash
sudo cp deploy/queuetracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now queuetracker
sudo systemctl status queuetracker          # should be "active (running)"
```

## 7. Put Caddy in front

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile               # set your real domain
sudo systemctl reload caddy
```

Visit `https://counter.example.com/` (public) and `/advanced/` (login). Caddy
will have fetched a TLS cert automatically.

## 8. Point the device at production

In `esp32/src/config.py`:

```python
LOG_URL    = "https://counter.example.com/api/log"
AUTH_TOKEN = "<the key from step 5>"
```

> ⚠️ **ESP32 + HTTPS:** MicroPython's `urequests` can do TLS but it's
> memory-heavy and does little/no certificate validation. If the device
> struggles to POST over HTTPS, keep the **device → server** hop on the local
> network over HTTP (e.g. expose `/api/log` on the LAN, or run the device on the
> same network) while still serving the **dashboards** publicly over HTTPS.

## Updating later

```bash
cd /opt/queueTracker_roskilde && sudo git pull
sudo .venv/bin/pip install -r web_backend/requirements.txt
cd web_backend
sudo ../.venv/bin/python manage.py migrate
sudo ../.venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart queuetracker
```

## Backups

Everything is in one SQLite file. Back it up with:

```bash
sqlite3 /opt/queueTracker_roskilde/web_backend/db.sqlite3 ".backup backup.sqlite3"
```
