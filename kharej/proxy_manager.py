"""HTTP proxy manager for the Kharej VPS worker.

Fetches HTTP proxy lists from multiple remote URLs, validates them by
measuring actual download throughput through each proxy, then maintains a
rotating pool of working proxies.  A background asyncio task refreshes the
pool every 15 minutes automatically.  Validated proxies are persisted to disk
so the pool is available immediately after a process restart without waiting
for the first full refresh cycle.

The module exposes a process-global singleton :data:`proxy_manager` that all
downloaders should use::

    from kharej.proxy_manager import proxy_manager

    proxy_url = proxy_manager.get_proxy()       # "http://ip:port" or None
    proxy_manager.mark_proxy_failed(proxy_url)  # evict a bad proxy immediately
    await proxy_manager.start()                 # begin background refresh
    await proxy_manager.stop()                  # cancel background refresh

Proxy validation
----------------
Each candidate proxy is tested with a real download speed check:

1. HTTP GET request for a 5 MB file through the proxy using the ``requests``
   library (timeout :data:`_VALIDATE_TIMEOUT`).
2. Verify the response status is 200 OK.
3. Download :data:`_SPEEDTEST_SAMPLE_BYTES` bytes and measure throughput.
4. Proxy is accepted only when throughput ≥ :data:`_MIN_SPEED_BPS`.

Measuring actual throughput rather than just connectivity filters out proxies
that accept connections but are too slow or throttled to be useful for media
downloads.  Validation runs concurrently in a
:class:`~concurrent.futures.ThreadPoolExecutor` so it does not block the
asyncio event loop.

Disk cache
----------
After every successful refresh the validated proxy list is written atomically
to :data:`_PROXY_CACHE_FILE` (``kharej/state/proxies.json``).  On startup the
cache is loaded immediately so requests do not fail while the first background
refresh is still in progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import time
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("kharej.proxy_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Remote URLs that provide HTTP proxy lists, one entry per line.
#: Supported formats: ``host:port`` and ``http://host:port``.
#: All lists are fetched and merged before validation so the pool is as large
#: as possible.
_PROXY_LIST_URLS: list[str] = [
    (
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
        "/proxies/protocols/http/data.txt"
    ),
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",  # repo name is misleading; file contains HTTP proxies
]

#: Public HTTP speed-test server used for proxy validation.  Using a numeric
#: IPv4 address avoids any DNS lookup through the proxy under test.
_SPEEDTEST_URL: str = "http://212.183.159.230/5MB.zip"

#: Number of bytes to download when measuring proxy speed.  30 KB is enough
#: to gauge throughput without wasting bandwidth on slow proxies.
_SPEEDTEST_SAMPLE_BYTES: int = 30 * 1024  # 30 KB

#: Minimum acceptable download speed through a proxy (bytes/sec).
#: Proxies slower than this are rejected as unsuitable for media downloads.
_MIN_SPEED_BPS: float = 30 * 1024  # 30 KB/s

#: Seconds before a validation attempt is considered failed.
_VALIDATE_TIMEOUT: float = 12.0

#: How many proxies to validate concurrently.
_VALIDATE_WORKERS: int = 60

#: URL used to verify that the proxy can reach YouTube's HTTPS endpoints.
#: A successful response (any HTTP status) proves that the proxy supports
#: HTTPS CONNECT tunnelling to Google/YouTube servers.
_YOUTUBE_CHECK_URL: str = "https://www.youtube.com/generate_204"

#: Timeout (seconds) for the YouTube HTTPS connectivity check.
_YOUTUBE_CHECK_TIMEOUT: float = 10.0

#: Seconds between automatic proxy list refreshes (15 minutes).
_REFRESH_INTERVAL: float = 900.0

#: HTTP request timeout when fetching the remote proxy list (seconds).
_FETCH_TIMEOUT: float = 20.0

#: Path to the disk cache that persists validated proxies across restarts.
_PROXY_CACHE_FILE: Path = Path(__file__).parent / "state" / "proxies.json"


# ---------------------------------------------------------------------------
# Disk cache helpers
# ---------------------------------------------------------------------------


def _load_proxy_cache(path: Path) -> list[str]:
    """Load the saved proxy list from *path*; return an empty list on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str)]
    except Exception:
        pass
    return []


def _save_proxy_cache(proxies: list[str], path: Path) -> None:
    """Atomically write *proxies* to *path* as a JSON array."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".proxies_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(proxies, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Proxy validation helpers
# ---------------------------------------------------------------------------


def _http_speed_check(proxy_url: str) -> bool:
    """Return True if the HTTP proxy passes a real download speed test.

    Uses the ``requests`` library to issue an HTTP GET through *proxy_url* to
    a public speed-test file.  The proxy is accepted only when:

    * The response status is 200 OK.
    * At least :data:`_SPEEDTEST_SAMPLE_BYTES` are received and the measured
      throughput is ≥ :data:`_MIN_SPEED_BPS`.

    This approach rejects proxies that are merely reachable but too slow or
    throttled to handle media downloads.
    """
    try:
        import requests  # noqa: PLC0415  (lazy import keeps startup cost low)
    except ImportError:
        logger.error({"event": "proxy_manager.requests_missing"})
        return False

    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        with requests.get(
            _SPEEDTEST_URL,
            stream=True,
            proxies=proxies,
            timeout=_VALIDATE_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return False

            downloaded = 0
            start = time.perf_counter()
            for chunk in resp.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded >= _SPEEDTEST_SAMPLE_BYTES:
                    break

            elapsed = time.perf_counter() - start
            if elapsed < 0.01:
                # Nearly instant response — only accept if the full sample
                # arrived, so a single-byte reply can't sneak through.
                return downloaded >= _SPEEDTEST_SAMPLE_BYTES
            return downloaded / elapsed >= _MIN_SPEED_BPS

    except Exception:
        return False


def _http_youtube_check(proxy_url: str) -> bool:
    """Return True if the proxy can reach YouTube's HTTPS endpoint.

    Sends a GET request to :data:`_YOUTUBE_CHECK_URL` (a YouTube no-content
    endpoint) through *proxy_url*.  Any HTTP response — including redirects
    and 4xx status codes — is treated as success because it proves that the
    proxy can establish an HTTPS CONNECT tunnel to Google/YouTube servers.

    This catches a large class of proxies that pass the plain-HTTP speed test
    but cannot proxy HTTPS traffic (the most common cause of yt-dlp
    ``ConnectTimeoutError`` / "Unable to connect to proxy" failures).
    """
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        return True  # If requests is missing, skip the check rather than blocking all proxies

    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            _YOUTUBE_CHECK_URL,
            proxies=proxies,
            timeout=_YOUTUBE_CHECK_TIMEOUT,
            allow_redirects=False,
            stream=False,
        )
        # Any HTTP response means the proxy forwarded the request successfully.
        return resp.status_code is not None
    except Exception:
        return False


def _validate_single_proxy(proxy_url: str) -> bool:
    """Return True only if *proxy_url* passes both the speed test and the YouTube check.

    Both checks must succeed:

    * :func:`_http_speed_check` — adequate download throughput via plain HTTP.
    * :func:`_http_youtube_check` — can establish an HTTPS tunnel to YouTube.

    Requiring both filters eliminates proxies that are fast enough but cannot
    proxy HTTPS traffic, which is the primary source of real-world yt-dlp
    proxy failures.
    """
    return _http_speed_check(proxy_url) and _http_youtube_check(proxy_url)


def _parse_proxy_line(line: str) -> str | None:
    """Parse a proxy list line into an ``http://host:port`` URL or ``None``.

    Accepts both plain ``host:port`` entries and lines that already carry an
    ``http://`` (or any other ``scheme://``) prefix.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Strip any existing scheme prefix (e.g. "http://", "socks5://")
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
    return f"http://{host}:{port}"


def _fetch_proxy_list(url: str) -> list[str]:
    """Fetch raw proxy list text from *url* and return parsed ``http://`` URLs."""
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
    """Return only the *proxy_urls* that pass the speed test."""
    if not proxy_urls:
        return []

    results: list[str] = []
    lock = threading.Lock()

    def _check(proxy_url: str) -> None:
        if _validate_single_proxy(proxy_url):
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
    """Manage a pool of validated HTTP proxies with periodic auto-refresh.

    The manager is safe to use from multiple threads (downloaders call
    :meth:`get_proxy` from ``asyncio.to_thread`` workers).

    On instantiation the disk cache is loaded immediately so callers always
    have a non-empty pool even before the first background refresh completes.
    """

    def __init__(self, urls: list[str] | None = None, cache_file: Path | None = None) -> None:
        self._urls: list[str] = urls if urls is not None else list(_PROXY_LIST_URLS)
        self._cache_file: Path = cache_file or _PROXY_CACHE_FILE
        self._lock = threading.Lock()
        # Pre-populate from disk so requests succeed immediately after restart.
        cached = _load_proxy_cache(self._cache_file)
        self._working: list[str] = cached
        if cached:
            logger.info(
                {
                    "event": "proxy_manager.cache_loaded",
                    "count": len(cached),
                }
            )
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
        """Return a random working ``http://host:port`` proxy URL, or ``None``."""
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
        # Persist to disk so the pool survives a process restart.
        try:
            _save_proxy_cache(working, self._cache_file)
        except Exception as exc:
            logger.warning(
                {
                    "event": "proxy_manager.cache_save_error",
                    "error": repr(exc),
                }
            )
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
