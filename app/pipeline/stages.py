"""Running stages 1 and 2 for one video.

Each stage records a `stage_runs` row, writes its artifacts atomically, and
registers them only once they are durably on disk. A stage that has already
completed is skipped rather than repeated, so restarting a job resumes where it
left off instead of redoing work.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.artifacts import register_artifact, relative_to_root
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
from app.pipeline.progress import StageProgress, format_clock
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

    #: Asked between units of work inside a long stage. True means the user has
    #: paused or cancelled the job, or the worker is shutting down. Only the
    #: description stage is long enough — and expensive enough — to consult it.
    should_stop: Callable[[], bool] | None = None

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
            relative_to_root(context.output_dir, context.output_root),
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

        # Seconds of video covered, not speech segments walked. Segments run
        # from a second to several minutes, so a count of them jumps unevenly
        # and any estimate built on it is wrong; audio-time advances steadily.
        progress = StageProgress(
            context.connection,
            stage_run_id,
            on_event=lambda done, total: _record_event(
                context,
                f"Still writing the transcript for {context.source_path.name} — "
                f"{format_clock(done)} of {format_clock(total)} covered.",
                kind="stage_progress",
            ),
        )
        progress.set_total(int(info.duration_seconds or 0))

        backend = resolve_backend(context.settings.transcription.backend)
        active = transcriber or FasterWhisperTranscriber(
            model=context.settings.transcription.model,
            backend=backend,
            language=context.settings.transcription.language,
        )

        transcript_segments = build_transcript(
            audio_path, segments, silences, active, on_progress=progress.advance_to
        )
        progress.finish()

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
        total_frames = len(records)

        # A rerun describes only the frames it was asked to. Everything else
        # keeps the description the previous version produced — those were
        # already paid for, and re-sending them would charge a second time to
        # arrive at the same answer.
        from app.pipeline.rerun import carried_descriptions, load_rerun_plan

        rerun_plan = load_rerun_plan(context.output_dir)
        carried: list[Any] = []
        if rerun_plan is not None:
            records = [r for r in records if int(r["index"]) in rerun_plan.indices]
            carried = carried_descriptions(rerun_plan, context.output_root)
            logger.info(
                "Rerun of %s: describing %d frame(s), keeping %d from version %d",
                context.source_path.name,
                len(records),
                len(carried),
                rerun_plan.from_version,
            )

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

        visual_progress = StageProgress(
            context.connection,
            stage_run_id,
            on_event=lambda done, total: _record_event(
                context,
                f"Still describing pictures from {context.source_path.name} — "
                f"{done:,} of {total:,} done.",
                kind="stage_progress",
            ),
        )
        visual_progress.set_total(sum(len(r.frames) for r in requests))

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
            should_stop=context.should_stop,
            on_progress=visual_progress.advance_to,
            on_flush=visual_progress.flush,
            on_carried=visual_progress.note_carried,
        )
        visual_progress.finish()

        if carried:
            # Merged before writing, and ordered by frame number, so the results
            # file for this version describes the whole video rather than only
            # the part that was redone.
            import dataclasses

            from app.providers.base import FrameDescription

            # Filtered to fields that still exist: a description written by an
            # earlier schema may carry keys this one dropped, and a rerun
            # failing on the previous version's file would make older output
            # impossible to build on — which is the opposite of the point.
            known = {f.name for f in dataclasses.fields(FrameDescription)}
            existing = {int(d.index) for d in result.descriptions}
            for entry in carried:
                if int(entry["index"]) in existing:
                    continue
                result.descriptions.append(
                    FrameDescription(**{k: v for k, v in entry.items() if k in known})
                )
            result.descriptions.sort(key=lambda d: int(d.index))

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

    # A stage that stopped early has not finished, whatever it managed to
    # describe. Recording it 'completed' would make `_stage_completed` skip it
    # on resume, and the frames after the stopping point would never be
    # described at all — the pause would quietly cost the user the rest of the
    # video. 'paused' is not a completed state, so resuming re-enters the stage,
    # and the batches already on disk are carried across attempts rather than
    # sent — and paid for — a second time.
    #
    # Scoped to a user stop. A budget stop already has its own reporting and its
    # own settled meaning — the job is finished, at the limit the user set — and
    # widening this to cover it would change what a capped job says it did.
    stopped_early = result.stopped_at_index is not None and not result.stopped_on_budget
    _finish_stage(
        context,
        stage_run_id,
        status="paused" if stopped_early else result.status,
        items_total=total_frames,
        items_done=len(result.descriptions),
        provider=visual.provider,
        model_id=visual.model_id,
        provenance={
            "batches_sent": result.batches_sent,
            "batches_reused": result.batches_skipped,
            "cost": result.cost_label,
            "stopped_on_budget": result.stopped_on_budget,
            # Separated so the record answers "what did this run actually do"
            # rather than only "what does this version contain".
            "described_this_run": len(records),
            "carried_over": len(carried),
            "rerun_scope": rerun_plan.scope if rerun_plan is not None else None,
        },
    )

    if stopped_early:
        _record_event(
            context,
            f"Stopped describing {context.source_path.name} because you asked it to stop. "
            f"{len(result.descriptions):,} pictures are described and kept; "
            "starting the job again picks up from the next one.",
            level="warning",
            kind="stage_paused",
        )
    elif result.has_gaps:
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


# ── Stages 4-5: enrich and assemble ───────────────────────────────────────


def _load_visual_descriptions(output_dir: Path) -> tuple[list[Any], int]:
    """Read back the descriptions Stage 3 wrote, if any. Returns (list, gaps)."""
    from app.pipeline.visual import VISUAL_RESULTS_FILENAME
    from app.providers.base import FrameDescription

    results_path = Path(output_dir) / VISUAL_RESULTS_FILENAME
    if not results_path.is_file():
        return [], 0

    import json

    payload = json.loads(results_path.read_text(encoding="utf-8"))
    descriptions = [
        FrameDescription(
            **{k: v for k, v in entry.items() if k in FrameDescription.__annotations__}
        )
        for entry in payload.get("descriptions", [])
    ]
    return descriptions, int(payload.get("skip_count", 0))


def _load_transcript_segments(output_dir: Path) -> list[Any]:
    from app.pipeline.transcribe import TRANSCRIPT_FILENAME, TranscriptSegment

    path = Path(output_dir) / TRANSCRIPT_FILENAME
    if not path.is_file():
        return []

    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        TranscriptSegment(
            start_seconds=float(entry["start_seconds"]),
            end_seconds=float(entry["end_seconds"]),
            text=str(entry["text"]),
            is_silence=bool(entry.get("is_silence", False)),
        )
        for entry in payload.get("segments", [])
    ]


def run_assembly_stage(context: StageContext, *, display_name: str = "") -> Path:
    """Enrich deterministically, then write this video's `assembled.txt`."""
    from app.pipeline.assemble import assemble_video, write_assembled
    from app.pipeline.enrich import enrich

    if _stage_completed(context.connection, context.job_video_id, "assemble"):
        logger.info("Already assembled for %s; skipping", context.source_path.name)
        return context.output_dir / "assembled.txt"

    stage_run_id = _begin_stage(context, "assemble")

    try:
        info = probe(context.source_path)
        transcript_segments = _load_transcript_segments(context.output_dir)
        descriptions, gap_count = _load_visual_descriptions(context.output_dir)

        silences: list[Any] = []
        silence_path = context.output_dir / SILENCE_FILENAME
        if silence_path.is_file():
            import json

            from app.pipeline.audio import SilenceWindow

            payload = json.loads(silence_path.read_text(encoding="utf-8"))
            silences = [
                SilenceWindow(float(w["start_seconds"]), float(w["end_seconds"]))
                for w in payload.get("windows", [])
            ]

        enrichment = enrich(descriptions, info.duration_seconds, silences)

        # Counted from the manifest rather than from the descriptions: they are
        # different numbers whenever descriptions are off or incomplete, which
        # is the common case rather than the exception.
        frame_records = []
        manifest_path = context.output_dir / MANIFEST_FILENAME
        if manifest_path.is_file():
            from app.pipeline.visual import load_frame_records

            frame_records = load_frame_records(manifest_path)

        content = assemble_video(
            display_name=display_name or context.source_path.name,
            duration_seconds=info.duration_seconds,
            transcript_segments=transcript_segments,
            descriptions=descriptions,
            enrichment=enrichment,
            interval_ms=context.interval_ms,
            gap_count=gap_count,
            frame_count=len(frame_records) if frame_records else None,
        )
        assembled_path = write_assembled(context.output_dir, content)
    except Exception as error:
        message = redacted_exception_text(error)
        _finish_stage(context, stage_run_id, status="failed", error=message)
        _record_event(
            context,
            f"Could not put {context.source_path.name} together. {message}",
            level="error",
            kind="stage_failed",
        )
        raise

    register_artifact(
        context.connection,
        output_root=context.output_root,
        path=assembled_path,
        kind="assembled",
        job_id=context.job_id,
        job_video_id=context.job_video_id,
    )

    _finish_stage(
        context,
        stage_run_id,
        items_total=len(transcript_segments) + len(descriptions),
        items_done=len(transcript_segments) + len(descriptions),
        provenance={
            "sections": len(enrichment.segments),
            "emphasis": len(enrichment.emphasis),
            "switches": len(enrichment.switches),
            "gap_count": gap_count,
        },
    )
    _record_event(
        context,
        f"Put {context.source_path.name} together in time order — "
        f"{len(enrichment.segments)} section(s).",
        kind="stage_completed",
    )
    return assembled_path
