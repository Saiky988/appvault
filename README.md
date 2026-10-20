# AppVault — Self-Hosted Application Catalog Tracker

AppVault is a self-hosted public app and game catalog tracker built with **FastAPI**, **discord.py 2.x**, **SQLAlchemy 2.x**, and **SQLite/PostgreSQL**.

It runs three integrated services sharing a single database and environment configuration:
1. **FastAPI REST API**: Public catalog endpoints (`/api/v1/*`) and Bearer-authenticated admin operations (`/api/v1/admin/*`).
2. **Discord Bot**: Slash commands for searching apps, browsing sources, checking health, and configuring alert subscriptions.
3. **Background Worker**: Resilient poller with SSRF safeguards, conditional HTTP (`ETag`/`Last-Modified`), credential scrubbing, DB lease locks, and deduplicated Discord alerts with exponential backoff.

---

## 1. Quick Start on Ubuntu 22.04 / 24.04 VPS

### Prerequisites
* Python 3.11+ (Python 3.12 recommended)
* `sqlite3`, `libxml2`, `libxslt1.1`, `ca-certificates`, `openssl`
* Optional: `cloudflared` (Cloudflare Tunnel)

### Installation Commands

```bash
# 1. Install system dependencies
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip libxml2 libxslt1.1 sqlite3 ca-certificates openssl

# 2. Create service user and app directory
sudo useradd --system --user-group --create-home --home-dir /opt/appvault --shell /usr/sbin/nologin appvault
sudo mkdir -p /opt/appvault /var/lib/appvault /etc/appvault
sudo cp -r . /opt/appvault/
sudo chown -R appvault:appvault /opt/appvault /var/lib/appvault
sudo chmod 750 /var/lib/appvault

# 3. Create virtual environment & install requirements
sudo -u appvault python3 -m venv /opt/appvault/.venv
sudo -u appvault /opt/appvault/.venv/bin/pip install --upgrade pip wheel
sudo -u appvault /opt/appvault/.venv/bin/pip install -r /opt/appvault/requirements.txt

# 4. Configure environment file
sudo cp /opt/appvault/.env.example /etc/appvault/appvault.env
sudo chown root:appvault /etc/appvault/appvault.env
sudo chmod 640 /etc/appvault/appvault.env
sudo nano /etc/appvault/appvault.env

# 5. Run database migrations & load seed catalog
sudo -u appvault bash -c "set -a && source /etc/appvault/appvault.env && set +a && cd /opt/appvault && ./.venv/bin/alembic upgrade head && ./.venv/bin/python -m scripts.seed_catalog"

# 6. Synchronize Discord slash commands
sudo -u appvault bash -c "set -a && source /etc/appvault/appvault.env && set +a && cd /opt/appvault && ./.venv/bin/python -m app.bot.client --sync"

# 7. Install and enable systemd units
sudo cp /opt/appvault/deploy/appvault-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now appvault-api.service appvault-worker.service appvault-bot.service
```

---

## 2. Exposing API via Cloudflare Tunnel (`cloudflared`)

Because AppVault listens on `127.0.0.1:8000` by default, you do not need Nginx or open firewall ports. Connect `cloudflared` directly to the loopback address.

In `/etc/cloudflared/config.yml`:

```yaml
tunnel: <YOUR-TUNNEL-UUID>
credentials-file: /etc/cloudflared/<YOUR-TUNNEL-UUID>.json

ingress:
  - hostname: appvault.yourdomain.com
    service: http://127.0.0.1:8000
    originRequest:
      connectTimeout: 10s
      noTLSVerify: false
  - service: http_status:404
```

Restart the tunnel:
```bash
sudo systemctl restart cloudflared
```

---

## 3. Environment Variables Reference

Edit `/etc/appvault/appvault.env`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `APP_ENV` | `production` | Environment mode (`development`, `testing`, `production`) |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DATABASE_URL` | `sqlite:////var/lib/appvault/appvault.db` | Database URL (SQLite path or PostgreSQL connection string) |
| `API_HOST` | `127.0.0.1` | Host to bind API server (keep `127.0.0.1` behind Cloudflare Tunnel) |
| `API_PORT` | `8000` | Port for FastAPI server |
| `API_ADMIN_TOKEN` | *Required* | High-entropy Bearer token protecting admin endpoints |
| `RATE_LIMIT_PER_MINUTE`| `120` | Max requests per minute per client IP |
| `DISCORD_BOT_TOKEN` | *Required* | Discord Bot token from Developer Portal |
| `DISCORD_ADMIN_USER_IDS`| `""` | Comma-separated Discord User IDs allowed to run `/admin` and `/check` |
| `DISCORD_GUILD_ID` | `""` | Optional dev guild ID for instant slash command sync |
| `MONITOR_INTERVAL_SECONDS`| `300` | Global monitoring interval (5 minutes) |
| `MONITOR_CONCURRENCY` | `3` | Max concurrent source checks |
| `HTTP_TIMEOUT_SECONDS` | `15` | Timeout for fetching public pages |
| `MAX_RESPONSE_BYTES` | `2097152` | Maximum download size per page (2 MiB) |
| `MAX_RETRIES` | `3` | Retries for transient HTTP/network errors |

---

## 4. Discord Slash Commands

* `/search query:<text>` — Search games across canonical titles, source titles, aliases, and fuzzy matches.
* `/app name:<text>` — Show app details, tracked sources, status, and recent change history.
* `/catalog [source_id]` — Browse apps in a specific source or page through the entire catalog.
* `/source id:<integer>` — View source status, polling interval, last checked/success time, and public URL.
* `/updates [limit]` — Show recent change events with timestamps and source links.
* `/subscribe channel:<channel>` — Subscribe a channel to automated change alerts (`Manage Channels` permission required).
* `/unsubscribe` — Disable change alerts for the current channel.
* `/status` — View bot latency, DB health, worker heartbeat, and source status breakdown.
* `/check source_id:<integer>` — Request an immediate manual check for a source (authorized admins only).
* `/admin pause` / `/admin resume` — Pause or resume the background monitoring loop globally.
* `/admin check-all` — Check all enabled sources concurrently subject to rate limits.
* `/admin reload-catalog` — Reload seed records idempotently without deleting history or subscriptions.

---

## 5. REST API Endpoints (`/api/v1`)

### Public Endpoints
* `GET /api/v1/health` — Service and database health check.
* `GET /api/v1/stats` — High-level statistics on apps, sources, events, and worker state.
* `GET /api/v1/apps?limit=20&offset=0` — Paginated canonical application catalog.
* `GET /api/v1/apps/search?q=...&limit=20&offset=0` — Ranked search with match types (`exact`, `case_insensitive`, `alias`, `partial`, `fuzzy`).
* `GET /api/v1/apps/{app_id}` — App detail with linked sources and recent change events.
* `GET /api/v1/sources?limit=20&offset=0` — List of tracked sources with health badges.
* `GET /api/v1/sources/{source_id}` — Single source configuration and monitoring status.
* `GET /api/v1/sources/{source_id}/apps?limit=50&offset=0` — Apps on a specific source in original display order.
* `GET /api/v1/updates?limit=20&offset=0` — List of detected source change events.
* `GET /api/v1/updates/{event_id}` — Single change event detail.

### Admin Endpoints (Require `Authorization: Bearer <API_ADMIN_TOKEN>`)
* `POST /api/v1/admin/sources/{source_id}/check` — Trigger manual check for a source.
* `POST /api/v1/admin/check-all` — Trigger bounded check across all enabled sources.
* `POST /api/v1/admin/pause` — Globally pause background polling worker.
* `POST /api/v1/admin/resume` — Resume background polling worker.
* `POST /api/v1/admin/catalog/reload` — Idempotently reload seed records.

---

## 6. Service Management & Maintenance

```bash
# Check service status
sudo systemctl status appvault-api appvault-worker appvault-bot

# View real-time logs
sudo journalctl -u appvault-api -u appvault-worker -u appvault-bot -f

# Restart services
sudo systemctl restart appvault-api appvault-worker appvault-bot

# Stop all services
sudo systemctl stop appvault-bot appvault-worker appvault-api

# Backup database
sudo cp /var/lib/appvault/appvault.db /var/lib/appvault/appvault.db.bak.$(date +%F_%H%M)
```

---

## 7. Security & Anti-Leak Safeguards

* **No Credential Storage**: Passwords, Apple IDs, email addresses, 2FA codes, and session cookies are never extracted, stored, logged, or returned.
* **SSRF Protection**: Outbound requests reject loopback (`127.0.0.0/8`), private RFC1918 IPs, link-local metadata addresses (`169.254.169.254`), and unsafe redirects.
* **Data Resilience**: Unreachable or broken pages never erase existing catalog apps. Seed data remains preserved with an explicit error state until a successful live check is recorded.