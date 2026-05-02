"""Tests for Step 9 — remaining platform downloaders.

Covers:
- kharej/downloaders/tidal.py     (TidalDownloader)
- kharej/downloaders/qobuz.py     (QobuzDownloader)
- kharej/downloaders/amazon.py    (AmazonDownloader)
- kharej/downloaders/soundcloud.py (SoundcloudDownloader)
- kharej/downloaders/bandcamp.py   (BandcampDownloader)
- kharej/downloaders/musicdl.py   (MusicdlDownloader)

All external I/O (spotify_dl, rubetunes providers, S2Client) is mocked —
no network calls, no subprocesses.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kharej.contracts import S2ObjectRef
from kharej.dispatcher import Job

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_JOB_ID = "step9-test-0000-0000-000000000001"

_DUMMY_REF = S2ObjectRef(
    key=f"media/{_JOB_ID}/track.flac",
    size=2048,
    mime="audio/flac",
    sha256="d" * 64,
)
_THUMB_REF = S2ObjectRef(
    key=f"thumbs/{_JOB_ID}.jpg",
    size=512,
    mime="image/jpeg",
    sha256="e" * 64,
)


def _make_job(
    *,
    job_id: str = _JOB_ID,
    platform: str = "tidal",
    url: str = "https://tidal.com/browse/track/12345678",
    quality: str = "flac",
    format_hint: str | None = None,
) -> Job:
    """Build a minimal Job-like object for testing."""
    from kharej.contracts import JobCreate

    msg = JobCreate.model_construct(
        v=1,
        ts=_NOW,
        job_id=job_id,
        user_id="user-test",
        platform=platform,
        url=url,
        quality=quality,
        job_type="single",
        user_status="active",
        format_hint=format_hint,
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
    settings.get = MagicMock(
        side_effect=lambda key, *args: _data.get(key, args[0] if args else None)
    )
    return settings


def _make_spodl_mock(
    *,
    track_id: str = "12345678",
    title: str = "Test Track",
    artists: list[str] | None = None,
    cover_url: str | None = None,
    audio_file: Path | None = None,
    parse_fn_name: str = "parse_tidal_track_id",
    info_fn_name: str = "get_tidal_track_info",
) -> MagicMock:
    """Build a MagicMock for the spotify_dl shim."""
    if artists is None:
        artists = ["Test Artist"]

    mod = MagicMock()
    getattr(mod, parse_fn_name).__class__ = MagicMock
    setattr(mod, parse_fn_name, MagicMock(return_value=track_id))

    info = {
        "title": title,
        "artists": artists,
        "cover_url": cover_url,
    }
    setattr(mod, info_fn_name, MagicMock(return_value=info))

    async def _fake_download_track(track_info, output_dir, ytdlp_bin):
        dest = Path(output_dir) / f"{artists[0]}_-_{title}.flac"
        dest.write_bytes(b"\x00" * 256)
        return dest

    mod.download_track = _fake_download_track
    return mod


# ===========================================================================
# TidalDownloader
# ===========================================================================


class TestTidalDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self) -> None:
        """TidalDownloader.run should upload audio and return S2ObjectRef."""
        from kharej.downloaders.tidal import TidalDownloader

        job = _make_job(platform="tidal", url="https://tidal.com/browse/track/12345678")
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_tidal_track_id",
            info_fn_name="get_tidal_track_info",
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = TidalDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) >= 1
        assert refs[0] is _DUMMY_REF
        s2.upload_file.assert_called()
        _, uploaded_key = s2.upload_file.call_args_list[0].args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")
        assert uploaded_key.endswith(".flac")

    @pytest.mark.asyncio
    async def test_bad_url_raises(self) -> None:
        """TidalDownloader.run should raise ValueError for unparseable URLs."""
        from kharej.downloaders.tidal import TidalDownloader

        fake_mod = MagicMock()
        fake_mod.parse_tidal_track_id = MagicMock(return_value=None)

        job = _make_job(platform="tidal", url="https://not-tidal.com/bad")
        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = TidalDownloader()
            with pytest.raises(ValueError, match="Tidal track ID"):
                await downloader.run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self) -> None:
        """TidalDownloader.run should call progress.report_progress."""
        from kharej.downloaders.tidal import TidalDownloader

        job = _make_job(platform="tidal")
        progress = _make_progress()
        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_tidal_track_id",
            info_fn_name="get_tidal_track_info",
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            await TidalDownloader().run(
                job, s2=_make_s2(), progress=progress, settings=_make_settings()
            )

        assert progress.report_progress.called

    @pytest.mark.asyncio
    async def test_thumbnail_failure_does_not_abort(self) -> None:
        """A thumbnail upload failure must not abort the overall job."""
        from kharej.downloaders.tidal import TidalDownloader

        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_tidal_track_id",
            info_fn_name="get_tidal_track_info",
            cover_url="https://example.com/cover.jpg",
        )
        # urlretrieve raises — thumbnail should be silently skipped
        with patch("urllib.request.urlretrieve", side_effect=OSError("no network")):
            with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
                refs = await TidalDownloader().run(
                    _make_job(platform="tidal"),
                    s2=_make_s2(),
                    progress=_make_progress(),
                    settings=_make_settings(),
                )
        # Must still return at least the audio ref
        assert len(refs) >= 1

    @pytest.mark.asyncio
    async def test_uploads_thumbnail_when_cover_url_provided(self) -> None:
        """TidalDownloader.run should upload thumbnail when cover_url is present."""
        from kharej.downloaders.tidal import TidalDownloader

        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_tidal_track_id",
            info_fn_name="get_tidal_track_info",
            cover_url="https://example.com/cover.jpg",
        )

        upload_calls: list[str] = []

        def _upload_side_effect(local_path, key, **kwargs):
            upload_calls.append(key)
            if "thumbs" in key:
                return _THUMB_REF
            return _DUMMY_REF

        s2 = MagicMock()
        s2.upload_file = MagicMock(side_effect=_upload_side_effect)

        def _fake_urlretrieve(url, dest):
            Path(dest).write_bytes(b"\xff\xd8\xff" * 50)
            return dest, {}

        with patch("urllib.request.urlretrieve", side_effect=_fake_urlretrieve):
            with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
                refs = await TidalDownloader().run(
                    _make_job(platform="tidal"),
                    s2=s2,
                    progress=_make_progress(),
                    settings=_make_settings(),
                )

        assert len(refs) == 2
        keys = {r.key for r in refs}
        assert any("thumbs" in k for k in keys)
        assert any("media" in k for k in keys)


# ===========================================================================
# QobuzDownloader
# ===========================================================================


class TestQobuzDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self) -> None:
        """QobuzDownloader.run should upload audio and return S2ObjectRef."""
        from kharej.downloaders.qobuz import QobuzDownloader

        job = _make_job(platform="qobuz", url="https://open.qobuz.com/track/99999999")
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        fake_mod = _make_spodl_mock(
            track_id="99999999",
            parse_fn_name="parse_qobuz_track_id",
            info_fn_name="get_qobuz_track_info",
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = QobuzDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) >= 1
        assert refs[0] is _DUMMY_REF
        _, uploaded_key = s2.upload_file.call_args_list[0].args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")
        assert uploaded_key.endswith(".flac")

    @pytest.mark.asyncio
    async def test_bad_url_raises(self) -> None:
        """QobuzDownloader.run should raise ValueError for unparseable URLs."""
        from kharej.downloaders.qobuz import QobuzDownloader

        fake_mod = MagicMock()
        fake_mod.parse_qobuz_track_id = MagicMock(return_value=None)

        job = _make_job(platform="qobuz", url="https://not-qobuz.com/bad")
        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            with pytest.raises(ValueError, match="Qobuz track ID"):
                await QobuzDownloader().run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self) -> None:
        """QobuzDownloader.run should call progress.report_progress."""
        from kharej.downloaders.qobuz import QobuzDownloader

        job = _make_job(platform="qobuz", url="https://open.qobuz.com/track/99999999")
        progress = _make_progress()
        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_qobuz_track_id",
            info_fn_name="get_qobuz_track_info",
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            await QobuzDownloader().run(
                job, s2=_make_s2(), progress=progress, settings=_make_settings()
            )

        assert progress.report_progress.called

    @pytest.mark.asyncio
    async def test_thumbnail_failure_does_not_abort(self) -> None:
        """A thumbnail upload failure must not abort the overall job."""
        from kharej.downloaders.qobuz import QobuzDownloader

        fake_mod = _make_spodl_mock(
            parse_fn_name="parse_qobuz_track_id",
            info_fn_name="get_qobuz_track_info",
            cover_url="https://example.com/cover.jpg",
        )
        with patch("urllib.request.urlretrieve", side_effect=OSError("no network")):
            with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
                refs = await QobuzDownloader().run(
                    _make_job(platform="qobuz", url="https://open.qobuz.com/track/99999999"),
                    s2=_make_s2(),
                    progress=_make_progress(),
                    settings=_make_settings(),
                )
        assert len(refs) >= 1


# ===========================================================================
# AmazonDownloader
# ===========================================================================


class TestAmazonDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self) -> None:
        """AmazonDownloader.run should upload audio and return S2ObjectRef."""
        from kharej.downloaders.amazon import AmazonDownloader

        job = _make_job(
            platform="amazon", url="https://music.amazon.com/tracks/B0ABCDE12345"
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        fake_mod = _make_spodl_mock(
            track_id="B0ABCDE12345",
            parse_fn_name="parse_amazon_track_id",
            info_fn_name="get_amazon_track_info",
        )
        # get_amazon_track_info takes (track_id, ytdlp_bin) — adjust mock
        fake_mod.get_amazon_track_info = MagicMock(
            return_value={
                "title": "Test Track",
                "artists": ["Test Artist"],
                "cover_url": None,
            }
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            downloader = AmazonDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) >= 1
        assert refs[0] is _DUMMY_REF
        _, uploaded_key = s2.upload_file.call_args_list[0].args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")

    @pytest.mark.asyncio
    async def test_bad_url_raises(self) -> None:
        """AmazonDownloader.run should raise ValueError for unparseable URLs."""
        from kharej.downloaders.amazon import AmazonDownloader

        fake_mod = MagicMock()
        fake_mod.parse_amazon_track_id = MagicMock(return_value=None)

        job = _make_job(platform="amazon", url="https://not-amazon.com/bad")
        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            with pytest.raises(ValueError, match="Amazon Music track ID"):
                await AmazonDownloader().run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self) -> None:
        """AmazonDownloader.run should call progress.report_progress."""
        from kharej.downloaders.amazon import AmazonDownloader

        job = _make_job(
            platform="amazon", url="https://music.amazon.com/tracks/B0ABCDE12345"
        )
        progress = _make_progress()
        fake_mod = _make_spodl_mock(
            track_id="B0ABCDE12345",
            parse_fn_name="parse_amazon_track_id",
            info_fn_name="get_amazon_track_info",
        )
        fake_mod.get_amazon_track_info = MagicMock(
            return_value={"title": "Track", "artists": ["Artist"], "cover_url": None}
        )

        with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
            await AmazonDownloader().run(
                job, s2=_make_s2(), progress=progress, settings=_make_settings()
            )

        assert progress.report_progress.called

    @pytest.mark.asyncio
    async def test_thumbnail_failure_does_not_abort(self) -> None:
        """A thumbnail upload failure must not abort the overall job."""
        from kharej.downloaders.amazon import AmazonDownloader

        fake_mod = _make_spodl_mock(
            track_id="B0ABCDE12345",
            parse_fn_name="parse_amazon_track_id",
            info_fn_name="get_amazon_track_info",
            cover_url="https://example.com/cover.jpg",
        )
        fake_mod.get_amazon_track_info = MagicMock(
            return_value={
                "title": "Track",
                "artists": ["Artist"],
                "cover_url": "https://example.com/cover.jpg",
            }
        )
        with patch("urllib.request.urlretrieve", side_effect=OSError("no network")):
            with patch.dict("sys.modules", {"spotify_dl": fake_mod}):
                refs = await AmazonDownloader().run(
                    _make_job(
                        platform="amazon",
                        url="https://music.amazon.com/tracks/B0ABCDE12345",
                    ),
                    s2=_make_s2(),
                    progress=_make_progress(),
                    settings=_make_settings(),
                )
        assert len(refs) >= 1


# ===========================================================================
# SoundcloudDownloader
# ===========================================================================


class TestSoundcloudDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self, tmp_path: Path) -> None:
        """SoundcloudDownloader.run should upload audio and return S2ObjectRef."""
        from kharej.downloaders.soundcloud import SoundcloudDownloader

        job = _make_job(
            platform="soundcloud",
            url="https://soundcloud.com/artist/track-name",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        # Fake download_soundcloud: writes a .mp3 file and returns it
        async def _fake_download(url, download_dir, ytdlp_bin, safe_name="soundcloud_track"):
            dest = Path(download_dir) / f"{safe_name}.mp3"
            dest.write_bytes(b"\xff\xfb" * 128)
            return dest

        with patch(
            "rubetunes.providers.soundcloud.download_soundcloud",
            side_effect=_fake_download,
        ):
            with patch(
                "rubetunes.providers.soundcloud.parse_soundcloud_url",
                return_value="https://soundcloud.com/artist/track-name",
            ):
                downloader = SoundcloudDownloader()
                refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1
        assert refs[0] is _DUMMY_REF
        _, uploaded_key = s2.upload_file.call_args.args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")
        assert uploaded_key.endswith(".mp3")

    @pytest.mark.asyncio
    async def test_bad_url_raises(self) -> None:
        """SoundcloudDownloader.run should raise ValueError for non-SoundCloud URLs."""
        from kharej.downloaders.soundcloud import SoundcloudDownloader

        job = _make_job(platform="soundcloud", url="https://not-soundcloud.com/bad")

        with patch(
            "rubetunes.providers.soundcloud.parse_soundcloud_url",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="SoundCloud URL"):
                await SoundcloudDownloader().run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self) -> None:
        """SoundcloudDownloader.run should call progress.report_progress."""
        from kharej.downloaders.soundcloud import SoundcloudDownloader

        job = _make_job(
            platform="soundcloud",
            url="https://soundcloud.com/artist/track-name",
        )
        progress = _make_progress()

        async def _fake_download(url, download_dir, ytdlp_bin, safe_name="soundcloud_track"):
            dest = Path(download_dir) / f"{safe_name}.mp3"
            dest.write_bytes(b"\x00" * 64)
            return dest

        with patch("rubetunes.providers.soundcloud.download_soundcloud", side_effect=_fake_download):
            with patch(
                "rubetunes.providers.soundcloud.parse_soundcloud_url",
                return_value="https://soundcloud.com/artist/track-name",
            ):
                await SoundcloudDownloader().run(
                    job, s2=_make_s2(), progress=progress, settings=_make_settings()
                )

        assert progress.report_progress.called


# ===========================================================================
# BandcampDownloader
# ===========================================================================


class TestBandcampDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self) -> None:
        """BandcampDownloader.run should upload audio and return S2ObjectRef."""
        from kharej.downloaders.bandcamp import BandcampDownloader

        job = _make_job(
            platform="bandcamp",
            url="https://artist.bandcamp.com/track/some-track",
        )
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        async def _fake_download(url, download_dir, ytdlp_bin, safe_name="bandcamp_track"):
            dest = Path(download_dir) / f"{safe_name}.flac"
            dest.write_bytes(b"\x00" * 256)
            return dest

        with patch(
            "rubetunes.providers.bandcamp.download_bandcamp",
            side_effect=_fake_download,
        ):
            with patch(
                "rubetunes.providers.bandcamp.parse_bandcamp_url",
                return_value="https://artist.bandcamp.com/track/some-track",
            ):
                downloader = BandcampDownloader()
                refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1
        assert refs[0] is _DUMMY_REF
        _, uploaded_key = s2.upload_file.call_args.args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")
        assert uploaded_key.endswith(".flac")

    @pytest.mark.asyncio
    async def test_bad_url_raises(self) -> None:
        """BandcampDownloader.run should raise ValueError for non-Bandcamp URLs."""
        from kharej.downloaders.bandcamp import BandcampDownloader

        job = _make_job(platform="bandcamp", url="https://not-bandcamp.com/bad")

        with patch(
            "rubetunes.providers.bandcamp.parse_bandcamp_url",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="Bandcamp URL"):
                await BandcampDownloader().run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self) -> None:
        """BandcampDownloader.run should call progress.report_progress."""
        from kharej.downloaders.bandcamp import BandcampDownloader

        job = _make_job(
            platform="bandcamp",
            url="https://artist.bandcamp.com/track/some-track",
        )
        progress = _make_progress()

        async def _fake_download(url, download_dir, ytdlp_bin, safe_name="bandcamp_track"):
            dest = Path(download_dir) / f"{safe_name}.flac"
            dest.write_bytes(b"\x00" * 64)
            return dest

        with patch("rubetunes.providers.bandcamp.download_bandcamp", side_effect=_fake_download):
            with patch(
                "rubetunes.providers.bandcamp.parse_bandcamp_url",
                return_value="https://artist.bandcamp.com/track/some-track",
            ):
                await BandcampDownloader().run(
                    job, s2=_make_s2(), progress=progress, settings=_make_settings()
                )

        assert progress.report_progress.called


# ===========================================================================
# MusicdlDownloader
# ===========================================================================


def _make_musicdl_track(
    *,
    song_name: str = "Test Song",
    singers: str = "Test Artist",
    source: str = "NeteaseMusicClient",
    ext: str = "mp3",
    file_path: str = "",
) -> Any:
    """Build a MusicdlTrack-like object."""
    from rubetunes.providers.musicdl.models import MusicdlTrack

    track = MusicdlTrack(
        song_name=song_name,
        singers=singers,
        source=source,
        ext=ext,
        file_path=file_path,
    )
    return track


class TestMusicdlDownloader:
    @pytest.mark.asyncio
    async def test_uploads_and_returns_ref(self, tmp_path: Path) -> None:
        """MusicdlDownloader.run should search, download, upload and return S2ObjectRef."""
        from kharej.downloaders.musicdl import MusicdlDownloader
        from rubetunes.providers.musicdl.models import MusicdlDownloadResult, MusicdlSearchResult

        job = _make_job(platform="musicdl", url="Bohemian Rhapsody Queen")
        s2 = _make_s2(_DUMMY_REF)
        progress = _make_progress()
        settings = _make_settings()

        # Create a fake audio file that the downloader will find
        fake_audio = tmp_path / "bohemian.mp3"
        fake_audio.write_bytes(b"\xff\xfb" * 256)

        track = _make_musicdl_track(
            song_name="Bohemian Rhapsody",
            singers="Queen",
            file_path=str(fake_audio),
        )

        search_result = MusicdlSearchResult(
            query="Bohemian Rhapsody Queen",
            tracks=[track],
            by_source={"NeteaseMusicClient": [track]},
            total=1,
        )
        dl_result = MusicdlDownloadResult(
            track=track,
            file_path=fake_audio,
            success=True,
            error="",
        )

        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=search_result)
        mock_client.download = AsyncMock(return_value=dl_result)

        # MusicdlClient is imported inside run() via
        # `from rubetunes.providers.musicdl import MusicdlClient`
        # so we patch it at its source module.
        with patch("rubetunes.providers.musicdl.MusicdlClient", return_value=mock_client):
            downloader = MusicdlDownloader()
            refs = await downloader.run(job, s2=s2, progress=progress, settings=settings)

        assert len(refs) == 1
        assert refs[0] is _DUMMY_REF
        _, uploaded_key = s2.upload_file.call_args.args
        assert uploaded_key.startswith(f"media/{_JOB_ID}/")

    @pytest.mark.asyncio
    async def test_empty_query_raises(self) -> None:
        """MusicdlDownloader.run should raise ValueError for empty url/query."""
        from kharej.downloaders.musicdl import MusicdlDownloader

        job = _make_job(platform="musicdl", url="   ")

        with pytest.raises(ValueError, match="search query"):
            await MusicdlDownloader().run(
                job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
            )

    @pytest.mark.asyncio
    async def test_no_results_raises(self) -> None:
        """MusicdlDownloader.run should raise RuntimeError when search returns no tracks."""
        from kharej.downloaders.musicdl import MusicdlDownloader
        from rubetunes.providers.musicdl.models import MusicdlSearchResult

        job = _make_job(platform="musicdl", url="xyzzy this does not exist")

        empty_result = MusicdlSearchResult(query="xyzzy", tracks=[], by_source={}, total=0)
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=empty_result)

        with patch("rubetunes.providers.musicdl.MusicdlClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="no results"):
                await MusicdlDownloader().run(
                    job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
                )

    @pytest.mark.asyncio
    async def test_reports_progress(self, tmp_path: Path) -> None:
        """MusicdlDownloader.run should call progress.report_progress."""
        from kharej.downloaders.musicdl import MusicdlDownloader
        from rubetunes.providers.musicdl.models import MusicdlDownloadResult, MusicdlSearchResult

        job = _make_job(platform="musicdl", url="Shape of You")
        progress = _make_progress()

        fake_audio = tmp_path / "track.mp3"
        fake_audio.write_bytes(b"\x00" * 128)

        track = _make_musicdl_track(file_path=str(fake_audio))
        search_result = MusicdlSearchResult(query="Shape of You", tracks=[track], by_source={}, total=1)
        dl_result = MusicdlDownloadResult(
            track=track, file_path=fake_audio, success=True, error=""
        )

        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=search_result)
        mock_client.download = AsyncMock(return_value=dl_result)

        with patch("rubetunes.providers.musicdl.MusicdlClient", return_value=mock_client):
            await MusicdlDownloader().run(
                job, s2=_make_s2(), progress=progress, settings=_make_settings()
            )

        assert progress.report_progress.called

    @pytest.mark.asyncio
    async def test_falls_back_to_next_result_on_failure(self, tmp_path: Path) -> None:
        """MusicdlDownloader.run should try subsequent results when one fails."""
        from kharej.downloaders.musicdl import MusicdlDownloader
        from rubetunes.providers.musicdl.models import MusicdlDownloadResult, MusicdlSearchResult

        job = _make_job(platform="musicdl", url="Some Track")

        # First track: download fails; second track: succeeds
        fake_audio = tmp_path / "track2.mp3"
        fake_audio.write_bytes(b"\x00" * 128)

        track1 = _make_musicdl_track(song_name="Track1")
        track2 = _make_musicdl_track(song_name="Track2", file_path=str(fake_audio))

        search_result = MusicdlSearchResult(
            query="Some Track", tracks=[track1, track2], total=2
        )

        def _dl_side_effect(track, dest_dir=None):
            if track.song_name == "Track1":
                # Return failure result
                return MusicdlDownloadResult(track=track, file_path=Path(""), success=False, error="error")
            return MusicdlDownloadResult(track=track, file_path=fake_audio, success=True, error="")

        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=search_result)
        mock_client.download = AsyncMock(side_effect=_dl_side_effect)

        with patch("rubetunes.providers.musicdl.MusicdlClient", return_value=mock_client):
            refs = await MusicdlDownloader().run(
                job, s2=_make_s2(), progress=_make_progress(), settings=_make_settings()
            )

        # Should have succeeded using track2
        assert len(refs) == 1


# ===========================================================================
# Dispatcher registration tests
# ===========================================================================


class TestDispatcherRegistration:
    """Verify the dispatcher registers all Step 9 platform downloaders."""

    def _make_dispatcher(self) -> Any:
        """Build a Dispatcher with default (real) downloaders."""
        import atexit
        import shutil
        import tempfile

        from kharej.access_control import AccessControl
        from kharej.dispatcher import Dispatcher
        from kharej.progress_reporter import ProgressReporter
        from kharej.settings import KharejSettings

        td = tempfile.mkdtemp()
        atexit.register(shutil.rmtree, td, True)

        access = AccessControl(state_path=Path(td) / "access.json")
        settings = KharejSettings()
        send = AsyncMock()
        progress = ProgressReporter(send, throttle_sec=0.0)
        s2 = MagicMock()
        rubika = MagicMock()
        rubika.send = send

        return Dispatcher(
            s2=s2,
            rubika=rubika,
            access=access,
            settings=settings,
            progress=progress,
        )

    @pytest.mark.parametrize(
        "platform",
        ["tidal", "qobuz", "amazon", "soundcloud", "bandcamp", "musicdl"],
    )
    def test_dispatcher_has_step9_platform(self, platform: str) -> None:
        """Dispatcher.has() must return True for each Step 9 platform."""
        dispatcher = self._make_dispatcher()
        assert dispatcher.has(platform), (
            f"Dispatcher does not have a downloader registered for platform {platform!r}"
        )

    def test_dispatcher_has_all_platforms(self) -> None:
        """Dispatcher must have all eight canonical platforms registered."""
        dispatcher = self._make_dispatcher()
        required = {"youtube", "spotify", "tidal", "qobuz", "amazon", "soundcloud", "bandcamp", "musicdl"}
        missing = {p for p in required if not dispatcher.has(p)}
        assert not missing, f"Dispatcher missing platforms: {missing}"
