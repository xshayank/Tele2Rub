# -*- coding: utf-8 -*-
"""Unit tests for Track B Step 11 — Deployment artifacts.

Covers:
- iran/Dockerfile exists and contains required directives.
- iran/docker-compose.yml exists and defines required services (api, db, nginx).
- iran/.env.example exists, contains all required env vars, and has no secrets.
- iran/nginx.conf exists and contains required proxy directives.
- GET /healthz returns 200 {"status": "ok"} (Docker healthcheck endpoint).
- run_migrations() is a no-op when DATABASE_URL is empty (test-safe).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IRAN_DIR = _REPO_ROOT / "iran"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_client(settings=None):
    from starlette.testclient import TestClient

    from iran.config import IranSettings
    from iran.main import create_app

    if settings is None:
        settings = IranSettings()
    return TestClient(create_app(settings), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


class TestDockerfile:
    """iran/Dockerfile must exist and contain the required build directives."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = _IRAN_DIR / "Dockerfile"
        self.content = self.path.read_text(encoding="utf-8")

    def test_dockerfile_exists(self):
        assert self.path.exists(), "iran/Dockerfile is missing"

    def test_has_frontend_stage(self):
        assert "FROM node:" in self.content, "Dockerfile must start a node frontend stage"

    def test_has_python_stage(self):
        assert "FROM python:3.11" in self.content, "Dockerfile must have a python:3.11 stage"

    def test_copies_requirements(self):
        assert "requirements.txt" in self.content

    def test_copies_frontend_dist(self):
        assert "iran/static/" in self.content or "iran/web/dist" in self.content

    def test_exposes_port_8000(self):
        assert "EXPOSE 8000" in self.content

    def test_entrypoint_uvicorn(self):
        assert "uvicorn" in self.content
        assert "iran.main:create_app" in self.content

    def test_healthcheck_uses_healthz(self):
        assert "/healthz" in self.content


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


class TestDockerCompose:
    """iran/docker-compose.yml must exist and define required services."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = _IRAN_DIR / "docker-compose.yml"
        self.content = self.path.read_text(encoding="utf-8")

    def test_compose_file_exists(self):
        assert self.path.exists(), "iran/docker-compose.yml is missing"

    def test_has_api_service(self):
        assert "api:" in self.content

    def test_has_db_service(self):
        assert "db:" in self.content

    def test_has_nginx_service(self):
        assert "nginx:" in self.content

    def test_api_healthcheck_uses_healthz(self):
        assert "/healthz" in self.content

    def test_db_uses_postgres(self):
        assert "postgres" in self.content.lower()

    def test_db_healthcheck_present(self):
        assert "pg_isready" in self.content

    def test_api_depends_on_db(self):
        assert "depends_on" in self.content

    def test_pgdata_volume_defined(self):
        assert "pgdata" in self.content


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


class TestEnvExample:
    """iran/.env.example must list all required env vars and contain no secrets."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = _IRAN_DIR / ".env.example"
        self.content = self.path.read_text(encoding="utf-8")

    def test_env_example_exists(self):
        assert self.path.exists(), "iran/.env.example is missing"

    def test_has_secret_key_var(self):
        assert "IRAN_SECRET_KEY" in self.content

    def test_has_database_url_var(self):
        assert "IRAN_DATABASE_URL" in self.content

    def test_has_rubika_session_var(self):
        assert "IRAN_RUBIKA_SESSION_IRAN" in self.content

    def test_has_s2_endpoint_var(self):
        assert "IRAN_S2_ENDPOINT_URL" in self.content

    def test_has_s2_access_key_var(self):
        assert "IRAN_S2_ACCESS_KEY" in self.content

    def test_has_s2_bucket_var(self):
        assert "IRAN_S2_BUCKET" in self.content

    def test_has_max_jobs_var(self):
        assert "IRAN_MAX_JOBS_PER_HOUR" in self.content

    def test_has_log_level_var(self):
        assert "IRAN_LOG_LEVEL" in self.content

    def test_placeholder_values_not_real_secrets(self):
        """Ensure no real secret values are committed."""
        assert "change-me-in-production" in self.content, (
            ".env.example should use a placeholder value for secrets"
        )
        # The example value for the DB URL must be a placeholder, not a real password
        assert "password@db" in self.content

    def test_no_real_credentials(self):
        """Placeholder angle-bracket markers must be present for credentials."""
        assert "<read-access-key>" in self.content or "<base64-encoded" in self.content


# ---------------------------------------------------------------------------
# nginx.conf
# ---------------------------------------------------------------------------


class TestNginxConf:
    """iran/nginx.conf must exist and contain required proxy directives."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = _IRAN_DIR / "nginx.conf"
        self.content = self.path.read_text(encoding="utf-8")

    def test_nginx_conf_exists(self):
        assert self.path.exists(), "iran/nginx.conf is missing"

    def test_listens_on_443(self):
        assert "listen 443 ssl" in self.content

    def test_proxy_pass_to_api(self):
        assert "proxy_pass http://api:8000" in self.content

    def test_disables_proxy_buffering(self):
        assert "proxy_buffering" in self.content
        assert "off" in self.content

    def test_ssl_certificate_directive(self):
        assert "ssl_certificate" in self.content

    def test_proxy_read_timeout(self):
        # Must be long enough to sustain SSE connections
        assert "proxy_read_timeout" in self.content

    def test_http_redirect_to_https(self):
        assert "listen 80" in self.content
        assert "301" in self.content or "return 301" in self.content


# ---------------------------------------------------------------------------
# /healthz endpoint
# ---------------------------------------------------------------------------


class TestHealthzEndpoint:
    """/healthz must return 200 {"status": "ok"} — used by Docker healthcheck."""

    def test_healthz_returns_200(self):
        client = _make_test_client()
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_status_ok(self):
        client = _make_test_client()
        body = client.get("/healthz").json()
        assert body["status"] == "ok"

    def test_healthz_service_name(self):
        client = _make_test_client()
        body = client.get("/healthz").json()
        assert body["service"] == "iran"

    def test_healthz_version_present(self):
        import iran

        client = _make_test_client()
        body = client.get("/healthz").json()
        assert body["version"] == iran.__version__

    def test_healthz_contract_version_present(self):
        client = _make_test_client()
        body = client.get("/healthz").json()
        assert isinstance(body["contract_version"], int)
        assert body["contract_version"] >= 1


# ---------------------------------------------------------------------------
# run_migrations no-op
# ---------------------------------------------------------------------------


class TestRunMigrationsNoop:
    """run_migrations must be a no-op when DATABASE_URL is empty (unit-test safe)."""

    @pytest.mark.asyncio
    async def test_run_migrations_noop_on_empty_url(self, monkeypatch):
        """No-op when IRAN_DATABASE_URL is not set — must not raise."""
        monkeypatch.delenv("IRAN_DATABASE_URL", raising=False)

        from iran.config import get_settings

        # Clear the lru_cache so the patched env is picked up
        get_settings.cache_clear()
        monkeypatch.setenv("IRAN_DATABASE_URL", "")

        from iran.db.engine import run_migrations

        # Should complete without raising any exception
        await run_migrations()

        # Restore cache so other tests are not affected
        get_settings.cache_clear()
