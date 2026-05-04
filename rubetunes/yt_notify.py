"""YouTube channel subscription / new-upload notifier for RubeTunes.

Commands (handled in rub.py):
  !ytsub <channel_url>   — subscribe to a YouTube channel
  !ytunsub <channel_url> — unsubscribe from a YouTube channel
  !ytsubs                — list your current subscriptions

Design:
  Per-channel polling — one yt-dlp call per channel per cycle, regardless of
  how many users subscribe to it.  Notifications fan out from that single result
  to every subscriber.

Persistence:
  ytsub_state.json in the repo root.

  Schema::

    {
      "channels": {
        "<channel_id>": {
          "channel_id": "UC...",
          "channel_name": "Some Channel",
          "channel_url": "https://www.youtube.com/channel/UC.../videos",
          "subscribers": ["rubika_guid_1", "rubika_guid_2"],
          "last_seen_video_ids": ["<yt_id>", ...]
        }
      }
    }

Environment variables:
  YTSUB_POLL_INTERVAL   Seconds between poll cycles (default 900 = 15 min).
  YTSUB_STATE_FILE      Override path for ytsub_state.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("yt_notify")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent.parent  # repo root

YTSUB_POLL_INTERVAL: int = int(os.getenv("YTSUB_POLL_INTERVAL", "900"))

_default_state_file = str(_BASE_DIR / "ytsub_state.json")
_STATE_FILE: Path = Path(os.getenv("YTSUB_STATE_FILE", _default_state_file))

# How many recent videos to seed as "already seen" when a channel is first added.
_SEED_COUNT = 10

# How many videos to fetch per poll tick to detect new uploads.
_POLL_FETCH_COUNT = 5

# yt-dlp timeout per invocation (seconds).
_YTDLP_TIMEOUT = 60

# ---------------------------------------------------------------------------
# URL normalisation helpers
# ---------------------------------------------------------------------------

# Regex that matches any recognisable YouTube channel URL.
_CHANNEL_URL_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/"
    r"(?:"
    r"channel/(?P<uc_id>UC[\w-]{10,})"  # /channel/UC… (UC + at least 10 chars)
    r"|@(?P<handle>[\w-]+)"  # /@handle
    r"|c/(?P<c_name>[\w.-]+)"  # /c/legacyname
    r"|user/(?P<user_name>[\w.-]+)"  # /user/legacyname
    r")"
    r"(?:/\S*)?"  # optional path suffix like /videos
)


def _normalise_input_url(raw: str) -> str | None:
    """Strip trailing path segments and return a clean channel root URL.

    Returns None if *raw* does not look like a YouTube channel URL.
    """
    raw = raw.strip().rstrip("/")
    m = _CHANNEL_URL_RE.match(raw)
    if not m:
        return None
    if m.group("uc_id"):
        return f"https://www.youtube.com/channel/{m.group('uc_id')}/videos"
    if m.group("handle"):
        return f"https://www.youtube.com/@{m.group('handle')}/videos"
    if m.group("c_name"):
        return f"https://www.youtube.com/c/{m.group('c_name')}/videos"
    if m.group("user_name"):
        return f"https://www.youtube.com/user/{m.group('user_name')}/videos"
    return None


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------


def _ytdlp_bin() -> str:
    """Return the path to the yt-dlp binary (local copy preferred)."""
    local = _BASE_DIR / "yt-dlp"
    return str(local) if local.exists() else "yt-dlp"


def _base_ytdlp_cmd() -> list[str]:
    """Common yt-dlp flags (user-agent + cookies)."""
    cmd: list[str] = [
        _ytdlp_bin(),
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "--referer",
        "https://www.youtube.com/",
    ]
    cmd += ["--cookies", "/root/newrube/RubeTunes/kharej/cookies.txt"]
    return cmd


def _run_ytdlp(args: list[str]) -> tuple[int, str, str]:
    """Run yt-dlp synchronously and return (returncode, stdout, stderr)."""
    cmd = _base_ytdlp_cmd() + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_YTDLP_TIMEOUT,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        log.error("yt-dlp timed out: %s", " ".join(args))
        return -1, "", "timeout"
    except Exception as exc:
        log.error("yt-dlp error: %s", exc)
        return -1, "", str(exc)


def resolve_channel(url: str) -> dict[str, str] | None:
    """Resolve a YouTube channel URL to ``{channel_id, channel_name, channel_url}``.

    Uses ``yt-dlp --flat-playlist -J --playlist-end 1`` to extract channel
    metadata without downloading any video.

    Returns None on failure.
    """
    rc, stdout, stderr = _run_ytdlp(
        ["--flat-playlist", "-J", "--playlist-end", "1", "--no-warnings", url]
    )
    if rc != 0 or not stdout.strip():
        log.error("yt-dlp channel resolve failed (rc=%d): %s", rc, stderr[:200])
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("yt-dlp channel resolve: invalid JSON: %s", exc)
        return None

    channel_id = data.get("channel_id") or data.get("uploader_id", "")
    channel_name = (
        data.get("channel")
        or data.get("uploader")
        or data.get("title")
        or channel_id
    )
    # Prefer a channel_url with the UC… id for stability.
    if channel_id and channel_id.startswith("UC"):
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    else:
        channel_url = data.get("webpage_url", url)

    if not channel_id:
        log.error("yt-dlp channel resolve: no channel_id in response")
        return None

    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }


def fetch_latest_video_ids(channel_url: str, count: int = _POLL_FETCH_COUNT) -> list[dict]:
    """Return the latest *count* videos from *channel_url*.

    Each item is ``{"id": "<yt_id>", "title": "<title>", "url": "<watch_url>"}``.
    Returns an empty list on failure.
    """
    rc, stdout, stderr = _run_ytdlp(
        [
            "--flat-playlist",
            "-J",
            "--playlist-end",
            str(count),
            "--no-warnings",
            channel_url,
        ]
    )
    if rc != 0 or not stdout.strip():
        log.error("yt-dlp fetch videos failed (rc=%d): %s", rc, stderr[:200])
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("yt-dlp fetch videos: invalid JSON: %s", exc)
        return []

    entries = data.get("entries") or []
    videos: list[dict] = []
    for entry in entries:
        vid_id = entry.get("id") or entry.get("url", "")
        title = entry.get("title") or vid_id
        watch_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
        if not watch_url.startswith("http"):
            watch_url = f"https://www.youtube.com/watch?v={vid_id}"
        if vid_id:
            videos.append({"id": vid_id, "title": title, "url": watch_url})
    return videos


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

_state_lock = asyncio.Lock()


def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("channels", {})
                return data
        except Exception as exc:
            log.warning("yt_notify: failed to load state: %s", exc)
    return {"channels": {}}


def _save_state(state: dict[str, Any]) -> None:
    tmp = _STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as exc:
        log.error("yt_notify: failed to save state: %s", exc)


# In-memory state — mutated under _state_lock.
_state: dict[str, Any] = _load_state()


# ---------------------------------------------------------------------------
# Public API (sync helpers — called from async handlers with run_in_executor)
# ---------------------------------------------------------------------------


def _channel_record(channel_id: str) -> dict | None:
    return _state["channels"].get(channel_id)


def subscribe_sync(
    raw_url: str, user_guid: str
) -> tuple[bool, str, dict | None, list[dict]]:
    """Subscribe *user_guid* to the channel at *raw_url*.

    Returns ``(ok, error_message, channel_record, seed_videos)``.

    *seed_videos* is non-empty only when the channel is **brand new** to the
    bot (first subscriber ever).
    """
    input_url = _normalise_input_url(raw_url)
    if input_url is None:
        return False, "❌ Invalid YouTube channel URL.", None, []

    # Resolve channel metadata via yt-dlp (network call).
    info = resolve_channel(input_url)
    if info is None:
        return False, "❌ Could not resolve that YouTube channel. Please check the URL.", None, []

    channel_id = info["channel_id"]
    channel_name = info["channel_name"]
    channel_url = info["channel_url"]

    seed_videos: list[dict] = []
    is_new_channel = channel_id not in _state["channels"]

    if is_new_channel:
        # Fetch last N videos to seed last_seen_video_ids.
        seed_videos = fetch_latest_video_ids(channel_url, _SEED_COUNT)
        last_seen = [v["id"] for v in seed_videos]
        _state["channels"][channel_id] = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "channel_url": channel_url,
            "subscribers": [],
            "last_seen_video_ids": last_seen,
        }

    record = _state["channels"][channel_id]
    # Update name in case it changed.
    record["channel_name"] = channel_name

    if user_guid in record["subscribers"]:
        return False, f"ℹ️ You are already subscribed to **{channel_name}**.", record, []

    record["subscribers"].append(user_guid)
    _save_state(_state)

    return True, "", record, seed_videos if is_new_channel else []


def unsubscribe_sync(raw_url: str, user_guid: str) -> tuple[bool, str]:
    """Remove *user_guid* from the subscriber list of the given channel.

    Returns ``(ok, message)``.
    """
    input_url = _normalise_input_url(raw_url)
    if input_url is None:
        return False, "❌ Invalid YouTube channel URL."

    # Try to match by URL without a network call first.
    target_id: str | None = None

    # Fast path: /channel/UC… URL — we can extract the id directly.
    uc_match = re.search(r"/channel/(UC[\w-]{10,})", input_url)
    if uc_match:
        target_id = uc_match.group(1)
        if target_id not in _state["channels"]:
            target_id = None  # id found but not tracked

    # Slow path: resolve via yt-dlp.
    if target_id is None:
        info = resolve_channel(input_url)
        if info is None:
            return False, "❌ Could not resolve that YouTube channel."
        target_id = info["channel_id"]

    record = _state["channels"].get(target_id)
    if record is None or user_guid not in record["subscribers"]:
        return False, "ℹ️ You are not subscribed to that channel."

    channel_name = record["channel_name"]
    record["subscribers"].remove(user_guid)

    # If nobody is left, remove the channel entry entirely.
    if not record["subscribers"]:
        del _state["channels"][target_id]

    _save_state(_state)
    return True, f"✅ Unsubscribed from **{channel_name}**."


def list_subscriptions_sync(user_guid: str) -> list[dict]:
    """Return channel records the user is subscribed to."""
    return [
        ch
        for ch in _state["channels"].values()
        if user_guid in ch["subscribers"]
    ]


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------


async def _poll_once(send_message) -> None:
    """Poll every tracked channel once and notify subscribers of new videos.

    *send_message* is a coroutine ``(guid, text) -> None`` (e.g. ``app.send_message``).
    """
    async with _state_lock:
        channel_ids = list(_state["channels"].keys())

    for channel_id in channel_ids:
        async with _state_lock:
            record = _state["channels"].get(channel_id)
            if record is None:
                continue
            channel_url = record["channel_url"]
            channel_name = record["channel_name"]
            last_seen: list[str] = list(record["last_seen_video_ids"])
            subscribers: list[str] = list(record["subscribers"])

        if not subscribers:
            continue

        log.info("yt_notify: polling %s (%s)", channel_name, channel_id)

        # Run blocking yt-dlp call in a thread so we don't block the event loop.
        loop = asyncio.get_event_loop()
        try:
            videos = await loop.run_in_executor(
                None, fetch_latest_video_ids, channel_url, _POLL_FETCH_COUNT
            )
        except Exception as exc:
            log.error("yt_notify: error fetching %s: %s", channel_id, exc)
            continue

        last_seen_set = set(last_seen)
        new_videos = [v for v in videos if v["id"] not in last_seen_set]

        if not new_videos:
            log.debug("yt_notify: no new videos for %s", channel_id)
            continue

        log.info(
            "yt_notify: %d new video(s) from %s — notifying %d subscriber(s)",
            len(new_videos),
            channel_name,
            len(subscribers),
        )

        # Build notification message.
        lines = [f"🔔 New upload{'s' if len(new_videos) > 1 else ''} from **{channel_name}**:"]
        for v in new_videos:
            lines.append(f"▶️ {v['title']}\n   {v['url']}")
        message = "\n".join(lines)

        # Fan out to all subscribers.
        for guid in subscribers:
            try:
                await send_message(guid, message)
            except Exception as exc:
                log.warning("yt_notify: failed to DM %s: %s", guid, exc)

        # Update last_seen — prepend new ids, keep a bounded list.
        new_ids = [v["id"] for v in new_videos]
        updated_seen = new_ids + last_seen
        # Keep at most 50 ids to cap file size.
        updated_seen = updated_seen[:50]

        async with _state_lock:
            rec = _state["channels"].get(channel_id)
            if rec is not None:
                rec["last_seen_video_ids"] = updated_seen
                _save_state(_state)


async def _poll_loop(send_message) -> None:
    """Background asyncio task: run :func:`_poll_once` every interval."""
    log.info(
        "yt_notify: poll loop started (interval=%ds)",
        YTSUB_POLL_INTERVAL,
    )
    while True:
        await asyncio.sleep(YTSUB_POLL_INTERVAL)
        try:
            await _poll_once(send_message)
        except Exception as exc:
            log.exception("yt_notify: unexpected error in poll tick: %s", exc)


def start_poll_loop(send_message) -> asyncio.Task:
    """Schedule the background poll loop.  Call after the event loop is running."""
    return asyncio.ensure_future(_poll_loop(send_message))


# ---------------------------------------------------------------------------
# Startup helper
# ---------------------------------------------------------------------------


def load_on_startup() -> None:
    """Reload state from disk.  Call once before the event loop starts."""
    global _state
    _state = _load_state()
    log.info(
        "yt_notify: loaded %d channel(s) from %s",
        len(_state["channels"]),
        _STATE_FILE,
    )
