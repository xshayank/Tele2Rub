"""Search API endpoints for the Iran VPS service.

Provides a single ``POST /search`` endpoint that:
1. Validates the request.
2. Sends a :class:`~kharej.contracts.SearchRequest` to the Kharej worker over Rubika.
3. Waits (up to 30 seconds) for a :class:`~kharej.contracts.SearchResult` or
   :class:`~kharej.contracts.SearchFailed` response.
4. Returns the results (or an error) as JSON.

The correlation between outbound request and inbound response uses an
``asyncio.Event`` stored on ``app.state.pending_searches`` — the same
pattern used by the health-ping flow in ``iran/api/admin.py``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from iran.api.deps import get_current_user
from iran.contracts import SearchRequest

logger = logging.getLogger("iran.api.search")

router = APIRouter(prefix="/search", tags=["search"])

_SEARCH_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SearchRequestBody(BaseModel):
    """Request body for ``POST /search``."""

    platform: Literal["youtube", "spotify", "musicdl"]
    query: str
    limit: int = 10


# ---------------------------------------------------------------------------
# POST /search
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_200_OK)
async def search(
    body: SearchRequestBody,
    request: Request,
    current_user: Any = Depends(get_current_user),
) -> dict[str, Any]:
    """Execute a search on the Kharej worker and return the results.

    The endpoint blocks (up to ``_SEARCH_TIMEOUT_SECONDS``) while waiting for
    the Kharej worker to reply, so it should be called via ``fetch()`` from
    the search UI rather than a streaming SSE endpoint.

    Response (success)::

        {
            "platform": "youtube",
            "results": [
                {"title": "...", "channel": "...", ...},
                ...
            ]
        }

    For Spotify the ``results`` list contains a single object with keys
    ``tracks``, ``albums``, ``playlists``::

        {
            "platform": "spotify",
            "results": [
                {
                    "tracks": [...],
                    "albums": [...],
                    "playlists": [...]
                }
            ]
        }
    """
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="Search query must not be empty.")

    limit = max(1, min(body.limit, 20))

    request_id = str(uuid.uuid4())
    event = asyncio.Event()

    # Register the pending search so the inbound handler can signal us.
    pending_searches: dict[str, asyncio.Event] = getattr(
        request.app.state, "pending_searches", {}
    )
    search_results: dict[str, Any] = getattr(request.app.state, "search_results", {})

    pending_searches[request_id] = event
    search_results[request_id] = None  # sentinel

    msg = SearchRequest(
        ts=datetime.now(tz=timezone.utc),
        request_id=request_id,
        platform=body.platform,
        query=query,
        limit=limit,
    )

    rubika_client = request.app.state.rubika_client
    try:
        await rubika_client.send(msg)
    except Exception as exc:
        pending_searches.pop(request_id, None)
        search_results.pop(request_id, None)
        logger.error(
            "Failed to send SearchRequest to Kharej",
            extra={"request_id": request_id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the search worker: {exc}",
        ) from exc

    # Wait for the Kharej worker to reply.
    try:
        await asyncio.wait_for(event.wait(), timeout=_SEARCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        pending_searches.pop(request_id, None)
        search_results.pop(request_id, None)
        raise HTTPException(
            status_code=504,
            detail="Search timed out. Please try again.",
        )

    pending_searches.pop(request_id, None)
    result = search_results.pop(request_id, None)

    if result is None:
        raise HTTPException(
            status_code=502,
            detail="No response received from the search worker.",
        )

    if result.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"Search failed: {result['error']}",
        )

    return {
        "platform": body.platform,
        "results": result.get("results", []),
    }
