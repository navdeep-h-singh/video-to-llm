"""Resolving a timestamp back to what was on screen at it.

The assembled document is full of times. That is most of its value — a model
handed it can say "at 00:12:34 the presenter switches to the second dataset" —
but a claim with a timestamp in it is only evidence if the timestamp can be
checked, and until now checking one meant opening the interface, finding the
job, and scrolling the frame reviewer to the right place.

`show` closes that loop from the terminal: a time in, the surrounding transcript
and the path to the exact picture out. It is the difference between a document
that *mentions* times and a document whose claims are verifiable.

Nothing here guesses. A timestamp past the end of the video says so; a job with
no frames on disk says that instead of naming a file that is not there.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.db import database_path, open_database
from app.core.logging import get_logger
from app.pipeline.frames import FRAMES_DIRNAME, MANIFEST_FILENAME
from app.services.export import ExportError, TimelineEntry, read_timeline

logger = get_logger(__name__)


class CitationError(RuntimeError):
    pass


@dataclass
class Citation:
    job_name: str
    video_name: str
    seconds: float
    frame_path: Path | None = None
    frame_seconds: float | None = None
    entries: list[TimelineEntry] = field(default_factory=list)
    duration_seconds: float | None = None


def parse_timestamp(raw: str) -> float:
    """`HH:MM:SS`, `MM:SS`, or plain seconds. Fractions allowed on the last part.

    Rejects rather than coerces. A timestamp is the one input here that has to
    be exact — accepting something ambiguous and resolving it to the wrong
    second would produce a citation that looks checked and is not.
    """
    text = (raw or "").strip()
    if not text:
        raise CitationError("Give a timestamp, like 00:12:34 or 754.")

    parts = text.split(":")
    if len(parts) > 3:
        raise CitationError(f"'{raw}' has too many parts to be a timestamp.")

    try:
        numbers = [float(p) for p in parts]
    except ValueError as error:
        raise CitationError(f"'{raw}' is not a timestamp. Try 00:12:34, 12:34, or 754.") from error

    if any(n < 0 for n in numbers):
        raise CitationError("A timestamp cannot be negative.")
    # Only the final component may carry a fraction, and only it may exceed its
    # base: "90" alone is ninety seconds, but "1:90" is not a real time.
    if len(numbers) > 1 and any(n >= 60 for n in numbers[1:]):
        raise CitationError(f"'{raw}' has a minutes or seconds part of 60 or more.")

    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def find_job(connection: sqlite3.Connection, reference: str) -> sqlite3.Row:
    """A job by identifier, then by exact name, then by unique prefix."""
    row = connection.execute(
        "SELECT id, name, output_dirname FROM jobs WHERE id = ?", (reference,)
    ).fetchone()
    if row is not None:
        return row

    rows = connection.execute(
        "SELECT id, name, output_dirname FROM jobs WHERE name = ?", (reference,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise CitationError(
            f"{len(rows)} jobs are named '{reference}'. Use the identifier instead — "
            "`video-to-llm status` lists them."
        )

    rows = connection.execute(
        "SELECT id, name, output_dirname FROM jobs WHERE id LIKE ? || '%'", (reference,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise CitationError(f"'{reference}' matches {len(rows)} jobs. Give more of the identifier.")

    raise CitationError(f"No job called '{reference}'. `video-to-llm status` lists them.")


def video_dirs(
    connection: sqlite3.Connection, output_root: Path, job_row: sqlite3.Row
) -> list[Path]:
    """The active per-video output folders for a job, in the confirmed order.

    Built from `job_videos` rather than by listing the job folder. Listing it
    returned `analysis_input/` too — the handoff package, which holds copies and
    no transcript — and because it sorts above a hex identifier, every citation
    resolved against it and failed. The database knows which folders are videos,
    which version of each is current, and what order the user confirmed; the
    filesystem knows none of those things.
    """
    folder = job_row["output_dirname"] or job_row["id"]
    job_dir = Path(output_root) / str(folder)
    if not job_dir.is_dir():
        return []

    rows = connection.execute(
        "SELECT id, version FROM job_videos WHERE job_id = ? AND is_active_version = 1"
        " ORDER BY sequence",
        (job_row["id"],),
    ).fetchall()

    found: list[Path] = []
    for row in rows:
        candidate = job_dir / f"{row['id']}_v{row['version']}"
        if candidate.is_dir():
            found.append(candidate)
    return found


def _nearest_frame(video_dir: Path, seconds: float) -> tuple[Path | None, float | None]:
    manifest_path = video_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None, None
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = payload.get("frames") or []
    if not frames:
        return None, None

    best = min(frames, key=lambda f: abs(float(f.get("timestamp_seconds") or 0.0) - seconds))
    filename = best.get("clean_filename")
    if not filename:
        return None, None
    candidate = video_dir / FRAMES_DIRNAME / str(filename)
    # The database is authoritative for state, the disk for evidence. A manifest
    # entry whose picture was deleted by the space-reclaiming screen must not be
    # reported as a file the user can open.
    if not candidate.exists():
        return None, float(best.get("timestamp_seconds") or 0.0)
    return candidate, float(best.get("timestamp_seconds") or 0.0)


def resolve_citation(
    settings: Settings, job_reference: str, timestamp: str, *, window: float = 15.0
) -> Citation:
    """What was happening at *timestamp* in *job_reference*."""
    seconds = parse_timestamp(timestamp)
    if window < 0:
        raise CitationError("The window cannot be negative.")

    root = settings.output_root
    if root is None or not database_path(Path(root)).exists():
        raise CitationError("No jobs yet. Run `video-to-llm process <video>` first.")

    connection = open_database(Path(root), migrate_on_open=False)
    try:
        job = find_job(connection, job_reference)
        folders = video_dirs(connection, Path(root), job)
        if not folders:
            raise CitationError(f"'{job['name']}' has no output on disk yet.")

        # One video per job is the common case. Where there are several, the
        # timestamp is read against the first, because a job-wide timeline would
        # need the master document's offsets and this command deliberately does
        # not invent one.
        video_dir = folders[0]

        duration: float | None = None
        manifest_path = video_dir / MANIFEST_FILENAME
        video_name = video_dir.name
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            duration = payload.get("duration_seconds")
            video_name = payload.get("source_filename") or video_name

        if duration is not None and seconds > duration:
            raise CitationError(
                f"{timestamp} is past the end of {video_name}, which runs "
                f"{int(duration // 3600):02d}:{int(duration % 3600 // 60):02d}:"
                f"{int(duration % 60):02d}."
            )

        try:
            timeline = read_timeline(video_dir)
        except ExportError as error:
            # A caller asking for a citation should not have to catch an export
            # failure to find out the video is not finished yet.
            raise CitationError(str(error)) from error
        entries = [entry for entry in timeline if abs(entry.seconds - seconds) <= window]
        frame_path, frame_seconds = _nearest_frame(video_dir, seconds)

        return Citation(
            job_name=str(job["name"]),
            video_name=str(video_name),
            seconds=seconds,
            frame_path=frame_path,
            frame_seconds=frame_seconds,
            entries=entries,
            duration_seconds=duration,
        )
    finally:
        connection.close()


def format_citation(citation: Citation) -> str:
    """The terminal rendering. One screen, the picture path last so it is easy
    to copy."""

    def clock(value: float) -> str:
        total = int(value)
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"

    lines = [
        f"{citation.video_name} — {clock(citation.seconds)}",
        f"in job '{citation.job_name}'",
        "",
    ]

    if not citation.entries:
        lines.append("  Nothing was recorded within the window at that time.")
    for entry in citation.entries:
        marker = ">" if abs(entry.seconds - citation.seconds) < 1.0 else " "
        if entry.kind == "silence":
            lines.append(f"{marker} {clock(entry.seconds)}  [quiet] {entry.text}".rstrip())
        elif entry.kind == "description":
            confidence = f" ({entry.confidence})" if entry.confidence else ""
            lines.append(
                f"{marker} {clock(entry.seconds)}  [screen]{confidence} {entry.text}".rstrip()
            )
        else:
            lines.append(f"{marker} {clock(entry.seconds)}  {entry.text}")

    lines.append("")
    if citation.frame_path is not None:
        offset = ""
        if citation.frame_seconds is not None:
            drift = citation.frame_seconds - citation.seconds
            if abs(drift) >= 0.5:
                offset = f"  (taken at {clock(citation.frame_seconds)})"
        lines.append(f"Picture: {citation.frame_path}{offset}")
    elif citation.frame_seconds is not None:
        lines.append("Picture: recorded in the manifest but no longer on disk.")
    else:
        lines.append("Picture: none — this video has no frames on disk.")

    return "\n".join(lines)
