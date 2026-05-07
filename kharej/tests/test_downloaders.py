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
)
from kharej.downloaders.instagram import _build_command as _build_instagram_command

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


class TestInstagramBuildCommand:
    def test_uses_instagram_sorting(self) -> None:
        cmd = _build_instagram_command(
            "/usr/bin/yt-dlp",
            "https://www.instagram.com/reel/abc/",
            "/tmp/%(title)s.%(ext)s",
            None,
        )
        assert "-S" in cmd
        assert "proto,ext:mp4,res,br" in cmd

    def test_passes_cookies_when_provided(self) -> None:
        cmd = _build_instagram_command(
            "/usr/bin/yt-dlp",
            "https://www.instagram.com/reel/abc/",
            "/tmp/%(title)s.%(ext)s",
            "/tmp/cookies.txt",
        )
        assert "--cookies" in cmd
        assert "/tmp/cookies.txt" in cmd

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

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        # Extract output dir from --output argument
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        (out_dir / "Rick Astley - Never Gonna Give You Up.mp3").write_bytes(b"\xff\xfb" * 512)

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
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

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        (out_dir / "track.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
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

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        pass  # produce nothing

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
        downloader = YoutubeDownloader()
        with pytest.raises(RuntimeError, match="no output file"):
            await downloader.run(job, s2=s2, progress=progress, settings=settings)


@pytest.mark.asyncio
async def test_youtube_downloader_uses_cookies(tmp_path: Path) -> None:
    """When cookies_path is set and the file exists, --cookies is passed to yt-dlp."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    # Create an actual cookies file so the existence check passes
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\n")
    settings = _make_settings({"cookies_path": str(cookies_file)})

    captured_cmds: list[list[str]] = []

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        captured_cmds.append(list(cmd))
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        (out_dir / "track.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
        downloader = YoutubeDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert captured_cmds
    assert "--cookies" in captured_cmds[0]
    cookies_idx = captured_cmds[0].index("--cookies")
    assert captured_cmds[0][cookies_idx + 1] == str(cookies_file)


@pytest.mark.asyncio
async def test_youtube_downloader_autodiscovers_cookies(tmp_path: Path, monkeypatch) -> None:
    """When no cookies_path is configured, a cookies.txt in kharej/ or repo root is used."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()  # no cookies_path

    # Place a cookies.txt where the auto-discovery logic will find it.
    # common.py auto-discovers via Path(__file__).parent.parent (kharej/).
    # We patch common.__file__ so that parent.parent resolves to tmp_path,
    # and place cookies.txt there.
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\n")

    captured_cmds: list[list[str]] = []

    def _fake_subprocess(cmd, _job_id, _loop, _progress_coro_factory):
        captured_cmds.append(list(cmd))
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        (out_dir / "track.mp3").write_bytes(b"\x00" * 64)

    # Patch __file__ on the common module so that Path(__file__).parent.parent
    # resolves to tmp_path ("kharej/"), where cookies.txt was created above.
    # Structure: tmp_path/downloaders/common.py → parent.parent == tmp_path
    import kharej.downloaders.common as common_mod
    monkeypatch.setattr(common_mod, "__file__", str(tmp_path / "downloaders" / "common.py"))

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
        downloader = YoutubeDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert captured_cmds
    assert "--cookies" in captured_cmds[0]
    cookies_idx = captured_cmds[0].index("--cookies")
    assert captured_cmds[0][cookies_idx + 1] == str(cookies_file)


@pytest.mark.asyncio
async def test_youtube_downloader_s2_key_uses_safe_filename() -> None:
    """S2 key should use a sanitized version of the downloaded filename."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        # Filename with unsafe chars
        (out_dir / "Track: Super? Cool.mp3").write_bytes(b"\x00" * 64)

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess):
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
    return mod


def _make_fake_yt_dlp(audio_file: Path | None = None) -> MagicMock:
    """Build a MagicMock for yt_dlp that writes a fake mp3 to the outtmpl directory."""

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            self._opts = opts

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            outtmpl = self._opts.get("outtmpl", "")
            out_dir = Path(outtmpl).parent
            if audio_file is not None:
                import shutil  # noqa: PLC0415
                shutil.copy(audio_file, out_dir / audio_file.name)
            else:
                (out_dir / "track.mp3").write_bytes(b"\xff\xfb" * 128)

    fake = MagicMock()
    fake.YoutubeDL = _FakeYDL
    return fake


@pytest.mark.asyncio
async def test_spotify_downloader_uploads_and_returns_ref() -> None:
    """SpotifyDownloader.run should upload the audio and return S2ObjectRef."""
    from kharej.downloaders.spotify import SpotifyDownloader

    with tempfile.TemporaryDirectory() as tmp:
        audio_file = Path(tmp) / "track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 128)

        fake_mod = _make_spotify_module(audio_file=audio_file)
        fake_yt_dlp = _make_fake_yt_dlp(audio_file=audio_file)
        job = _make_job(platform="spotify", url="https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUIOKE")
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
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

    fake_yt_dlp = _make_fake_yt_dlp()

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

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
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

    fake_yt_dlp = _make_fake_yt_dlp()

    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(platform="spotify", url="https://open.spotify.com/track/xyz789")

    # Make urlretrieve raise an OSError
    def _bad_retrieve(url, dest):
        raise OSError("network error")

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        with patch("urllib.request.urlretrieve", side_effect=_bad_retrieve):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    # Should still return the audio ref despite thumbnail failure.
    assert len(refs) == 1
    assert refs[0] is _DUMMY_REF


@pytest.mark.asyncio
async def test_spotify_downloader_passes_cookies_to_ytdlp(tmp_path: Path) -> None:
    """SpotifyDownloader.run should pass cookiefile to yt-dlp when cookies_path is configured."""
    from kharej.downloaders.spotify import SpotifyDownloader

    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\n")
    settings = _make_settings({"cookies_path": str(cookies_file)})

    captured_opts: list[dict] = []

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value="abc123")
    fake_mod.get_track_info = MagicMock(
        return_value={"title": "Test Track", "artists": ["Artist"], "cover_url": None}
    )

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            captured_opts.append(opts)

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            outtmpl = captured_opts[-1].get("outtmpl", "")
            out_dir = Path(outtmpl).parent
            (out_dir / "test.mp3").write_bytes(b"\xff\xfb" * 32)

    fake_yt_dlp = MagicMock()
    fake_yt_dlp.YoutubeDL = _FakeYDL

    job = _make_job(platform="spotify", url="https://open.spotify.com/track/abc123", quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        downloader = SpotifyDownloader()
        await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert captured_opts, "YoutubeDL was never instantiated"
    assert captured_opts[0].get("cookiefile") == str(cookies_file)


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


# ===========================================================================
# SpotifyDownloader – playlist / album collection support
# ===========================================================================


@pytest.mark.asyncio
async def test_spotify_downloader_playlist_expands_tracks() -> None:
    """SpotifyDownloader.run should download each track in a playlist."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    # Mock the real spotify_dl API used for playlist expansion
    fake_mod.parse_spotify_playlist_id = MagicMock(return_value="37i9dQZF1DXcBWIGoYBM5M")
    fake_mod.get_spotify_playlist_tracks = MagicMock(
        return_value=({"name": "Test Playlist"}, ["tid1", "tid2"])
    )
    # get_track_info is called once per track to fetch metadata
    fake_mod.get_track_info = MagicMock(
        side_effect=[
            {"title": "Track One", "artists": ["Artist A"], "cover_url": None},
            {"title": "Track Two", "artists": ["Artist B"], "cover_url": None},
        ]
    )
    fake_yt_dlp = _make_fake_yt_dlp()

    refs_returned = [
        S2ObjectRef(key=f"media/{_JOB_ID}/0000_Artist_A_-_Track_One.mp3", size=100, mime="audio/mpeg", sha256="a" * 64),
        S2ObjectRef(key=f"media/{_JOB_ID}/0001_Artist_B_-_Track_Two.mp3", size=100, mime="audio/mpeg", sha256="b" * 64),
    ]
    s2 = MagicMock()
    s2.upload_file = MagicMock(side_effect=refs_returned)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(
        platform="spotify",
        url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
    )

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        downloader = SpotifyDownloader()
        result = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(result) == 2
    fake_mod.get_spotify_playlist_tracks.assert_called_once_with("37i9dQZF1DXcBWIGoYBM5M")
    assert fake_mod.get_track_info.call_count == 2


@pytest.mark.asyncio
async def test_spotify_downloader_album_expands_tracks() -> None:
    """SpotifyDownloader.run should download each track in an album."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    # Mock the real spotify_dl API used for album expansion
    fake_mod.parse_spotify_album_id = MagicMock(return_value="6dVIqQ8qmQ5GBnJ9shOYGE")
    fake_mod.get_spotify_album_tracks = MagicMock(
        return_value=({"name": "Test Album"}, ["tid1", "tid2", "tid3"])
    )
    # get_track_info is called once per track
    fake_mod.get_track_info = MagicMock(
        side_effect=[
            {"title": "Song A", "artists": ["Band"], "cover_url": None},
            {"title": "Song B", "artists": ["Band"], "cover_url": None},
            {"title": "Song C", "artists": ["Band"], "cover_url": None},
        ]
    )
    fake_yt_dlp = _make_fake_yt_dlp()

    dummy_ref = S2ObjectRef(key=f"media/{_JOB_ID}/x.mp3", size=100, mime="audio/mpeg", sha256="c" * 64)
    s2 = MagicMock()
    s2.upload_file = MagicMock(return_value=dummy_ref)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(
        platform="spotify",
        url="https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
    )

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        downloader = SpotifyDownloader()
        result = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(result) == 3
    fake_mod.get_spotify_album_tracks.assert_called_once_with("6dVIqQ8qmQ5GBnJ9shOYGE")
    assert fake_mod.get_track_info.call_count == 3


@pytest.mark.asyncio
async def test_spotify_downloader_collection_fallback_when_no_getter() -> None:
    """SpotifyDownloader.run should fall back to single-track when collection expansion fails."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    # Make collection expansion fail (parse returns None → no expansion)
    fake_mod.parse_spotify_playlist_id = MagicMock(return_value=None)
    fake_mod.parse_spotify_track_id = MagicMock(return_value="trackxyz")
    # get_track_info is called by the single-track path
    fake_mod.get_track_info = MagicMock(
        return_value={"title": "Solo Track", "artists": ["Solo Artist"], "cover_url": None}
    )
    fake_yt_dlp = _make_fake_yt_dlp()

    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()
    job = _make_job(
        platform="spotify",
        url="https://open.spotify.com/playlist/abc",
    )

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        downloader = SpotifyDownloader()
        result = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    # Should fall back to single track download
    assert len(result) >= 1
    fake_mod.parse_spotify_track_id.assert_called_once()


@pytest.mark.asyncio
async def test_spotify_musicdl_download_is_awaited() -> None:
    """_download_spotify_track_locally must properly await MusicdlClient.download (not wrap in thread)."""
    from kharej.downloaders.spotify import _download_spotify_track_locally

    # Simulate yt-dlp failing so musicdl fallback is triggered.
    fake_yt_dlp = MagicMock()

    class _FailYDL:
        def __init__(self, opts: dict) -> None:
            pass

        def __enter__(self) -> "_FailYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            raise RuntimeError("yt-dlp unavailable")

    fake_yt_dlp.YoutubeDL = _FailYDL

    # Build a fake MusicdlClient whose download is an AsyncMock (verifies it's awaited).
    download_mock = AsyncMock(return_value=MagicMock(success=False, file_path=None))

    class _FakeMusicdlClient:
        async def search(self, query: str, limit: int = 5) -> object:
            track = MagicMock()
            result = MagicMock()
            result.tracks = [track]
            return result

        download = download_mock  # noqa: RUF012

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # Patch yt_dlp module and MusicdlClient at its source location
        with patch.dict("sys.modules", {"yt_dlp": fake_yt_dlp}):
            with patch(
                "rubetunes.providers.musicdl.client.MusicdlClient",
                return_value=_FakeMusicdlClient(),
            ):
                try:
                    await _download_spotify_track_locally(
                        "Test Song", "Test Artist", "mp3", tmp_dir, {}
                    )
                except Exception:
                    pass  # We only care that download was awaited, not that a file was found

    # The critical assertion: download was awaited (AsyncMock records awaits)
    assert download_mock.await_count >= 1, (
        "MusicdlClient.download must be awaited, not called in a thread"
    )


# ===========================================================================
# embed_metadata integration tests
# ===========================================================================


@pytest.mark.asyncio
async def test_spotify_downloader_calls_embed_metadata() -> None:
    """SpotifyDownloader.run should call embed_metadata with the audio path and info dict."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value="trackembed1")
    track_info = {
        "title": "Embed Test Track",
        "artists": ["Embed Artist"],
        "cover_url": None,
        "album": "Test Album",
        "release_date": "2024-01-01",
        "track_number": 1,
        "disc_number": 1,
    }
    fake_mod.get_track_info = MagicMock(return_value=track_info)
    fake_yt_dlp = _make_fake_yt_dlp()

    job = _make_job(platform="spotify", url="https://open.spotify.com/track/trackembed1")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    embed_calls: list[tuple] = []

    def _fake_embed(filepath, info):
        embed_calls.append((filepath, info))

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        with patch("rubetunes.tagging.embed_metadata", side_effect=_fake_embed):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(refs) >= 1
    assert len(embed_calls) >= 1, "embed_metadata should have been called at least once"
    called_path, called_info = embed_calls[0]
    assert called_path.suffix == ".mp3"
    assert called_info.get("title") == "Embed Test Track"


@pytest.mark.asyncio
async def test_spotify_downloader_embed_metadata_failure_does_not_abort() -> None:
    """A failure in embed_metadata must not abort the download/upload flow."""
    from kharej.downloaders.spotify import SpotifyDownloader

    fake_mod = MagicMock()
    fake_mod.parse_spotify_track_id = MagicMock(return_value="trackembed2")
    fake_mod.get_track_info = MagicMock(
        return_value={"title": "Resilient Track", "artists": ["Artist"], "cover_url": None}
    )
    fake_yt_dlp = _make_fake_yt_dlp()

    job = _make_job(platform="spotify", url="https://open.spotify.com/track/trackembed2")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    with patch.dict("sys.modules", {"spotify_dl": fake_mod, "yt_dlp": fake_yt_dlp}):
        with patch("rubetunes.tagging.embed_metadata", side_effect=RuntimeError("tag error")):
            downloader = SpotifyDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    # Upload should still succeed despite tagging failure
    assert len(refs) >= 1
    assert refs[0] is _DUMMY_REF


def test_youtube_build_command_does_not_include_write_info_json() -> None:
    """_build_command must not request --write-info-json (metadata fetch is for searcher only)."""
    from kharej.downloaders.youtube import _build_command

    cmd = _build_command(
        ytdlp_bin="/usr/bin/yt-dlp",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        outtmpl="/tmp/%(title)s.%(ext)s",
        quality="mp3",
        cookies_path=None,
    )
    assert "--write-info-json" not in cmd


@pytest.mark.asyncio
async def test_youtube_downloader_does_not_embed_info_json_metadata(tmp_path: Path) -> None:
    """YoutubeDownloader.run must not perform metadata embedding from info.json."""
    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        # Assert the command never requests --write-info-json
        assert "--write-info-json" not in cmd
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        audio_file = out_dir / "YouTube Test Track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 512)

    embed_calls: list[tuple] = []

    def _fake_embed(filepath, info):
        embed_calls.append((filepath, info))

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess), \
         patch("rubetunes.tagging.embed_metadata", side_effect=_fake_embed):
        downloader = YoutubeDownloader()
        refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    assert len(refs) == 1
    assert refs[0] is _DUMMY_REF
    assert len(embed_calls) == 0, "embed_metadata must not be called during download"


@pytest.mark.asyncio
async def test_youtube_downloader_embed_metadata_failure_does_not_abort() -> None:
    """A failure in embed_metadata must not abort the YouTube download/upload flow."""
    import json

    job = _make_job(quality="mp3")
    s2 = _make_s2(_DUMMY_REF)
    progress = _make_progress()
    settings = _make_settings()

    def _fake_subprocess(cmd, job_id, loop, progress_coro_factory):
        idx = cmd.index("--output")
        out_dir = Path(cmd[idx + 1]).parent
        audio_file = out_dir / "track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 64)
        info_json = out_dir / "track.info.json"
        info_json.write_text(json.dumps({"title": "track", "uploader": "someone"}), encoding="utf-8")

    with patch("kharej.downloaders.youtube._find_ytdlp", return_value="/usr/bin/yt-dlp"), \
         patch("kharej.downloaders.youtube._run_ytdlp_subprocess", side_effect=_fake_subprocess), \
         patch("rubetunes.tagging.embed_metadata", side_effect=RuntimeError("tag boom")):
        downloader = YoutubeDownloader()
        refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

    # Upload should still succeed despite tagging failure
    assert len(refs) == 1
    assert refs[0] is _DUMMY_REF


# ===========================================================================
# Quality-based download priority tests
# ===========================================================================


@pytest.mark.asyncio
async def test_spotify_flac_prefers_musicdl() -> None:
    """When quality='flac', musicdl should be tried before yt-dlp."""
    import tempfile
    import types

    from kharej.downloaders.spotify import _download_spotify_track_locally

    call_order: list[str] = []

    class _FakeMusicdlClient:
        async def search(self, query: str, limit: int = 5) -> object:
            call_order.append("musicdl_search")
            track = MagicMock()
            result = MagicMock()
            result.tracks = [track]
            return result

        async def download(self, track: object, dest_dir: Any) -> object:
            call_order.append("musicdl_download")
            audio = Path(dest_dir) / "track.flac"
            audio.write_bytes(b"fLaC" + b"\x00" * 64)
            return MagicMock(success=True, file_path=str(audio))

    fake_yt_dlp = MagicMock()

    # Build fake rubetunes.providers.musicdl.client module
    fake_musicdl_module = types.ModuleType("rubetunes.providers.musicdl.client")
    fake_musicdl_module.MusicdlClient = _FakeMusicdlClient  # type: ignore[attr-defined]

    fake_modules = {
        "yt_dlp": fake_yt_dlp,
        "rubetunes.providers.musicdl.client": fake_musicdl_module,
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.dict("sys.modules", fake_modules):
            result = await _download_spotify_track_locally(
                "Apocalypse Please", "Muse", "flac", tmp_dir, {}
            )

    assert result.suffix == ".flac"
    assert "musicdl_search" in call_order
    assert call_order[0] == "musicdl_search", "musicdl must be tried first for flac quality"
    # yt-dlp YoutubeDL should NOT have been instantiated since musicdl succeeded
    fake_yt_dlp.YoutubeDL.assert_not_called()


@pytest.mark.asyncio
async def test_spotify_mp3_prefers_ytdlp() -> None:
    """When quality='mp3', yt-dlp should be tried before musicdl."""
    import tempfile
    import types

    from kharej.downloaders.spotify import _download_spotify_track_locally

    call_order: list[str] = []

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            call_order.append("ytdlp_init")
            self._opts = opts

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            outtmpl = self._opts.get("outtmpl", "")
            out_dir = Path(outtmpl).parent
            (out_dir / "track.mp3").write_bytes(b"\xff\xfb" * 128)

    fake_yt_dlp = MagicMock()
    fake_yt_dlp.YoutubeDL = _FakeYDL

    musicdl_search_called: list[bool] = []

    class _FakeMusicdlClient:
        async def search(self, query: str, limit: int = 5) -> object:
            musicdl_search_called.append(True)
            result = MagicMock()
            result.tracks = []
            return result

    fake_musicdl_module = types.ModuleType("rubetunes.providers.musicdl.client")
    fake_musicdl_module.MusicdlClient = _FakeMusicdlClient  # type: ignore[attr-defined]

    fake_modules = {
        "yt_dlp": fake_yt_dlp,
        "rubetunes.providers.musicdl.client": fake_musicdl_module,
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.dict("sys.modules", fake_modules):
            result = await _download_spotify_track_locally(
                "Never Gonna Give You Up", "Rick Astley", "mp3", tmp_dir, {}
            )

    assert result.suffix == ".mp3"
    assert call_order[0] == "ytdlp_init", "yt-dlp must be tried first for mp3 quality"
    assert not musicdl_search_called, "musicdl must not be called when yt-dlp succeeds for mp3"


@pytest.mark.asyncio
async def test_spotify_flac_falls_back_to_ytdlp() -> None:
    """When musicdl fails for a flac request, yt-dlp should be used as backup."""
    import tempfile
    import types

    from kharej.downloaders.spotify import _download_spotify_track_locally

    class _FakeMusicdlClient:
        async def search(self, query: str, limit: int = 5) -> object:
            raise RuntimeError("musicdl unavailable")

    class _FakeYDL:
        def __init__(self, opts: dict) -> None:
            self._opts = opts

        def __enter__(self) -> "_FakeYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            outtmpl = self._opts.get("outtmpl", "")
            out_dir = Path(outtmpl).parent
            (out_dir / "track.flac").write_bytes(b"fLaC" + b"\x00" * 64)

    fake_yt_dlp = MagicMock()
    fake_yt_dlp.YoutubeDL = _FakeYDL

    fake_musicdl_module = types.ModuleType("rubetunes.providers.musicdl.client")
    fake_musicdl_module.MusicdlClient = _FakeMusicdlClient  # type: ignore[attr-defined]

    fake_modules = {
        "yt_dlp": fake_yt_dlp,
        "rubetunes.providers.musicdl.client": fake_musicdl_module,
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.dict("sys.modules", fake_modules):
            result = await _download_spotify_track_locally(
                "Apocalypse Please", "Muse", "flac", tmp_dir, {}
            )

    assert result.suffix == ".flac"


@pytest.mark.asyncio
async def test_spotify_mp3_falls_back_to_musicdl() -> None:
    """When yt-dlp fails for an mp3 request, musicdl should be used as backup."""
    import tempfile
    import types

    from kharej.downloaders.spotify import _download_spotify_track_locally

    class _FailYDL:
        def __init__(self, opts: dict) -> None:
            pass

        def __enter__(self) -> "_FailYDL":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def download(self, queries: list) -> None:
            raise RuntimeError("yt-dlp failed")

    fake_yt_dlp = MagicMock()
    fake_yt_dlp.YoutubeDL = _FailYDL

    class _FakeMusicdlClient:
        async def search(self, query: str, limit: int = 5) -> object:
            track = MagicMock()
            result = MagicMock()
            result.tracks = [track]
            return result

        async def download(self, track: object, dest_dir: Any) -> object:
            audio = Path(dest_dir) / "track.mp3"
            audio.write_bytes(b"\xff\xfb" * 128)
            return MagicMock(success=True, file_path=str(audio))

    fake_musicdl_module = types.ModuleType("rubetunes.providers.musicdl.client")
    fake_musicdl_module.MusicdlClient = _FakeMusicdlClient  # type: ignore[attr-defined]

    fake_modules = {
        "yt_dlp": fake_yt_dlp,
        "rubetunes.providers.musicdl.client": fake_musicdl_module,
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with patch.dict("sys.modules", fake_modules):
            result = await _download_spotify_track_locally(
                "Never Gonna Give You Up", "Rick Astley", "mp3", tmp_dir, {}
            )

    assert result.suffix == ".mp3"
