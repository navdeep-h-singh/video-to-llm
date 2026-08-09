"""Job-level finalisation.

Runs once every video in a job has been processed: writes the multi-video
`master_assembled.txt` where one is warranted, builds the `analysis_input`
handoff, and records the job's provenance.

`master_assembled.txt` exists only for jobs holding more than one video. A
single-video job already has `assembled.txt`; a second file with the same
content under a different name is clutter that invites the reader to wonder
which one is authoritative.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.artifacts import register_artifact, write_json
from app.core.db import utc_now
from app.core.logging import get_logger
from app.pipeline.archive import HandoffSource, build_handoff
from app.pipeline.assemble import (
    MasterSource,
    assemble_master,
    write_master_assembled,
)

logger = get_logger(__name__)

PROVENANCE_FILENAME = "provenance.json"


@dataclass
class FinalizeResult:
    master_path: Path | None = None
    handoff_dir: Path | None = None
    provenance_path: Path | None = None
    video_count: int = 0
    warnings: list[str] = field(default_factory=list)


def job_folder_name(connection: sqlite3.Connection, job_id: str) -> str:
    """The folder holding this job's output, by name where one was recorded."""
    row = connection.execute("SELECT output_dirname FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return job_id
    recorded = row["output_dirname"]
    return str(recorded) if recorded else job_id


def collect_sources(
    connection: sqlite3.Connection, job_id: str, output_root: Path
) -> list[tuple[sqlite3.Row, Path]]:
    """Every active video in the job, in confirmed order, with its output dir."""
    rows = connection.execute(
        "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1 ORDER BY sequence",
        (job_id,),
    ).fetchall()

    sources: list[tuple[sqlite3.Row, Path]] = []
    for row in rows:
        if not row["output_dir"]:
            continue
        sources.append((row, Path(output_root) / row["output_dir"]))
    return sources


def finalize_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    job_name: str,
    output_root: Path,
    portable: bool = False,
) -> FinalizeResult:
    """Write the job-level outputs. Safe to run more than once."""
    result = FinalizeResult()
    output_root = Path(output_root)
    # The folder the job's own output already lives in. Building this from the
    # identifier put the job-level package — the analysis_input folder and the
    # provenance file — in a *different* directory from the per-video output it
    # describes, splitting one job across two folders. Read from the row for the
    # same reason the worker does: NULL means an older job whose folder really is
    # named with its identifier.
    job_dir = output_root / job_folder_name(connection, job_id)
    sources = collect_sources(connection, job_id, output_root)
    result.video_count = len(sources)

    if not sources:
        logger.info("Job %s has no finished videos to gather up", job_id[:8])
        return result

    handoff_sources: list[HandoffSource] = []
    master_sources: list[MasterSource] = []

    for row, video_dir in sources:
        assembled = video_dir / "assembled.txt"
        if not assembled.is_file():
            result.warnings.append(
                f"{row['display_name']} has no assembled document and was left out."
            )
            continue

        gap_count = _gap_count(connection, row["id"])
        frames_dir = video_dir / "frames"

        handoff_sources.append(
            HandoffSource(
                display_name=row["display_name"],
                sequence=row["sequence"],
                assembled_path=assembled,
                frames_dir=frames_dir if frames_dir.is_dir() else None,
                frame_count=row["frame_count"] or 0,
                duration_seconds=row["duration_seconds"] or 0.0,
                gap_count=gap_count,
            )
        )
        master_sources.append(
            MasterSource(
                sequence=row["sequence"],
                display_name=row["display_name"],
                duration_seconds=row["duration_seconds"] or 0.0,
                assembled_text=assembled.read_text(encoding="utf-8"),
                job_video_id=row["id"],
                version=row["version"],
            )
        )

    if not handoff_sources:
        return result

    # Only for genuinely multi-video jobs. A second identical file under a
    # different name invites the reader to wonder which is authoritative.
    if len(master_sources) > 1:
        content = assemble_master(job_name, master_sources)
        result.master_path = write_master_assembled(job_dir, content)
        register_artifact(
            connection,
            output_root=output_root,
            path=result.master_path,
            kind="master_assembled",
            job_id=job_id,
        )
        logger.info("Wrote the combined document for %d videos", len(master_sources))

    handoff = build_handoff(
        job_dir,
        handoff_sources,
        master_assembled=result.master_path,
        portable=portable,
        job_name=job_name,
    )
    result.handoff_dir = handoff.directory
    register_artifact(
        connection,
        output_root=output_root,
        path=handoff.directory,
        kind="analysis_input",
        job_id=job_id,
    )

    result.provenance_path = _write_provenance(
        connection, job_id=job_id, job_name=job_name, job_dir=job_dir, sources=sources
    )
    register_artifact(
        connection,
        output_root=output_root,
        path=result.provenance_path,
        kind="provenance",
        job_id=job_id,
    )

    return result


def _gap_count(connection: sqlite3.Connection, job_video_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(SUM(b.frame_count), 0) AS gaps FROM batches b"
        " JOIN stage_runs s ON s.id = b.stage_run_id"
        " WHERE s.job_video_id = ? AND b.status = 'skipped'",
        (job_video_id,),
    ).fetchone()
    return int(row["gaps"]) if row else 0


def _write_provenance(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    job_name: str,
    job_dir: Path,
    sources: list[tuple[sqlite3.Row, Path]],
) -> Path:
    """Record what produced this job, with which settings, and when.

    Deliberately records the source *filename* and its checksum but never the
    absolute path — the layout of someone's disk is not part of the evidence and
    should not travel with an export.
    """
    job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    videos = []
    for row, _ in sources:
        stage_runs = connection.execute(
            "SELECT stage, status, attempt, backend, fell_back_from, provider,"
            " model_id, items_total, items_done, provenance_json"
            " FROM stage_runs WHERE job_video_id = ? ORDER BY stage, attempt",
            (row["id"],),
        ).fetchall()

        videos.append(
            {
                "sequence": row["sequence"],
                "display_name": row["display_name"],
                "source_sha256": row["source_sha256"],
                "duration_seconds": row["duration_seconds"],
                "frame_count": row["frame_count"],
                "version": row["version"],
                "status": row["status"],
                "stages": [
                    {
                        "stage": s["stage"],
                        "status": s["status"],
                        "attempt": s["attempt"],
                        "backend": s["backend"],
                        "fell_back_from": s["fell_back_from"],
                        "provider": s["provider"],
                        "model_id": s["model_id"],
                        "items_total": s["items_total"],
                        "items_done": s["items_done"],
                    }
                    for s in stage_runs
                ],
            }
        )

    path = job_dir / PROVENANCE_FILENAME
    write_json(
        path,
        {
            "version": 1,
            "job_id": job_id,
            "job_name": job_name,
            "created_at": job["created_at"] if job else None,
            "completed_at": utc_now(),
            "frame_interval_ms": job["frame_interval_ms"] if job else None,
            "visual_provider": job["visual_provider"] if job else "none",
            "visual_model_id": job["visual_model_id"] if job else "",
            "budget_spent_usd": job["budget_spent_usd"] if job else 0.0,
            "video_count": len(videos),
            "videos": videos,
        },
    )
    return path
