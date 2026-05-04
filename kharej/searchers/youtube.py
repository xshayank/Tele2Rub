"""YouTube search adapter for the Kharej VPS worker.

Uses yt-dlp's ``ytsearch`` extractor to fetch the top *limit* results.
No audio is downloaded — only metadata (title, channel, duration, video ID,
and the standard YouTube thumbnail URL) is returned.

Thumbnail note
--------------
YouTube thumbnails are returned as native ``i.ytimg.com`` URLs using the
predictable pattern ``https://i.ytimg.com/vi/{video_id}/hqdefault.jpg``.
No S3 upload is performed because:
  1. The URLs are stable, publicly accessible, and free.
  2. Uploading ~10 thumbnails per search would consume S3 quota for ephemeral
     data that is never downloaded through RubeTunes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("kharej.searchers.youtube")

# Maximum number of results allowed (guards against accidental large requests)
_MAX_LIMIT: int = 20


async def youtube_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search YouTube and return the top *limit* results.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results to return (capped at ``_MAX_LIMIT``).

    Returns
    -------
    list[dict]
        Each dict contains:
        ``title``, ``channel``, ``duration``, ``video_id``, ``url``,
        ``thumbnail_url``.
        Returns an empty list on any error.
    """
    limit = min(limit, _MAX_LIMIT)

    def _blocking_search() -> list[dict[str, Any]]:
        try:
            import yt_dlp  # noqa: PLC0415
        except ImportError:
            logger.warning("yt_dlp is not installed; YouTube search unavailable")
            return []

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,  # don't download, just extract metadata
            "noplaylist": True,
            "skip_download": True,
        }
        search_url = f"ytsearch{limit}:{query}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yt_dlp search failed: %s", exc)
            return []

        entries = (info or {}).get("entries") or []
        results: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id: str = entry.get("id") or entry.get("url") or ""
            # Strip any URL prefix to get a bare video ID
            if "youtube.com" in video_id or "youtu.be" in video_id:
                from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

                parsed = urlparse(video_id)
                qs_v = parse_qs(parsed.query).get("v")
                if qs_v:
                    video_id = qs_v[0]
                elif parsed.path.startswith("/"):
                    video_id = parsed.path.lstrip("/")

            duration_sec: int | None = entry.get("duration")
            if duration_sec:
                minutes, seconds = divmod(int(duration_sec), 60)
                duration_str = f"{minutes}:{seconds:02d}"
            else:
                duration_str = ""

            thumbnail_url = (
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
            )
            results.append(
                {
                    "title": entry.get("title") or "",
                    "channel": entry.get("uploader") or entry.get("channel") or "",
                    "duration": duration_str,
                    "video_id": video_id,
                    "url": (
                        f"https://www.youtube.com/watch?v={video_id}"
                        if video_id
                        else entry.get("webpage_url") or entry.get("url") or ""
                    ),
                    "thumbnail_url": thumbnail_url,
                }
            )

        return results

    try:
        return await asyncio.to_thread(_blocking_search)
    except Exception as exc:  # noqa: BLE001
        logger.error("youtube_search thread error: %s", exc)
        return []
