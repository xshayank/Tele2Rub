"""Access-control layer for the Kharej VPS worker.

Persists whitelist / blocklist to ``kharej/state/access_state.json`` and
exposes a synchronous ``check_access()`` gate plus async handlers for the
four control messages:

- ``user.whitelist.add``
- ``user.whitelist.remove``
- ``user.block.add``
- ``user.block.remove``

Each handler atomically rewrites the state file (write-temp + ``os.replace``)
and sends an ``admin.ack`` back to the Iran VPS.

State schema::

    {"v": 1, "whitelist": ["<user_id>", ...], "blocklist": ["<user_id>", ...]}

Empty whitelist means *everyone* is allowed (whitelist-mode only activates
when the list is non-empty).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from kharej.contracts import (
    AccessDecision,
    AdminAck,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
)

logger = logging.getLogger("kharej.access_control")

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

_STATE_VERSION = 1
_STATE_FILE = Path(__file__).parent / "state" / "access_state.json"

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _default_state() -> dict:
    return {"v": _STATE_VERSION, "whitelist": [], "blocklist": []}


def _load_state(path: Path) -> dict:
    """Load access state from *path*; return defaults on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except Exception:
        logger.exception(
            "Failed to load access state; using defaults",
            extra={"event": "access.load_error", "path": str(path)},
        )
        return _default_state()

    if data.get("v") != _STATE_VERSION:
        logger.warning(
            "Unexpected access state version; resetting to defaults",
            extra={"event": "access.version_mismatch", "v": data.get("v")},
        )
        return _default_state()

    return data


def _save_state(state: dict, path: Path) -> None:
    """Atomically write *state* to *path* using a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".access_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# AccessControl
# ---------------------------------------------------------------------------


class AccessControl:
    """Singleton-style access-control manager for the Kharej VPS worker.

    Loads state from disk at construction time; every mutation is immediately
    persisted atomically.  Safe to call ``check_access`` from sync code;
    safe to ``await`` the handlers from async code.

    Parameters
    ----------
    state_path:
        Override the default state-file path (used in tests).
    """

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._path: Path = state_path or _STATE_FILE
        self._state: dict = _load_state(self._path)
        logger.info(
            "AccessControl loaded",
            extra={
                "event": "access.loaded",
                "whitelist_size": len(self._state.get("whitelist", [])),
                "blocklist_size": len(self._state.get("blocklist", [])),
            },
        )

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    @property
    def whitelist(self) -> list[str]:
        """Current whitelist (copy)."""
        return list(self._state.get("whitelist", []))

    @property
    def blocklist(self) -> list[str]:
        """Current blocklist (copy)."""
        return list(self._state.get("blocklist", []))

    def check_access(self, user_id: str) -> AccessDecision:
        """Return the ``AccessDecision`` for *user_id*.

        Logic (evaluated in order):

        1. If *user_id* is in the blocklist → ``block``.
        2. If the whitelist is non-empty and *user_id* is not in it →
           ``not_whitelisted``.
        3. Otherwise → ``allow``.
        """
        if user_id in self._state.get("blocklist", []):
            return AccessDecision.block
        whitelist = self._state.get("whitelist", [])
        if whitelist and user_id not in whitelist:
            return AccessDecision.not_whitelisted
        return AccessDecision.allow

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        _save_state(self._state, self._path)

    @staticmethod
    def _ack(acked_type: str) -> AdminAck:
        return AdminAck(
            ts=datetime.now(tz=timezone.utc),
            acked_type=acked_type,
            status="ok",
        )

    # ------------------------------------------------------------------
    # Control-message handlers
    # ------------------------------------------------------------------

    async def handle_whitelist_add(
        self,
        msg: UserWhitelistAdd,
        send: Callable[[BaseModel], Awaitable[None]],
    ) -> None:
        """Handle ``user.whitelist.add``: add *msg.user_id* to the whitelist."""
        user_id = msg.user_id
        wl: list[str] = self._state.setdefault("whitelist", [])
        if user_id not in wl:
            wl.append(user_id)
            self._persist()
        logger.info(
            "Whitelisted user",
            extra={"event": "access.whitelist_add", "user_id": user_id},
        )
        await send(self._ack("user.whitelist.add"))

    async def handle_whitelist_remove(
        self,
        msg: UserWhitelistRemove,
        send: Callable[[BaseModel], Awaitable[None]],
    ) -> None:
        """Handle ``user.whitelist.remove``: remove *msg.user_id* from the whitelist."""
        user_id = msg.user_id
        wl: list[str] = self._state.setdefault("whitelist", [])
        if user_id in wl:
            wl.remove(user_id)
            self._persist()
        logger.info(
            "Removed user from whitelist",
            extra={"event": "access.whitelist_remove", "user_id": user_id},
        )
        await send(self._ack("user.whitelist.remove"))

    async def handle_block_add(
        self,
        msg: UserBlockAdd,
        send: Callable[[BaseModel], Awaitable[None]],
    ) -> None:
        """Handle ``user.block.add``: add *msg.user_id* to the blocklist."""
        user_id = msg.user_id
        bl: list[str] = self._state.setdefault("blocklist", [])
        if user_id not in bl:
            bl.append(user_id)
            self._persist()
        logger.info(
            "Blocked user",
            extra={"event": "access.block_add", "user_id": user_id},
        )
        await send(self._ack("user.block.add"))

    async def handle_block_remove(
        self,
        msg: UserBlockRemove,
        send: Callable[[BaseModel], Awaitable[None]],
    ) -> None:
        """Handle ``user.block.remove``: remove *msg.user_id* from the blocklist."""
        user_id = msg.user_id
        bl: list[str] = self._state.setdefault("blocklist", [])
        if user_id in bl:
            bl.remove(user_id)
            self._persist()
        logger.info(
            "Unblocked user",
            extra={"event": "access.block_remove", "user_id": user_id},
        )
        await send(self._ack("user.block.remove"))
