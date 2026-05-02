"""Stub downloader for Step 6 lifecycle smoke testing.

This downloader is built in by default so the worker can round-trip
``job.accepted`` → ``job.failed: not_implemented`` without any real downloader.
Steps 7/8/9 replace it with real platform handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from kharej.contracts import S2ObjectRef
    from kharej.progress_reporter import ProgressReporter
    from kharej.s2_client import S2Client
    from kharej.settings import KharejSettings


class StubDownloader:
    """Stub platform — raises :exc:`NotImplementedError` immediately."""

    platform: ClassVar[str] = "stub"

    async def run(
        self,
        job: object,
        *,
        s2: "S2Client",
        progress: "ProgressReporter",
        settings: "KharejSettings",
    ) -> "list[S2ObjectRef]":
        raise NotImplementedError("stub platform — Step 6 lifecycle smoke only")
