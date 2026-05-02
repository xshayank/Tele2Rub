"""Iran-side Rubika transport client (Track B, Step 5).

This module implements:

- :class:`IranRubikaConfig` — Pydantic-Settings configuration subset for the
  Rubika transport.
- :class:`IranRubikaClient` — sends outbound messages to the Kharej account
  and dispatches validated inbound messages to registered handlers.
- :class:`RubikaSendError` — raised when a send operation fails.
- :class:`FakeRubikaTransport` — test double that can inject arbitrary
  ``AnyMessage`` objects and record all outbound sends.
- :func:`make_rubika_client` — DI factory wired into ``iran/main.py``.

Design notes
------------
*Testability*: the client accepts an optional ``transport`` argument that must
implement :class:`_TransportProtocol` (``connect``, ``disconnect``,
``send_text``, ``receive_loop``).  In production the real :class:`_RubpyTransport`
wraps rubpy; tests inject :class:`FakeRubikaTransport`.

*Reconnect*: the supervisor loop retries with exponential back-off
(1 s → 2 s → 4 s → … ≤ 60 s, ±20 % jitter).

*De-duplication*: a size-bounded LRU set keyed on ``(job_id, type, ts)``
suppresses duplicate retransmits (max 2 000 entries).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from iran.contracts import MAX_MESSAGE_BYTES, RTUNES_PREFIX, decode, encode

logger = logging.getLogger("iran.rubika")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RubikaSendError(Exception):
    """Raised when a Rubika send operation fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class IranRubikaConfig(BaseSettings):
    """Rubika transport configuration (env_prefix=``IRAN_``)."""

    RUBIKA_SESSION_IRAN: str = ""
    KHAREJ_RUBIKA_ACCOUNT_GUID: str = ""
    IRAN_RUBIKA_ACCOUNT_GUID: str = ""  # local account GUID — used to reject echo

    model_config = SettingsConfigDict(
        env_prefix="IRAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _TransportProtocol(Protocol):
    """Minimal interface that the real (rubpy) and fake (test) transports satisfy."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_text(self, account_guid: str, text: str) -> None: ...

    async def receive_loop(self, on_message: Callable[[str, str], None]) -> None:
        """Run until disconnected, calling ``on_message(sender_guid, raw_text)``
        for each incoming text message.  Raise any exception to signal
        disconnection and trigger the reconnect supervisor."""
        ...


# ---------------------------------------------------------------------------
# Size-bounded LRU set (for de-duplication)
# ---------------------------------------------------------------------------


class _LRUSet:
    """Bounded set — oldest entries evicted when *maxsize* is exceeded.

    Uses dict insertion order as a simple LRU approximation (Python 3.7+ dicts
    maintain insertion order, so evicting ``next(iter(…))`` drops the oldest
    entry).
    """

    def __init__(self, maxsize: int = 2000) -> None:
        self._maxsize = maxsize
        self._store: dict[Any, None] = {}

    def contains(self, key: Any) -> bool:
        return key in self._store

    def add(self, key: Any) -> None:
        if key in self._store:
            del self._store[key]  # move to end (most recently seen)
        self._store[key] = None
        if len(self._store) > self._maxsize:
            self._store.pop(next(iter(self._store)))  # evict oldest


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class IranRubikaClient:
    """Iran-side Rubika transport — sends to and receives from the Kharej account.

    Parameters
    ----------
    config:
        Rubika connection parameters.
    transport:
        Optional transport override (real rubpy wrapper or
        :class:`FakeRubikaTransport` for tests).  When ``None`` the real
        :class:`_RubpyTransport` is constructed lazily in :meth:`start`.
    """

    #: Reconnect back-off parameters
    _BACKOFF_BASE: float = 1.0
    _BACKOFF_MAX: float = 60.0
    _BACKOFF_JITTER: float = 0.20  # ±20 %

    def __init__(
        self,
        config: IranRubikaConfig,
        *,
        transport: _TransportProtocol | None = None,
    ) -> None:
        self._config = config
        self._transport: _TransportProtocol | None = transport
        # Each handler must be an async callable: async def handler(msg: AnyMessage) -> None
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._dedup: _LRUSet = _LRUSet(maxsize=2000)
        self._running: bool = False
        self._loop_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Rubika and begin the supervised receive loop."""
        if self._running:
            return
        self._running = True
        if self._transport is None:
            self._transport = _RubpyTransport(self._config.RUBIKA_SESSION_IRAN)
        self._loop_task = asyncio.create_task(
            self._supervisor_loop(), name="iran-rubika-supervisor"
        )

    async def stop(self) -> None:
        """Gracefully stop the receive loop and disconnect."""
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        if self._transport is not None:
            try:
                await self._transport.disconnect()
            except Exception:
                pass

    # Alias used by the DI protocol (``close`` matches ``RubikaClientProtocol``)
    async def close(self) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_handler(self, msg_type: str, handler: Callable) -> None:
        """Register *handler* (``async def handler(msg) → None``) for *msg_type*."""
        self._handlers.setdefault(msg_type, []).append(handler)

    async def send(self, msg: BaseModel) -> None:
        """Encode *msg* and send it to the Kharej account over Rubika.

        Raises
        ------
        RubikaSendError
            If encoding or the Rubika send operation fails.
        """
        if self._transport is None:
            raise RubikaSendError("Transport not initialised; call start() first.")
        try:
            wire = encode(msg)
        except ValueError as exc:
            raise RubikaSendError(f"Encoding failed: {exc}") from exc
        try:
            await self._transport.send_text(
                self._config.KHAREJ_RUBIKA_ACCOUNT_GUID, wire
            )
        except Exception as exc:
            logger.error(
                "Rubika send failed",
                extra={"event": "send_error", "error": str(exc)},
            )
            raise RubikaSendError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Internal: supervisor loop
    # ------------------------------------------------------------------

    async def _supervisor_loop(self) -> None:
        """Connect, run the receive loop; on failure reconnect with back-off."""
        backoff = self._BACKOFF_BASE
        attempt = 0
        while self._running:
            try:
                await self._transport.connect()  # type: ignore[union-attr]
                logger.info(
                    "Rubika connected",
                    extra={"event": "connected", "attempt": attempt},
                )
                backoff = self._BACKOFF_BASE  # reset on success
                attempt = 0
                await self._transport.receive_loop(self._on_raw_message)  # type: ignore[union-attr]
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                jitter = backoff * self._BACKOFF_JITTER * (2 * random.random() - 1)
                delay = min(backoff + jitter, self._BACKOFF_MAX)
                delay = max(delay, 0.0)
                logger.warning(
                    "Rubika disconnected; reconnecting",
                    extra={
                        "event": "reconnect",
                        "attempt": attempt,
                        "delay_s": round(delay, 2),
                        "error": str(exc),
                    },
                )
                attempt += 1
                backoff = min(backoff * 2, self._BACKOFF_MAX)
                await asyncio.sleep(delay)
        logger.info("Rubika supervisor stopped", extra={"event": "stopped"})

    # ------------------------------------------------------------------
    # Internal: inbound message pipeline
    # ------------------------------------------------------------------

    def _on_raw_message(self, sender_guid: str, raw: str) -> None:
        """Validate, de-duplicate, and schedule dispatch for an inbound message.

        Called synchronously by the transport's receive_loop while the event
        loop is running, so ``asyncio.create_task`` is safe here.
        """
        # 1. Reject echo from our own account
        if sender_guid == self._config.IRAN_RUBIKA_ACCOUNT_GUID:
            return
        # 2. Reject messages not from the expected Kharej account
        if (
            self._config.KHAREJ_RUBIKA_ACCOUNT_GUID
            and sender_guid != self._config.KHAREJ_RUBIKA_ACCOUNT_GUID
        ):
            logger.debug(
                "Ignoring message from unexpected sender",
                extra={"sender": sender_guid},
            )
            return
        # 3. Reject messages without the RTUNES:: routing prefix
        if not raw.startswith(RTUNES_PREFIX):
            logger.debug(
                "Ignoring non-RTUNES message",
                extra={"prefix": raw[:16]},
            )
            return
        # 4. Reject oversized messages
        raw_bytes = raw.encode()
        if len(raw_bytes) > MAX_MESSAGE_BYTES:
            logger.warning(
                "Oversized message rejected",
                extra={"event": "oversized", "size": len(raw_bytes)},
            )
            return
        # 5. Decode to a typed message
        try:
            msg = decode(raw)
        except Exception as exc:
            logger.warning(
                "Failed to decode inbound message",
                extra={"event": "decode_error", "error": str(exc)},
            )
            return
        # 6. De-duplicate on (job_id, type, ts)
        dedup_key = (msg.job_id, msg.type, str(msg.ts))
        if self._dedup.contains(dedup_key):
            logger.debug(
                "Duplicate message suppressed",
                extra={"event": "dedup", "type": msg.type},
            )
            return
        self._dedup.add(dedup_key)
        # 7. Schedule handler dispatch
        asyncio.create_task(
            self._dispatch(msg), name=f"iran-rubika-dispatch-{msg.type}"
        )

    async def _dispatch(self, msg: Any) -> None:
        """Call every registered handler for *msg.type* in order."""
        for handler in self._handlers.get(msg.type, []):
            try:
                await handler(msg)
            except Exception as exc:
                logger.error(
                    "Handler raised an exception",
                    extra={
                        "event": "handler_error",
                        "type": msg.type,
                        "error": str(exc),
                    },
                )


# ---------------------------------------------------------------------------
# Real rubpy-based transport
# ---------------------------------------------------------------------------


class _RubpyTransport:
    """Production transport backed by the ``rubpy`` library.

    ``connect`` starts the rubpy client; ``receive_loop`` runs rubpy's
    internal update loop and calls *on_message* for every inbound text message.
    """

    def __init__(self, session: str) -> None:
        self._session = session
        self._client: Any = None

    async def connect(self) -> None:
        import rubpy  # lazy import — not required in tests

        self._client = rubpy.Client(name=self._session, display_welcome=False)
        await self._client.start()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    async def send_text(self, account_guid: str, text: str) -> None:
        if self._client is None:
            raise RubikaSendError("Not connected.")
        await self._client.send_message(account_guid, text)

    async def receive_loop(self, on_message: Callable[[str, str], None]) -> None:
        """Run rubpy's update loop, forwarding text messages via *on_message*."""
        if self._client is None:
            raise ConnectionError("Transport not connected.")

        import rubpy.handlers as _handlers

        async def _handler(update: Any) -> None:
            try:
                text = update.message.text
                sender = update.object_guid
                if text:
                    on_message(sender, text)
            except Exception:
                pass

        self._client.add_handler(_handler, _handlers.MessageUpdates())
        await self._client.get_updates()


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeRubikaTransport:
    """In-process test double for :class:`_TransportProtocol`.

    Usage::

        transport = FakeRubikaTransport(
            kharej_guid="kharej-guid",
            iran_guid="iran-guid",
        )
        # ... build IranRubikaClient(config, transport=transport)
        # Inject a message:
        from iran.contracts import encode, JobAccepted
        raw = encode(msg)
        await transport.inject_raw("kharej-guid", raw)
        # Check outbound sends:
        assert transport.sent[0][1].startswith("RTUNES::")
        # Simulate a disconnect:
        transport.simulate_disconnect()
    """

    def __init__(
        self,
        *,
        kharej_guid: str = "kharej-guid",
        iran_guid: str = "iran-guid",
    ) -> None:
        self.kharej_guid = kharej_guid
        self.iran_guid = iran_guid
        self.connected: bool = False
        #: List of ``(account_guid, wire_text)`` recorded by :meth:`send_text`.
        self.sent: list[tuple[str, str]] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self._queue.put_nowait(None)  # unblock receive_loop

    async def send_text(self, account_guid: str, text: str) -> None:
        self.sent.append((account_guid, text))

    async def receive_loop(self, on_message: Callable[[str, str], None]) -> None:
        """Process items from the injection queue.

        ``None`` in the queue triggers a simulated disconnect
        (raises :class:`ConnectionError`).
        """
        while True:
            item = await self._queue.get()
            if item is None:
                raise ConnectionError("FakeRubikaTransport: simulated disconnect")
            sender_guid, raw = item
            on_message(sender_guid, raw)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    async def inject_raw(self, sender_guid: str, raw: str) -> None:
        """Inject a raw wire-format text message."""
        await self._queue.put((sender_guid, raw))

    async def inject_msg(self, sender_guid: str, msg: BaseModel) -> None:
        """Encode *msg* via :func:`iran.contracts.encode` and inject it."""
        wire = encode(msg)
        await self.inject_raw(sender_guid, wire)

    def simulate_disconnect(self) -> None:
        """Enqueue a disconnect signal (``None``) for the receive loop."""
        self._queue.put_nowait(None)


# ---------------------------------------------------------------------------
# DI factory + legacy protocol stub
# ---------------------------------------------------------------------------


@runtime_checkable
class RubikaClientProtocol(Protocol):
    """Minimal interface expected by consumers of the Rubika client."""

    async def send(self, msg: BaseModel) -> None: ...

    async def close(self) -> None: ...


def make_rubika_client(config: IranRubikaConfig | None = None) -> IranRubikaClient:
    """DI factory used by ``iran/main.py``.

    Builds a fully configured :class:`IranRubikaClient` from *config*
    (or a fresh :class:`IranRubikaConfig` loaded from env vars).
    """
    if config is None:
        config = IranRubikaConfig()
    return IranRubikaClient(config)
