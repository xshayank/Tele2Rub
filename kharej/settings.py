"""Runtime settings for the Kharej VPS worker.

Loads key-value settings from two sources (lower priority first):

1. **Environment variables** — any ``KHAREJ_*`` env var is imported as a
   setting with its prefix stripped and key lower-cased
   (``KHAREJ_MAX_PARALLEL=4`` → ``"max_parallel": "4"``).
2. **Disk settings** — ``kharej/state/kharej_settings.json`` is loaded and
   merged *on top of* the env-var baseline (disk values win).

The ``set(key, value)`` method persists the new value to disk atomically
(write-temp + ``os.replace``) and immediately updates the in-memory merged
view.

Handles the ``admin.settings.update`` control message: applies each key in
``msg.settings``, persists, then sends an ``admin.ack`` with the new
effective config.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kharej.contracts import AdminAck, AdminSettingsUpdate

logger = logging.getLogger("kharej.settings")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SETTINGS_FILE = Path(__file__).parent / "state" / "kharej_settings.json"
_ENV_PREFIX = "KHAREJ_"

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_env_defaults() -> dict[str, Any]:
    """Return a dict of settings derived from ``KHAREJ_*`` env vars."""
    defaults: dict[str, Any] = {}
    for k, v in os.environ.items():
        if k.startswith(_ENV_PREFIX):
            key = k[len(_ENV_PREFIX):].lower()
            defaults[key] = v
    return defaults


def _load_disk(path: Path) -> dict[str, Any]:
    """Load the JSON settings file; return ``{}`` on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning(
                "Settings file is not a JSON object; ignoring",
                extra={"event": "settings.invalid_format", "path": str(path)},
            )
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception(
            "Failed to load settings file; using env defaults only",
            extra={"event": "settings.load_error", "path": str(path)},
        )
        return {}


def _save_disk(settings: dict[str, Any], path: Path) -> None:
    """Atomically write *settings* to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".kharej_settings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# KharejSettings
# ---------------------------------------------------------------------------


class KharejSettings:
    """Runtime settings store for the Kharej VPS worker.

    Parameters
    ----------
    state_path:
        Override the default settings-file path (used in tests).
    """

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._path: Path = state_path or _SETTINGS_FILE
        self._env: dict[str, Any] = _load_env_defaults()
        self._disk: dict[str, Any] = _load_disk(self._path)
        # Disk values override env defaults.
        self._merged: dict[str, Any] = {**self._env, **self._disk}
        logger.info(
            "KharejSettings loaded",
            extra={
                "event": "settings.loaded",
                "env_keys": len(self._env),
                "disk_keys": len(self._disk),
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not set."""
        return self._merged.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        """Return the value for *key* as an integer, or *default* if not set or not convertible."""
        value = self._merged.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Return the value for *key* as a boolean, or *default* if not set.

        Recognises truthy string literals ``"1"``, ``"true"``, ``"yes"``, ``"on"``
        (case-insensitive) as ``True``; all other strings are ``False``.
        """
        value = self._merged.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default

    def set(self, key: str, value: Any) -> None:
        """Persist *key*/*value* to disk and update the in-memory view."""
        self._disk[key] = value
        self._merged[key] = value
        _save_disk(self._disk, self._path)
        logger.debug(
            "Setting updated",
            extra={"event": "settings.set", "key": key},
        )

    def effective_config(self) -> dict[str, Any]:
        """Return a snapshot of all currently effective settings."""
        return dict(self._merged)

    # ------------------------------------------------------------------
    # Control-message handler
    # ------------------------------------------------------------------

    async def handle_settings_update(
        self,
        msg: AdminSettingsUpdate,
        send: Callable[[BaseModel], Awaitable[None]],
    ) -> None:
        """Handle ``admin.settings.update``: apply each key and ack."""
        for key, value in msg.settings.items():
            self.set(key, value)
        logger.info(
            "Applied settings update",
            extra={"event": "settings.update_applied", "keys": list(msg.settings.keys())},
        )
        await send(
            AdminAck(
                ts=datetime.now(tz=timezone.utc),
                acked_type="admin.settings.update",
                status="ok",
                effective_config=self.effective_config(),
            )
        )
