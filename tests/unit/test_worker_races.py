"""Probe suite E — state races, asserted against the database, not page text."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL

RUNNING_STATES = ("preparing", "transcribing", "analyzing")


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


def seed(connection, job_id="j1", status="completed", provider="none"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, "Course", status, "/out", 2000, provider, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, frame_count, duration_seconds, output_dir, is_active_version,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{job_id}v1",
            job_id,
            "/src/a.mp4",
            "a.mp4",
            0,
            status,
            10,
            20.0,
            f"{job_id}/v1",
            1,
            utc_now(),
            utc_now(),
        ),
    )
    connection.commit()


@pytest.mark.parametrize("state", RUNNING_STATES)
def test_e1_describe_does_not_reset_a_job_the_worker_owns(client, db, settings, state):
    """AC: 'Describe again' must not rewrite the status of a job in flight.

    The route sets status='ready' unconditionally. A worker part-way through
    that job then has the row it is processing put back in the queue, so a
    second pass can start over the top of the first.
    """
    from app.core.config import VisualAnalysisSettings

    seed(db, status=state)
    live = settings.__class__(
        output_root=settings.output_root,
        visual_analysis=VisualAnalysisSettings(enabled=True, provider="ollama_local"),
    )
    with TestClient(create_app(live), base_url=LOOPBACK_BASE_URL) as c:
        c.post("/jobs/j1/describe", data={"video_id": "j1v1"}, follow_redirects=False)

    after = db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"]
    assert after == state, (
        f"A job in '{state}' was moved to '{after}' by 'Describe again'. "
        "The worker is still processing that row."
    )


@pytest.mark.parametrize("state", RUNNING_STATES)
def test_e2_delete_refuses_a_job_in_flight(client, db, state):
    """AC: the worker's output folder must not be removed under it."""
    seed(db, status=state)
    client.post("/jobs/j1/delete", data={"remove_files": "1"}, follow_redirects=False)
    row = db.execute("SELECT id FROM jobs WHERE id='j1'").fetchone()
    assert row is not None, (
        f"A job in '{state}' was deleted with its files while the worker was writing "
        "into that folder."
    )


def test_e3_second_worker_cannot_claim_a_live_root(settings, db):
    """AC: two workers must never own one output root."""
    from app.core.locks import WorkerAlreadyRunningError, worker_lock

    with worker_lock(db, settings.output_root):
        second = open_database(settings.output_root, migrate_on_open=False)
        try:
            with pytest.raises(WorkerAlreadyRunningError):
                with worker_lock(second, settings.output_root):
                    pass
        finally:
            second.close()


def test_e4_cancel_then_describe_does_not_silently_restart(client, db, settings):
    """AC: a cancelled job restarted by another control must say it restarted."""
    from app.core.config import VisualAnalysisSettings

    seed(db, status="cancelled")
    live = settings.__class__(
        output_root=settings.output_root,
        visual_analysis=VisualAnalysisSettings(enabled=True, provider="ollama_local"),
    )
    with TestClient(create_app(live), base_url=LOOPBACK_BASE_URL) as c:
        c.post("/jobs/j1/describe", data={"video_id": "j1v1"}, follow_redirects=False)
    after = db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"]
    events = db.execute(
        "SELECT message FROM events WHERE job_id='j1' ORDER BY id DESC LIMIT 3"
    ).fetchall()
    if after == "ready":
        text = " ".join(e["message"].lower() for e in events)
        # Any wording that admits the job was stopped and has been requeued.
        assert any(w in text for w in ("cancel", "restart", "stopped", "back in the queue")), (
            "A cancelled job was put back in the queue by 'Describe again' with no "
            "event explaining that the cancellation was undone."
        )


def test_e5_describe_on_a_missing_job_is_not_a_silent_success(client, db, settings):
    from app.core.config import VisualAnalysisSettings

    live = settings.__class__(
        output_root=settings.output_root,
        visual_analysis=VisualAnalysisSettings(enabled=True, provider="ollama_local"),
    )
    with TestClient(create_app(live), base_url=LOOPBACK_BASE_URL) as c:
        response = c.post("/jobs/ghost/describe", data={"video_id": "x"}, follow_redirects=False)
    assert response.status_code == 404 or "ghost" not in response.headers.get("location", ""), (
        "Requesting descriptions for a job that does not exist redirects to that "
        "job's page as though it had worked."
    )


def test_e6_orphan_events_are_not_created_for_missing_jobs(client, db, settings):
    """AC: a foreign key on events must not be writable for a job that is gone."""
    from app.core.config import VisualAnalysisSettings

    live = settings.__class__(
        output_root=settings.output_root,
        visual_analysis=VisualAnalysisSettings(enabled=False, provider="none"),
    )
    with TestClient(create_app(live), base_url=LOOPBACK_BASE_URL) as c:
        response = c.post("/jobs/ghost/describe", data={"video_id": "x"}, follow_redirects=False)
    assert response.status_code < 500, (
        f"Describing a nonexistent job returned {response.status_code} — the route "
        "writes an event row keyed to a job that does not exist."
    )
