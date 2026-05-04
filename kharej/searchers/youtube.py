"""YouTube search adapter for the Kharej VPS worker.

Uses yt-dlp's ``ytsearch`` extractor to fetch the top *limit* results.
No audio is downloaded — only metadata (title, channel, duration, video ID,
and the thumbnail) is returned.

Thumbnail handling
------------------
The Iran VPS **cannot** reach YouTube CDN (i.ytimg.com) directly.  For each
search result the Kharej worker therefore downloads the thumbnail and uploads
it to the shared S3 bucket under the key::

    thumbs/search/yt/{video_id}.jpg

The S3 key is returned in the result dict as ``thumbnail_key``.  If S3 upload
fails for a particular result (network issue, oversized image, etc.) the key
is omitted from that result — callers should handle a missing ``thumbnail_key``
gracefully (show a placeholder image).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kharej.s2_client import S2Client

logger = logging.getLogger("kharej.searchers.youtube")

# Maximum number of results allowed (guards against accidental large requests)
_MAX_LIMIT: int = 20


async def youtube_search(
    query: str,
    limit: int = 10,
    *,
    s2: "S2Client | None" = None,
) -> list[dict[str, Any]]:
    """Search YouTube and return the top *limit* results.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results to return (capped at ``_MAX_LIMIT``).
    s2:
        Optional Kharej S2 client.  When provided each result's thumbnail is
        downloaded and uploaded to S3; the S3 key is included in the result
        as ``thumbnail_key``.  When ``None`` thumbnails are omitted.

    Returns
    -------
    list[dict]
        Each dict contains:
        ``title``, ``channel``, ``duration``, ``video_id``, ``url``,
        ``upload_date`` (ISO string ``"YYYY-MM-DD"`` or ``None``),
        ``upload_timestamp`` (integer UTC epoch seconds or ``None``),
        and optionally ``thumbnail_key`` (S3 key).
        Returns an empty list on any error.
    """
    limit = min(limit, _MAX_LIMIT)

    def _blocking_search() -> list[dict[str, Any]]:
        try:
            import yt_dlp  # noqa: PLC0415
        except ImportError:
            logger.warning("yt_dlp is not installed; YouTube search unavailable")
            return []

        # Same cookies file used by the download pipeline (kharej/downloaders/youtube.py).
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,  # metadata only, no download
            "noplaylist": True,
            "skip_download": True,
            "cookiefile": "/root/newrube/RubeTunes/kharej/cookies.txt",
        }
        search_url = f"ytsearch{limit}:{query}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yt_dlp search failed: %s", exc)
            return []

        entries = (info or {}).get("entries") or []
        raw: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id: str = entry.get("id") or entry.get("url") or ""
            # Strip any URL prefix to get a bare video ID.
            # Use proper URL parsing (not substring check) to avoid false matches.
            if "://" in video_id:
                from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

                parsed = urlparse(video_id)
                hostname = (parsed.hostname or "").lower()
                if hostname.endswith("youtube.com") or hostname.endswith("youtu.be"):
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

            # --- upload date / timestamp -----------------------------------
            # yt-dlp flat-playlist entries may carry "upload_date" (YYYYMMDD)
            # and/or "timestamp" (Unix epoch seconds).  Use what's available
            # without triggering any extra network requests.
            upload_date_raw: str | None = entry.get("upload_date")
            upload_date_iso: str | None = None
            if upload_date_raw and len(upload_date_raw) == 8:
                upload_date_iso = (
                    f"{upload_date_raw[:4]}-{upload_date_raw[4:6]}-{upload_date_raw[6:]}"
                )

            ts_raw = entry.get("timestamp")
            upload_timestamp: int | None = None
            if ts_raw is not None:
                try:
                    upload_timestamp = int(ts_raw)
                except (TypeError, ValueError):
                    pass
            elif upload_date_iso:
                # Derive epoch from the date (UTC midnight) so the UI can still
                # show a relative string like "2 سال پیش".
                from datetime import datetime, timezone  # noqa: PLC0415

                try:
                    y = int(upload_date_raw[:4])  # type: ignore[index]
                    m = int(upload_date_raw[4:6])  # type: ignore[index]
                    d = int(upload_date_raw[6:])   # type: ignore[index]
                    upload_timestamp = int(
                        datetime(y, m, d, tzinfo=timezone.utc).timestamp()
                    )
                except (TypeError, ValueError):
                    pass

            raw.append(
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
                    "upload_date": upload_date_iso,
                    "upload_timestamp": upload_timestamp,
                    # Native YouTube thumbnail URL — will be replaced by S3 key below
                    "_thumb_src": (
                        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                        if video_id
                        else ""
                    ),
                }
            )

        return raw

    try:
        raw_results = await asyncio.to_thread(_blocking_search)
    except Exception as exc:  # noqa: BLE001
        logger.error("youtube_search thread error: %s", exc)
        return []

    if not raw_results:
        return []

    # Upload thumbnails to S3 concurrently (if s2 provided)
    if s2 is not None:
        from kharej.searchers.common import upload_thumb_to_s3  # noqa: PLC0415

        async def _upload_one(item: dict[str, Any]) -> dict[str, Any]:
            thumb_src: str = item.pop("_thumb_src", "")
            video_id: str = item.get("video_id", "")
            if thumb_src and video_id:
                s3_key = f"thumbs/search/yt/{video_id}.jpg"
                uploaded_key = await upload_thumb_to_s3(thumb_src, s2, s3_key)
                if uploaded_key:
                    item["thumbnail_key"] = uploaded_key
            return item

        results = await asyncio.gather(*[_upload_one(r) for r in raw_results])
        return list(results)
    else:
        # No S3 client — strip the internal thumbnail src field
        for item in raw_results:
            item.pop("_thumb_src", None)
        return raw_results
