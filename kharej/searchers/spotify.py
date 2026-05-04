"""Spotify search adapter for the Kharej VPS worker.

Uses the existing ``spotify_search_multi()`` function from
``rubetunes.spotify_meta`` (Spotify's public GraphQL endpoint — no API
credentials required) to search tracks, albums, and playlists.

Cover image handling
--------------------
The Iran VPS **cannot** reach Spotify CDN (i.scdn.co) directly.  For each
result that has a cover image URL the Kharej worker downloads the image and
uploads it to S3 under the key::

    thumbs/search/sp/{type}_{id}.jpg

where *type* is ``track``, ``album``, or ``playlist`` and *id* is the Spotify
resource ID derived from the result URL.  The S3 key is returned in the result
dict as ``cover_key``.  Missing or failed images are silently omitted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kharej.s2_client import S2Client

logger = logging.getLogger("kharej.searchers.spotify")

_MAX_PER_CATEGORY: int = 5

# Extract Spotify resource ID from URL
_SP_ID_RE = re.compile(r"open\.spotify\.com/(?:track|album|playlist)/([A-Za-z0-9]{22})")


def _extract_sp_id(url: str) -> str:
    """Return the Spotify resource ID from a URL, or '' if not found."""
    m = _SP_ID_RE.search(url)
    return m.group(1) if m else ""


async def spotify_search(
    query: str,
    limit_per_category: int = 5,
    *,
    s2: "S2Client | None" = None,
) -> dict[str, list[dict[str, Any]]]:
    """Search Spotify for tracks, albums, and playlists.

    Parameters
    ----------
    query:
        Free-text search query.
    limit_per_category:
        Maximum results per category (tracks / albums / playlists).
    s2:
        Optional Kharej S2 client.  When provided each result's cover image is
        downloaded and uploaded to S3; the S3 key is included as ``cover_key``.

    Returns
    -------
    dict
        Keys: ``tracks``, ``albums``, ``playlists``.
        Each value is a list of result dicts.  Returns empty lists on error.
    """
    limit_per_category = min(limit_per_category, _MAX_PER_CATEGORY)

    def _blocking_search() -> dict[str, list[dict[str, Any]]]:
        try:
            from rubetunes.spotify_meta import spotify_search_multi  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("rubetunes.spotify_meta not importable") from exc
        return spotify_search_multi(query, limit_per_category=limit_per_category)

    try:
        data = await asyncio.to_thread(_blocking_search)
    except Exception as exc:  # noqa: BLE001
        logger.error("spotify_search error: %s", exc)
        return {"tracks": [], "albums": [], "playlists": []}

    if s2 is None:
        return data

    # Upload cover images to S3 concurrently for each category
    from kharej.searchers.common import upload_thumb_to_s3  # noqa: PLC0415

    async def _enrich_item(item: dict[str, Any]) -> dict[str, Any]:
        cover_url: str = item.pop("cover_url", "") or ""
        if not cover_url:
            return item
        item_type: str = item.get("type", "track")
        item_url: str = item.get("url", "")
        sp_id = _extract_sp_id(item_url)
        if not sp_id:
            return item
        s3_key = f"thumbs/search/sp/{item_type}_{sp_id}.jpg"
        uploaded_key = await upload_thumb_to_s3(cover_url, s2, s3_key)
        if uploaded_key:
            item["cover_key"] = uploaded_key
        return item

    all_items: list[dict[str, Any]] = (
        list(data.get("tracks") or [])
        + list(data.get("albums") or [])
        + list(data.get("playlists") or [])
    )
    enriched = await asyncio.gather(*[_enrich_item(item) for item in all_items])

    # Re-split into categories preserving original order
    track_count = len(data.get("tracks") or [])
    album_count = len(data.get("albums") or [])
    return {
        "tracks": list(enriched[:track_count]),
        "albums": list(enriched[track_count : track_count + album_count]),
        "playlists": list(enriched[track_count + album_count :]),
    }
