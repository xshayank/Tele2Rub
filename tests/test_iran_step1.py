# -*- coding: utf-8 -*-
"""Unit tests for Track B Step 1 — Iran service skeleton.

Covers:
- Health endpoint (GET /health) returns correct payload.
- IranSettings parses environment variables correctly.
- Package is importable (python -c "import iran").
- CLI --version and --check-config modes work.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_client(settings=None):
    """Return a Starlette TestClient backed by the Iran FastAPI app."""
    from starlette.testclient import TestClient

    from iran.config import IranSettings
    from iran.main import create_app

    if settings is None:
        settings = IranSettings()
    app = create_app(settings)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


class TestPackageImport:
    def test_iran_package_importable(self):
        import iran  # noqa: F401

        assert iran.__version__ is not None

    def test_iran_version_is_string(self):
        import iran

        assert isinstance(iran.__version__, str)
        assert len(iran.__version__) > 0

    def test_iran_config_importable(self):
        from iran.config import IranSettings  # noqa: F401

    def test_iran_main_importable(self):
        from iran.main import create_app  # noqa: F401


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_200(self):
        client = _make_test_client()
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_status_ok(self):
        client = _make_test_client()
        resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"

    def test_health_service_name(self):
        client = _make_test_client()
        body = client.get("/health").json()
        assert body["service"] == "iran"

    def test_health_version_present(self):
        client = _make_test_client()
        body = client.get("/health").json()
        import iran

        assert body["version"] == iran.__version__

    def test_health_contract_version_is_int(self):
        client = _make_test_client()
        body = client.get("/health").json()
        assert isinstance(body["contract_version"], int)
        assert body["contract_version"] >= 1

    def test_health_contract_version_matches_contracts(self):
        from kharej.contracts import CONTRACT_VERSION

        client = _make_test_client()
        body = client.get("/health").json()
        assert body["contract_version"] == CONTRACT_VERSION

    def test_health_content_type_json(self):
        client = _make_test_client()
        resp = client.get("/health")
        assert "application/json" in resp.headers["content-type"]

    def test_health_no_auth_required(self):
        """Health endpoint must be publicly accessible (no auth headers needed)."""
        client = _make_test_client()
        # No Authorization header — must still return 200.
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Settings (IranSettings)
# ---------------------------------------------------------------------------


class TestIranSettings:
    def test_defaults_are_sensible(self):
        from iran.config import IranSettings

        s = IranSettings()
        assert s.HOST == "0.0.0.0"
        assert s.PORT == 8000
        assert s.LOG_LEVEL == "INFO"
        assert s.LOG_FORMAT == "json"

    def test_env_override_port(self):
        from iran.config import IranSettings

        with patch.dict(os.environ, {"IRAN_PORT": "9090"}):
            s = IranSettings()
        assert s.PORT == 9090

    def test_env_override_host(self):
        from iran.config import IranSettings

        with patch.dict(os.environ, {"IRAN_HOST": "127.0.0.1"}):
            s = IranSettings()
        assert s.HOST == "127.0.0.1"

    def test_env_override_log_level(self):
        from iran.config import IranSettings

        with patch.dict(os.environ, {"IRAN_LOG_LEVEL": "DEBUG"}):
            s = IranSettings()
        assert s.LOG_LEVEL == "DEBUG"

    def test_env_override_secret_key(self):
        from iran.config import IranSettings

        with patch.dict(os.environ, {"IRAN_SECRET_KEY": "supersecret"}):
            s = IranSettings()
        assert s.SECRET_KEY == "supersecret"

    def test_unknown_env_vars_are_ignored(self):
        """extra='ignore' means unrecognised IRAN_* vars don't raise."""
        from iran.config import IranSettings

        with patch.dict(os.environ, {"IRAN_UNKNOWN_FIELD_XYZ": "value"}):
            s = IranSettings()  # must not raise
        assert s is not None

    def test_get_settings_returns_same_instance(self):
        """get_settings() is cached — two calls return the same object."""
        from iran.config import get_settings

        # The lru_cache may already hold a value from a previous test;
        # clear it to ensure consistent behaviour in isolation.
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b

    def test_settings_passed_to_app(self):
        """create_app() stores settings on app.state.settings."""
        from iran.config import IranSettings
        from iran.main import create_app

        custom = IranSettings()
        app = create_app(custom)
        assert app.state.settings is custom


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------


class TestCLI:
    def test_version_flag(self, capsys):
        from iran.__main__ import main

        code = main(["--version"])
        assert code == 0
        captured = capsys.readouterr()
        assert "iran" in captured.out.lower()

    def test_check_config_flag(self, capsys):
        from iran.__main__ import main

        code = main(["--check-config"])
        assert code == 0
        captured = capsys.readouterr()
        assert "Configuration OK" in captured.out

    def test_help_flag(self):
        from iran.__main__ import _build_parser

        parser = _build_parser()
        # argparse --help raises SystemExit(0)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# DI stubs (rubika_client, s2_client, event_bus)
# ---------------------------------------------------------------------------


class TestDIStubs:
    def test_make_rubika_client_returns_protocol(self):
        from iran.rubika_client import RubikaClientProtocol, make_rubika_client

        client = make_rubika_client()
        assert isinstance(client, RubikaClientProtocol)

    def test_make_s2_client_returns_protocol(self):
        from iran.s2_client import S2ClientProtocol, make_s2_client

        client = make_s2_client()
        assert isinstance(client, S2ClientProtocol)

    def test_make_event_bus_returns_protocol(self):
        from iran.event_bus import EventBusProtocol, make_event_bus

        bus = make_event_bus()
        assert isinstance(bus, EventBusProtocol)

    def test_app_state_has_rubika_client_after_startup(self):
        """After lifespan startup, app.state.rubika_client is populated."""
        from starlette.testclient import TestClient

        from iran.config import IranSettings
        from iran.main import create_app

        app = create_app(IranSettings())
        with TestClient(app):
            assert hasattr(app.state, "rubika_client")
            assert hasattr(app.state, "s2_client")
            assert hasattr(app.state, "event_bus")
