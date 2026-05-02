"""Stub placeholder for the Iran-side Rubika transport client (Step 5).

This module exposes the interface that the rest of the application will
depend on so that the DI wiring in ``iran/main.py`` already has a type to
reference.  The real implementation is added in Step 5.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("iran.rubika")


@runtime_checkable
class RubikaClientProtocol(Protocol):
    """Minimal interface expected by consumers of the Rubika client."""

    async def send(self, text: str) -> None:
        """Send a text message to the Kharej account."""
        ...

    async def close(self) -> None:
        """Gracefully close the Rubika session."""
        ...


class _StubRubikaClient:
    """No-op stub used until Step 5 wires the real client."""

    async def send(self, text: str) -> None:  # noqa: ARG002
        logger.debug("StubRubikaClient.send (no-op)", extra={"text_len": len(text)})

    async def close(self) -> None:
        logger.debug("StubRubikaClient.close (no-op)")


def make_rubika_client() -> RubikaClientProtocol:
    """Factory used by the DI container in ``iran/main.py``.

    Returns the stub until Step 5 introduces the real implementation.
    """
    return _StubRubikaClient()
