"""Search API endpoints for the Iran VPS service.

Provides:

POST /search
    Forwards a search query to the Kharej worker, waits for results, and
    returns them as JSON.  The Iran VPS cannot reach external platforms
    directly; all thumbnails/cover images are therefore stored in S3 by the
    Kharej worker and referenced by S3 key in the results.

GET /search/thumb
    Thumbnail proxy — generates a short-lived presigned GET URL for a given S3
    key and redirects the browser to it.  Used by the search UI to display
    thumbnails without exposing S3 credentials to the browser.

The search correlation (outbound request ↔ inbound reply) uses the same
``asyncio.Event`` pattern as the health-ping flow in ``iran/api/admin.py``:
an event is registered on ``app.state.pending_searches`` keyed by
``request_id``; the inbound ``on_search_result`` / ``on_search_failed``
handlers in ``iran/main.py`` signal the event after storing the payload in
``app.state.search_results``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from iran.api.deps import get_current_user
from iran.contracts import SearchRequest

logger = logging.getLogger("iran.api.search")

router = APIRouter(prefix="/search", tags=["search"])

_SEARCH_TIMEOUT_SECONDS: float = 30.0
# Presigned URL lifetime for thumbnails (short — they are ephemeral search results)
_THUMB_PRESIGN_SECONDS: int = 300


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
    the Kharej worker to reply.

    Response (YouTube / musicdl)::

        {
            "platform": "youtube",
            "results": [
                {"title": "...", "channel": "...", "thumbnail_key": "thumbs/search/yt/...", ...},
                ...
            ]
        }

    Response (Spotify)::

        {
            "platform": "spotify",
            "results": [
                {
                    "tracks":    [{"title": "...", "cover_key": "thumbs/search/sp/...", ...}],
                    "albums":    [...],
                    "playlists": [...]
                }
            ]
        }

    Use ``GET /search/thumb?key=<thumbnail_key>`` to retrieve presigned image URLs.
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


# ---------------------------------------------------------------------------
# GET /search/thumb
# ---------------------------------------------------------------------------


@router.get("/thumb", include_in_schema=False)
async def thumbnail(
    key: str = Query(..., description="S3 object key of the thumbnail."),
    request: Request = None,  # type: ignore[assignment]
    current_user: Any = Depends(get_current_user),
) -> RedirectResponse:
    """Generate a presigned GET URL for an S3 thumbnail key and redirect to it.

    The browser follows the redirect and downloads the image directly from S3.
    The presigned URL is short-lived (``_THUMB_PRESIGN_SECONDS`` seconds) to
    limit the window of unintended access.

    Only keys under ``thumbs/search/`` are allowed to prevent this endpoint
    from being used as an oracle for arbitrary S3 objects.
    """
    if not key.startswith("thumbs/search/"):
        raise HTTPException(status_code=400, detail="Invalid thumbnail key prefix.")

    s2_client = getattr(request.app.state, "s2_client", None)
    if s2_client is None:
        raise HTTPException(status_code=503, detail="Storage client not available.")

    try:
        presigned_url: str = s2_client.generate_presigned_url(
            key, expires=_THUMB_PRESIGN_SECONDS
        )
    except Exception as exc:
        logger.warning("Failed to generate presigned URL for %s: %s", key, exc)
        raise HTTPException(status_code=502, detail="Could not generate thumbnail URL.") from exc

    return RedirectResponse(url=presigned_url, status_code=302)
