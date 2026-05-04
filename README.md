<div align="center">

<img src="https://img.shields.io/badge/RubeTunes-Music%20%26%20Video%20Bot-blueviolet?style=for-the-badge&logo=music&logoColor=white" alt="RubeTunes"/>

# 🎵 RubeTunes

**A powerful Rubika bot that downloads music & videos from the web and sends them straight to your chat.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)](https://python.org)
[![Rubika](https://img.shields.io/badge/Platform-Rubika-orange?style=flat-square)](https://rubika.ir)
[![License](https://img.shields.io/badge/License-GNUGPLv3-green?style=flat-square)](https://github.com/xshayank/RubeTunes/blob/main/LICENSE.md)
[![Tests](https://github.com/xshayank/RubeTunes/actions/workflows/tests.yml/badge.svg)](https://github.com/xshayank/RubeTunes/actions/workflows/tests.yml)
[![Version](https://img.shields.io/badge/version-2.0.0-blue?style=flat-square)](CHANGELOG.md)

</div>

---

## 🌟 What is RubeTunes?

**RubeTunes** is a self-hosted Rubika bot that lets you send any YouTube, Spotify, Qobuz, Tidal,
Amazon Music, SoundCloud, or Bandcamp link and receive the downloaded file — video or audio —
directly in your Rubika chat.

---

## ✨ Features

### 🎵 Music Sources

| Source | Format | Notes |
|--------|--------|-------|
| **Spotify** | MP3 / FLAC | Track, album, playlist, artist browse |
| **Qobuz** | Hi-Res FLAC / CD FLAC | No account needed |
| **Tidal** | FLAC | Metadata + ISRC-based download |
| **Amazon Music** | FLAC / M4A | Via ISRC resolution |
| **Deezer** | FLAC CD | Requires `DEEZER_ARL` |
| **SoundCloud** | MP3 | Direct URL download via yt-dlp |
| **Bandcamp** | FLAC / MP3 | Direct URL download via yt-dlp |
| **YouTube Music** | MP3 | Always available as fallback |
| **musicdl sources** | MP3 / FLAC | 40+ sources: Netease, QQ, Kuwo, Kugou, Migu, Deezer, Qobuz, Tidal, Spotify, Apple, … via `!musicdl` |

### 📺 Video

| Source | Notes |
|--------|-------|
| **YouTube** | Up to 4K; subtitles auto-detected |

### 🔧 Infrastructure

| Feature | Description |
|---------|-------------|
| 🔍 **`!search`** | Spotify search — top 10 results as numbered menu |
| 📋 **`!queue`** | Show your position in the download queue |
| 🕘 **`!history`** | Your recent downloads |
| ⚡ **Circuit Breaker** | Failing providers skipped automatically |
| 🔀 **Concurrent Batch** | Albums/playlists download up to 3 tracks in parallel |
| 💾 **Download History** | Repeat requests reuse cached files |
| 📊 **Prometheus Metrics** | `/metrics` endpoint on port 9090 |
| 🔒 **Admin Controls** | Whitelist, ban, logs, health check, cache management |
| 🎵 **`!musicdl`** | Multi-source music search \& download (40+ platforms via musicdl) |
| �� **Structured Logging** | JSON logs via `LOG_FORMAT=json` |
| 🛡️ **Rate Limiting** | Per-user rolling-hour track limit |
| 💾 **Disk Guard** | Rejects batches if insufficient disk space |



---

## 🔍 Search

RubeTunes includes a web-based search UI available at `/search`.  All searches
are executed on the **Kharej** (outside-Iran) worker — the Iran server only
forwards queries and renders results.  Thumbnails and cover images are uploaded
to S3 by the Kharej worker so the Iran server can serve them without accessing
external platforms directly.

### YouTube Search

- Navigate to `/search`, choose **YouTube**, enter a query, click **جستجو**.
- Top results are shown with thumbnails (served from S3 presigned URLs via
  `GET /search/thumb?key=…`).
- Each result displays the upload date as a relative Persian string (e.g. "📅 ۲ سال پیش")
  when the data is available, or the absolute date (e.g. "📅 2023-08-03") as a fallback.
- Each result has a **⬇ دانلود** button that triggers the existing YouTube
  download pipeline (`POST /jobs` with `platform=youtube`).

### Spotify Search

- Choose **Spotify** from the platform dropdown.
- Results are split into three categories: **Tracks**, **Albums**, **Playlists**.
- Cover images are downloaded by Kharej and stored in S3 under
  `thumbs/search/sp/{type}_{id}.jpg`.
- Download buttons invoke the existing Spotify download flow:
  - Tracks → `single` job type
  - Albums / Playlists → `batch` job type

### musicdl Search

- Choose **musicdl** from the platform dropdown.
- Text-only results (title, artist, source, duration) — no thumbnails.
- The Download button passes the query to the existing musicdl download path.

### API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/search` | Execute a search (body: `{platform, query, limit}`) |
| `GET`  | `/search/thumb?key=…` | Redirect to presigned S3 URL for a thumbnail |

---

## 🚀 Quick Start

### Docker (recommended)

```bash
# 1. Copy the example env file and fill in your credentials
cp .env.example .env
# Edit .env with your Rubika session, Spotify credentials, etc.

# 2. Start the bot
docker compose up -d

# 3. View logs
docker compose logs -f bot
```

The Prometheus metrics endpoint will be available at `http://localhost:9090/metrics`.

### Manual (Python)

```bash
# 1. Clone and install
git clone https://github.com/xshayank/RubeTunes.git
cd RubeTunes
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Download yt-dlp
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o yt-dlp
chmod +x yt-dlp

# 3. Configure
cp .env.example .env
# Edit .env

# 4. Run
python main.py
```

---

## 📋 Commands Reference

| Command | Description |
|---------|-------------|
| `!start` | Show help and command list |
| `!download <url>` | Download a YouTube video (quality menu) |
| `!spotify <url>` | Download Spotify track / album / playlist / artist |
| `!tidal <url>` | Download a Tidal track |
| `!qobuz <url>` | Download a Qobuz track |
| `!amazon <url>` | Download an Amazon Music track |
| `!soundcloud <url>` | Download a SoundCloud track |
| `!bandcamp <url>` | Download a Bandcamp track or album |
| `!search <query>` | Search Spotify, get a numbered menu |
| `!queue` | Show your position in the download queue |
| `!history [N]` | Show your last N downloads (default 10) |
| `!cancel` | Cancel any pending quality/platform selection |

**Format hint:** Append `mp3`, `flac`, or `m4a` to music commands:
```
!spotify https://open.spotify.com/track/... flac
```

### Admin Commands

| Command | Description |
|---------|-------------|
| `!admin whitelist on/off` | Enable/disable whitelist mode |
| `!admin whitelist add/remove <guid>` | Manage whitelist |
| `!admin ban/unban <guid>` | Ban/unban a user |
| `!admin logs [N]` | Show last N usage log entries |
| `!admin status` | Show bot status summary |
| `!admin clearcache [lru\|isrc\|all]` | Flush in-memory or disk cache |
| `!admin breakers` | Inspect provider circuit-breaker states |
| `!admin health` | Ping all provider endpoints (up/down/slow) |

---

## ⚙️ Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `RUBIKA_SESSION` | `rubika_session` | Rubika session name |
| `RUBIKA_PHONE` | — | Phone number for session auth |
| `ADMIN_GUIDS` | — | Comma-separated admin GUIDs |

### Music Sources

| Variable | Default | Description |
|----------|---------|-------------|
| `SPOTIFY_CLIENT_ID` | — | Spotify app client ID (optional fallback) |
| `SPOTIFY_CLIENT_SECRET` | — | Spotify app client secret |
| `DEEZER_ARL` | — | Deezer account ARL cookie (enables FLAC) |
| `TIDAL_TOKEN` | — | Tidal OAuth token (metadata only) |
| `QOBUZ_EMAIL` | — | Qobuz account email (authenticated fallback) |
| `QOBUZ_PASSWORD` | — | Qobuz account password |
| `TIDAL_ALT_BASES` | 4 mirrors | Comma-separated Tidal Alt proxy URLs |

### Downloads

| Variable | Default | Description |
|----------|---------|-------------|
| `BATCH_CONCURRENCY` | `3` | Parallel tracks in batch (1–6) |
| `ZIP_PART_SIZE_MB` | `1997` | Max ZIP part size in MB |

### Circuit Breaker

| Variable | Default | Description |
|----------|---------|-------------|
| `CIRCUIT_FAIL_THRESHOLD` | `3` | Failures before opening circuit |
| `CIRCUIT_FAIL_WINDOW_SEC` | `300` | Failure window (seconds) |
| `CIRCUIT_OPEN_DURATION_SEC` | `600` | Open duration (seconds) |

### Operations (New in v2.0)

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_FORMAT` | `text` | `text` or `json` (requires python-json-logger) |
| `METRICS_PORT` | `9090` | Prometheus port (`0` to disable) |
| `SENTRY_DSN` | — | Sentry DSN (optional error reporting) |
| `SHUTDOWN_TIMEOUT_SEC` | `30` | Graceful shutdown timeout seconds |
| `USER_TRACKS_PER_HOUR` | `100` | Per-user rate limit (non-admins) |
| `MIN_FREE_SPACE_MB` | `500` | Minimum free disk space for batch reject |
| `MIN_FREE_SPACE_MULTIPLIER` | `2.0` | Free space must be N× estimated |
| `HEURISTIC_MB_PER_TRACK` | `30` | Estimated MB per FLAC track |

---

## 🏗️ Architecture

```
RubeTunes/
├── main.py              # Entry point: graceful shutdown, metrics, Sentry init
├── rub.py               # Bot handlers (Rubika events → download commands)
├── spotify_dl.py        # Compatibility shim → imports from rubetunes/
├── rubetunes/
│   ├── __init__.py      # __version__ = "2.0.0"
│   ├── cache.py         # LRU track-info cache + ISRC disk cache
│   ├── circuit_breaker.py  # Provider circuit-breaker state machine
│   ├── disk_guard.py    # Disk space check before batch download
│   ├── downloader.py    # Quality constants + download orchestration
│   ├── history.py       # downloads_history.json helpers
│   ├── logging_setup.py # setup_logging() (text/JSON toggle)
│   ├── metrics.py       # Prometheus counters/gauges/histograms
│   ├── rate_limiter.py  # Per-user rolling-hour rate limit
│   ├── resolver.py      # _resolve_all_platforms, Odesli, Songstats, MusicBrainz
│   ├── sentry_setup.py  # Sentry SDK init + capture helpers
│   ├── spotify_meta.py  # Spotify TOTP, tokens, GraphQL, SpotifyClient, ISRC
│   ├── tagging.py       # embed_metadata (MP3/FLAC/M4A)
│   ├── spotify/         # SpotiFLAC port — strict pathfinder-only endpoint policy
│   │   ├── __init__.py  # re-exports SpotifyClient, TOTPGenerator, get_token
│   │   ├── totp.py      # HMAC-SHA1 TOTP (no pyotp); known-vector constant
│   │   ├── session.py   # scrape/anon-token/cc-token/client-token chain
│   │   ├── client.py    # SpotifyClient (session + pathfinder v2 POST)
│   │   └── models.py    # dataclass models (SpotifyTrack, SpotifyAlbum, …)
│   └── providers/
│       ├── amazon.py    # Amazon Music
│       ├── apple_music.py  # iTunes Search API (cover art + metadata)
│       ├── bandcamp.py  # Bandcamp via yt-dlp
│       ├── deezer.py    # Deezer ISRC resolution
│       ├── qobuz.py     # Qobuz credential scraping + FLAC download
│       ├── soundcloud.py  # SoundCloud via yt-dlp
│       ├── tidal.py     # Tidal API (TIDAL_TOKEN)
│       └── tidal_alt.py # Tidal Alt proxy (no token)
└── tests/
    ├── test_spotify_dl.py      # Core resolver tests
    ├── test_new_modules.py     # Rate-limiter, disk-guard, Apple Music provider tests
    ├── test_regressions.py     # Regression tests + Spotify TOTP session fix
    └── test_spotify_pathfinder.py  # SpotiFLAC port: TOTP known-vector + GraphQL policy
```

### Music quality resolution chain

```
Spotify / Tidal / Qobuz / Amazon / SoundCloud / Bandcamp URL
        ↓
  ISRC extraction (Spotify/Tidal/Qobuz/Amazon metadata APIs)
        ↓
  Fan-out resolution (Odesli, Songstats, Deezer ISRC API)
        ↓
  1. Qobuz FLAC Hi-Res 27-bit  (no account needed)
  2. Qobuz FLAC CD 16-bit
  3. Deezer FLAC               (requires DEEZER_ARL)
  4. YouTube Music MP3 320k    (always available)
```

---

## 🎵 Spotify Backend: SpotiFLAC Port

This repo includes a full port of the [spotbye/SpotiFLAC](https://github.com/spotbye/SpotiFLAC)
backend (Go → Python), implementing **strict endpoint policy** — Spotify's public REST API
(`api.spotify.com/v1/*`) is **never called** for track info, album, playlist, search, or artist
operations.

### Endpoint policy

| Purpose | Endpoint |
|---------|----------|
| Web-player HTML scrape (`clientVersion`) | `https://open.spotify.com` |
| Anonymous token via TOTP | `https://open.spotify.com/api/token` |
| Client-credentials fallback | `https://accounts.spotify.com/api/token` |
| `client-token` header (v2 GraphQL) | `https://clienttoken.spotify.com/v1/clienttoken` |
| Internal track metadata (ISRC enrichment) | `https://spclient.wg.spotify.com/metadata/4/track/{gid}` |
| Internal album metadata (UPC enrichment) | `https://spclient.wg.spotify.com/metadata/4/album/{gid}` |
| **GraphQL v1 — getTrack, getAlbum, fetchPlaylist, queryArtistOverview, queryArtistDiscographyAll, searchDesktop** | `https://api-partner.spotify.com/pathfinder/v1/query` |
| **GraphQL v2 — session POST queries** | `https://api-partner.spotify.com/pathfinder/v2/query` |
| Cover art CDN | `https://i.scdn.co/image/{hex}` |

**Forbidden endpoints (never called):**
- `https://api.spotify.com/v1/tracks/*`
- `https://api.spotify.com/v1/playlists/*`
- `https://api.spotify.com/v1/albums/*`
- `https://api.spotify.com/v1/artists/*`
- `https://api.spotify.com/v1/search`

### Auth chain

1. Scrape `https://open.spotify.com` → `clientVersion` from `<script id="appServerConfig">`.
2. HMAC-SHA1 TOTP (secret embedded in the web-player, version 61) → call `/api/token`.
3. Fallback to `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` client-credentials flow.
4. `client-token` via `clienttoken.spotify.com` (required for pathfinder/v2).

### TOTP generator

A pure-Python HMAC-SHA1 TOTP implementation (no `pyotp`) lives in
`rubetunes/spotify/totp.py`.  The same secret bytes, period (30 s), digit count (6),
and `totpVer` (61) as SpotiFLAC are used.

A pinned known-vector test in `tests/test_spotify_pathfinder.py` verifies parity
with SpotiFLAC:
```python
# timestamp 1_700_000_000 → deterministic 6-digit code
assert _totp_raw(SPOTIFY_TOTP_SECRET, 1_700_000_000) == KNOWN_VECTOR_CODE
```

### New modules

```
rubetunes/spotify/
├── __init__.py   # re-exports SpotifyClient, TOTPGenerator, get_token
├── totp.py       # HMAC-SHA1 TOTP generator + known-vector constant
├── session.py    # web-player auth: scrape, anon-token, cc-token, client-token
├── client.py     # SpotifyClient (session + pathfinder v2 POST)
└── models.py     # dataclass models for track/album/playlist/artist responses
```

### New environment variables

| Variable | Description |
|----------|-------------|
| `SPOTIFY_CLIENT_ID` | Spotify app client ID — used for CC-token fallback when anonymous TOTP fails |
| `SPOTIFY_CLIENT_SECRET` | Spotify app client secret — same as above |

### Credit

Backend auth flow, TOTP secret/version, and GraphQL persisted-query hashes ported from
**[spotbye/SpotiFLAC](https://github.com/spotbye/SpotiFLAC)** (Go implementation).

---

## 🎵 Monochrome/Tidal Backend Port

This repo includes a full port of the backend logic from
**[monochrome-music/monochrome](https://github.com/monochrome-music/monochrome)**
(itself a fork of [edideaur/monochrome](https://github.com/edideaur/monochrome))
— a JavaScript/Bun/Vite application that streams and downloads Hi-Res FLACs
from Tidal.

### New module

```
rubetunes/providers/monochrome/
├── __init__.py      # Public exports
├── constants.py     # Base URLs, client ID/secret, proxy instances, quality maps
├── models.py        # Dataclass models (Track, Album, Playlist, Artist, ...)
├── auth.py          # Client-credentials token bootstrap + on-disk cache
├── client.py        # Async httpx client: search, track, album, playlist, artist, stream
├── manifest.py      # Streaming manifest parser (BTS JSON / DASH MPD / HLS)
└── download.py      # Track download helper with mutagen tag embedding
```

### API summary

| Feature | Endpoints |
|---------|-----------|
| Auth | POST `https://auth.tidal.com/v1/oauth2/token` (client_credentials) |
| Track metadata | `GET /info/?id=N` (proxy) → `GET https://api.tidal.com/v1/tracks/{id}/` |
| Album | `GET /album/?id=N` (proxy) → `GET https://api.tidal.com/v1/albums/{id}` |
| Playlist | `GET /playlist/?id=N` (proxy) → `GET https://api.tidal.com/v1/playlists/{id}` |
| Artist | `GET /artist/?id=N` (proxy) → `GET https://api.tidal.com/v1/artists/{id}` |
| Search | `GET /search/?q=N` (combined), `?s=`, `?a=`, `?al=`, `?p=`, `?v=` |
| Stream | `GET /trackManifests/…` then `/stream?id=N&quality=Q` |
| Cover art | `https://resources.tidal.com/images/{uuid}/{size}x{size}.jpg` |

Full endpoint inventory with upstream file:line citations:
**[docs/MONOCHROME_API_INVENTORY.md](docs/MONOCHROME_API_INVENTORY.md)**

### New environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONOCHROME_COUNTRY` | `US` | Country code passed to all Tidal API calls |
| `MONOCHROME_INSTANCES` | _(auto-discovered)_ | Comma-separated proxy instance base URLs; overrides auto-discovery |

### Credit

Backend logic ported from
**[monochrome-music/monochrome](https://github.com/monochrome-music/monochrome)**
(MIT-licensed community Tidal frontend).

---

### Running the Iran & Kharej servers locally

See **[docs/how-to-run-iran-kharej.md](docs/how-to-run-iran-kharej.md)** for a complete guide
covering prerequisites, environment variables, database setup, how to start both processes, and
troubleshooting tips.

### Running tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

### Type checking (mypy)

```bash
mypy rubetunes/ --ignore-missing-imports
```

### Linting

```bash
ruff check .
black --check .
```

### Pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

---

## 🚀 Recent improvements (v2.0.0)

See [CHANGELOG.md](CHANGELOG.md) for full details.

### SpotiFLAC features ported

| Feature | SpotiFLAC reference |
|---------|---------------------|
| Amazon Music decryption | `backend/amazon.go` |
| Tidal V2 manifest downloads | `backend/tidal.go DownloadFromManifest` |
| Parallel platform resolution | `backend/analysis.go CheckTrackAvailability` |
| M4A metadata tagging | `backend/metadata.go EmbedMetadata` |
| Tidal endpoint rotation | `backend/tidal_api_list.go` |
| In-process LRU metadata cache | `backend/recent_fetches.go` |
| Download history | `backend/history.go` |
| Authenticated Qobuz fallback | `backend/qobuz_api.go userLogin` |

### New in v2.0.0

- **Package refactor**: `spotify_dl.py` split into `rubetunes/` package (8 modules + providers/)
- **SoundCloud** (`!soundcloud`) and **Bandcamp** (`!bandcamp`) providers
- **Spotify search** (`!search`) — top 10 results as numbered menu
- **Queue status** (`!queue`) — position + items ahead
- **Apple Music metadata** enrichment — 1400×1400 cover art via iTunes API
- **Prometheus metrics** endpoint (`METRICS_PORT=9090`)
- **Structured JSON logging** (`LOG_FORMAT=json`)
- **Sentry integration** (`SENTRY_DSN`)
- **Graceful shutdown** (SIGTERM/SIGINT with configurable timeout)
- **Per-user rate limiting** (`USER_TRACKS_PER_HOUR`)
- **Disk space guard** before batch downloads
- **`!admin health`** — concurrent endpoint health check
- **Docker + compose** support
- **mypy**, **ruff**, **black**, **pre-commit** toolchain

---

## 📄 License

MIT © xshayank

---

## 🎵 musicdl Integration

RubeTunes ships an optional **musicdl** backend that brings 40+ music platforms to the bot
via the [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl) library.

### Bot commands

| Command | Description |
|---|---|
| `!musicdl sources` | List all registered source client names |
| `!musicdl search <query>` | Search across default sources |
| `!musicdl search <query> <SrcMusicClient>` | Search a specific source |
| `!musicdl <number>` | Download a track from the last search result |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `MUSICDL_DOWNLOAD_DIR` | `downloads/musicdl` | Directory for downloaded files |
| `MUSICDL_DEFAULT_SOURCES` | *(musicdl defaults)* | Comma-separated source list |
| `MUSICDL_PROXY` | *(none)* | HTTP/HTTPS proxy for all musicdl requests |
| `MUSICDL_MAX_RETRIES` | `1` | Per-request retry count per source (lower = faster failover) |
| `MUSICDL_CONNECT_TIMEOUT` | `5` | Seconds to wait for TCP connect to a source endpoint |
| `MUSICDL_READ_TIMEOUT` | `15` | Seconds to wait for response body (raise for slow connections) |

### Supported sources (as of v2.11.1)

QQ, Netease, Kuwo, Kugou, Migu, Qianqian, Bilibili, Fivesing, Soda, StreetVoice,
Spotify, Deezer, Qobuz, Tidal, Apple Music, YouTube Music, JOOX, SoundCloud, Jamendo,
Free Music Archive, Ximalaya, Lizhi, Qingting, LRTS, GDStudio, TuneHub, MP3Juice,
MyFreeMP3, JBSou, and many more.  Use `!musicdl sources` to see the full live list.

### Installation

```bash
pip install musicdl==2.11.1
```

musicdl bundles a Node.js binary via `nodejs-wheel` — no system `nodejs` required.

See [`docs/MUSICDL_INTEGRATION.md`](docs/MUSICDL_INTEGRATION.md) for full setup,
proxy configuration, geo-restriction notes, and known limitations.

> **Attribution**: musicdl is developed by [Zhenchao Jin (CharlesPikachu)](https://github.com/CharlesPikachu/musicdl)
> and licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).
> Commercial use of musicdl is **prohibited** per upstream license terms.
