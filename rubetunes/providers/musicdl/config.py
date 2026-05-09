from __future__ import annotations

"""Configuration loader for the musicdl provider.

Reads environment variables and builds the keyword-argument dicts expected
by ``musicdl.musicdl.MusicClient``.

Environment variables
---------------------
MUSICDL_DOWNLOAD_DIR
    Directory where musicdl saves downloaded files.
    Defaults to ``<repo-root>/downloads/musicdl``.
MUSICDL_DEFAULT_SOURCES
    Comma-separated list of musicdl source client names.
    Defaults to the upstream DEFAULT_MUSIC_SOURCES list.
MUSICDL_PROXY
    Optional HTTP/HTTPS proxy URL applied to all musicdl requests
    (e.g. ``http://user:pass@host:port``).
MUSICDL_MAX_RETRIES
    Per-request retry count for every musicdl source.  Defaults to ``1``
    (lower than musicdl's upstream default of 3 for faster failover).
    Set to ``0`` or a negative number to use the default.
MUSICDL_CONNECT_TIMEOUT
    Seconds to wait for a TCP connection to a musicdl source endpoint.
    Defaults to ``5``.  Set to ``0`` or a negative number to use the default.
MUSICDL_READ_TIMEOUT
    Seconds to wait for the response body from a musicdl source endpoint.
    Defaults to ``15``.  Set to ``0`` or a negative number to use the default.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "MUSICDL_DOWNLOAD_DIR",
    "MUSICDL_DEFAULT_SOURCES",
    "MUSICDL_PROXY",
    "MUSICDL_MAX_RETRIES",
    "MUSICDL_CONNECT_TIMEOUT",
    "MUSICDL_READ_TIMEOUT",
    "build_init_cfg",
    "build_requests_overrides",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_positive_int(env_var: str, default: int) -> int:
    """Read *env_var* as a positive integer, falling back to *default* on error."""
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning(
            "MUSICDL: invalid value %r for %s (must be a positive integer); using default %d",
            raw,
            env_var,
            default,
        )
        return default
    if val <= 0:
        log.warning(
            "MUSICDL: invalid value %r for %s (must be > 0); using default %d",
            raw,
            env_var,
            default,
        )
        return default
    return val


def _parse_positive_float(env_var: str, default: float) -> float:
    """Read *env_var* as a positive float, falling back to *default* on error."""
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        log.warning(
            "MUSICDL: invalid value %r for %s (must be a positive number); using default %.1f",
            raw,
            env_var,
            default,
        )
        return default
    if val <= 0:
        log.warning(
            "MUSICDL: invalid value %r for %s (must be > 0); using default %.1f",
            raw,
            env_var,
            default,
        )
        return default
    return val


# ---------------------------------------------------------------------------
# Resolved configuration values
# ---------------------------------------------------------------------------

_BASE_DOWNLOADS = Path(os.getenv("MUSICDL_DOWNLOAD_DIR", "")).resolve() or (
    Path(__file__).resolve().parent.parent.parent.parent / "downloads" / "musicdl"
)
MUSICDL_DOWNLOAD_DIR: Path = _BASE_DOWNLOADS

_raw_sources = os.getenv("MUSICDL_DEFAULT_SOURCES", "").strip()
MUSICDL_DEFAULT_SOURCES: list[str] = (
    [s.strip() for s in _raw_sources.split(",") if s.strip()] if _raw_sources else []
)
"""Empty list means: use musicdl's own DEFAULT_MUSIC_SOURCES."""

MUSICDL_PROXY: str | None = os.getenv("MUSICDL_PROXY", "").strip() or None

MUSICDL_MAX_RETRIES: int = _parse_positive_int("MUSICDL_MAX_RETRIES", 1)
"""Per-request retry count for every musicdl source (default: 1)."""

MUSICDL_CONNECT_TIMEOUT: float = _parse_positive_float("MUSICDL_CONNECT_TIMEOUT", 5.0)
"""Seconds to wait for TCP connect to a musicdl source (default: 5)."""

MUSICDL_READ_TIMEOUT: float = _parse_positive_float("MUSICDL_READ_TIMEOUT", 15.0)
"""Seconds to wait for response body from a musicdl source (default: 15)."""


# ---------------------------------------------------------------------------
# Config dict builders
# ---------------------------------------------------------------------------


def build_init_cfg(source: str) -> dict:
    """Return the ``init_music_clients_cfg`` entry for a single source."""
    MUSICDL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cfg: dict = {
        "work_dir": str(MUSICDL_DOWNLOAD_DIR / source),
        "disable_print": True,
        "auto_set_proxies": False,
        "random_update_ua": False,
        "max_retries": MUSICDL_MAX_RETRIES,
    }
    return cfg


def build_requests_overrides(proxy: str | None = None) -> dict:
    """Return a ``requests_overrides`` dict suitable for MusicClient.

    Always includes a ``timeout`` tuple ``(connect, read)`` so musicdl
    sources never hang indefinitely on unresponsive endpoints.  If
    *proxy* is provided it takes precedence over the ``MUSICDL_PROXY``
    environment variable.  Either way, a ``proxies`` key is included when
    a proxy URL is available.
    """
    effective_proxy = proxy or MUSICDL_PROXY
    overrides: dict = {
        "timeout": (MUSICDL_CONNECT_TIMEOUT, MUSICDL_READ_TIMEOUT),
    }
    if effective_proxy:
        overrides["proxies"] = {"http": effective_proxy, "https": effective_proxy}
    return overrides
