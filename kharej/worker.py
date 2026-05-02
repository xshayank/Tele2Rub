"""Kharej VPS worker — real entrypoint (Step 6).

Responsibilities:
- Boot, load config, construct all service objects.
- Connect to Rubika and wire the dispatcher as the message handler.
- Publish lifecycle events (``job.accepted`` → ``job.completed`` | ``job.failed``).
- Handle SIGINT / SIGTERM with graceful 60 s drain.

CLI modes
---------
``python -m kharej.worker``
    Run the worker forever.
``python -m kharej.worker --healthcheck``
    Probe Rubika and S2; exit 0 if healthy, non-zero otherwise.
``python -m kharej.worker --check-config``
    Validate env-var config; exit 0 and print redacted summary on success.
``python -m kharej.worker --version``
    Print ``kharej.__version__`` and exit 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any

import kharej

logger = logging.getLogger("kharej.worker")

# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    If ``record.msg`` is already a :class:`dict`, it is JSON-serialised as the
    ``msg`` field value; otherwise ``str(record.msg)`` is used.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
        }
        if isinstance(record.msg, dict):
            payload["msg"] = record.msg
        else:
            payload["msg"] = record.getMessage()
        # Merge any extra fields that were passed via the ``extra`` kwarg.
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "taskName",
        }
        for key, val in record.__dict__.items():
            if key not in skip:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(*, debug: bool = False) -> None:
    """Configure the root logger with JSON output."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kharej.worker",
        description="RubeTunes Kharej VPS worker — downloads media and pushes to Arvan S2.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="Probe Rubika and S2; exit 0 if healthy.",
    )
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="Validate required env vars; print redacted summary.",
    )
    mode.add_argument(
        "--version",
        action="store_true",
        help="Print kharej version and exit.",
    )
    parser.add_argument(
        "--healthcheck-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Timeout for --healthcheck probe (default: 5.0 s).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


# ---------------------------------------------------------------------------
# --check-config mode
# ---------------------------------------------------------------------------


def _cmd_check_config() -> int:
    """Validate config from environment; return exit code."""
    from kharej.rubika_client import RubikaConfig
    from kharej.s2_client import S2Config

    errors: list[str] = []

    rubika_cfg = None
    try:
        rubika_cfg = RubikaConfig.from_env()
    except ValueError as exc:
        errors.append(f"RubikaConfig: {exc}")

    s2_cfg = None
    try:
        s2_cfg = S2Config.from_env()
    except ValueError as exc:
        errors.append(f"S2Config: {exc}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # Print a redacted summary — never reveal secrets.
    summary: dict[str, Any] = {
        "rubika": {
            "session_name_set": bool(rubika_cfg and rubika_cfg.session_name),
            "iran_account_guid_set": bool(rubika_cfg and rubika_cfg.iran_account_guid),
        },
        "s2": {
            "endpoint_url": (s2_cfg.endpoint_url if s2_cfg else None),
            "bucket": (s2_cfg.bucket if s2_cfg else None),
            "region": (s2_cfg.region if s2_cfg else None),
            "access_key_set": bool(s2_cfg and s2_cfg.access_key),
            "secret_key_set": bool(s2_cfg and s2_cfg.secret_key),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


# ---------------------------------------------------------------------------
# --healthcheck mode
# ---------------------------------------------------------------------------


_RUBIKA_CONNECT_GRACE_PERIOD_SEC = 0.1  # brief settle time after start()


async def _probe_rubika(rubika: Any, *, timeout: float) -> bool:
    """Return ``True`` if Rubika connects within *timeout* seconds."""
    try:
        await asyncio.wait_for(rubika.start(), timeout=timeout)
        await asyncio.sleep(_RUBIKA_CONNECT_GRACE_PERIOD_SEC)
        connected = rubika.connected
        await rubika.stop()
        return connected
    except Exception:
        return False


async def _probe_s2(s2: Any) -> bool:
    """Return ``True`` if S2 is reachable (AccessDenied → unhealthy)."""
    from kharej.s2_client import S2AccessDenied

    try:
        await asyncio.to_thread(s2.head_object, "healthcheck/.keep")
        return True
    except S2AccessDenied:
        return False
    except Exception:
        # Other errors (404 already handled inside head_object → None) mean
        # something is wrong but likely not credentials; treat 4xx ≠ 403 as
        # "healthy enough" per spec.
        return True


async def _cmd_healthcheck(*, timeout: float) -> int:
    """Run health probes; return 0 if healthy, 1 if not."""
    from kharej.rubika_client import RubikaClient, RubikaConfig
    from kharej.s2_client import S2Client, S2Config

    try:
        rubika_cfg = RubikaConfig.from_env()
        s2_cfg = S2Config.from_env()
    except ValueError as exc:
        print(f"UNHEALTHY (config): {exc}", file=sys.stderr)
        return 1

    rubika = RubikaClient(rubika_cfg)
    s2 = S2Client(s2_cfg)

    try:
        rubika_ok, s2_ok = await asyncio.wait_for(
            asyncio.gather(
                _probe_rubika(rubika, timeout=timeout),
                _probe_s2(s2),
                return_exceptions=False,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        print("UNHEALTHY (timeout)", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"UNHEALTHY: {exc}", file=sys.stderr)
        return 1

    if rubika_ok and s2_ok:
        print("HEALTHY")
        return 0
    else:
        issues = []
        if not rubika_ok:
            issues.append("rubika=unreachable")
        if not s2_ok:
            issues.append("s2=access_denied")
        print(f"UNHEALTHY ({', '.join(issues)})", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------


async def run() -> int:
    """Async entrypoint — connects to Rubika and runs until SIGINT/SIGTERM."""
    from kharej.access_control import AccessControl
    from kharej.dispatcher import Dispatcher
    from kharej.progress_reporter import ProgressReporter
    from kharej.rubika_client import RubikaClient, RubikaConfig
    from kharej.s2_client import S2Client, S2Config
    from kharej.settings import KharejSettings

    settings = KharejSettings()
    access = AccessControl()
    s2 = S2Client(S2Config.from_env())
    rubika = RubikaClient(RubikaConfig.from_env())
    progress = ProgressReporter(
        rubika.send,
        throttle_sec=float(settings.get_int("progress_throttle_seconds", 3)),
    )
    dispatcher = Dispatcher(
        s2=s2,
        rubika=rubika,
        access=access,
        settings=settings,
        progress=progress,
    )
    rubika.on_message(dispatcher.handle_message)

    await rubika.start()
    logger.info({"event": "worker.started"})

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info({"event": "worker.stopping", "in_flight": dispatcher.in_flight})

    await dispatcher.shutdown(drain_timeout=60.0)
    await rubika.stop()
    logger.info({"event": "worker.stopped"})
    return 0


# ---------------------------------------------------------------------------
# main() — sync entry-point used by CLI and tests
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the appropriate mode.

    Parameters
    ----------
    argv:
        Argument list (defaults to ``sys.argv[1:]`` when ``None``).

    Returns
    -------
    int
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    _setup_logging(debug=args.debug)

    if args.version:
        print(kharej.__version__)
        return 0

    if args.check_config:
        return _cmd_check_config()

    if args.healthcheck:
        try:
            return asyncio.run(_cmd_healthcheck(timeout=args.healthcheck_timeout))
        except Exception as exc:
            if args.debug:
                raise
            print(f"UNHEALTHY: {exc}", file=sys.stderr)
            return 1

    # Default: run the worker.
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        if args.debug:
            raise
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
