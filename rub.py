# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import collections
import concurrent.futures
import logging
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from rubpy import Client as RubikaClient, filters

import spotify_dl as _spodl
import zip_split as _zip_split

# New rubetunes modules
try:
    from rubetunes.rate_limiter import check_rate_limit, record_usage
    _HAS_RATE_LIMITER = True
except ImportError:
    _HAS_RATE_LIMITER = False
    def check_rate_limit(guid): return (True, "")  # noqa: E704
    def record_usage(guid): pass  # noqa: E704

try:
    from rubetunes.disk_guard import check_disk_space
    _HAS_DISK_GUARD = True
except ImportError:
    _HAS_DISK_GUARD = False
    def check_disk_space(n, d): return (True, "")  # noqa: E704

try:
    from rubetunes.providers.soundcloud import parse_soundcloud_url, download_soundcloud
    _HAS_SOUNDCLOUD = True
except ImportError:
    _HAS_SOUNDCLOUD = False

try:
    from rubetunes.providers.bandcamp import parse_bandcamp_url, download_bandcamp
    _HAS_BANDCAMP = True
except ImportError:
    _HAS_BANDCAMP = False

try:
    from rubetunes.providers.musicdl import MusicdlClient
    from rubetunes.providers.musicdl.errors import MusicdlNotInstalledError
    _HAS_MUSICDL = True
except ImportError:
    _HAS_MUSICDL = False
    MusicdlClient = None  # type: ignore[assignment,misc]
    MusicdlNotInstalledError = None  # type: ignore[assignment,misc]

import rubetunes.upload_retry as _upload_retry
import rubetunes.yt_notify as _yt_notify

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class _SuppressDataEnc(logging.Filter):
    """Drop the noisy 'Missing data_enc key' debug lines from rubpy internals."""
    def filter(self, record):
        return "data_enc" not in record.getMessage()


logging.getLogger("rubpy.network").addFilter(_SuppressDataEnc())

SESSION = os.getenv("RUBIKA_SESSION", "rubika_session").strip()
PHONE_NUMBER = os.getenv("RUBIKA_PHONE", "").strip() or None

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
STATE_FILE = BASE_DIR / "state.json"

# Admin GUIDs loaded from env (comma-separated Rubika object GUIDs or phone numbers
# that identify the admin accounts in private chats).
_raw_admins = os.getenv("ADMIN_GUIDS", "").strip()
ADMIN_GUIDS: set = {g.strip() for g in _raw_admins.split(",") if g.strip()}

YTDLP_BIN = BASE_DIR / "yt-dlp"
COOKIES_FILE = BASE_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Queue snapshot — save on SIGTERM, restore on startup (R9)
# ---------------------------------------------------------------------------
_QUEUE_SNAPSHOT_FILE = BASE_DIR / "queue_snapshot.json"


def _save_queue_snapshot() -> None:
    """Persist the current download queue to disk so it survives a restart."""
    try:
        entries = []
        for entry in download_queue:
            entries.append({
                "user_guid":    entry.get("object_guid", ""),
                "command":      entry.get("command", ""),
                "args":         entry.get("title", ""),
                "submitted_at": entry.get("submitted_at", ""),
            })
        _QUEUE_SNAPSHOT_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
        log_startup = logging.getLogger("startup")
        log_startup.info("queue snapshot saved: %d entries", len(entries))
    except Exception as exc:
        logging.getLogger("startup").warning("queue snapshot save failed: %s", exc)


def _restore_queue_snapshot() -> None:
    """Read queue_snapshot.json (if present) and push entries back into download_queue."""
    if not _QUEUE_SNAPSHOT_FILE.exists():
        return
    log_startup = logging.getLogger("startup")
    try:
        data = json.loads(_QUEUE_SNAPSHOT_FILE.read_text())
        if not isinstance(data, list):
            return
        for item in data:
            if item.get("user_guid"):
                download_queue.append({
                    "object_guid":  item["user_guid"],
                    "url":          "",
                    "choice":       None,
                    "title":        item.get("args", ""),
                    "command":      item.get("command", ""),
                    "submitted_at": item.get("submitted_at", ""),
                    "queue_msg_id": None,
                })
        _QUEUE_SNAPSHOT_FILE.unlink(missing_ok=True)
        log_startup.info("queue snapshot restored: %d entries", len(data))
    except Exception as exc:
        log_startup.warning("queue snapshot restore failed: %s", exc)


import signal as _signal


def _handle_sigterm(signum: int, frame: object) -> None:
    """On SIGTERM/SIGINT, save the queue then re-raise default behavior."""
    _save_queue_snapshot()
    _signal.signal(signum, _signal.SIG_DFL)
    _signal.raise_signal(signum)


try:
    _signal.signal(_signal.SIGTERM, _handle_sigterm)
except (OSError, AttributeError):
    pass  # Not available on all platforms

YOUTUBE_RE = re.compile(
    r'https?://(?:(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/|v/)|youtu\.be/)[\w\-]+'
)

SPOTIFY_RE = re.compile(
    r'https?://open\.spotify\.com/track/([A-Za-z0-9]{22})'
    r'|spotify:track:([A-Za-z0-9]{22})'
    r'|(?<![A-Za-z0-9])([A-Za-z0-9]{22})(?![A-Za-z0-9])'
)

SPOTIFY_PLAYLIST_RE = re.compile(
    r'https?://open\.spotify\.com/playlist/([A-Za-z0-9]{22})'
    r'|spotify:playlist:([A-Za-z0-9]{22})'
)

SPOTIFY_ALBUM_RE = re.compile(
    r'https?://open\.spotify\.com/album/([A-Za-z0-9]{22})'
    r'|spotify:album:([A-Za-z0-9]{22})'
)

SPOTIFY_ARTIST_RE = re.compile(
    r'https?://open\.spotify\.com/artist/([A-Za-z0-9]{22})'
    r'|spotify:artist:([A-Za-z0-9]{22})'
)

TIDAL_RE = re.compile(
    r'https?://(?:www\.|listen\.)?tidal\.com/(?:browse/)?(?:track|album/[^/\s]+/track)/(\d+)'
    r'|https?://listen\.tidal\.com/album/[^/\s]+/track/(\d+)'
)

QOBUZ_RE = re.compile(
    r'https?://open\.qobuz\.com/track/(\d+)'
    r'|https?://(?:www\.)?qobuz\.com/[^\s]+/track[s]?/(\d+)'
)

AMAZON_RE = re.compile(
    r'https?://music\.amazon\.[a-z.]+/tracks/([A-Z0-9]{10,})'
    r'|[?&]trackAsin=([A-Z0-9]{10,})'
)

SOUNDCLOUD_RE = re.compile(
    r'https?://(?:www\.)?soundcloud\.com/[\w\-]+/[\w\-]+'
)

BANDCAMP_RE = re.compile(
    r'https?://[\w\-]+\.bandcamp\.com/(?:track|album)/[\w\-]+'
)

PROGRESS_RE = re.compile(
    r'\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+/s)'
)

UPDATE_INTERVAL = 3.0
SELECTION_TIMEOUT = 300.0          # seconds before a pending quality menu expires
MUSIC_SELECTION_TIMEOUT = 300.0    # same for music quality/platform menus
SIZE_LIMIT_BYTES = 2 * 1024 ** 3   # 2 GB hard limit — options above this are hidden
MAX_LOG_ENTRIES = 500               # keep the most recent N log lines

# Maximum size for each ZIP part produced by split_zip_from_files.
# Default: 1.95 GiB ≈ 2 094 006 272 bytes (safe margin below the 2 GB upload limit).
# Override via env var ZIP_PART_SIZE_BYTES (bytes) or ZIP_PART_SIZE_MB (megabytes).
_env_zip_bytes = os.getenv("ZIP_PART_SIZE_BYTES", "")
_env_zip_mb    = os.getenv("ZIP_PART_SIZE_MB", "")
if _env_zip_bytes:
    MAX_ZIP_PART_SIZE_BYTES = int(_env_zip_bytes)
elif _env_zip_mb:
    MAX_ZIP_PART_SIZE_BYTES = int(float(_env_zip_mb) * 1024 * 1024)
else:
    MAX_ZIP_PART_SIZE_BYTES = int(1.95 * 1024 ** 3)  # 1.95 GiB

# Concurrent track downloads within a batch (playlist / album).
# Override via BATCH_CONCURRENCY env var (default 3, clamped 1–6).
BATCH_CONCURRENCY = max(1, min(6, int(os.getenv("BATCH_CONCURRENCY", "3"))))

# Number of albums/singles shown per page in the artist album browser
ARTIST_ALBUMS_PAGE_SIZE = 10

# Per-chat pending quality-selection state.
# Key: object_guid  Value: {url, choices, title, timeout_task}
pending_selections: dict = {}

# Download queue -- one download runs at a time.
# Each entry: {object_guid, url, choice, title, queue_msg_id}
download_queue: collections.deque = collections.deque()
is_downloading: bool = False

# ---------------------------------------------------------------------------
# Persistent state: whitelist / ban / usage logs
# ---------------------------------------------------------------------------
# Structure stored in state.json:
#   whitelist_enabled : bool
#   whitelist         : list[str]   – allowed object_guids (when enabled)
#   banned            : list[str]   – permanently blocked object_guids
#   logs              : list[dict]  – usage log entries

_state_lock = asyncio.Lock()

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # Normalise
                data.setdefault("whitelist_enabled", False)
                data.setdefault("whitelist", [])
                data.setdefault("banned", [])
                data.setdefault("logs", [])
                return data
        except Exception:
            pass
    return {"whitelist_enabled": False, "whitelist": [], "banned": [], "logs": []}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# Load into memory at import time; all mutations go through async helpers.
_state: dict = _load_state()


def _is_admin(object_guid: str) -> bool:
    return object_guid in ADMIN_GUIDS


def _check_access(object_guid: str) -> tuple[bool, str]:
    """Return (allowed, reason).  Admins always pass."""
    if _is_admin(object_guid):
        return True, ""
    if object_guid in _state["banned"]:
        return False, "🚫 You have been banned from using this bot."
    if _state["whitelist_enabled"] and object_guid not in _state["whitelist"]:
        return False, "🔒 This bot is currently restricted. Contact the admin."
    return True, ""


def _append_log(object_guid: str, action: str, detail: str = "") -> None:
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "guid": object_guid,
        "action": action,
        "detail": detail,
    }
    _state["logs"].append(entry)
    # Keep only the most recent MAX_LOG_ENTRIES
    if len(_state["logs"]) > MAX_LOG_ENTRIES:
        _state["logs"] = _state["logs"][-MAX_LOG_ENTRIES:]
    _save_state(_state)


def make_bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return '\u2588' * filled + '\u2591' * (width - filled)


def _ytdlp_bin() -> str:
    return str(YTDLP_BIN) if YTDLP_BIN.exists() else "yt-dlp"


def _base_cmd() -> list:
    """Common yt-dlp flags shared by every invocation."""
    cmd = [
        _ytdlp_bin(),
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "--referer", "https://www.youtube.com/",
    ]
    cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]
    return cmd


async def fetch_video_info(url: str):
    """Run yt-dlp -j and return the parsed JSON dict, or None on failure."""
    cmd = _base_cmd() + ["-j", "--no-warnings", url]
    log = logging.getLogger("fetch_info")
    log.info("fetching info: %s", url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        log.error("yt-dlp -j timed out")
        return None
    if proc.returncode != 0:
        log.error("yt-dlp -j failed: %s", stderr.decode("utf-8", errors="replace"))
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("json parse error: %s", exc)
        return None


def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"~{mb / 1024:.1f} GB"
    return f"~{mb:.0f} MB"


def _estimate_size(fmt: dict, duration: float) -> int:
    """Return best-effort file size in bytes for a format dict.

    Prefers the exact ``filesize`` field, then ``filesize_approx``, and
    finally falls back to ``tbr`` (total bitrate in kbps) × duration.
    Returns 0 when no estimate is possible.
    """
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    tbr = fmt.get("tbr")  # kilobits per second
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return 0


def build_quality_menu(info: dict) -> list:
    """
    Build a numbered list of download choices from yt-dlp video JSON.

    Rules:
    - One entry per distinct resolution (best stream at each target height).
    - Audio-only MP3 entry.
    - Subtitle entry when available.
    - Any option whose estimated combined size exceeds SIZE_LIMIT_BYTES is skipped.
    """
    formats = info.get("formats", [])
    duration = float(info.get("duration") or 0)

    video_fmts = [
        f for f in formats
        if f.get("vcodec", "none") != "none" and f.get("height")
    ]
    audio_fmts = [
        f for f in formats
        if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"
    ]

    best_audio = (
        max(audio_fmts, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        if audio_fmts else None
    )
    audio_size = (
        _estimate_size(best_audio, duration)
        if best_audio else 0
    )

    choices = []
    seen_heights = set()

    for target_h in [2160, 1440, 1080, 720, 480, 360, 240]:
        at_height = [f for f in video_fmts if f.get("height") == target_h]
        if not at_height:
            # Fall back to best available height strictly below this target
            below = [f for f in video_fmts if f.get("height", 0) < target_h]
            if not below:
                continue
            actual_h = max(f["height"] for f in below)
            if actual_h in seen_heights:
                continue
            at_height = [f for f in video_fmts if f["height"] == actual_h]
        else:
            actual_h = target_h

        if actual_h in seen_heights:
            continue
        seen_heights.add(actual_h)

        best_vid = max(at_height, key=lambda f: f.get("tbr") or f.get("vbr") or 0)
        vid_size = _estimate_size(best_vid, duration)
        total_size = vid_size + audio_size

        # Skip options that would exceed the 2 GB send limit
        if total_size > SIZE_LIMIT_BYTES:
            continue

        size_str = _fmt_size(total_size)
        label = "\U0001f3ac {}p".format(actual_h)
        if size_str:
            label += "  ({})".format(size_str)

        choices.append({
            "label": label,
            "format": (
                "bestvideo[height<={}]+bestaudio[ext=m4a]"
                "/bestvideo[height<={}]+bestaudio"
                "/best[height<={}]"
            ).format(actual_h, actual_h, actual_h),
            "audio_only": False,
            "subtitle_only": False,
            "out_ext": "mp4",
            "target_height": actual_h,
        })

    # Audio-only MP3
    if best_audio and audio_size <= SIZE_LIMIT_BYTES:
        audio_size_str = _fmt_size(audio_size)
        label = "\U0001f3b5 Audio only (MP3)"
        if audio_size_str:
            label += "  ({})".format(audio_size_str)
        choices.append({
            "label": label,
            "format": "bestaudio/best",
            "audio_only": True,
            "subtitle_only": False,
            "out_ext": "mp3",
        })

    # Subtitles (negligible size \u2014 always include when available)
    subtitles = info.get("subtitles", {})
    auto_captions = info.get("automatic_captions", {})
    all_langs = list(subtitles.keys()) + [
        la for la in auto_captions if la not in subtitles
    ]
    if all_langs:
        preferred = next((la for la in all_langs if la.startswith("en")), all_langs[0])
        auto_note = (
            " (auto-generated)"
            if preferred in auto_captions and preferred not in subtitles
            else ""
        )
        choices.append({
            "label": "\U0001f4c4 Subtitles \u2014 {}{}".format(preferred, auto_note),
            "format": None,
            "audio_only": False,
            "subtitle_only": True,
            "subtitle_lang": preferred,
            "out_ext": "srt",
        })

    return choices


def build_ytdlp_cmd_for_choice(url: str, choice: dict) -> list:
    """Build the full yt-dlp command for a specific quality choice."""
    cmd = _base_cmd()

    if choice.get("subtitle_only"):
        lang = choice.get("subtitle_lang", "en")
        cmd += [
            "--skip-download",
            "--write-sub", "--write-auto-sub",
            "--sub-lang", lang,
            "--convert-subs", "srt",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        ]
    elif choice.get("audio_only"):
        cmd += [
            "-f", choice["format"],
            "-x", "--audio-format", "mp3",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
            "--newline",
        ]
    else:
        cmd += [
            "-f", choice["format"],
            "--merge-output-format", "mp4",
            "-o", str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
            "--newline",
        ]

    cmd.append(url)
    return cmd


app = RubikaClient(name=SESSION, display_welcome=True)


async def _notify_queue_positions() -> None:
    """Edit each waiting user's message to show their current queue position."""
    for pos, entry in enumerate(download_queue, 1):
        try:
            people = "person" if pos == 1 else "people"
            await app.edit_message(
                entry["object_guid"],
                entry["queue_msg_id"],
                "\u23f3 The bot is busy right now.\n"
                "There {} {} {} ahead of you.".format(
                    "is" if pos == 1 else "are", pos, people
                ),
            )
        except Exception:
            pass


async def _run_download_and_queue(
    object_guid: str, url: str, choice: dict, title: str
) -> None:
    """Run one download then hand off to the next entry in the queue."""
    global is_downloading
    log = logging.getLogger("queue")
    try:
        await _do_download(object_guid, url, choice, title, log)
    finally:
        if download_queue:
            next_entry = download_queue.popleft()
            await _notify_queue_positions()
            try:
                await app.edit_message(
                    next_entry["object_guid"],
                    next_entry["queue_msg_id"],
                    "\U0001f7e2 You are the first person in the queue!\n"
                    "Starting your download\u2026",
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            _dispatch_queue_entry(next_entry)
        else:
            is_downloading = False


async def _expire_selection(object_guid: str) -> None:
    """Cancel a pending quality menu after SELECTION_TIMEOUT seconds."""
    await asyncio.sleep(SELECTION_TIMEOUT)
    entry = pending_selections.pop(object_guid, None)
    if entry:
        try:
            await app.send_message(
                object_guid,
                "\u23f0 Selection timed out. Send !download <url> to try again."
            )
        except Exception:
            pass


@app.on_message_updates(filters.commands("start", prefixes="!"))
async def start_handler(update):
    await app.send_message(
        update.object_guid,
        "\U0001f3ac Welcome!\n\n"
        "\U0001f4cc Commands:\n"
        "  !download <url>        \u2014 Download a YouTube video\n"
        "  !spotify <url>         \u2014 Download a Spotify track / album / playlist\n"
        "  !tidal <url>           \u2014 Download a Tidal track\n"
        "  !qobuz <url>           \u2014 Download a Qobuz track\n"
        "  !amazon <url>          \u2014 Download an Amazon Music track\n"
        "  !soundcloud <url>      \u2014 Download a SoundCloud track\n"
        "  !bandcamp <url>        \u2014 Download a Bandcamp track\n"
        "  !search <query>        \u2014 Search Spotify (top 10 results)\n"
        "  !queue                 \u2014 Show your position in the download queue\n"
        "  !cancel                \u2014 Cancel any pending selection\n"
        "  !history [N]           \u2014 Show your last N downloads (default 10)\n"
        "  !start                 \u2014 Show this message\n\n"
        "\U0001f3b5 Format hint: append mp3 / flac / m4a to music commands\n"
        "  e.g. !spotify <url> mp3\n\n"
        "\U0001f3b5 Music download flow:\n"
        "  1\ufe0f\u20e3 Send a music command with a URL\n"
        "  2\ufe0f\u20e3 Choose audio quality (MP3 / FLAC CD / FLAC Hi-Res)\n"
        "  3\ufe0f\u20e3 Choose from the platforms that have the track\n"
        "  \U0001f4c2 For playlists/albums: quality is asked once, then all tracks\n"
        "     are downloaded and zipped automatically.\n"
        "  \U0001f4e6 Large zips are automatically split into multiple parts\n"
        "     (each \u2264 {:.1f} GiB) for upload.\n\n"
        "\U0001f3a4 Spotify artist pages:\n"
        "  Send !spotify https://open.spotify.com/artist/<id>\n"
        "  \u2022 See the artist\u2019s top 5 tracks\n"
        "  \u2022 Browse Albums and Singles\n"
        "  \u2022 Pick any item to download\n\n"
        "Supported lossless sources (when credentials are set):\n"
        "  \u2022 Qobuz FLAC Hi-Res / CD  (QOBUZ_EMAIL + QOBUZ_PASSWORD)\n"
        "  \u2022 Deezer FLAC CD          (DEEZER_ARL)\n"
        "  \u2022 YouTube Music MP3       (always available as fallback)".format(
            MAX_ZIP_PART_SIZE_BYTES / (1024 ** 3)
        )
    )


def _relative_time(iso_ts: str) -> str:
    """Return a human-readable relative time string for an ISO-8601 UTC timestamp."""
    if not iso_ts:
        return "unknown time"
    try:
        # Parse ISO 8601 (e.g. "2026-04-24T10:30:00Z")
        ts_str = iso_ts.rstrip("Z")
        fmt = "%Y-%m-%dT%H:%M:%S"
        then = time.mktime(time.strptime(ts_str, fmt))
        # Compensate: strptime gives local time; we stored UTC
        then_utc = then - (time.mktime(time.gmtime()) - time.mktime(time.localtime()))
        diff = time.time() - then_utc
        if diff < 60:
            return "just now"
        if diff < 3600:
            return "{} minute{} ago".format(int(diff // 60), "s" if diff >= 120 else "")
        if diff < 86400:
            return "{} hour{} ago".format(int(diff // 3600), "s" if diff >= 7200 else "")
        days = int(diff // 86400)
        return "{} day{} ago".format(days, "s" if days > 1 else "")
    except Exception:
        return iso_ts  # fall back to raw timestamp


@app.on_message_updates(filters.commands("history", prefixes="!"))
async def history_handler(update):
    """
    Show the user's recent downloads.
    Usage: !history [N|all]
      N    – show N most recent (default 10, max 25)
      all  – admin-only; show global recent downloads
    """
    object_guid = update.object_guid
    log = logging.getLogger("history")
    log.info("!history | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    parts = update.command  # ['history'] or ['history', arg]
    arg   = parts[1].strip().lower() if len(parts) >= 2 else ""

    show_all = False
    limit    = 10

    if arg == "all":
        if not _is_admin(object_guid):
            await app.send_message(
                object_guid,
                "🚫 Only admins can view global history. Use !history [N] to see your own."
            )
            return
        show_all = True
        limit    = 25
    elif arg:
        try:
            limit = max(1, min(25, int(arg)))
        except ValueError:
            await app.send_message(object_guid, "Usage: !history [N] (number 1–25) or !history all (admin only)")
            return

    try:
        all_entries = _spodl.get_download_history()
    except Exception as exc:
        log.error("get_download_history failed: %s", exc)
        await app.send_message(object_guid, "❌ Could not read download history.")
        return

    if show_all:
        entries = all_entries[:limit]
    else:
        # Filter to this user only
        entries = [e for e in all_entries if e.get("user_guid") == object_guid][:limit]

    if not entries:
        await app.send_message(object_guid, "📭 No downloads yet.")
        return

    source_labels = {
        "qobuz":   "Qobuz",
        "deezer":  "Deezer",
        "tidal":   "Tidal",
        "amazon":  "Amazon Music",
        "spotify": "Spotify",
        "youtube": "YouTube Music",
    }
    quality_labels = {
        _spodl.QUALITY_MP3:     "MP3 320k",
        _spodl.QUALITY_FLAC_CD: "FLAC CD",
        _spodl.QUALITY_FLAC_HI: "FLAC Hi-Res",
    }

    header = "🕘 Global recent downloads:" if show_all else "🕘 Your recent downloads:"
    lines  = [header]
    for i, e in enumerate(entries, 1):
        title   = e.get("title")   or "Unknown title"
        artists = e.get("artists") or "Unknown artist"
        source  = source_labels.get((e.get("source") or "").lower(), e.get("source") or "?")
        quality = quality_labels.get(e.get("quality") or "", e.get("quality") or "?")
        rel     = _relative_time(e.get("timestamp") or "")
        user_part = " | user: {}".format(e["user_guid"][:8]) if show_all and e.get("user_guid") else ""
        lines.append(
            "{}. {} — {}\n   {} | {} | {}{}".format(
                i, title, artists, source, quality, rel, user_part
            )
        )

    await app.send_message(object_guid, "\n".join(lines))


@app.on_message_updates(filters.commands("download", prefixes="!"))
async def download_handler(update):
    log = logging.getLogger("download")
    log.info("!download | text=%r | guid=%s", update.text, update.object_guid)

    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    match = YOUTUBE_RE.search(args)

    if not match:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid YouTube link.\n"
            "Example: !download https://youtu.be/abc123"
        )
        return

    url = match.group(0)
    _append_log(object_guid, "download_requested", url)

    # Cancel any existing pending selection for this chat
    old = pending_selections.pop(object_guid, None)
    if old and old.get("timeout_task"):
        old["timeout_task"].cancel()

    status = await app.send_message(object_guid, "\U0001f50d Fetching available qualities\u2026")
    status_id = status.message_id

    info = await fetch_video_info(url)
    if not info:
        await app.edit_message(
            object_guid, status_id,
            "\u274c Could not fetch video info. Check the URL and try again."
        )
        return

    choices = build_quality_menu(info)
    if not choices:
        await app.edit_message(
            object_guid, status_id,
            "\u274c No downloadable formats found under 2 GB."
        )
        return

    title = info.get("title", "")
    duration_s = info.get("duration")
    duration_str = ""
    if duration_s:
        m_val, s_val = divmod(int(duration_s), 60)
        h_val, m_val = divmod(m_val, 60)
        duration_str = "{}:{:02d}:{:02d}".format(h_val, m_val, s_val) if h_val else "{}:{:02d}".format(m_val, s_val)

    lines = []
    if title:
        lines.append("\U0001f3ac {}".format(title))
    if duration_str:
        lines.append("\u23f1 {}".format(duration_str))
    lines.append("")
    lines.append("Choose a quality (options above 2 GB are hidden):")
    for i, c in enumerate(choices, 1):
        lines.append("  !{} \u2014 {}".format(i, c["label"]))
    lines.append("  !cancel \u2014 Cancel")
    lines.append("")
    lines.append("\u23f0 This menu expires in 5 minutes.")

    await app.edit_message(object_guid, status_id, "\n".join(lines))

    timeout_task = asyncio.create_task(_expire_selection(object_guid))
    pending_selections[object_guid] = {
        "url": url,
        "choices": choices,
        "title": title,
        "timeout_task": timeout_task,
    }
    log.info("menu sent | %d choices | guid=%s", len(choices), object_guid)


@app.on_message_updates(
    filters.commands(["1", "2", "3", "4", "5", "6", "7", "8", "9",
                      "10", "11", "cancel"], prefixes="!")
)
async def selection_handler(update):
    global is_downloading
    log = logging.getLogger("selection")
    object_guid = update.object_guid
    cmd_name = update.command[0] if update.command else ""
    log.info("!%s | guid=%s", cmd_name, object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    if cmd_name == "cancel":
        entry = pending_selections.pop(object_guid, None)
        if entry and entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        # Also remove from download queue if the user is waiting there
        queue_before = len(download_queue)
        new_queue = collections.deque(
            e for e in download_queue if e["object_guid"] != object_guid
        )
        download_queue.clear()
        download_queue.extend(new_queue)
        queue_removed = len(download_queue) < queue_before
        if queue_removed:
            asyncio.create_task(_notify_queue_positions())

        msg = (
            "\u274c Download cancelled."
            if (entry or queue_removed)
            else "\u2139\ufe0f No active download to cancel."
        )
        await app.send_message(object_guid, msg)
        return

    entry = pending_selections.get(object_guid)
    if not entry:
        await app.send_message(
            object_guid,
            "\u26a0\ufe0f No active menu. Use !download or a music command first."
        )
        return

    try:
        idx = int(cmd_name) - 1
    except ValueError:
        await app.send_message(object_guid, "\u274c Invalid selection.")
        return

    entry_type = entry.get("type", "youtube")

    # ------------------------------------------------------------------
    # Music quality selection (step 1 of 2 for tracks, step 1 of 1 for
    # playlists/albums)
    # ------------------------------------------------------------------
    if entry_type == "music_quality":
        choices = entry["choices"]  # QUALITY_MENU list from spotify_dl
        if idx < 0 or idx >= len(choices):
            await app.send_message(
                object_guid,
                "\u274c Please choose between !1 and !{}, or !cancel.".format(len(choices))
            )
            return

        # Consume
        pending_selections.pop(object_guid, None)
        if entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        quality = choices[idx]["quality"]
        url     = entry["url"]
        platform = entry["platform"]
        url_type = entry["url_type"]   # "track" | "playlist" | "album"
        extra    = entry.get("extra", {})

        _append_log(object_guid, "music_quality_selected", "{} | {}".format(quality, url))

        if url_type == "track":
            # Resolve platforms then show platform-selection menu
            asyncio.create_task(
                _show_platform_menu(object_guid, url, platform, quality, log)
            )
        else:
            # Playlist / album — start batch download directly
            asyncio.create_task(
                _do_batch_download(object_guid, url, platform, quality, url_type, extra, log)
            )
        return

    # ------------------------------------------------------------------
    # Music platform selection (step 2 of 2 for single tracks)
    # ------------------------------------------------------------------
    if entry_type == "music_platform":
        choices = entry["choices"]
        if idx < 0 or idx >= len(choices):
            await app.send_message(
                object_guid,
                "\u274c Please choose between !1 and !{}, or !cancel.".format(len(choices))
            )
            return

        # Consume
        pending_selections.pop(object_guid, None)
        if entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        choice = choices[idx]
        url   = entry["url"]
        info  = entry["info"]
        _append_log(object_guid, "music_platform_selected", "{} | {}".format(choice["label"], url))

        if not is_downloading:
            is_downloading = True
            asyncio.create_task(
                _run_music_choice_and_queue(object_guid, info, choice)
            )
        else:
            pos = len(download_queue) + 1
            people = "person" if pos == 1 else "people"
            queue_msg = await app.send_message(
                object_guid,
                "\u23f3 The bot is busy right now.\n"
                "There {} {} {} ahead of you.".format(
                    "is" if pos == 1 else "are", pos, people
                ),
            )
            download_queue.append({
                "object_guid": object_guid,
                "url":         url,
                "choice":      None,          # sentinel: music-choice entry
                "info":        info,
                "music_choice": choice,
                "title":       "{} — {}".format(info.get("title",""), ", ".join(info.get("artists") or [])),
                "queue_msg_id": queue_msg.message_id,
            })
            log.info("music_choice queued at position %d | guid=%s", pos, object_guid)
        return

    # ------------------------------------------------------------------
    # Search result selection (R6)
    # ------------------------------------------------------------------
    if entry_type == "search_result":
        choices = entry.get("choices", [])
        if idx < 0 or idx >= len(choices):
            await app.send_message(
                object_guid,
                "\u274c Please choose between !1 and !{}, or !cancel.".format(len(choices))
            )
            return

        # Consume
        pending_selections.pop(object_guid, None)
        if entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        selected = choices[idx]
        track_url = selected.get("url") or ""
        label     = selected.get("label") or "Track"

        if not track_url:
            await app.send_message(object_guid, "\u274c No URL for that result.")
            return

        _append_log(object_guid, "search_selected", "{} | {}".format(label, track_url))
        await _ask_quality(object_guid, "\U0001f3b5 {}".format(label),
                           track_url, "spotify", "track", {})
        return

    # ------------------------------------------------------------------
    # Artist top-track / albums / singles selection
    # ------------------------------------------------------------------
    if entry_type == "artist_menu":
        # Indices 0-4: top tracks; index 5: Albums; index 6: Singles
        artist_id   = entry["artist_id"]
        artist_name = entry["artist_name"]
        top_tracks  = entry["top_tracks"]

        # !6 → Albums (0-indexed: 5)
        if idx == 5:
            pending_selections.pop(object_guid, None)
            if entry.get("timeout_task"):
                entry["timeout_task"].cancel()
            _append_log(object_guid, "artist_albums", artist_id)
            asyncio.create_task(
                _show_artist_album_list(object_guid, artist_id, artist_name, "album", 0, log)
            )
            return

        # !7 → Singles (0-indexed: 6)
        if idx == 6:
            pending_selections.pop(object_guid, None)
            if entry.get("timeout_task"):
                entry["timeout_task"].cancel()
            _append_log(object_guid, "artist_singles", artist_id)
            asyncio.create_task(
                _show_artist_album_list(object_guid, artist_id, artist_name, "single", 0, log)
            )
            return

        # Otherwise it should be a top track selection (0-4)
        if idx < 0 or idx >= len(top_tracks):
            await app.send_message(
                object_guid,
                "\u274c Please choose a number from the menu, or !cancel."
            )
            return

        pending_selections.pop(object_guid, None)
        if entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        track = top_tracks[idx]
        track_id = track.get("id")
        if not track_id:
            await app.send_message(object_guid, "\u274c Could not get track ID.")
            return

        _append_log(object_guid, "artist_top_track", track_id)
        track_url = "https://open.spotify.com/track/{}".format(track_id)
        await _ask_quality(object_guid, "\U0001f3b5 {}".format(track["title"]),
                           track_url, "spotify", "track", {})
        return

    if entry_type == "artist_list":
        # Items are albums/singles; last item may be "Next page" if has_next
        items    = entry["items"]
        has_next = entry.get("has_next", False)
        total_choices = len(items) + (1 if has_next else 0)

        if idx < 0 or idx >= total_choices:
            await app.send_message(
                object_guid,
                "\u274c Please choose between !1 and !{}, or !cancel.".format(total_choices)
            )
            return

        pending_selections.pop(object_guid, None)
        if entry.get("timeout_task"):
            entry["timeout_task"].cancel()

        # "Next page" was selected
        if has_next and idx == len(items):
            _append_log(object_guid, "artist_list_next", "{} {}".format(entry["group"], entry["artist_id"]))
            asyncio.create_task(
                _show_artist_album_list(
                    object_guid,
                    entry["artist_id"],
                    entry["artist_name"],
                    entry["group"],
                    entry["next_offset"],
                    log,
                )
            )
            return

        # Album / single selected — start batch download
        alb = items[idx]
        alb_id   = alb["id"]
        alb_name = alb["name"]
        _append_log(object_guid, "artist_list_album", alb_id)
        await app.send_message(object_guid, "\U0001f50d Fetching tracks for {}\u2026".format(alb_name))
        try:
            loop = asyncio.get_event_loop()
            alb_info, track_ids = await loop.run_in_executor(
                None, _spodl.get_spotify_album_tracks, alb_id
            )
            collection_name = "{} \u2014 {}".format(
                alb_info.get("name", alb_name),
                ", ".join(alb_info.get("artists") or []),
            )
            header = "\U0001f4bf {} ({} tracks)".format(collection_name, len(track_ids))
            await _ask_quality(object_guid, header,
                               "https://open.spotify.com/album/{}".format(alb_id),
                               "spotify", "album", {
                                   "collection_name": collection_name,
                                   "track_ids":       track_ids,
                               })
        except Exception as exc:
            log.error("artist album fetch failed: %s", exc)
            await app.send_message(object_guid, "\u274c Could not fetch album tracks: {}".format(exc))
        return

    # ------------------------------------------------------------------
    # YouTube quality selection (original behaviour)
    # ------------------------------------------------------------------
    choices = entry["choices"]
    if idx < 0 or idx >= len(choices):
        await app.send_message(
            object_guid,
            "\u274c Please choose between !1 and !{}, or !cancel.".format(len(choices))
        )
        return

    # Consume the entry and cancel its expiry timer before starting download
    pending_selections.pop(object_guid, None)
    if entry.get("timeout_task"):
        entry["timeout_task"].cancel()

    choice = choices[idx]
    url = entry["url"]
    title = entry.get("title", "")
    _append_log(object_guid, "download_started", "{} | {}".format(choice["label"], url))

    # -- Queue logic ----------------------------------------------------------
    # Check and set is_downloading atomically (no await in between -- safe in asyncio).
    if not is_downloading:
        is_downloading = True
        asyncio.create_task(_run_download_and_queue(object_guid, url, choice, title))
    else:
        pos = len(download_queue) + 1
        people = "person" if pos == 1 else "people"
        queue_msg = await app.send_message(
            object_guid,
            "\u23f3 The bot is busy right now.\n"
            "There {} {} {} ahead of you.".format(
                "is" if pos == 1 else "are", pos, people
            ),
        )
        download_queue.append({
            "object_guid": object_guid,
            "url": url,
            "choice": choice,
            "title": title,
            "queue_msg_id": queue_msg.message_id,
        })
        log.info("queued at position %d | guid=%s", pos, object_guid)



async def _show_platform_menu(
    object_guid: str, url: str, platform: str, quality: str, log
) -> None:
    """
    Resolve all platforms for *url* and show the user a numbered platform menu.
    Stores a 'music_platform' entry in pending_selections so the user can pick.
    """
    status = await app.send_message(
        object_guid, "\U0001f50d Checking platforms\u2026"
    )
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()

        # Fetch metadata (reuse existing per-platform fetchers)
        await app.edit_message(object_guid, status_id, "\U0001f4e1 Fetching track info\u2026")
        if platform == "spotify":
            track_id = _spodl.parse_spotify_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Spotify track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_track_info, track_id)

        elif platform == "tidal":
            track_id = _spodl.parse_tidal_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Tidal track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_tidal_track_info, track_id)

        elif platform == "qobuz":
            track_id = _spodl.parse_qobuz_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Qobuz track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_qobuz_track_info, track_id)

        elif platform == "amazon":
            track_id = _spodl.parse_amazon_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Amazon Music track ID from: {}".format(url))
                return
            ytbin = _ytdlp_bin()
            info = await loop.run_in_executor(None, _spodl.get_amazon_track_info, track_id, ytbin)

        else:
            await app.edit_message(object_guid, status_id,
                                    "\u274c Unknown platform: {}".format(platform))
            return

        title       = info.get("title") or "Unknown"
        artists_str = ", ".join(info.get("artists") or [])

        # Build platform choices for the requested quality
        choices = _spodl.build_platform_choices(info, quality)

        if not choices:
            await app.edit_message(
                object_guid, status_id,
                "\u274c No download sources found for this track."
            )
            return

        quality_label = _spodl._QUALITY_LABELS.get(quality, quality)
        lines = [
            "\U0001f3b5 {} \u2014 {}".format(title, artists_str),
            "\U0001f4c0 Requested: {}".format(quality_label),
            "",
            "Available platforms:",
        ]
        for i, c in enumerate(choices, 1):
            lines.append("  !{} \u2014 {}".format(i, c["label"]))
        lines += ["  !cancel \u2014 Cancel", "", "\u23f0 This menu expires in 5 minutes."]

        await app.edit_message(object_guid, status_id, "\n".join(lines))

        timeout_task = asyncio.create_task(_expire_selection(object_guid))
        pending_selections[object_guid] = {
            "type":         "music_platform",
            "url":          url,
            "info":         info,
            "quality":      quality,
            "choices":      choices,
            "title":        "{} \u2014 {}".format(title, artists_str),
            "timeout_task": timeout_task,
        }
        log.info("platform menu sent | %d choices | guid=%s", len(choices), object_guid)

    except Exception as exc:
        log.exception("_show_platform_menu error: %s", exc)
        try:
            await app.edit_message(object_guid, status_id,
                                    "\u274c Error resolving track: {}".format(exc))
        except Exception:
            pass


async def _do_batch_download(
    object_guid: str, url: str, platform: str, quality: str,
    url_type: str, extra: dict, log
) -> None:
    """
    Download all tracks in a playlist or album, zip them (splitting if needed),
    and send the zip parts.

    Up to BATCH_CONCURRENCY (default 3) tracks are fetched in parallel using a
    ThreadPoolExecutor.  Final ZIP ordering matches the original track order.
    Per-track errors are collected and reported in the summary; they never abort
    the entire batch.

    *url_type* is "playlist" or "album".
    *extra* carries pre-fetched info like collection_name and track_ids.
    """
    status = await app.send_message(
        object_guid, "\U0001f4c2 Starting batch download\u2026"
    )
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()
        collection_name = extra.get("collection_name", "music")
        track_ids       = extra.get("track_ids", [])

        if not track_ids:
            await app.edit_message(object_guid, status_id,
                                    "\u274c No tracks found in the playlist/album.")
            return

        total = len(track_ids)

        # D2: Disk space guard
        if _HAS_DISK_GUARD:
            ok, disk_msg = check_disk_space(total, DOWNLOAD_DIR)
            if not ok:
                await app.edit_message(object_guid, status_id, disk_msg)
                return

        await app.edit_message(
            object_guid, status_id,
            "\U0001f4c2 {} — {} tracks\n\U0001f3b5 Quality: {}".format(
                collection_name,
                total,
                _spodl._QUALITY_LABELS.get(quality, quality),
            )
        )

        # Create a temp directory for this batch
        batch_dir = DOWNLOAD_DIR / _spodl._safe_filename(collection_name)
        batch_dir.mkdir(parents=True, exist_ok=True)

        ytbin = _ytdlp_bin()

        # Shared progress state, protected by a lock for thread safety
        _progress_lock = threading.Lock()
        _done_count    = [0]   # mutable container so worker lambdas can update it
        _fail_count    = [0]

        def _download_one(track_id: str) -> "Path | None":
            """Download a single track; return the file path or None on failure."""
            try:
                info    = _spodl.get_track_info(track_id)
                choices = _spodl.build_platform_choices(info, quality)
                choice  = choices[0] if choices else None
                if not choice:
                    with _progress_lock:
                        _fail_count[0] += 1
                    return None

                # download_track_from_choice is async; run it in a fresh event loop
                # (asyncio.run creates and closes a new loop — safe from a non-async thread)
                fp = asyncio.run(
                    _spodl.download_track_from_choice(info, choice, batch_dir, ytbin)
                )

                with _progress_lock:
                    _done_count[0] += 1
                return fp
            except Exception as exc:
                log.warning("batch track %s failed: %s", track_id, exc)
                with _progress_lock:
                    _fail_count[0] += 1
                return None

        # Submit all tracks to the thread pool; preserve original order via index
        track_files: list = [None] * total   # slot per track, in original order
        failed_ids: list = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=BATCH_CONCURRENCY, thread_name_prefix="batch"
        ) as pool:
            future_to_idx = {
                pool.submit(_download_one, tid): i
                for i, tid in enumerate(track_ids)
            }

            last_ui_update = time.monotonic()
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                fp = future.result()  # never raises — _download_one catches everything
                if fp is not None:
                    track_files[idx] = fp
                else:
                    failed_ids.append(track_ids[idx])

                # Throttle progress edits to avoid flood-control
                now = time.monotonic()
                if now - last_ui_update >= 3.0:
                    last_ui_update = now
                    with _progress_lock:
                        done  = _done_count[0]
                        fails = _fail_count[0]
                    try:
                        await app.edit_message(
                            object_guid, status_id,
                            "\U0001f4e5 Downloading [{}/{}] ({} failed so far)\u2026".format(
                                done + fails, total, fails
                            )
                        )
                    except Exception:
                        pass

        # Collect only successful downloads in original track order
        downloaded = [r for r in track_files if r is not None]

        if not downloaded:
            await app.edit_message(object_guid, status_id,
                                    "\u274c No tracks could be downloaded.")
            return

        # Zip all downloaded files — split into parts if needed
        safe_name  = _spodl._safe_filename(collection_name)
        out_prefix = DOWNLOAD_DIR / safe_name
        await app.edit_message(
            object_guid, status_id,
            "\U0001f4e6 Packaging {} files\u2026".format(len(downloaded))
        )
        zip_parts = _zip_split.split_zip_from_files(
            downloaded, out_prefix, MAX_ZIP_PART_SIZE_BYTES
        )

        # Clean up individual track files
        for fp in downloaded:
            try:
                fp.unlink()
            except Exception:
                pass
        try:
            batch_dir.rmdir()
        except Exception:
            pass

        n_parts = len(zip_parts)
        caption_base = "{}\n{} tracks | {}".format(
            collection_name, len(downloaded),
            _spodl._QUALITY_LABELS.get(quality, quality)
        )
        if failed_ids:
            caption_base += "\n\u26a0\ufe0f {} track(s) failed".format(len(failed_ids))

        if n_parts > 1:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e6 Packaging into {} parts\u2026".format(n_parts)
            )

        for i, zip_path in enumerate(zip_parts, 1):
            size_mb = zip_path.stat().st_size / (1024 * 1024)
            if n_parts > 1:
                caption = "{}\nPart {}/{}\n{} tracks | {}".format(
                    collection_name, i, n_parts, len(downloaded),
                    _spodl._QUALITY_LABELS.get(quality, quality)
                )
                if failed_ids:
                    caption += "\n\u26a0\ufe0f {} track(s) failed".format(len(failed_ids))
                await app.edit_message(
                    object_guid, status_id,
                    "\U0001f4e4 Sending part {}/{}: {} ({:.1f} MB)\u2026".format(
                        i, n_parts, zip_path.name, size_mb
                    )
                )
            else:
                caption = caption_base
                await app.edit_message(
                    object_guid, status_id,
                    "\U0001f4e4 Sending zip ({:.1f} MB)\u2026".format(size_mb)
                )
            try:
                await app.send_document(object_guid, str(zip_path), caption=caption)
                log.info("batch zip part sent: %s (%.1f MB)", zip_path.name, size_mb)
            except Exception as exc:
                log.error("send zip part failed: %s", exc)
                _upload_retry.enqueue_failed_upload(
                    app_send_document=app.send_document,
                    object_guid=object_guid,
                    file_path=zip_path,
                    file_name=zip_path.name,
                    caption=caption,
                    provider="batch_zip_part",
                    exc=exc,
                )
                await app.send_message(
                    object_guid,
                    "📦 Upload of part {}/{} failed ({}). Will retry automatically every hour. "
                    "Send !uploads to see status, or !uploads cancel <id> to drop it.".format(
                        i, n_parts, exc
                    ),
                )
            finally:
                if zip_path.exists():
                    try:
                        zip_path.unlink()
                    except Exception:
                        pass

        summary = "\u2705 Done! {} tracks sent{}.".format(
            len(downloaded),
            " ({} parts)".format(n_parts) if n_parts > 1 else ""
        )
        if failed_ids:
            # Show up to 5 failed track IDs in the summary
            shown = failed_ids[:5]
            summary += "\n\u26a0\ufe0f {} of {} failed: {}{}".format(
                len(failed_ids), total,
                ", ".join(shown),
                " …" if len(failed_ids) > 5 else "",
            )
        await app.edit_message(object_guid, status_id, summary)

    except Exception as exc:
        log.exception("_do_batch_download error: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Spotify artist pages
# ---------------------------------------------------------------------------

async def _show_artist_menu(object_guid: str, artist_id: str, log) -> None:
    """
    Fetch Spotify artist info and send an artist overview menu with:
    - artist name + image caption
    - top 5 tracks (numbered)
    - options: Albums / Singles
    """
    status = await app.send_message(
        object_guid, "\U0001f50d Fetching artist info\u2026"
    )
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()
        artist = await loop.run_in_executor(None, _spodl.get_spotify_artist_info, artist_id)

        name       = artist.get("name", "Unknown Artist")
        image_url  = artist.get("image_url")
        top_tracks = artist.get("top_tracks", [])

        lines = ["\U0001f3a4 {}".format(name), ""]
        if top_tracks:
            lines.append("\U0001f3b5 Top tracks:")
            for i, t in enumerate(top_tracks, 1):
                artists_str = ", ".join(t.get("artists") or [])
                lines.append("  !{} \u2014 {} \u2014 {} [{}]".format(
                    i, t["title"], artists_str, t.get("duration", "")
                ))
        lines += [
            "",
            "  !6 \u2014 \U0001f4bf Albums",
            "  !7 \u2014 \U0001f4c0 Singles",
            "  !cancel \u2014 Cancel",
            "",
            "\u23f0 This menu expires in 5 minutes.",
        ]

        try:
            await app.edit_message(object_guid, status_id, "\n".join(lines))
        except Exception:
            await app.send_message(object_guid, "\n".join(lines))

        timeout_task = asyncio.create_task(_expire_selection(object_guid))
        pending_selections[object_guid] = {
            "type":        "artist_menu",
            "artist_id":   artist_id,
            "artist_name": name,
            "top_tracks":  top_tracks,
            "choices":     top_tracks,  # indices 0-4 → top tracks
            "timeout_task": timeout_task,
        }

        # Send artist cover image if available
        if image_url:
            try:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name
                urllib.request.urlretrieve(image_url, tmp_path)
                await app.send_document(
                    object_guid, tmp_path,
                    caption="\U0001f3a4 {}".format(name)
                )
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            except Exception as exc:
                log.warning("artist image send failed: %s", exc)

        log.info("artist menu sent for %s (%s) | guid=%s", name, artist_id, object_guid)

    except Exception as exc:
        log.exception("_show_artist_menu error: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error fetching artist: {}".format(exc))
        except Exception:
            pass


async def _show_artist_album_list(
    object_guid: str,
    artist_id: str,
    artist_name: str,
    group: str,
    offset: int,
    log,
) -> None:
    """
    Show a paginated list of albums or singles for an artist.
    *group* is "album" or "single".
    """
    status = await app.send_message(
        object_guid,
        "\U0001f50d Fetching {} list\u2026".format("albums" if group == "album" else "singles")
    )
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()
        items, total = await loop.run_in_executor(
            None,
            lambda: _spodl.get_spotify_artist_albums(artist_id, group, offset, ARTIST_ALBUMS_PAGE_SIZE),
        )

        label_plural = "albums" if group == "album" else "singles"
        label_icon   = "\U0001f4bf" if group == "album" else "\U0001f4c0"
        lines = [
            "{} {} \u2014 {} ({}/{})".format(
                label_icon, artist_name, label_plural.capitalize(),
                min(offset + ARTIST_ALBUMS_PAGE_SIZE, total), total
            ),
            "",
        ]
        for i, alb in enumerate(items, 1):
            artists_str = ", ".join(alb.get("artists") or [])
            year = (alb.get("release_date") or "")[:4]
            lines.append("  !{} \u2014 {} \u2014 {} ({}) [{} tracks]".format(
                i, alb["name"], artists_str, year, alb.get("total_tracks", "?")
            ))

        has_next = (offset + ARTIST_ALBUMS_PAGE_SIZE) < total
        next_offset = offset + ARTIST_ALBUMS_PAGE_SIZE
        if has_next:
            lines.append("  !{} \u2014 \u27a1\ufe0f Next page".format(len(items) + 1))
        lines += ["  !cancel \u2014 Cancel", "", "\u23f0 This menu expires in 5 minutes."]

        try:
            await app.edit_message(object_guid, status_id, "\n".join(lines))
        except Exception:
            await app.send_message(object_guid, "\n".join(lines))

        timeout_task = asyncio.create_task(_expire_selection(object_guid))
        pending_selections[object_guid] = {
            "type":        "artist_list",
            "artist_id":   artist_id,
            "artist_name": artist_name,
            "group":       group,
            "offset":      offset,
            "items":       items,
            "total":       total,
            "has_next":    has_next,
            "next_offset": next_offset,
            "choices":     items,
            "timeout_task": timeout_task,
        }
        log.info("artist list sent (%s, group=%s, offset=%d/%d) | guid=%s",
                 artist_name, group, offset, total, object_guid)

    except Exception as exc:
        log.exception("_show_artist_album_list error: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error fetching list: {}".format(exc))
        except Exception:
            pass


async def _do_music_from_choice(object_guid: str, info: dict, choice: dict, log) -> None:
    """Download a single track using a specific platform choice and send it."""
    title       = info.get("title") or "Unknown"
    artists_str = ", ".join(info.get("artists") or [])

    if choice.get("source") == "auto":
        status_msg = "\u26a1 Auto-selecting best source for: {} \u2014 {}".format(title, artists_str)
    else:
        status_msg = "\U0001f4e5 Downloading [{}]: {} \u2014 {}".format(
            choice["label"], title, artists_str
        )

    status = await app.send_message(object_guid, status_msg)
    status_id = status.message_id

    try:
        fp = await _spodl.download_track_from_choice(info, choice, DOWNLOAD_DIR, _ytdlp_bin())

        try:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e4 Sending {} ({})\u2026".format(
                    fp.name, fp.suffix.lstrip(".").upper()
                ),
            )
        except Exception:
            pass

        caption = "{} \u2014 {}".format(title, artists_str) if artists_str else title
        try:
            await app.send_document(object_guid, str(fp), caption=caption)
            log.info("sent: %s", fp)
        except Exception as exc:
            log.error("send failed: %s", exc)
            _upload_retry.enqueue_failed_upload(
                app_send_document=app.send_document,
                object_guid=object_guid,
                file_path=fp,
                file_name=fp.name,
                caption=caption,
                provider="spotify_track",
                exc=exc,
            )
            await app.send_message(
                object_guid,
                "📦 Upload failed. Will retry automatically every hour. "
                "Send !uploads to see status, or !uploads cancel <id> to drop it.",
            )
            await app.edit_message(object_guid, status_id, "\u274c Upload failed \u2014 queued for retry.")
            return
        finally:
            if fp.exists():
                try:
                    fp.unlink()
                except Exception:
                    pass

        await app.edit_message(object_guid, status_id, "\u2705 Done!")

    except Exception as exc:
        log.exception("_do_music_from_choice error: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass


async def _run_music_choice_and_queue(
    object_guid: str, info: dict, choice: dict
) -> None:
    """Run one music-from-choice download then hand off to the next queue entry."""
    global is_downloading
    log = logging.getLogger("queue")
    try:
        await _do_music_from_choice(object_guid, info, choice, log)
    finally:
        if download_queue:
            next_entry = download_queue.popleft()
            await _notify_queue_positions()
            try:
                await app.edit_message(
                    next_entry["object_guid"], next_entry["queue_msg_id"],
                    "\U0001f7e2 You are first in the queue!\nStarting your download\u2026",
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            _dispatch_queue_entry(next_entry)
        else:
            is_downloading = False


def _dispatch_queue_entry(entry: dict) -> None:
    """Create the appropriate asyncio task for a queue entry."""
    if entry.get("music_choice") is not None:
        asyncio.create_task(
            _run_music_choice_and_queue(
                entry["object_guid"],
                entry["info"],
                entry["music_choice"],
            )
        )
    elif entry.get("choice") is None:
        # Old-style music platform entry
        asyncio.create_task(
            _run_music_and_queue(
                entry["object_guid"],
                entry["url"],
                entry.get("platform", "spotify"),
            )
        )
    else:
        asyncio.create_task(
            _run_download_and_queue(
                entry["object_guid"],
                entry["url"],
                entry["choice"],
                entry.get("title", ""),
            )
        )


async def _do_download(object_guid: str, url: str, choice: dict, title: str, log) -> None:
    """Run yt-dlp for the selected choice, stream progress, then send the file."""
    status = await app.send_message(object_guid, "\u23f3 Starting: {}\u2026".format(choice["label"]))
    status_id = status.message_id

    cmd = build_ytdlp_cmd_for_choice(url, choice)
    log.info("cmd: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log.info("yt-dlp pid=%s", proc.pid)

        downloaded_file = None
        last_update = 0.0
        out_ext = choice.get("out_ext", "mp4")

        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log.debug("yt-dlp | %s", line)

            # Capture file path printed by --print after_move:filepath
            if line and not line.startswith("[") and line.endswith(".{}".format(out_ext)):
                downloaded_file = line
                log.info("filepath: %s", downloaded_file)
                continue

            m = PROGRESS_RE.search(line)
            if not m:
                continue

            percent = float(m.group(1))
            speed = m.group(2)
            now = time.monotonic()

            if now - last_update >= UPDATE_INTERVAL:
                last_update = now
                bar = make_bar(percent)
                try:
                    await app.edit_message(
                        object_guid, status_id,
                        "\U0001f4e5 {}\n[{}] {:.1f}%\n\u26a1 {}".format(
                            choice["label"], bar, percent, speed
                        )
                    )
                except Exception as exc:
                    log.warning("edit failed: %s", exc)

        await proc.wait()
        log.info("returncode=%s", proc.returncode)

        if proc.returncode != 0:
            await app.edit_message(object_guid, status_id, "\u274c Download failed. Please try again.")
            return

        # Subtitle-only: find the newest .srt written by yt-dlp
        if choice.get("subtitle_only"):
            srts = list(DOWNLOAD_DIR.glob("*.srt"))
            if srts:
                downloaded_file = str(max(srts, key=lambda p: p.stat().st_mtime))

        # Fallback: glob for the expected file extension
        if not downloaded_file:
            candidates = list(DOWNLOAD_DIR.glob("*.{}".format(out_ext)))
            if candidates:
                downloaded_file = str(max(candidates, key=lambda p: p.stat().st_mtime))

        if not downloaded_file or not Path(downloaded_file).exists():
            await app.edit_message(object_guid, status_id, "\u274c Downloaded file not found.")
            return

        file_path = Path(downloaded_file)
        size_mb = file_path.stat().st_size / (1024 * 1024)
        log.info("sending: %s (%.2f MB)", downloaded_file, size_mb)

        await app.edit_message(object_guid, status_id, "\u2705 Download complete. Sending\u2026")
        caption = "{}\n{}".format(title, url) if title else url
        try:
            await app.send_document(object_guid, downloaded_file, caption=caption)
            log.info("sent successfully")
        except Exception as exc:
            log.error("upload failed: %s", exc)
            _upload_retry.enqueue_failed_upload(
                app_send_document=app.send_document,
                object_guid=object_guid,
                file_path=file_path,
                file_name=file_path.name,
                caption=caption,
                provider="youtube",
                exc=exc,
            )
            await app.send_message(
                object_guid,
                "📦 Upload failed. Will retry automatically every hour. "
                "Send !uploads to see status, or !uploads cancel <id> to drop it.",
            )
            await app.edit_message(object_guid, status_id, "\u274c Upload failed — queued for retry.")
            return

        try:
            file_path.unlink()
            log.info("removed: %s", downloaded_file)
        except Exception as exc:
            log.warning("cleanup failed: %s", exc)

        await app.edit_message(object_guid, status_id, "\u2705 Done! File sent.")

    except Exception as exc:
        log.exception("error in _do_download: %s", exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass



@app.on_message_updates(filters.commands("admin", prefixes="!"))
async def admin_handler(update):
    """
    Admin command handler.  Only accounts listed in ADMIN_GUIDS may use this.

    Sub-commands
    ------------
    !admin whitelist add <guid>    – add a user to the whitelist
    !admin whitelist remove <guid> – remove a user from the whitelist
    !admin whitelist on            – enable whitelist mode (only listed users can use bot)
    !admin whitelist off           – disable whitelist mode (open to everyone)
    !admin ban <guid>              – ban a user
    !admin unban <guid>            – unban a user
    !admin logs [N]                – show last N (default 20) usage log entries
    !admin status                  – show current settings summary
    !admin clearcache [lru|isrc|all] – flush in-memory and/or disk caches
    !admin breakers                – show provider circuit-breaker states
    """
    object_guid = update.object_guid
    log = logging.getLogger("admin")

    if not _is_admin(object_guid):
        await app.send_message(object_guid, "🚫 You do not have admin privileges.")
        return

    parts = update.command  # ['admin', 'sub', ...]
    if len(parts) < 2:
        await app.send_message(
            object_guid,
            "⚙️ Admin commands:\n"
            "  !admin whitelist add <guid>\n"
            "  !admin whitelist remove <guid>\n"
            "  !admin whitelist on\n"
            "  !admin whitelist off\n"
            "  !admin ban <guid>\n"
            "  !admin unban <guid>\n"
            "  !admin logs [N]\n"
            "  !admin status\n"
            "  !admin clearcache [lru|isrc|all]\n"
            "  !admin breakers\n"
            "  !admin health"
        )
        return

    sub = parts[1].lower()

    # ------------------------------------------------------------------
    # whitelist sub-commands
    # ------------------------------------------------------------------
    if sub == "whitelist":
        if len(parts) < 3:
            await app.send_message(
                object_guid,
                "Usage: !admin whitelist add|remove|on|off [<guid>]"
            )
            return
        action = parts[2].lower()

        if action in ("on", "off"):
            _state["whitelist_enabled"] = (action == "on")
            _save_state(_state)
            status_word = "enabled" if _state["whitelist_enabled"] else "disabled"
            log.info("whitelist %s by %s", status_word, object_guid)
            await app.send_message(
                object_guid,
                "✅ Whitelist mode {}.".format(status_word)
            )
            return

        if action in ("add", "remove"):
            if len(parts) < 4:
                await app.send_message(
                    object_guid,
                    "Usage: !admin whitelist {} <guid>".format(action)
                )
                return
            target = parts[3].strip()
            wl = _state["whitelist"]
            if action == "add":
                if target not in wl:
                    wl.append(target)
                    _save_state(_state)
                    log.info("whitelist add %s by %s", target, object_guid)
                await app.send_message(
                    object_guid,
                    "✅ {} added to whitelist.".format(target)
                )
            else:  # remove
                if target in wl:
                    wl.remove(target)
                    _save_state(_state)
                    log.info("whitelist remove %s by %s", target, object_guid)
                    await app.send_message(
                        object_guid,
                        "✅ {} removed from whitelist.".format(target)
                    )
                else:
                    await app.send_message(
                        object_guid,
                        "ℹ️ {} was not in the whitelist.".format(target)
                    )
            return

        await app.send_message(
            object_guid,
            "❌ Unknown whitelist action. Use add / remove / on / off."
        )
        return

    # ------------------------------------------------------------------
    # ban / unban
    # ------------------------------------------------------------------
    if sub in ("ban", "unban"):
        if len(parts) < 3:
            await app.send_message(
                object_guid,
                "Usage: !admin {} <guid>".format(sub)
            )
            return
        target = parts[2].strip()
        banned = _state["banned"]
        if sub == "ban":
            if target not in banned:
                banned.append(target)
                _save_state(_state)
                log.info("banned %s by %s", target, object_guid)
            await app.send_message(
                object_guid,
                "🚫 {} has been banned.".format(target)
            )
        else:  # unban
            if target in banned:
                banned.remove(target)
                _save_state(_state)
                log.info("unbanned %s by %s", target, object_guid)
                await app.send_message(
                    object_guid,
                    "✅ {} has been unbanned.".format(target)
                )
            else:
                await app.send_message(
                    object_guid,
                    "ℹ️ {} was not banned.".format(target)
                )
        return

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------
    if sub == "logs":
        try:
            n = int(parts[2]) if len(parts) >= 3 else 20
        except ValueError:
            n = 20
        n = max(1, min(n, 100))
        entries = _state["logs"][-n:]
        if not entries:
            await app.send_message(object_guid, "ℹ️ No usage logs yet.")
            return
        lines = ["📋 Last {} usage log entries:".format(len(entries))]
        for e in reversed(entries):
            lines.append(
                "[{}] {} | {} | {}".format(
                    e.get("time", "?"),
                    e.get("guid", "?"),
                    e.get("action", "?"),
                    e.get("detail", ""),
                )
            )
        # Split into chunks of ≤4000 chars to avoid message size limits
        chunk, chunks = [], []
        for line in lines:
            if sum(len(l) + 1 for l in chunk) + len(line) > 3800:
                chunks.append("\n".join(chunk))
                chunk = []
            chunk.append(line)
        if chunk:
            chunks.append("\n".join(chunk))
        for c in chunks:
            await app.send_message(object_guid, c)
        return

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    if sub == "status":
        wl_mode = "ON" if _state["whitelist_enabled"] else "OFF"
        wl_count = len(_state["whitelist"])
        ban_count = len(_state["banned"])
        log_count = len(_state["logs"])
        admin_count = len(ADMIN_GUIDS)
        await app.send_message(
            object_guid,
            "⚙️ Bot status:\n"
            "  Admins configured : {}\n"
            "  Whitelist mode    : {}\n"
            "  Whitelisted users : {}\n"
            "  Banned users      : {}\n"
            "  Log entries       : {}".format(
                admin_count, wl_mode, wl_count, ban_count, log_count
            )
        )
        return

    # ------------------------------------------------------------------
    # clearcache
    # ------------------------------------------------------------------
    if sub == "clearcache":
        target = (parts[2].lower() if len(parts) >= 3 else "all")
        cleared = []

        if target in ("lru", "all"):
            n_lru = _spodl.clear_track_info_cache()
            cleared.append("track-info LRU ({} entries)".format(n_lru))

        if target in ("isrc", "all"):
            try:
                isrc_path = _spodl._isrc_cache_path()
                n_isrc = 0
                if isrc_path.exists():
                    try:
                        n_isrc = len(json.loads(isrc_path.read_text()))
                    except Exception:
                        pass
                    isrc_path.write_text("{}")
                    log.info("ISRC disk cache truncated (%d entries) by %s", n_isrc, object_guid)
                cleared.append("ISRC disk cache ({} entries)".format(n_isrc))
            except Exception as exc:
                cleared.append("ISRC disk cache (error: {})".format(exc))

        if not cleared:
            await app.send_message(
                object_guid,
                "❌ Unknown target. Use: !admin clearcache [lru|isrc|all]"
            )
            return

        await app.send_message(
            object_guid,
            "🗑️ Cleared: {}".format(", ".join(cleared))
        )
        return

    # ------------------------------------------------------------------
    # breakers
    # ------------------------------------------------------------------
    if sub == "breakers":
        states = _spodl.get_breaker_states()
        if not states:
            await app.send_message(object_guid, "ℹ️ No circuit-breaker data yet.")
            return
        lines = ["⚡ Circuit-breaker states:"]
        for cb in states:
            state = cb["state"]
            icon = {"closed": "🟢", "open": "🔴", "half_open": "🟡"}.get(state, "⚪")
            line = "{} {} — {}".format(icon, cb["key"], state)
            if state == "open":
                line += " (closes in {:.0f}s)".format(cb["seconds_until_close"])
            if cb["consecutive_failures"]:
                line += " | {} consecutive failures".format(cb["consecutive_failures"])
            if cb["last_reason"]:
                line += " | last: {}".format(cb["last_reason"])
            lines.append(line)
        await app.send_message(object_guid, "\n".join(lines))
        return

    # ------------------------------------------------------------------
    # health — D1: ping each provider endpoint
    # ------------------------------------------------------------------
    if sub == "health":
        import concurrent.futures as _cf
        endpoints = {
            "Qobuz API":    "https://www.qobuz.com",
            "Deezer API":   "https://api.deezer.com/track/3135556",
            "Tidal API":    "https://api.tidal.com",
            "lrclib":       "https://lrclib.net/api/search?q=test",
            "MusicBrainz":  "https://musicbrainz.org/ws/2/",
            "Odesli":       "https://odesli.co",
            "YouTube Music":"https://music.youtube.com",
        }
        _HEALTH_TIMEOUT = 5

        def _ping(name_url):
            name, url = name_url
            try:
                import requests as _req
                t0 = time.time()
                r = _req.head(url, timeout=_HEALTH_TIMEOUT, allow_redirects=True)
                elapsed = time.time() - t0
                status = "🟢 up" if r.status_code < 500 else "🔴 down"
                if elapsed > 2:
                    status = "🟡 slow"
                return f"{status} {name} ({elapsed*1000:.0f}ms)"
            except Exception as exc:
                return f"🔴 down {name} ({exc})"

        status_msg = await app.send_message(object_guid, "⏳ Pinging all endpoints…")
        with _cf.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_ping, endpoints.items()))
        await app.edit_message(
            object_guid, status_msg.message_id,
            "🩺 Health check:\n" + "\n".join(results)
        )
        return

    await app.send_message(
        object_guid,
        "❌ Unknown admin sub-command: {}".format(sub)
    )


# ---------------------------------------------------------------------------
# Generic music downloading  (SpotiFLAC approach via spotify_dl.py)
# ---------------------------------------------------------------------------

_PLATFORM_NAMES = {
    "spotify": "Spotify",
    "tidal":   "Tidal",
    "qobuz":   "Qobuz",
    "amazon":  "Amazon Music",
}


def _send_quality_menu(object_guid: str, url: str, platform: str,
                       url_type: str, extra: dict) -> None:
    """
    Store a 'music_quality' pending entry for *object_guid* so the user can
    choose MP3 / FLAC CD / FLAC Hi-Res.
    """
    timeout_task = asyncio.create_task(_expire_selection(object_guid))
    pending_selections[object_guid] = {
        "type":         "music_quality",
        "url":          url,
        "platform":     platform,
        "url_type":     url_type,
        "extra":        extra,
        "choices":      list(_spodl.QUALITY_MENU),
        "timeout_task": timeout_task,
    }


async def _ask_quality(object_guid: str, header: str,
                       url: str, platform: str, url_type: str, extra: dict) -> None:
    """Send a quality-selection menu to the user."""
    lines = [header, ""]
    lines.append("Choose audio quality:")
    for i, q in enumerate(_spodl.QUALITY_MENU, 1):
        lines.append("  !{} \u2014 {}".format(i, q["label"]))
    lines += ["  !cancel \u2014 Cancel", "", "\u23f0 This menu expires in 5 minutes."]
    await app.send_message(object_guid, "\n".join(lines))
    _send_quality_menu(object_guid, url, platform, url_type, extra)


# Keep the old _do_music_download for backward-compat (used by old queue entries)
async def _do_music_download(object_guid: str, url: str, platform: str, log) -> None:
    """Resolve metadata for *url* from *platform*, pick the best download source, send file."""
    pname = _PLATFORM_NAMES.get(platform, platform.capitalize())
    status = await app.send_message(object_guid, "\U0001f50d Looking up {} track\u2026".format(pname))
    status_id = status.message_id

    try:
        loop = asyncio.get_event_loop()

        if platform == "spotify":
            track_id = _spodl.parse_spotify_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Spotify track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_track_info, track_id)

        elif platform == "tidal":
            track_id = _spodl.parse_tidal_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Tidal track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_tidal_track_info, track_id)

        elif platform == "qobuz":
            track_id = _spodl.parse_qobuz_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Qobuz track ID from: {}".format(url))
                return
            info = await loop.run_in_executor(None, _spodl.get_qobuz_track_info, track_id)

        elif platform == "amazon":
            track_id = _spodl.parse_amazon_track_id(url)
            if not track_id:
                await app.edit_message(object_guid, status_id,
                                        "\u274c Could not parse Amazon Music track ID from: {}".format(url))
                return
            ytbin = _ytdlp_bin()
            info = await loop.run_in_executor(None, _spodl.get_amazon_track_info, track_id, ytbin)

        else:
            await app.edit_message(object_guid, status_id, "\u274c Unknown platform: {}".format(platform))
            return

        title       = info.get("title") or "Unknown"
        artists_str = ", ".join(info.get("artists") or [])
        source_label = _spodl.best_source_label(info)

        try:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e5 Downloading [{}]: {} \u2014 {}".format(source_label, title, artists_str),
            )
        except Exception:
            pass

        fp = await _spodl.download_track(info, DOWNLOAD_DIR, _ytdlp_bin())

        try:
            await app.edit_message(
                object_guid, status_id,
                "\U0001f4e4 Sending {} ({})…".format(fp.name, fp.suffix.lstrip(".").upper()),
            )
        except Exception:
            pass

        caption = "{} — {}".format(title, artists_str) if artists_str else title
        try:
            await app.send_document(object_guid, str(fp), caption=caption)
            log.info("sent: %s", fp)
        except Exception as exc:
            log.error("send failed: %s", exc)
            _upload_retry.enqueue_failed_upload(
                app_send_document=app.send_document,
                object_guid=object_guid,
                file_path=fp,
                file_name=fp.name,
                caption=caption,
                provider=platform,
                exc=exc,
            )
            await app.send_message(
                object_guid,
                "📦 Upload failed. Will retry automatically every hour. "
                "Send !uploads to see status, or !uploads cancel <id> to drop it.",
            )
            await app.edit_message(object_guid, status_id, "\u274c Upload failed \u2014 queued for retry.")
            return
        finally:
            if fp.exists():
                try:
                    fp.unlink()
                except Exception:
                    pass

        await app.edit_message(object_guid, status_id, "\u2705 Done!")

    except Exception as exc:
        log.exception("_do_music_download (%s) error: %s", platform, exc)
        try:
            await app.edit_message(object_guid, status_id, "\u274c Error: {}".format(exc))
        except Exception:
            pass


# Keep the old name as an alias so queue code can call it unchanged
async def _do_spotify_download(object_guid: str, url: str, log) -> None:
    await _do_music_download(object_guid, url, "spotify", log)


@app.on_message_updates(filters.commands("spotify", prefixes="!"))
async def spotify_handler(update):
    log = logging.getLogger("spotify")
    object_guid = update.object_guid
    log.info("!spotify | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    # D3: rate limiting (single tracks only — batch goes through _do_batch_download)
    if _HAS_RATE_LIMITER and not _is_admin(object_guid):
        ok, rl_msg = check_rate_limit(object_guid)
        if not ok:
            await app.send_message(object_guid, rl_msg)
            return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    args = args.strip()
    if not args:
        await app.send_message(
            object_guid,
            "\u274c Please provide a Spotify track, album, or playlist link.\n"
            "Example: !spotify https://open.spotify.com/track/<id>"
        )
        return

    # Detect URL type: artist > playlist > album > track
    artist_id = _spodl.parse_spotify_artist_id(args)
    pl_id  = _spodl.parse_spotify_playlist_id(args)
    alb_id = _spodl.parse_spotify_album_id(args)
    tr_id  = _spodl.parse_spotify_track_id(args)

    if artist_id:
        _append_log(object_guid, "spotify_artist", args)
        # Cancel any existing pending selection
        old = pending_selections.pop(object_guid, None)
        if old and old.get("timeout_task"):
            old["timeout_task"].cancel()
        asyncio.create_task(_show_artist_menu(object_guid, artist_id, log))
        return

    if pl_id:
        _append_log(object_guid, "spotify_playlist", args)
        await app.send_message(object_guid, "\U0001f50d Fetching playlist info\u2026")
        try:
            loop = asyncio.get_event_loop()
            pl_info, track_ids = await loop.run_in_executor(
                None, _spodl.get_spotify_playlist_tracks, pl_id
            )
            collection_name = pl_info.get("name", "playlist")
            header = "\U0001f4c2 Playlist: {} ({} tracks)".format(collection_name, len(track_ids))
            await _ask_quality(object_guid, header, args, "spotify", "playlist", {
                "collection_name": collection_name,
                "track_ids":       track_ids,
            })
        except Exception as exc:
            log.error("playlist fetch failed: %s", exc)
            await app.send_message(object_guid, "\u274c Could not fetch playlist: {}".format(exc))
        return

    if alb_id:
        _append_log(object_guid, "spotify_album", args)
        await app.send_message(object_guid, "\U0001f50d Fetching album info\u2026")
        try:
            loop = asyncio.get_event_loop()
            alb_info, track_ids = await loop.run_in_executor(
                None, _spodl.get_spotify_album_tracks, alb_id
            )
            collection_name = "{} \u2014 {}".format(
                alb_info.get("name", "album"),
                ", ".join(alb_info.get("artists") or []),
            )
            header = "\U0001f4bf Album: {} ({} tracks)".format(collection_name, len(track_ids))
            await _ask_quality(object_guid, header, args, "spotify", "album", {
                "collection_name": collection_name,
                "track_ids":       track_ids,
            })
        except Exception as exc:
            log.error("album fetch failed: %s", exc)
            await app.send_message(object_guid, "\u274c Could not fetch album: {}".format(exc))
        return

    if tr_id:
        _append_log(object_guid, "spotify_track", args)
        await _ask_quality(
            object_guid, "\U0001f3b5 Spotify track", args, "spotify", "track", {}
        )
        return

    await app.send_message(
        object_guid,
        "\u274c Please provide a valid Spotify track, album, playlist, or artist link.\n"
        "Accepted formats:\n"
        "  https://open.spotify.com/track/<id>\n"
        "  https://open.spotify.com/album/<id>\n"
        "  https://open.spotify.com/playlist/<id>\n"
        "  https://open.spotify.com/artist/<id>"
    )


@app.on_message_updates(filters.commands("tidal", prefixes="!"))
async def tidal_handler(update):
    log = logging.getLogger("tidal")
    object_guid = update.object_guid
    log.info("!tidal | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = TIDAL_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Tidal track link.\n"
            "Example: https://tidal.com/browse/track/12345678"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "tidal_track", url)
    await _ask_quality(object_guid, "\U0001f3b5 Tidal track", url, "tidal", "track", {})


@app.on_message_updates(filters.commands("qobuz", prefixes="!"))
async def qobuz_handler(update):
    log = logging.getLogger("qobuz")
    object_guid = update.object_guid
    log.info("!qobuz | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = QOBUZ_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Qobuz track link.\n"
            "Example: https://open.qobuz.com/track/12345678"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "qobuz_track", url)
    await _ask_quality(object_guid, "\U0001f3b5 Qobuz track", url, "qobuz", "track", {})


@app.on_message_updates(filters.commands("amazon", prefixes="!"))
async def amazon_handler(update):
    log = logging.getLogger("amazon")
    object_guid = update.object_guid
    log.info("!amazon | guid=%s", object_guid)

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    m = AMAZON_RE.search(args)
    if not m:
        await app.send_message(
            object_guid,
            "\u274c Please provide a valid Amazon Music track link.\n"
            "Example: https://music.amazon.com/tracks/B01N2XLBTT"
        )
        return

    url = m.group(0)
    _append_log(object_guid, "amazon_track", url)
    await _ask_quality(object_guid, "\U0001f3b5 Amazon Music track", url, "amazon", "track", {})


async def _enqueue_music(object_guid: str, url: str, platform: str, log) -> None:
    """Add a music download (old-style, auto-pick source) to the queue."""
    global is_downloading
    if not is_downloading:
        is_downloading = True
        asyncio.create_task(_run_music_and_queue(object_guid, url, platform))
    else:
        pos = len(download_queue) + 1
        people = "person" if pos == 1 else "people"
        queue_msg = await app.send_message(
            object_guid,
            "\u23f3 The bot is busy right now.\n"
            "There {} {} {} ahead of you.".format(
                "is" if pos == 1 else "are", pos, people
            ),
        )
        download_queue.append({
            "object_guid":  object_guid,
            "url":          url,
            "choice":       None,
            "platform":     platform,
            "title":        url,
            "queue_msg_id": queue_msg.message_id,
        })
        log.info("%s queued at position %d | guid=%s", platform, pos, object_guid)


async def _run_music_and_queue(object_guid: str, url: str, platform: str) -> None:
    """Run one music download (old-style) then hand off to the next queue entry."""
    global is_downloading
    log = logging.getLogger("queue")
    try:
        await _do_music_download(object_guid, url, platform, log)
    finally:
        if download_queue:
            next_entry = download_queue.popleft()
            await _notify_queue_positions()
            try:
                await app.edit_message(
                    next_entry["object_guid"],
                    next_entry["queue_msg_id"],
                    "\U0001f7e2 You are the first person in the queue!\n"
                    "Starting your download\u2026",
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            _dispatch_queue_entry(next_entry)
        else:
            is_downloading = False


# ---------------------------------------------------------------------------
# B1: !search <query> — Spotify search, show top 10 results as numbered menu
# ---------------------------------------------------------------------------
@app.on_message_updates(filters.commands("search", prefixes="!"))
async def search_handler(update):
    log = logging.getLogger("search")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    query = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    query = query.strip()
    if not query:
        await app.send_message(
            object_guid,
            "❌ Please provide a search query.\nExample: !search bohemian rhapsody queen"
        )
        return

    _append_log(object_guid, "search", query)
    status = await app.send_message(object_guid, f"🔍 Searching Spotify for: {query}…")

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, _spodl.spotify_search, query, 10
        )
    except AttributeError:
        # Fallback if spotify_search not yet exported
        await app.edit_message(
            object_guid, status.message_id,
            "❌ Spotify search is not available. Try a direct link instead."
        )
        return
    except Exception as exc:
        log.error("search failed: %s", exc)
        await app.edit_message(object_guid, status.message_id, f"❌ Search failed: {exc}")
        return

    if not results:
        await app.edit_message(object_guid, status.message_id, "❌ No results found.")
        return

    lines = [f"🔍 Spotify results for: {query}\n"]
    choices = []
    for i, track in enumerate(results[:10], 1):
        title = track.get("title", "Unknown")
        artist = ", ".join(track.get("artists", ["Unknown"]))
        album = track.get("album", "")
        duration = track.get("duration", "")
        url = track.get("url") or track.get("spotify_url") or f"https://open.spotify.com/track/{track.get('track_id','')}"
        lines.append(f"  !{i} — {artist} — {title}" + (f" [{duration}]" if duration else ""))
        choices.append({"label": f"{artist} — {title}", "url": url, "type": "spotify_track"})

    lines.append("\n  !cancel — Cancel\n⏰ Expires in 5 minutes.")

    await app.edit_message(object_guid, status.message_id, "\n".join(lines))

    # Cancel any existing pending selection
    old = pending_selections.pop(object_guid, None)
    if old and old.get("timeout_task"):
        old["timeout_task"].cancel()

    timeout_task = asyncio.create_task(_expire_selection(object_guid))
    pending_selections[object_guid] = {
        "type": "search_result",
        "choices": choices,
        "timeout_task": timeout_task,
    }
    log.info("search menu sent | %d results | guid=%s", len(choices), object_guid)


# ---------------------------------------------------------------------------
# B3: !queue — Show queue position and ETA
# ---------------------------------------------------------------------------
@app.on_message_updates(filters.commands("queue", prefixes="!"))
async def queue_handler(update):
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    queue_list = list(download_queue)
    total = len(queue_list)

    # Find user's position(s) in queue
    user_positions = [i + 1 for i, e in enumerate(queue_list) if e.get("object_guid") == object_guid]

    if is_downloading:
        current_user = queue_list[0]["object_guid"] if queue_list else "someone"
        busy_note = "⏳ A download is currently in progress."
    else:
        busy_note = "✅ The bot is idle."

    if not user_positions:
        await app.send_message(
            object_guid,
            f"{busy_note}\n"
            f"📋 Queue depth: {total} item{'s' if total != 1 else ''}\n"
            "You are not currently in the queue."
        )
        return

    pos = user_positions[0]
    ahead = pos - 1
    await app.send_message(
        object_guid,
        f"{busy_note}\n"
        f"📋 Your position: #{pos} ({ahead} item{'s' if ahead != 1 else ''} ahead)\n"
        f"Total queue depth: {total}"
    )


# ---------------------------------------------------------------------------
# C1: !soundcloud <url> — Download a SoundCloud track
# ---------------------------------------------------------------------------
@app.on_message_updates(filters.commands("soundcloud", prefixes="!"))
async def soundcloud_handler(update):
    log = logging.getLogger("soundcloud")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    # D3: rate limiting
    if _HAS_RATE_LIMITER:
        ok, msg = check_rate_limit(object_guid)
        if not ok:
            await app.send_message(object_guid, msg)
            return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    url = SOUNDCLOUD_RE.search(args)
    if not url:
        await app.send_message(
            object_guid,
            "❌ Please provide a valid SoundCloud track URL.\n"
            "Example: !soundcloud https://soundcloud.com/artist/track"
        )
        return

    sc_url = url.group(0)
    _append_log(object_guid, "soundcloud", sc_url)

    if not _HAS_SOUNDCLOUD:
        await app.send_message(
            object_guid, "❌ SoundCloud provider not available. Install rubetunes package."
        )
        return

    status = await app.send_message(object_guid, "⬇️ Downloading from SoundCloud…")

    try:
        ytdlp = str(_ytdlp_bin())
        safe = _spodl._safe_filename(sc_url.split("/")[-1]) if hasattr(_spodl, "_safe_filename") else "soundcloud_track"

        import tempfile as _tmp
        with _tmp.TemporaryDirectory() as tmpdir:
            fp = await download_soundcloud(sc_url, Path(tmpdir), ytdlp, safe)
            if _HAS_RATE_LIMITER:
                record_usage(object_guid)
            await app.send_message(object_guid, "📤 Uploading…")
            try:
                with fp.open("rb") as f:
                    await app.send_document(object_guid, file=f, file_name=fp.name)
                await app.delete_messages(object_guid, [status.message_id])
            except Exception as upload_exc:
                log.error("SoundCloud upload failed: %s", upload_exc)
                _upload_retry.enqueue_failed_upload(
                    app_send_document=app.send_document,
                    object_guid=object_guid,
                    file_path=fp,
                    file_name=fp.name,
                    caption="",
                    provider="soundcloud",
                    exc=upload_exc,
                )
                await app.edit_message(
                    object_guid, status.message_id,
                    "📦 Upload failed. Will retry automatically every hour. "
                    "Send !uploads to see status, or !uploads cancel <id> to drop it.",
                )
    except Exception as exc:
        log.error("SoundCloud download failed: %s", exc)
        await app.edit_message(
            object_guid, status.message_id, f"❌ SoundCloud download failed: {exc}"
        )


# ---------------------------------------------------------------------------
# C2: !bandcamp <url> — Download a Bandcamp track
# ---------------------------------------------------------------------------
@app.on_message_updates(filters.commands("bandcamp", prefixes="!"))
async def bandcamp_handler(update):
    log = logging.getLogger("bandcamp")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    # D3: rate limiting
    if _HAS_RATE_LIMITER:
        ok, msg = check_rate_limit(object_guid)
        if not ok:
            await app.send_message(object_guid, msg)
            return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    url = BANDCAMP_RE.search(args)
    if not url:
        await app.send_message(
            object_guid,
            "❌ Please provide a valid Bandcamp track/album URL.\n"
            "Example: !bandcamp https://artist.bandcamp.com/track/song-name"
        )
        return

    bc_url = url.group(0)
    _append_log(object_guid, "bandcamp", bc_url)

    if not _HAS_BANDCAMP:
        await app.send_message(
            object_guid, "❌ Bandcamp provider not available. Install rubetunes package."
        )
        return

    status = await app.send_message(object_guid, "⬇️ Downloading from Bandcamp…")

    try:
        ytdlp = str(_ytdlp_bin())
        safe = _spodl._safe_filename(bc_url.split("/")[-1]) if hasattr(_spodl, "_safe_filename") else "bandcamp_track"

        import tempfile as _tmp
        with _tmp.TemporaryDirectory() as tmpdir:
            fp = await download_bandcamp(bc_url, Path(tmpdir), ytdlp, safe)
            if _HAS_RATE_LIMITER:
                record_usage(object_guid)
            await app.send_message(object_guid, "📤 Uploading…")
            try:
                with fp.open("rb") as f:
                    await app.send_document(object_guid, file=f, file_name=fp.name)
                await app.delete_messages(object_guid, [status.message_id])
            except Exception as upload_exc:
                log.error("Bandcamp upload failed: %s", upload_exc)
                _upload_retry.enqueue_failed_upload(
                    app_send_document=app.send_document,
                    object_guid=object_guid,
                    file_path=fp,
                    file_name=fp.name,
                    caption="",
                    provider="bandcamp",
                    exc=upload_exc,
                )
                await app.edit_message(
                    object_guid, status.message_id,
                    "📦 Upload failed. Will retry automatically every hour. "
                    "Send !uploads to see status, or !uploads cancel <id> to drop it.",
                )
    except Exception as exc:
        log.error("Bandcamp download failed: %s", exc)
        await app.edit_message(
            object_guid, status.message_id, f"❌ Bandcamp download failed: {exc}"
        )


# ---------------------------------------------------------------------------
# musicdl commands
# ---------------------------------------------------------------------------

# Pending musicdl search selections: {object_guid: {"tracks": [...], "timeout_task": ...}}
_musicdl_selections: dict = {}


@app.on_message_updates(filters.commands("musicdl", prefixes="!"))
async def musicdl_handler(update):
    """
    Multi-source music downloader via musicdl.

    Usage:
      !musicdl sources              — list all available musicdl sources
      !musicdl search <query>       — search for tracks
      !musicdl search <query> [src] — search a specific source (e.g. NeteaseMusicClient)
    After a search, reply with a number to download that track.
    """
    log = logging.getLogger("musicdl")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = update.command[1:] if update.command and len(update.command) > 1 else []
    subcmd = args[0].lower() if args else ""

    if not subcmd or subcmd == "help":
        await app.send_message(
            object_guid,
            "🎵 musicdl — multi-source music downloader\n\n"
            "Commands:\n"
            "  !musicdl sources             — list available sources\n"
            "  !musicdl search <query>      — search across default sources\n"
            "  !musicdl search <query> [src]— search a specific source\n\n"
            "After !musicdl search, reply with a number to download.\n"
            "Sources: Netease, QQ, Kuwo, Migu, Kugou, Deezer, Qobuz, Tidal, Spotify, …"
        )
        return

    if subcmd == "sources":
        await _musicdl_sources(object_guid, log)
        return

    if subcmd == "search":
        query_parts = args[1:]
        if not query_parts:
            await app.send_message(object_guid, "❌ Usage: !musicdl search <query>")
            return
        # Last token may be a source name (ends with MusicClient)
        sources = None
        if query_parts and query_parts[-1].endswith("MusicClient"):
            sources = [query_parts[-1]]
            query_parts = query_parts[:-1]
        query = " ".join(query_parts)
        if not query:
            await app.send_message(object_guid, "❌ Usage: !musicdl search <query>")
            return
        await _musicdl_search(object_guid, query, sources, log)
        return

    # Otherwise try to parse as a number (selection after a search)
    try:
        choice = int(subcmd)
    except ValueError:
        await app.send_message(
            object_guid,
            "❌ Unknown musicdl subcommand. Try !musicdl help"
        )
        return
    await _musicdl_pick(object_guid, choice, log)


async def _musicdl_sources(object_guid: str, log) -> None:
    """Handle !musicdl sources."""
    if not _HAS_MUSICDL:
        await app.send_message(
            object_guid,
            "❌ musicdl is not installed. Install it with: pip install musicdl==2.11.1"
        )
        return
    try:
        client = MusicdlClient()
        sources = client.list_sources()
        lines = ["🎵 musicdl registered sources ({} total):".format(len(sources)), ""]
        lines.extend("  • {}".format(s) for s in sources)
        await app.send_message(object_guid, "\n".join(lines))
    except Exception as exc:
        log.error("musicdl sources error: %s", exc)
        await app.send_message(object_guid, f"❌ Could not list musicdl sources: {exc}")


async def _musicdl_search(
    object_guid: str, query: str, sources: list | None, log
) -> None:
    """Handle !musicdl search."""
    if not _HAS_MUSICDL:
        await app.send_message(
            object_guid,
            "❌ musicdl is not installed. Install it with: pip install musicdl==2.11.1"
        )
        return

    # Cancel any previous pending selection
    old = _musicdl_selections.pop(object_guid, None)
    if old and old.get("timeout_task"):
        old["timeout_task"].cancel()

    status = await app.send_message(object_guid, f"🔍 Searching musicdl for: {query!r}…")
    status_id = status.message_id

    try:
        client = MusicdlClient(sources=sources)
        result = await client.search(query, sources=sources, limit=10)
    except Exception as exc:
        log.error("musicdl search error: %s", exc)
        await app.edit_message(object_guid, status_id, f"❌ musicdl search failed: {exc}")
        return

    if not result.tracks:
        await app.edit_message(
            object_guid, status_id,
            f"🔍 No results found for {query!r} on musicdl."
        )
        return

    lines = [f"🎵 musicdl results for {query!r} ({result.total} tracks):"]
    for i, track in enumerate(result.tracks[:15], 1):
        src_short = track.source.replace("MusicClient", "")
        ext_hint = f" [{track.ext.upper()}]" if track.ext else ""
        lines.append(
            f"  {i}. {track.display_title} — {track.album} [{src_short}{ext_hint}]"
        )
    lines += ["", "Reply !musicdl <number> to download, or !musicdl search <new query>"]

    await app.edit_message(object_guid, status_id, "\n".join(lines))

    async def _expire():
        await asyncio.sleep(300)
        _musicdl_selections.pop(object_guid, None)

    timeout_task = asyncio.create_task(_expire())
    _musicdl_selections[object_guid] = {
        "tracks": result.tracks[:15],
        "timeout_task": timeout_task,
    }
    log.info("musicdl search done | %d results | guid=%s", result.total, object_guid)


async def _musicdl_pick(object_guid: str, choice: int, log) -> None:
    """Download the track chosen after !musicdl search."""
    pending = _musicdl_selections.get(object_guid)
    if not pending:
        await app.send_message(
            object_guid,
            "❌ No active musicdl search. Use !musicdl search <query> first."
        )
        return

    tracks = pending.get("tracks", [])
    if choice < 1 or choice > len(tracks):
        await app.send_message(
            object_guid,
            f"❌ Please enter a number between 1 and {len(tracks)}."
        )
        return

    track = tracks[choice - 1]
    # Remove selection so a second press doesn't trigger a second download
    _musicdl_selections.pop(object_guid, None)
    if pending.get("timeout_task"):
        pending["timeout_task"].cancel()

    # Rate limit
    if _HAS_RATE_LIMITER:
        ok, msg = check_rate_limit(object_guid)
        if not ok:
            await app.send_message(object_guid, f"🚦 {msg}")
            return

    status = await app.send_message(
        object_guid,
        f"⬇️ Downloading: {track.display_title} via {track.source.replace('MusicClient', '')}…"
    )
    status_id = status.message_id

    try:
        client = MusicdlClient()
        dl_result = await client.download(track)
    except Exception as exc:
        log.error("musicdl download error: %s", exc)
        await app.edit_message(object_guid, status_id, f"❌ musicdl download failed: {exc}")
        return

    if not dl_result.success or not dl_result.file_path.exists():
        await app.edit_message(
            object_guid, status_id,
            f"❌ musicdl download failed: {dl_result.error or 'file not found'}"
        )
        return

    if _HAS_RATE_LIMITER:
        record_usage(object_guid)

    fp = dl_result.file_path
    log.info("musicdl download ok | file=%s | guid=%s", fp, object_guid)
    await app.edit_message(object_guid, status_id, "📤 Uploading…")
    file_moved_to_retry = False
    try:
        try:
            with fp.open("rb") as f:
                await app.send_document(object_guid, file=f, file_name=fp.name)
            await app.delete_messages(object_guid, [status_id])
        except Exception as exc:
            log.error("musicdl upload failed: %s", exc)
            _upload_retry.enqueue_failed_upload(
                app_send_document=app.send_document,
                object_guid=object_guid,
                file_path=fp,
                file_name=fp.name,
                caption="",
                provider="musicdl",
                exc=exc,
            )
            file_moved_to_retry = True
            await app.edit_message(
                object_guid, status_id,
                "📦 Upload failed. Will retry automatically every hour. "
                "Send !uploads to see status, or !uploads cancel <id> to drop it.",
            )
    finally:
        # Only clean up the file if it wasn't moved to the retry queue
        if not file_moved_to_retry:
            try:
                if fp.exists():
                    fp.unlink()
                    log.info("musicdl: cleaned up %s", fp)
            except Exception as exc:
                log.warning("musicdl cleanup failed for %s: %s", fp, exc)
            # Best-effort removal of empty parent dirs, but never touch MUSICDL_DOWNLOAD_DIR
            try:
                from rubetunes.providers.musicdl.config import MUSICDL_DOWNLOAD_DIR as _ROOT
                root = Path(_ROOT).resolve()
                parent = fp.parent.resolve()
                while parent != root and root in parent.parents:
                    try:
                        parent.rmdir()
                    except OSError:
                        break  # not empty or perms — stop walking up
                    parent = parent.parent
            except Exception:
                pass


# ---------------------------------------------------------------------------
# !uploads — inspect and manage the upload retry queue
# ---------------------------------------------------------------------------

@app.on_message_updates(filters.commands("uploads", prefixes="!"))
async def uploads_handler(update):
    """
    Manage the upload retry queue.

    Usage:
      !uploads                    — list all pending retry uploads
      !uploads cancel <id>        — cancel a specific queued upload (by UUID prefix)
    """
    log = logging.getLogger("uploads")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = update.command[1:] if update.command and len(update.command) > 1 else []
    subcmd = args[0].lower() if args else ""

    if subcmd == "cancel":
        if len(args) < 2:
            await app.send_message(object_guid, "❌ Usage: !uploads cancel <id>")
            return
        target_id = args[1]
        # Allow prefix matching so users don't have to type the full UUID
        entries = _upload_retry.list_entries()
        matched = [e for e in entries if e["id"].startswith(target_id)]
        if not matched:
            await app.send_message(object_guid, f"❌ No queued upload found with id starting with: {target_id}")
            return
        if len(matched) > 1:
            await app.send_message(
                object_guid,
                f"❌ Ambiguous id prefix '{target_id}' matches {len(matched)} entries. "
                "Please provide more characters."
            )
            return
        entry = matched[0]
        if _upload_retry.cancel_entry(entry["id"]):
            await app.send_message(
                object_guid,
                f"🗑️ Cancelled queued upload: {entry['file_name']} (id: {entry['id'][:8]}…)"
            )
        else:
            await app.send_message(object_guid, "❌ Could not cancel — entry may have already completed.")
        return

    # Default: list all entries
    entries = _upload_retry.list_entries()
    if not entries:
        await app.send_message(object_guid, "✅ No pending uploads in the retry queue.")
        return

    lines = [f"📋 Upload retry queue ({len(entries)} item{'s' if len(entries) != 1 else ''}):"]
    for e in entries:
        short_id = e["id"][:8]
        lines.append(
            f"• [{short_id}…] {e['file_name']} | provider={e['provider']} | "
            f"attempts={e['attempts']} | last_error={e['last_error'][:60]}"
        )
    lines.append("\nUse !uploads cancel <id> to remove an entry.")
    await app.send_message(object_guid, "\n".join(lines))


# ---------------------------------------------------------------------------
# !ytsub / !ytunsub / !ytsubs — YouTube channel subscription notifier
# ---------------------------------------------------------------------------


@app.on_message_updates(filters.commands("ytsub", prefixes="!"))
async def ytsub_handler(update):
    """Subscribe to a YouTube channel.

    Usage: !ytsub <channel_url>
    """
    log = logging.getLogger("ytsub")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    if not args:
        await app.send_message(
            object_guid,
            "❌ Please provide a YouTube channel URL.\n"
            "Example: !ytsub https://www.youtube.com/@SomeChannel",
        )
        return

    raw_url = args.strip()
    log.info("!ytsub | guid=%s url=%r", object_guid, raw_url)
    _append_log(object_guid, "ytsub", raw_url)

    status_id = (
        await app.send_message(object_guid, "🔍 Resolving channel, please wait…")
    ).message_id

    loop = asyncio.get_event_loop()
    try:
        ok, error_msg, record, seed_videos = await loop.run_in_executor(
            None, _yt_notify.subscribe_sync, raw_url, object_guid
        )
    except Exception as exc:
        log.error("ytsub: unexpected error: %s", exc)
        await app.edit_message(object_guid, status_id, "❌ An unexpected error occurred.")
        return

    if not ok:
        await app.edit_message(object_guid, status_id, error_msg)
        return

    channel_name = record["channel_name"]

    if seed_videos:
        # Brand-new channel: show welcome list.
        lines = [f"✅ Subscribed to **{channel_name}**.", "📜 Latest uploads from this channel:"]
        for i, v in enumerate(seed_videos, 1):
            lines.append(f"  {i}. {v['title']} — {v['url']}")
        lines.append("\nYou will be notified here when new videos are uploaded.")
        await app.edit_message(object_guid, status_id, "\n".join(lines))
    else:
        await app.edit_message(
            object_guid,
            status_id,
            f"✅ Subscribed to **{channel_name}**.\n"
            "You will be notified here when new videos are uploaded.",
        )


@app.on_message_updates(filters.commands("ytunsub", prefixes="!"))
async def ytunsub_handler(update):
    """Unsubscribe from a YouTube channel.

    Usage: !ytunsub <channel_url>
    """
    log = logging.getLogger("ytunsub")
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    args = " ".join(update.command[1:]) if update.command and len(update.command) > 1 else ""
    if not args:
        await app.send_message(
            object_guid,
            "❌ Please provide a YouTube channel URL.\n"
            "Example: !ytunsub https://www.youtube.com/@SomeChannel",
        )
        return

    raw_url = args.strip()
    log.info("!ytunsub | guid=%s url=%r", object_guid, raw_url)
    _append_log(object_guid, "ytunsub", raw_url)

    status_id = (
        await app.send_message(object_guid, "⏳ Processing…")
    ).message_id

    loop = asyncio.get_event_loop()
    try:
        ok, message = await loop.run_in_executor(
            None, _yt_notify.unsubscribe_sync, raw_url, object_guid
        )
    except Exception as exc:
        log.error("ytunsub: unexpected error: %s", exc)
        await app.edit_message(object_guid, status_id, "❌ An unexpected error occurred.")
        return

    await app.edit_message(object_guid, status_id, message)


@app.on_message_updates(filters.commands("ytsubs", prefixes="!"))
async def ytsubs_handler(update):
    """List your YouTube channel subscriptions.

    Usage: !ytsubs
    """
    object_guid = update.object_guid

    allowed, reason = _check_access(object_guid)
    if not allowed:
        await app.send_message(object_guid, reason)
        return

    subscriptions = _yt_notify.list_subscriptions_sync(object_guid)

    if not subscriptions:
        await app.send_message(
            object_guid,
            "ℹ️ You are not subscribed to any YouTube channels.\n"
            "Use !ytsub <channel_url> to subscribe.",
        )
        return

    lines = [f"📋 Your YouTube subscriptions ({len(subscriptions)}):"]
    for ch in subscriptions:
        lines.append(f"• {ch['channel_name']} — {ch['channel_url']}")
    await app.send_message(object_guid, "\n".join(lines))


if __name__ == "__main__":
    _upload_retry.load_on_startup()
    _yt_notify.load_on_startup()
    _restore_queue_snapshot()
    print("[rub] Connecting to Rubika...")

    async def _main():
        await app.start(phone_number=PHONE_NUMBER)
        _upload_retry.start_retry_loop(app)
        _yt_notify.start_poll_loop(app.send_message)
        await app.get_updates()

    try:
        asyncio.run(_main())
    except Exception as exc:
        print("[rub] Connection failed: {}".format(exc))
        raise
    print("[rub] Disconnected.")
