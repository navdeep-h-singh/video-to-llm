"""Serving files out of the output root.

Everything this application produces is a plain file on the user's own disk, and
until now none of it was reachable from the interface. These helpers make a file
viewable without ever letting a request escape the output root.

The containment rule is simple and enforced in one place: resolve the requested
path, resolve the output root, and refuse anything that is not underneath.
Resolving both sides matters — a symlinked output root (``/var`` on macOS is a
symlink to ``/private/var``) would otherwise fail a naive prefix comparison, and
a symlink *inside* the root pointing outward would otherwise pass one.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Rendered inline in the browser rather than downloaded.
INLINE_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "text/plain",
        "text/markdown",
        "application/json",
    }
)

#: Previewed as text in the interface. Anything else is offered as a download.
TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".csv", ".log", ".toml", ".yaml", ".yml"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

#: Cap on how much of a text file is read for preview. `assembled.txt` for a long
#: video runs to megabytes, and streaming that into a page helps nobody.
PREVIEW_BYTES = 256 * 1024


class OutsideOutputRoot(ValueError):
    """The requested path is not inside the output root."""


@dataclass(frozen=True)
class ResolvedFile:
    path: Path
    relative: str
    size_bytes: int
    is_dir: bool

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def is_text(self) -> bool:
        return self.suffix in TEXT_SUFFIXES

    @property
    def is_image(self) -> bool:
        return self.suffix in IMAGE_SUFFIXES

    @property
    def media_type(self) -> str:
        guessed, _ = mimetypes.guess_type(self.path.name)
        return guessed or "application/octet-stream"

    @property
    def serves_inline(self) -> bool:
        return self.media_type in INLINE_TYPES


def resolve_within(output_root: Path, relative_path: str) -> ResolvedFile:
    """Resolve *relative_path* under *output_root*, refusing anything outside.

    Raises :class:`OutsideOutputRoot` rather than returning a sentinel, so a
    caller cannot forget to check.
    """
    root = Path(output_root).resolve()
    candidate = (root / relative_path).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as error:
        logger.warning("Refused a file request outside the output folder: %r", relative_path)
        raise OutsideOutputRoot("That file is outside the output folder.") from error

    if not candidate.exists():
        raise FileNotFoundError(relative_path)

    is_dir = candidate.is_dir()
    return ResolvedFile(
        path=candidate,
        relative=candidate.relative_to(root).as_posix(),
        size_bytes=0 if is_dir else candidate.stat().st_size,
        is_dir=is_dir,
    )


def read_preview(resolved: ResolvedFile, *, limit: int = PREVIEW_BYTES) -> tuple[str, bool]:
    """Return up to *limit* bytes of text, and whether it was cut short."""
    with resolved.path.open("r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(limit + 1)
    if len(text) > limit:
        return text[:limit], True
    return text, False


def directory_size(path: Path) -> tuple[int, int]:
    """Total bytes and file count under *path*.

    Folders are the largest thing this application creates — a frames directory
    runs to gigabytes — and showing a dash where the size belongs hides the one
    number a user most needs when deciding what to keep.
    """
    total = 0
    count = 0
    for entry in Path(path).rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
            count += 1
    return total, count


def frame_listing(frames_dir: Path) -> list[dict[str, object]]:
    """Every extracted frame, in index order, with its timestamp recovered.

    Filenames carry both — ``000046_t000132.jpg`` is frame index 46 at one minute
    thirty-two — so a listing can be built from the directory alone when the
    manifest is unavailable.
    """
    frames: list[dict[str, object]] = []
    for path in sorted(Path(frames_dir).glob("*.jpg")):
        stem = path.stem
        index: int | None = None
        seconds: int | None = None

        if "_t" in stem:
            left, _, right = stem.partition("_t")
            if left.isdigit():
                index = int(left)
            if len(right) == 6 and right.isdigit():
                seconds = int(right[:2]) * 3600 + int(right[2:4]) * 60 + int(right[4:])

        frames.append(
            {
                "filename": path.name,
                "index": index if index is not None else len(frames),
                "seconds": seconds or 0,
                "label": format_timecode(seconds or 0),
            }
        )
    return frames


def format_timecode(seconds: int) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def friendly_name(relative_path: str) -> str:
    """The part of a path a person recognises.

    Stored paths are ``<job-uuid>/<video-uuid>_v1/assembled.txt`` — sixty-five
    characters of hexadecimal wrapped around the only word that identifies the
    file. Lead with the name.
    """
    return Path(relative_path).name or relative_path
