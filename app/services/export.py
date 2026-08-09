"""Other shapes of the same timeline.

`assembled.txt` is for a person to read and for a model to be handed. These
formats are for a program: JSONL to stream into someone else's pipeline, SRT and
VTT to load beside the video in a player, Markdown to paste into a document.

**Everything here reads the structured artifacts, never `assembled.txt`.**
Parsing a rendered document back into data is how a display and its source drift
apart — this codebase has already paid for reading the wrong row once. The
transcript JSON and the visual results JSON are what the pipeline wrote and what
the database registered; they are the source, and `assembled.txt` is one more
rendering of them, no more authoritative than these.

A format that cannot represent something says so by omission rather than by
inventing a stand-in. Subtitle formats carry speech only: a description has no
spoken duration, and writing one into a caption track would put text on screen
that nobody said.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.pipeline.transcribe import TRANSCRIPT_FILENAME
from app.pipeline.visual import VISUAL_RESULTS_FILENAME
from app.providers.base import UNKNOWN

logger = get_logger(__name__)


class ExportError(RuntimeError):
    pass


@dataclass
class TimelineEntry:
    """One thing that happened, at a time, from one of the two sources."""

    seconds: float
    kind: str  # "speech" | "silence" | "description"
    text: str
    end_seconds: float | None = None
    frame: str | None = None
    confidence: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _timestamp(seconds: float, *, separator: str = ",") -> str:
    """SRT and VTT differ only in the decimal mark, so they share this."""
    if seconds < 0:
        seconds = 0.0
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def read_timeline(video_dir: Path) -> list[TimelineEntry]:
    """Every entry for one video, in time order.

    A missing visual results file is normal and not an error: descriptions are
    off by default, and most jobs never produce one.
    """
    video_dir = Path(video_dir)
    transcript_path = video_dir / TRANSCRIPT_FILENAME
    if not transcript_path.exists():
        raise ExportError(f"No transcript in {video_dir}. Has this video finished processing?")

    entries: list[TimelineEntry] = []

    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    for segment in payload.get("segments", []):
        entries.append(
            TimelineEntry(
                seconds=float(segment.get("start_seconds") or 0.0),
                end_seconds=float(segment.get("end_seconds") or 0.0),
                kind="silence" if segment.get("is_silence") else "speech",
                text=str(segment.get("text") or "").strip(),
            )
        )

    visual_path = video_dir / VISUAL_RESULTS_FILENAME
    if visual_path.exists():
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
        for description in visual.get("descriptions", []):
            readable = {
                key: value
                for key, value in description.items()
                if isinstance(value, str) and value and value != UNKNOWN
            }
            # The plain description is the sentence; the rest are the structured
            # fields. Keeping them apart means a consumer can take one without
            # having to know which keys this schema happens to use.
            summary = str(description.get("visual_description") or "").strip()
            entries.append(
                TimelineEntry(
                    seconds=float(description.get("timestamp_seconds") or 0.0),
                    kind="description",
                    text=summary if summary and summary != UNKNOWN else "",
                    frame=description.get("clean_filename"),
                    confidence=description.get("confidence"),
                    fields={
                        k: v
                        for k, v in readable.items()
                        if k
                        not in {
                            "visual_description",
                            "confidence",
                            "clean_filename",
                            "api_filename",
                            "batch_id",
                            "provider",
                            "model_id",
                            "prompt_hash",
                        }
                    },
                )
            )

    #: Speech before the picture taken at the same second, matching the order
    #: `assemble.py` uses. Two renderings of one timeline that disagreed about
    #: order would be a bug in whichever one the user was not looking at.
    order = {"silence": 0, "speech": 1, "description": 2}
    entries.sort(key=lambda e: (e.seconds, order.get(e.kind, 9)))
    return entries


# ── Renderers ─────────────────────────────────────────────────────────────


def to_jsonl(entries: list[TimelineEntry], meta: dict[str, Any]) -> str:
    lines = []
    for entry in entries:
        record: dict[str, Any] = {
            "t": round(entry.seconds, 3),
            "timestamp": _clock(entry.seconds),
            "kind": entry.kind,
            "text": entry.text,
        }
        if entry.end_seconds is not None and entry.kind != "description":
            record["end"] = round(entry.end_seconds, 3)
        if entry.frame:
            record["frame"] = entry.frame
        if entry.confidence:
            record["confidence"] = entry.confidence
        if entry.fields:
            record["fields"] = entry.fields
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def to_json(entries: list[TimelineEntry], meta: dict[str, Any]) -> str:
    payload = {
        "version": 1,
        "source": meta.get("source_filename", ""),
        "entry_count": len(entries),
        "entries": [json.loads(line) for line in to_jsonl(entries, meta).splitlines()],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def to_markdown(entries: list[TimelineEntry], meta: dict[str, Any]) -> str:
    out = [f"# {meta.get('source_filename', 'Video')}", ""]
    for entry in entries:
        stamp = _clock(entry.seconds)
        if entry.kind == "silence":
            out.append(f"`{stamp}` *— {entry.text or 'quiet'} —*")
        elif entry.kind == "speech":
            out.append(f"`{stamp}` {entry.text}")
        else:
            body = entry.text or "(no description)"
            suffix = f" _{entry.confidence}_" if entry.confidence else ""
            out.append(f"`{stamp}` **screen:** {body}{suffix}")
        out.append("")
    return "\n".join(out)


def _subtitles(entries: list[TimelineEntry], *, vtt: bool) -> str:
    """Speech only. See the module docstring for why descriptions are omitted."""
    separator = "." if vtt else ","
    blocks: list[str] = ["WEBVTT", ""] if vtt else []
    number = 0
    for entry in entries:
        if entry.kind != "speech" or not entry.text:
            continue
        number += 1
        # A segment with no recorded end still needs one; two seconds is long
        # enough to read and short enough not to overlap the next line.
        end = entry.end_seconds if entry.end_seconds else entry.seconds + 2.0
        if end <= entry.seconds:
            end = entry.seconds + 2.0
        if not vtt:
            blocks.append(str(number))
        blocks.append(
            f"{_timestamp(entry.seconds, separator=separator)} --> "
            f"{_timestamp(end, separator=separator)}"
        )
        blocks.append(entry.text)
        blocks.append("")
    return "\n".join(blocks)


def to_srt(entries: list[TimelineEntry], meta: dict[str, Any]) -> str:
    return _subtitles(entries, vtt=False)


def to_vtt(entries: list[TimelineEntry], meta: dict[str, Any]) -> str:
    return _subtitles(entries, vtt=True)


Renderer = Callable[[list[TimelineEntry], dict[str, Any]], str]

#: Format name → (file suffix, renderer). The CLI's `--format` choices come from
#: these keys, so a format cannot be offered without something to render it.
EXPORTERS: dict[str, tuple[str, Renderer]] = {
    "jsonl": (".jsonl", to_jsonl),
    "json": (".json", to_json),
    "md": (".md", to_markdown),
    "srt": (".srt", to_srt),
    "vtt": (".vtt", to_vtt),
}


def export_video_dir(video_dir: Path, fmt: str) -> Path:
    """Write one format for one video. Returns the path written."""
    if fmt not in EXPORTERS:
        raise ExportError(f"Unknown format '{fmt}'. Known: {', '.join(sorted(EXPORTERS))}.")

    video_dir = Path(video_dir)
    suffix, render = EXPORTERS[fmt]
    entries = read_timeline(video_dir)

    meta: dict[str, Any] = {}
    transcript_path = video_dir / TRANSCRIPT_FILENAME
    if transcript_path.exists():
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        meta["source_filename"] = payload.get("source_filename", "")

    from app.core.artifacts import write_text

    destination = video_dir / f"timeline{suffix}"
    write_text(destination, render(entries, meta))
    return destination
