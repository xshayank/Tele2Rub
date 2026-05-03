# How to Run Iran and Kharej Servers Locally

This guide walks you through running the **Iran** (web API / UI) and **Kharej** (worker / downloader)
services side-by-side on your local machine for development or testing.

The two processes communicate through a shared **Rubika** account pair and an **Arvan S2** (S3-compatible)
object-storage bucket. Iran sends `job.create` control messages over Rubika; Kharej downloads the
requested media, uploads the result to S2, and sends `job.done` / `job.error` events back over
Rubika. Iran then serves the media to end-users via pre-signed S2 URLs.

> **Platform note** — all shell examples below use Linux/macOS (bash/zsh). Windows users can run the
> same commands inside WSL2, or replace `source .venv/bin/activate` with
> `.venv\Scripts\activate.bat` (cmd) / `.venv\Scripts\Activate.ps1` (PowerShell).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Repository layout](#2-repository-layout)
3. [Install dependencies](#3-install-dependencies)
4. [Configure Iran](#4-configure-iran)
5. [Configure Kharej](#5-configure-kharej)
6. [How the two processes communicate](#6-how-the-two-processes-communicate)
7. [Start the servers](#7-start-the-servers)
8. [Verify things are working](#8-verify-things-are-working)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| **Python** | 3.10 | `python --version` |
| **pip** | 23.x | bundled with Python 3.10+ |
| **virtualenv / venv** | any | built in as `python -m venv` |
| **Rubika accounts** | — | two separate Rubika accounts: one for Iran, one for Kharej |
| **Arvan S2 bucket** | — | one bucket; Kharej needs **write** credentials, Iran needs **read-only** credentials |
| **PostgreSQL** *(Iran only)* | 14+ | or use `sqlite+aiosqlite:///./dev.db` for quick local testing |

Optional tools used only if you enable specific features:

- **Redis** — not required by any current step; reserved for future caching.
- **MinIO** — can replace Arvan S2 for fully offline local testing (see [Troubleshooting](#9-troubleshooting)).

---

## 2. Repository layout

```
RubeTunes/
├── iran/                    # FastAPI ASGI service (the "Iran VPS")
│   ├── __main__.py          # python -m iran  →  uvicorn server
│   ├── config.py            # IranSettings — env prefix IRAN_
│   ├── requirements.txt
│   └── .env.example         # copy → iran/.env  (authoritative template)
└── kharej/                  # Async worker (the "Kharej VPS")
    ├── __main__.py          # python -m kharej  →  worker loop
    ├── worker.py            # CLI + run loop
    ├── settings.py          # KharejSettings — env prefix KHAREJ_
    ├── rubika_client.py     # RubikaConfig — reads RUBIKA_SESSION_KHAREJ, IRAN_RUBIKA_ACCOUNT_GUID
    ├── s2_client.py         # S2Config — reads ARVAN_S2_* vars
    └── requirements.txt
```

---

## 3. Install dependencies

Create a single virtual environment at the repo root and install both sets of dependencies:

```bash
cd /path/to/RubeTunes

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate.bat

# Iran dependencies (FastAPI, SQLAlchemy, rubpy, boto3, …)
pip install -r iran/requirements.txt

# Kharej dependencies (worker, boto3, rubpy, prometheus-client, …)
pip install -r kharej/requirements.txt
```

> **Tip** — if you see dependency conflicts between the two `requirements.txt` files, create
> two separate virtual environments (`.venv-iran` and `.venv-kharej`) and install each set
> independently.

---

## 4. Configure Iran

### 4a. Create `iran/.env`

The authoritative template is `iran/.env.example`.  Copy and edit it:

```bash
cp iran/.env.example iran/.env
$EDITOR iran/.env
```

All Iran settings are read via `pydantic-settings` with the **`IRAN_`** prefix
(source: `iran/config.py`).  This means every variable in `.env` must be prefixed with `IRAN_`.

### 4b. Minimal `iran/.env` for local development

```dotenv
# iran/.env  (local dev — never commit this file)

# ── HTTP server ───────────────────────────────────────────────────────────────
# IRAN_HOST=0.0.0.0          # default: 0.0.0.0
# IRAN_PORT=8000             # default: 8000

# ── Security ──────────────────────────────────────────────────────────────────
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
IRAN_SECRET_KEY=change-me-for-local-dev

# ── Database ──────────────────────────────────────────────────────────────────
# PostgreSQL (recommended):
IRAN_DATABASE_URL=postgresql+asyncpg://iran:password@localhost:5432/rubetunes_iran
# SQLite (quick local test — no Postgres needed):
# IRAN_DATABASE_URL=sqlite+aiosqlite:///./dev_iran.db

# ── Rubika transport ──────────────────────────────────────────────────────────
# Session name for the Iran-side Rubika account (rubpy session file)
IRAN_RUBIKA_SESSION_IRAN=iran_session
# GUID of the Kharej Rubika account (so Iran knows where to send job.create)
IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID=<kharej-rubika-guid>
# GUID of the Iran Rubika account (used to reject echo-back messages)
IRAN_IRAN_RUBIKA_ACCOUNT_GUID=<iran-rubika-guid>

# ── Arvan S2 (read-only) ──────────────────────────────────────────────────────
IRAN_S2_ENDPOINT_URL=https://s3.ir-thr-at1.arvanstorage.ir
IRAN_S2_ACCESS_KEY=<read-only-access-key>
IRAN_S2_SECRET_KEY=<read-only-secret-key>
IRAN_S2_BUCKET=rubetunes-media

# ── Feature toggles ───────────────────────────────────────────────────────────
# IRAN_S2_PRESIGN_EXPIRE_SECONDS=3600   # default: 3600
# IRAN_MAX_JOBS_PER_HOUR=10             # default: 10

# ── Database migrations ───────────────────────────────────────────────────────
# Set to 0 to skip Alembic migrations on startup (e.g. in CI or dev without a DB).
# Default is 1 (run migrations automatically on every startup).
# IRAN_RUN_MIGRATIONS=1

# ── Logging ───────────────────────────────────────────────────────────────────
IRAN_LOG_LEVEL=INFO
IRAN_LOG_FORMAT=text       # "text" (human-readable) or "json" (structured)
```

> The `IRAN_` prefix is mandatory.  `IRAN_SECRET_KEY` is required at startup;
> the server will refuse to start if it is missing or empty.
> `IRAN_DATABASE_URL` is required for any endpoint that touches the database.
>
> Source files to cross-check: `iran/config.py`, `iran/.env.example`.

### 4c. Database migrations

**Automatic (default):** The Iran server runs `alembic upgrade head` automatically on every startup.
Alembic is idempotent — already-applied revisions are skipped.  The migration scripts live at
`iran/db/migrations/` and the Alembic configuration is at `iran/db/alembic.ini`.

**Manual:** If you need to apply or inspect migrations outside of a running server:

```bash
cd /path/to/RubeTunes
source .venv/bin/activate
export $(grep -v '^#' iran/.env | xargs)   # load env vars

# Apply all Alembic migrations
alembic --config iran/db/alembic.ini upgrade head

# Show current revision
alembic --config iran/db/alembic.ini current
```

**Skip migrations in dev/CI:** Set `IRAN_RUN_MIGRATIONS=0` in `iran/.env` (or as an environment
variable) to skip Alembic entirely on startup.  Useful when no live database is available, or
during unit-test runs.  The default is `1` so production deployments are always kept up-to-date.

```dotenv
# iran/.env  — disable auto-migrations for a run without a real DB
IRAN_RUN_MIGRATIONS=0
```

---

## 5. Configure Kharej

Kharej settings come from **two sources** (lower priority first):

1. Environment variables prefixed with `KHAREJ_` — loaded at startup via `kharej/settings.py`.
2. A JSON settings file at `kharej/state/kharej_settings.json` — written at runtime by the
   `admin.settings.update` control message; **disk values override env vars**.

The Rubika and S2 configs use their own, unprefixed env var names (source: `kharej/rubika_client.py`
and `kharej/s2_client.py`).

### 5a. Minimal `kharej/.env` for local development

```dotenv
# kharej/.env  (local dev — never commit this file)

# ── Rubika transport ──────────────────────────────────────────────────────────
# Session name for the Kharej-side Rubika account (rubpy session file)
RUBIKA_SESSION_KHAREJ=kharej_session
# GUID of the Iran Rubika account (so Kharej knows whose messages to accept)
IRAN_RUBIKA_ACCOUNT_GUID=<iran-rubika-guid>

# ── Arvan S2 (write access) ───────────────────────────────────────────────────
ARVAN_S2_ENDPOINT=https://s3.ir-thr-at1.arvanstorage.ir
ARVAN_S2_ACCESS_KEY_WRITE=<write-access-key>
ARVAN_S2_SECRET_WRITE=<write-secret-key>
ARVAN_S2_BUCKET=rubetunes-media
# ARVAN_S2_REGION=ir-thr-at1        # default: ir-thr-at1

# ── Optional worker knobs (KHAREJ_ prefix) ────────────────────────────────────
# These map to KharejSettings keys (source: kharej/settings.py).
# KHAREJ_MAX_PARALLEL=2
# KHAREJ_SHUTDOWN_TIMEOUT_SEC=60
```

> **Important**: Kharej's Rubika vars (`RUBIKA_SESSION_KHAREJ`, `IRAN_RUBIKA_ACCOUNT_GUID`) and
> S2 vars (`ARVAN_S2_*`) are **not** prefixed with `KHAREJ_`.  They are read directly by
> `RubikaConfig.from_env()` and `S2Config.from_env()` in `kharej/rubika_client.py` and
> `kharej/s2_client.py` respectively.

---

## 6. How the two processes communicate

```
┌─────────────┐   RTUNES:: envelope over Rubika text channel   ┌──────────────┐
│  Iran VPS   │ ──────── job.create ──────────────────────────► │  Kharej VPS  │
│  (FastAPI)  │ ◄──── job.accepted / job.progress / job.done ── │   (Worker)   │
└──────┬──────┘                                                  └──────┬───────┘
       │  read (presign URLs)                    write (upload files)   │
       └──────────────────────► Arvan S2 ◄───────────────────────────┘
```

- **Rubika transport**: both processes use the `rubpy` library to connect to separate Rubika
  accounts.  Iran sends to `IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID`; Kharej accepts messages only from
  `IRAN_RUBIKA_ACCOUNT_GUID`.  The two GUIDs must cross-match.
- **Arvan S2**: the same bucket is used.  Kharej uploads with write credentials
  (`ARVAN_S2_ACCESS_KEY_WRITE` / `ARVAN_S2_SECRET_WRITE`).  Iran reads with read-only credentials
  (`IRAN_S2_ACCESS_KEY` / `IRAN_S2_SECRET_KEY`).
- **No direct HTTP connection** between Iran and Kharej is required.  They are fully decoupled
  through the Rubika message channel and the S2 bucket.

---

## 7. Start the servers

Open **two terminals** (both with the virtual environment activated).

### Terminal 1 — Iran (HTTP server)

```bash
cd /path/to/RubeTunes
source .venv/bin/activate
export $(grep -v '^#' iran/.env | xargs)

python -m iran
# or, to override host/port without editing .env:
python -m iran --host 127.0.0.1 --port 8000
```

Expected output (text format):

```
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

| Option | Default | Description |
|--------|---------|-------------|
| `IRAN_HOST` | `0.0.0.0` | Interface to bind |
| `IRAN_PORT` | `8000` | TCP port |

Validate config without starting the server:

```bash
python -m iran --check-config
```

### Terminal 2 — Kharej (worker)

```bash
cd /path/to/RubeTunes
source .venv/bin/activate
export $(grep -v '^#' kharej/.env | xargs)

python -m kharej
# or equivalently:
python -m kharej.worker
```

Expected output (JSON log, abbreviated):

```json
{"ts": "...", "level": "INFO", "logger": "kharej.worker", "msg": "KharejSettings loaded", ...}
{"ts": "...", "level": "INFO", "logger": "kharej.rubika", "msg": "Connecting to Rubika", ...}
```

Validate config without starting the worker:

```bash
python -m kharej.worker --check-config
# prints a redacted JSON summary of Rubika + S2 config

python -m kharej.worker --healthcheck
# probes Rubika connectivity and S2 access; exits 0 if healthy
```

---

## 8. Verify things are working

### Iran health endpoint

```bash
curl -s http://localhost:8000/healthz
# Expected: {"status": "ok"}
```

### Iran API root

```bash
curl -s http://localhost:8000/
# Returns the web UI HTML page (Jinja2 template)
```

### Validate end-to-end flow

1. Register a user on Iran:

   ```bash
   curl -s -X POST http://localhost:8000/auth/register \
     -H 'Content-Type: application/json' \
     -d '{"username": "testuser", "password": "testpass"}'
   ```

2. Log in to obtain a session cookie:

   ```bash
   curl -sc cookies.txt -X POST http://localhost:8000/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username": "testuser", "password": "testpass"}'
   ```

3. Submit a download job:

   ```bash
   curl -sb cookies.txt -X POST http://localhost:8000/jobs \
     -H 'Content-Type: application/json' \
     -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
   # Returns: {"id": "<uuid>", "status": "pending", ...}
   ```

4. Watch **Terminal 2** (Kharej): you should see `job.create` received, followed by download
   progress events, and finally `job.done`.

5. Poll the job status on Iran:

   ```bash
   curl -sb cookies.txt http://localhost:8000/jobs/<uuid>
   # status transitions: pending → accepted → running → completed
   ```

6. Stream SSE events (optional):

   ```bash
   curl -sb cookies.txt http://localhost:8000/jobs/<uuid>/events
   # SSE stream — keep the terminal open to watch live progress
   ```

---

## 9. Troubleshooting

### Port already in use

```
ERROR:    [Errno 98] Address already in use
```

Change the port:

```bash
python -m iran --port 8001
# or set IRAN_PORT=8001 in iran/.env
```

### Missing required environment variable (Iran)

```
Configuration error: 1 validation error for IranSettings
IRAN_SECRET_KEY
  Value error, ...
```

Ensure `IRAN_SECRET_KEY` is set in `iran/.env` and that you loaded it with
`export $(grep -v '^#' iran/.env | xargs)` before running `python -m iran`.

### Missing required environment variable (Kharej)

```
ERROR: RubikaConfig: Missing required environment variables: RUBIKA_SESSION_KHAREJ
```

Check that `RUBIKA_SESSION_KHAREJ` and `IRAN_RUBIKA_ACCOUNT_GUID` are set.
Run `python -m kharej.worker --check-config` for a full diagnostic.

### Database connection refused (Iran)

```
sqlalchemy.exc.OperationalError: ... could not connect to server
```

- Make sure PostgreSQL is running: `pg_lsclusters` (Debian) or `brew services list` (macOS).
- For quick local testing without Postgres, switch to SQLite:
  ```dotenv
  IRAN_DATABASE_URL=sqlite+aiosqlite:///./dev_iran.db
  ```

### Alembic: target database is not up to date

```
alembic.util.exc.CommandError: Target database is not up to date.
```

Run migrations:

```bash
export $(grep -v '^#' iran/.env | xargs)
alembic --config iran/alembic.ini upgrade head
```

### S2 credential errors (Kharej)

```
S2AccessDenied: Access Denied
```

- Kharej needs **write** credentials (`ARVAN_S2_ACCESS_KEY_WRITE` / `ARVAN_S2_SECRET_WRITE`).
- Iran needs **read-only** credentials (`IRAN_S2_ACCESS_KEY` / `IRAN_S2_SECRET_KEY`).
- Do not swap them — Kharej uploads files; Iran only reads/presigns.

### Rubika messages not flowing (jobs stuck in `pending`)

- Verify the GUIDs are correct and cross-matched:
  - `IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID` in Iran ↔ the GUID of the account used by Kharej.
  - `IRAN_RUBIKA_ACCOUNT_GUID` in Kharej ↔ the GUID of the account used by Iran.
- Check that both Rubika accounts have an active `rubpy` session.  If this is the first run,
  `rubpy` will prompt for a phone-number OTP and create a session file on disk.
- Run `python -m kharej.worker --healthcheck` to probe Rubika connectivity directly.

### Fully offline local development (MinIO instead of Arvan S2)

If you do not have Arvan credentials, you can use [MinIO](https://min.io) locally:

```bash
# Start MinIO in Docker
docker run -d -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  --name minio quay.io/minio/minio server /data --console-address ":9001"

# Create the bucket
docker exec minio mc alias set local http://localhost:9000 minioadmin minioadmin
docker exec minio mc mb local/rubetunes-media
```

Then update your env files:

```dotenv
# Kharej
ARVAN_S2_ENDPOINT=http://localhost:9000
ARVAN_S2_ACCESS_KEY_WRITE=minioadmin
ARVAN_S2_SECRET_WRITE=minioadmin
ARVAN_S2_BUCKET=rubetunes-media
ARVAN_S2_REGION=us-east-1

# Iran
IRAN_S2_ENDPOINT_URL=http://localhost:9000
IRAN_S2_ACCESS_KEY=minioadmin
IRAN_S2_SECRET_KEY=minioadmin
IRAN_S2_BUCKET=rubetunes-media
```

> **Note**: pre-signed URLs generated against `http://localhost:9000` are only valid from the
> same machine.  This is fine for local development.

---

## Summary of environment variables

### Iran (prefix `IRAN_`) — source: `iran/config.py`

| Variable | Required | Default | Description |
|---|---|---|---|
| `IRAN_SECRET_KEY` | **yes** | — | JWT signing key; generate with `secrets.token_hex(32)` |
| `IRAN_DATABASE_URL` | **yes** | — | SQLAlchemy async database URL |
| `IRAN_RUBIKA_SESSION_IRAN` | for Rubika | `""` | rubpy session name (Iran account) |
| `IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID` | for Rubika | `""` | GUID of the Kharej Rubika account |
| `IRAN_IRAN_RUBIKA_ACCOUNT_GUID` | for Rubika | `""` | GUID of the Iran Rubika account |
| `IRAN_S2_ENDPOINT_URL` | for S2 | `""` | Arvan S2 endpoint URL |
| `IRAN_S2_ACCESS_KEY` | for S2 | `""` | S2 read-only access key |
| `IRAN_S2_SECRET_KEY` | for S2 | `""` | S2 read-only secret key |
| `IRAN_S2_BUCKET` | for S2 | `""` | S2 bucket name |
| `IRAN_S2_PRESIGN_EXPIRE_SECONDS` | no | `3600` | Presigned URL TTL in seconds |
| `IRAN_MAX_JOBS_PER_HOUR` | no | `10` | Per-user job rate limit |
| `IRAN_HOST` | no | `0.0.0.0` | HTTP bind address |
| `IRAN_PORT` | no | `8000` | HTTP port |
| `IRAN_LOG_LEVEL` | no | `INFO` | Python log level |
| `IRAN_LOG_FORMAT` | no | `json` | `json` or `text` |
| `IRAN_ACCESS_TOKEN_EXPIRE_MINUTES` | no | `15` | JWT access token lifetime |
| `IRAN_REFRESH_TOKEN_EXPIRE_DAYS` | no | `7` | JWT refresh token lifetime |

### Kharej — source: `kharej/rubika_client.py`, `kharej/s2_client.py`, `kharej/settings.py`

| Variable | Required | Default | Description |
|---|---|---|---|
| `RUBIKA_SESSION_KHAREJ` | **yes** | — | rubpy session name (Kharej account) |
| `IRAN_RUBIKA_ACCOUNT_GUID` | **yes** | — | GUID of the Iran Rubika account |
| `ARVAN_S2_ENDPOINT` | **yes** | — | Arvan S2 endpoint URL |
| `ARVAN_S2_ACCESS_KEY_WRITE` | **yes** | — | S2 **write** access key |
| `ARVAN_S2_SECRET_WRITE` | **yes** | — | S2 **write** secret key |
| `ARVAN_S2_BUCKET` | **yes** | — | S2 bucket name |
| `ARVAN_S2_REGION` | no | `ir-thr-at1` | Arvan region |
| `KHAREJ_*` | no | — | Any arbitrary worker knob (stored in `KharejSettings`) |
