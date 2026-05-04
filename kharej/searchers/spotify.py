"""Spotify search adapter for the Kharej VPS worker.

Uses the existing ``spotify_search_multi()`` function from ``rubetunes.spotify_meta``
(which uses Spotify's public GraphQL endpoint — no API credentials required).

Returns the top results split into three categories: tracks, albums, playlists.
Cover images are passed through as Spotify CDN URLs — no S3 upload is needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("kharej.searchers.spotify")

_MAX_PER_CATEGORY: int = 5


async def spotify_search(
    query: str, limit_per_category: int = 5
) -> dict[str, list[dict[str, Any]]]:
    """Search Spotify for tracks, albums, and playlists.

    Parameters
    ----------
    query:
        Free-text search query.
    limit_per_category:
        Maximum results per category (tracks / albums / playlists).

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
        result = await asyncio.to_thread(_blocking_search)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("spotify_search error: %s", exc)
        return {"tracks": [], "albums": [], "playlists": []}
