# -*- coding: utf-8 -*-
"""Regression tests for Iran server startup / lifespan behaviour.

Covers:
- Lifespan startup completes (does not exit immediately).
- app.state is populated with event_bus, s2_client, rubika_client after startup.
- Startup exceptions are logged with a full traceback via ``logger.exception``
  and then re-raised so uvicorn can surface "Application startup failed".
- ``run_migrations`` is a no-op when DATABASE_URL is not set.
"""

from __future__ import annotations

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


def _make_app(settings=None):
    """Return a FastAPI app with migrations disabled (no live DB needed)."""
    from iran.config import IranSettings
    from iran.main import create_app

    if settings is None:
        settings = IranSettings(RUN_MIGRATIONS=False)
    return create_app(settings)


# ---------------------------------------------------------------------------
# Lifespan startup completes without immediate exit
# ---------------------------------------------------------------------------


class TestLifespanStartup:
    def test_startup_completes_without_exit(self):
        """lifespan must not exit immediately; app.state must be populated."""
        from starlette.testclient import TestClient

        app = _make_app()
        with TestClient(app) as client:
            # Server is up — health endpoint must respond
            resp = client.get("/health")
            assert resp.status_code == 200

            # DI stubs must be wired onto app.state
            assert hasattr(app.state, "event_bus"), "app.state.event_bus not set"
            assert hasattr(app.state, "s2_client"), "app.state.s2_client not set"
            assert hasattr(app.state, "rubika_client"), "app.state.rubika_client not set"

    def test_startup_does_not_call_sys_exit(self):
        """Startup must never call sys.exit (which would kill the server)."""
        from starlette.testclient import TestClient

        app = _make_app()
        with patch("sys.exit") as mock_exit:
            with TestClient(app):
                pass
        mock_exit.assert_not_called()

    def test_server_stays_running_after_startup(self):
        """Repeated requests after startup must succeed (server stays alive)."""
        from starlette.testclient import TestClient

        app = _make_app()
        with TestClient(app) as client:
            for _ in range(3):
                resp = client.get("/health")
                assert resp.status_code == 200


# ---------------------------------------------------------------------------
# run_migrations is a no-op when DATABASE_URL is empty
# ---------------------------------------------------------------------------


class TestRunMigrationsNoOp:
    @pytest.mark.asyncio
    async def test_run_migrations_noop_when_no_url(self):
        """run_migrations() must return silently when DATABASE_URL is unset."""
        import os

        from iran.db.engine import run_migrations

        with patch.dict(os.environ, {"IRAN_DATABASE_URL": "", "IRAN_RUN_MIGRATIONS": "1"}):
            # Should complete without raising even though there is no real DB
            await run_migrations()

    @pytest.mark.asyncio
    async def test_run_migrations_noop_when_flag_off(self):
        """run_migrations() must return silently when IRAN_RUN_MIGRATIONS=0."""
        import os

        from iran.db.engine import run_migrations

        with patch.dict(
            os.environ,
            {"IRAN_DATABASE_URL": "sqlite+aiosqlite:///:memory:", "IRAN_RUN_MIGRATIONS": "0"},
        ):
            await run_migrations()  # must not raise


# ---------------------------------------------------------------------------
# Startup exception logging
# ---------------------------------------------------------------------------


class TestStartupExceptionLogging:
    def test_startup_exception_is_logged_and_reraised(self, capsys):
        """If startup raises, it must be logged with exc_info and then re-raised."""
        from unittest.mock import MagicMock, patch

        from starlette.testclient import TestClient

        from iran.config import IranSettings
        from iran.main import create_app

        settings = IranSettings(RUN_MIGRATIONS=False)
        app = create_app(settings)

        boom = RuntimeError("injected startup failure")

        # Patch both make_event_bus (to trigger the error) and the iran.main logger
        # so we can assert logger.exception was called with the right arguments.
        mock_logger = MagicMock()

        with patch("iran.main.make_event_bus", side_effect=boom), \
             patch("iran.main.logger", mock_logger):
            with pytest.raises(Exception):
                with TestClient(app, raise_server_exceptions=True):
                    pass

        # logger.exception must have been called (not just logger.error)
        mock_logger.exception.assert_called_once()
        call_args = mock_logger.exception.call_args
        # First positional arg is the message
        assert "startup" in call_args[0][0].lower(), (
            f"Expected 'startup' in exception message, got: {call_args[0][0]!r}"
        )


class TestStartupSystemExitHandling:
    def test_startup_systemexit_converted_to_runtime_error(self):
        """If startup raises SystemExit the lifespan must convert it to RuntimeError."""
        from unittest.mock import patch

        from starlette.testclient import TestClient

        from iran.config import IranSettings
        from iran.main import create_app

        settings = IranSettings(RUN_MIGRATIONS=False)
        app = create_app(settings)

        with patch("iran.main.make_event_bus", side_effect=SystemExit(1)):
            with pytest.raises(RuntimeError, match="SystemExit"):
                with TestClient(app, raise_server_exceptions=True):
                    pass
