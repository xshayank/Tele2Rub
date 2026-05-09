"""SOCKS5 proxy manager for the Kharej VPS worker.

Fetches a list of SOCKS5 proxies from a remote URL, validates them using a
raw SOCKS5 handshake, and maintains a rotating pool of working proxies.
A background asyncio task refreshes the pool every hour automatically.

The module exposes a process-global singleton :data:`proxy_manager` that all
downloaders should use::

    from kharej.proxy_manager import proxy_manager

    proxy_url = proxy_manager.get_proxy()   # "socks5://ip:port" or None
    await proxy_manager.start()             # begin background refresh
    await proxy_manager.stop()              # cancel background refresh

Proxy validation
----------------
Each candidate proxy is tested by attempting a no-authentication SOCKS5
handshake (RFC 1928) to the Cloudflare resolver at ``1.1.1.1:80``.  Only
proxies that complete the handshake successfully within
:data:`_VALIDATE_TIMEOUT` seconds are kept.  Validation runs concurrently
in a :class:`~concurrent.futures.ThreadPoolExecutor` so it does not block
the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import threading
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("kharej.proxy_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Remote URL that contains one ``ip:port`` SOCKS5 proxy entry per line.
_PROXY_LIST_URL: str = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
    "/proxies/protocols/socks5/data.txt"
)

#: Target used for SOCKS5 handshake validation (Cloudflare DNS resolver).
_VALIDATE_HOST: str = "1.1.1.1"
_VALIDATE_PORT: int = 80

#: Seconds before a validation attempt is considered failed.
_VALIDATE_TIMEOUT: float = 8.0

#: How many proxies to validate concurrently.
_VALIDATE_WORKERS: int = 20

#: Seconds between automatic proxy list refreshes (1 hour).
_REFRESH_INTERVAL: float = 3600.0

#: HTTP request timeout when fetching the remote proxy list (seconds).
_FETCH_TIMEOUT: float = 20.0


# ---------------------------------------------------------------------------
# Proxy validation
# ---------------------------------------------------------------------------


def _socks5_check(host: str, port: int) -> bool:
    """Return True if a no-auth SOCKS5 handshake to the validation target succeeds.

    Implements the minimal SOCKS5 handshake (RFC 1928):
    1. Client → Server: ``\\x05\\x01\\x00`` (SOCKS5, 1 method, no-auth)
    2. Server → Client: ``\\x05\\x00`` (SOCKS5, accept no-auth)
    3. Client → Server: CONNECT request to :data:`_VALIDATE_HOST`::data:`_VALIDATE_PORT`
    4. Server → Client: reply with ``reply[1] == 0x00`` (success)
    """
    try:
        with socket.create_connection((host, port), timeout=_VALIDATE_TIMEOUT) as s:
            # Greeting
            s.sendall(b"\x05\x01\x00")
            resp = s.recv(2)
            if len(resp) < 2 or resp[0] != 0x05 or resp[1] != 0x00:
                return False

            # CONNECT request: VER=5, CMD=CONNECT, RSV=0, ATYP=IPv4
            target_ip = socket.inet_aton(_VALIDATE_HOST)
            request = (
                b"\x05\x01\x00\x01"
                + target_ip
                + struct.pack("!H", _VALIDATE_PORT)
            )
            s.sendall(request)
            reply = s.recv(10)
            return len(reply) >= 2 and reply[1] == 0x00
    except Exception:
        return False


def _parse_proxy_line(line: str) -> str | None:
    """Parse a proxy list line into a ``socks5://host:port`` URL or ``None``."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Strip protocol prefix if present (e.g. "socks5://")
    if "://" in line:
        line = line.split("://", 1)[1]
    parts = line.rsplit(":", 1)
    if len(parts) != 2:
        return None
    host, port_str = parts
    host = host.strip()
    port_str = port_str.strip()
    if not host or not port_str.isdigit():
        return None
    port = int(port_str)
    if not (1 <= port <= 65535):
        return None
    return f"socks5://{host}:{port}"


def _fetch_proxy_list(url: str) -> list[str]:
    """Fetch raw proxy list text from *url* and return parsed ``socks5://`` URLs."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RubeTunes-ProxyManager/1.0"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning(
            {
                "event": "proxy_manager.fetch_failed",
                "url": url,
                "error": repr(exc),
            }
        )
        return []

    proxies: list[str] = []
    for line in raw.splitlines():
        parsed = _parse_proxy_line(line)
        if parsed:
            proxies.append(parsed)

    logger.info(
        {
            "event": "proxy_manager.fetch_done",
            "url": url,
            "candidates": len(proxies),
        }
    )
    return proxies


def _validate_proxies(proxy_urls: Sequence[str]) -> list[str]:
    """Return only the *proxy_urls* that pass :func:`_socks5_check`."""
    if not proxy_urls:
        return []

    results: list[str] = []
    lock = threading.Lock()

    def _check(proxy_url: str) -> None:
        # proxy_url is "socks5://host:port"
        addr = proxy_url.split("://", 1)[1]
        host, port_str = addr.rsplit(":", 1)
        port = int(port_str)
        if _socks5_check(host, port):
            with lock:
                results.append(proxy_url)

    with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as executor:
        list(executor.map(_check, proxy_urls))

    logger.info(
        {
            "event": "proxy_manager.validation_done",
            "total": len(proxy_urls),
            "working": len(results),
        }
    )
    return results


# ---------------------------------------------------------------------------
# ProxyManager
# ---------------------------------------------------------------------------


class ProxyManager:
    """Manage a pool of validated SOCKS5 proxies with hourly auto-refresh.

    The manager is safe to use from multiple threads (downloaders call
    :meth:`get_proxy` from ``asyncio.to_thread`` workers).
    """

    def __init__(self, url: str = _PROXY_LIST_URL) -> None:
        self._url = url
        self._lock = threading.Lock()
        self._working: list[str] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Fetch and validate the proxy list immediately, then schedule hourly refresh."""
        # Initial fetch in a background thread so we don't block the event loop.
        await asyncio.to_thread(self._refresh)
        # Schedule recurring refresh
        self._task = asyncio.get_event_loop().create_task(self._refresh_loop())
        logger.info({"event": "proxy_manager.started", "interval_sec": _REFRESH_INTERVAL})

    async def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info({"event": "proxy_manager.stopped"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_proxy(self) -> str | None:
        """Return a random working ``socks5://host:port`` proxy URL, or ``None``."""
        with self._lock:
            if not self._working:
                return None
            return random.choice(self._working)

    def working_count(self) -> int:
        """Return the number of currently validated working proxies."""
        with self._lock:
            return len(self._working)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Synchronously fetch and validate the proxy list, then update the pool."""
        logger.info({"event": "proxy_manager.refresh_start", "url": self._url})
        candidates = _fetch_proxy_list(self._url)
        if not candidates:
            logger.warning(
                {
                    "event": "proxy_manager.empty_list",
                    "msg": "Proxy list is empty; keeping existing pool",
                }
            )
            return
        working = _validate_proxies(candidates)
        with self._lock:
            self._working = working
        logger.info(
            {
                "event": "proxy_manager.refresh_done",
                "working": len(working),
            }
        )

    async def _refresh_loop(self) -> None:
        """Background task: refresh the proxy pool every :data:`_REFRESH_INTERVAL` seconds."""
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            try:
                await asyncio.to_thread(self._refresh)
            except Exception as exc:
                logger.warning(
                    {
                        "event": "proxy_manager.refresh_error",
                        "error": repr(exc),
                    }
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Global proxy manager instance used by all downloaders.
proxy_manager: ProxyManager = ProxyManager()
