"""What a finished job actually did.

The pre-run panel says what will happen. This is the other half: what did. The
two are deliberately the same shape, because the pair is the whole promise —
you were told 1,488 pictures and nothing uploaded, and afterwards you can see
1,488 pictures and nothing uploaded.

The Files screen already listed what exists and what each file is for. What it
never said is the part someone came to find out: how long the video was, what
was done to it, whether anything left the machine, and how much smaller the
document is than the alternatives.

Same rule as the plan: **measured or absent**. Durations come from the recorded
stage times, sizes from the files on disk, and the token figures are labelled
estimates because they are — a character ratio, not a real tokenisation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.collections.tokens import estimate_tokens
from app.core.config import PROVIDER_LABELS
from app.core.logging import get_logger
from app.services.plan import human_bytes, human_duration

logger = get_logger(__name__)

#: What a 768px picture costs a tile-based vision model. Used only for the
#: "if you had sent the pictures instead" comparison, which is the realistic
#: alternative — Claude and OpenAI accept no video at all.
TOKENS_PER_IMAGE = 1_393


@dataclass
class Receipt:
    """The summary above the file list. Every field may legitimately be absent."""

    videos: list[str] = field(default_factory=list)
    length_label: str | None = None

    interval_label: str | None = None
    described_label: str | None = None
    described_by: str | None = None

    finished_at: str | None = None
    took_label: str | None = None

    document_size: str | None = None
    document_tokens: int | None = None
    frames_tokens: int | None = None
    saving_label: str | None = None

    #: The counterpart to the plan's promise. Never omitted.
    left_machine: str = ""
    anything_left: bool = False

    @property
    def has_anything(self) -> bool:
        return bool(self.videos)


def _left_sentence(provider: str, sent: int) -> tuple[str, bool]:
    if provider in {"", "none"}:
        return ("Nothing left this computer. No network request was made.", False)
    if provider == "ollama_local":
        return (
            "Nothing left this computer. The pictures were described by the model "
            "you installed, here.",
            False,
        )
    label = PROVIDER_LABELS.get(provider, provider)
    return (
        f"{sent:,} still pictures were sent to {label}. The video and its audio were not.",
        True,
    )


def build_receipt(connection: sqlite3.Connection, output_root: Path | None, job_id: str) -> Receipt:
    """Read back what this job did, from what it recorded while doing it."""
    receipt = Receipt()

    job = connection.execute(
        "SELECT name, status, frame_interval_ms, visual_provider, visual_model_id,"
        " started_at, completed_at FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        return receipt

    videos = connection.execute(
        "SELECT display_name, duration_seconds FROM job_videos"
        " WHERE job_id = ? AND is_active_version = 1 ORDER BY sequence",
        (job_id,),
    ).fetchall()
    receipt.videos = [str(row["display_name"]) for row in videos]

    total_seconds = sum(float(row["duration_seconds"] or 0) for row in videos)
    if total_seconds > 0:
        hours, remainder = divmod(int(total_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        receipt.length_label = f"{hours:d}:{minutes:02d}:{seconds:02d}"

    interval = job["frame_interval_ms"]
    if interval:
        receipt.interval_label = f"a picture every {interval / 1000:g} seconds"

    provider = str(job["visual_provider"] or "none")

    described = connection.execute(
        "SELECT COALESCE(SUM(items_done), 0) AS done FROM stage_runs s"
        " JOIN job_videos v ON v.id = s.job_video_id"
        " WHERE v.job_id = ? AND s.stage = 'visual'"
        " AND s.status IN ('completed', 'completed_with_gaps')",
        (job_id,),
    ).fetchone()["done"]
    described = int(described or 0)

    if provider in {"", "none"}:
        receipt.described_label = "not described"
    else:
        receipt.described_label = f"{described:,} pictures described"
        model = str(job["visual_model_id"] or "").strip()
        service = PROVIDER_LABELS.get(provider, provider)
        receipt.described_by = f"{service} · {model}" if model else service

    receipt.left_machine, receipt.anything_left = _left_sentence(provider, described)

    receipt.finished_at = str(job["completed_at"]) if job["completed_at"] else None
    if job["started_at"] and job["completed_at"]:
        from datetime import UTC, datetime

        try:
            start = datetime.fromisoformat(str(job["started_at"]))
            end = datetime.fromisoformat(str(job["completed_at"]))
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
            receipt.took_label = human_duration((end - start).total_seconds())
        except (TypeError, ValueError):
            receipt.took_label = None

    # The document, and what it saved against the realistic alternative.
    document = connection.execute(
        "SELECT relative_path FROM artifacts WHERE job_id = ?"
        " AND kind IN ('master_assembled', 'assembled') ORDER BY kind LIMIT 1",
        (job_id,),
    ).fetchone()
    if document is not None and output_root is not None:
        path = Path(output_root) / document["relative_path"]
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            receipt.document_size = human_bytes(len(text.encode("utf-8")))
            receipt.document_tokens = estimate_tokens(text).tokens

    frames = connection.execute(
        "SELECT COALESCE(SUM(items_done), 0) AS done FROM stage_runs s"
        " JOIN job_videos v ON v.id = s.job_video_id"
        " WHERE v.job_id = ? AND s.stage = 'frames'"
        " AND s.status IN ('completed', 'completed_with_gaps')",
        (job_id,),
    ).fetchone()["done"]
    frame_count = int(frames or 0)

    if frame_count and receipt.document_tokens:
        receipt.frames_tokens = frame_count * TOKENS_PER_IMAGE
        if receipt.frames_tokens > receipt.document_tokens:
            saving = 1 - (receipt.document_tokens / receipt.frames_tokens)
            receipt.saving_label = f"{saving:.0%}"

    return receipt
