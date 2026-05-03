# -*- coding: utf-8 -*-
"""
Tests for kharej.downloaders.batch — covering the empty track_ids fallback
and the _build_track_urls helper.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kharej.downloaders.batch import _build_track_urls  # noqa: E402


# ===========================================================================
# _build_track_urls unit tests
# ===========================================================================

class TestBuildTrackUrls:
    def test_spotify_bare_ids_become_track_urls(self):
        result = _build_track_urls(
            "spotify",
            ["abc123", "def456"],
            "https://open.spotify.com/playlist/xyz",
        )
        assert result == [
            "https://open.spotify.com/track/abc123",
            "https://open.spotify.com/track/def456",
        ]

    def test_spotify_full_https_urls_pass_through_unchanged(self):
        full_url = "https://open.spotify.com/playlist/myplaylist"
        result = _build_track_urls("spotify", [full_url], "https://open.spotify.com/album/x")
        assert result == [full_url]

    def test_spotify_mixed_ids_and_urls(self):
        full_url = "https://open.spotify.com/track/zzz"
        result = _build_track_urls("spotify", ["bareId", full_url], "https://example.com")
        assert result == [
            "https://open.spotify.com/track/bareId",
            full_url,
        ]

    def test_youtube_bare_ids_become_youtu_be_urls(self):
        result = _build_track_urls("youtube", ["dQw4w9WgXcQ"], "https://youtube.com/playlist?list=x")
        assert result == ["https://youtu.be/dQw4w9WgXcQ"]

    def test_youtube_full_urls_pass_through(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = _build_track_urls("youtube", [url], "https://youtube.com/playlist")
        assert result == [url]

    def test_unknown_platform_passthrough(self):
        result = _build_track_urls("soundcloud", ["sc_track_url"], "https://soundcloud.com/x")
        assert result == ["sc_track_url"]

    def test_empty_track_ids_returns_empty(self):
        assert _build_track_urls("spotify", [], "https://open.spotify.com/playlist/x") == []
        assert _build_track_urls("youtube", [], "https://youtu.be/x") == []


# ===========================================================================
# BatchDownloader.run — empty track_ids fallback to job.url
# ===========================================================================

def _make_job(
    platform: str = "spotify",
    url: str = "https://open.spotify.com/playlist/testplaylist",
    track_ids: list[str] | None = None,
    total_tracks: int | None = None,
):
    """Build a minimal Job fixture using MagicMock to avoid full contract validation."""
    payload = MagicMock()
    payload.collection_name = "Test Playlist"
    payload.track_ids = track_ids
    payload.total_tracks = total_tracks

    job = MagicMock()
    job.job_id = "741a55cb-0000-0000-0000-000000000000"
    job.user_id = "user1"
    job.platform = platform
    job.url = url
    job.quality = "mp3"
    job.job_type = "batch"
    job.payload = payload
    return job


def _make_settings(concurrency: int = 1) -> MagicMock:
    settings = MagicMock()
    settings.get_int.side_effect = lambda key, default=None: {
        "download_concurrency": concurrency,
        "zip_split_threshold_mb": 200,
    }.get(key, default)
    settings.get_bool.return_value = False
    return settings


class TestBatchDownloaderEmptyTrackIds:
    """When track_ids is empty the downloader must use job.url."""

    def test_empty_track_ids_calls_downloader_with_job_url(self, tmp_path):
        """track_ids=[] → BatchDownloader resolves track_ids to [job.url] and passes it to the per-track downloader."""
        from kharej.downloaders.batch import BatchDownloader

        job_url = "https://www.youtube.com/playlist?list=PLabc"
        job = _make_job(platform="youtube", url=job_url, track_ids=[])

        # We'll record which track_url was passed to the per-track downloader
        called_with: list[str] = []

        mock_track_dl = MagicMock()

        async def fake_per_track_run(track_job, *, s2, progress, settings):
            called_with.append(track_job.url)
            fake_file = tmp_path / "track.mp3"
            fake_file.write_bytes(b"audio")
            ref = MagicMock()
            ref.key = str(fake_file)
            return [ref]

        mock_track_dl.run = fake_per_track_run

        downloader = BatchDownloader(per_track_downloaders={"youtube": mock_track_dl})

        s2 = MagicMock()
        uploaded_ref = MagicMock()
        s2.upload_file.return_value = uploaded_ref

        progress = MagicMock()
        progress.report_progress = AsyncMock()

        settings = _make_settings()

        async def run():
            with patch("kharej.downloaders.batch._split_zip_from_files", None):
                return await downloader.run(job, s2=s2, progress=progress, settings=settings)

        result = asyncio.run(run())

        # The downloader should have been called exactly once with the playlist URL
        assert called_with == [job_url], (
            f"Expected downloader called with [{job_url!r}], got {called_with!r}"
        )
        assert result == [uploaded_ref]

    def test_none_track_ids_also_falls_back_to_job_url(self, tmp_path):
        """track_ids=None → same fallback behaviour as track_ids=[]."""
        from kharej.downloaders.batch import BatchDownloader

        job_url = "https://www.youtube.com/playlist?list=PLxyz"
        job = _make_job(platform="youtube", url=job_url, track_ids=None)

        called_with: list[str] = []
        mock_track_dl = MagicMock()

        async def fake_per_track_run(track_job, *, s2, progress, settings):
            called_with.append(track_job.url)
            fake_file = tmp_path / "track.mp3"
            fake_file.write_bytes(b"audio")
            ref = MagicMock()
            ref.key = str(fake_file)
            return [ref]

        mock_track_dl.run = fake_per_track_run

        downloader = BatchDownloader(per_track_downloaders={"youtube": mock_track_dl})

        s2 = MagicMock()
        s2.upload_file.return_value = MagicMock()

        progress = MagicMock()
        progress.report_progress = AsyncMock()

        settings = _make_settings()

        async def run():
            with patch("kharej.downloaders.batch._split_zip_from_files", None):
                return await downloader.run(job, s2=s2, progress=progress, settings=settings)

        asyncio.run(run())

        assert called_with == [job_url]

    def test_non_empty_track_ids_are_not_replaced(self, tmp_path):
        """When track_ids is populated it must NOT be replaced with job.url."""
        from kharej.downloaders.batch import BatchDownloader

        job_url = "https://www.youtube.com/playlist?list=PLabc"
        track_id = "dQw4w9WgXcQ"
        job = _make_job(platform="youtube", url=job_url, track_ids=[track_id])

        called_with: list[str] = []
        mock_track_dl = MagicMock()

        async def fake_per_track_run(track_job, *, s2, progress, settings):
            called_with.append(track_job.url)
            fake_file = tmp_path / "track.mp3"
            fake_file.write_bytes(b"audio")
            ref = MagicMock()
            ref.key = str(fake_file)
            return [ref]

        mock_track_dl.run = fake_per_track_run

        downloader = BatchDownloader(per_track_downloaders={"youtube": mock_track_dl})

        s2 = MagicMock()
        s2.upload_file.return_value = MagicMock()

        progress = MagicMock()
        progress.report_progress = AsyncMock()

        settings = _make_settings()

        async def run():
            with patch("kharej.downloaders.batch._split_zip_from_files", None):
                return await downloader.run(job, s2=s2, progress=progress, settings=settings)

        asyncio.run(run())

        # The track_id is a bare YouTube ID → should be converted to a youtu.be URL
        expected_url = f"https://youtu.be/{track_id}"
        assert called_with == [expected_url]
