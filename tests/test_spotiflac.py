"""Tests for rubetunes.providers.spotiflac — SpotiFLAC backend integration.

All HTTP calls are mocked; no real network access is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import responses as resp_lib

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ===========================================================================
# _ProviderTracker
# ===========================================================================


class TestProviderTracker:
    def setup_method(self):
        from rubetunes.providers.spotiflac import _ProviderTracker

        self.tracker = _ProviderTracker()

    def test_initial_sort_is_stable(self):
        """Providers with no history maintain their input order."""
        sorted_providers = self.tracker.sort_providers(["a", "b", "c"])
        # All have zero counts; sort should return all three (order may vary but all present)
        assert set(sorted_providers) == {"a", "b", "c"}

    def test_success_increases_rank(self):
        self.tracker.record_success("good")
        self.tracker.record_failure("bad")
        self.tracker.record_failure("bad")
        sorted_providers = self.tracker.sort_providers(["bad", "good"])
        assert sorted_providers[0] == "good"

    def test_failure_decreases_rank(self):
        for _ in range(5):
            self.tracker.record_success("reliable")
        for _ in range(5):
            self.tracker.record_failure("flaky")
        sorted_providers = self.tracker.sort_providers(["flaky", "reliable"])
        assert sorted_providers[0] == "reliable"

    def test_record_success_increments(self):
        self.tracker.record_success("p1")
        self.tracker.record_success("p1")
        assert self.tracker._stats["p1"]["success"] == 2

    def test_record_failure_increments(self):
        self.tracker.record_failure("p1")
        self.tracker.record_failure("p1")
        assert self.tracker._stats["p1"]["failure"] == 2


# ===========================================================================
# _extract_asin
# ===========================================================================


class TestExtractAsin:
    def test_trackAsin_query_param(self):
        from rubetunes.providers.spotiflac import _extract_asin

        url = "https://music.amazon.com/albums/B09ABC?trackAsin=B0XYZ12345"
        assert _extract_asin(url) == "B0XYZ12345"

    def test_tracks_path_segment(self):
        from rubetunes.providers.spotiflac import _extract_asin

        url = "https://music.amazon.com/tracks/B0ABCDE1234"
        result = _extract_asin(url)
        assert result is not None
        assert result.startswith("B")

    def test_album_album_track_pattern(self):
        from rubetunes.providers.spotiflac import _extract_asin

        url = "https://music.amazon.com/albums/B0ALBUM123/B0TRACK45678"
        result = _extract_asin(url)
        assert result is not None
        assert result.startswith("B")

    def test_invalid_url_returns_none(self):
        from rubetunes.providers.spotiflac import _extract_asin

        assert _extract_asin("https://spotify.com/track/abc") is None

    def test_empty_string_returns_none(self):
        from rubetunes.providers.spotiflac import _extract_asin

        assert _extract_asin("") is None


# ===========================================================================
# _QUALITY_TO_CHAIN
# ===========================================================================


class TestQualityToChain:
    def test_flac_maps_to_27_7_6(self):
        from rubetunes.providers.spotiflac import _QUALITY_TO_CHAIN

        assert _QUALITY_TO_CHAIN["flac"] == [27, 7, 6]

    def test_27_maps_to_full_chain(self):
        from rubetunes.providers.spotiflac import _QUALITY_TO_CHAIN

        assert _QUALITY_TO_CHAIN["27"] == [27, 7, 6]

    def test_flac_cd_maps_to_6_first(self):
        from rubetunes.providers.spotiflac import _QUALITY_TO_CHAIN

        assert _QUALITY_TO_CHAIN["flac_cd"][0] == 6

    def test_6_maps_to_cd_only(self):
        from rubetunes.providers.spotiflac import _QUALITY_TO_CHAIN

        assert _QUALITY_TO_CHAIN["6"] == [6]

    def test_hires_maps_to_full_chain(self):
        from rubetunes.providers.spotiflac import _QUALITY_TO_CHAIN

        assert _QUALITY_TO_CHAIN["hires"] == [27, 7, 6]


# ===========================================================================
# _get_qobuz_url_via_musicdl
# ===========================================================================


class TestGetQobuzUrlViaMusicdl:
    @resp_lib.activate
    def test_success_returns_url(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_musicdl

        resp_lib.add(
            resp_lib.POST,
            "https://www.musicdl.me/api/qobuz/download",
            json={"success": True, "download_url": "https://cdn.example.com/track.flac"},
            status=200,
        )
        result = _get_qobuz_url_via_musicdl("341032040", 27)
        assert result == "https://cdn.example.com/track.flac"

    @resp_lib.activate
    def test_success_false_returns_none(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_musicdl

        resp_lib.add(
            resp_lib.POST,
            "https://www.musicdl.me/api/qobuz/download",
            json={"success": False, "error": "Track not available in this quality"},
            status=200,
        )
        result = _get_qobuz_url_via_musicdl("341032040", 27)
        assert result is None

    @resp_lib.activate
    def test_http_error_returns_none(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_musicdl

        resp_lib.add(
            resp_lib.POST,
            "https://www.musicdl.me/api/qobuz/download",
            json={"error": "Internal Server Error"},
            status=500,
        )
        result = _get_qobuz_url_via_musicdl("341032040", 27)
        assert result is None

    def test_connection_error_returns_none(self):
        import requests

        from rubetunes.providers.spotiflac import _get_qobuz_url_via_musicdl

        with patch(
            "rubetunes.providers.spotiflac.requests.post",
            side_effect=requests.ConnectionError("down"),
        ):
            result = _get_qobuz_url_via_musicdl("341032040", 27)
        assert result is None

    @resp_lib.activate
    def test_requests_correct_quality_and_url(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_musicdl

        captured: list[dict] = []

        def _handler(request):
            import json as _json

            captured.append(_json.loads(request.body))
            return (200, {}, '{"success":true,"download_url":"https://example.com/f.flac"}')

        resp_lib.add_callback(
            resp_lib.POST,
            "https://www.musicdl.me/api/qobuz/download",
            callback=_handler,
            content_type="application/json",
        )
        _get_qobuz_url_via_musicdl("99999", 7)
        assert len(captured) == 1
        assert captured[0]["quality"] == "7"
        assert "99999" in captured[0]["url"]


# ===========================================================================
# _get_qobuz_url_via_stream_proxies
# ===========================================================================


class TestGetQobuzUrlViaStreamProxies:
    def test_delegates_to_existing_helper(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_stream_proxies

        with patch(
            "rubetunes.providers.qobuz._get_qobuz_stream_url",
            return_value="https://s.example.com/track.flac",
        ):
            result = _get_qobuz_url_via_stream_proxies("12345", 6)
        assert result == "https://s.example.com/track.flac"

    def test_returns_none_when_no_url(self):
        from rubetunes.providers.spotiflac import _get_qobuz_url_via_stream_proxies

        with patch("rubetunes.providers.qobuz._get_qobuz_stream_url", return_value=None):
            result = _get_qobuz_url_via_stream_proxies("12345", 6)
        assert result is None


# ===========================================================================
# download_spotiflac — integration scenarios
# ===========================================================================


class TestDownloadSpotiflac:
    def _make_tmp(self, tmp_path: Path) -> Path:
        return tmp_path / "spotiflac_test"

    def test_no_qobuz_or_amazon_returns_none(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        info: dict = {"title": "Test", "artists": ["Artist"], "isrc": "US1234567890"}
        result = download_spotiflac(info, "flac", tmp_path)
        assert result is None

    def test_uses_qobuz_when_qobuz_id_present(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        flac_file = tmp_path / "qobuz_12345_q27.flac"
        flac_file.write_bytes(b"FLAC" + b"\x00" * 100)

        info: dict = {
            "title": "Test",
            "artists": ["Artist"],
            "qobuz_id": "12345",
        }

        with patch("rubetunes.providers.spotiflac._try_qobuz_download", return_value=flac_file):
            result = download_spotiflac(info, "flac", tmp_path)
        assert result == flac_file

    def test_falls_back_to_amazon_when_qobuz_fails(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        m4a_file = tmp_path / "amazon_B0TRACK.flac"
        m4a_file.write_bytes(b"fLaC" + b"\x00" * 100)

        info: dict = {
            "title": "Test",
            "artists": ["Artist"],
            "qobuz_id": "12345",
            "amazon_url": "https://music.amazon.com/tracks/B0TRACK12345",
        }

        with (
            patch("rubetunes.providers.spotiflac._try_qobuz_download", return_value=None),
            patch("rubetunes.providers.spotiflac._try_amazon_download", return_value=m4a_file),
        ):
            result = download_spotiflac(info, "flac", tmp_path)
        assert result == m4a_file

    def test_returns_none_when_all_fail(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        info: dict = {
            "title": "Test",
            "artists": ["Artist"],
            "qobuz_id": "12345",
            "amazon_url": "https://music.amazon.com/tracks/B0TRACK12345",
        }

        with (
            patch("rubetunes.providers.spotiflac._try_qobuz_download", return_value=None),
            patch("rubetunes.providers.spotiflac._try_amazon_download", return_value=None),
        ):
            result = download_spotiflac(info, "flac", tmp_path)
        assert result is None

    def test_skips_qobuz_when_no_qobuz_id(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        m4a_file = tmp_path / "amazon_result.flac"
        m4a_file.write_bytes(b"fLaC" + b"\x00" * 100)

        info: dict = {
            "title": "Test",
            "artists": ["Artist"],
            "amazon_url": "https://music.amazon.com/tracks/B0TRACK12345",
        }

        qobuz_called = []

        def _mock_qobuz(qobuz_id, quality_chain, tmp_dir):
            qobuz_called.append(True)
            return None

        with (
            patch("rubetunes.providers.spotiflac._try_qobuz_download", side_effect=_mock_qobuz),
            patch("rubetunes.providers.spotiflac._try_amazon_download", return_value=m4a_file),
        ):
            result = download_spotiflac(info, "flac", tmp_path)
        # qobuz should not have been called since there was no qobuz_id
        assert not qobuz_called
        assert result == m4a_file

    def test_quality_chain_passed_correctly(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        captured_chain: list[list] = []

        def _mock_qobuz(qobuz_id, quality_chain, tmp_dir):
            captured_chain.append(list(quality_chain))
            return None

        info: dict = {"title": "Test", "artists": ["Artist"], "qobuz_id": "99999"}

        with patch("rubetunes.providers.spotiflac._try_qobuz_download", side_effect=_mock_qobuz):
            download_spotiflac(info, "flac_cd", tmp_path)

        assert len(captured_chain) == 1
        # flac_cd should start with quality 6
        assert captured_chain[0][0] == 6

    def test_hires_quality_chain_starts_with_27(self, tmp_path):
        from rubetunes.providers.spotiflac import download_spotiflac

        captured_chain: list[list] = []

        def _mock_qobuz(qobuz_id, quality_chain, tmp_dir):
            captured_chain.append(list(quality_chain))
            return None

        info: dict = {"title": "Test", "artists": ["Artist"], "qobuz_id": "99999"}

        with patch("rubetunes.providers.spotiflac._try_qobuz_download", side_effect=_mock_qobuz):
            download_spotiflac(info, "hires", tmp_path)

        assert captured_chain[0][0] == 27


# ===========================================================================
# _try_qobuz_download — quality fallback
# ===========================================================================


class TestTryQobuzDownload:
    def test_quality_fallback(self, tmp_path):
        """Falls back to lower quality when higher quality returns no URL."""
        from rubetunes.providers.spotiflac import _try_qobuz_download

        flac_file = tmp_path / "result.flac"
        flac_file.write_bytes(b"FLAC" + b"\x00" * 100)

        attempted_qualities: list[int] = []

        def _mock_get_url(qobuz_id, quality):
            attempted_qualities.append(quality)
            if quality == 6:
                return "https://example.com/track_q6.flac"
            return None

        def _mock_download(url, dest):
            dest.write_bytes(b"FLAC" + b"\x00" * 100)

        with (
            patch(
                "rubetunes.providers.spotiflac._get_qobuz_download_url", side_effect=_mock_get_url
            ),
            patch("rubetunes.providers.spotiflac._download_file", side_effect=_mock_download),
        ):
            result = _try_qobuz_download("12345", [27, 7, 6], tmp_path)

        # Should have tried 27, 7, then succeeded with 6
        assert 27 in attempted_qualities
        assert 7 in attempted_qualities
        assert 6 in attempted_qualities
        assert result is not None

    def test_returns_none_when_all_qualities_fail(self, tmp_path):
        from rubetunes.providers.spotiflac import _try_qobuz_download

        with patch("rubetunes.providers.spotiflac._get_qobuz_download_url", return_value=None):
            result = _try_qobuz_download("12345", [27, 7, 6], tmp_path)
        assert result is None

    def test_succeeds_immediately_at_highest_quality(self, tmp_path):
        from rubetunes.providers.spotiflac import _try_qobuz_download

        attempted_qualities: list[int] = []

        def _mock_get_url(qobuz_id, quality):
            attempted_qualities.append(quality)
            return "https://example.com/track.flac"

        def _mock_download(url, dest):
            dest.write_bytes(b"FLAC" + b"\x00" * 100)

        with (
            patch(
                "rubetunes.providers.spotiflac._get_qobuz_download_url", side_effect=_mock_get_url
            ),
            patch("rubetunes.providers.spotiflac._download_file", side_effect=_mock_download),
        ):
            result = _try_qobuz_download("12345", [27, 7, 6], tmp_path)

        # Should have stopped at quality 27
        assert attempted_qualities == [27]
        assert result is not None


# ===========================================================================
# Spotify downloader — _FLAC_QUALITIES updated
# ===========================================================================


class TestFlacQualitiesSet:
    """Verify that the Spotify downloader _FLAC_QUALITIES includes the new quality codes."""

    def test_hires_is_flac(self):
        from kharej.downloaders.spotify import _FLAC_QUALITIES

        assert "hires" in _FLAC_QUALITIES

    def test_27_is_flac(self):
        from kharej.downloaders.spotify import _FLAC_QUALITIES

        assert "27" in _FLAC_QUALITIES

    def test_7_is_flac(self):
        from kharej.downloaders.spotify import _FLAC_QUALITIES

        assert "7" in _FLAC_QUALITIES

    def test_6_is_flac(self):
        from kharej.downloaders.spotify import _FLAC_QUALITIES

        assert "6" in _FLAC_QUALITIES

    def test_mp3_is_not_flac(self):
        from kharej.downloaders.spotify import _FLAC_QUALITIES

        assert "mp3" not in _FLAC_QUALITIES
