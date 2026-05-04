"""musicdl search adapter for the Kharej VPS worker.

Uses the existing :class:`~rubetunes.providers.musicdl.client.MusicdlClient`
(which wraps musicdl's ``MusicClient``) to search across configured sources.

Returns text-only results (title, artist, source, duration).  No thumbnail
images are fetched or returned.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("kharej.searchers.musicdl")

_MAX_LIMIT: int = 20


async def musicdl_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search musicdl sources and return the top *limit* results.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of tracks to return (capped at ``_MAX_LIMIT``).

    Returns
    -------
    list[dict]
        Each dict contains: ``title``, ``artist``, ``source``, ``duration``.
        Returns an empty list on any error.
    """
    limit = min(limit, _MAX_LIMIT)

    try:
        from rubetunes.providers.musicdl.client import MusicdlClient  # noqa: PLC0415
        from rubetunes.providers.musicdl.errors import MusicdlNotInstalledError  # noqa: PLC0415
    except ImportError as exc:
        logger.warning("musicdl or rubetunes not importable: %s", exc)
        return []

    try:
        client = MusicdlClient()
        search_result = await client.search(query, limit=limit)
    except MusicdlNotInstalledError:
        logger.warning("musicdl package is not installed; musicdl search unavailable")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.error("musicdl_search error: %s", exc)
        return []

    results: list[dict[str, Any]] = []
    for track in (search_result.tracks or [])[:limit]:
        results.append(
            {
                "title": track.song_name or "",
                "artist": track.singers or "",
                "source": track.source or "",
                "duration": track.duration or "",
            }
        )
    return results
