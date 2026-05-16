from __future__ import annotations

import asyncio
import time
from pathlib import Path

from kharej.proxy_manager import ProxyManager, _ProxyRecord


class _RefillingProxyManager(ProxyManager):
    def __init__(self, cache_file: Path, *, delay: float = 0.0) -> None:
        super().__init__(sources=[], cache_file=cache_file)
        self.delay = delay
        self.refresh_calls = 0

    def _refresh(self) -> None:
        self.refresh_calls += 1
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self._proxy_records = {
                "http://127.0.0.2:8080": _ProxyRecord(speed_bps=100_000),
            }
            self._working = ["http://127.0.0.2:8080"]


def test_scan_and_get_proxy_refills_after_all_proxies_evicted(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json")
    with mgr._lock:
        mgr._proxy_records = {"http://127.0.0.1:8080": _ProxyRecord(speed_bps=100_000)}
        mgr._working = ["http://127.0.0.1:8080"]

    mgr.mark_proxy_failed("http://127.0.0.1:8080")

    proxy = asyncio.run(mgr.scan_and_get_proxy())

    assert proxy == "http://127.0.0.2:8080"
    assert mgr.working_count() == 1
    assert mgr.refresh_calls == 1


def test_concurrent_empty_pool_requests_share_one_refill(tmp_path: Path) -> None:
    mgr = _RefillingProxyManager(tmp_path / "proxies.json", delay=0.05)

    async def _run() -> list[str | None]:
        return await asyncio.gather(*(mgr.scan_and_get_proxy() for _ in range(5)))

    proxies = asyncio.run(_run())

    assert proxies == ["http://127.0.0.2:8080"] * 5
    assert mgr.refresh_calls == 1


def test_freeproxy_source_list_includes_recent_http_sources(tmp_path: Path) -> None:
    mgr = ProxyManager(cache_file=tmp_path / "proxies.json")

    assert "GeonixProxiedSession" in mgr._sources
    assert "ProxyVerityProxiedSession" in mgr._sources
    assert "PubProxyProxiedSession" in mgr._sources
    assert "FloppyDataProxiedSession" in mgr._sources
