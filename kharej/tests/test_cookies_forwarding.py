"""Tests for cookies forwarding across all kharej downloaders and rubetunes providers.

Verifies that when ``cookies_path`` is configured (via ``KHAREJ_COOKIES_PATH`` /
``settings["cookies_path"]``), every downloader resolves and forwards it to the
underlying yt-dlp subprocess as ``--cookies <path>``.

Covered:
- rubetunes.providers.soundcloud.download_soundcloud
- rubetunes.providers.bandcamp.download_bandcamp
- kharej.downloaders.soundcloud.SoundcloudDownloader
- kharej.downloaders.bandcamp.BandcampDownloader
- kharej.downloaders.tidal.TidalDownloader (ytdlp_bin from settings + cookies)
- kharej.downloaders.qobuz.QobuzDownloader  (ytdlp_bin from settings + cookies)
- kharej.downloaders.amazon.AmazonDownloader
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kharej.contracts import S2ObjectRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_JOB_ID = "cccccccc-0000-0000-0000-000000000099"
_DUMMY_REF = S2ObjectRef(
    key=f"media/{_JOB_ID}/track.mp3",
    size=1024,
    mime="audio/mpeg",
    sha256="c" * 64,
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


def _make_job(
    *,
    job_id: str = _JOB_ID,
    platform: str = "soundcloud",
    url: str = "https://soundcloud.com/artist/track",
    quality: str = "mp3",
) -> Any:
    from kharej.contracts import JobCreate, Platform
    from kharej.dispatcher import Job

    platform_enum = getattr(Platform, platform, Platform.soundcloud)
    msg = JobCreate.model_construct(
        v=1,
        ts=_NOW,
        job_id=job_id,
        user_id="user-test",
        platform=platform_enum,
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


# ===========================================================================
# rubetunes.providers.soundcloud — download_soundcloud
# ===========================================================================


class TestDownloadSoundcloudCookies:
    """rubetunes.providers.soundcloud.download_soundcloud forwards --cookies."""

    @pytest.mark.asyncio
    async def test_cookies_appended_when_file_exists(self, tmp_path: Path) -> None:
        """--cookies <path> should appear in the yt-dlp command when the file exists."""
        from rubetunes.providers.soundcloud import download_soundcloud

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")

        audio_file = tmp_path / "soundcloud_track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 32)

        captured_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: str, **kwargs: object) -> "asyncio.subprocess.Process":  # type: ignore[name-defined]
            captured_cmds.append(list(cmd))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await download_soundcloud(
                "https://soundcloud.com/a/b",
                tmp_path,
                "yt-dlp",
                cookies_path=str(cookies_file),
            )

        assert captured_cmds, "yt-dlp was never called"
        cmd = captured_cmds[0]
        assert "--cookies" in cmd
        idx = cmd.index("--cookies")
        assert cmd[idx + 1] == str(cookies_file)

    @pytest.mark.asyncio
    async def test_no_cookies_when_not_provided(self, tmp_path: Path) -> None:
        """--cookies must NOT appear when cookies_path is None."""
        from rubetunes.providers.soundcloud import download_soundcloud

        audio_file = tmp_path / "soundcloud_track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 32)

        captured_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: str, **kwargs: object) -> "asyncio.subprocess.Process":  # type: ignore[name-defined]
            captured_cmds.append(list(cmd))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await download_soundcloud(
                "https://soundcloud.com/a/b",
                tmp_path,
                "yt-dlp",
            )

        assert captured_cmds
        assert "--cookies" not in captured_cmds[0]

    @pytest.mark.asyncio
    async def test_no_cookies_when_file_missing(self, tmp_path: Path) -> None:
        """--cookies must NOT appear when cookies_path points to a non-existent file."""
        from rubetunes.providers.soundcloud import download_soundcloud

        audio_file = tmp_path / "soundcloud_track.mp3"
        audio_file.write_bytes(b"\xff\xfb" * 32)

        captured_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: str, **kwargs: object) -> "asyncio.subprocess.Process":  # type: ignore[name-defined]
            captured_cmds.append(list(cmd))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await download_soundcloud(
                "https://soundcloud.com/a/b",
                tmp_path,
                "yt-dlp",
                cookies_path="/nonexistent/cookies.txt",
            )

        assert captured_cmds
        assert "--cookies" not in captured_cmds[0]


# ===========================================================================
# rubetunes.providers.bandcamp — download_bandcamp
# ===========================================================================


class TestDownloadBandcampCookies:
    """rubetunes.providers.bandcamp.download_bandcamp forwards --cookies."""

    @pytest.mark.asyncio
    async def test_cookies_appended_when_file_exists(self, tmp_path: Path) -> None:
        from rubetunes.providers.bandcamp import download_bandcamp

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")

        audio_file = tmp_path / "bandcamp_track.flac"
        audio_file.write_bytes(b"fLaC" + b"\x00" * 28)

        captured_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: str, **kwargs: object) -> "asyncio.subprocess.Process":  # type: ignore[name-defined]
            captured_cmds.append(list(cmd))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await download_bandcamp(
                "https://artist.bandcamp.com/track/song",
                tmp_path,
                "yt-dlp",
                cookies_path=str(cookies_file),
            )

        assert captured_cmds, "yt-dlp was never called"
        cmd = captured_cmds[0]
        assert "--cookies" in cmd
        idx = cmd.index("--cookies")
        assert cmd[idx + 1] == str(cookies_file)

    @pytest.mark.asyncio
    async def test_no_cookies_when_not_provided(self, tmp_path: Path) -> None:
        from rubetunes.providers.bandcamp import download_bandcamp

        audio_file = tmp_path / "bandcamp_track.flac"
        audio_file.write_bytes(b"fLaC" + b"\x00" * 28)

        captured_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: str, **kwargs: object) -> "asyncio.subprocess.Process":  # type: ignore[name-defined]
            captured_cmds.append(list(cmd))
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            await download_bandcamp(
                "https://artist.bandcamp.com/track/song",
                tmp_path,
                "yt-dlp",
            )

        assert captured_cmds
        assert "--cookies" not in captured_cmds[0]


# ===========================================================================
# kharej.downloaders.soundcloud.SoundcloudDownloader
# ===========================================================================


class TestSoundcloudDownloaderCookies:
    """SoundcloudDownloader.run passes cookies_path to download_soundcloud."""

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_download_soundcloud(self, tmp_path: Path) -> None:
        from kharej.downloaders.soundcloud import SoundcloudDownloader

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")
        settings = _make_settings({"cookies_path": str(cookies_file)})

        job = _make_job(
            platform="soundcloud",
            url="https://soundcloud.com/artist/the-track",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_kwargs: list[dict] = []

        async def _fake_download_soundcloud(**kwargs: object) -> Path:
            captured_kwargs.append(dict(kwargs))
            audio = tmp_path / "the-track.mp3"
            audio.write_bytes(b"\xff\xfb" * 32)
            return audio

        with patch(
            "rubetunes.providers.soundcloud.download_soundcloud",
            side_effect=_fake_download_soundcloud,
        ):
            downloader = SoundcloudDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_kwargs, "download_soundcloud was never called"
        assert captured_kwargs[0].get("cookies_path") == str(cookies_file)


# ===========================================================================
# kharej.downloaders.bandcamp.BandcampDownloader
# ===========================================================================


class TestBandcampDownloaderCookies:
    """BandcampDownloader.run passes cookies_path to download_bandcamp."""

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_download_bandcamp(self, tmp_path: Path) -> None:
        from kharej.downloaders.bandcamp import BandcampDownloader

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")
        settings = _make_settings({"cookies_path": str(cookies_file)})

        job = _make_job(
            platform="bandcamp",
            url="https://artist.bandcamp.com/track/the-track",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_kwargs: list[dict] = []

        async def _fake_download_bandcamp(**kwargs: object) -> Path:
            captured_kwargs.append(dict(kwargs))
            audio = tmp_path / "the-track.flac"
            audio.write_bytes(b"fLaC" + b"\x00" * 28)
            return audio

        with patch(
            "rubetunes.providers.bandcamp.download_bandcamp",
            side_effect=_fake_download_bandcamp,
        ):
            downloader = BandcampDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_kwargs, "download_bandcamp was never called"
        assert captured_kwargs[0].get("cookies_path") == str(cookies_file)


# ===========================================================================
# kharej.downloaders.tidal.TidalDownloader
# ===========================================================================


class TestTidalDownloaderCookies:
    """TidalDownloader.run resolves ytdlp_bin from settings and passes cookies."""

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_download_track(self, tmp_path: Path) -> None:
        from kharej.downloaders.tidal import TidalDownloader

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")
        settings = _make_settings(
            {
                "cookies_path": str(cookies_file),
                "ytdlp_bin": "yt-dlp",
            }
        )

        job = _make_job(
            platform="tidal",
            url="https://tidal.com/browse/track/12345678",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_calls: list[dict] = []

        async def _fake_download_track(info: dict, output_dir: object, ytdlp_bin: str, *, cookies_path: str | None = None) -> Path:  # type: ignore[override]
            captured_calls.append({"cookies_path": cookies_path, "ytdlp_bin": ytdlp_bin})
            audio = tmp_path / "tidal.mp3"
            audio.write_bytes(b"\xff\xfb" * 32)
            return audio

        fake_mod = MagicMock()
        fake_mod.parse_tidal_track_id = MagicMock(return_value="12345678")
        fake_mod.get_tidal_track_info = MagicMock(
            return_value={"title": "My Song", "artists": ["Artist"], "cover_url": None}
        )
        fake_mod.download_track = _fake_download_track

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = TidalDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_calls, "download_track was never called"
        assert captured_calls[0]["cookies_path"] == str(cookies_file)

    @pytest.mark.asyncio
    async def test_ytdlp_bin_from_settings(self, tmp_path: Path) -> None:
        """ytdlp_bin should come from settings, not be hardcoded to 'yt-dlp'."""
        from kharej.downloaders.tidal import TidalDownloader

        custom_bin = "/opt/yt-dlp/bin/yt-dlp"
        settings = _make_settings({"ytdlp_bin": custom_bin})

        job = _make_job(
            platform="tidal",
            url="https://tidal.com/browse/track/12345678",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_calls: list[dict] = []

        async def _fake_download_track(info: dict, output_dir: object, ytdlp_bin: str, *, cookies_path: str | None = None) -> Path:  # type: ignore[override]
            captured_calls.append({"ytdlp_bin": ytdlp_bin})
            audio = tmp_path / "tidal.mp3"
            audio.write_bytes(b"\xff\xfb" * 32)
            return audio

        fake_mod = MagicMock()
        fake_mod.parse_tidal_track_id = MagicMock(return_value="12345678")
        fake_mod.get_tidal_track_info = MagicMock(
            return_value={"title": "My Song", "artists": ["Artist"], "cover_url": None}
        )
        fake_mod.download_track = _fake_download_track

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = TidalDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_calls
        assert captured_calls[0]["ytdlp_bin"] == custom_bin


# ===========================================================================
# kharej.downloaders.qobuz.QobuzDownloader
# ===========================================================================


class TestQobuzDownloaderCookies:
    """QobuzDownloader.run resolves ytdlp_bin from settings and passes cookies."""

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_download_track(self, tmp_path: Path) -> None:
        from kharej.downloaders.qobuz import QobuzDownloader

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")
        settings = _make_settings(
            {
                "cookies_path": str(cookies_file),
                "ytdlp_bin": "yt-dlp",
            }
        )

        job = _make_job(
            platform="qobuz",
            url="https://www.qobuz.com/us-en/album/some-title/87654321",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_calls: list[dict] = []

        async def _fake_download_track(info: dict, output_dir: object, ytdlp_bin: str, *, cookies_path: str | None = None) -> Path:  # type: ignore[override]
            captured_calls.append({"cookies_path": cookies_path, "ytdlp_bin": ytdlp_bin})
            audio = tmp_path / "qobuz.flac"
            audio.write_bytes(b"fLaC" + b"\x00" * 28)
            return audio

        fake_mod = MagicMock()
        fake_mod.parse_qobuz_track_id = MagicMock(return_value="87654321")
        fake_mod.get_qobuz_track_info = MagicMock(
            return_value={"title": "Classic", "artists": ["Composer"], "cover_url": None}
        )
        fake_mod.download_track = _fake_download_track

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = QobuzDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_calls, "download_track was never called"
        assert captured_calls[0]["cookies_path"] == str(cookies_file)

    @pytest.mark.asyncio
    async def test_ytdlp_bin_from_settings(self, tmp_path: Path) -> None:
        from kharej.downloaders.qobuz import QobuzDownloader

        custom_bin = "/usr/local/bin/yt-dlp"
        settings = _make_settings({"ytdlp_bin": custom_bin})

        job = _make_job(
            platform="qobuz",
            url="https://www.qobuz.com/us-en/album/x/11111111",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_calls: list[dict] = []

        async def _fake_download_track(info: dict, output_dir: object, ytdlp_bin: str, *, cookies_path: str | None = None) -> Path:  # type: ignore[override]
            captured_calls.append({"ytdlp_bin": ytdlp_bin})
            audio = tmp_path / "qobuz.flac"
            audio.write_bytes(b"fLaC" + b"\x00" * 28)
            return audio

        fake_mod = MagicMock()
        fake_mod.parse_qobuz_track_id = MagicMock(return_value="11111111")
        fake_mod.get_qobuz_track_info = MagicMock(
            return_value={"title": "Classic", "artists": ["Composer"], "cover_url": None}
        )
        fake_mod.download_track = _fake_download_track

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = QobuzDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_calls
        assert captured_calls[0]["ytdlp_bin"] == custom_bin


# ===========================================================================
# kharej.downloaders.amazon.AmazonDownloader
# ===========================================================================


class TestAmazonDownloaderCookies:
    """AmazonDownloader.run resolves and passes cookies_path to download_track."""

    @pytest.mark.asyncio
    async def test_cookies_forwarded_to_download_track(self, tmp_path: Path) -> None:
        from kharej.downloaders.amazon import AmazonDownloader

        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n")
        settings = _make_settings({"cookies_path": str(cookies_file)})

        job = _make_job(
            platform="amazon",
            url="https://music.amazon.com/tracks/B09ABCDEF",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()

        captured_calls: list[dict] = []

        async def _fake_download_track(info: dict, output_dir: object, ytdlp_bin: str, *, cookies_path: str | None = None) -> Path:  # type: ignore[override]
            captured_calls.append({"cookies_path": cookies_path, "ytdlp_bin": ytdlp_bin})
            audio = tmp_path / "amazon.mp3"
            audio.write_bytes(b"\xff\xfb" * 32)
            return audio

        fake_mod = MagicMock()
        fake_mod.parse_amazon_track_id = MagicMock(return_value="B09ABCDEF")
        fake_mod.get_amazon_track_info = MagicMock(
            return_value={"title": "Pop Song", "artists": ["Pop Star"], "cover_url": None}
        )
        fake_mod.download_track = _fake_download_track

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = AmazonDownloader()
            await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert captured_calls, "download_track was never called"
        assert captured_calls[0]["cookies_path"] == str(cookies_file)
