"""Logging setup.

Every handler this application creates is fitted with both redaction layers
before it is attached. That is the whole point of routing setup through here
rather than calling ``logging.basicConfig`` at each entry point: a handler
configured somewhere else is a handler that can leak.

See ``docs/SECURITY.md`` and ``app/core/redaction.py``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.core.redaction import install_redaction

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(process)d]: %(message)s"
DATE_FORMAT = "%H:%M:%S"

LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3

_configured = False


def configure_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    stream: object | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure the application logger and return it.

    Idempotent: calling it again is a no-op unless ``force`` is set. Both the
    controller and the worker call this at start-up, and in ``start`` mode they
    may share a process.
    """
    global _configured

    root = logging.getLogger("video_to_llm")
    if _configured and not force:
        return root

    root.handlers.clear()
    root.setLevel(_resolve_level(level))
    # Nothing above us should re-emit these records; a stray root handler
    # without our formatter would be an unredacted second copy.
    root.propagate = False

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(install_redaction(console))

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "video-to-llm.log",
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(install_redaction(file_handler))

    # Third-party loggers are noisy at DEBUG and occasionally dump request
    # headers. Keep them at WARNING and route them through our handlers so they
    # inherit redaction rather than reaching a handler we do not control.
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn", "uvicorn.error", "faster_whisper"):
        third_party = logging.getLogger(noisy)
        third_party.handlers.clear()
        third_party.setLevel(logging.WARNING)
        third_party.propagate = False
        for handler in root.handlers:
            third_party.addHandler(handler)

    _configured = True
    return root


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def get_logger(name: str) -> logging.Logger:
    """Return a child of the application logger.

    Always use this rather than ``logging.getLogger(__name__)`` — a logger
    outside the ``video_to_llm`` tree does not inherit the redacting handlers.
    """
    suffix = name.removeprefix("app.").removeprefix("video_to_llm.")
    return logging.getLogger(f"video_to_llm.{suffix}" if suffix else "video_to_llm")


def reset_logging() -> None:
    """Tear down configuration. For tests."""
    global _configured
    logger = logging.getLogger("video_to_llm")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    _configured = False
