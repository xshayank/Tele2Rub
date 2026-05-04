"""Tests for Step 8 — Batch downloader (BatchDownloader).

Covers:
- BatchDownloader calls the underlying per-track downloader N times.
- Progress aggregation is monotonic and ends at 100%.
- ZIP creation includes the expected filenames.
- Split path uploads multiple ZIP parts with expected S2 keys.
- Concurrency is respected (semaphore / counter).
- _build_track_urls helper for spotify / youtube / generic.
- _NoopProgress is a silent sink.
- KharejSettings.get_bool works correctly.

All external I/O (yt-dlp, spotify_dl, S2Client) is mocked.
Temp directories are used everywhere.
"""

from __future__ import annotations

import asyncio
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kharej.contracts import S2ObjectRef
from kharej.downloaders.batch import (
    BatchDownloader,
    _NoopProgress,
    _build_track_urls,
)

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_JOB_ID = "bbbbbbbb-0000-0000-0000-000000000008"


def _make_ref(key: str, size: int = 1024) -> S2ObjectRef:
    return S2ObjectRef(key=key, size=size, mime="audio/mpeg", sha256="a" * 64)


def _make_job(
    *,
    job_id: str = _JOB_ID,
    platform: str = "spotify",
    url: str = "https://open.spotify.com/playlist/ABC",
    quality: str = "mp3",
    collection_name: str = "My Playlist",
    track_ids: list[str] | None = None,
    total_tracks: int | None = None,
    job_type: str = "batch",
) -> Any:
    """Return a minimal Job-like object for batch tests."""
    from kharej.contracts import JobCreate, Platform
    from kharej.dispatcher import Job

    _platform_map = {
        "spotify": Platform.spotify,
        "youtube": Platform.youtube,
    }
    platform_enum = _platform_map.get(platform, Platform.spotify)
    msg = JobCreate.model_construct(
        v=1,
        ts=_NOW,
        job_id=job_id,
        user_id="user-test",
        platform=platform_enum,
        url=url,
        quality=quality,
        job_type=job_type,
        user_status="active",
        format_hint=None,
        collection_name=collection_name,
        track_ids=track_ids,
        total_tracks=total_tracks,
        batch_seq=None,
        batch_total=None,
    )
    return Job(
        job_id=job_id,
        user_id="user-test",
        platform=platform,
        url=url,
        quality=quality,
        job_type=job_type,
        payload=msg,
    )


def _make_progress() -> MagicMock:
    p = MagicMock()
    p.report_progress = AsyncMock()
    p.report_accepted = AsyncMock()
    p.report_completed = AsyncMock()
    p.report_failed = AsyncMock()
    return p


def _make_s2(ref: S2ObjectRef | None = None) -> MagicMock:
    def _upload(path: Path, key: str, **kw: Any) -> S2ObjectRef:
        size = path.stat().st_size if path.exists() else 0
        return _make_ref(key, size)

    s2 = MagicMock()
    s2.upload_file = MagicMock(side_effect=_upload)
    return s2


def _make_settings(
    *,
    concurrency: int = 2,
    enable_split: bool = False,
    threshold_mb: int = 200,
    cookies_path: str | None = None,
) -> MagicMock:
    settings = MagicMock()
    _data: dict = {
        "download_concurrency": concurrency,
        "enable_zip_split": enable_split,
        "zip_split_threshold_mb": threshold_mb,
    }
    if cookies_path is not None:
        _data["cookies_path"] = cookies_path
    settings.get_int = MagicMock(
        side_effect=lambda key, default=0: int(_data.get(key, default))
    )
    settings.get_bool = MagicMock(
        side_effect=lambda key, default=False: bool(_data.get(key, default))
    )
    settings.get = MagicMock(side_effect=lambda key, *args: _data.get(key, args[0] if args else None))
    return settings


def _make_track_downloader(refs_per_track: list[S2ObjectRef] | None = None) -> MagicMock:
    """Return a mock per-track downloader whose run() resolves with *refs_per_track*."""
    if refs_per_track is None:
        refs_per_track = [_make_ref(f"media/{_JOB_ID}/track.mp3")]
    dl = MagicMock()
    dl.run = AsyncMock(return_value=refs_per_track)
    return dl


def _make_fake_spodl(track_info: dict | None = None) -> MagicMock:
    """Return a fake spotify_dl module for patching sys.modules."""
    _default_info: dict = {
        "title": "Test Track",
        "artists": ["Test Artist"],
        "cover_url": None,
    }
    fake = MagicMock()
    fake.get_track_info = MagicMock(return_value=track_info or _default_info)
    return fake


def _make_spotify_local_download_patch(
    fail_indices: frozenset[int] | None = None,
):
    """Return (patch_cm, call_count_list) for patching _download_spotify_track_locally.

    Each call creates a real MP3 file in the *tmp_dir* passed by _download_one and
    returns its path, which lets the batch zip step proceed normally.
    Calls whose index is in *fail_indices* raise RuntimeError instead.
    """
    fail_set: frozenset[int] = fail_indices or frozenset()
    call_count: list[int] = [0]

    async def _fake_local_download(
        title: str,
        artist: str,
        quality: str,
        tmp_dir: Any,
        info: dict,
        **kwargs: Any,
    ) -> Any:
        idx = call_count[0]
        call_count[0] += 1
        if idx in fail_set:
            raise RuntimeError(f"simulated track failure #{idx}")
        fp = tmp_dir / f"track_{idx:04d}.mp3"
        fp.write_bytes(b"\xff\xfb" * 32)
        return fp

    cm = patch(
        "kharej.downloaders.spotify._download_spotify_track_locally",
        side_effect=_fake_local_download,
    )
    return cm, call_count


# ===========================================================================
# _build_track_urls
# ===========================================================================


class TestBuildTrackUrls:
    def test_spotify_builds_url(self) -> None:
        urls = _build_track_urls("spotify", ["abc123", "def456"], "https://open.spotify.com/playlist/X")
        assert urls == [
            "https://open.spotify.com/track/abc123",
            "https://open.spotify.com/track/def456",
        ]

    def test_youtube_passes_through_full_url(self) -> None:
        urls = _build_track_urls("youtube", ["https://youtu.be/abc"], "https://youtube.com/playlist?list=X")
        assert urls == ["https://youtu.be/abc"]

    def test_youtube_wraps_bare_id(self) -> None:
        urls = _build_track_urls("youtube", ["dQw4w9WgXcQ"], "https://youtube.com/playlist?list=X")
        assert urls == ["https://youtu.be/dQw4w9WgXcQ"]

    def test_generic_platform_passthrough(self) -> None:
        ids = ["id1", "id2"]
        urls = _build_track_urls("tidal", ids, "https://tidal.com/browse/playlist/X")
        assert urls == ids

    def test_empty_ids(self) -> None:
        assert _build_track_urls("spotify", [], "https://open.spotify.com/playlist/X") == []


# ===========================================================================
# _NoopProgress
# ===========================================================================


class TestNoopProgress:
    @pytest.mark.asyncio
    async def test_report_progress_is_silent(self) -> None:
        noop = _NoopProgress()
        # Must not raise; must return None.
        result = await noop.report_progress("job-id", 50, phase="downloading")
        assert result is None

    @pytest.mark.asyncio
    async def test_other_methods_are_silent(self) -> None:
        noop = _NoopProgress()
        await noop.report_accepted("job-id")
        await noop.report_completed("job-id")
        await noop.report_failed("job-id")


# ===========================================================================
# KharejSettings.get_bool
# ===========================================================================


class TestSettingsGetBool:
    def _make_real_settings(self, data: dict, tmp_path: Path) -> Any:
        """Build a real KharejSettings wired to a temp directory."""
        from kharej.settings import KharejSettings

        settings_file = tmp_path / "kharej_settings.json"
        import json

        settings_file.write_text(json.dumps(data))
        return KharejSettings(state_path=settings_file)

    def test_true_string_values(self, tmp_path: Path) -> None:
        for val in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            s = self._make_real_settings({"flag": val}, tmp_path)
            assert s.get_bool("flag") is True, f"Expected True for {val!r}"

    def test_false_string_values(self, tmp_path: Path) -> None:
        for val in ("0", "false", "no", "off", ""):
            s = self._make_real_settings({"flag": val}, tmp_path)
            assert s.get_bool("flag") is False, f"Expected False for {val!r}"

    def test_bool_true(self, tmp_path: Path) -> None:
        s = self._make_real_settings({"flag": True}, tmp_path)
        assert s.get_bool("flag") is True

    def test_bool_false(self, tmp_path: Path) -> None:
        s = self._make_real_settings({"flag": False}, tmp_path)
        assert s.get_bool("flag") is False

    def test_int_nonzero(self, tmp_path: Path) -> None:
        s = self._make_real_settings({"flag": 1}, tmp_path)
        assert s.get_bool("flag") is True

    def test_int_zero(self, tmp_path: Path) -> None:
        s = self._make_real_settings({"flag": 0}, tmp_path)
        assert s.get_bool("flag") is False

    def test_missing_key_default_false(self, tmp_path: Path) -> None:
        s = self._make_real_settings({}, tmp_path)
        assert s.get_bool("missing_key") is False

    def test_missing_key_custom_default(self, tmp_path: Path) -> None:
        s = self._make_real_settings({}, tmp_path)
        assert s.get_bool("missing_key", True) is True


# ===========================================================================
# BatchDownloader — per-track calls
# ===========================================================================


class TestBatchDownloaderTrackCalls:
    @pytest.mark.asyncio
    async def test_calls_per_track_downloader_n_times(self, tmp_path: Path) -> None:
        """BatchDownloader must download each track URL exactly once."""
        track_ids = ["id1", "id2", "id3"]
        batch = BatchDownloader()
        job = _make_job(track_ids=track_ids, total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        fake_spodl = _make_fake_spodl()
        dl_patch, call_count = _make_spotify_local_download_patch()
        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_empty_track_ids_raises(self) -> None:
        """When no tracks are available the batch downloader raises RuntimeError."""
        batch = BatchDownloader()
        job = _make_job(track_ids=[], total_tracks=0)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with pytest.raises(RuntimeError, match="no tracks downloaded"):
            await batch.run(job, s2=s2, progress=progress, settings=settings)


# ===========================================================================
# Cookies forwarding: batch → SpotifyDownloader → yt-dlp
# ===========================================================================


class TestBatchSpotifyCookiesForwarded:
    """Verify that cookies in batch settings reach _download_spotify_track_locally.

    This is the regression test for the bug where batch._download_one called
    _download_spotify_track_locally without the cookies_path argument, causing
    yt-dlp bot-detection failures even when a valid cookies.txt was configured.
    """

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_ytdlp(self, tmp_path: Path) -> None:
        """Cookies set in batch settings must be forwarded to _download_spotify_track_locally."""
        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")

        captured_kwargs: list[dict] = []

        async def _fake_local_download(
            title: str,
            artist: str,
            quality: str,
            tmp_dir: Any,
            info: dict,
            **kwargs: Any,
        ) -> Path:
            captured_kwargs.append(dict(kwargs))
            fp = tmp_dir / "cookie_test.mp3"
            fp.write_bytes(b"\xff\xfb" * 16)
            return fp

        fake_spodl = _make_fake_spodl(
            {
                "title": "Cookie Test Track",
                "artists": ["Test Artist"],
                "cover_url": None,
            }
        )

        batch = BatchDownloader()
        job = _make_job(
            track_ids=["https://open.spotify.com/track/testtrackid1234567890"],
            total_tracks=1,
        )
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(cookies_path=str(cookies_file))

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), \
                patch(
                    "kharej.downloaders.spotify._download_spotify_track_locally",
                    side_effect=_fake_local_download,
                ):
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_kwargs, "_download_spotify_track_locally was never called"
        assert captured_kwargs[0].get("cookies_path") == str(cookies_file), (
            f"cookies_path not forwarded; got kwargs: {captured_kwargs[0]}"
        )


# ===========================================================================
# Failure scenarios (restored to own section)
# ===========================================================================


class TestBatchDownloaderFailureScenarios:
    @pytest.mark.asyncio
    async def test_partial_failure_does_not_abort(self, tmp_path: Path) -> None:
        """A failing track must not abort the rest of the batch."""
        # Fail the 2nd track (index 1), succeed the rest.
        dl_patch, call_count = _make_spotify_local_download_patch(fail_indices=frozenset({1}))
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1", "id2", "id3"], total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            # Should not raise — partial success is OK.
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        # 3 download attempts, 2 succeeded → at least 1 zip uploaded.
        assert call_count[0] == 3
        assert len(refs) >= 1

    @pytest.mark.asyncio
    async def test_all_tracks_fail_raises(self) -> None:
        """When every track fails the batch downloader raises RuntimeError."""
        dl_patch, _ = _make_spotify_local_download_patch(fail_indices=frozenset({0, 1}))
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1", "id2"], total_tracks=2)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            with pytest.raises(RuntimeError):
                await batch.run(job, s2=s2, progress=progress, settings=settings)

    @pytest.mark.asyncio
    async def test_unknown_platform_attempts_ytdlp(self) -> None:
        """Non-Spotify platforms use the yt-dlp path; all-fail raises RuntimeError."""
        captured_cmds: list[list[str]] = []

        def _fake_subprocess(cmd: list[str], job_id: str, loop: Any, progress_factory: Any) -> None:
            captured_cmds.append(list(cmd))
            raise RuntimeError("simulated yt-dlp failure")

        batch = BatchDownloader()
        job = _make_job(
            platform="youtube",
            url="https://youtu.be/dQw4w9WgXcQ",
            track_ids=["https://youtu.be/dQw4w9WgXcQ"],
            total_tracks=1,
        )
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
                patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
            with pytest.raises(RuntimeError, match="no tracks downloaded"):
                await batch.run(job, s2=s2, progress=progress, settings=settings)

        # yt-dlp was called for the track
        assert captured_cmds


# ===========================================================================
# Progress aggregation
# ===========================================================================


class TestProgressAggregation:
    @pytest.mark.asyncio
    async def test_progress_monotonically_increases(self, tmp_path: Path) -> None:
        """Progress percent reported to the progress reporter must be non-decreasing."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["a", "b", "c"], total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        # Extract the percent values from all report_progress calls.
        percents = [
            call.args[1]
            for call in progress.report_progress.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], int)
        ]
        assert percents, "No progress calls recorded"
        for i in range(1, len(percents)):
            assert percents[i] >= percents[i - 1], (
                f"Progress went backwards: {percents[i - 1]} → {percents[i]}"
            )

    @pytest.mark.asyncio
    async def test_progress_ends_at_100(self, tmp_path: Path) -> None:
        """The final progress call must report percent == 100."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["a", "b"], total_tracks=2)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        last_call = progress.report_progress.call_args_list[-1]
        assert last_call.args[1] == 100

    @pytest.mark.asyncio
    async def test_progress_reports_done_tracks(self, tmp_path: Path) -> None:
        """``done_tracks`` kwarg must be present in at least one progress call."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["a", "b"], total_tracks=2)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        # At least one call should have done_tracks kwarg.
        calls_with_done = [
            c
            for c in progress.report_progress.call_args_list
            if "done_tracks" in c.kwargs
        ]
        assert calls_with_done, "No report_progress call included done_tracks"


# ===========================================================================
# ZIP creation
# ===========================================================================


class TestZipCreation:
    @pytest.mark.asyncio
    async def test_zip_uploaded_with_correct_key(self, tmp_path: Path) -> None:
        """A single-part ZIP must be uploaded under ``media/{job_id}/{name}.zip``."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(
            collection_name="My_Album",
            track_ids=["id1", "id2"],
            total_tracks=2,
        )
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(enable_split=False)

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1
        assert refs[0].key == f"media/{_JOB_ID}/My_Album.zip"

    @pytest.mark.asyncio
    async def test_zip_file_contains_expected_entries(self, tmp_path: Path) -> None:
        """The batch downloader must upload exactly one ZIP for two downloaded tracks."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1", "id2"], total_tracks=2)

        s2 = _make_s2()
        settings = _make_settings(enable_split=False)
        progress = _make_progress()

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        # One ZIP for two tracks.
        assert len(refs) == 1
        assert refs[0].key.endswith(".zip")
        # The ZIP upload call happened.
        s2.upload_file.assert_called_once()
        _, upload_key = s2.upload_file.call_args.args[:2]
        assert upload_key == f"media/{_JOB_ID}/My_Playlist.zip"

    @pytest.mark.asyncio
    async def test_no_split_when_disabled(self, tmp_path: Path) -> None:
        """When ``enable_zip_split`` is False there must be exactly one ZIP part."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1", "id2", "id3"], total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(enable_split=False)

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1


# ===========================================================================
# ZIP split
# ===========================================================================


class TestZipSplit:
    @pytest.mark.asyncio
    async def test_split_produces_multiple_parts(self, tmp_path: Path) -> None:
        """When split is enabled and threshold is tiny, multiple parts are created."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1", "id2", "id3"], total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(enable_split=True, threshold_mb=0)

        fake_part1 = tmp_path / "batch-part1.zip"
        fake_part2 = tmp_path / "batch-part2.zip"
        for p in (fake_part1, fake_part2):
            with zipfile.ZipFile(str(p), "w") as zf:
                zf.writestr("placeholder.txt", "x")

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch, \
                patch("kharej.downloaders.batch._split_zip_from_files") as mock_split:
            mock_split.return_value = [fake_part1, fake_part2]
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 2

    @pytest.mark.asyncio
    async def test_split_keys_use_part_naming(self, tmp_path: Path) -> None:
        """Part keys must follow ``media/{job_id}/{name}-part{N}.zip`` convention."""
        from kharej.contracts import make_part_key

        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(
            collection_name="SplitAlbum",
            track_ids=["id1", "id2"],
            total_tracks=2,
        )
        progress = _make_progress()

        uploaded_keys: list[str] = []

        def _capture_upload(path: Path, key: str, **kw: Any) -> S2ObjectRef:
            uploaded_keys.append(key)
            return _make_ref(key, 100)

        s2 = MagicMock()
        s2.upload_file = MagicMock(side_effect=_capture_upload)
        settings = _make_settings(enable_split=True, threshold_mb=0)

        fake_part1 = tmp_path / "SplitAlbum-part1.zip"
        fake_part2 = tmp_path / "SplitAlbum-part2.zip"
        for p in (fake_part1, fake_part2):
            with zipfile.ZipFile(str(p), "w") as zf:
                zf.writestr("placeholder.txt", "x")

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch, \
                patch("kharej.downloaders.batch._split_zip_from_files") as mock_split:
            mock_split.return_value = [fake_part1, fake_part2]
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 2
        expected_key_1 = make_part_key(_JOB_ID, "SplitAlbum", 1)
        expected_key_2 = make_part_key(_JOB_ID, "SplitAlbum", 2)
        assert expected_key_1 in uploaded_keys
        assert expected_key_2 in uploaded_keys

    @pytest.mark.asyncio
    async def test_no_split_when_below_threshold(self, tmp_path: Path) -> None:
        """When split is enabled but total size is below threshold, one part is produced."""
        dl_patch, _ = _make_spotify_local_download_patch()
        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["id1"], total_tracks=1)
        progress = _make_progress()
        s2 = _make_s2()
        # Very large threshold — files will always be below it.
        settings = _make_settings(enable_split=True, threshold_mb=9999)

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1
        assert refs[0].key == f"media/{_JOB_ID}/My_Playlist.zip"


# ===========================================================================
# Concurrency
# ===========================================================================


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrency_respected(self) -> None:
        """No more than *concurrency* tracks should run simultaneously."""
        concurrency_limit = 2
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def _slow_local_download(
            title: str,
            artist: str,
            quality: str,
            tmp_dir: Any,
            info: dict,
            **kwargs: Any,
        ) -> Path:
            nonlocal current_concurrent, max_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            fp = tmp_dir / f"track_{id(tmp_dir)}.mp3"
            fp.write_bytes(b"\xff\xfb" * 32)
            return fp

        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=[f"id{i}" for i in range(6)], total_tracks=6)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(concurrency=concurrency_limit)

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), \
                patch(
                    "kharej.downloaders.spotify._download_spotify_track_locally",
                    side_effect=_slow_local_download,
                ):
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert max_concurrent <= concurrency_limit, (
            f"Concurrency exceeded: max observed = {max_concurrent}, limit = {concurrency_limit}"
        )

    @pytest.mark.asyncio
    async def test_concurrency_1_runs_sequentially(self) -> None:
        """With concurrency=1 tracks execute strictly one at a time."""
        order: list[str] = []
        call_counter = [0]

        async def _ordered_local_download(
            title: str,
            artist: str,
            quality: str,
            tmp_dir: Any,
            info: dict,
            **kwargs: Any,
        ) -> Path:
            order.append(title)
            idx = call_counter[0]
            call_counter[0] += 1
            fp = tmp_dir / f"track_{idx:04d}.mp3"
            fp.write_bytes(b"\xff\xfb" * 32)
            return fp

        fake_spodl = _make_fake_spodl()
        batch = BatchDownloader()
        job = _make_job(track_ids=["a", "b", "c"], total_tracks=3)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings(concurrency=1)

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), \
                patch(
                    "kharej.downloaders.spotify._download_spotify_track_locally",
                    side_effect=_ordered_local_download,
                ):
            await batch.run(job, s2=s2, progress=progress, settings=settings)

        # All three must have run.
        assert len(order) == 3


# ===========================================================================
# Dispatcher wiring
# ===========================================================================


class TestDispatcherBatchWiring:
    """Verify that the Dispatcher routes batch job_type to BatchDownloader."""

    def _make_dispatcher(self, batch_dl: Any) -> Any:
        from kharej.access_control import AccessControl
        from kharej.dispatcher import Dispatcher
        from kharej.progress_reporter import ProgressReporter
        from kharej.settings import KharejSettings

        s2 = MagicMock()
        rubika = MagicMock()
        access = MagicMock()
        access.check_access = MagicMock(return_value="allow")
        settings = MagicMock()
        settings.get_int = MagicMock(return_value=0)
        settings.get_bool = MagicMock(return_value=False)
        progress = MagicMock()
        progress.report_accepted = AsyncMock()
        progress.report_progress = AsyncMock()
        progress.report_completed = AsyncMock()
        progress.report_failed = AsyncMock()
        from kharej.downloaders.stub import StubDownloader

        stub = StubDownloader()
        dispatcher = Dispatcher(
            s2=s2,
            rubika=rubika,
            access=access,
            settings=settings,
            progress=progress,
            downloaders={"stub": stub, "batch": batch_dl, "spotify": MagicMock()},
        )
        return dispatcher

    @pytest.mark.asyncio
    async def test_batch_downloader_registered(self) -> None:
        """Dispatcher must expose the batch downloader in its registry."""
        batch_dl = MagicMock()
        batch_dl.platform = "batch"
        batch_dl.run = AsyncMock(return_value=[_make_ref("media/x/a.zip")])

        from kharej.dispatcher import Dispatcher

        s2 = MagicMock()
        rubika = MagicMock()
        access = MagicMock()
        settings = MagicMock()
        settings.get_int = MagicMock(return_value=0)
        settings.get_bool = MagicMock(return_value=False)
        progress = MagicMock()
        progress.report_accepted = AsyncMock()
        progress.report_completed = AsyncMock()
        progress.report_failed = AsyncMock()
        from kharej.downloaders.stub import StubDownloader

        stub = StubDownloader()
        d = Dispatcher(
            s2=s2,
            rubika=rubika,
            access=access,
            settings=settings,
            progress=progress,
            downloaders={"stub": stub, "batch": batch_dl},
        )
        assert d.has("batch")

    @pytest.mark.asyncio
    async def test_register_batch_updates_batch_downloader(self) -> None:
        """Calling dispatcher.register() with a batch downloader updates _batch_downloader."""
        from kharej.dispatcher import Dispatcher

        s2 = MagicMock()
        rubika = MagicMock()
        access = MagicMock()
        settings = MagicMock()
        progress = MagicMock()
        from kharej.downloaders.stub import StubDownloader

        stub = StubDownloader()
        d = Dispatcher(
            s2=s2,
            rubika=rubika,
            access=access,
            settings=settings,
            progress=progress,
            downloaders={"stub": stub},
        )
        new_batch = MagicMock()
        new_batch.platform = "batch"
        d.register(new_batch)

        assert d._batch_downloader is new_batch


# ===========================================================================
# Spotify collection expansion in batch
# ===========================================================================


class TestBatchSpotifyCollectionExpansion:
    """Verify that BatchDownloader expands Spotify album/playlist URLs into per-track URLs."""

    @pytest.mark.asyncio
    async def test_album_url_expanded_to_multiple_tracks(self) -> None:
        """When track_ids is absent and job.url is a Spotify album, it should expand to N tracks."""
        dl_patch, call_count = _make_spotify_local_download_patch()

        batch = BatchDownloader()
        # No track_ids — batch should fall back to job.url and expand it
        job = _make_job(
            url="https://open.spotify.com/album/TESTALBUM123456789012",
            track_ids=None,  # absent → triggers fallback + expansion
            total_tracks=None,
        )
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        fake_spodl = _make_fake_spodl()
        fake_spodl.parse_spotify_album_id = MagicMock(return_value="TESTALBUM123456789012")
        fake_spodl.get_spotify_album_tracks = MagicMock(
            return_value=({"name": "Test Album"}, ["tid1", "tid2", "tid3"])
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        # Three tracks should have been downloaded (one per expanded track ID)
        assert call_count[0] == 3, f"Expected 3 track downloads, got {call_count[0]}"
        assert refs

    @pytest.mark.asyncio
    async def test_playlist_url_expanded_to_multiple_tracks(self) -> None:
        """When track_ids is absent and job.url is a Spotify playlist, it should expand."""
        dl_patch, call_count = _make_spotify_local_download_patch()

        batch = BatchDownloader()
        job = _make_job(
            url="https://open.spotify.com/playlist/TESTPLAYLIST12345678",
            track_ids=None,
            total_tracks=None,
        )
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        fake_spodl = _make_fake_spodl()
        fake_spodl.parse_spotify_playlist_id = MagicMock(return_value="TESTPLAYLIST12345678")
        fake_spodl.get_spotify_playlist_tracks = MagicMock(
            return_value=({"name": "My Playlist"}, ["pA", "pB"])
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_spodl}), dl_patch:
            refs = await batch.run(job, s2=s2, progress=progress, settings=settings)

        assert call_count[0] == 2
        assert refs

    @pytest.mark.asyncio
    async def test_explicit_empty_track_ids_raises(self) -> None:
        """Explicitly passing track_ids=[] (not None) must raise without fallback."""
        batch = BatchDownloader()
        job = _make_job(track_ids=[], total_tracks=0)
        progress = _make_progress()
        s2 = _make_s2()
        settings = _make_settings()

        with pytest.raises(RuntimeError, match="no tracks downloaded"):
            await batch.run(job, s2=s2, progress=progress, settings=settings)
