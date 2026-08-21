"""Pausing a running job actually stops it.

The defect these cover: `pause_job` wrote 'paused' and nothing in the worker
ever read it back. Two things followed, both seen on a real machine. The next
stage's status write landed straight over 'paused', so the pause evaporated and
the interface showed a running status for a job the user believed they had
stopped. And frames kept going to the description model afterwards — on a paid
provider, money spent past an explicit stop request.

Asserted against the database and the calls actually made, never against page
text: the whole point is that the displayed status was the thing lying.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.core.logging import configure_logging
from app.services.jobs import pause_job, resume_job
from app.worker.runner import (
    _HALTED_SQL,
    HALTING_JOB_STATES,
    JobHalted,
    Worker,
)


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


def seed(connection, *, job_status="ready", video_count=2):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("j1", "Course", job_status, "/out", 2000, utc_now(), utc_now()),
    )
    for index in range(video_count):
        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " status, is_active_version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"v{index}",
                "j1",
                f"/src/{index}.mp4",
                f"{index}.mp4",
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


def job_status(db) -> str:
    return db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"]


# ── The guard itself ──────────────────────────────────────────────────────


def test_the_guard_lists_exactly_the_halting_states():
    # The SQL is a literal so it reads plainly at the call site. This is what
    # stops it drifting away from the tuple the rest of the worker checks.
    rendered = "(" + ", ".join(f"'{state}'" for state in HALTING_JOB_STATES) + ")"
    assert rendered == _HALTED_SQL


def test_a_paused_job_is_seen_as_halted(settings, db):
    seed(db)
    subject = worker(settings, db)
    assert subject.halt_requested("j1") is None

    pause_job(db, "j1")
    assert subject.halt_requested("j1") == "paused"


def test_a_cancelled_job_is_seen_as_halted(settings, db):
    seed(db)
    subject = worker(settings, db)
    db.execute("UPDATE jobs SET status='cancelled' WHERE id='j1'")
    assert subject.halt_requested("j1") == "cancelled"


def test_a_deleted_job_reads_as_cancelled_rather_than_crashing(settings, db):
    # Deleting a running job used to be discovered by the worker crashing into a
    # foreign-key failure on its next write. Being told is better than that.
    seed(db)
    subject = worker(settings, db)
    db.execute("DELETE FROM jobs WHERE id='j1'")
    assert subject.halt_requested("j1") == "cancelled"


def test_worker_shutdown_halts_too(settings, db):
    seed(db)
    subject = worker(settings, db)
    subject.request_stop()
    assert subject.halt_requested("j1") == "stopping"


# ── The status write that used to clobber the pause ───────────────────────


def test_a_running_status_does_not_overwrite_a_pause(settings, db):
    seed(db)
    subject = worker(settings, db)
    pause_job(db, "j1")

    subject._set_job_status("j1", "analyzing")

    assert job_status(db) == "paused"


def test_starting_a_job_does_not_overwrite_a_pause(settings, db):
    # `process_job` opens with this write. A job paused between being claimed
    # and being picked up must stay paused.
    seed(db)
    subject = worker(settings, db)
    pause_job(db, "j1")

    subject._set_job_status("j1", "preparing", starting=True)

    assert job_status(db) == "paused"


def test_a_running_status_still_moves_a_job_that_was_not_stopped(settings, db):
    seed(db)
    subject = worker(settings, db)

    subject._set_job_status("j1", "transcribing")

    assert job_status(db) == "transcribing"


def test_a_paused_video_is_not_dragged_back_into_a_running_state(settings, db):
    seed(db)
    subject = worker(settings, db)
    pause_job(db, "j1")

    subject._set_video_status("v0", "transcribing")

    assert db.execute("SELECT status FROM job_videos WHERE id='v0'").fetchone()["status"] == (
        "paused"
    )


# ── Stopping between videos ───────────────────────────────────────────────


def test_a_pause_stops_the_job_before_the_next_video(settings, db, monkeypatch):
    seed(db, video_count=3)
    subject = worker(settings, db)
    started: list[str] = []

    def fake_process_video(job, video):
        started.append(video["id"])
        if len(started) == 1:
            # The user hits Pause while the first video is running.
            pause_job(db, "j1")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    subject.process_job(job)

    assert started == ["v0"], "the second video must not be started after a pause"
    assert job_status(db) == "paused"


def test_a_paused_job_is_not_settled_as_completed(settings, db, monkeypatch):
    # Settling is what made a paused job report a finished one.
    seed(db, video_count=2)
    subject = worker(settings, db)

    def fake_process_video(job, video):
        pause_job(db, "j1")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    subject.process_job(job)

    row = db.execute("SELECT status, completed_at FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "paused"
    assert row["completed_at"] is None


def test_a_job_nobody_stopped_still_settles(settings, db, monkeypatch):
    seed(db, video_count=2)
    subject = worker(settings, db)
    monkeypatch.setattr(subject, "process_video", lambda job, video: True)

    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    subject.process_job(job)

    assert job_status(db) == "completed"


def test_pausing_then_resuming_puts_the_job_back_in_the_queue(settings, db, monkeypatch):
    seed(db, video_count=2)
    subject = worker(settings, db)

    def fake_process_video(job, video):
        pause_job(db, "j1")
        return True

    monkeypatch.setattr(subject, "process_video", fake_process_video)
    subject.process_job(db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone())
    assert job_status(db) == "paused"

    assert resume_job(db, "j1") is True
    assert job_status(db) == "ready"
    remaining = db.execute(
        "SELECT status FROM job_videos WHERE job_id='j1' ORDER BY sequence"
    ).fetchall()
    assert [row["status"] for row in remaining] == ["pending", "pending"]


# ── Stopping inside the expensive stage ───────────────────────────────────


def test_the_stage_is_given_a_stop_check_that_reads_the_job(settings, db, monkeypatch):
    """The callback handed to the description stage must consult the database.

    This is the one that costs money. `run_visual_analysis` already asked a
    `should_stop` callback between batches; nothing ever supplied one, so the
    check was dead code and frames kept being sent after a stop request.
    """
    seed(db, video_count=1)
    subject = worker(settings, db)
    captured: dict = {}

    def capture(context, **_kwargs):
        captured["should_stop"] = context.should_stop
        raise RuntimeError("stop here — the stage itself is not under test")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", capture)
    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    video = db.execute("SELECT * FROM job_videos WHERE id='v0'").fetchone()

    subject.process_video(job, video)

    should_stop = captured["should_stop"]
    assert should_stop is not None
    assert should_stop() is False

    pause_job(db, "j1")
    assert should_stop() is True, "a paused job must stop the description loop"


def test_a_video_halted_mid_stage_never_reaches_assembly(settings, db, monkeypatch):
    seed(db, video_count=1)
    subject = worker(settings, db)
    assembled: list[str] = []

    def pause_and_return(context, **_kwargs):
        pause_job(db, "j1")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", pause_and_return)
    monkeypatch.setattr("app.worker.runner.run_transcription_stage", lambda context: None)
    monkeypatch.setattr(
        "app.worker.runner.run_assembly_stage",
        lambda context, **kwargs: assembled.append(context.job_video_id),
    )

    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    video = db.execute("SELECT * FROM job_videos WHERE id='v0'").fetchone()

    with pytest.raises(JobHalted):
        subject.process_video(job, video)

    assert assembled == []
    assert db.execute("SELECT status FROM job_videos WHERE id='v0'").fetchone()["status"] == (
        "paused"
    )


def test_a_halt_is_not_recorded_as_a_failed_video(settings, db, monkeypatch):
    # A paused video is not a broken one. Marking it 'needs_attention' would put
    # a red state on work the user stopped deliberately.
    seed(db, video_count=1)
    subject = worker(settings, db)

    def pause_and_return(context, **_kwargs):
        pause_job(db, "j1")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", pause_and_return)

    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    video = db.execute("SELECT * FROM job_videos WHERE id='v0'").fetchone()

    with pytest.raises(JobHalted):
        subject.process_video(job, video)

    row = db.execute("SELECT status, error_message FROM job_videos WHERE id='v0'").fetchone()
    assert row["status"] == "paused"
    assert row["error_message"] is None


# ── Across two connections, which is the only way it happens for real ─────


def test_a_pause_written_by_the_interface_is_seen_by_the_worker(settings, db):
    """The load-bearing assumption, proved rather than reasoned about.

    The interface writes 'paused' on its own connection while the worker is
    mid-stage on another. If the worker's connection held a read snapshot — an
    open transaction, say — it would never see the write, and every guard above
    would pass its own tests while doing nothing on a real machine.
    """
    seed(db)
    interface = open_database(settings.output_root)
    try:
        subject = worker(settings, db)
        assert subject.halt_requested("j1") is None

        pause_job(interface, "j1")

        assert subject.halt_requested("j1") == "paused"
    finally:
        interface.close()


def test_the_stage_callback_also_sees_the_other_connection(settings, db, monkeypatch):
    # Same property, at the point where it costs money: the callback handed to
    # the description loop, asked between batches.
    seed(db, video_count=1)
    subject = worker(settings, db)
    captured: dict = {}

    def capture(context, **_kwargs):
        captured["should_stop"] = context.should_stop
        raise RuntimeError("stop here")

    monkeypatch.setattr("app.worker.runner.run_frames_stage", capture)
    job = db.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()
    video = db.execute("SELECT * FROM job_videos WHERE id='v0'").fetchone()
    subject.process_video(job, video)

    interface = open_database(settings.output_root)
    try:
        assert captured["should_stop"]() is False
        pause_job(interface, "j1")
        assert captured["should_stop"]() is True
    finally:
        interface.close()
