"""Preflight: everything checked before a single frame is written.

Runs before any expensive work, because the failures worth catching are the ones
that would otherwise surface forty minutes into a job — an unreadable file, a
disk that fills up, a duplicate already processed.

Source fingerprinting lives here too. SHA-256 is computed *before* acceptance,
so a file that has already been processed is recognised as the same content even
if it has been renamed or moved.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger
from app.pipeline.frames import expected_frame_count
from app.pipeline.probe import ProbeError, VideoInfo, is_supported_extension, probe

logger = get_logger(__name__)

FINGERPRINT_CHUNK_BYTES = 1024 * 1024

#: Rough bytes per clean frame at 1280x720, quality 3. Used only for an estimate
#: the user sees before starting, never for an allocation.
BYTES_PER_FRAME = 520_000

#: Refuse to start a job that would leave less than this much room.
DISK_HEADROOM_GB = 2.0

MAX_VIDEOS_PER_JOB = 20


class PreflightError(RuntimeError):
    pass


def fingerprint(path: Path) -> str:
    """SHA-256 of the file's contents.

    Whole-file rather than a sampled hash: a partial hash would let two different
    recordings that share a header collide, and silently reusing one video's
    output for another is the worst outcome this check exists to prevent.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(FINGERPRINT_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class VideoCheck:
    path: Path
    ok: bool
    info: VideoInfo | None = None
    sha256: str | None = None
    problem: str = ""
    warning: str = ""
    duplicate_of: str | None = None


@dataclass
class PreflightReport:
    videos: list[VideoCheck] = field(default_factory=list)
    interval_ms: int = 2000
    total_duration_seconds: float = 0.0
    total_frames: int = 0
    estimated_bytes: int = 0
    free_bytes: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def accepted(self) -> list[VideoCheck]:
        return [v for v in self.videos if v.ok]

    @property
    def estimated_gb(self) -> float:
        return self.estimated_bytes / 1024**3

    @property
    def duration_label(self) -> str:
        total = int(self.total_duration_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    def batch_count(self, batch_size: int) -> int:
        if batch_size <= 0:
            return 0
        return sum(
            (expected_frame_count(v.info.duration_seconds, self.interval_ms) + batch_size - 1)
            // batch_size
            for v in self.accepted
            if v.info
        )


def preflight(
    paths: list[Path],
    settings: Settings,
    *,
    connection: sqlite3.Connection | None = None,
    interval_ms: int | None = None,
    compute_fingerprints: bool = True,
) -> PreflightReport:
    """Check a set of source files against everything that could stop a job."""
    report = PreflightReport(interval_ms=interval_ms or settings.sampling.interval_ms())

    if not paths:
        report.problems.append("No videos were chosen.")
        return report

    if len(paths) > MAX_VIDEOS_PER_JOB:
        report.problems.append(
            f"{len(paths)} videos were chosen, which is more than the "
            f"{MAX_VIDEOS_PER_JOB} a single job takes. Split them across jobs."
        )
        return report

    root = settings.output_root
    if root is None:
        report.problems.append("No output folder has been chosen yet.")
        return report

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        report.problems.append("FFmpeg was not found on your PATH.")
        return report

    seen_in_this_job: dict[str, Path] = {}

    for path in paths:
        check = _check_one(
            Path(path),
            connection=connection,
            seen_in_this_job=seen_in_this_job,
            compute_fingerprint=compute_fingerprints,
        )
        report.videos.append(check)

        if not check.ok:
            report.problems.append(f"{Path(path).name}: {check.problem}")
        else:
            if check.warning:
                report.warnings.append(f"{Path(path).name}: {check.warning}")
            if check.info:
                report.total_duration_seconds += check.info.duration_seconds
                report.total_frames += expected_frame_count(
                    check.info.duration_seconds, report.interval_ms
                )

    report.estimated_bytes = report.total_frames * BYTES_PER_FRAME

    try:
        root.mkdir(parents=True, exist_ok=True)
        report.free_bytes = shutil.disk_usage(root).free
    except OSError as error:
        report.problems.append(f"The output folder cannot be used: {error}")
        return report

    headroom = int(DISK_HEADROOM_GB * 1024**3)
    if report.estimated_bytes + headroom > report.free_bytes:
        report.problems.append(
            f"Not enough room: this job needs about {report.estimated_gb:.1f} GB and "
            f"only {report.free_bytes / 1024**3:.1f} GB is free. "
            "Free up space, choose a longer interval, or use a different folder."
        )

    if settings.visual_analysis.enabled and settings.visual_analysis.provider == "none":
        report.problems.append("Descriptions are switched on but no provider has been chosen.")

    return report


def _check_one(
    path: Path,
    *,
    connection: sqlite3.Connection | None,
    seen_in_this_job: dict[str, Path],
    compute_fingerprint: bool,
) -> VideoCheck:
    if not path.exists():
        return VideoCheck(path, False, problem="this file could not be found.")
    if not path.is_file():
        return VideoCheck(path, False, problem="that is a folder, not a video file.")

    if not is_supported_extension(path):
        return VideoCheck(
            path,
            False,
            problem=f"{path.suffix or 'this file type'} is not supported. "
            "Use .mp4, .mov, or .webm.",
        )

    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as error:
        return VideoCheck(path, False, problem=f"this file could not be read ({error.strerror}).")

    try:
        info = probe(path)
    except ProbeError as error:
        return VideoCheck(path, False, problem=str(error).split(": ", 1)[-1])

    digest: str | None = None
    if compute_fingerprint:
        digest = fingerprint(path)

        if digest in seen_in_this_job:
            return VideoCheck(
                path,
                False,
                info=info,
                sha256=digest,
                problem=f"this is the same file as {seen_in_this_job[digest].name}, "
                "already in this job.",
            )
        seen_in_this_job[digest] = path

    check = VideoCheck(path, True, info=info, sha256=digest)

    if digest and connection is not None:
        existing = connection.execute(
            "SELECT display_name FROM job_videos WHERE source_sha256 = ? LIMIT 1", (digest,)
        ).fetchone()
        if existing is not None:
            # A warning, not a refusal: reprocessing the same video with different
            # settings is a legitimate thing to want.
            check.warning = (
                f"this has been processed before (as {existing['display_name']}). "
                "Processing it again creates a new version and keeps the old one."
            )
            check.duplicate_of = existing["display_name"]

    if not info.has_audio:
        check.warning = (
            "there is no sound in this file, so there will be no transcript — pictures only."
        )

    return check


def format_preflight(report: PreflightReport) -> str:
    lines = [
        f"Videos            {len(report.accepted)}",
        f"Total length      {report.duration_label}",
        f"Pictures to make  {report.total_frames:,}",
        f"Space needed      ~{report.estimated_gb:.1f} GB",
        f"Space free        {report.free_bytes / 1024**3:.1f} GB",
    ]
    for warning in report.warnings:
        lines.append(f"Worth knowing     {warning}")
    for problem in report.problems:
        lines.append(f"Problem           {problem}")
    lines.append("")
    lines.append("Ready to start." if report.ok else "Cannot start yet.")
    return "\n".join(lines)
