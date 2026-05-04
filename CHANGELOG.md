# Changelog

All notable changes to RubeTunes are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] — Search Feature PR

### Added — Search

- **YouTube Search** (`/search`, platform=`youtube`):
  - Iran UI sends query → Kharej executes `yt-dlp ytsearchN:` → results returned to Iran.
  - Kharej downloads each thumbnail from `i.ytimg.com` and uploads it to S3 at
    `thumbs/search/yt/{video_id}.jpg` (existence-checked before re-upload).
  - Iran serves thumbnails via `GET /search/thumb?key=…` (presigned S3 redirect).
  - Download button triggers existing YouTube download pipeline (`POST /jobs`).

- **Spotify Search** (`/search`, platform=`spotify`):
  - Uses existing `spotify_search_multi()` (Spotify public GraphQL, no credentials needed).
  - Returns top results split across three categories: **Tracks**, **Albums**, **Playlists**.
  - Cover images fetched and uploaded to S3 at `thumbs/search/sp/{type}_{id}.jpg`.
  - Download button reuses existing Spotify download flow (single/batch job types).

- **musicdl Search** (`/search`, platform=`musicdl`):
  - Uses existing `MusicdlClient.search()` from `rubetunes/providers/musicdl/`.
  - Text-only results (title, artist, source, duration) — no thumbnails.
  - Download button passes query to existing musicdl download handler.

- **New contracts**: `SearchRequest`, `SearchResult`, `SearchFailed` message types
  added to `kharej/contracts.py` and re-exported from `iran/contracts.py`.

- **New kharej modules**:
  - `kharej/searchers/__init__.py`
  - `kharej/searchers/common.py` — shared `upload_thumb_to_s3()` helper
  - `kharej/searchers/youtube.py`
  - `kharej/searchers/spotify.py`
  - `kharej/searchers/musicdl.py`

- **Iran API**:
  - `POST /search` — sends `SearchRequest` to Kharej, waits up to 30 s for reply.
  - `GET /search/thumb?key=…` — presigned S3 redirect for thumbnails (only
    `thumbs/search/` prefix allowed).
  - `/search` UI page added; navigation link added to base template.

- **Tests**: `tests/test_search_handlers.py`, `kharej/tests/test_search.py`.

---

## [Unreleased] — Mega Improvement PR

### Added — Priority A: Foundation

- **A1 Package refactor**: Split `spotify_dl.py` (4543 lines) into `rubetunes/` package:
  - `rubetunes/cache.py` — LRU track-info cache + ISRC disk cache
  - `rubetunes/circuit_breaker.py` — provider circuit-breaker state machine
  - `rubetunes/history.py` — download history helpers
  - `rubetunes/tagging.py` — `embed_metadata` (MP3/FLAC/M4A)
  - `rubetunes/spotify_meta.py` — Spotify TOTP, tokens, GraphQL, SpotifyClient, ISRC
  - `rubetunes/providers/` — per-provider modules (Qobuz, Tidal, Tidal Alt, Deezer, Amazon)
  - `rubetunes/resolver.py` — multi-platform resolver (Odesli, Songstats, MusicBrainz)
  - `rubetunes/downloader.py` — quality constants + download orchestration
  - `spotify_dl.py` kept as thin compatibility shim (zero changes to `rub.py` imports)
- **A2 Type hints + mypy**: `mypy.ini` added; all public functions annotated; non-blocking mypy step in CI
- **A3 Pre-commit + linting**: `.pre-commit-config.yaml` with black, ruff, end-of-file-fixer, trailing-whitespace; `pyproject.toml` with black line-length 100 and ruff E/F/I/UP/B rules
- **A4 Docker + Compose**: `Dockerfile` (python:3.11-slim + ffmpeg + yt-dlp), `docker-compose.yml` with named volumes, port 9090 for metrics, `.dockerignore`
- **A5 Structured JSON logging**: `rubetunes/logging_setup.py` with `setup_logging()`; toggle via `LOG_FORMAT=json`; python-json-logger optional dependency
- **A6 Prometheus metrics**: `rubetunes/metrics.py`; counters `rubetunes_downloads_total`, `rubetunes_provider_failures_total`, `rubetunes_resolutions_total`; gauges `rubetunes_queue_depth`, `rubetunes_circuit_open`; histograms for duration; served on `METRICS_PORT` (default 9090)
- **A7 Sentry**: `rubetunes/sentry_setup.py`; init only if `SENTRY_DSN` set; captures user GUID + command tag
- **A8 Graceful shutdown**: `main.py` traps SIGTERM/SIGINT; waits up to `SHUTDOWN_TIMEOUT_SEC` for in-flight downloads; restores `queue_snapshot.json` on startup

### Added — Priority B: User-visible features

- **B1 `!search <query>`**: Spotify search via public REST API; top 10 results as numbered menu; user replies `!1`–`!10` to download
- **B3 `!queue`**: Shows user's position in queue + items ahead
- **B6 Format hint**: Append `mp3`/`flac`/`m4a` to music commands; documented in `!start`

### Added — Priority C: New providers

- **C1 SoundCloud provider**: `!soundcloud <url>` via yt-dlp; `rubetunes/providers/soundcloud.py`
- **C2 Bandcamp provider**: `!bandcamp <url>` via yt-dlp (FLAC preferred); `rubetunes/providers/bandcamp.py`
- **C4 Apple Music metadata enrichment**: `rubetunes/providers/apple_music.py`; iTunes Search API for high-res cover art (1400×1400) and track/disc numbers

### Added — Priority D: Operations & abuse prevention

- **D1 `!admin health`**: Concurrent HEAD pings of Qobuz, Deezer, Tidal, lrclib, MusicBrainz, Odesli, YouTube Music; reports up/down/slow (>2s)
- **D2 Disk space guard**: `rubetunes/disk_guard.py`; checks free space before batch download; rejects if < 2× estimated (configurable via `MIN_FREE_SPACE_MB`)
- **D3 Per-user rate limiting**: `rubetunes/rate_limiter.py`; rolling 1-hour window; default 100 tracks/hour (`USER_TRACKS_PER_HOUR`); cooldown ETA in rejection message

### Added — Priority E: Versioning

- **E1 Versioned releases**: `rubetunes/__init__.py` exports `__version__ = "2.0.0"`; this `CHANGELOG.md`; GitHub Actions release workflow `.github/workflows/release.yml` (tag `v*.*.*` → Docker image pushed to GHCR + GitHub Release)

### Changed

- `main.py` overhauled: logging, Sentry, Prometheus initialisation at startup; graceful SIGTERM/SIGINT shutdown
- `!start` help message updated with new commands
- `requirements.txt` adds `python-json-logger`, `prometheus-client`, `sentry-sdk`
- `requirements-dev.txt` adds `pre-commit`, `black`, `ruff`, `mypy`

### Fixed / Restored (regression fixes from audit)

- **R1 `build_platform_choices` / `best_source_label` / `download_track`**: Implemented in `rubetunes/downloader.py` and re-exported from `spotify_dl.py` compat shim. Waterfall order: Qobuz → Tidal Alt → Deezer → Amazon → YouTube Music. Auto entry prepended when ≥2 sources available.
- **R2 `download_track_from_choice`**: Replaced `NotImplementedError` stub with full async implementation using correct call-site signature `(info, choice, output_dir, ytdlp_bin)`. Includes: history dedup, provider dispatch, metadata embedding, circuit breaker reporting, Prometheus counters.
- **R3 Amazon proxy resolver**: `_get_amazon_stream_url(asin)` added to `rubetunes/providers/amazon.py`. Rotates through `amazon.spotbye.qzz.io` and `afkar.xyz` proxy bases with per-base timeout.
- **R4 Amazon decryption**: `_convert_or_rename_amazon()` added — applies `ffmpeg -decryption_key`, probes codec with ffprobe, renames/converts to `.flac`.
- **R5 Qobuz auth fallback**: `_qobuz_auth_login()` and `_get_qobuz_stream_url_auth()` added to `rubetunes/providers/qobuz.py`. Reads `QOBUZ_EMAIL`/`QOBUZ_PASSWORD`; MD5 wire format; cached token with 1-hour TTL.
- **R6 `!search` selection crash**: Added `search_result` branch to `selection_handler` in `rub.py`. Pulls `choices[idx]["url"]` and calls `_ask_quality`.
- **R7 YouTube Music MP3 fallback**: Fully implemented `rubetunes/providers/youtube.py` with `_get_youtube_music_url_by_isrc()` (ISRC-first, title fallback) and `_download_youtube_music()` (yt-dlp V0 MP3).
- **R8 Format hint parsing**: `_parse_format_hint(args)` added to `rubetunes/resolver.py`. Strips trailing `mp3`/`flac`/`m4a`/`hires`/`24bit` tokens.
- **R9 Queue snapshot restore**: `_save_queue_snapshot()` and `_restore_queue_snapshot()` added to `rub.py`; SIGTERM handler saves queue on shutdown; `__main__` block restores on startup.
- **R10 MusicBrainz pre-flight guard**: `_mb_available` flag cached for 60 s on failure; short-circuits subsequent calls during outage window. Mirrors SpotiFLAC's `ShouldSkipMusicBrainzMetadataFetch()`.

### Fixed — Spotify TOTP Authentication

- **`_fetch_anon_token`** now uses a persistent `requests.Session` that visits `open.spotify.com` first to obtain the `sp_t` cookie, which Spotify requires for the TOTP token endpoint to succeed.
- **Server-time sync**: `_fetch_spotify_server_time()` fetches Spotify's server timestamp before computing the TOTP code, eliminating clock-skew failures.
- **`_totp()` server_time parameter**: Accepts optional `server_time` override so the TOTP counter uses Spotify's clock rather than the bot host clock.
- **`SPOTIFY_TOTP_SECRET` env var**: Operators can override the hardcoded TOTP secret without code changes if Spotify rotates it.
- **Bundle scraping fallback**: `_try_scrape_totp_secret()` attempts to extract the active secret from Spotify's web-player JS bundle.
- **Retry with session reset**: `get_token()` resets the anonymous session and retries on first failure; falls back to client-credentials if both anon attempts fail.
- **`SpotifyClient._get_access_token`**: Updated to use `_get_totp_secret()` and `_fetch_spotify_server_time()` for consistent behaviour with the standalone token path.

### TODO (not yet implemented)

- **B4 `!favorite`** — per-user favorites file
- **B5 Inline progress bars** — `▰▰▰▱▱▱ 50% · 2.3 MB/s`
- **B7 Loudness normalization** — `--normalize` flag, ffmpeg loudnorm two-pass
- **B8 Auto-remember quality** — per-user preference file
- **B9 ReplayGain tags** — r128gain integration
- **B10 Album art upscale** — Apple Music cover fallback already implemented (C4)
- **C3 YouTube Music ISRC search** — ytmusicapi ISRC-first search
- **C5 Spotify in-app lyrics** — `/color-lyrics/v2/track/{id}` endpoint
- **C6 Batch dedup** — check history across simultaneous duplicate playlists
- **D4 Per-source rate limiting** — token-bucket per provider
- **D5 Audit log redaction** — redact full URLs for banned users

---

## [1.1.0] — UX & Resilience PR

### Added

- `!history` command — users see their own recent downloads; admins see global history
- `!admin clearcache` — flush LRU and/or ISRC disk cache on demand
- `!admin breakers` — inspect provider circuit-breaker states
- Concurrent batch downloads (albums/playlists, up to 3 in parallel, configurable)
- Provider circuit breaker — auto-skip failing providers
- `pytest` test suite covering parsers, resolvers, circuit breaker, LRU cache

---

## [1.0.0] — SpotiFLAC Parity PR

### Added

- Amazon Music decryption (`ffmpeg -decryption_key`)
- Tidal V2 manifest downloads (multi-segment)
- Parallel platform resolution (saves 5–10 s per track)
- M4A metadata tagging via `mutagen.mp4`
- Tidal endpoint rotation (`TIDAL_ALT_BASES`)
- In-process LRU metadata cache (256 entries, 10 min TTL)
- Download history (`downloads_history.json`)
- Authenticated Qobuz fallback (`QOBUZ_EMAIL` / `QOBUZ_PASSWORD`)
