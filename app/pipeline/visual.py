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

import dataclasses
import json
import sqlite3
from collections.abc import Callable
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
    """Batch indexes already durably completed for this video's stage.

    Across **every attempt**, not just this one. Scoped to a single
    ``stage_run_id`` this was a promise the code did not keep: a stage that is
    retried gets a fresh ``stage_runs`` row from ``_begin_stage``, so a restarted
    worker saw none of the earlier attempt's work and described all of it again.
    On a local model that silently costs hours — three of them, on the run that
    exposed this. On a paid provider it is the difference between resuming and
    being billed twice, which is exactly what this function exists to prevent.

    Attempts of the same stage on the same video are the same work by
    definition: a rerun with different settings produces a new ``job_videos``
    row, so it cannot collide here.
    """
    rows = connection.execute(
        "SELECT DISTINCT b.batch_index FROM batches b"
        " JOIN stage_runs prior ON prior.id = b.stage_run_id"
        " JOIN stage_runs mine ON mine.job_video_id = prior.job_video_id"
        "   AND mine.stage = prior.stage"
        " WHERE mine.id = ? AND b.status = 'completed'",
        (stage_run_id,),
    ).fetchall()
    return {int(row["batch_index"]) for row in rows}


def completed_batch_descriptions(
    connection: sqlite3.Connection, stage_run_id: str, output_root: Path
) -> dict[int, list[FrameDescription]]:
    """Descriptions already on disk, read back so a resume can carry them.

    Skipping a completed batch used to contribute nothing to the run's results:
    the counter went up and the descriptions did not come with it, so a resumed
    stage wrote a results file containing only what it happened to redo. The
    work was on disk and the document did not mention it.

    A batch whose artifact is missing or unreadable is **not** returned, so it
    gets described again. Redoing work is wasteful; omitting it silently from
    the evidence is the failure this whole product exists to avoid.
    """
    rows = connection.execute(
        "SELECT DISTINCT b.batch_index, b.artifact_path FROM batches b"
        " JOIN stage_runs prior ON prior.id = b.stage_run_id"
        " JOIN stage_runs mine ON mine.job_video_id = prior.job_video_id"
        "   AND mine.stage = prior.stage"
        " WHERE mine.id = ? AND b.status = 'completed' AND b.artifact_path IS NOT NULL",
        (stage_run_id,),
    ).fetchall()

    fields = {f.name for f in dataclasses.fields(FrameDescription)}
    recovered: dict[int, list[FrameDescription]] = {}

    for row in rows:
        path = Path(output_root) / str(row["artifact_path"])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            described = [
                FrameDescription(**{k: v for k, v in item.items() if k in fields})
                for item in payload.get("descriptions", [])
            ]
        except (OSError, ValueError, TypeError) as error:
            logger.warning(
                "Batch %s was recorded complete but could not be read back (%s); "
                "it will be described again.",
                row["batch_index"],
                error,
            )
            continue
        recovered[int(row["batch_index"])] = described

    return recovered


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
    on_progress: Callable[[int], None] | None = None,
) -> VisualStageResult:
    """Describe every batch, persisting each one before marking it complete.

    ``on_progress`` receives the running count of pictures this stage has dealt
    with — described, skipped, or recognised as already done on a resume. All
    three have moved the run forward, and a counter that only advanced on
    success would stall on a video whose pictures were mostly carried over,
    which is exactly the resume case where someone is most anxious to see
    movement.

    This is the stage that most needs it. A local model at roughly half a minute
    a picture turns a fifteen-hundred-frame video into most of a day, and until
    now every minute of that looked identical to a hang.
    """
    result = VisualStageResult()
    output_dir = Path(output_dir)
    batch_dir = output_dir / BATCH_DIRNAME
    already_done = completed_batch_indexes(connection, stage_run_id)
    carried_forward = completed_batch_descriptions(connection, stage_run_id, output_root)
    running_cost = 0.0
    charged = False
    frames_seen = 0

    def moved_past(request: AnalysisRequest) -> None:
        nonlocal frames_seen
        frames_seen += len(request.frames)
        if on_progress is not None:
            on_progress(frames_seen)

    for batch_index, request in enumerate(requests):
        if batch_index in already_done:
            # Resuming: this batch is already paid for and on disk.
            result.batches_skipped += 1
            if batch_index in carried_forward:
                # Carried, not merely counted. Without this the resumed run wrote
                # a results file describing only the frames it happened to redo.
                result.descriptions.extend(carried_forward[batch_index])
            else:
                # Recorded complete, but the descriptions cannot be read back.
                # Re-sending is not the answer: a completed batch is never sent
                # twice, because on a paid provider that is being charged twice
                # for work already done. So this becomes a visible gap instead —
                # the same treatment as a batch that failed outright. A gap the
                # user can see is recoverable; a frame missing from the document
                # with nothing to say so is not.
                result.skips.extend(
                    skips_for(
                        request,
                        "described earlier, but the saved result could not be read back",
                        attempts=0,
                    )
                )
            logger.debug("Batch %d already completed; not sending again", batch_index)
            moved_past(request)
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
            moved_past(request)
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
        moved_past(request)

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
