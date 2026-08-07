"""Bringing previously processed output back under management.

Reads a folder produced by an earlier run — or by an older version of this
application — and registers it so its videos can be used in a Collection without
being reprocessed.

Import is deliberately **non-destructive and read-only** with respect to the
imported folder. Nothing is moved, rewritten, or renamed. If the import is wrong,
the remedy is to delete the database rows, and the original output is untouched.

Compatibility is reported rather than enforced. Output described under an older
prompt or schema is still usable; the user is told so and decides. Refusing it
would strand work that is perfectly good for most purposes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.db import new_id, open_database, utc_now
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.providers.base import schema_hash

logger = get_logger(__name__)

REQUIRED_MARKER = "assembled.txt"


@dataclass
class ImportCandidate:
    """One processed video found on disk."""

    directory: Path
    display_name: str
    assembled_path: Path
    frames_dir: Path | None = None
    transcript_path: Path | None = None
    visual_results_path: Path | None = None
    manifest_path: Path | None = None
    duration_seconds: float = 0.0
    frame_count: int = 0
    frame_interval_ms: int | None = None
    source_sha256: str | None = None
    schema_hash: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def compatibility(self) -> str:
        if any("pictures are missing" in w for w in self.warnings):
            return "missing_artifacts"
        if any("older wording" in w for w in self.warnings):
            return "provenance_mismatch"
        if any("no descriptions" in w for w in self.warnings):
            return "no_visual"
        return "ok"

    @property
    def compatibility_label(self) -> str:
        return {
            "ok": "Fits with the rest",
            "no_visual": "No descriptions",
            "provenance_mismatch": "Older wording",
            "missing_artifacts": "Pictures missing",
        }[self.compatibility]


@dataclass
class ImportReport:
    candidates: list[ImportCandidate] = field(default_factory=list)
    imported: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def discover(root: Path) -> list[ImportCandidate]:
    """Find every processed video under *root*.

    A directory counts as a processed video when it holds `assembled.txt` — that
    file is only written after a video finished assembly, so its presence is the
    cheapest reliable signal that there is something worth importing.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    found: list[ImportCandidate] = []
    for assembled in sorted(root.rglob(REQUIRED_MARKER)):
        # analysis_input holds copies, not originals. Importing them would
        # register the same video twice under two different paths.
        if "analysis_input" in assembled.parts:
            continue
        found.append(_inspect(assembled.parent))
    return found


def _inspect(directory: Path) -> ImportCandidate:
    candidate = ImportCandidate(
        directory=directory,
        display_name=directory.name,
        assembled_path=directory / REQUIRED_MARKER,
    )

    manifest = directory / "frames_manifest.json"
    if manifest.is_file():
        candidate.manifest_path = manifest
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate.warnings.append("the picture list could not be read.")
        else:
            candidate.display_name = payload.get("source_filename") or directory.name
            candidate.duration_seconds = float(payload.get("duration_seconds") or 0.0)
            candidate.frame_count = int(payload.get("frame_count") or 0)
            candidate.frame_interval_ms = payload.get("frame_interval_ms")
    else:
        candidate.warnings.append("there is no picture list, so pictures cannot be checked.")

    frames = directory / "frames"
    if frames.is_dir() and any(frames.glob("*.jpg")):
        candidate.frames_dir = frames
    elif candidate.frame_count:
        candidate.warnings.append(
            "the pictures are missing — the text is fine, but the picture folder "
            "has been moved or deleted."
        )

    transcript = directory / "transcript.json"
    if transcript.is_file():
        candidate.transcript_path = transcript

    visual = directory / "visual_results.json"
    if visual.is_file():
        candidate.visual_results_path = visual
        try:
            payload = json.loads(visual.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate.warnings.append("the descriptions could not be read.")
        else:
            descriptions = payload.get("descriptions") or []
            if descriptions:
                found_hash = descriptions[0].get("schema_hash")
                candidate.schema_hash = found_hash
                if found_hash and found_hash != schema_hash():
                    candidate.warnings.append(
                        "these were described with older wording, so they read a "
                        "little differently from newer ones."
                    )
    else:
        candidate.warnings.append("this has no descriptions — pictures and words only.")

    return candidate


def import_candidates(
    connection: sqlite3.Connection,
    candidates: list[ImportCandidate],
    *,
    output_root: Path,
    job_name: str = "Brought in from an earlier run",
) -> ImportReport:
    """Register discovered output. Never modifies the imported folder."""
    report = ImportReport(candidates=candidates)
    if not candidates:
        report.problems.append("No processed videos were found in that folder.")
        return report

    job_id = new_id()
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at,"
        " completed_at) VALUES (?,?,?,?,?,?,?)",
        (
            job_id,
            job_name,
            "completed",
            str(output_root),
            utc_now(),
            utc_now(),
            utc_now(),
        ),
    )

    for sequence, candidate in enumerate(candidates):
        existing = connection.execute(
            "SELECT id FROM job_videos WHERE imported_from = ? LIMIT 1",
            (str(candidate.directory),),
        ).fetchone()
        if existing is not None:
            # Already under management. Importing again would create a second
            # row pointing at the same output, and a collection built from it
            # would silently include the video twice.
            report.skipped += 1
            logger.info("%s is already imported; leaving it alone", candidate.display_name)
            continue

        try:
            relative = candidate.directory.relative_to(Path(output_root))
            output_dir = str(relative)
        except ValueError:
            # Imported from outside the output root: keep the absolute path in
            # imported_from, but leave output_dir empty so nothing assumes the
            # artifacts live under our tree.
            output_dir = ""

        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name,"
            " source_sha256, duration_seconds, sequence, version, is_active_version,"
            " status, frame_count, output_dir, imported_from, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                job_id,
                "",
                candidate.display_name,
                candidate.source_sha256,
                candidate.duration_seconds,
                sequence,
                1,
                1,
                "completed_with_gaps" if candidate.warnings else "completed",
                candidate.frame_count,
                output_dir,
                str(candidate.directory),
                utc_now(),
                utc_now(),
            ),
        )
        report.imported += 1

        for warning in candidate.warnings:
            connection.execute(
                "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
                (
                    job_id,
                    "warning",
                    "import_warning",
                    f"{candidate.display_name}: {warning}",
                    utc_now(),
                ),
            )

    if report.imported == 0:
        # Nothing was added, so the empty job would only be confusing.
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    return report


def import_processed_output(settings: Settings, path: Path) -> int:
    """CLI entry point. Returns an exit code."""
    root = settings.output_root
    if root is None:
        logger.error("No output folder has been chosen yet.")
        return 1

    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        logger.error("%s is not a folder.", path.name)
        return 1

    candidates = discover(path)
    if not candidates:
        logger.error(
            "No processed videos were found in %s. Look for a folder containing assembled.txt.",
            path.name,
        )
        return 1

    connection = open_database(root)
    try:
        report = import_candidates(connection, candidates, output_root=root)
    except sqlite3.Error as error:
        logger.error("Could not bring that work in: %s", redacted_exception_text(error))
        return 1
    finally:
        connection.close()

    print(f"Found {len(candidates)} processed video(s).")
    for candidate in candidates:
        marker = "  " if candidate.compatibility == "ok" else "! "
        print(f"{marker}{candidate.display_name} — {candidate.compatibility_label}")
        for warning in candidate.warnings:
            print(f"    {warning}")

    print()
    print(f"Brought in {report.imported}; {report.skipped} already known.")
    if report.problems:
        for problem in report.problems:
            print(f"Problem: {problem}")
        return 1
    return 0
