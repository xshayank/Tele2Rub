# Current RubeTunes Feature Inventory

> This document enumerates every feature found in the current RubeTunes codebase.  
> Each feature lists: description · where it lives · how it maps to the new architecture.
>
> Source files read: `rub.py`, `main.py`, `spotify_dl.py`, `zip_split.py`,
> `rubetunes/`, `.env.example`, `README.md`, `CHANGELOG.md`.

---

## 1. Download Commands

### 1.1 YouTube / yt-dlp Video & Audio Download

**Description**: Users send `!download <youtube-url>` and receive a quality-selection menu (4K, 1080p, 720p, … audio-only MP3, subtitles). After selection, yt-dlp downloads the file and the bot sends it.

**Where in code**:
- Command handler: [`rub.py` — `download_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L752)
- Quality menu builder: [`rub.py` — `build_quality_menu`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L385)
- yt-dlp invocation: [`rub.py` — `_do_download`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L1791)
- yt-dlp command builder: [`rub.py` — `build_ytdlp_cmd_for_choice`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L502)

**Supported qualities**: 4K (2160p), 1440p, 1080p, 720p, 480p, 360p, 240p, Audio-only MP3, Subtitles (SRT).

**Size limit**: Options exceeding 2 GB are hidden from the menu ([`rub.py:215`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L215)).

**New architecture mapping**:
- **Kharej Worker (Track A)**: Receives `job.create` message, runs yt-dlp, uploads result to Arvan S2, publishes `job.completed` with S2 object key.
- **Iran Web UI (Track B)**: Shows quality selection UI; on user pick sends `job.create` via Rubika control bus; listens for `job.completed` event and provides presigned URL to browser.

---

### 1.2 Spotify Track Download

**Description**: `!spotify <url>` downloads a Spotify track. The bot fetches track metadata (title, artists, ISRC) then runs a waterfall resolution chain: Qobuz Hi-Res → Qobuz CD → Deezer FLAC → YouTube Music MP3.

**Where in code**:
- Command handler: [`rub.py` — `spotify_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2264)
- Metadata fetch: [`rubetunes/spotify_meta.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/spotify_meta.py)
- Waterfall resolution: [`rubetunes/resolver.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/resolver.py)
- Download orchestration: [`rubetunes/downloader.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/downloader.py)

**Quality format hint**: Users can append `mp3`, `flac`, `m4a`, or `hires` to the command (e.g., `!spotify <url> flac`). Handled by [`rubetunes/resolver.py` — `_parse_format_hint`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/resolver.py).

**New architecture mapping**: Kharej Worker handles resolution chain and S2 upload. Iran Web UI presents quality picker and delivers file via S2 presigned URL.

---

### 1.3 Spotify Playlist & Album Download (Batch)

**Description**: `!spotify <playlist-url>` or `!spotify <album-url>` downloads all tracks, then ZIP-archives them (splitting large archives into parts). Up to `BATCH_CONCURRENCY` (default 3) tracks are downloaded in parallel.

**Where in code**:
- Batch handler: [`rub.py` — `_do_batch_download`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L1270)
- ZIP splitting: [`zip_split.py` — `split_zip_from_files`](https://github.com/xshayank/RubeTunes/blob/main/zip_split.py#L20)
- Concurrency setting: [`rub.py:232`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L232) — `BATCH_CONCURRENCY = max(1, min(6, int(os.getenv("BATCH_CONCURRENCY", "3"))))`

**ZIP part size**: Default 1.95 GiB; configurable via `ZIP_PART_SIZE_BYTES` or `ZIP_PART_SIZE_MB` env vars ([`rub.py:221–228`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L221)).

**New architecture mapping**: Kharej Worker downloads tracks in parallel, zips and splits, uploads each zip part to Arvan S2. Iran Web UI displays progress in real-time (via SSE/WebSocket) and shows a multi-part download list.

---

### 1.4 Spotify Artist Browse

**Description**: `!spotify <artist-url>` shows the artist's top 5 tracks and a paginated discography (albums/singles, 10 per page). Users can select any item to download.

**Where in code**:
- Spotify artist handler: [`rub.py`](https://github.com/xshayank/RubeTunes/blob/main/rub.py) (artist regex `SPOTIFY_ARTIST_RE`)
- Pagination constant: [`rub.py:235`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L235) — `ARTIST_ALBUMS_PAGE_SIZE = 10`

**New architecture mapping**: Iran Web UI has a dedicated artist detail page with top tracks and paginated discography.

---

### 1.5 Tidal Track Download

**Description**: `!tidal <url>` downloads a Tidal track via ISRC-based resolution (same waterfall as Spotify).

**Where in code**:
- Command handler: [`rub.py` — `tidal_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2264)
- Tidal metadata: [`rubetunes/providers/tidal.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/tidal.py), [`rubetunes/providers/tidal_alt.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/tidal_alt.py)
- Tidal Alt proxy rotation: env var `TIDAL_ALT_BASES` (default 4 mirrors)

**New architecture mapping**: Kharej Worker resolves ISRC and downloads; uploads to S2.

---

### 1.6 Qobuz Track Download

**Description**: `!qobuz <url>` downloads from Qobuz (Hi-Res FLAC or CD FLAC). No account needed — credentials auto-scraped from `open.qobuz.com`. Optional authenticated fallback via `QOBUZ_EMAIL`/`QOBUZ_PASSWORD`.

**Where in code**:
- Command handler: [`rub.py` — `qobuz_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2264)
- Provider: [`rubetunes/providers/qobuz.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/qobuz.py)

**New architecture mapping**: Kharej Worker; credentials stored securely on Kharej VPS.

---

### 1.7 Amazon Music Track Download

**Description**: `!amazon <url>` downloads Amazon Music tracks via proxy ISRC resolution.

**Where in code**:
- Command handler: [`rub.py` — `amazon_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2264)
- Provider: [`rubetunes/providers/amazon.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/amazon.py)

**New architecture mapping**: Kharej Worker.

---

### 1.8 SoundCloud Track Download

**Description**: `!soundcloud <url>` downloads a SoundCloud track via yt-dlp.

**Where in code**:
- Command handler: [`rub.py` — `soundcloud_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2583)
- Provider: [`rubetunes/providers/soundcloud.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/soundcloud.py)

**New architecture mapping**: Kharej Worker.

---

### 1.9 Bandcamp Track / Album Download

**Description**: `!bandcamp <url>` downloads a Bandcamp track or album via yt-dlp (FLAC preferred).

**Where in code**:
- Command handler: [`rub.py` — `bandcamp_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2583)
- Provider: [`rubetunes/providers/bandcamp.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/bandcamp.py)

**New architecture mapping**: Kharej Worker.

---

### 1.10 musicdl Multi-Source Download (40+ Platforms)

**Description**: `!musicdl search <query>` / `!musicdl <number>` lets users search and download from 40+ platforms (Netease, QQ, Kuwo, Kugou, Migu, Bilibili, Deezer, Qobuz, Tidal, Spotify, Apple Music, YouTube Music, JOOX, SoundCloud, Jamendo, …) via the `musicdl` library.

**Where in code**:
- Provider: [`rubetunes/providers/musicdl/`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/musicdl/)
- Commands: `!musicdl sources`, `!musicdl search <query> [source]`, `!musicdl <N>`
- Config: `MUSICDL_DOWNLOAD_DIR`, `MUSICDL_DEFAULT_SOURCES`, `MUSICDL_PROXY`, `MUSICDL_MAX_RETRIES`, `MUSICDL_CONNECT_TIMEOUT`, `MUSICDL_READ_TIMEOUT`

**New architecture mapping**: Kharej Worker; results uploaded to S2.

---

## 2. Search

### 2.1 Spotify Search

**Description**: `!search <query>` searches Spotify (top 10 results) via the internal GraphQL API. Results are shown as a numbered menu; user replies `!1`–`!10` to select.

**Where in code**:
- Command handler: [`rub.py` — `search_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2609)
- API call: `_spodl.spotify_search(query, 10)` → [`rubetunes/spotify_meta.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/spotify_meta.py)

**New architecture mapping**: Iran Web UI provides a search bar; metadata lookup can happen directly on the Iran VPS (lightweight metadata-only calls to Spotify GraphQL). Results displayed in UI; user clicks to request download.

---

## 3. Queue System

### 3.1 Download Queue

**Description**: Only one download runs at a time. Additional requests are queued. Each user sees their position. Queue persists across restarts via `queue_snapshot.json`.

**Where in code**:
- Queue: [`rub.py:243–244`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L243) — `download_queue: collections.deque`
- Queue runner: [`rub.py` — `_run_download_and_queue`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L556)
- Queue status: [`rub.py` — `queue_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2687)
- Queue snapshot save/restore: [`rub.py:101–142`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L101)
- SIGTERM handler saves snapshot: [`rub.py:148–158`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L148)

**New architecture mapping**:
- Kharej Worker has its own internal queue (in-process or Redis-backed).
- Iran Web UI shows the user's active job status in real-time via SSE/WebSocket.
- Admin Panel shows the global queue across all users.

---

### 3.2 Pending Quality Selection State

**Description**: When a quality/platform menu is sent, the bot remembers the pending selection per-user. A 5-minute timeout cancels the selection automatically.

**Where in code**:
- State dict: [`rub.py:239`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L239) — `pending_selections: dict`
- Timeout: [`rub.py:213`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L213) — `SELECTION_TIMEOUT = 300.0`
- Expiry handler: [`rub.py` — `_expire_selection`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L583)

**New architecture mapping**: Iran Web UI handles quality selection in the browser (modal/dropdown); no separate pending-state dict needed. Server-side session holds the job context.

---

## 4. User Access Control

### 4.1 Whitelist Mode

**Description**: When enabled, only users in the whitelist can use the bot. Toggle with `!admin whitelist on/off`. Add/remove with `!admin whitelist add|remove <guid>`.

**Where in code**:
- State: [`rub.py:257–310`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L257) — `state.json` with `whitelist_enabled`, `whitelist` list
- Access check: [`rub.py` — `_check_access`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L288)
- Admin handler: [`rub.py` — `admin_handler`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L1911)

**New architecture mapping**: Iran Web UI Admin Panel manages the user registry. Registration → Admin approval flow replaces the `whitelist` list. The Kharej Worker still validates user identity on `job.create` messages via a `user.whitelist` control message.

---

### 4.2 Ban / Unban

**Description**: Admins can permanently ban users with `!admin ban <guid>`. Banned users see "You have been banned" on any command.

**Where in code**:
- State: `state.json` `banned` list
- Handler: [`rub.py:2023–2055`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2023)

**New architecture mapping**: Admin Panel "Block user" action; DB `users.status = 'blocked'`. Propagated to Kharej via `user.block.add` control message.

---

### 4.3 Admin Identification

**Description**: Admin privileges are gated on `ADMIN_GUIDS` env var (comma-separated Rubika object GUIDs).

**Where in code**: [`rub.py:87–88`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L87), [`rub.py` — `_is_admin`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L284)

**New architecture mapping**: Admin role stored in DB (`users.role = 'admin'`). Admin Panel access gated on role. `ADMIN_GUIDS` still used for Rubika bot admin escalation path on Kharej side.

---

## 5. Usage Logging

### 5.1 Usage Log

**Description**: Every user action (download_requested, search, etc.) is appended to `state.json#logs`. Admins view with `!admin logs [N]`. Capped at 500 entries.

**Where in code**:
- Log append: [`rub.py` — `_append_log`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L299)
- Max entries: [`rub.py:216`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L216) — `MAX_LOG_ENTRIES = 500`

**New architecture mapping**: DB audit log table (user, action, detail, timestamp). Admin Panel shows sortable/filterable audit log.

---

### 5.2 Download History

**Description**: Successful downloads are recorded in `downloads_history.json` (track_id|source|quality → file path, user, timestamp, title, artists). Duplicate requests re-use cached files.

**Where in code**:
- History module: [`rubetunes/history.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/history.py)
- Check + record: `_check_download_history`, `_record_download_history`

**New architecture mapping**: Kharej Worker checks history before re-downloading. Iran Web UI shows per-user download history (`!history` equivalent).

---

### 5.3 `!history [N]` Command

**Description**: Shows the user's last N downloads (default 10).

**Where in code**: [`rub.py`](https://github.com/xshayank/RubeTunes/blob/main/rub.py) — `history_handler` (implements `!history`).

**New architecture mapping**: Iran Web UI "My Downloads" / "Library" page.

---

## 6. Upload Retry Queue

**Description**: If uploading a file to Rubika fails, the upload is enqueued in a persistent retry queue (`upload_retry_queue.json`). A background task retries every hour. Users can check status with `!uploads` and cancel with `!uploads cancel <id>`.

**Where in code**:
- Module: [`rubetunes/upload_retry.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/upload_retry.py)
- Enqueue on failure: [`rub.py:1877–1891`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L1877)

**New architecture mapping**: With S2, uploads go directly from Kharej Worker to S2 (not through Rubika). S2 multipart upload with retry replaces this mechanism. The retry queue concept moves to the S2 upload client (`boto3` retry config + explicit re-try loop in Track A).

---

## 7. YouTube Channel Subscription Notifier

**Description**: Subscribe to YouTube channels; the bot polls for new uploads and sends notifications.

**Where in code**:
- Module: [`rubetunes/yt_notify.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/yt_notify.py)
- State: `ytsub_state.json`
- Commands: `subscribe_sync`, `unsubscribe_sync`, `list_subscriptions_sync`

**New architecture mapping**: Kharej Worker runs the poller. Notifications sent to Iran VPS via Rubika control channel (`job.notify` message type) and displayed in the Web UI notification centre.

---

## 8. Provider Infrastructure

### 8.1 Circuit Breaker

**Description**: After `CIRCUIT_FAIL_THRESHOLD` consecutive failures within `CIRCUIT_FAIL_WINDOW_SEC` seconds, a provider circuit opens for `CIRCUIT_OPEN_DURATION_SEC` seconds. Failing providers are automatically skipped.

**Where in code**: [`rubetunes/circuit_breaker.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/circuit_breaker.py)

**Admin visibility**: `!admin breakers` shows per-provider state.

**New architecture mapping**: Kharej Worker; Admin Panel exposes circuit-breaker states via API.

---

### 8.2 Metadata Caching

**Description**: Two-layer cache: in-process LRU (`cache.py`) for recently fetched track info; ISRC disk cache (`isrc_cache.json`) mapping ISRC → stream URL.

**Where in code**: [`rubetunes/cache.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/cache.py)

**Admin control**: `!admin clearcache [lru|isrc|all]`

**New architecture mapping**: Kharej Worker retains both caches. Admin Panel exposes a "Clear Cache" button that sends `admin.clearcache` control message to Kharej.

---

### 8.3 Multi-Platform ISRC Resolution

**Description**: Given a Spotify/Tidal/Qobuz/Amazon track, the resolver queries Odesli, Songstats, and MusicBrainz to find the ISRC and cross-platform streaming links. The ISRC is used to download from any available provider.

**Where in code**: [`rubetunes/resolver.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/resolver.py)

**New architecture mapping**: Kharej Worker.

---

### 8.4 Apple Music Metadata Enrichment

**Description**: Enriches track metadata with high-resolution cover art (1400×1400) from the iTunes Search API and track/disc numbers.

**Where in code**: [`rubetunes/providers/apple_music.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/providers/apple_music.py)

**New architecture mapping**: Kharej Worker (enriches metadata before tagging); thumbnails uploaded to S2 `thumbs/` prefix.

---

### 8.5 Audio Metadata Tagging

**Description**: After download, embeds ID3/FLAC/M4A tags: title, artist, album, date, track number, cover art, genre.

**Where in code**: [`rubetunes/tagging.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/tagging.py)

**New architecture mapping**: Kharej Worker (before S2 upload).

---

## 9. Rate Limiting & Disk Guard

### 9.1 Per-User Rate Limiting

**Description**: Non-admin users are limited to `USER_TRACKS_PER_HOUR` (default 100) track requests per rolling hour. Rejected requests include a cooldown ETA message.

**Where in code**: [`rubetunes/rate_limiter.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/rate_limiter.py)

**New architecture mapping**: Iran Web UI API layer (enforced in the HTTP request handler before a `job.create` is sent). DB-backed per-user counter.

---

### 9.2 Disk Space Guard

**Description**: Before starting a batch download, checks that free disk space is at least `MIN_FREE_SPACE_MULTIPLIER` × estimated download size. Rejects if insufficient.

**Where in code**: [`rubetunes/disk_guard.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/disk_guard.py)

**New architecture mapping**: Kharej Worker (checks disk before starting batch). S2 storage has no such constraint on the Iran side.

---

## 10. Observability

### 10.1 Prometheus Metrics

**Description**: `/metrics` HTTP endpoint on port 9090 exposes counters (`rubetunes_downloads_total`, `rubetunes_provider_failures_total`, `rubetunes_resolutions_total`), gauges (`rubetunes_queue_depth`, `rubetunes_circuit_open`), and histograms for download duration.

**Where in code**: [`rubetunes/metrics.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/metrics.py), [`main.py:57–62`](https://github.com/xshayank/RubeTunes/blob/main/main.py#L57)

**New architecture mapping**: Kharej Worker exposes a `/metrics` endpoint. Iran VPS Web API also exposes its own metrics. Admin Panel shows a stats dashboard (query Prometheus or expose via API).

---

### 10.2 Structured JSON Logging

**Description**: Toggle between human-readable text and structured JSON logs via `LOG_FORMAT=json`.

**Where in code**: [`rubetunes/logging_setup.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/logging_setup.py)

**New architecture mapping**: Both VPSes retain structured logging.

---

### 10.3 Sentry Error Reporting

**Description**: Unhandled exceptions are sent to Sentry when `SENTRY_DSN` is configured. Each event is tagged with the user GUID and command.

**Where in code**: [`rubetunes/sentry_setup.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/sentry_setup.py), [`main.py:47–52`](https://github.com/xshayank/RubeTunes/blob/main/main.py#L47)

**New architecture mapping**: Both VPSes retain Sentry integration.

---

### 10.4 Provider Health Check

**Description**: `!admin health` pings Qobuz, Deezer, Tidal, lrclib, MusicBrainz, Odesli, YouTube Music in parallel, reports up/down/slow (>2 s response).

**Where in code**: [`rub.py:2180–2214`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L2180)

**New architecture mapping**: Kharej Worker exposes a `/health` REST endpoint. Admin Panel polls it and shows a health dashboard.

---

## 11. Cookies Handling

**Description**: `cookies.txt` (Netscape format) is passed to yt-dlp for age-gated or auth-required YouTube content. Path resolved relative to `BASE_DIR`.

**Where in code**: [`rub.py:91`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L91), [`rub.py` — `_base_cmd`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L322)

**New architecture mapping**: `cookies.txt` stored on Kharej VPS (never leaves). Admin Panel settings page allows uploading a new cookies file (sent via secure channel, never over Rubika).

---

## 12. Deployment

### 12.1 Docker / docker-compose

**Description**: `Dockerfile` (python:3.11-slim + ffmpeg + yt-dlp) and `docker-compose.yml` with named volumes for `downloads` and `state`, port 9090 for Prometheus.

**Where in code**: [`Dockerfile`](https://github.com/xshayank/RubeTunes/blob/main/Dockerfile), [`docker-compose.yml`](https://github.com/xshayank/RubeTunes/blob/main/docker-compose.yml)

**New architecture mapping**: Separate `docker-compose.yml` files for Kharej VPS and Iran VPS. See [`task-split.md`](task-split.md).

---

### 12.2 Graceful Shutdown

**Description**: `main.py` traps SIGTERM/SIGINT and waits up to `SHUTDOWN_TIMEOUT_SEC` (default 30) for in-flight downloads before killing the process. The queue is saved to `queue_snapshot.json` on shutdown and restored on startup.

**Where in code**: [`main.py:73–110`](https://github.com/xshayank/RubeTunes/blob/main/main.py#L73), [`rub.py:101–158`](https://github.com/xshayank/RubeTunes/blob/main/rub.py#L101)

**New architecture mapping**: Both VPSes implement graceful shutdown. Kharej Worker saves job state; Iran Web API drains active connections.

---

## 13. Spotify Backend (SpotiFLAC Port)

**Description**: Full port of the SpotiFLAC Go backend: anonymous TOTP auth (no account needed), GraphQL persisted queries at `api-partner.spotify.com/pathfinder/v1/query` for track, album, playlist, artist, search. Strict endpoint policy (public REST API never called).

**Where in code**:
- [`rubetunes/spotify/`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/spotify/) — TOTP, session, client, models
- [`rubetunes/spotify_meta.py`](https://github.com/xshayank/RubeTunes/blob/main/rubetunes/spotify_meta.py)

**New architecture mapping**: Metadata lookups for search results can run on the Iran VPS (lightweight, no binary files). Full download resolution runs on Kharej Worker.

---

## 14. Monochrome / Tidal Backend Port

**Description**: Port of the Monochrome Tidal frontend: client-credentials auth, track/album/playlist/artist metadata, DASH/HLS streaming manifest parsing, Hi-Res FLAC download.

**Where in code**: `rubetunes/providers/monochrome/` (auth, client, manifest, download, models)

**New architecture mapping**: Kharej Worker.

---

## Feature → Architecture Mapping Summary

| Feature | Kharej Worker | Iran Web UI | Admin Panel | Shared |
|---------|:---:|:---:|:---:|:---:|
| YouTube download | ✅ | ✅ (quality picker) | | |
| Spotify track | ✅ | ✅ | | |
| Spotify playlist/album | ✅ | ✅ | | |
| Spotify artist browse | ✅ (metadata) | ✅ (browse UI) | | |
| Tidal, Qobuz, Amazon | ✅ | ✅ | | |
| SoundCloud, Bandcamp | ✅ | ✅ | | |
| musicdl (40+ sources) | ✅ | ✅ | | |
| Spotify search | ✅ (optional) | ✅ | | |
| Download queue | ✅ | ✅ (status display) | ✅ (global queue) | |
| Whitelist / ban | ✅ (validate) | | ✅ | ✅ (message schema) |
| Usage log / audit | ✅ (emit events) | | ✅ | ✅ (DB schema) |
| Download history | ✅ | ✅ ("My Library") | | |
| Upload retry | ✅ (S2 retry) | | | |
| YT channel notify | ✅ | ✅ (notifications) | | |
| Circuit breaker | ✅ | | ✅ (view) | |
| Metadata caching | ✅ | | ✅ (clear cache) | |
| ISRC resolution | ✅ | | | |
| Apple Music cover art | ✅ | | | |
| Audio metadata tagging | ✅ | | | |
| Rate limiting | | ✅ (enforce) | ✅ (configure) | ✅ (DB) |
| Disk guard | ✅ | | | |
| Prometheus metrics | ✅ | ✅ | ✅ (dashboard) | |
| Structured logging | ✅ | ✅ | | |
| Sentry integration | ✅ | ✅ | | |
| Health check | ✅ (endpoint) | | ✅ (dashboard) | |
| Cookies handling | ✅ | | ✅ (upload UI) | |
| Docker deployment | ✅ | ✅ | | |
| Graceful shutdown | ✅ | ✅ | | |
| ZIP splitting | ✅ | | | |
| Format hint (mp3/flac) | ✅ | ✅ (quality picker) | | |
