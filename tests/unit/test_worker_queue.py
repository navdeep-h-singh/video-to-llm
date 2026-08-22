"""One job at a time, and now a way to say which one.

The defect: `claim_next_job` took the single oldest `ready` job and
`process_job` did not return until every video in it was finished, so a job
queued behind a long one waited for the whole thing. Observed 2026-08-12 — a
thirteen-video job sat at `ready` with `started_at` NULL while a one-video job
ahead of it ground through local descriptions for hours.

Note what that observation rules out. The blocker was a *single video*, so
neither reordering the queue nor stopping between videos would have helped on
its own: the running job has to be able to put the work down mid-video, at the
same checkpoints a pause uses, and pick it up again afterwards.

The twin of the pause defect, and fixed with the same machinery: re-read the
state at a checkpoint instead of treating a claimed job as uninterruptible.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.core.logging import configure_logging
from app.pipeline import stages as stages_module
from app.services.jobs import (
    _QUEUEABLE_SQL,
    QUEUEABLE,
    pause_job,
    queue_order,
    run_next,
)
from app.worker.runner import JobHalted, JobYielded, Worker
from tests.fixtures.synthetic import ffmpeg_available, make_video


@pytest.fixture(autouse=True)
def _quiet_logging():
    configure_logging(level="CRITICAL", force=True)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


def seed(connection, job_id, *, status="ready", created="2026-01-01T00:00:00Z", videos=1):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, f"Job {job_id}", status, "/out", 2000, created, utc_now()),
    )
    for index in range(videos):
        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " status, is_active_version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"{job_id}v{index}",
                job_id,
                f"/src/{job_id}{index}.mp4",
                f"{job_id}{index}.mp4",
                index,
                "pending",
                1,
                utc_now(),
                utc_now(),
            ),
        )
    connection.commit()


def worker(settings, db) -> Worker:
    return Worker(settings, db, worker_id="w1")


def row(db, job_id):
    return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


# ── The guard, and the default ────────────────────────────────────────────


def test_the_queue_sql_lists_exactly_the_queueable_states():
    rendered = "(" + ", ".join(f"'{state}'" for state in QUEUEABLE) + ")"
    assert rendered == _QUEUEABLE_SQL


def test_a_queue_nobody_reordered_still_runs_oldest_first(settings, db):
    # Every job starts at priority 0, so the existing behaviour is the default
    # and nothing changes for anyone who never touches this.
    seed(db, "old", created="2026-01-01T00:00:00Z")
    seed(db, "new", created="2026-06-01T00:00:00Z")

    assert worker(settings, db).claim_next_job()["id"] == "old"


def test_priority_beats_age(settings, db):
    seed(db, "old", created="2026-01-01T00:00:00Z")
    seed(db, "new", created="2026-06-01T00:00:00Z")

    assert run_next(db, "new") is True

    assert worker(settings, db).claim_next_job()["id"] == "new"


# ── Saying which job goes next ────────────────────────────────────────────


def test_run_next_puts_a_job_at_the_front(settings, db):
    seed(db, "a", created="2026-01-01T00:00:00Z")
    seed(db, "b", created="2026-02-01T00:00:00Z")
    seed(db, "c", created="2026-03-01T00:00:00Z")

    run_next(db, "c")

    assert [r["id"] for r in queue_order(db)] == ["c", "a", "b"]


def test_promoting_twice_keeps_the_most_recent_request_in_front(settings, db):
    seed(db, "a", created="2026-01-01T00:00:00Z")
    seed(db, "b", created="2026-02-01T00:00:00Z")
    seed(db, "c", created="2026-03-01T00:00:00Z")

    run_next(db, "c")
    run_next(db, "b")

    assert [r["id"] for r in queue_order(db)] == ["b", "c", "a"]


def test_promoting_a_job_already_at_the_front_is_success(settings, db):
    # The user asked for this job to run next; it is running next. Reporting
    # "cannot" for a request already satisfied makes people press again.
    seed(db, "a")
    seed(db, "b", created="2026-02-01T00:00:00Z")
    run_next(db, "b")
    before = row(db, "b")["priority"]

    assert run_next(db, "b") is True
    assert row(db, "b")["priority"] == before


def test_a_finished_job_cannot_be_moved_up_a_queue(settings, db):
    seed(db, "done", status="completed")
    assert run_next(db, "done") is False


def test_a_paused_job_cannot_be_moved_up_a_queue(settings, db):
    # A paused job is not waiting for the worker. It is waiting for the user,
    # and putting it at the front of a queue it is not in would say otherwise.
    seed(db, "a")
    pause_job(db, "a")
    assert run_next(db, "a") is False


def test_a_missing_job_is_refused_rather_than_created(settings, db):
    assert run_next(db, "no-such-job") is False


# ── Stepping aside ────────────────────────────────────────────────────────


def test_a_running_job_sees_a_job_moved_ahead_of_it(settings, db):
    seed(db, "long", created="2026-01-01T00:00:00Z")
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)

    assert subject.outranked_by(row(db, "long")) is None

    run_next(db, "urgent")

    ahead = subject.outranked_by(row(db, "long"))
    assert ahead is not None
    assert ahead["id"] == "urgent"


def test_equal_priority_is_never_enough_to_take_over(settings, db):
    # Strictly higher, so two jobs cannot take turns pushing each other aside.
    seed(db, "a", created="2026-01-01T00:00:00Z")
    seed(db, "b", created="2026-02-01T00:00:00Z")

    assert worker(settings, db).outranked_by(row(db, "a")) is None


def test_a_one_shot_run_never_steps_aside(settings, db):
    # `video-to-llm process` was told to do one particular thing. Abandoning it
    # halfway to run something queued in the interface serves nobody.
    seed(db, "mine", created="2026-01-01T00:00:00Z")
    seed(db, "other", created="2026-02-01T00:00:00Z")
    run_next(db, "other")

    subject = Worker(settings, db, worker_id="w1", only_job_id="mine")

    assert subject.outranked_by(row(db, "mine")) is None


def test_the_job_in_front_goes_back_to_ready_not_paused(settings, db, monkeypatch):
    """The whole point of a separate outcome.

    Nobody stopped this job. Writing 'paused' would be exactly the kind of
    status-the-user-did-not-ask-for that the pause fix existed to remove.
    """
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=3)
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)
    started: list[str] = []

    def fake_process_video(job, video):
        started.append(video["id"])
        if len(started) == 1:
            run_next(db, "urgent")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    subject.process_job(row(db, "long"))

    assert started == ["longv0"], "the next video must not start once something is ahead"
    assert row(db, "long")["status"] == "ready"
    assert row(db, "long")["completed_at"] is None


def test_the_promoted_job_is_what_the_worker_picks_up_next(settings, db, monkeypatch):
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=3)
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)

    def fake_process_video(job, video):
        run_next(db, "urgent")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    subject.process_job(row(db, "long"))

    assert subject.claim_next_job()["id"] == "urgent"


def test_the_interrupted_video_goes_back_in_the_queue_not_into_a_failed_state(
    settings, db, monkeypatch
):
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=2)
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)

    def yield_during_frames(context, **_kwargs):
        run_next(db, "urgent")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", yield_during_frames)

    with pytest.raises(JobYielded):
        subject.process_video(
            row(db, "long"), db.execute("SELECT * FROM job_videos WHERE id='longv0'").fetchone()
        )

    subject._step_aside(row(db, "long"), JobYielded("Job urgent"))

    video = db.execute("SELECT status, error_message FROM job_videos WHERE id='longv0'").fetchone()
    assert video["status"] == "pending"
    assert video["error_message"] is None


def test_stepping_aside_says_so_in_the_job_log(settings, db, monkeypatch):
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=2)
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)

    def fake_process_video(job, video):
        run_next(db, "urgent")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    subject.process_job(row(db, "long"))

    message = db.execute(
        "SELECT message FROM events WHERE job_id='long' AND kind='queue' ORDER BY id DESC LIMIT 1"
    ).fetchone()["message"]
    assert "Job urgent" in message
    assert "picks up where it stopped" in message


# ── A pause still wins ────────────────────────────────────────────────────


def test_a_pause_beats_a_queue_change(settings, db, monkeypatch):
    """One is an instruction about this job; the other is a preference about
    the queue. The instruction wins."""
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=3)
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)

    run_next(db, "urgent")
    pause_job(db, "long")

    with pytest.raises(JobHalted):
        subject._checkpoint(row(db, "long"))


def test_a_paused_job_is_not_dragged_back_to_ready_by_a_step_aside(settings, db):
    seed(db, "long", created="2026-01-01T00:00:00Z", videos=2)
    subject = worker(settings, db)
    pause_job(db, "long")

    subject._step_aside(row(db, "long"), JobYielded("Something else"))

    assert row(db, "long")["status"] == "paused"


# ── The long stage puts itself down ───────────────────────────────────────


def test_the_description_stage_is_told_to_stop_for_a_queue_change_too(settings, db, monkeypatch):
    """The checkpoint that matters for the case actually observed.

    The job that blocked the queue was a *single video* being described
    locally, so stopping between videos would not have freed anything. The
    callback the description loop consults between batches has to answer yes
    for a queue change, not only for a pause.
    """
    seed(db, "long", created="2026-01-01T00:00:00Z")
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)
    captured: dict = {}

    def capture(context, **_kwargs):
        captured["should_stop"] = context.should_stop
        raise RuntimeError("stop here — the stage itself is not under test")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", capture)
    subject.process_video(
        row(db, "long"),
        db.execute("SELECT * FROM job_videos WHERE id='longv0'").fetchone(),
    )

    should_stop = captured["should_stop"]
    assert should_stop() is False

    run_next(db, "urgent")
    assert should_stop() is True, "a description loop must put itself down for a queue change"


def test_a_queue_change_made_on_another_connection_is_seen(settings, db):
    # Same property the pause fix rests on: the interface writes on its own
    # connection while the worker is mid-stage on another.
    seed(db, "long", created="2026-01-01T00:00:00Z")
    seed(db, "urgent", created="2026-02-01T00:00:00Z")
    subject = worker(settings, db)
    interface = open_database(settings.output_root)
    try:
        assert subject.outranked_by(row(db, "long")) is None
        run_next(interface, "urgent")
        assert subject.outranked_by(row(db, "long")) is not None
    finally:
        interface.close()


# ── The whole cycle, on real media ────────────────────────────────────────
#
# Everything above fakes the stages, which proves the parts and not the whole.
# This runs the real pipeline: a job is interrupted partway, goes back in the
# queue, the promoted job runs, and then the interrupted one resumes and
# finishes with the output it would have had if nothing had happened.

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


@needs_ffmpeg
def test_a_job_that_stepped_aside_resumes_and_finishes(settings, db, tmp_path, monkeypatch):
    from app.services.jobs import create_job
    from app.worker.runner import run_worker

    first = make_video(tmp_path / "src" / "first.mp4", duration_seconds=4.0)
    second = make_video(tmp_path / "src" / "second.mp4", duration_seconds=4.0)

    long_job = create_job(db, settings, name="Long", paths=[first.path])
    urgent_job = create_job(db, settings, name="Urgent", paths=[second.path])
    assert long_job.ok and urgent_job.ok
    db.commit()

    # 'Long' is older, so it is claimed first. Partway through its only video,
    # the user moves 'Urgent' ahead of it.
    real_transcription = stages_module.run_transcription_stage
    promoted = {"done": False}

    def promote_once(context):
        if not promoted["done"]:
            promoted["done"] = True
            interface = open_database(settings.output_root)
            try:
                run_next(interface, urgent_job.job_id)
            finally:
                interface.close()
        return real_transcription(context)

    monkeypatch.setattr("app.worker.runner.run_transcription_stage", promote_once)

    def statuses():
        return {
            r["name"]: r["status"] for r in db.execute("SELECT name, status FROM jobs").fetchall()
        }

    # Turn one: 'Long' is claimed, is interrupted partway, and goes back in the
    # queue. Checked here rather than at the end because start-up reconciliation
    # would return an interrupted job to 'ready' anyway — asserting only the
    # final state would pass whether this worked or not.
    run_worker(settings, once=True)
    assert statuses() == {"Long": "ready", "Urgent": "ready"}
    assert (
        db.execute("SELECT status FROM job_videos WHERE job_id = ?", (long_job.job_id,)).fetchone()[
            "status"
        ]
        == "pending"
    )

    # Turn two: the promoted job runs, because it now outranks the other.
    run_worker(settings, once=True)
    assert statuses() == {"Long": "ready", "Urgent": "completed"}

    # Turn three: nothing is ahead of it any more, so it resumes and finishes.
    run_worker(settings, once=True)
    assert statuses() == {"Long": "completed", "Urgent": "completed"}

    # And the interrupted job's output is whole, not a fragment.
    root = settings.output_root
    documents = sorted(root.glob("*/*/assembled.txt"))
    assert len(documents) == 2
    for document in documents:
        assert document.read_text(encoding="utf-8").strip()


@needs_ffmpeg
def test_stepping_aside_does_not_redo_the_stage_it_already_finished(
    settings, db, tmp_path, monkeypatch
):
    """Resuming must cost only the interrupted part.

    Extraction is the expensive stage before descriptions exist, and a resume
    that ran it again would quietly double the work on every promotion. Counted
    at `extract_frames` rather than at the stage wrapper: the wrapper is
    re-entered on a resume and returns immediately, which is the cheap half of
    exactly the behaviour under test.
    """
    from app.services.jobs import create_job
    from app.worker.runner import run_worker

    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    long_job = create_job(db, settings, name="Long", paths=[source.path])
    other = create_job(db, settings, name="Urgent", paths=[source.path])
    assert long_job.ok and other.ok
    db.commit()

    real_extract = stages_module.extract_frames
    calls = {"extractions": 0}
    promoted = {"done": False}

    def counting_extract(*args, **kwargs):
        calls["extractions"] += 1
        return real_extract(*args, **kwargs)

    def promote_once(context):
        if not promoted["done"]:
            promoted["done"] = True
            interface = open_database(settings.output_root)
            try:
                run_next(interface, other.job_id)
            finally:
                interface.close()
        return stages_module.run_transcription_stage(context)

    monkeypatch.setattr("app.pipeline.stages.extract_frames", counting_extract)
    monkeypatch.setattr("app.worker.runner.run_transcription_stage", promote_once)

    for _ in range(3):
        run_worker(settings, once=True)

    assert {r["status"] for r in db.execute("SELECT status FROM jobs")} == {"completed"}

    # Two jobs, one video each. A third extraction would mean the interruption
    # cost a full re-run of a stage that had already finished.
    assert calls["extractions"] == 2
