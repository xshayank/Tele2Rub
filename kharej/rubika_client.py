"""Rubika control-channel client for the Kharej VPS.

Provides a high-level async interface for:
- Connecting to Rubika using the same ``rubpy`` library as ``rub.py``.
- Subscribing to messages from the Iran-side Rubika account.
- Parsing and validating ``RTUNES::`` envelopes.
- Publishing outbound messages back to the Iran account.
- Automatic reconnection with exponential backoff.

Small payloads travel over this channel; binary file data is routed
exclusively through Arvan S2.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from kharej.contracts import MAX_MESSAGE_BYTES, RTUNES_PREFIX, decode, encode

logger = logging.getLogger("kharej.rubika")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class RubikaError(Exception):
    """Base class for all Rubika client errors."""


class RubikaSendError(RubikaError):
    """Raised when a send operation fails after transport retries."""


class RubikaNotConnectedError(RubikaError):
    """Raised when ``send()`` is called while the transport is disconnected."""


# ---------------------------------------------------------------------------
# Transport seam (protocol + inbound message type)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    """A message received from Rubika."""

    sender_guid: str
    text: str
    raw_id: str | None = None


@runtime_checkable
class RubikaTransport(Protocol):
    """Minimal Rubika transport interface (facilitates unit testing)."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_text(self, peer_guid: str, text: str) -> None: ...

    def subscribe(self, callback: Callable[[InboundMessage], Awaitable[None]]) -> None: ...

    @property
    def connected(self) -> bool: ...


# ---------------------------------------------------------------------------
# Default (real) transport — wraps rubpy
# ---------------------------------------------------------------------------


class _DefaultRubikaTransport:
    """Real transport that wraps the ``rubpy`` library (same as ``rub.py``)."""

    def __init__(self, session_name: str) -> None:
        self._session_name = session_name
        self._client: Any = None
        self._connected = False
        self._callback: Callable[[InboundMessage], Awaitable[None]] | None = None

    @property
    def connected(self) -> bool:  # pragma: no cover
        return self._connected

    def subscribe(self, callback: Callable[[InboundMessage], Awaitable[None]]) -> None:  # pragma: no cover
        self._callback = callback

    async def connect(self) -> None:  # pragma: no cover
        import rubpy  # local import so tests can stub it

        self._client = rubpy.Client(name=self._session_name)
        await self._client.__aenter__()

        @self._client.on_message_updates()
        async def _on_update(update: Any) -> None:
            try:
                text = update.text or ""
                sender = getattr(update, "object_guid", None) or getattr(
                    update, "author_object_guid", ""
                )
                raw_id = getattr(update, "message_id", None)
                if self._callback is not None:
                    await self._callback(InboundMessage(sender_guid=sender, text=text, raw_id=raw_id))
            except Exception:
                logger.debug("Error in rubpy update handler", exc_info=True)

        self._connected = True

    async def disconnect(self) -> None:  # pragma: no cover
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
        self._connected = False

    async def send_text(self, peer_guid: str, text: str) -> None:  # pragma: no cover
        if self._client is None:
            raise RubikaNotConnectedError("Transport not connected")
        await self._client.send_message(object_guid=peer_guid, text=text)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RubikaConfig(BaseModel):
    """Configuration for the Kharej Rubika control client."""

    session_name: str
    iran_account_guid: str
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    inbound_max_bytes: int = MAX_MESSAGE_BYTES

    @classmethod
    def from_env(cls) -> RubikaConfig:
        """Build config from environment variables.

        Raises
        ------
        ValueError
            If any required environment variable is missing.
        """
        missing: list[str] = []
        session_name = os.environ.get("RUBIKA_SESSION_KHAREJ")
        iran_account_guid = os.environ.get("IRAN_RUBIKA_ACCOUNT_GUID")

        if not session_name:
            missing.append("RUBIKA_SESSION_KHAREJ")
        if not iran_account_guid:
            missing.append("IRAN_RUBIKA_ACCOUNT_GUID")

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        # Never log the session_name value — it may contain auth material.
        logger.debug("RubikaConfig loaded", extra={"event": "rubika.config_loaded", "session_name_set": True})

        return cls(session_name=session_name, iran_account_guid=iran_account_guid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Safe handler invocation
# ---------------------------------------------------------------------------


async def _safe_invoke(
    handler: Callable[[Any], Awaitable[None]],
    msg: Any,
) -> None:
    """Invoke *handler* with *msg*; swallow and log any exception."""
    try:
        await handler(msg)
    except Exception as exc:
        msg_type = getattr(msg, "type", "unknown")
        logger.error(
            "Handler raised an exception",
            extra={"event": "rubika.handler_failed", "type": msg_type, "exc": repr(exc)},
        )


# ---------------------------------------------------------------------------
# RubikaClient
# ---------------------------------------------------------------------------


class RubikaClient:
    """Async Rubika control-channel client for the Kharej VPS.

    Parameters
    ----------
    config:
        Configuration instance (built via ``RubikaConfig.from_env()``).
    transport_factory:
        Optional factory that returns a ``RubikaTransport``-compatible object.
        Defaults to a factory that builds ``_DefaultRubikaTransport``.
    """

    def __init__(
        self,
        config: RubikaConfig,
        *,
        transport_factory: Callable[[RubikaConfig], RubikaTransport] | None = None,
    ) -> None:
        self._config = config
        if transport_factory is None:

            def transport_factory(cfg: RubikaConfig) -> RubikaTransport:
                return _DefaultRubikaTransport(cfg.session_name)

        self._transport: RubikaTransport = transport_factory(config)
        self._handler: Callable[[Any], Awaitable[None]] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """``True`` if the underlying transport reports a live connection."""
        return self._transport.connected

    def on_message(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        """Register the single async handler for validated inbound messages.

        Replaces any previously registered handler.
        """
        self._handler = handler

    async def start(self) -> None:
        """Connect, subscribe, and start the reconnect supervisor. Idempotent."""
        if self._started:
            return
        self._started = True
        self._transport.subscribe(self._handle_inbound)
        await self._transport.connect()
        self._supervisor_task = asyncio.create_task(self._supervisor())

    async def stop(self) -> None:
        """Gracefully shut down: cancel supervisor, disconnect transport."""
        if not self._started:
            return
        self._started = False
        if self._supervisor_task is not None and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
        await self._transport.disconnect()

    async def send(self, message: BaseModel) -> None:
        """Encode *message* and publish it to the Iran account.

        Raises
        ------
        RubikaNotConnectedError
            If the transport is not currently connected.
        ValueError
            If the encoded payload exceeds ``MAX_MESSAGE_BYTES`` (from ``encode``).
        RubikaSendError
            On transport-level failure after retries.
        """
        if not self._transport.connected:
            raise RubikaNotConnectedError("Not connected to Rubika")
        wire = encode(message)  # raises ValueError on oversize
        try:
            await self._transport.send_text(self._config.iran_account_guid, wire)
        except RubikaNotConnectedError:
            raise
        except Exception as exc:
            raise RubikaSendError(f"Failed to send message: {exc}") from exc

    # ------------------------------------------------------------------
    # Inbound pipeline
    # ------------------------------------------------------------------

    async def _handle_inbound(self, msg: InboundMessage) -> None:
        """Process a single inbound message through the filter pipeline."""
        # Step 1: sender filter
        if msg.sender_guid != self._config.iran_account_guid:
            logger.info(
                "Rejected message from unexpected sender",
                extra={"event": "rubika.reject_sender", "sender_guid": msg.sender_guid},
            )
            return

        # Step 2: size filter
        byte_len = len(msg.text.encode("utf-8"))
        if byte_len > self._config.inbound_max_bytes:
            logger.warning(
                "Rejected oversized message",
                extra={"event": "rubika.reject_oversize", "bytes": byte_len},
            )
            return

        # Step 3: prefix filter
        if not msg.text.startswith(RTUNES_PREFIX):
            logger.info(
                "Rejected unprefixed message",
                extra={"event": "rubika.reject_unprefixed"},
            )
            return

        logger.debug(
            "Processing inbound message",
            extra={"event": "rubika.inbound_received"},
        )

        # Step 4: decode / validate
        try:
            parsed = decode(msg.text)
        except Exception as exc:
            logger.warning(
                "Rejected invalid message",
                extra={"event": "rubika.reject_invalid", "error": str(exc)},
            )
            return

        # Step 5: handler check
        if self._handler is None:
            msg_type = getattr(parsed, "type", "unknown")
            logger.warning(
                "No handler registered",
                extra={"event": "rubika.no_handler", "type": msg_type},
            )
            return

        # Step 6: dispatch via task
        asyncio.create_task(_safe_invoke(self._handler, parsed))

    # ------------------------------------------------------------------
    # Reconnect supervisor
    # ------------------------------------------------------------------

    async def _supervisor(self) -> None:
        """Background task: watch for disconnects and reconnect with backoff."""
        backoff = self._config.reconnect_initial_seconds
        while True:
            await asyncio.sleep(0.1)  # polling interval
            if not self._transport.connected:
                logger.warning(
                    "Detected disconnection",
                    extra={"event": "rubika.disconnected"},
                )
                while True:
                    # Apply jitter ±20%
                    jitter = backoff * random.uniform(-0.2, 0.2)
                    sleep_time = max(0.0, backoff + jitter)
                    logger.debug(
                        "Reconnect backoff sleeping",
                        extra={"event": "rubika.reconnect_backoff", "sleep_sec": sleep_time},
                    )
                    await asyncio.sleep(sleep_time)
                    try:
                        await self._transport.connect()
                        logger.info(
                            "Reconnected successfully",
                            extra={"event": "rubika.reconnected", "backoff_used": sleep_time},
                        )
                        backoff = self._config.reconnect_initial_seconds  # reset
                        break
                    except Exception as exc:
                        logger.warning(
                            "Reconnect attempt failed",
                            extra={"event": "rubika.reconnect_failed", "exc": repr(exc)},
                        )
                        backoff = min(backoff * 2, self._config.reconnect_max_seconds)

