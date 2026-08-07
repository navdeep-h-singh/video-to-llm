"""Startup reconciliation.

Run before a worker takes any new work. The database records state; the
filesystem records evidence. A process that died mid-job leaves the two
disagreeing, and this is where they are brought back into line.

The governing rule: **trust the artifact over the row.** An artifact that exists
was fsynced and atomically renamed, so its presence is proof the work finished.
A row can be written by a transaction that never committed, or describe a file
that a later crash removed.

Nothing here re-runs work. Reconciliation only ever moves state *backwards* to
something safe to resume from, so validated — and especially paid — work is
never silently repeated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.artifacts import cleanup_temp_files
from app.core.db import transaction, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

#: States a process can be sitting in only because it was alive. Finding one at
#: startup means the process that owned it is gone.
INTERRUPTED_JOB_STATES = ("preparing", "transcribing", "analyzing")
INTERRUPTED_VIDEO_STATES = ("preparing", "transcribing", "analyzing")


@dataclass
class ReconciliationReport:
    temp_files_removed: list[Path] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)
    jobs_reset: list[str] = field(default_factory=list)
    videos_reset: list[str] = field(default_factory=list)
    stage_runs_reset: list[str] = field(default_factory=list)
    batches_reset: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(
            (
                self.temp_files_removed,
                self.missing_artifacts,
                self.jobs_reset,
                self.videos_reset,
                self.stage_runs_reset,
                self.batches_reset,
            )
        )

    def summary(self) -> str:
        if not self.changed:
            return "State and artifacts agree; nothing to repair."
        parts = []
        if self.temp_files_removed:
            parts.append(f"{len(self.temp_files_removed)} interrupted write(s) cleaned up")
        if self.missing_artifacts:
            parts.append(f"{len(self.missing_artifacts)} missing artifact(s) de-registered")
        if self.jobs_reset:
            parts.append(f"{len(self.jobs_reset)} job(s) returned to a resumable state")
        if self.videos_reset:
            parts.append(f"{len(self.videos_reset)} video(s) returned to a resumable state")
        if self.stage_runs_reset:
            parts.append(f"{len(self.stage_runs_reset)} stage run(s) reset")
        if self.batches_reset:
            parts.append(f"{len(self.batches_reset)} unfinished batch(es) reset")
        return "; ".join(parts) + "."


def reconcile(connection: sqlite3.Connection, output_root: Path) -> ReconciliationReport:
    """Bring state and artifacts back into agreement. Safe to run repeatedly."""
    report = ReconciliationReport()
    output_root = Path(output_root)

    # 1. Remove interrupted writes. A temp file exists only because a process
    #    died between creating it and renaming it, so it is unreferenced by
    #    construction and there is nothing in it worth keeping.
    report.temp_files_removed = cleanup_temp_files(output_root)

    with transaction(connection):
        _drop_missing_artifacts(connection, output_root, report)
        _reset_unfinished_batches(connection, report)
        _reset_interrupted_stage_runs(connection, report)
        _reset_interrupted_videos(connection, report)
        _reset_interrupted_jobs(connection, report)

    if report.changed:
        logger.info("Reconciliation: %s", report.summary())
    else:
        logger.debug("Reconciliation: nothing to repair")
    return report


def _drop_missing_artifacts(
    connection: sqlite3.Connection, output_root: Path, report: ReconciliationReport
) -> None:
    """De-register rows whose file is gone.

    The file may have been deleted by the user, sat on a drive that is no longer
    mounted, or never survived a crash. Whatever the cause, a row pointing at
    nothing will mislead every later stage into believing the work exists.
    """
    rows = connection.execute("SELECT id, relative_path FROM artifacts").fetchall()
    for row in rows:
        if not (output_root / row["relative_path"]).exists():
            report.missing_artifacts.append(row["relative_path"])
            connection.execute("DELETE FROM artifacts WHERE id = ?", (row["id"],))

    if report.missing_artifacts:
        logger.warning(
            "De-registered %d artifact(s) that are no longer on disk",
            len(report.missing_artifacts),
        )


def _reset_unfinished_batches(connection: sqlite3.Connection, report: ReconciliationReport) -> None:
    """Return running batches to pending so they can be retried.

    Deliberately scoped to 'running'. A *completed* batch is never touched: it
    was marked complete only after its artifact was durably persisted, and
    re-running it would re-send frames to a provider and bill for them twice.
    """
    rows = connection.execute("SELECT id FROM batches WHERE status = 'running'").fetchall()
    for row in rows:
        report.batches_reset.append(row["id"])
        connection.execute(
            "UPDATE batches SET status = 'pending', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )


def _reset_interrupted_stage_runs(
    connection: sqlite3.Connection, report: ReconciliationReport
) -> None:
    rows = connection.execute("SELECT id FROM stage_runs WHERE status = 'running'").fetchall()
    for row in rows:
        report.stage_runs_reset.append(row["id"])
        connection.execute(
            "UPDATE stage_runs SET status = 'pending', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )


def _reset_interrupted_videos(connection: sqlite3.Connection, report: ReconciliationReport) -> None:
    placeholders = ",".join("?" * len(INTERRUPTED_VIDEO_STATES))
    rows = connection.execute(
        f"SELECT id FROM job_videos WHERE status IN ({placeholders})",  # noqa: S608
        INTERRUPTED_VIDEO_STATES,
    ).fetchall()
    for row in rows:
        report.videos_reset.append(row["id"])
        connection.execute(
            "UPDATE job_videos SET status = 'pending', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )


def _reset_interrupted_jobs(connection: sqlite3.Connection, report: ReconciliationReport) -> None:
    """Return interrupted jobs to 'ready'.

    'ready' rather than 'needs_attention': an interrupted job is the ordinary
    consequence of closing a laptop, and the worker resumes it without the user
    having to do anything. Reserving 'needs_attention' for genuine problems is
    what keeps that status meaningful.
    """
    placeholders = ",".join("?" * len(INTERRUPTED_JOB_STATES))
    rows = connection.execute(
        f"SELECT id, name FROM jobs WHERE status IN ({placeholders})",  # noqa: S608
        INTERRUPTED_JOB_STATES,
    ).fetchall()

    for row in rows:
        report.jobs_reset.append(row["id"])
        connection.execute(
            "UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?",
            (utc_now(), row["id"]),
        )
        connection.execute(
            "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                row["id"],
                "info",
                "recovered",
                "Picked this back up after an interruption. "
                "Everything finished before the interruption was kept.",
                utc_now(),
            ),
        )
