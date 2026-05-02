"""Smoke tests for the kharej package skeleton (Step 1)."""

from __future__ import annotations

import subprocess
import sys


def test_package_imports() -> None:
    """The package must be importable and expose the correct version."""
    import kharej

    assert kharej.__version__ == "0.1.0"


def test_worker_help_runs() -> None:
    """``python -m kharej.worker --help`` must exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0


def test_worker_healthcheck_runs() -> None:
    """``python -m kharej.worker --healthcheck`` must run without a Python traceback.

    The exit code depends on whether the required env vars are configured.
    Without config the command exits non-zero (config error) — that is the
    correct behaviour after the Step 6 rewrite replaces the stub.
    """
    result = subprocess.run(
        [sys.executable, "-m", "kharej.worker", "--healthcheck"],
        capture_output=True,
    )
    # Must not crash with an unhandled Python exception / traceback.
    assert b"Traceback (most recent call last)" not in result.stderr
