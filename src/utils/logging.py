"""Structured logging setup for novel-crawler."""

from __future__ import annotations

import logging
import sys
from typing import Literal

_LOG_FORMAT = "%(message)s"
_DEBUG_FORMAT = "%(levelname)s: %(message)s"


def setup_logging(
    level: Literal["debug", "info", "warning", "error"] = "info",
) -> None:
    """Configure root logger for the CLI.

    Args:
        level: Minimum log level to emit.
    """
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    log_level = level_map.get(level, logging.INFO)

    fmt = _DEBUG_FORMAT if log_level == logging.DEBUG else _LOG_FORMAT
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))

    logger = logging.getLogger("novel_crawler")
    logger.setLevel(log_level)
    logger.handlers = []
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str = "novel_crawler") -> logging.Logger:
    """Return the crawler logger."""
    return logging.getLogger(name)
