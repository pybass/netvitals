"""Logging setup shared by every client: a rotating file log, optionally mirrored to stderr."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "netvitals"
"""The package logger; every module logger below it inherits these handlers."""

_FORMATTER = logging.Formatter(fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def setup_logging(log_path: Path, *, debug: bool = False) -> None:
    """Send the package log to a rotating file at *log_path*, at DEBUG level when *debug*.

    Repeated calls are ignored: a second CLI run inside one interpreter (as in tests)
    must not double every line or leak a second open file.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return
    # Rotate at ~1 MB, keep 3: a day of `--debug` probe output is a few megabytes.
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(_FORMATTER)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False  # Nothing above us is configured, so propagating would only reach the root's lastResort
    # aiohttp logs a line per request; under --debug our own records would drown in it.
    for noisy in ("aiohttp.client", "aiohttp.internal"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_to_stderr() -> None:
    """Mirror the package log to stderr as well — for a monitor running in the foreground."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_FORMATTER)
    logging.getLogger(LOGGER_NAME).addHandler(handler)
