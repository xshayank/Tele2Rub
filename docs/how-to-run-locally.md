# How to Run RubeTunes Locally (Iran + Kharej)

This guide explains how to start both sides of the RubeTunes service — the
**Iran VPS** (Track B web service) and the **Kharej VPS** (Track A worker) —
on a single developer machine for local testing.

> **Architecture summary**: Iran receives download requests from end-users over
> HTTP, forwards them to Kharej over a Rubika control channel, Kharej performs
> the actual media download and uploads it to Arvan S2, then publishes
> completion events back to Iran so the web UI can stream progress to the
> browser. See
> [`docs/research/arvan-webui-migration/architecture.md`](research/arvan-webui-migration/architecture.md)
> for the full picture.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Both services require Python 3.11 or newer |
| Two Rubika accounts | One for the Iran side, one for the Kharej side |
| Arvan S2 bucket | With one **read-only** and one **write** credential pair |
| `yt-dlp` binary | In `$PATH` on the Kharej machine |
| (Optional) Sentry project | For error reporting |

---

## Step 1 — Clone and create a virtual environment

```bash
git clone https://github.com/xshayank/RubeTunes.git
cd RubeTunes
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

---

## Step 2 — Install dependencies

```bash
# Iran-side dependencies
pip install -r iran/requirements.txt

# Kharej-side dependencies
pip install -r kharej/requirements.txt

# Shared rubetunes library (needed by Kharej downloaders)
pip install -r requirements.txt
```

---

## Step 3 — Configure Iran

Create `iran/.env` from the example:

```bash
cp iran/.env.example iran/.env
```

Edit `iran/.env` and fill in the required values (see
[`iran/.env.example`](../iran/.env.example) for all options with comments).

Minimum required settings:

```dotenv
# iran/.env (minimum)
IRAN_SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
IRAN_DATABASE_URL=sqlite+aiosqlite:///iran_dev.db

# Rubika — Iran-side session
IRAN_RUBIKA_SESSION_IRAN=<base64-encoded rubpy session for the Iran account>
IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID=<kharej-rubika-guid>
IRAN_IRAN_RUBIKA_ACCOUNT_GUID=<iran-rubika-guid>

# Arvan S2 (read-only credentials)
IRAN_S2_ENDPOINT_URL=https://s3.ir-thr-at1.arvanstorage.ir
IRAN_S2_ACCESS_KEY=<read-access-key>
IRAN_S2_SECRET_KEY=<read-secret-key>
IRAN_S2_BUCKET=rubetunes-media
```

> **Note**: Iran settings use the `IRAN_` prefix (e.g. `IRAN_PORT=8000`).
> Source: [`iran/config.py`](../iran/config.py).

---

## Step 4 — Configure Kharej

Create `.env.kharej` from the example:

```bash
cp .env.kharej.example .env.kharej
```

Edit `.env.kharej` and fill in at minimum the six required variables. See the
[Kharej environment variables](#kharej-environment-variables) section below for
a full reference of every variable.

Minimum required settings:

```dotenv
# .env.kharej (minimum)
RUBIKA_SESSION_KHAREJ=<kharej-rubpy-session-name>
IRAN_RUBIKA_ACCOUNT_GUID=<iran-rubika-guid>

ARVAN_S2_ENDPOINT=https://s3.ir-thr-at1.arvanstorage.ir
ARVAN_S2_ACCESS_KEY_WRITE=<write-access-key>
ARVAN_S2_SECRET_WRITE=<write-secret-key>
ARVAN_S2_BUCKET=rubetunes-media
```

To validate the configuration before running:

```bash
export $(grep -v '^#' .env.kharej | xargs)
python -m kharej.worker --check-config
```

---

## Step 5 — Start Kharej (Terminal 1)

```bash
source .venv/bin/activate
export $(grep -v '^#' .env.kharej | xargs)
python -m kharej.worker
```

Expected output (JSON log lines on stderr):

```
{"ts":"...","level":"INFO","logger":"kharej.settings","msg":"KharejSettings loaded",...}
{"ts":"...","level":"INFO","logger":"kharej.worker","msg":{"event":"worker.started"}}
```

### CLI options

```
python -m kharej.worker --help            # show all options
python -m kharej.worker --version         # print kharej version
python -m kharej.worker --check-config    # validate env vars (no side-effects)
python -m kharej.worker --healthcheck     # probe Rubika + S2, exit 0 if healthy
python -m kharej.worker --debug           # enable DEBUG logging
```

---

## Step 6 — Start Iran (Terminal 2)

```bash
source .venv/bin/activate
# Iran reads iran/.env automatically via pydantic-settings
python -m iran
```

Expected output:

```
INFO:     Started server process [...]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### CLI options

```
python -m iran --help          # show all options
python -m iran --version       # print iran version
python -m iran --check-config  # validate env-var configuration
python -m iran --port 9000     # override listen port
```

### Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"iran","version":"0.1.0","contract_version":1}
```

---

## Step 7 — Verify the end-to-end link

1. Open [http://localhost:8000/docs](http://localhost:8000/docs) for the
   interactive API playground.
2. Register a user and log in via the API or the web UI at
   [http://localhost:8000](http://localhost:8000).
3. Submit a download job (POST `/jobs`). Iran will forward a `job.create`
   message to Kharej over Rubika.
4. Watch both terminals:
   - Kharej logs `job.accepted` then incremental `job.progress` events.
   - Iran logs corresponding SSE fan-out to the browser.
5. When the job completes Kharej uploads the file to S2 and publishes
   `job.completed`. Iran marks the job done and makes the download link
   available.

---

## Kharej environment variables

The tables below document every environment variable read by the Kharej worker
and the shared `rubetunes/` library modules it uses.

### Required variables

These must be set; `python -m kharej.worker --check-config` exits non-zero if
any are missing.

| Variable | Description | Source |
|---|---|---|
| `RUBIKA_SESSION_KHAREJ` | Rubika session name for the Kharej account. Treat as a secret — it grants full account access. | [`kharej/rubika_client.py`](../kharej/rubika_client.py) |
| `IRAN_RUBIKA_ACCOUNT_GUID` | Rubika GUID of the Iran-side account. Messages from any other sender are silently rejected. | [`kharej/rubika_client.py`](../kharej/rubika_client.py) |
| `ARVAN_S2_ENDPOINT` | Arvan S2 endpoint URL (e.g. `https://s3.ir-thr-at1.arvanstorage.ir`). | [`kharej/s2_client.py`](../kharej/s2_client.py) |
| `ARVAN_S2_ACCESS_KEY_WRITE` | Arvan S2 write-capable access key. Do **not** use the Iran read-only key here. | [`kharej/s2_client.py`](../kharej/s2_client.py) |
| `ARVAN_S2_SECRET_WRITE` | Arvan S2 write-capable secret key. | [`kharej/s2_client.py`](../kharej/s2_client.py) |
| `ARVAN_S2_BUCKET` | Arvan S2 bucket name (must match Iran's `IRAN_S2_BUCKET`). | [`kharej/s2_client.py`](../kharej/s2_client.py) |

### Optional — Arvan S2

| Variable | Default | Description | Source |
|---|---|---|---|
| `ARVAN_S2_REGION` | `ir-thr-at1` | Arvan S2 region. Must match the bucket's region. | [`kharej/s2_client.py`](../kharej/s2_client.py) |

### Optional — KHAREJ_* runtime settings

Any `KHAREJ_<KEY>` environment variable is imported into
[`KharejSettings`](../kharej/settings.py) with the prefix stripped and the key
lower-cased. Values can also be overridden at runtime via the
`admin.settings.update` Rubika control message (disk values win over env
defaults).

| Variable | Default | Description | Source |
|---|---|---|---|
| `KHAREJ_PROGRESS_THROTTLE_SECONDS` | `3` | Minimum seconds between progress events sent to Iran. | [`kharej/worker.py`](../kharej/worker.py) |
| `KHAREJ_DOWNLOAD_DIR` | `/tmp/kharej_downloads` | Base directory for in-progress download files. | [`kharej/downloaders/common.py`](../kharej/downloaders/common.py) |
| `KHAREJ_DEFAULT_AUDIO_QUALITY` | `bestaudio/best` | yt-dlp format selector for YouTube/SoundCloud/Bandcamp/Amazon. | [`kharej/downloaders/youtube.py`](../kharej/downloaders/youtube.py) |
| `KHAREJ_YTDLP_BIN` | `yt-dlp` | Path to (or name of) the `yt-dlp` executable. | [`kharej/downloaders/bandcamp.py`](../kharej/downloaders/bandcamp.py) |
| `KHAREJ_COOKIES_PATH` | _(unset)_ | Path to a Netscape-format `cookies.txt` for yt-dlp. Leave unset to run without cookies. | [`kharej/downloaders/youtube.py`](../kharej/downloaders/youtube.py) |
| `KHAREJ_DOWNLOAD_CONCURRENCY` | `2` | Maximum parallel per-track downloads inside a batch job. | [`kharej/downloaders/batch.py`](../kharej/downloaders/batch.py) |
| `KHAREJ_ENABLE_ZIP_SPLIT` | `false` | Split large ZIP archives into parts before uploading. | [`kharej/downloaders/batch.py`](../kharej/downloaders/batch.py) |
| `KHAREJ_ZIP_SPLIT_THRESHOLD_MB` | `200` | ZIP part size threshold (MB); only used when `KHAREJ_ENABLE_ZIP_SPLIT=true`. | [`kharej/downloaders/batch.py`](../kharej/downloaders/batch.py) |

### Optional — Logging and observability (shared with `rubetunes/`)

These variables are read by the shared `rubetunes/` library and affect Kharej
when it runs. They are also respected by the legacy `rub.py` / `main.py` bot.

| Variable | Default | Description | Source |
|---|---|---|---|
| `LOG_FORMAT` | `text` | Log output format: `text` (human-readable) or `json` (structured). | [`rubetunes/logging_setup.py`](../rubetunes/logging_setup.py) |
| `METRICS_PORT` | `9090` | Port for the Prometheus `/metrics` endpoint. Set to `0` to disable. | [`rubetunes/metrics.py`](../rubetunes/metrics.py) |
| `SENTRY_DSN` | _(empty)_ | Sentry DSN for error reporting. Leave blank to disable. | [`rubetunes/sentry_setup.py`](../rubetunes/sentry_setup.py) |

### Optional — Rate limiting and disk guard (shared with `rubetunes/`)

| Variable | Default | Description | Source |
|---|---|---|---|
| `USER_TRACKS_PER_HOUR` | `100` | Maximum tracks a user can request per rolling hour. | [`rubetunes/rate_limiter.py`](../rubetunes/rate_limiter.py) |
| `HEURISTIC_MB_PER_TRACK` | `30` | Estimated MB per FLAC track used to forecast disk usage. | [`rubetunes/disk_guard.py`](../rubetunes/disk_guard.py) |
| `MIN_FREE_SPACE_MULTIPLIER` | `2.0` | Batch is rejected when `free_mb < multiplier × estimated_mb`. | [`rubetunes/disk_guard.py`](../rubetunes/disk_guard.py) |
| `MIN_FREE_SPACE_MB` | `500` | Absolute minimum free disk space (MB) regardless of estimate. | [`rubetunes/disk_guard.py`](../rubetunes/disk_guard.py) |

### Optional — Provider credentials (shared with `rubetunes/`)

Leave any provider blank if you do not use it; the worker will skip that
source and fall back to the next available provider.

| Variable | Default | Description | Source |
|---|---|---|---|
| `TIDAL_TOKEN` | _(empty)_ | Tidal OAuth client token for metadata resolution and ISRC lookup. | [`rubetunes/providers/tidal.py`](../rubetunes/providers/tidal.py) |
| `TIDAL_ALT_BASES` | _(built-in list)_ | Comma-separated Tidal Alt proxy base URLs. Defaults to public proxies when blank. | [`rubetunes/providers/tidal_alt.py`](../rubetunes/providers/tidal_alt.py) |
| `MUSICDL_DOWNLOAD_DIR` | `<repo>/downloads/musicdl` | Directory where musicdl saves downloaded files. | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `MUSICDL_DEFAULT_SOURCES` | _(musicdl defaults)_ | Comma-separated list of musicdl source client names. | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `MUSICDL_PROXY` | _(none)_ | HTTP/HTTPS proxy URL for all musicdl requests (e.g. `http://user:pass@host:port`). | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `MUSICDL_MAX_RETRIES` | `1` | Per-request retry count for every musicdl source. | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `MUSICDL_CONNECT_TIMEOUT` | `5` | TCP connect timeout (seconds) for musicdl sources. | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `MUSICDL_READ_TIMEOUT` | `15` | Response-body read timeout (seconds) for musicdl sources. | [`rubetunes/providers/musicdl/config.py`](../rubetunes/providers/musicdl/config.py) |
| `SPOTIFY_CLIENT_ID` | _(empty)_ | Spotify OAuth app client ID (optional — anonymous token is obtained automatically). | [`rubetunes/spotify/session.py`](../rubetunes/spotify/session.py) |
| `SPOTIFY_CLIENT_SECRET` | _(empty)_ | Spotify OAuth app client secret. | [`rubetunes/spotify/session.py`](../rubetunes/spotify/session.py) |
| `DEEZER_ARL` | _(empty)_ | Deezer account ARL cookie for lossless FLAC downloads. | [`rubetunes/downloader.py`](../rubetunes/downloader.py) |
| `QOBUZ_EMAIL` | _(empty)_ | Qobuz account email (usually not required — credentials are auto-scraped). | [`rubetunes/downloader.py`](../rubetunes/downloader.py) |
| `QOBUZ_PASSWORD` | _(empty)_ | Qobuz account password. | [`rubetunes/downloader.py`](../rubetunes/downloader.py) |

### Optional — Circuit breaker (shared with `rubetunes/`)

| Variable | Default | Description | Source |
|---|---|---|---|
| `CIRCUIT_FAIL_THRESHOLD` | `3` | Consecutive failures before a provider's circuit is opened. | [`rubetunes/circuit_breaker.py`](../rubetunes/circuit_breaker.py) |
| `CIRCUIT_FAIL_WINDOW_SEC` | `300` | Rolling window (seconds) in which `CIRCUIT_FAIL_THRESHOLD` must be reached. | [`rubetunes/circuit_breaker.py`](../rubetunes/circuit_breaker.py) |
| `CIRCUIT_OPEN_DURATION_SEC` | `600` | Seconds a tripped circuit stays open before allowing retries. | [`rubetunes/circuit_breaker.py`](../rubetunes/circuit_breaker.py) |

---

## Running tests

```bash
# Iran tests
pytest iran/tests -v

# Kharej tests
pytest kharej/tests -v

# Shared rubetunes tests
pytest tests/ -v

# All tests
pytest -v
```

---

## Troubleshooting

### `Missing required environment variables: RUBIKA_SESSION_KHAREJ`

The Kharej worker requires `RUBIKA_SESSION_KHAREJ` and
`IRAN_RUBIKA_ACCOUNT_GUID` at minimum. Run:

```bash
python -m kharej.worker --check-config
```

for a redacted summary of what was found and what is missing.

### Kharej connects but Iran never receives progress events

1. Verify that `IRAN_RUBIKA_ACCOUNT_GUID` on the Kharej side equals the Iran
   account's GUID — Kharej silently rejects messages from any other sender.
2. Verify that Iran's `IRAN_KHAREJ_RUBIKA_ACCOUNT_GUID` equals the Kharej
   account's GUID.
3. Check that both Rubika sessions are logged in (run rubpy's session helper on
   each machine/account).

### S2 upload failures (`S2AccessDenied`)

Ensure `ARVAN_S2_ACCESS_KEY_WRITE` and `ARVAN_S2_SECRET_WRITE` are the
**write** credentials, not the read-only pair used by Iran.

---

## See also

- [`iran/.env.example`](../iran/.env.example) — Iran environment variables
- [`.env.kharej.example`](../.env.kharej.example) — Kharej environment variables (full annotated template)
- [`docs/research/arvan-webui-migration/architecture.md`](research/arvan-webui-migration/architecture.md) — system architecture
- [`docs/research/arvan-webui-migration/CONTRACTS.md`](research/arvan-webui-migration/CONTRACTS.md) — Iran ↔ Kharej message contracts
- [`kharej/README.md`](../kharej/README.md) — Kharej package overview
- [`iran/README.md`](../iran/README.md) — Iran package overview
