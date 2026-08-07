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
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (job_id, "Integration job", "ready", str(settings.output_root), utc_now(), utc_now()),
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

    video_dir = next((settings.output_root / "j1").iterdir())
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
