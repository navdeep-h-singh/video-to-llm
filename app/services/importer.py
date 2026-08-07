"""Importing previously processed output.

Implemented in Phase 5, alongside versioned reruns — the two share the version
resolution logic and building one without the other would mean writing it twice.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def import_processed_output(settings: Settings, path: Path) -> int:
    logger.error(
        "Importing earlier work is not available in this build yet. "
        "Nothing was read from %s and nothing was changed.",
        path,
    )
    return 1
