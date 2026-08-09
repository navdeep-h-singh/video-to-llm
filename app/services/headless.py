"""Running a job to completion without the interface.

The interface has never been the only way to own a job — the worker is — but it
was, until now, the only way to *create* one. That left the command line able to
start, watch, and import work it could not begin, and it left every agent host
with nothing to call.

Everything here is deliberately synchronous. `process_videos` returns when the
job has reached a terminal state, because the callers that need it — a terminal,
a Makefile, an MCP tool — have no way to observe progress after they return.
Progress still lands in the database as it always did, so a second window
running `status` sees the same picture the interface would.

Nothing here re-implements the pipeline. It creates a job the way the interface
creates one, turns the worker on that job and no other, then reads back what
reached disk.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.db import open_database
from app.core.logging import get_logger
from app.services.jobs import create_job

logger = get_logger(__name__)

#: Job states that mean the worker is finished with it, one way or another.
TERMINAL = frozenset(
    {"completed", "completed_with_gaps", "failed", "needs_attention", "cancelled", "paused"}
)

#: Artifact kinds that are the point of the exercise, best first. A job holding
#: one video has no master document; a job holding several has both.
DOCUMENT_KINDS = ("master_assembled", "assembled")


@dataclass
class ProcessResult:
    """What a headless run produced. `ok` is not the same as `documents`.

    A job can finish `completed_with_gaps` — some pictures went undescribed —
    and still be exactly what the user wanted, because the transcript and the
    frames are there. Callers that need the stricter reading check `status`.
    """

    job_id: str | None = None
    status: str = "not_created"
    documents: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"completed", "completed_with_gaps"} and bool(self.documents)


def default_job_name(paths: list[Path]) -> str:
    """A name for a job the caller did not name.

    The first file's stem, because that is what the person typed and will
    recognise in the list. Several files become "<first> +N" rather than a
    joined list, which would blow past the length cap on any real folder.
    """
    if not paths:
        return "Untitled job"
    first = paths[0].stem.strip() or paths[0].name
    if len(paths) > 1:
        return f"{first} +{len(paths) - 1}"
    return first


def documents_for(connection: sqlite3.Connection, output_root: Path, job_id: str) -> list[Path]:
    """The assembled documents a job produced, master first.

    Read from the artifacts table rather than guessed from the folder layout:
    the database records what was actually registered, and a path built by
    convention would quietly return a file from a previous version of a rerun.
    """
    found: list[Path] = []
    for kind in DOCUMENT_KINDS:
        rows = connection.execute(
            "SELECT relative_path FROM artifacts WHERE job_id = ? AND kind = ? ORDER BY created_at",
            (job_id, kind),
        ).fetchall()
        for row in rows:
            candidate = Path(output_root) / row["relative_path"]
            if candidate.exists():
                found.append(candidate)
    return found


def process_videos(
    settings: Settings,
    *,
    paths: list[Path],
    name: str | None = None,
    interval_ms: int | None = None,
    provider: str = "none",
    model_id: str = "",
) -> ProcessResult:
    """Create a job for *paths* and run it to completion. Returns what it made.

    The worker is scoped to the job created here. Turning a general worker once
    would have taken the oldest waiting job instead, which on a machine with
    anything queued means the command silently does something other than what
    was asked — and, where that other job names a paid service, spends money
    doing it.
    """
    from app.worker.runner import run_worker

    result = ProcessResult()
    root = settings.output_root
    if root is None:
        result.problems.append("No output folder is set. Pass --output-root or run `doctor`.")
        return result

    connection = open_database(root)
    try:
        creation = create_job(
            connection,
            settings,
            name=(name or default_job_name(paths)).strip(),
            paths=paths,
            interval_ms=interval_ms,
            provider=provider,
            model_id=model_id,
        )
        if creation.report is not None:
            result.warnings.extend(creation.report.warnings)
        if not creation.ok or creation.job_id is None:
            result.problems.extend(creation.problems)
            return result
        result.job_id = creation.job_id
    finally:
        connection.close()

    # The worker opens its own connection and takes the file lock, so ours is
    # closed above rather than held across the run.
    run_worker(settings, once=True, only_job_id=result.job_id)

    connection = open_database(root, migrate_on_open=False)
    try:
        row = connection.execute(
            "SELECT status, error_message FROM jobs WHERE id = ?", (result.job_id,)
        ).fetchone()
        if row is None:
            # The job existed a moment ago. Something outside this call removed
            # it; say so rather than reporting a success with no documents.
            result.status = "missing"
            result.problems.append("The job disappeared while it was running.")
            return result

        result.status = str(row["status"])
        if row["error_message"]:
            result.problems.append(str(row["error_message"]))
        result.documents = documents_for(connection, Path(root), result.job_id)
    finally:
        connection.close()

    return result
