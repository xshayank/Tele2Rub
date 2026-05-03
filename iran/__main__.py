"""Package entrypoint — ``python -m iran`` starts the HTTP server.

CLI modes
---------
``python -m iran``
    Start the Uvicorn ASGI server on the configured host/port.
``python -m iran --help``
    Print usage information and exit.
``python -m iran --check-config``
    Validate environment-variable config; print a redacted summary and exit 0.
``python -m iran --version``
    Print the service version and exit 0.
"""

from __future__ import annotations

import argparse
import sys
import traceback


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m iran",
        description="RubeTunes Iran VPS service (Track B).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the service version and exit.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and print a redacted summary.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override the listen host (default: IRAN_HOST env var or 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the listen port (default: IRAN_PORT env var or 8000).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns an exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.version:
        import iran

        print(f"iran {iran.__version__}")
        return 0

    if args.check_config:
        from iran.config import IranSettings

        try:
            settings = IranSettings()
        except Exception as exc:  # noqa: BLE001
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 1

        print("Configuration OK")
        print(f"  HOST            = {settings.HOST}")
        print(f"  PORT            = {settings.PORT}")
        print(f"  LOG_LEVEL       = {settings.LOG_LEVEL}")
        print(f"  LOG_FORMAT      = {settings.LOG_FORMAT}")
        print(f"  DATABASE_URL    = {'<set>' if settings.DATABASE_URL else '<not set>'}")
        print(f"  SECRET_KEY      = {'<set>' if settings.SECRET_KEY else '<not set>'}")
        print(
            f"  RUBIKA_SESSION  = {'<set>' if settings.RUBIKA_SESSION_IRAN else '<not set>'}"
        )
        print(
            f"  S2_ENDPOINT_URL = {'<set>' if settings.S2_ENDPOINT_URL else '<not set>'}"
        )
        return 0

    # Default: start the server.
    import uvicorn

    from iran.config import IranSettings
    from iran.logging_setup import configure_logging
    from iran.main import create_app

    settings = IranSettings()
    if args.host:
        settings = settings.model_copy(update={"HOST": args.host})
    if args.port:
        settings = settings.model_copy(update={"PORT": args.port})

    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    try:
        uvicorn.run(
            create_app(settings),
            host=settings.HOST,
            port=settings.PORT,
            log_config=None,  # use our own logging configuration
        )
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001
        print(f"[FATAL] uvicorn crashed: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
