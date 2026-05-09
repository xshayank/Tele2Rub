"""SOCKS5 proxy manager for the Kharej VPS worker.

Fetches SOCKS5 proxy lists from multiple remote URLs, validates them by
establishing a real SOCKS5 tunnel to YouTube and confirming an HTTP response,
then maintains a rotating pool of working proxies.  A background asyncio task
refreshes the pool every 15 minutes automatically.

The module exposes a process-global singleton :data:`proxy_manager` that all
downloaders should use::

    from kharej.proxy_manager import proxy_manager

    proxy_url = proxy_manager.get_proxy()       # "socks5://ip:port" or None
    proxy_manager.mark_proxy_failed(proxy_url)  # evict a bad proxy immediately
    await proxy_manager.start()                 # begin background refresh
    await proxy_manager.stop()                  # cancel background refresh

Proxy validation
----------------
Each candidate proxy is tested end-to-end:

1. TCP connection to the proxy host/port.
2. SOCKS5 no-auth greeting (RFC 1928).
3. SOCKS5 CONNECT to ``youtube.com:80`` using a domain-name address (ATYP 0x03)
   so the proxy must resolve and reach the real target.
4. HTTP ``HEAD / HTTP/1.0`` request sent through the established tunnel.
5. Proxy is accepted only when the response starts with ``HTTP/``.

This ensures every proxy in the pool can actually reach YouTube/media hosts
before being used for video or music downloads.  Validation runs concurrently
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

#: Remote URLs that each contain one ``ip:port`` SOCKS5 proxy entry per line.
#: All lists are fetched and merged before validation so the pool is as large
#: as possible.
_PROXY_LIST_URLS: list[str] = [
    (
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
        "/proxies/protocols/socks5/data.txt"
    ),
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/refs/heads/master/socks5.txt",
]

#: Target host used for end-to-end SOCKS5 + HTTP validation.
#: Using ``youtube.com`` ensures proxies can reach the primary media platform.
_VALIDATE_HOST_BYTES: bytes = b"youtube.com"
_VALIDATE_PORT: int = 80

#: Seconds before a validation attempt is considered failed.
_VALIDATE_TIMEOUT: float = 10.0

#: How many proxies to validate concurrently.
_VALIDATE_WORKERS: int = 50

#: Seconds between automatic proxy list refreshes (15 minutes).
_REFRESH_INTERVAL: float = 900.0

#: HTTP request timeout when fetching the remote proxy list (seconds).
_FETCH_TIMEOUT: float = 20.0


# ---------------------------------------------------------------------------
# Proxy validation helpers
# ---------------------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, returning fewer only on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _socks5_check(host: str, port: int) -> bool:
    """Return True if the proxy at *host*:*port* can reach YouTube via HTTP.

    Full end-to-end validation (RFC 1928):

    1. TCP connect to the proxy.
    2. SOCKS5 no-auth greeting.
    3. SOCKS5 CONNECT to ``youtube.com:80`` (domain-name ATYP so the proxy
       must resolve and route to the real host).
    4. Drain the variable-length CONNECT reply.
    5. Send ``HEAD / HTTP/1.0`` and verify the response starts with ``HTTP/``.

    Only proxies that pass all five steps are kept in the working pool.
    """
    try:
        with socket.create_connection((host, port), timeout=_VALIDATE_TIMEOUT) as s:
            s.settimeout(_VALIDATE_TIMEOUT)

            # Step 1 – greeting: no-auth only
            s.sendall(b"\x05\x01\x00")
            resp = _recv_exact(s, 2)
            if len(resp) < 2 or resp[0] != 0x05 or resp[1] != 0x00:
                return False

            # Step 2 – CONNECT using domain name (ATYP=0x03)
            target = _VALIDATE_HOST_BYTES
            request = (
                b"\x05\x01\x00\x03"
                + bytes([len(target)])
                + target
                + struct.pack("!H", _VALIDATE_PORT)
            )
            s.sendall(request)

            # Step 3 – read CONNECT reply header (4 bytes: VER, REP, RSV, ATYP)
            reply_hdr = _recv_exact(s, 4)
            if len(reply_hdr) < 4 or reply_hdr[1] != 0x00:
                return False

            # Step 4 – drain the bound address so the socket is ready for data
            atyp = reply_hdr[3]
            if atyp == 0x01:       # IPv4: 4 bytes addr + 2 bytes port
                _recv_exact(s, 6)
            elif atyp == 0x03:     # domain: 1 byte len + N bytes + 2 bytes port
                dlen_buf = _recv_exact(s, 1)
                if dlen_buf:
                    _recv_exact(s, dlen_buf[0] + 2)
            elif atyp == 0x04:     # IPv6: 16 bytes addr + 2 bytes port
                _recv_exact(s, 18)

            # Step 5 – make a real HTTP request through the tunnel
            s.sendall(
                b"HEAD / HTTP/1.0\r\n"
                b"Host: youtube.com\r\n"
                b"User-Agent: curl/7.68.0\r\n"
                b"\r\n"
            )
            http_resp = s.recv(16)
            return http_resp.startswith(b"HTTP/")
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


def _fetch_all_proxy_lists(urls: list[str]) -> list[str]:
    """Fetch and deduplicate proxies from all *urls*."""
    seen: set[str] = set()
    combined: list[str] = []
    for url in urls:
        for proxy in _fetch_proxy_list(url):
            if proxy not in seen:
                seen.add(proxy)
                combined.append(proxy)
    logger.info(
        {
            "event": "proxy_manager.fetch_all_done",
            "sources": len(urls),
            "total_candidates": len(combined),
        }
    )
    return combined


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
    """Manage a pool of validated SOCKS5 proxies with periodic auto-refresh.

    The manager is safe to use from multiple threads (downloaders call
    :meth:`get_proxy` from ``asyncio.to_thread`` workers).
    """

    def __init__(self, urls: list[str] | None = None) -> None:
        self._urls: list[str] = urls if urls is not None else list(_PROXY_LIST_URLS)
        self._lock = threading.Lock()
        self._working: list[str] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Fetch and validate the proxy list immediately, then schedule periodic refresh."""
        # Initial fetch in a background thread so we don't block the event loop.
        await asyncio.to_thread(self._refresh)
        # Schedule recurring refresh
        self._task = asyncio.create_task(self._refresh_loop())
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

    def mark_proxy_failed(self, proxy_url: str) -> None:
        """Evict *proxy_url* from the working pool immediately.

        Call this when a download fails with a proxy-related error so that
        subsequent requests from other jobs do not reuse the broken proxy.
        If the pool becomes empty after the eviction, a background refresh is
        triggered automatically.
        """
        with self._lock:
            try:
                self._working.remove(proxy_url)
            except ValueError:
                return  # already removed, nothing to do
            remaining = len(self._working)

        logger.warning(
            {
                "event": "proxy_manager.proxy_evicted",
                "proxy": proxy_url,
                "remaining": remaining,
            }
        )

        if remaining == 0:
            logger.warning(
                {
                    "event": "proxy_manager.pool_empty",
                    "msg": "No working proxies left; triggering background refresh",
                }
            )
            # Fire-and-forget refresh from a thread so we don't block the caller.
            threading.Thread(target=self._refresh, daemon=True, name="proxy-refresh").start()

    def working_count(self) -> int:
        """Return the number of currently validated working proxies."""
        with self._lock:
            return len(self._working)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Synchronously fetch and validate the proxy list, then update the pool."""
        logger.info({"event": "proxy_manager.refresh_start", "urls": self._urls})
        candidates = _fetch_all_proxy_lists(self._urls)
        if not candidates:
            logger.warning(
                {
                    "event": "proxy_manager.empty_list",
                    "msg": "All proxy lists are empty; keeping existing pool",
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
