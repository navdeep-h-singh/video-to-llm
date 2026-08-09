"""Reclaiming space from a finished job, one kind of file at a time.

Deleting a job offered exactly two choices: keep everything, or remove the whole
folder. On a real job that is 185 MB of pictures, 93 MB of copies made only to
send to a service, and 91 MB of extracted audio that exists purely as an
intermediate — next to a 1 MB document that is the entire point of the run.
"Everything or nothing" makes the expensive-to-replace and the trivially
regenerable the same decision.

Each group here says what it costs to lose, because the sizes alone do not:
the largest folder is cheap to rebuild from the source video, and the smallest
one may represent hours of a local model or money already spent.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RemovableGroup:
    """One kind of file, and what removing it means."""

    key: str
    label: str
    #: What is lost, in the user's terms. Never "deletes frames/".
    consequence: str
    #: Whether this machine can produce it again without a provider.
    remakeable: bool
    paths: list[Path] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def present(self) -> bool:
        return self.total_bytes > 0


def _size_of(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


#: The groups, in the order they should be offered: safest to reclaim first.
#:
#: Ordering is a recommendation and the only one this module makes. Sorting by
#: size would put the pictures at the top, which is the one thing a review
#: screen cannot work without.
GROUP_DEFINITIONS: tuple[tuple[str, str, str, bool, tuple[str, ...]], ...] = (
    (
        "audio",
        "Extracted audio",
        "Nothing you can see. It is pulled out of the video to make the "
        "transcript and is not used again — the transcript itself is kept.",
        True,
        ("audio.wav",),
    ),
    (
        "api_frames",
        "Numbered copies of the pictures",
        "Nothing you can see. These are made only to send to a service, and "
        "are remade if you ever describe the pictures again.",
        True,
        ("frames_api",),
    ),
    (
        "frames",
        "The pictures",
        "The frame viewer and the contact sheet stop working for this video. "
        "They can be taken again from the original video, if you still have it.",
        True,
        ("frames",),
    ),
    (
        "descriptions",
        "The descriptions",
        "What a model said about each picture, and the document keeps whatever "
        "was already written into it. Producing these again costs whatever they "
        "cost the first time — hours on this computer, or money on a service.",
        False,
        ("batches", "visual_results.json", "gaps.txt"),
    ),
    (
        "transcript",
        "The transcript",
        "The spoken words and the quiet stretches. They can be made again from "
        "the original video, which takes about as long as it did the first time.",
        True,
        ("transcript.json", "transcript.txt", "silence_windows.json"),
    ),
)


def removable_groups(video_dirs: list[Path]) -> list[RemovableGroup]:
    """What can be reclaimed under *video_dirs*, with sizes and consequences.

    The assembled document is deliberately absent. It is the thing the whole run
    exists to produce, it is small next to everything else, and offering it here
    would put "delete the output" next to "delete a scratch file" as though they
    were comparable choices.
    """
    groups = []
    for key, label, consequence, remakeable, names in GROUP_DEFINITIONS:
        found: list[Path] = []
        total = 0
        for directory in video_dirs:
            for name in names:
                candidate = directory / name
                if candidate.exists():
                    found.append(candidate)
                    total += _size_of(candidate)
        groups.append(
            RemovableGroup(
                key=key,
                label=label,
                consequence=consequence,
                remakeable=remakeable,
                paths=found,
                total_bytes=total,
            )
        )
    return groups


@dataclass
class RemovalResult:
    removed: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    problems: list[str] = field(default_factory=list)


def remove_groups(video_dirs: list[Path], keys: set[str], *, output_root: Path) -> RemovalResult:
    """Delete the chosen groups. Anything outside *output_root* is refused.

    Containment is re-checked here rather than trusted from the caller. This
    function deletes directories recursively, and a path assembled from a job
    row is still a path: the check costs nothing and the failure it prevents is
    unrecoverable.
    """
    result = RemovalResult()
    root = Path(output_root).resolve()

    for group in removable_groups(video_dirs):
        if group.key not in keys or not group.present:
            continue

        for path in group.paths:
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                logger.error("Refused to remove a path outside the output folder")
                result.problems.append(
                    f"{group.label} was not removed: it is outside the output folder."
                )
                continue

            size = _size_of(resolved)
            try:
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()
            except OSError as error:
                result.problems.append(f"{group.label} could not be removed: {error.strerror}.")
                continue
            result.freed_bytes += size

        if group.label not in result.removed:
            result.removed.append(group.label)

    return result
