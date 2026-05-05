# Task Split — Two-Developer Parallel Work Plan

> **Status:** Frozen at `v=1`. Canonical implementation: [`kharej/contracts.py`](../../../kharej/contracts.py). See [`CONTRACTS.md`](CONTRACTS.md).

> See also: [architecture.md](architecture.md) · [message-schema.md](message-schema.md) · [webui-spec.md](webui-spec.md)

Both developers **must agree on the contracts in section 3 (Shared Contracts) before writing any code**.  These contracts are the coupling surface between the two tracks.

---

## Track A — "Kharej Worker + Storage + Control Bus"

**Owner**: Backend / Infrastructure developer  
**VPS**: Kharej VPS  
**Primary languages**: Python (existing), with `boto3` for S2

### Scope

Refactor the existing download workers into a clean, message-driven process that:
1. Receives `job.create` control messages via Rubika.
2. Executes all existing download logic (yt-dlp, spotify, tidal, qobuz, amazon, soundcloud, bandcamp, musicdl, zip_split, tagging).
3. Uploads results to Arvan S2.
4. Publishes progress and completion events back over Rubika.
5. Handles admin control messages (`user.whitelist.*`, `user.block.*`, `admin.clearcache`, `admin.settings.update`, `health.ping`).

### Files / Modules Owned by Track A

| File / Module | Action |
|---------------|--------|
| `kharej/worker.py` | NEW — main async worker loop |
| `kharej/rubika_client.py` | NEW — Rubika control client (Kharej side) |
| `kharej/s2_client.py` | NEW — boto3 S2 abstraction (upload, delete, presign-write) |
| `kharej/dispatcher.py` | NEW — routes job.create to the correct downloader |
| `kharej/access_control.py` | NEW — local whitelist/ban state, updated via control messages |
| `kharej/progress_reporter.py` | NEW — throttled job.progress publisher |
| `kharej/settings.py` | NEW — local settings, updated via admin.settings.update |
| `kharej/docker-compose.yml` | NEW — Kharej VPS compose file |
| `kharej/Dockerfile` | NEW — Kharej worker Docker image |
| `kharej/.env.example` | NEW — Kharej-specific env vars |
| Existing `rubetunes/` package | **READ ONLY** — import without modification |
| Existing `zip_split.py` | **READ ONLY** — import without modification |

### Subtask Checklist

**Phase 1: Core Worker Skeleton**
- [ ] **A1** Define the `kharej/` directory layout and Python package structure **(S)**
- [ ] **A2** Implement `kharej/rubika_client.py`: connect to Rubika, listen for messages from the Iran-side account, parse `RTUNES::` prefix, dispatch to `worker.py` **(M)**
- [ ] **A3** Implement `kharej/s2_client.py`: boto3 client wrapper, `upload_file(path, job_id, filename)`, `delete_object(key)`, `generate_presigned_upload_url()`, retry logic **(M)**
- [ ] **A4** Implement `kharej/access_control.py`: in-memory whitelist/ban state loaded from `access_state.json`; `check_access(user_id)` method; handlers for `user.whitelist.*` and `user.block.*` messages **(S)**
- [ ] **A5** Implement `kharej/dispatcher.py`: routes `job.create` message to the correct downloader module by `platform` field **(S)**
- [ ] **A6** Implement `kharej/progress_reporter.py`: throttled (every 3 s) publisher of `job.progress` messages; final `job.completed` / `job.failed` publisher **(S)**

**Phase 2: Downloader Integration**
- [ ] **A7** Integrate YouTube yt-dlp downloader: adapt `_do_download` logic into a standalone async function; call `s2_client.upload_file` after download; emit progress events **(M)**
- [ ] **A8** Integrate Spotify single-track flow: adapt `_do_music_download` (Spotify branch); call S2 upload **(M)**
- [ ] **A9** Integrate batch download (playlist/album): adapt `_do_batch_download`; run `zip_split_from_files`; upload each part; emit per-part progress **(L)**
- [ ] **A10** Integrate Tidal, Qobuz, Amazon, SoundCloud, Bandcamp handlers **(M)**
- [ ] **A11** Integrate musicdl handler **(S)**

**Phase 3: Admin Control Messages**
- [ ] **A12** Handle `admin.clearcache` message: call `_spodl.clear_track_info_cache()` and truncate ISRC disk cache **(S)**
- [ ] **A13** Handle `admin.settings.update` message: update local `settings.py` state, persist to `kharej_settings.json` **(S)**
- [ ] **A14** Handle `health.ping` message: probe all provider endpoints, publish `health.pong` JSON **(S)**
- [ ] **A15** Handle `admin.cookies.update` message: download the new `cookies.txt` from the S2 `tmp/` path included in the message, replace local file **(S)**

**Phase 4: Deployment**
- [ ] **A16** Write `kharej/Dockerfile` (based on existing `Dockerfile` but entrypoint is `python -m kharej.worker`) **(S)**
- [ ] **A17** Write `kharej/docker-compose.yml` with volumes for `downloads`, `state`, `logs`; expose port 9091 for Prometheus **(S)**
- [ ] **A18** Write `kharej/.env.example` (Kharej-specific: Rubika session, S2 write credentials, admin GUIDs, provider keys) **(S)**

**Phase 5: Tests**
- [ ] **A19** Unit tests for `s2_client.py` (mock boto3) **(M)**
- [ ] **A20** Unit tests for `access_control.py` **(S)**
- [ ] **A21** Integration test for the worker: mock Rubika client + mock S2 + real downloader modules (with `pytest-asyncio`) **(L)**

### Effort Summary (Track A)

| Subtask | Effort |
|---------|--------|
| A1–A6 (skeleton) | ~5 days |
| A7–A11 (downloaders) | ~8 days |
| A12–A15 (admin messages) | ~2 days |
| A16–A18 (deployment) | ~1 day |
| A19–A21 (tests) | ~4 days |
| **Total** | **~20 developer-days** |

---

## Track B — "Iran VPS Web UI + Admin Panel + Auth"

**Owner**: Full-stack developer  
**VPS**: Iran VPS  
**Primary languages**: Python (FastAPI backend) + TypeScript (React frontend)

### Scope

Build the Iran-side Web UI and Admin Panel:
1. FastAPI backend with JWT auth, job management API, SSE/WebSocket event bridge.
2. Iran-side Rubika client that publishes job requests and consumes progress/completion events.
3. React + TypeScript SPA: public pages, admin panel, real-time progress.
4. PostgreSQL DB schema and Alembic migrations.
5. S2 read client (presigned URLs or proxy).
6. Docker-compose for Iran VPS.

### Files / Modules Owned by Track B

| File / Module | Action |
|---------------|--------|
| `iran/api/` | NEW — FastAPI application |
| `iran/api/auth.py` | NEW — registration, login, JWT |
| `iran/api/jobs.py` | NEW — POST /jobs, GET /jobs/{id}, SSE |
| `iran/api/downloads.py` | NEW — presigned URL / proxy endpoint |
| `iran/api/admin.py` | NEW — admin CRUD endpoints |
| `iran/api/settings.py` | NEW — settings read/write |
| `iran/rubika_client.py` | NEW — Iran-side Rubika client (publishes job.create, subscribes events) |
| `iran/s2_read_client.py` | NEW — boto3 read-only S2 client + presign |
| `iran/db/models.py` | NEW — SQLAlchemy models (users, jobs, audit, settings, refresh_tokens) |
| `iran/db/migrations/` | NEW — Alembic migrations |
| `iran/web/` (React app) | NEW — entire frontend |
| `iran/docker-compose.yml` | NEW — Iran VPS compose file |
| `iran/Dockerfile` | NEW — Iran VPS Docker image |
| `iran/.env.example` | NEW — Iran VPS env vars |

### Subtask Checklist

**Phase 1: Backend Foundation**
- [ ] **B1** Set up FastAPI project structure: `iran/api/`, dependency injection, CORS for React dev server **(S)**
- [ ] **B2** DB models: `users`, `jobs`, `job_parts`, `audit_log`, `settings`, `refresh_tokens`, `registrations` **(M)**
- [ ] **B3** Alembic migrations: initial schema migration **(S)**
- [ ] **B4** Auth: registration (pending), login, JWT issue/refresh/revoke, password hashing (bcrypt) **(M)**
- [ ] **B5** Iran-side Rubika client: connect, listen for messages from Kharej account, parse and emit events to in-process SSE bus **(M)**
- [ ] **B6** S2 read client: `generate_presigned_url(job_id, filename)`, `head_object(key)`, `get_object_stream(key)` **(S)**

**Phase 2: Core API Endpoints**
- [ ] **B7** `POST /jobs` — validate URL, check rate limit, create job in DB, publish `job.create` to Rubika **(M)**
- [ ] **B8** `GET /jobs/{id}/events` — SSE endpoint; subscribe to in-process event bus for this job_id **(M)**
- [ ] **B9** `GET /jobs/{id}/download` — validate job ownership, generate presigned URL or proxy stream **(M)**
- [ ] **B10** `GET /library` — paginated user download history **(S)**

**Phase 3: Admin API**
- [ ] **B13** `GET|PATCH /admin/users` — list and update user status; on approve/block send control message to Kharej via Rubika **(M)**
- [ ] **B14** `GET /admin/jobs` — all jobs with pagination **(S)**
- [ ] **B15** `GET /admin/storage` — query S2 storage usage via `ListObjectsV2` **(M)**
- [ ] **B16** `GET|PATCH /admin/settings` — read/write settings table; on update send `admin.settings.update` control message **(M)**
- [ ] **B17** `GET /admin/audit` — paginated audit log with filters **(S)**
- [ ] **B18** `GET /admin/health` + `POST /admin/health/ping` — read cached health state or trigger `health.ping` **(S)**

**Phase 4: Frontend — Public Pages**
- [ ] **B19** Set up React + Vite + TypeScript project; Tailwind CSS; shadcn/ui; RTL layout (Vazirmatn font, `dir="rtl"`) **(M)**
- [ ] **B20** Auth pages: Login, Register, pending-approval page **(M)**
- [ ] **B21** URL paste page with platform auto-detection and quality picker **(M)**
- [ ] **B22** Job progress page with real-time SSE progress bar **(M)**
- [ ] **B23** Completed download page: download buttons, SHA-256 checksum, expiry indicator **(M)**
- [ ] **B24** Library / My Downloads page **(M)**
- [ ] **B25** Streaming audio player **(M)**
- [ ] **B26** Account settings page **(S)**

**Phase 5: Frontend — Admin Panel**
- [ ] **B27** Admin dashboard (summary cards, recent activity) **(M)**
- [ ] **B28** Users list with approve/block actions **(M)**
- [ ] **B29** Pending registrations queue **(S)**
- [ ] **B30** Active jobs / queue monitor with live updates **(M)**
- [ ] **B31** S2 storage usage dashboard **(M)**
- [ ] **B32** Settings editor with form validation **(M)**
- [ ] **B33** Audit log with filters and CSV export **(M)**
- [ ] **B34** Provider health dashboard **(S)**

**Phase 6: Deployment**
- [ ] **B35** `iran/Dockerfile` (Python + Node build stage for React; nginx serving static files + reverse proxy to FastAPI) **(M)**
- [ ] **B36** `iran/docker-compose.yml` (services: api, web, db, redis, nginx) **(S)**
- [ ] **B37** `iran/.env.example` **(S)**

**Phase 7: Tests**
- [ ] **B38** Unit tests for auth (registration, login, JWT) **(M)**
- [ ] **B39** API tests for job lifecycle (create → progress → completed → download) with mocked Rubika client and S2 client **(L)**
- [ ] **B40** Admin API tests (user approve/block, settings update) **(M)**

### Effort Summary (Track B)

| Subtask | Effort |
|---------|--------|
| B1–B6 (foundation) | ~6 days |
| B7–B12 (core API) | ~8 days |
| B13–B18 (admin API) | ~5 days |
| B19–B26 (public frontend) | ~10 days |
| B27–B34 (admin frontend) | ~8 days |
| B35–B37 (deployment) | ~2 days |
| B38–B40 (tests) | ~5 days |
| **Total** | **~44 developer-days** |

---

## 3. Shared Contracts (Must Agree Before Coding Starts)

Both developers must sign off on these contracts.  Changes to these contracts require a joint review.

### 3.1 Rubika Message Schema

**Owner**: Neither — defined jointly in [`message-schema.md`](message-schema.md) before coding begins.

**Rules**:
- All messages are UTF-8 JSON strings prefixed with `RTUNES::`.
- All messages have `"v": 1`, `"type": "<message-type>"`, `"job_id"` (where applicable), `"ts"` (ISO-8601 UTC timestamp).
- No binary payloads. Maximum message size: 4 KB.
- Breaking changes require a version bump (`"v": 2`) and a migration period where both versions are accepted.

### 3.2 Arvan S2 Object Key Convention

```
media/{job_id}/{safe_filename}[.ext]
media/{job_id}/{safe_filename}-part{N}.zip
thumbs/{isrc_or_job_id}.jpg
tmp/{job_id}/             (multipart upload staging)
```

- `job_id`: UUID v4 (generated by Track B on `POST /jobs`; included in `job.create` message).
- `safe_filename`: ASCII-only, no spaces, max 200 chars. Generated by Kharej Worker (`_safe_filename()` from existing `spotify_dl.py`).
- The Iran VPS (Track B) generates the `job_id` and passes it in `job.create`.  This lets the Iran VPS know the S2 key prefix before the upload completes.

### 3.3 Job Lifecycle State Machine

```
pending → accepted → running → completed
                   → failed
              → cancelled (by user or admin)
```

Track A owns the `accepted → running → completed / failed` transitions (via Rubika messages).  
Track B owns the `pending` creation and `cancelled` transition (via API + Rubika `job.cancel` message).

### 3.4 DB Schema for Shared Tables

Track B owns the DB entirely, but the `jobs` and `users` table structures are the shared contract.

**`users` table (minimum required columns)**

```sql
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'admin'
    status      TEXT NOT NULL DEFAULT 'pending_approval', -- 'pending_approval' | 'active' | 'blocked' | 'deleted'
    rubika_guid TEXT,                           -- optional: user's Rubika GUID (for cross-referencing)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ
);
```

**`jobs` table (minimum required columns)**

```sql
CREATE TABLE jobs (
    id          UUID PRIMARY KEY,              -- generated by Iran VPS before job.create
    user_id     UUID NOT NULL REFERENCES users(id),
    platform    TEXT NOT NULL,                 -- 'youtube' | 'spotify' | 'tidal' | 'qobuz' | 'amazon' | 'soundcloud' | 'bandcamp' | 'musicdl'
    url         TEXT NOT NULL,
    quality     TEXT,                          -- 'mp3' | 'flac' | 'hires' | '1080p' | etc.
    job_type    TEXT NOT NULL DEFAULT 'single', -- 'single' | 'batch'
    status      TEXT NOT NULL DEFAULT 'pending', -- lifecycle states above
    progress    INT DEFAULT 0,                 -- 0–100
    speed       TEXT,                          -- e.g. '3.2 MB/s'
    error_code  TEXT,
    error_msg   TEXT,
    s2_keys     JSONB,                         -- array of { key, size, mime, sha256 }
    total_tracks INT,
    done_tracks  INT DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

### 3.5 `job.create` Message — Fields Track B Must Populate

Track B generates `job_id` (UUID v4) and sends:

```json
{
  "v": 1,
  "type": "job.create",
  "job_id": "<uuid>",
  "user_id": "<uuid>",
  "user_status": "active",
  "platform": "spotify",
  "url": "https://open.spotify.com/track/...",
  "quality": "flac",
  "job_type": "single",
  "ts": "2026-04-26T17:05:56Z"
}
```

For batch jobs, `track_ids` is always `null` — the Iran VPS sends only the playlist/album URL. The Kharej Worker fetches all track IDs from the platform directly.

### 3.6 Environment Variables — Shared Understanding

| Variable | Owner | VPS |
|----------|-------|-----|
| `RUBIKA_SESSION_KHAREJ` | Track A | Kharej |
| `RUBIKA_SESSION_IRAN` | Track B | Iran |
| `IRAN_RUBIKA_ACCOUNT_GUID` | Both (Track B configures, Track A validates) | Kharej (config) |
| `KHAREJ_RUBIKA_ACCOUNT_GUID` | Both (Track A configures, Track B sends to) | Iran (config) |
| `ARVAN_S2_ACCESS_KEY_WRITE` + `SECRET` | Track A | Kharej only |
| `ARVAN_S2_ACCESS_KEY_READ` + `SECRET` | Track B | Iran only |
| `ARVAN_S2_ENDPOINT` | Both | Both |
| `ARVAN_S2_BUCKET` | Both | Both |
| `DATABASE_URL` | Track B | Iran only |
| `SECRET_KEY` | Track B | Iran only |

---

## 4. Coordination Process

1. **Before day 1**: Both developers review and sign off on section 3 (Shared Contracts). File `message-schema.md` is frozen until both agree to a version bump.
2. **Pull Requests**: Each developer works on a feature branch. PRs require a review from the other developer when touching shared contracts (message schema, DB schema, S2 key conventions).
3. **Weekly sync**: 30-minute call to review progress, unblock dependencies, and update the schema version if needed.
4. **Integration testing**: At the end of each phase, a combined integration test is run: Track B posts a job request; Track A worker processes it and uploads to S2; Track B retrieves the presigned URL.
5. **Schema versioning**: Any change to `message-schema.md` increments the `"v"` field and is announced to both developers. Both clients must handle both `v=1` and `v=2` during the transition period.
