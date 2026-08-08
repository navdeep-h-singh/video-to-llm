"""A whole job, end to end, on generated media.

Runs the real worker against real video: real ffprobe, real FFmpeg frame
extraction, real audio extraction, real silence detection. Only the speech model
is substituted, because a real one would need a 1.5 GB download and prove
nothing that the transcription unit tests do not already prove.

The point is that the pieces fit together — that a job goes from 'ready' to
'completed' and leaves the artifacts a later phase will read.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.core.logging import configure_logging
from app.pipeline.frames import API_FRAMES_DIRNAME, FRAMES_DIRNAME, MANIFEST_FILENAME
from app.pipeline.stages import StageContext, run_frames_stage, run_transcription_stage
from app.worker.runner import run_worker
from tests.fixtures.synthetic import ffmpeg_available, make_video, make_video_with_silence

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


@pytest.fixture(autouse=True)
def _quiet_logging():
    configure_logging(level="CRITICAL", force=True)


class StubTranscriber:
    """Stands in for the speech model, reporting window-relative times."""

    def transcribe_window(self, audio_path, start_seconds, end_seconds):
        return [(0.1, min(1.0, end_seconds - start_seconds), "synthetic speech")]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


def _make_job(connection, settings, sources, *, job_id="j1"):
    # The description choice is recorded on the job, exactly as create_job
    # records it. The worker honours the job's own choice rather than whatever
    # the global setting happens to be at the time it runs, so a helper that
    # left this at its 'none' default would be building a job that declines
    # descriptions and then asserting it produced some.
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, visual_provider,"
        " visual_model_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            job_id,
            "Integration job",
            "ready",
            str(settings.output_root),
            settings.visual_analysis.provider,
            settings.visual_analysis.model_id,
            utc_now(),
            utc_now(),
        ),
    )
    for index, source in enumerate(sources):
        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                f"v{index + 1}",
                job_id,
                str(source.path),
                source.path.name,
                index,
                "pending",
                utc_now(),
                utc_now(),
            ),
        )


# ── Stage level ───────────────────────────────────────────────────────────


def test_both_stages_run_and_write_their_artifacts(settings, db, tmp_path):
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
    _make_job(db, settings, [source])

    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=2000,
    )

    frame_count = run_frames_stage(context)
    result = run_transcription_stage(context, transcriber=StubTranscriber())

    assert frame_count >= 3
    assert (context.output_dir / MANIFEST_FILENAME).is_file()
    assert (context.output_dir / "transcript.json").is_file()
    assert (context.output_dir / "silence_windows.json").is_file()
    assert result.segments


def test_a_completed_stage_is_skipped_rather_than_repeated(settings, db, tmp_path):
    # Restarting a job must resume, not redo. Repeating stage 1 would be slow;
    # repeating stage 3 later would cost money.
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=2000,
    )

    first = run_frames_stage(context)
    second = run_frames_stage(context)

    assert first >= 2
    assert second == 0, "a completed stage must not run again"


def test_the_frame_interval_becomes_immutable_after_extraction(settings, db, tmp_path):
    # Changing it later would invalidate every frame index already recorded.
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=1000,
    )

    run_frames_stage(context)
    assert db.execute("SELECT frame_interval_ms FROM jobs WHERE id='j1'").fetchone()[0] == 1000

    context.interval_ms = 5000
    db.execute("DELETE FROM stage_runs WHERE job_video_id='v1'")
    run_frames_stage(context)

    # COALESCE keeps the original; the job's interval is set once and stays.
    assert db.execute("SELECT frame_interval_ms FROM jobs WHERE id='j1'").fetchone()[0] == 1000


def test_a_video_with_no_audio_completes_without_a_transcript(settings, db, tmp_path):
    source = make_video(tmp_path / "src" / "silent.mp4", duration_seconds=4.0, with_audio=False)
    _make_job(db, settings, [source])
    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=2000,
    )

    run_frames_stage(context)
    result = run_transcription_stage(context, transcriber=StubTranscriber())

    assert result.segments == []
    row = db.execute(
        "SELECT status FROM stage_runs WHERE job_video_id='v1' AND stage='transcribe'"
    ).fetchone()
    assert row["status"] == "completed", "no audio is not a failure"


def test_silence_in_real_audio_is_found_and_marked(settings, db, tmp_path):
    source = make_video_with_silence(
        tmp_path / "src" / "gaps.mp4",
        speech_segments=((0.0, 2.0), (8.0, 10.0)),
        duration_seconds=12.0,
    )
    _make_job(db, settings, [source])
    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=2000,
    )

    run_frames_stage(context)
    result = run_transcription_stage(context, transcriber=StubTranscriber())

    payload = json.loads((context.output_dir / "silence_windows.json").read_text("utf-8"))
    assert payload["count"] >= 1, "the 6-second gap should have been found"
    assert any(s.is_silence for s in result.segments)

    # And the timeline is preserved: nothing may claim a time past the video.
    assert all(s.start_seconds <= source.duration_seconds + 1 for s in result.segments)


# ── Job level, through the worker ─────────────────────────────────────────


def test_a_single_video_job_completes(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
    _make_job(db, settings, [source])

    assert run_worker(settings, once=True) == 0

    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "completed"
    assert db.execute("SELECT status FROM job_videos WHERE id='v1'").fetchone()["status"] == (
        "completed"
    )


def test_a_two_video_job_processes_both_in_order(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    first = make_video(tmp_path / "src" / "one.mp4", duration_seconds=4.0)
    second = make_video(tmp_path / "src" / "two.mp4", duration_seconds=4.0)
    _make_job(db, settings, [first, second])

    run_worker(settings, once=True)

    statuses = [
        row["status"] for row in db.execute("SELECT status FROM job_videos ORDER BY sequence")
    ]
    assert statuses == ["completed", "completed"]
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "completed"


def test_one_unreadable_video_does_not_abandon_the_others(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    good = make_video(tmp_path / "src" / "good.mp4", duration_seconds=4.0)
    _make_job(db, settings, [good])
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v2", "j1", "/gone.mp4", "gone.mp4", 1, "pending", utc_now(), utc_now()),
    )

    run_worker(settings, once=True)

    statuses = {row["id"]: row["status"] for row in db.execute("SELECT id, status FROM job_videos")}
    assert statuses["v1"] == "completed", "the readable video must still be processed"
    assert statuses["v2"] == "needs_attention"
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == (
        "needs_attention"
    )


def test_artifacts_are_registered_and_verifiable(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    run_worker(settings, once=True)

    rows = db.execute("SELECT relative_path, kind FROM artifacts").fetchall()
    kinds = {row["kind"] for row in rows}
    assert {"frames_manifest", "frames_dir", "transcript", "silence_windows"} <= kinds

    for row in rows:
        assert (settings.output_root / row["relative_path"]).exists()
        assert not row["relative_path"].startswith("/"), "paths must be relative to the root"


def test_the_recovery_log_reads_in_plain_language(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    run_worker(settings, once=True)

    messages = [row["message"] for row in db.execute("SELECT message FROM events ORDER BY id")]
    assert any("pictures" in m for m in messages)
    assert any("transcript" in m for m in messages)
    # Plain language: no jargon leaking into what the user reads.
    assert not any("ffmpeg" in m.lower() or "sha256" in m.lower() for m in messages)


def test_provider_copies_are_not_made_when_descriptions_are_off(
    settings, db, tmp_path, monkeypatch
):
    # A local-only job has no use for them, and they double the disk cost.
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    run_worker(settings, once=True)

    # The job directory also holds provenance.json and analysis_input/, so pick
    # the video directory by the artifact that identifies one.
    video_dir = next(p.parent for p in (settings.output_root / "j1").rglob("frames_manifest.json"))
    assert (video_dir / FRAMES_DIRNAME).is_dir()
    api_dir = video_dir / API_FRAMES_DIRNAME
    assert not api_dir.exists() or not list(api_dir.glob("*.jpg"))


def test_a_restarted_job_resumes_rather_than_redoing_work(settings, db, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    _make_job(db, settings, [source])
    run_worker(settings, once=True)

    frame_runs_before = db.execute(
        "SELECT COUNT(*) FROM stage_runs WHERE stage='frames'"
    ).fetchone()[0]

    # Put the job back to ready, as a resumed job would be.
    db.execute("UPDATE jobs SET status='ready' WHERE id='j1'")
    db.execute("UPDATE job_videos SET status='pending' WHERE id='v1'")
    run_worker(settings, once=True)

    frame_runs_after = db.execute(
        "SELECT COUNT(*) FROM stage_runs WHERE stage='frames'"
    ).fetchone()[0]
    assert frame_runs_after == frame_runs_before, "completed stages must not run again"


# ── Stage 3 through the worker ────────────────────────────────────────────


class StubVisualProvider:
    """Describes every frame it is given, at no cost."""

    def __init__(self):
        self.batches: list[list[int]] = []

    def describe(self, request):
        from app.providers.base import AnalysisResult, FrameDescription

        self.batches.append([f.index for f in request.frames])
        return AnalysisResult(
            descriptions=[
                FrameDescription(
                    index=f.index,
                    visual_description="a synthetic test pattern",
                    confidence="High",
                )
                for f in request.frames
            ],
            provider="stub",
            model_id="stub-model",
            cost_usd=None,
        )


def _visual_settings(tmp_path):
    from app.core.config import Settings, VisualAnalysisSettings

    return Settings(
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="ollama_local", model_id="qwen2.5vl:7b"
        )
    ).with_output_root(tmp_path / "out")


def test_descriptions_are_produced_and_written(tmp_path, monkeypatch):
    import json

    from app.core.db import open_database
    from app.pipeline.stages import run_visual_stage

    settings = _visual_settings(tmp_path)
    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
        _make_job(connection, settings, [source])

        context = StageContext(
            connection=connection,
            settings=settings,
            job_id="j1",
            job_video_id="v1",
            source_path=source.path,
            output_dir=settings.output_root / "j1" / "v1",
            interval_ms=2000,
        )
        run_frames_stage(context, make_api_copies=True)

        stub = StubVisualProvider()
        result = run_visual_stage(context, provider=stub)

        assert result.descriptions, "descriptions should have been produced"
        assert result.has_gaps is False
        assert result.cost_label == "No provider API charge"

        payload = json.loads(
            (context.output_dir / "visual_results.json").read_text(encoding="utf-8")
        )
        assert payload["description_count"] == len(result.descriptions)
        assert not (context.output_dir / "gaps.txt").exists()
    finally:
        connection.close()


def test_a_local_provider_uses_small_batches(tmp_path):
    from app.core.db import open_database
    from app.pipeline.stages import run_visual_stage

    settings = _visual_settings(tmp_path)
    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
        _make_job(connection, settings, [source])

        context = StageContext(
            connection=connection,
            settings=settings,
            job_id="j1",
            job_video_id="v1",
            source_path=source.path,
            output_dir=settings.output_root / "j1" / "v1",
            interval_ms=2000,
        )
        run_frames_stage(context, make_api_copies=True)

        stub = StubVisualProvider()
        run_visual_stage(context, provider=stub)

        # Never cloud-sized. Batching 20 frames against a local 7B model
        # exhausts memory long before it saves any time.
        assert all(len(batch) <= 2 for batch in stub.batches), stub.batches
    finally:
        connection.close()


def test_descriptions_are_not_produced_again_on_resume(tmp_path):
    from app.core.db import open_database
    from app.pipeline.stages import run_visual_stage

    settings = _visual_settings(tmp_path)
    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
        _make_job(connection, settings, [source])

        context = StageContext(
            connection=connection,
            settings=settings,
            job_id="j1",
            job_video_id="v1",
            source_path=source.path,
            output_dir=settings.output_root / "j1" / "v1",
            interval_ms=2000,
        )
        run_frames_stage(context, make_api_copies=True)
        run_visual_stage(context, provider=StubVisualProvider())

        second = StubVisualProvider()
        run_visual_stage(context, provider=second)

        assert second.batches == [], "a completed visual stage must not run again"
    finally:
        connection.close()


def test_a_local_only_job_makes_no_provider_call(tmp_path, monkeypatch):
    """The default path: descriptions off means Stage 3 never runs at all."""
    from app.core.db import open_database
    from app.pipeline.stages import run_visual_stage

    settings = Settings().with_output_root(tmp_path / "out")  # visual disabled
    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
        _make_job(connection, settings, [source])

        context = StageContext(
            connection=connection,
            settings=settings,
            job_id="j1",
            job_video_id="v1",
            source_path=source.path,
            output_dir=settings.output_root / "j1" / "v1",
            interval_ms=2000,
        )
        run_frames_stage(context, make_api_copies=False)

        def explode(*args, **kwargs):
            raise AssertionError("a provider was built for a local-only job")

        monkeypatch.setattr("app.providers.cloud.build_provider", explode)
        result = run_visual_stage(context)

        assert result.descriptions == []
        assert not (context.output_dir / "visual_results.json").exists()
    finally:
        connection.close()


# ── Assembly and the handoff ──────────────────────────────────────────────


def test_a_job_produces_an_assembled_document(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    from app.core.db import open_database

    settings = Settings().with_output_root(tmp_path / "out")
    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
        _make_job(connection, settings, [source])
    finally:
        connection.close()

    run_worker(settings, once=True)

    assembled = list((settings.output_root / "j1").rglob("assembled.txt"))
    assert len(assembled) == 1
    content = assembled[0].read_text(encoding="utf-8")
    assert "clip.mp4" in content
    assert "synthetic speech" in content, "the transcript should be woven in"


def test_the_assembled_document_is_in_time_order(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    from app.core.db import open_database

    settings = Settings().with_output_root(tmp_path / "out")
    connection = open_database(settings.output_root)
    try:
        source = make_video_with_silence(
            tmp_path / "src" / "gaps.mp4",
            speech_segments=((0.0, 2.0), (8.0, 10.0)),
            duration_seconds=12.0,
        )
        _make_job(connection, settings, [source])
    finally:
        connection.close()

    run_worker(settings, once=True)

    content = next((settings.output_root / "j1").rglob("assembled.txt")).read_text("utf-8")
    stamps = [
        line[:8]
        for line in content.splitlines()
        if len(line) > 8 and line[2] == ":" and line[5] == ":"
    ]
    assert stamps == sorted(stamps), "entries must be in time order"


def test_a_job_with_gaps_completes_with_gaps_rather_than_failing(tmp_path, monkeypatch):
    """A shortfall must be visible, not hidden behind a green tick."""
    from app.core.db import open_database
    from app.providers.base import PermanentProviderError

    class RefusingProvider:
        def describe(self, request):
            raise PermanentProviderError("the model refused")

    settings = _visual_settings(tmp_path)
    monkeypatch.setattr(
        "app.pipeline.stages.FasterWhisperTranscriber", lambda **kwargs: StubTranscriber()
    )
    monkeypatch.setattr("app.providers.cloud.build_provider", lambda *a, **k: RefusingProvider())

    connection = open_database(settings.output_root)
    try:
        source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
        _make_job(connection, settings, [source])
    finally:
        connection.close()

    run_worker(settings, once=True)

    connection = open_database(settings.output_root)
    try:
        job = connection.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()
        video = connection.execute("SELECT status FROM job_videos WHERE id='v1'").fetchone()
    finally:
        connection.close()

    assert video["status"] == "completed_with_gaps"
    assert job["status"] == "completed_with_gaps"
    # And the document still exists — the frames and transcript are worth having.
    assert list((settings.output_root / "j1").rglob("assembled.txt"))


def test_the_handoff_folder_maps_pictures_to_files(tmp_path):
    from app.pipeline.archive import HandoffSource, build_handoff

    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "000000_t000000.jpg").write_bytes(b"x")

    assembled = tmp_path / "assembled.txt"
    assembled.write_text("content", encoding="utf-8")

    result = build_handoff(
        tmp_path / "job",
        [
            HandoffSource(
                display_name="clip.mp4",
                sequence=0,
                assembled_path=assembled,
                frames_dir=frames,
                frame_count=1,
                duration_seconds=10.0,
            )
        ],
        job_name="Test job",
    )

    readme = result.readme_path.read_text(encoding="utf-8")
    assert "picture 47" in readme, "the README should explain the numbering"
    assert "000046_" in readme
    assert result.assembled_files
    assert result.manifest_path.is_file()


def test_the_handoff_references_frames_rather_than_copying_them(tmp_path):
    # A 1,265-frame video is ~1.7 GB. Copying would double the job's disk cost.
    from app.pipeline.archive import HandoffSource, build_handoff

    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "000000_t000000.jpg").write_bytes(b"x" * 1000)
    assembled = tmp_path / "assembled.txt"
    assembled.write_text("content", encoding="utf-8")

    result = build_handoff(
        tmp_path / "job",
        [HandoffSource("clip.mp4", 0, assembled, frames_dir=frames, frame_count=1)],
    )

    # On a platform without symlinks this falls back to copying, which is
    # correct — the handoff must always work.
    assert result.frame_links
    assert result.frame_links[0].exists()


def test_a_portable_handoff_copies_the_frames(tmp_path):
    from app.pipeline.archive import HandoffSource, build_handoff

    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "000000_t000000.jpg").write_bytes(b"x")
    assembled = tmp_path / "assembled.txt"
    assembled.write_text("content", encoding="utf-8")

    result = build_handoff(
        tmp_path / "job",
        [HandoffSource("clip.mp4", 0, assembled, frames_dir=frames, frame_count=1)],
        portable=True,
    )

    assert result.copied_frames is True
    assert (result.frame_links[0] / "000000_t000000.jpg").is_file()
    assert not result.frame_links[0].is_symlink()


# ── Progress is visible while the stage is still running ──────────────────


def test_the_transcript_stage_reports_progress_another_process_can_read(settings, db, tmp_path):
    """The interface is a separate process reading the same database.

    So it is not enough that progress is recorded — it has to be *committed* and
    readable from another connection while the stage is still going. That is the
    whole difference between a bar that moves and the one that sat at 0% for an
    hour before jumping to 100%.

    The observing connection is opened separately and read from inside the
    speech model's own call, which is as close to "what the web process sees
    mid-stage" as a test can get without a second process.
    """
    source = make_video_with_silence(tmp_path / "src" / "talk.mp4", duration_seconds=12.0)
    _make_job(db, settings, [source])

    observer = open_database(settings.output_root, migrate_on_open=False)
    seen: list[tuple[int | None, int | None]] = []

    class WatchingTranscriber:
        def transcribe_window(self, audio_path, start_seconds, end_seconds):
            row = observer.execute(
                "SELECT items_total, items_done FROM stage_runs"
                " WHERE stage = 'transcribe' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                seen.append((row["items_total"], row["items_done"]))
            return [(0.1, min(1.0, end_seconds - start_seconds), "synthetic speech")]

    context = StageContext(
        connection=db,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=source.path,
        output_dir=settings.output_root / "j1" / "v1",
        interval_ms=2000,
    )
    try:
        run_frames_stage(context)
        run_transcription_stage(context, transcriber=WatchingTranscriber())
    finally:
        observer.close()

    assert seen, "the stub was never called; the test proves nothing"

    # The size of the work is published before any of it is done, so the bar has
    # a denominator from the first moment rather than dividing by nothing.
    assert seen[0][0], (
        "items_total was still unset when transcription began — the bar has "
        f"nothing to divide by and renders 0%. Saw {seen[0]}."
    )

    row = db.execute(
        "SELECT items_total, items_done FROM stage_runs WHERE stage = 'transcribe'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["items_done"] > 0
    assert row["items_total"] >= row["items_done"]
