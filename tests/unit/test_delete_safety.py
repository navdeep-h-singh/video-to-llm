"""Probe suite C — the delete path, proven against real files on disk."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL


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


def seed_job_with_artifacts(connection, root, job_id="j1"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, "Fifteen hour course", "completed", str(root), 2000, "none", utc_now(), utc_now()),
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
            "completed",
            10,
            20.0,
            f"{job_id}/v1",
            1,
            utc_now(),
            utc_now(),
        ),
    )
    connection.commit()

    job_dir = root / job_id / "v1"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "assembled.txt").write_text("expensive evidence")
    (job_dir / "transcript.txt").write_text("hours of transcription")
    return job_id, root / job_id


def test_c1_failed_delete_does_not_destroy_the_artifacts_first(client, db, settings):
    """AC: a delete that cannot complete must leave the artifacts intact.

    The route removes the folder and *then* deletes the row. When a collection
    references the job the DELETE raises, so the expensive output is already
    gone while the row that describes it survives.
    """
    job_id, job_dir = seed_job_with_artifacts(db, settings.output_root)
    client.post(
        "/collections",
        data={"name": "c", "video": [f"{job_id}v1"], "order": f"{job_id}v1"},
        follow_redirects=False,
    )
    assert db.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 1

    try:
        client.post(f"/jobs/{job_id}/delete", data={"remove_files": "1"}, follow_redirects=False)
    except Exception:
        pass  # the 500 itself is asserted separately

    row = db.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is not None:
        assert job_dir.exists(), (
            "The delete failed and rolled back the database row, but the frames, "
            "transcript and assembled document had already been erased from disk. "
            "The job still appears in the dashboard and every file it names is gone."
        )


def test_c2_delete_of_a_referenced_job_is_a_message_not_a_500(client, db, settings):
    """AC: a job a collection depends on cannot just crash the server."""
    job_id, _ = seed_job_with_artifacts(db, settings.output_root)
    client.post(
        "/collections",
        data={"name": "c", "video": [f"{job_id}v1"], "order": f"{job_id}v1"},
        follow_redirects=False,
    )
    response = client.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=False)
    assert response.status_code < 500, (
        f"Deleting a job referenced by a collection returned {response.status_code}. "
        "The user sees a server error page."
    )


def test_c3_delete_without_files_keeps_the_folder(client, db, settings):
    """AC: the documented default keeps artifacts. Verify it really does."""
    job_id, job_dir = seed_job_with_artifacts(db, settings.output_root)
    client.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=False)
    assert job_dir.exists(), "A delete without remove_files erased the folder anyway."
    assert db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone() is None


def test_c4_orphaned_folder_is_discoverable_after_a_row_only_delete(client, db, settings):
    """AC: output that outlives its row must still be reachable or reported.

    Otherwise 'delete' silently leaks gigabytes with no screen that admits it.
    """
    job_id, job_dir = seed_job_with_artifacts(db, settings.output_root)
    client.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=False)
    assert job_dir.exists()

    dashboard = client.get("/").text.lower()
    imports = client.get("/imports").text.lower()
    settings_page = client.get("/settings").text.lower()
    mentioned = any(
        job_id in text or "orphan" in text or "no longer tracked" in text
        for text in (dashboard, imports, settings_page)
    )
    assert mentioned, (
        "After deleting the job row, its folder is still on disk consuming space and "
        "no screen mentions it. The space cannot be reclaimed from inside the app."
    )
