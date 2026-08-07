"""Stage 6 — the analysis handoff.

Per job, an `analysis_input/` folder holding everything needed to hand this work
to whatever comes next: the assembled documents, the clean frames, and a README
explaining how a picture reference in the text maps to a file on disk.

Clean frames are referenced by symlink where the platform allows it. A 1,265-
frame video is roughly 1.7 GB; copying it into every handoff would double the
disk cost of a job for no benefit. Copying is reserved for an explicit portable
export, where self-containment is the point.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.artifacts import write_json, write_text
from app.core.logging import get_logger

logger = get_logger(__name__)

ANALYSIS_INPUT_DIRNAME = "analysis_input"
FRAME_MAP_FILENAME = "README.md"
MANIFEST_FILENAME = "analysis_input_manifest.json"


@dataclass
class HandoffSource:
    display_name: str
    sequence: int
    assembled_path: Path
    frames_dir: Path | None = None
    frame_count: int = 0
    duration_seconds: float = 0.0
    gap_count: int = 0


@dataclass
class HandoffResult:
    directory: Path
    assembled_files: list[Path] = field(default_factory=list)
    frame_links: list[Path] = field(default_factory=list)
    readme_path: Path | None = None
    manifest_path: Path | None = None
    copied_frames: bool = False


def link_or_copy(source: Path, destination: Path, *, prefer_copy: bool = False) -> bool:
    """Reference *source* from *destination*. True when the bytes were copied.

    Symlinks are tried first and fall back to copying — Windows refuses them
    without Developer Mode or elevation, and some network filesystems ignore
    them. Falling back means the handoff always works; it just costs more disk
    on those platforms.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()

    if not prefer_copy:
        try:
            destination.symlink_to(source)
        except (OSError, NotImplementedError, AttributeError):
            logger.debug("Symlinks unavailable; copying %s instead", source.name)
        else:
            return False

    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)
    return True


def build_frame_map(sources: list[HandoffSource], *, portable: bool) -> str:
    """The README explaining how a reference in the text finds a file."""
    lines = [
        "# What is in this folder",
        "",
        "Everything here was produced on this computer from your own video files.",
        "The original videos were never moved or changed.",
        "",
        "## The documents",
        "",
    ]

    if len(sources) > 1:
        lines.append(
            "`master_assembled.txt` holds every video, one after another, in the order you set."
        )
        lines.append("")
        lines.append("Each video also has its own document:")
        lines.append("")

    for source in sorted(sources, key=lambda s: s.sequence):
        name = f"{source.sequence + 1:02d}_{Path(source.display_name).stem}_assembled.txt"
        detail = f"{source.frame_count:,} pictures"
        if source.gap_count:
            detail += f", {source.gap_count:,} without a description"
        lines.append(f"- `{name}` — {source.display_name} ({detail})")

    lines += [
        "",
        "## Finding a picture",
        "",
        "The documents refer to pictures by number, like `picture 47`. Pictures are",
        "numbered from 1, in the order they were taken.",
        "",
        "Picture files are named `<index>_t<time>.jpg`, where the index counts from",
        "zero and the time is the position in the video. So `picture 47` in the text",
        "is the file beginning `000046_`.",
        "",
        "```",
        "picture 1   ->  000000_t000000.jpg   (the very start)",
        "picture 47  ->  000046_t000132.jpg   (1 minute 32 seconds in)",
        "```",
        "",
    ]

    if any(s.frames_dir for s in sources):
        lines += [
            "## The pictures",
            "",
        ]
        if portable:
            lines.append(
                "The pictures are copied into this folder, so it can be moved or "
                "sent elsewhere on its own."
            )
        else:
            lines.append(
                "The pictures are referenced from where they already live rather than "
                "copied, so this folder takes almost no extra space. If you need a "
                "folder you can move or send elsewhere, ask for a portable export."
            )
        lines.append("")
        for source in sorted(sources, key=lambda s: s.sequence):
            if source.frames_dir:
                lines.append(
                    f"- `frames/{source.sequence + 1:02d}_{Path(source.display_name).stem}/`"
                )
        lines.append("")

    lines += [
        "## What is not here",
        "",
        "Your original video files, and any audio taken from them. This folder holds",
        "only what was produced: text, pictures, and records of how they were made.",
        "",
    ]

    return "\n".join(lines) + "\n"


def build_handoff(
    job_output_dir: Path,
    sources: list[HandoffSource],
    *,
    master_assembled: Path | None = None,
    portable: bool = False,
    job_name: str = "",
) -> HandoffResult:
    """Assemble the `analysis_input/` folder for one job."""
    directory = Path(job_output_dir) / ANALYSIS_INPUT_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    result = HandoffResult(directory=directory)

    ordered = sorted(sources, key=lambda s: s.sequence)

    for source in ordered:
        target = directory / (
            f"{source.sequence + 1:02d}_{Path(source.display_name).stem}_assembled.txt"
        )
        if source.assembled_path.is_file():
            shutil.copy2(source.assembled_path, target)
            result.assembled_files.append(target)

    if master_assembled is not None and master_assembled.is_file():
        target = directory / master_assembled.name
        shutil.copy2(master_assembled, target)
        result.assembled_files.append(target)

    for source in ordered:
        if source.frames_dir is None or not source.frames_dir.is_dir():
            continue
        target = (
            directory / "frames" / f"{source.sequence + 1:02d}_{Path(source.display_name).stem}"
        )
        copied = link_or_copy(source.frames_dir, target, prefer_copy=portable)
        result.copied_frames = result.copied_frames or copied
        result.frame_links.append(target)

    readme = directory / FRAME_MAP_FILENAME
    write_text(readme, build_frame_map(ordered, portable=portable))
    result.readme_path = readme

    manifest = directory / MANIFEST_FILENAME
    write_json(
        manifest,
        {
            "version": 1,
            "job_name": job_name,
            "portable": portable,
            "frames_copied": result.copied_frames,
            "video_count": len(ordered),
            "total_duration_seconds": round(sum(s.duration_seconds for s in ordered), 3),
            "total_frames": sum(s.frame_count for s in ordered),
            "total_gaps": sum(s.gap_count for s in ordered),
            "videos": [
                {
                    "sequence": s.sequence,
                    "display_name": s.display_name,
                    "frame_count": s.frame_count,
                    "gap_count": s.gap_count,
                    "duration_seconds": s.duration_seconds,
                }
                for s in ordered
            ],
        },
    )
    result.manifest_path = manifest

    logger.info(
        "Prepared the handoff folder: %d document(s), %d picture folder(s)",
        len(result.assembled_files),
        len(result.frame_links),
    )
    return result
