"""Stage 3 — visual descriptions.

Off by default and never blocking: a job that leaves it off produces frames, a
transcript, and an assembled document without a single network call.

Batch durability is the whole design here. A batch is marked completed only
after its artifact is durably on disk, and a completed batch is never re-sent.
That is what makes a resumed job safe: with a cloud provider, re-sending a batch
that already succeeded means paying for it twice.

The budget is checked before each send, never after. Checking after would mean
the spend that crossed the limit had already left.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.artifacts import register_artifact, relative_to_root, write_json
from app.core.db import new_id, utc_now
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.providers.base import (
    AnalysisRequest,
    AnalysisResult,
    FrameDescription,
    FrameRequest,
    SkipRecord,
)
from app.providers.costs import BudgetExceededError, BudgetTracker, estimate_cost
from app.providers.retry import RetryPolicy, call_with_retries, skips_for

logger = get_logger(__name__)

VISUAL_RESULTS_FILENAME = "visual_results.json"
GAPS_FILENAME = "gaps.txt"
BATCH_DIRNAME = "batches"

DEFAULT_PROMPT = """You are looking at numbered still pictures taken from a screen recording.
Each picture has its number stamped in the top-left corner as "IDX nn".

For every picture, return one JSON object with exactly these keys:
  index                  the picture's number, exactly as stamped
  timeframe              the chart timeframe shown, or "Unknown"
  currency_pair          the instrument or pair shown, or "Unknown"
  indicators_and_states  indicators visible and their readings, or "Unknown"
  exact_action           what is happening in this frame, or "Unknown"
  visible_text           text legible on screen, or "Unknown"
  visual_description     a plain description of what is shown
  setup_type             the kind of moment this is, or "Unknown"
  confidence             High, Medium, or Low

Reply with ONLY a JSON array. No prose, no code fences.

Use "Unknown" whenever you cannot read something reliably. Do not guess a value
that is not legible: an honest "Unknown" is far more useful than a plausible
invention. Set confidence to Low whenever you are unsure."""


@dataclass
class VisualStageResult:
    descriptions: list[FrameDescription] = field(default_factory=list)
    skips: list[SkipRecord] = field(default_factory=list)
    batches_sent: int = 0
    batches_skipped: int = 0
    total_cost_usd: float | None = None
    stopped_on_budget: bool = False
    stopped_at_index: int | None = None

    @property
    def has_gaps(self) -> bool:
        return bool(self.skips)

    @property
    def status(self) -> str:
        return "completed_with_gaps" if self.has_gaps else "completed"

    @property
    def cost_label(self) -> str:
        if self.total_cost_usd is None:
            return "No provider API charge"
        return f"${self.total_cost_usd:.4f}"


def load_frame_records(manifest_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return payload.get("frames", [])


def build_batches(
    frame_records: list[dict[str, Any]],
    api_frames_dir: Path,
    *,
    batch_size: int,
    model_id: str,
    prompt: str = DEFAULT_PROMPT,
) -> list[AnalysisRequest]:
    """Group frames into provider requests of at most *batch_size*."""
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")

    requests: list[AnalysisRequest] = []
    for start in range(0, len(frame_records), batch_size):
        chunk = frame_records[start : start + batch_size]
        frames = tuple(
            FrameRequest(
                index=int(record["index"]),
                timestamp_seconds=float(record["timestamp_seconds"]),
                image_path=Path(api_frames_dir) / record["api_filename"],
            )
            for record in chunk
        )
        requests.append(AnalysisRequest(frames=frames, model_id=model_id, prompt=prompt))
    return requests


def completed_batch_indexes(connection: sqlite3.Connection, stage_run_id: str) -> set[int]:
    """Batch indexes already durably recorded as completed.

    These are never re-sent. On a cloud provider, re-sending one means paying
    for the same work twice.
    """
    rows = connection.execute(
        "SELECT batch_index FROM batches WHERE stage_run_id = ? AND status = 'completed'",
        (stage_run_id,),
    ).fetchall()
    return {int(row["batch_index"]) for row in rows}


def run_visual_analysis(
    connection: sqlite3.Connection,
    *,
    stage_run_id: str,
    job_id: str,
    job_video_id: str,
    output_root: Path,
    output_dir: Path,
    provider: Any,
    requests: list[AnalysisRequest],
    budget: BudgetTracker | None = None,
    retry_policy: RetryPolicy | None = None,
    should_stop: Any = None,
) -> VisualStageResult:
    """Describe every batch, persisting each one before marking it complete."""
    result = VisualStageResult()
    output_dir = Path(output_dir)
    batch_dir = output_dir / BATCH_DIRNAME
    already_done = completed_batch_indexes(connection, stage_run_id)
    running_cost = 0.0
    charged = False

    for batch_index, request in enumerate(requests):
        if batch_index in already_done:
            # Resuming: this batch is already paid for and on disk.
            result.batches_skipped += 1
            logger.debug("Batch %d already completed; not sending again", batch_index)
            continue

        if should_stop is not None and should_stop():
            logger.info("Stopping before batch %d as requested.", batch_index)
            result.stopped_at_index = batch_index
            break

        if budget is not None and budget.applies:
            estimate = estimate_cost(budget.provider, len(request.frames))
            try:
                budget.check_before_send(estimate.estimated_usd or 0.0)
            except BudgetExceededError as error:
                logger.warning("Stopping at the spending limit: %s", error)
                result.stopped_on_budget = True
                result.stopped_at_index = batch_index
                _record_budget_stop(connection, job_id, job_video_id, str(error))
                break

        batch_id = _open_batch(connection, stage_run_id, batch_index, request)

        outcome = call_with_retries(request, provider.describe, policy=retry_policy)

        if not outcome.succeeded or outcome.result is None:
            reason = outcome.gave_up_reason or "the description could not be produced"
            skips = skips_for(request, reason, attempts=len(outcome.history))
            result.skips.extend(skips)
            _close_batch(
                connection,
                batch_id,
                status="skipped",
                skip_reason=reason,
                attempts=len(outcome.history),
                retry_history=[r.as_dict() for r in outcome.history],
            )
            continue

        analysis: AnalysisResult = outcome.result

        # Persist first, then mark complete. If the process dies between the
        # two, reconciliation finds a running batch and retries it — wasteful
        # but safe. The reverse order would mark work complete that is not on
        # disk, which is unrecoverable.
        artifact_path = batch_dir / f"{request.frames[0].index:06d}_batch.json"
        checksum = write_json(
            artifact_path,
            {
                "version": 1,
                "batch_index": batch_index,
                "provider": analysis.provider,
                "model_id": analysis.model_id,
                "frame_indexes": [f.index for f in request.frames],
                "descriptions": [d.as_dict() for d in analysis.descriptions],
                "retry_history": analysis.retry_history,
            },
        )
        register_artifact(
            connection,
            output_root=output_root,
            path=artifact_path,
            kind="visual_batch",
            job_id=job_id,
            job_video_id=job_video_id,
            sha256=checksum,
        )

        _close_batch(
            connection,
            batch_id,
            status="completed",
            provider=analysis.provider,
            model_id=analysis.model_id,
            cost_usd=analysis.cost_usd,
            input_tokens=analysis.input_tokens,
            output_tokens=analysis.output_tokens,
            latency_ms=analysis.latency_ms,
            artifact_path=relative_to_root(artifact_path, output_root),
            artifact_sha256=checksum,
            retry_history=analysis.retry_history,
        )

        result.descriptions.extend(analysis.descriptions)
        result.batches_sent += 1

        if analysis.cost_usd is not None:
            charged = True
            running_cost += analysis.cost_usd
            if budget is not None:
                budget.record(analysis.cost_usd)

    # None rather than 0.0 for a local run, so the interface can say
    # "No provider API charge" instead of implying the run was free.
    result.total_cost_usd = round(running_cost, 6) if charged else None
    return result


def _open_batch(
    connection: sqlite3.Connection,
    stage_run_id: str,
    batch_index: int,
    request: AnalysisRequest,
) -> str:
    batch_id = new_id()
    connection.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'running',?,?)",
        (
            batch_id,
            stage_run_id,
            batch_index,
            request.frames[0].index,
            request.frames[-1].index,
            len(request.frames),
            utc_now(),
            utc_now(),
        ),
    )
    return batch_id


def _close_batch(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    status: str,
    provider: str | None = None,
    model_id: str | None = None,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    artifact_path: str | None = None,
    artifact_sha256: str | None = None,
    skip_reason: str | None = None,
    attempts: int = 0,
    retry_history: list[dict[str, Any]] | None = None,
) -> None:
    connection.execute(
        "UPDATE batches SET status = ?, provider = ?, model_id = ?, cost_usd = ?,"
        " input_tokens = ?, output_tokens = ?, latency_ms = ?, artifact_path = ?,"
        " artifact_sha256 = ?, skip_reason = ?, attempt = ?, retry_history_json = ?,"
        " updated_at = ? WHERE id = ?",
        (
            status,
            provider,
            model_id,
            cost_usd,
            input_tokens,
            output_tokens,
            latency_ms,
            artifact_path,
            artifact_sha256,
            skip_reason,
            attempts,
            json.dumps(retry_history or []),
            utc_now(),
            batch_id,
        ),
    )


def _record_budget_stop(
    connection: sqlite3.Connection, job_id: str, job_video_id: str, message: str
) -> None:
    connection.execute(
        "INSERT INTO events (job_id, job_video_id, level, kind, message, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (job_id, job_video_id, "warning", "budget_stop", message, utc_now()),
    )


def write_visual_results(
    output_dir: Path, result: VisualStageResult, *, source_filename: str
) -> tuple[Path, Path | None]:
    """Write the merged descriptions and, when there are gaps, a gaps file."""
    output_dir = Path(output_dir)
    results_path = output_dir / VISUAL_RESULTS_FILENAME

    write_json(
        results_path,
        {
            "version": 1,
            "source_filename": source_filename,
            "description_count": len(result.descriptions),
            "skip_count": len(result.skips),
            "batches_sent": result.batches_sent,
            "batches_reused": result.batches_skipped,
            "cost": result.cost_label,
            "stopped_on_budget": result.stopped_on_budget,
            "descriptions": [d.as_dict() for d in result.descriptions],
            "skips": [
                {"index": s.index, "reason": s.reason, "attempts": s.attempts} for s in result.skips
            ],
        },
    )

    gaps_path: Path | None = None
    if result.skips:
        from app.core.artifacts import write_text

        lines = [
            f"{len(result.skips)} picture(s) have no description.",
            "",
            "Everything else in this video was processed normally. You can ask for",
            "these to be described again later without redoing any other work.",
            "",
        ]
        lines.extend(f"  picture {skip.index + 1:>6}   {skip.reason}" for skip in result.skips)
        gaps_path = output_dir / GAPS_FILENAME
        write_text(gaps_path, "\n".join(lines) + "\n")

    return results_path, gaps_path


def describe_failure(error: BaseException) -> str:
    return redacted_exception_text(error)
