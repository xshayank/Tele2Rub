"""Tests for MUSICDL_MAX_RETRIES / MUSICDL_CONNECT_TIMEOUT / MUSICDL_READ_TIMEOUT.

Uses ``importlib.reload`` to re-evaluate the module-level constants after each
``monkeypatch.setenv`` call.
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _reload_config():
    """Reload the config module so env-var changes take effect."""
    import rubetunes.providers.musicdl.config as cfg_module

    importlib.reload(cfg_module)
    return cfg_module


# ---------------------------------------------------------------------------
# Defaults (no env vars set)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_max_retries_default(self, monkeypatch):
        monkeypatch.delenv("MUSICDL_MAX_RETRIES", raising=False)
        cfg = _reload_config()
        assert cfg.MUSICDL_MAX_RETRIES == 1

    def test_connect_timeout_default(self, monkeypatch):
        monkeypatch.delenv("MUSICDL_CONNECT_TIMEOUT", raising=False)
        cfg = _reload_config()
        assert cfg.MUSICDL_CONNECT_TIMEOUT == 5.0

    def test_read_timeout_default(self, monkeypatch):
        monkeypatch.delenv("MUSICDL_READ_TIMEOUT", raising=False)
        cfg = _reload_config()
        assert cfg.MUSICDL_READ_TIMEOUT == 15.0


# ---------------------------------------------------------------------------
# Custom valid values
# ---------------------------------------------------------------------------


class TestCustomValues:
    def test_max_retries_custom(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_MAX_RETRIES", "2")
        cfg = _reload_config()
        assert cfg.MUSICDL_MAX_RETRIES == 2

    def test_connect_timeout_custom_int(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "10")
        cfg = _reload_config()
        assert cfg.MUSICDL_CONNECT_TIMEOUT == 10.0

    def test_read_timeout_custom_float(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", "30.5")
        cfg = _reload_config()
        assert cfg.MUSICDL_READ_TIMEOUT == 30.5

    def test_connect_timeout_fractional(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "3.5")
        cfg = _reload_config()
        assert cfg.MUSICDL_CONNECT_TIMEOUT == 3.5


# ---------------------------------------------------------------------------
# Invalid values → fall back to default + emit WARNING
# ---------------------------------------------------------------------------


class TestInvalidValues:
    @pytest.mark.parametrize("bad_value", ["0", "-1", "-10"])
    def test_max_retries_zero_or_negative(self, monkeypatch, caplog, bad_value):
        monkeypatch.setenv("MUSICDL_MAX_RETRIES", bad_value)
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_MAX_RETRIES == 1
        assert any("MUSICDL_MAX_RETRIES" in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad_value", ["0", "-5"])
    def test_connect_timeout_zero_or_negative(self, monkeypatch, caplog, bad_value):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", bad_value)
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_CONNECT_TIMEOUT == 5.0
        assert any("MUSICDL_CONNECT_TIMEOUT" in r.message for r in caplog.records)

    @pytest.mark.parametrize("bad_value", ["0", "-1"])
    def test_read_timeout_zero_or_negative(self, monkeypatch, caplog, bad_value):
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", bad_value)
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_READ_TIMEOUT == 15.0
        assert any("MUSICDL_READ_TIMEOUT" in r.message for r in caplog.records)

    def test_max_retries_non_numeric(self, monkeypatch, caplog):
        monkeypatch.setenv("MUSICDL_MAX_RETRIES", "abc")
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_MAX_RETRIES == 1
        assert any("MUSICDL_MAX_RETRIES" in r.message for r in caplog.records)

    def test_connect_timeout_non_numeric(self, monkeypatch, caplog):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "abc")
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_CONNECT_TIMEOUT == 5.0
        assert any("MUSICDL_CONNECT_TIMEOUT" in r.message for r in caplog.records)

    def test_read_timeout_non_numeric(self, monkeypatch, caplog):
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", "abc")
        with caplog.at_level(logging.WARNING, logger="rubetunes.providers.musicdl.config"):
            cfg = _reload_config()
        assert cfg.MUSICDL_READ_TIMEOUT == 15.0
        assert any("MUSICDL_READ_TIMEOUT" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# build_init_cfg includes max_retries
# ---------------------------------------------------------------------------


class TestBuildInitCfg:
    def test_includes_max_retries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSICDL_MAX_RETRIES", "3")
        cfg_module = _reload_config()
        monkeypatch.setattr(cfg_module, "MUSICDL_DOWNLOAD_DIR", tmp_path)
        result = cfg_module.build_init_cfg("FooClient")
        assert result["max_retries"] == 3

    def test_default_max_retries_in_init_cfg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MUSICDL_MAX_RETRIES", raising=False)
        cfg_module = _reload_config()
        monkeypatch.setattr(cfg_module, "MUSICDL_DOWNLOAD_DIR", tmp_path)
        result = cfg_module.build_init_cfg("FooClient")
        assert result["max_retries"] == 1


# ---------------------------------------------------------------------------
# build_requests_overrides includes timeout
# ---------------------------------------------------------------------------


class TestBuildRequestsOverrides:
    def test_timeout_present_without_proxy(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "5")
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", "15")
        monkeypatch.delenv("MUSICDL_PROXY", raising=False)
        cfg_module = _reload_config()
        monkeypatch.setattr(cfg_module, "MUSICDL_PROXY", None)
        overrides = cfg_module.build_requests_overrides()
        assert "timeout" in overrides
        assert overrides["timeout"] == (5.0, 15.0)
        assert "proxies" not in overrides

    def test_timeout_and_proxies_with_proxy(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "7")
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", "20")
        monkeypatch.setenv("MUSICDL_PROXY", "http://proxy:8080")
        cfg_module = _reload_config()
        overrides = cfg_module.build_requests_overrides()
        assert "timeout" in overrides
        assert overrides["timeout"] == (7.0, 20.0)
        assert "proxies" in overrides
        assert overrides["proxies"]["http"] == "http://proxy:8080"
        assert overrides["proxies"]["https"] == "http://proxy:8080"

    def test_timeout_uses_configured_values(self, monkeypatch):
        monkeypatch.setenv("MUSICDL_CONNECT_TIMEOUT", "2")
        monkeypatch.setenv("MUSICDL_READ_TIMEOUT", "60")
        monkeypatch.delenv("MUSICDL_PROXY", raising=False)
        cfg_module = _reload_config()
        monkeypatch.setattr(cfg_module, "MUSICDL_PROXY", None)
        overrides = cfg_module.build_requests_overrides()
        assert overrides["timeout"] == (2.0, 60.0)


# ---------------------------------------------------------------------------
# MUSICDL_USE_PROXY default and opt-in
# ---------------------------------------------------------------------------


class TestUseProxy:
    @pytest.mark.parametrize("env_val", [None, "", "0", "false", "no", "off"])
    def test_use_proxy_default_and_falsy_values(self, monkeypatch, env_val):
        if env_val is None:
            monkeypatch.delenv("MUSICDL_USE_PROXY", raising=False)
        else:
            monkeypatch.setenv("MUSICDL_USE_PROXY", env_val)
        cfg = _reload_config()
        assert cfg.MUSICDL_USE_PROXY is False

    @pytest.mark.parametrize("env_val", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_use_proxy_truthy_values(self, monkeypatch, env_val):
        monkeypatch.setenv("MUSICDL_USE_PROXY", env_val)
        cfg = _reload_config()
        assert cfg.MUSICDL_USE_PROXY is True
