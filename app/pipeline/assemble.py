"""Stage 5 — chronological assembly.

Everything a video produced, woven back into one document in the order it
happened: spoken words, marked silences, frame descriptions, section headings,
and emphasis markers.

Time order is the organising principle, not source order. A transcript line at
09:04 and a frame description at 09:04 belong next to each other; grouping all
the transcript first and all the descriptions after would preserve the same
facts and destroy the thing that makes them useful.

`master_assembled.txt` is written only when a job holds more than one video, in
the order the user explicitly confirmed. Never inferred from filename or date.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.artifacts import write_text
from app.core.logging import get_logger
from app.pipeline.enrich import Enrichment
from app.providers.base import UNKNOWN

logger = get_logger(__name__)

ASSEMBLED_FILENAME = "assembled.txt"
MASTER_ASSEMBLED_FILENAME = "master_assembled.txt"

#: Ordering within the same second: a heading introduces what follows, silence
#: closes what came before, speech precedes the picture it describes.
KIND_ORDER = {"heading": 0, "silence": 1, "speech": 2, "description": 3}


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass
class Entry:
    seconds: float
    kind: str
    text: str

    @property
    def sort_key(self) -> tuple[float, int]:
        return (self.seconds, KIND_ORDER.get(self.kind, 9))


def _readable(value: Any) -> bool:
    return bool(value) and str(value) != UNKNOWN


def describe_frame(description: Any, emphasis: Any = None) -> str:
    """Render one frame description as readable lines.

    Fields the model could not read are omitted rather than printed as a column
    of "Unknown". The count is stated instead, so the reader knows the frame was
    seen and mostly unreadable rather than skipped entirely.
    """
    number = description.index + 1
    lines: list[str] = []

    marker = f"  [{emphasis.label}]" if emphasis is not None else ""
    lines.append(f"  picture {number}{marker}")

    fields = (
        ("chart", getattr(description, "currency_pair", UNKNOWN)),
        ("timeframe", getattr(description, "timeframe", UNKNOWN)),
        ("on screen", getattr(description, "indicators_and_states", UNKNOWN)),
        ("happening", getattr(description, "exact_action", UNKNOWN)),
        ("text", getattr(description, "visible_text", UNKNOWN)),
        ("in words", getattr(description, "visual_description", UNKNOWN)),
        ("kind", getattr(description, "setup_type", UNKNOWN)),
    )

    unreadable = 0
    for label, value in fields:
        if _readable(value):
            lines.append(f"    {label}: {value}")
        else:
            unreadable += 1

    if unreadable:
        lines.append(f"    ({unreadable} detail(s) could not be read in this picture)")

    confidence = getattr(description, "confidence", None)
    if confidence:
        lines.append(f"    confidence: {confidence}")

    return "\n".join(lines)


def build_entries(
    transcript_segments: list[Any],
    descriptions: list[Any],
    enrichment: Enrichment | None = None,
) -> list[Entry]:
    """Interleave everything into one time-ordered sequence."""
    entries: list[Entry] = []

    for segment in transcript_segments:
        kind = "silence" if getattr(segment, "is_silence", False) else "speech"
        entries.append(Entry(seconds=segment.start_seconds, kind=kind, text=f"  {segment.text}"))

    for description in descriptions:
        emphasis = enrichment.emphasis_for(description.index) if enrichment else None
        entries.append(
            Entry(
                seconds=getattr(description, "timestamp_seconds", 0.0) or 0.0,
                kind="description",
                text=describe_frame(description, emphasis),
            )
        )

    if enrichment:
        for segment in enrichment.segments:
            entries.append(
                Entry(
                    seconds=segment.start_seconds,
                    kind="heading",
                    text=f"\n── {segment.title} "
                    f"({format_timestamp(segment.start_seconds)}"
                    f" to {format_timestamp(segment.end_seconds)}) ──",
                )
            )

    entries.sort(key=lambda e: e.sort_key)
    return entries


def assemble_video(
    *,
    display_name: str,
    duration_seconds: float,
    transcript_segments: list[Any],
    descriptions: list[Any],
    enrichment: Enrichment | None = None,
    interval_ms: int | None = None,
    gap_count: int = 0,
) -> str:
    """Produce one video's `assembled.txt` content."""
    header = [
        "=" * 72,
        display_name,
        "=" * 72,
        f"Length            {format_timestamp(duration_seconds)}",
    ]
    if interval_ms:
        header.append(f"Picture every     {interval_ms / 1000:g} seconds")
    header.append(f"Pictures          {len(descriptions):,}")
    header.append(f"Transcript lines  {len(transcript_segments):,}")
    if gap_count:
        header.append(
            f"Missing           {gap_count:,} picture(s) have no description (see gaps.txt)"
        )
    if enrichment and enrichment.segments:
        header.append(f"Sections          {len(enrichment.segments)}")
    header.append("")
    header.append("Everything below is in the order it happened. Times are from the start")
    header.append("of the video.")
    header.append("")

    body: list[str] = []
    for entry in build_entries(transcript_segments, descriptions, enrichment):
        if entry.kind == "heading":
            body.append(entry.text)
        else:
            body.append(f"{format_timestamp(entry.seconds)}{entry.text}")

    return "\n".join(header + body) + "\n"


def write_assembled(output_dir: Path, content: str) -> Path:
    path = Path(output_dir) / ASSEMBLED_FILENAME
    write_text(path, content)
    return path


@dataclass
class MasterSource:
    """One video's contribution to a multi-video job."""

    sequence: int
    display_name: str
    duration_seconds: float
    assembled_text: str
    job_video_id: str = ""
    version: int = 1


def assemble_master(job_name: str, sources: list[MasterSource]) -> str:
    """Concatenate several videos in the confirmed order, with strong boundaries.

    The order comes from the user. It is never inferred from filename, date, or
    content — two recordings from the same morning have no inherent order, and
    guessing wrong silently reverses the narrative.
    """
    ordered = sorted(sources, key=lambda s: s.sequence)
    total = sum(s.duration_seconds for s in ordered)

    lines = [
        "=" * 72,
        job_name,
        "=" * 72,
        f"Videos            {len(ordered)}",
        f"Total length      {format_timestamp(total)}",
        "",
        "The videos appear in the order you set. Each one is marked with a clear",
        "boundary so a reader knows where one ends and the next begins.",
        "",
    ]

    for source in ordered:
        lines.append("")
        lines.append(
            f'<video sequence="{source.sequence + 1}" '
            f'source_video_id="{source.job_video_id}" '
            f'processed_version="{source.version}">'
        )
        lines.append(f"  <title>{source.display_name}</title>")
        lines.append(f"  <duration>{format_timestamp(source.duration_seconds)}</duration>")
        lines.append("")
        lines.append(source.assembled_text.rstrip("\n"))
        lines.append("</video>")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_master_assembled(output_dir: Path, content: str) -> Path:
    path = Path(output_dir) / MASTER_ASSEMBLED_FILENAME
    write_text(path, content)
    return path
