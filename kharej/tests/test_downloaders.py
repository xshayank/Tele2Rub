"""Tests for Step 7 — YouTube and Spotify single-track downloaders.

Covers:
- kharej/downloaders/common.py  (safe_filename, get_downloads_dir, cleanup_path)
- kharej/downloaders/youtube.py (parse_percent, parse_speed, parse_eta, YoutubeDownloader.run)
- kharej/downloaders/spotify.py (SpotifyDownloader.run)

All external I/O (yt-dlp, spotify_dl, S2Client) is mocked — no network needed.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kharej.contracts import S2ObjectRef
from kharej.downloaders.common import (
    cleanup_path,
    get_downloads_dir,
    make_job_dir,
    safe_filename,
)
from kharej.downloaders.youtube import (
    YoutubeDownloader,
    _audio_codec,
    _is_audio_quality,
    _resolve_format,
    parse_eta,
    parse_percent,
    parse_speed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_JOB_ID = "aaaaaaaa-0000-0000-0000-000000000001"
_DUMMY_REF = S2ObjectRef(
    key=f"media/{_JOB_ID}/track.mp3",
    size=1024,
    mime="audio/mpeg",
    sha256="a" * 64,
)
_THUMB_REF = S2ObjectRef(
    key=f"thumbs/{_JOB_ID}.jpg",
    size=512,
    mime="image/jpeg",
    sha256="b" * 64,
)


def _make_job(
    *,
    job_id: str = _JOB_ID,
    platform: str = "youtube",
    url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    quality: str = "mp3",
) -> Any:
    """Build a minimal Job-like object without importing the full dispatcher."""
    from kharej.contracts import JobCreate, Platform
    from kharej.dispatcher import Job

    msg = JobCreate.model_construct(
        v=1,
        ts=_NOW,
        job_id=job_id,
        user_id="user-test",
        platform=Platform.youtube if platform == "youtube" else Platform.spotify,
        url=url,
        quality=quality,
        job_type="single",
        user_status="active",
        format_hint=None,
        collection_name=None,
        track_ids=None,
        total_tracks=None,
        batch_seq=None,
        batch_total=None,
    )
    return Job(
        job_id=job_id,
        user_id="user-test",
        platform=platform,
        url=url,
        quality=quality,
        job_type="single",
        payload=msg,
    )


def _make_progress() -> AsyncMock:
    progress = MagicMock()
    progress.report_progress = AsyncMock()
    return progress


def _make_s2(ref: S2ObjectRef = _DUMMY_REF) -> MagicMock:
    s2 = MagicMock()
    s2.upload_file = MagicMock(return_value=ref)
    return s2


def _make_settings(extra: dict | None = None) -> MagicMock:
    settings = MagicMock()
    _data = extra or {}
    settings.get = MagicMock(side_effect=lambda key, *args: _data.get(key, args[0] if args else None))
    return settings


# ===========================================================================
# safe_filename
# ===========================================================================


class TestSafeFilename:
    def test_basic(self) -> None:
        assert safe_filename("Hello World") == "Hello_World"

    def test_colon_replaced(self) -> None:
        result = safe_filename("AC/DC: Back in Black")
        assert ":" not in result
        assert "/" not in result

    def test_backslash_replaced(self) -> None:
        assert "\\" not in safe_filename("some\\path")

    def test_angle_brackets_replaced(self) -> None:
        assert "<" not in safe_filename("<html>")
        assert ">" not in safe_filename("<html>")

    def test_pipe_replaced(self) -> None:
        assert "|" not in safe_filename("a|b")

    def test_question_mark_replaced(self) -> None:
        assert "?" not in safe_filename("what?")

    def test_star_replaced(self) -> None:
        assert "*" not in safe_filename("star*")

    def test_multiple_spaces_collapsed(self) -> None:
        assert "  " not in safe_filename("a  b")

    def test_strips_leading_trailing(self) -> None:
        result = safe_filename("  .leading.")
        assert not result.startswith(" ")
        assert not result.startswith(".")

    def test_empty_string_returns_unknown(self) -> None:
        assert safe_filename("") == "unknown"

    def test_only_unsafe_chars_returns_unknown(self) -> None:
        assert safe_filename(":::") == "unknown"

    def test_normal_filename_unchanged(self) -> None:
        assert safe_filename("Shape_of_You") == "Shape_of_You"

    def test_unicode_preserved(self) -> None:
        result = safe_filename("Рок музыка")
        assert "Рок" in result

    def test_parentheses_preserved(self) -> None:
        result = safe_filename("Track (Remix)")
        assert "(" in result
        assert ")" in result


# ===========================================================================
# get_downloads_dir / make_job_dir
# ===========================================================================


class TestDownloadsDir:
    def test_default_dir_created(self, tmp_path: Path) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=str(tmp_path / "dl"))
        result = get_downloads_dir(settings)
        assert result.exists()
        assert result.is_dir()

    def test_job_dir_created(self, tmp_path: Path) -> None:
        settings = MagicMock()
        settings.get = MagicMock(return_value=str(tmp_path / "dl"))
        result = make_job_dir(settings, "my-job-id")
        assert result.exists()
        assert result.name == "my-job-id"


# ===========================================================================
# cleanup_path
# ===========================================================================


class TestCleanupPath:
    def test_removes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.mp3"
        f.write_bytes(b"data")
        cleanup_path(f)
        assert not f.exists()

    def test_removes_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "subdir"
        d.mkdir()
        (d / "file.txt").write_text("x")
        cleanup_path(d)
        assert not d.exists()

    def test_missing_file_ok(self, tmp_path: Path) -> None:
        cleanup_path(tmp_path / "nonexistent.mp3")  # must not raise

    def test_missing_ok_false_does_not_raise(self, tmp_path: Path) -> None:
        # Even with missing_ok=False the function must never raise.
        cleanup_path(tmp_path / "ghost.mp3", missing_ok=False)


# ===========================================================================
# parse_percent / parse_speed / parse_eta (YouTube progress hook helpers)
# ===========================================================================


class TestParsePercent:
    def test_from_percent_str(self) -> None:
        assert parse_percent({"_percent_str": " 45.7%"}) == 45

    def test_from_percent_str_100(self) -> None:
        assert parse_percent({"_percent_str": "100%"}) == 100

    def test_from_bytes(self) -> None:
        info = {"downloaded_bytes": 500, "total_bytes": 1000}
        assert parse_percent(info) == 50

    def test_from_bytes_estimate(self) -> None:
        info = {"downloaded_bytes": 300, "total_bytes_estimate": 1000}
        assert parse_percent(info) == 30

    def test_capped_at_100(self) -> None:
        assert parse_percent({"_percent_str": "150%"}) == 100

    def test_empty_returns_zero(self) -> None:
        assert parse_percent({}) == 0

    def test_none_total_bytes_returns_zero(self) -> None:
        info = {"downloaded_bytes": 500, "total_bytes": None}
        assert parse_percent(info) == 0

    def test_zero_total_bytes_returns_zero(self) -> None:
        info = {"downloaded_bytes": 500, "total_bytes": 0}
        assert parse_percent(info) == 0


class TestParseSpeed:
    def test_returns_stripped_string(self) -> None:
        assert parse_speed({"_speed_str": " 3.2 MiB/s "}) == "3.2 MiB/s"

    def test_missing_returns_none(self) -> None:
        assert parse_speed({}) is None

    def test_none_returns_none(self) -> None:
        assert parse_speed({"_speed_str": None}) is None


class TestParseEta:
    def test_integer_eta(self) -> None:
        assert parse_eta({"eta": 42}) == 42

    def test_float_eta_truncated(self) -> None:
        assert parse_eta({"eta": 9.9}) == 9

    def test_missing_returns_none(self) -> None:
        assert parse_eta({}) is None


# ===========================================================================
# _resolve_format / _is_audio_quality / _audio_codec
# ===========================================================================


class TestResolveFormat:
    def test_mp3(self) -> None:
        assert "bestaudio" in _resolve_format("mp3")

    def test_1080p(self) -> None:
        fmt = _resolve_format("1080p")
        assert "1080" in fmt

    def test_unknown_passthrough(self) -> None:
        assert _resolve_format("my_custom_format") == "my_custom_format"

    def test_case_insensitive(self) -> None:
        assert _resolve_format("MP3") == _resolve_format("mp3")


class TestIsAudioQuality:
    def test_mp3_is_audio(self) -> None:
        assert _is_audio_quality("mp3")

    def test_flac_is_audio(self) -> None:
        assert _is_audio_quality("flac")

    def test_1080p_not_audio(self) -> None:
        assert not _is_audio_quality("1080p")

    def test_best_not_audio(self) -> None:
        assert not _is_audio_quality("best")


class TestAudioCodec:
    def test_mp3(self) -> None:
        assert _audio_codec("mp3") == "mp3"

    def test_flac(self) -> None:
        assert _audio_codec("flac") == "flac"

    def test_unknown_defaults_to_mp3(self) -> None:
        assert _audio_codec("unknown") == "mp3"


# ===========================================================================
# YoutubeDownloader.run
# ===========================================================================


@pytest.mark.asyncio
async def test_youtube_downloader_uploads_and_returns_ref(tmp_path: Path) -> None:
    """YoutubeDownloader.run should call s2.upload_file with the correct key."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    # Create a fake downloaded file so the downloader finds it.
    fake_file = tmp_path / "Rick Astley - Never Gonna Give You Up.mp3"
    fake_file.write_bytes(b"\xff\xfb" * 512)

    def _fake_yt_download(url: str, opts: dict) -> None:
        # Write a fake file to the outtmpl directory.
        outtmpl = opts["outtmpl"]
        out_dir = Path(outtmpl).parent
        (out_dir / "Rick Astley - Never Gonna Give You Up.mp3").write_bytes(b"\xff\xfb" * 512)

    with patch("kharej.downloaders.youtube._do_yt_download", side_effect=_fake_yt_download):
        downloader = YoutubeDownloader()
        refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(refs) == 1
    assert refs[0] is _DUMMY_REF
    # Confirm upload_file was called with the correct positional key argument.
    s2.upload_file.assert_called_once()
    _, uploaded_key = s2.upload_file.call_args.args
    assert uploaded_key.startswith(f"media/{_JOB_ID}/")
    assert uploaded_key.endswith(".mp3")


@pytest.mark.asyncio
async def test_youtube_downloader_reports_progress(tmp_path: Path) -> None:
    """YoutubeDownloader.run should call progress.report_progress at least once (upload phase)."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_yt_download(url: str, opts: dict) -> None:
        outtmpl = opts["outtmpl"]
        out_dir = Path(outtmpl).parent
        (out_dir / "track.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._do_yt_download", side_effect=_fake_yt_download):
        downloader = YoutubeDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert progress.report_progress.called


@pytest.mark.asyncio
async def test_youtube_downloader_no_output_file_raises() -> None:
    """YoutubeDownloader.run should raise if yt-dlp produces no file."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_yt_download(url: str, opts: dict) -> None:
        pass  # produce nothing

    with patch("kharej.downloaders.youtube._do_yt_download", side_effect=_fake_yt_download):
        downloader = YoutubeDownloader()
        with pytest.raises(RuntimeError, match="no output file"):
            await downloader.run(job, s2=s2, progress=progress, settings=settings)


@pytest.mark.asyncio
async def test_youtube_downloader_uses_cookies(tmp_path: Path) -> None:
    """When cookies_path is set in settings, it should be passed to yt-dlp."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings({"cookies_path": str(tmp_path / "cookies.txt")})

    captured_opts: list[dict] = []

    def _fake_yt_download(url: str, opts: dict) -> None:
        captured_opts.append(dict(opts))
        outtmpl = opts["outtmpl"]
        out_dir = Path(outtmpl).parent
        (out_dir / "track.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._do_yt_download", side_effect=_fake_yt_download):
        downloader = YoutubeDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert captured_opts
    assert "cookiefile" in captured_opts[0]


@pytest.mark.asyncio
async def test_youtube_downloader_s2_key_uses_safe_filename() -> None:
    """S2 key should use a sanitized version of the downloaded filename."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_yt_download(url: str, opts: dict) -> None:
        outtmpl = opts["outtmpl"]
        out_dir = Path(outtmpl).parent
        # Filename with unsafe chars
        (out_dir / "Track: Super? Cool.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._do_yt_download", side_effect=_fake_yt_download):
        downloader = YoutubeDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    _, key = s2.upload_file.call_args.args
    # Must not contain colon or question mark
    assert ":" not in key
    assert "?" not in key


# ===========================================================================
# SpotifyDownloader.run
# ===========================================================================


def _make_spotify_module(*, audio_file: Path) -> MagicMock:
    """Build a MagicMock for the spotify_dl shim."""
    mod = MagicMock()
    mod.parse_spotify_track_id = MagicMock(return_value="3n3Ppam7vgaVa1iaRUIOKE")
    mod.get_track_info = MagicMock(
        return_value={
            "title": "Never Gonna Give You Up",
            "artists": ["Rick Astley"],
            "cover_url": None,
        }
    )

    async def _fake_download_track(info, output_dir, ytdlp_bin):
        dest = Path(output_dir) / "Rick_Astley_-_Never_Gonna_Give_You_Up.mp3"
        dest.write_bytes(b"\xff\xfb" * 128)
        return dest

    mod.download_track = _fake_download_track
    return mod


@pytest.mark.asyncio
async def test_spotify_downloader_uploads_and_returns_ref() -> None:
    """SpotifyDownloader.run should upload the audio and return S2ObjectRef."""
    from kharej.downloaders.spotify import SpotifyDownloader

    with tempfile.TemporaryDirectory() as tmp:
        audio_file = Path(tmp) / "track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 128)

        fake_mod = _make_spotify_module(audio_file=audio_file)
        job = _make_job(platform="spotify", url="https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUIOKE")
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(refs) >= 1
    assert refs[0] is _DUMMY_REF
    s2.upload_file.assert_called()
    _, first_key = s2.upload_file.call_args_list[0].args
    assert first_key.startswith(f"media/{_JOB_ID}/")
    assert first_key.endswith(".mp3")


@pytest.mark.asyncio
async def test_spotify_downloader_uploads_thumbnail() -> None:
    """SpotifyDownloader.run should also upload the thumbnail when cover_url is provided."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value="abc123")
    fake_mod.get_track_info = MagicMock(
        return_value={
            "title": "Some Track",
            "artists": ["Some Artist"],
            "cover_url": "https://example.com/cover.jpg",
        }
    )

    async def _fake_download_track(info, output_dir, ytdlp_bin):
        dest = Path(output_dir) / "track.mp3"
        dest.write_bytes(b"\xff\xfb" * 64)
        return dest

    fake_mod.download_track = _fake_download_track

    thumb_ref = S2ObjectRef(
        key=f"thumbs/{_JOB_ID}.jpg",
        size=256,
        mime="image/jpeg",
        sha256="c" * 64,
    )
    # s2 returns DUMMY_REF for audio, thumb_ref for thumbnail
    upload_call_count = [0]
    def _upload_side_effect(local_path, key, **kwargs):
        upload_call_count[0] += 1
        if "thumbs" in key:
            return thumb_ref
        return _DUMMY_REF

    s2 = MagicMock()
    s2.upload_file = MagicMock(side_effect=_upload_side_effect)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(platform="spotify", url="https://open.spotify.com/track/abc123")

    # Patch urllib.request.urlretrieve to write a fake thumbnail file
    def _fake_urlretrieve(url, dest):
        Path(dest).write_bytes(b"\xff\xd8\xff" * 50)
        return dest, {}

    with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
        with patch("urllib.request.urlretrieve", side_effect=_fake_urlretrieve):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(refs) == 2
    keys = {r.key for r in refs}
    assert any("thumbs" in k for k in keys)
    assert any("media" in k for k in keys)


@pytest.mark.asyncio
async def test_spotify_downloader_bad_url_raises() -> None:
    """SpotifyDownloader.run should raise ValueError for unparseable URLs."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value=None)

    job = _make_job(platform="spotify", url="https://not-spotify.com/bad")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
        downloader = SpotifyDownloader()
        with pytest.raises(ValueError, match="Could not parse Spotify track ID"):
            await downloader.run(job, s2=s2, progress=progress, settings=settings)


@pytest.mark.asyncio
async def test_spotify_downloader_thumbnail_failure_does_not_abort() -> None:
    """A thumbnail upload failure must not abort the overall job."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value="xyz789")
    fake_mod.get_track_info = MagicMock(
        return_value={
            "title": "Resilient Track",
            "artists": ["Artist"],
            "cover_url": "https://example.com/thumb.jpg",
        }
    )

    async def _fake_download_track(info, output_dir, ytdlp_bin):
        dest = Path(output_dir) / "track.mp3"
        dest.write_bytes(b"\x00" * 64)
        return dest

    fake_mod.download_track = _fake_download_track

    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(platform="spotify", url="https://open.spotify.com/track/xyz789")

    # Make urlretrieve raise an OSError
    def _bad_retrieve(url, dest):
        raise OSError("network error")

    with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
        with patch("urllib.request.urlretrieve", side_effect=_bad_retrieve):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    # Should still return the audio ref despite thumbnail failure.
    assert len(refs) == 1
    assert refs[0] is _DUMMY_REF


# ===========================================================================
# Dispatcher integration: youtube and spotify are now registered by default
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatcher_registers_youtube_and_spotify() -> None:
    """The default Dispatcher should have 'youtube' and 'spotify' registered."""
    import tempfile
    from unittest.mock import MagicMock

    from kharej.access_control import AccessControl
    from kharej.dispatcher import Dispatcher
    from kharej.progress_reporter import ProgressReporter
    from kharej.settings import KharejSettings

    with tempfile.TemporaryDirectory() as td:
        access = AccessControl(state_path=Path(td) / "state.json")
        settings = KharejSettings()
        send_mock = AsyncMock()
        progress = ProgressReporter(send_mock)
        rubika = MagicMock()

        dispatcher = Dispatcher(
            s2=MagicMock(),
            rubika=rubika,
            access=access,
            settings=settings,
            progress=progress,
        )

        assert dispatcher.has("youtube"), "youtube downloader must be registered by default"
        assert dispatcher.has("spotify"), "spotify downloader must be registered by default"
        assert dispatcher.has("stub"), "stub downloader must still be registered"
