"""Small logging helpers.

The package never configures logging on import; :func:`configure_logging` is
opt-in and used by the CLI.  Library consumers keep full control.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAME = "onenote_parser"

__all__ = ["get_logger", "configure_logging", "JsonLogFormatter"]


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of the package logger."""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


class JsonLogFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    """Attach a single stderr handler to the package logger.

    Idempotent: calling it twice will not duplicate handlers.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(logger.handlers):
        if getattr(handler, "_onenote_parser_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler._onenote_parser_handler = True  # type: ignore[attr-defined]
    if json_format:
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    logger.addHandler(handler)
    logger.propagate = False
