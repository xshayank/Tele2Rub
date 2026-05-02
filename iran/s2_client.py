"""Stub placeholder for the S2 read-only client (Step 6).

Exposes the interface that the rest of the application will depend on so
that DI wiring in ``iran/main.py`` already has a type to reference.  The
real implementation is added in Step 6.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("iran.s2")


@runtime_checkable
class S2ClientProtocol(Protocol):
    """Minimal read-only interface for the Arvan S2 object store."""

    async def presign_get(self, key: str, expires_in: int = 3600) -> str:
        """Return a presigned GET URL for *key*."""
        ...

    async def get_bytes(self, key: str) -> bytes:
        """Download *key* and return its contents as bytes."""
        ...


class _StubS2Client:
    """No-op stub used until Step 6 wires the real client."""

    async def presign_get(self, key: str, expires_in: int = 3600) -> str:  # noqa: ARG002
        logger.debug("StubS2Client.presign_get (no-op)", extra={"key": key})
        return ""

    async def get_bytes(self, key: str) -> bytes:  # noqa: ARG002
        logger.debug("StubS2Client.get_bytes (no-op)", extra={"key": key})
        return b""


def make_s2_client() -> S2ClientProtocol:
    """Factory used by the DI container in ``iran/main.py``.

    Returns the stub until Step 6 introduces the real implementation.
    """
    return _StubS2Client()
