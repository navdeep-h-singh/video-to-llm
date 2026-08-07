"""Running stages 1 and 2 for one video.

Each stage records a `stage_runs` row, writes its artifacts atomically, and
registers them only once they are durably on disk. A stage that has already
completed is skipped rather than repeated, so restarting a job resumes where it
left off instead of redoing work.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.artifacts import register_artifact
from app.core.config import Settings
from app.core.db import new_id, utc_now
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.pipeline.audio import (
    AUDIO_FILENAME,
    SILENCE_FILENAME,
    detect_silence,
    extract_audio,
    speech_segments,
    write_silence_windows,
)
from app.pipeline.frames import MANIFEST_FILENAME, extract_frames
from app.pipeline.probe import probe
from app.pipeline.transcribe import (
    TRANSCRIPT_FILENAME,
    FasterWhisperTranscriber,
    SegmentTranscriber,
    TranscriptionProvenance,
    TranscriptionResult,
    build_transcript,
    resolve_backend,
    write_transcript,
)

logger = get_logger(__name__)


@dataclass
class StageContext:
    connection: sqlite3.Connection
    settings: Settings
    job_id: str
    job_video_id: str
    source_path: Path
    output_dir: Path
    interval_ms: int

    @property
    def output_root(self) -> Path:
        return self.settings.output_root  # type: ignore[return-value]


def _stage_completed(connection: sqlite3.Connection, job_video_id: str, stage: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM stage_runs WHERE job_video_id = ? AND stage = ?"
        " AND status IN ('completed', 'completed_with_gaps') LIMIT 1",
        (job_video_id, stage),
    ).fetchone()
    return row is not None


def _begin_stage(context: StageContext, stage: str) -> str:
    attempt_row = context.connection.execute(
        "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM stage_runs"
        " WHERE job_video_id = ? AND stage = ?",
        (context.job_video_id, stage),
    ).fetchone()
    stage_run_id = new_id()
    context.connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, attempt, status, started_at,"
        " created_at, updated_at) VALUES (?,?,?,?,'running',?,?,?)",
        (
            stage_run_id,
            context.job_video_id,
            stage,
            int(attempt_row["next"]),
            utc_now(),
            utc_now(),
            utc_now(),
        ),
    )
    return stage_run_id


def _finish_stage(
    context: StageContext,
    stage_run_id: str,
    *,
    status: str = "completed",
    items_total: int | None = None,
    items_done: int = 0,
    provenance: dict | None = None,
    backend: str | None = None,
    fell_back_from: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    error: str | None = None,
) -> None:
    import json

    context.connection.execute(
        "UPDATE stage_runs SET status = ?, items_total = ?, items_done = ?,"
        " provenance_json = ?, backend = ?, fell_back_from = ?, provider = ?,"
        " model_id = ?, error_message = ?, finished_at = ?, updated_at = ? WHERE id = ?",
        (
            status,
            items_total,
            items_done,
            json.dumps(provenance or {}),
            backend,
            fell_back_from,
            provider,
            model_id,
            error,
            utc_now(),
            utc_now(),
            stage_run_id,
        ),
    )


def _record_event(context: StageContext, message: str, *, level: str = "info", kind: str) -> None:
    context.connection.execute(
        "INSERT INTO events (job_id, job_video_id, level, kind, message, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (context.job_id, context.job_video_id, level, kind, message, utc_now()),
    )


# ── Stage 1 ───────────────────────────────────────────────────────────────


def run_frames_stage(context: StageContext, *, make_api_copies: bool = True) -> int:
    """Extract frames. Returns the number written; 0 when already done."""
    if _stage_completed(context.connection, context.job_video_id, "frames"):
        logger.info("Frames already extracted for %s; skipping", context.source_path.name)
        return 0

    stage_run_id = _begin_stage(context, "frames")
    started = time.monotonic()

    try:
        info = probe(context.source_path)
        result = extract_frames(
            info,
            context.output_dir,
            interval_ms=context.interval_ms,
            make_api_copies=make_api_copies,
        )
    except Exception as error:
        message = redacted_exception_text(error)
        _finish_stage(context, stage_run_id, status="failed", error=message)
        _record_event(
            context,
            f"Could not take pictures from {context.source_path.name}. {message}",
            level="error",
            kind="stage_failed",
        )
        raise

    for path, kind in (
        (result.manifest_path, "frames_manifest"),
        (result.frames_dir, "frames_dir"),
    ):
        register_artifact(
            context.connection,
            output_root=context.output_root,
            path=path,
            kind=kind,
            job_id=context.job_id,
            job_video_id=context.job_video_id,
        )
    if make_api_copies and any(result.api_frames_dir.iterdir()):
        register_artifact(
            context.connection,
            output_root=context.output_root,
            path=result.api_frames_dir,
            kind="frames_api_dir",
            job_id=context.job_id,
            job_video_id=context.job_video_id,
        )

    _finish_stage(
        context,
        stage_run_id,
        items_total=len(result.frames),
        items_done=len(result.frames),
        provenance={
            "interval_ms": context.interval_ms,
            "runtime_seconds": round(time.monotonic() - started, 2),
            "source_duration_seconds": info.duration_seconds,
        },
    )

    # The interval becomes immutable the moment extraction has begun. Changing it
    # afterwards would silently invalidate every frame index already recorded.
    context.connection.execute(
        "UPDATE jobs SET frame_interval_ms = COALESCE(frame_interval_ms, ?), updated_at = ?"
        " WHERE id = ?",
        (context.interval_ms, utc_now(), context.job_id),
    )
    context.connection.execute(
        "UPDATE job_videos SET frame_count = ?, output_dir = ?, updated_at = ? WHERE id = ?",
        (
            len(result.frames),
            str(context.output_dir.relative_to(context.output_root)),
            utc_now(),
            context.job_video_id,
        ),
    )
    _record_event(
        context,
        f"Took {len(result.frames):,} pictures from {context.source_path.name}, "
        f"one every {context.interval_ms / 1000:g} seconds.",
        kind="stage_completed",
    )
    return len(result.frames)


# ── Stage 2 ───────────────────────────────────────────────────────────────


def run_transcription_stage(
    context: StageContext, *, transcriber: SegmentTranscriber | None = None
) -> TranscriptionResult:
    """Extract audio, find silence, transcribe, and preserve the timeline."""
    if _stage_completed(context.connection, context.job_video_id, "transcribe"):
        logger.info("Transcript already exists for %s; skipping", context.source_path.name)
        return TranscriptionResult()

    stage_run_id = _begin_stage(context, "transcribe")
    started = time.monotonic()
    threshold = context.settings.transcription.silence_threshold_seconds

    try:
        info = probe(context.source_path)

        if not info.has_audio:
            # Not a failure. A screen recording with no microphone is ordinary,
            # and the pictures are still worth having.
            _finish_stage(
                context,
                stage_run_id,
                items_total=0,
                items_done=0,
                provenance={"skipped": "no audio track"},
            )
            _record_event(
                context,
                f"{context.source_path.name} has no sound, so there is no transcript.",
                kind="stage_completed",
            )
            return TranscriptionResult()

        audio_path = extract_audio(context.source_path, context.output_dir / AUDIO_FILENAME)

        silences = detect_silence(audio_path, threshold_seconds=threshold)
        segments = speech_segments(silences, info.duration_seconds)

        backend = resolve_backend(context.settings.transcription.backend)
        active = transcriber or FasterWhisperTranscriber(
            model=context.settings.transcription.model,
            backend=backend,
            language=context.settings.transcription.language,
        )

        transcript_segments = build_transcript(audio_path, segments, silences, active)

        result = TranscriptionResult(
            segments=transcript_segments,
            provenance=TranscriptionProvenance(
                requested_backend=context.settings.transcription.backend,
                resolved_backend=backend.name,
                fell_back=backend.fell_back,
                fallback_reason=backend.reason,
                model=context.settings.transcription.model,
                compute_type=backend.compute_type,
                device=backend.device,
                language=context.settings.transcription.language,
                runtime_seconds=time.monotonic() - started,
                segment_count=len([s for s in transcript_segments if not s.is_silence]),
                silence_marker_count=len(silences),
            ),
        )

        silence_path = context.output_dir / SILENCE_FILENAME
        write_silence_windows(silence_path, silences, threshold_seconds=threshold)
        transcript_path, text_path = write_transcript(
            context.output_dir, result, source_filename=context.source_path.name
        )
    except Exception as error:
        message = redacted_exception_text(error)
        _finish_stage(context, stage_run_id, status="failed", error=message)
        _record_event(
            context,
            f"Could not make a transcript for {context.source_path.name}. {message}",
            level="error",
            kind="stage_failed",
        )
        raise

    for path, kind in (
        (transcript_path, "transcript"),
        (text_path, "transcript"),
        (silence_path, "silence_windows"),
    ):
        register_artifact(
            context.connection,
            output_root=context.output_root,
            path=path,
            kind=kind,
            job_id=context.job_id,
            job_video_id=context.job_video_id,
        )

    _finish_stage(
        context,
        stage_run_id,
        items_total=len(segments),
        items_done=len(segments),
        backend=result.provenance.resolved_backend if result.provenance else None,
        fell_back_from=(
            context.settings.transcription.backend
            if result.provenance and result.provenance.fell_back
            else None
        ),
        provenance=result.provenance.as_dict() if result.provenance else {},
    )
    _record_event(
        context,
        f"Finished the transcript for {context.source_path.name}. "
        f"{len(silences)} quiet stretch(es) marked.",
        kind="stage_completed",
    )
    return result


# ── Stage 3 ───────────────────────────────────────────────────────────────


def run_visual_stage(context: StageContext, *, provider: Any = None) -> Any:
    """Describe the extracted frames. Off by default and never blocking.

    A job with descriptions switched off never reaches this function, and a
    failure here produces gaps rather than losing the frames and transcript
    that already succeeded.
    """
    from app.pipeline.visual import (
        DEFAULT_PROMPT,
        VisualStageResult,
        build_batches,
        load_frame_records,
        run_visual_analysis,
        write_visual_results,
    )
    from app.providers.cloud import build_provider
    from app.providers.costs import BudgetTracker

    settings = context.settings
    visual = settings.visual_analysis

    if not visual.enabled or visual.provider == "none":
        return VisualStageResult()

    if _stage_completed(context.connection, context.job_video_id, "visual"):
        logger.info("Descriptions already exist for %s; skipping", context.source_path.name)
        return VisualStageResult()

    manifest = context.output_dir / MANIFEST_FILENAME
    if not manifest.is_file():
        raise FileNotFoundError(
            f"no frame manifest for {context.source_path.name}; "
            "pictures must be taken before they can be described"
        )

    api_frames_dir = context.output_dir / "frames_api"
    stage_run_id = _begin_stage(context, "visual")

    try:
        records = load_frame_records(manifest)

        if visual.provider == "ollama_local":
            from app.providers.ollama_local import resolve_batch_size

            batch_size = resolve_batch_size(settings.ollama.batch_size, preflight_passed=True)
            active = provider or build_provider(
                "ollama_local",
                endpoint=settings.ollama.endpoint,
                model_id=visual.model_id,
                batch_size=batch_size,
            )
        else:
            batch_size = 20
            active = provider or build_provider(visual.provider, model_id=visual.model_id)

        requests = build_batches(
            records,
            api_frames_dir,
            batch_size=batch_size,
            model_id=visual.model_id,
            prompt=DEFAULT_PROMPT,
        )

        budget = BudgetTracker(limit_usd=visual.budget.hard_limit_usd, provider=visual.provider)

        result = run_visual_analysis(
            context.connection,
            stage_run_id=stage_run_id,
            job_id=context.job_id,
            job_video_id=context.job_video_id,
            output_root=context.output_root,
            output_dir=context.output_dir,
            provider=active,
            requests=requests,
            budget=budget,
        )

        results_path, gaps_path = write_visual_results(
            context.output_dir, result, source_filename=context.source_path.name
        )
    except Exception as error:
        message = redacted_exception_text(error)
        _finish_stage(context, stage_run_id, status="failed", error=message)
        _record_event(
            context,
            f"Could not describe the pictures from {context.source_path.name}. {message}",
            level="error",
            kind="stage_failed",
        )
        raise

    register_artifact(
        context.connection,
        output_root=context.output_root,
        path=results_path,
        kind="visual_results",
        job_id=context.job_id,
        job_video_id=context.job_video_id,
    )
    if gaps_path is not None:
        register_artifact(
            context.connection,
            output_root=context.output_root,
            path=gaps_path,
            kind="gaps",
            job_id=context.job_id,
            job_video_id=context.job_video_id,
        )

    _finish_stage(
        context,
        stage_run_id,
        status=result.status,
        items_total=len(records),
        items_done=len(result.descriptions),
        provider=visual.provider,
        model_id=visual.model_id,
        provenance={
            "batches_sent": result.batches_sent,
            "batches_reused": result.batches_skipped,
            "cost": result.cost_label,
            "stopped_on_budget": result.stopped_on_budget,
        },
    )

    if result.has_gaps:
        _record_event(
            context,
            f"Described {len(result.descriptions):,} pictures from "
            f"{context.source_path.name}. {len(result.skips)} could not be described "
            "and are listed in gaps.txt.",
            level="warning",
            kind="stage_completed_with_gaps",
        )
    else:
        _record_event(
            context,
            f"Described {len(result.descriptions):,} pictures from "
            f"{context.source_path.name}. {result.cost_label}.",
            kind="stage_completed",
        )

    return result


def artifact_paths(output_dir: Path) -> dict[str, Path]:
    """Where each stage-1/2 artifact lives, for callers that need to check."""
    return {
        "frames_manifest": output_dir / MANIFEST_FILENAME,
        "transcript": output_dir / TRANSCRIPT_FILENAME,
        "silence_windows": output_dir / SILENCE_FILENAME,
        "audio": output_dir / AUDIO_FILENAME,
    }
