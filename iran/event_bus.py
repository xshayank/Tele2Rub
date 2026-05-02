"""In-process SSE/WebSocket event bus stub (Step 7).

Exposes the interface that the job API and progress layers will use to fan
out job-progress events to connected clients.  The real implementation is
added in Step 7 alongside the SSE endpoint.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("iran.event_bus")


@runtime_checkable
class EventBusProtocol(Protocol):
    """Minimal interface for the in-process event fan-out bus."""

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Publish *event* to all subscribers of *job_id*."""
        ...

    async def close(self) -> None:
        """Release resources held by the event bus."""
        ...


class _StubEventBus:
    """No-op stub used until Step 7 wires the real bus."""

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:  # noqa: ARG002
        logger.debug(
            "StubEventBus.publish (no-op)",
            extra={"job_id": job_id, "event_type": event.get("type")},
        )

    async def close(self) -> None:
        logger.debug("StubEventBus.close (no-op)")


def make_event_bus() -> EventBusProtocol:
    """Factory used by the DI container in ``iran/main.py``.

    Returns the stub until Step 7 introduces the real implementation.
    """
    return _StubEventBus()
