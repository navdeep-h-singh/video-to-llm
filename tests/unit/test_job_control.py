"""Creating jobs, and pause / resume / cancel.

The rule running through all of it: **stopping never destroys finished work.**
Frames, transcripts, and descriptions already on disk cost time and possibly
money. The user asked to stop, not to undo.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.services.jobs import (
    cancel_job,
    create_job,
    parse_paths,
    pause_job,
    resume_job,
)
from app.web.app import create_app
from tests.fixtures.synthetic import ffmpeg_available, make_video
from tests.loopback import LOOPBACK_BASE_URL

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
        yield test_client


def seed(connection, *, job_id="j1", status="ready", video_status="pending"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (job_id, "A job", status, "/out", utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"{job_id}v1", job_id, "/a.mp4", "a.mp4", 0, video_status, utc_now(), utc_now()),
    )


# ── Parsing paths ─────────────────────────────────────────────────────────


def test_one_path_per_line():
    assert len(parse_paths("/a.mp4\n/b.mp4\n/c.mp4")) == 3


def test_blank_lines_and_comments_are_ignored():
    assert len(parse_paths("/a.mp4\n\n  \n# a note\n/b.mp4")) == 2


def test_surrounding_quotes_are_stripped():
    # Dragging a file into a terminal is how most people get a path with
    # spaces, and it arrives quoted.
    assert parse_paths('"/some folder/a video.mp4"')[0].name == "a video.mp4"


def test_whitespace_is_trimmed():
    assert parse_paths("   /a.mp4   ")[0].name == "a.mp4"


def test_an_empty_input_yields_nothing():
    assert parse_paths("") == []
    assert parse_paths("   \n  ") == []


# ── Creating ──────────────────────────────────────────────────────────────


def test_a_job_needs_a_name(db, settings):
    result = create_job(db, settings, name="  ", paths=[])
    assert not result.ok
    assert "needs a name" in result.problems[0]


def test_more_than_twenty_videos_is_refused(db, settings, tmp_path):
    paths = [tmp_path / f"v{i}.mp4" for i in range(21)]
    result = create_job(db, settings, name="Too many", paths=paths)
    assert not result.ok
    assert "more than the 20" in result.problems[0]


def test_unreadable_videos_stop_the_job_before_it_starts(db, settings, tmp_path):
    result = create_job(db, settings, name="Bad", paths=[tmp_path / "missing.mp4"])
    assert not result.ok
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@needs_ffmpeg
def test_a_valid_job_is_created_ready_for_the_worker(db, settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    result = create_job(db, settings, name="Good job", paths=[source.path])

    assert result.ok
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert row["status"] == "ready", "the worker picks it up; the UI does not run it"
    assert row["name"] == "Good job"


@needs_ffmpeg
def test_videos_keep_the_order_they_were_listed_in(db, settings, tmp_path):
    # Never sorted. Two recordings from the same morning have no inherent order.
    # Different durations so they are genuinely different files — identical
    # content would be caught by duplicate detection, which is a different test.
    zulu = make_video(tmp_path / "zulu.mp4", duration_seconds=2.0)
    alpha = make_video(tmp_path / "alpha.mp4", duration_seconds=3.0)

    result = create_job(db, settings, name="Ordered", paths=[zulu.path, alpha.path])
    rows = db.execute(
        "SELECT display_name FROM job_videos WHERE job_id = ? ORDER BY sequence",
        (result.job_id,),
    ).fetchall()
    assert [r["display_name"] for r in rows] == ["zulu.mp4", "alpha.mp4"]


@needs_ffmpeg
def test_the_chosen_interval_is_recorded(db, settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    result = create_job(db, settings, name="Job", paths=[source.path], interval_ms=1000)

    row = db.execute("SELECT frame_interval_ms FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert row["frame_interval_ms"] == 1000


@needs_ffmpeg
def test_a_local_provider_gets_no_spending_cap(db, settings, tmp_path):
    # There is no provider charge to cap.
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    result = create_job(db, settings, name="Local", paths=[source.path], provider="ollama_local")
    row = db.execute("SELECT budget_limit_usd FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert row["budget_limit_usd"] is None


@needs_ffmpeg
def test_an_external_provider_gets_a_spending_cap(db, settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    result = create_job(db, settings, name="Cloud", paths=[source.path], provider="anthropic")
    row = db.execute("SELECT budget_limit_usd FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert row["budget_limit_usd"] == 25.0


@needs_ffmpeg
def test_creation_is_recorded_where_the_user_will_see_it(db, settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    result = create_job(db, settings, name="Job", paths=[source.path])

    row = db.execute(
        "SELECT message FROM events WHERE job_id = ? ORDER BY id", (result.job_id,)
    ).fetchone()
    assert "Job created" in row["message"]


# ── Pause, resume, cancel ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state", ["ready", "preparing", "transcribing", "analyzing", "waiting_retry"]
)
def test_a_live_job_can_be_paused(db, state):
    seed(db, status=state)
    assert pause_job(db, "j1") is True
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "paused"


@pytest.mark.parametrize("state", ["completed", "completed_with_gaps", "cancelled"])
def test_a_finished_job_cannot_be_paused(db, state):
    seed(db, status=state)
    assert pause_job(db, "j1") is False
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == state


def test_pausing_also_pauses_the_unfinished_videos(db):
    seed(db, status="analyzing", video_status="analyzing")
    pause_job(db, "j1")
    assert db.execute("SELECT status FROM job_videos WHERE id='j1v1'").fetchone()["status"] == (
        "paused"
    )


def test_pausing_leaves_finished_videos_alone(db):
    seed(db, status="analyzing", video_status="completed")
    pause_job(db, "j1")
    assert db.execute("SELECT status FROM job_videos WHERE id='j1v1'").fetchone()["status"] == (
        "completed"
    )


def test_pausing_says_finished_work_is_kept(db):
    seed(db, status="analyzing")
    pause_job(db, "j1")
    row = db.execute(
        "SELECT message FROM events WHERE job_id='j1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "kept" in row["message"]


def test_a_paused_job_resumes(db):
    seed(db, status="paused", video_status="paused")
    assert resume_job(db, "j1") is True
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "ready"
    assert db.execute("SELECT status FROM job_videos WHERE id='j1v1'").fetchone()["status"] == (
        "pending"
    )


def test_only_a_paused_job_resumes(db):
    seed(db, status="completed")
    assert resume_job(db, "j1") is False


def test_cancelling_keeps_finished_videos(db):
    """Cancelling is not undoing.

    Work already on disk cost time and possibly money. The user asked to stop.
    """
    seed(db, status="analyzing", video_status="completed")
    assert cancel_job(db, "j1") is True

    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "cancelled"
    assert db.execute("SELECT status FROM job_videos WHERE id='j1v1'").fetchone()["status"] == (
        "completed"
    ), "a finished video must survive cancellation"


def test_cancelling_stops_the_unfinished_videos(db):
    seed(db, status="analyzing", video_status="analyzing")
    cancel_job(db, "j1")
    assert db.execute("SELECT status FROM job_videos WHERE id='j1v1'").fetchone()["status"] == (
        "cancelled"
    )


def test_cancelling_says_nothing_was_thrown_away(db):
    seed(db, status="analyzing")
    cancel_job(db, "j1")
    row = db.execute(
        "SELECT message FROM events WHERE job_id='j1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "nothing was thrown away" in row["message"].lower()


def test_an_already_finished_job_cannot_be_cancelled(db):
    seed(db, status="completed")
    assert cancel_job(db, "j1") is False


def test_controlling_an_unknown_job_is_harmless(db):
    assert pause_job(db, "nope") is False
    assert resume_job(db, "nope") is False
    assert cancel_job(db, "nope") is False


# ── Through the interface ─────────────────────────────────────────────────


def test_the_pause_button_pauses(client, db):
    seed(db, status="analyzing")
    response = client.post("/jobs/j1/pause", follow_redirects=False)
    assert response.status_code == 303
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "paused"


def test_the_resume_button_resumes(client, db):
    seed(db, status="paused", video_status="paused")
    client.post("/jobs/j1/resume", follow_redirects=False)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "ready"


def test_the_cancel_button_cancels(client, db):
    seed(db, status="analyzing")
    client.post("/jobs/j1/cancel", follow_redirects=False)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "cancelled"


def test_the_job_screen_offers_pause_while_running(client, db):
    seed(db, status="analyzing")
    assert "/jobs/j1/pause" in client.get("/jobs/j1").text


def test_the_job_screen_offers_resume_while_paused(client, db):
    seed(db, status="paused")
    body = client.get("/jobs/j1").text
    assert "/jobs/j1/resume" in body
    assert "/jobs/j1/pause" not in body


def test_a_finished_job_offers_neither(client, db):
    seed(db, status="completed")
    body = client.get("/jobs/j1").text
    assert "/jobs/j1/pause" not in body
    assert "/jobs/j1/cancel" not in body


def test_stopping_explains_that_work_is_kept(client, db):
    seed(db, status="analyzing")
    body = client.get("/jobs/j1").text
    assert "nothing is thrown away" in body.lower()


def test_a_bad_job_submission_returns_the_problems(client, db):
    response = client.post("/jobs", data={"name": "", "paths": "/nope.mp4"}, follow_redirects=False)
    assert response.status_code == 200
    assert "cannot start yet" in response.text


def test_a_bad_submission_keeps_what_was_typed(client, db):
    # Retyping a list of paths because one was wrong is a poor experience.
    response = client.post(
        "/jobs", data={"name": "My job", "paths": "/nope.mp4"}, follow_redirects=False
    )
    assert "My job" in response.text
    assert "/nope.mp4" in response.text


@needs_ffmpeg
def test_a_good_job_submission_redirects_to_the_job(client, db, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    response = client.post(
        "/jobs",
        data={"name": "Real job", "paths": str(source.path), "interval": "2000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")


def test_a_collection_needs_a_name_and_a_video(client, db):
    # 400, not 200: the submission was rejected, and the status should say so
    # as plainly as the page does. The message is the part that matters to the
    # person, and it is still there.
    response = client.post("/collections", data={"name": ""}, follow_redirects=False)
    assert response.status_code == 400
    assert "choose at least one video" in response.text
