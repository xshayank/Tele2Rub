"""Logging configuration for the Iran VPS service.

Supports two output formats controlled by ``IranSettings.LOG_FORMAT``:

- ``"json"`` — one JSON object per line (production default; structured for
  log aggregators such as Grafana Loki or Arvan Log Service).
- ``"text"`` — human-readable coloured output for local development.

Usage::

    from iran.logging_setup import configure_logging
    from iran.config import get_settings

    configure_logging(get_settings())
"""

from __future__ import annotations

import logging
import sys
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge any extra fields passed via the ``extra`` kwarg.
        _skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        for key, val in record.__dict__.items():
            if key not in _skip:
                payload[key] = val
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure the root logger.

    Parameters
    ----------
    log_level:
        One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.
    log_format:
        ``"json"`` for structured JSON output, ``"text"`` for plain text.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.setLevel(level)
    # Replace any existing handlers (idempotent on re-import).
    root.handlers = [handler]

    # Suppress noisy third-party loggers at WARNING level.
    for noisy in ("uvicorn.access", "httpx", "boto3", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
