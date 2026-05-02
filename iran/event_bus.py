"""In-process event bus for SSE fan-out (Track B, Step 5).

This module provides:

- :class:`EventBus` — real implementation backed by per-job
  :class:`asyncio.Queue` instances.  Multiple SSE connections can subscribe
  to the same job's events; all receive every update.
- :func:`make_event_bus` — DI factory used by ``iran/main.py``.

The protocol :class:`EventBusProtocol` is retained for type-checking against
the stub used in previous steps.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger("iran.event_bus")


# ---------------------------------------------------------------------------
# Protocol (for type checking / duck-typing)
# ---------------------------------------------------------------------------


@runtime_checkable
class EventBusProtocol(Protocol):
    """Minimal interface for the in-process event fan-out bus."""

    async def publish(self, job_id: str, event: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------


class EventBus:
    """In-process pub/sub hub keyed on *job_id*.

    Subscribers receive a :class:`asyncio.Queue` via the :meth:`subscribe`
    async context manager.  :meth:`publish` fans out *event* to every active
    subscriber for *job_id*.

    Example usage (SSE endpoint)::

        async with bus.subscribe(job_id) as queue:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\\n\\n"
    """

    def __init__(self) -> None:
        # job_id → set of asyncio.Queue instances
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    @asynccontextmanager
    async def subscribe(self, job_id: str) -> AsyncIterator[asyncio.Queue]:
        """Context manager that yields a :class:`~asyncio.Queue` for *job_id*.

        The queue is automatically removed on exit.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[job_id].add(queue)
        logger.debug(
            "EventBus subscriber added",
            extra={"job_id": job_id, "total": len(self._subscribers[job_id])},
        )
        try:
            yield queue
        finally:
            self.unsubscribe(job_id, queue)

    def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Fan *event* out to every active subscriber of *job_id*.

        Uses :meth:`~asyncio.Queue.put_nowait` so this method is synchronous
        and can be called from within a coroutine without ``await``.
        """
        queues = self._subscribers.get(job_id)
        if not queues:
            return
        for q in list(queues):  # snapshot to avoid mutation during iteration
            q.put_nowait(event)
        logger.debug(
            "EventBus published",
            extra={"job_id": job_id, "subscribers": len(queues)},
        )

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        """Remove *queue* from the subscriber set for *job_id*."""
        queues = self._subscribers.get(job_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self._subscribers[job_id]
            logger.debug(
                "EventBus subscriber removed",
                extra={"job_id": job_id},
            )

    async def close(self) -> None:
        """Signal all active subscribers with a ``None`` sentinel and clear state."""
        for job_id, queues in list(self._subscribers.items()):
            for q in list(queues):
                q.put_nowait(None)  # sentinel: SSE layer should stop on None
        self._subscribers.clear()
        logger.debug("EventBus closed")


# ---------------------------------------------------------------------------
# DI factory
# ---------------------------------------------------------------------------


def make_event_bus() -> EventBus:
    """Return a fresh :class:`EventBus` instance (used by ``iran/main.py``)."""
    return EventBus()
