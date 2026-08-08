"""Probe suite D — configuration scoping, concurrency, and double submission."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, load_settings, save_settings
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


def seed(connection, job_id="j1", status="completed"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, "Course", status, "/out", 2000, "none", utc_now(), utc_now()),
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


# ── D1. Orphaned output after a row-only delete ───────────────────────────


def test_d1_orphaned_folder_is_reported_somewhere(client, db, settings):
    """AC: gigabytes that outlive their row must be visible to the user."""
    job_id = "zqx9distinctive"
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, "Course", "completed", "/out", 2000, "none", utc_now(), utc_now()),
    )
    db.commit()
    folder = settings.output_root / job_id
    folder.mkdir(parents=True)
    (folder / "assembled.txt").write_text("x" * 1000)

    client.post(f"/jobs/{job_id}/delete", data={}, follow_redirects=False)
    assert folder.exists()

    pages = [client.get(u).text.lower() for u in ("/", "/imports", "/settings")]
    assert any(job_id in text or "orphan" in text for text in pages), (
        "Deleting a job leaves its output folder on disk and no screen mentions it. "
        "The only way to reclaim the space is the file manager."
    )


# ── D2. Configuration scoping ─────────────────────────────────────────────


def test_d2_settings_file_is_not_inside_the_installed_package(tmp_path):
    """AC: user configuration lives in a user-writable location.

    A path under the installation directory breaks a read-only install, breaks
    two instances pointed at different roots, and is lost on upgrade.
    """
    from app.core.config import repo_root, settings_file

    target = settings_file()
    assert repo_root() not in target.parents, (
        f"Settings are written to {target}, inside the application directory. "
        "Two output roots share one settings file, a read-only install cannot save, "
        "and an upgrade that replaces the folder discards the user's configuration."
    )


def test_d2_settings_round_trip_preserves_unknown_keys(tmp_path):
    """AC: saving one section must not discard configuration it does not model."""
    target = tmp_path / "settings.toml"
    target.write_text('[general]\noutput_root = "/tmp/x"\n\n[experimental]\nfuture_flag = true\n')
    loaded = load_settings(path=target)
    save_settings(loaded, path=target)
    rendered = target.read_text()
    assert "future_flag" in rendered, (
        "A settings key the current version does not model is silently dropped on save. "
        "Downgrading, or a newer key written by hand, is lost the first time any "
        "settings form is submitted."
    )


# ── D3. Double submission ─────────────────────────────────────────────────


def test_d3_double_sample_press_does_not_make_two_jobs(client, db):
    """AC: pressing a slow button twice must not queue the work twice.

    The sample takes ~2.4s to draw; the button gives no immediate feedback, so a
    second press is the expected human behaviour.
    """
    client.post("/sample", follow_redirects=False)
    client.post("/sample", follow_redirects=False)
    count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count <= 1, f"Two presses of 'Try it with a generated sample' created {count} jobs."


def test_d3_double_collection_submit_does_not_duplicate(client, db):
    seed(db)
    payload = {"name": "c", "video": ["j1v1"], "order": "j1v1"}
    client.post("/collections", data=payload, follow_redirects=False)
    client.post("/collections", data=payload, follow_redirects=False)
    count = db.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
    assert count <= 1, f"Submitting the collection form twice created {count} collections."


# ── D6. Actions that cost money or time on a busy job ─────────────────────


def test_d6_describe_again_on_a_running_job_is_refused(client, db):
    """AC: queueing description work on a job already mid-flight must be refused,
    or the same frames are described twice — twice the money on a paid provider."""
    seed(db, status="transcribing")
    response = client.post("/jobs/j1/describe", data={"video_id": "j1v1"}, follow_redirects=True)
    assert response.status_code < 500
    body = response.text.lower()
    assert "still" in body or "wait" in body or "cannot" in body or "running" in body, (
        "'Describe again' was accepted on a job that is still being processed, with "
        "no warning that the work is already under way."
    )


def test_d7_rerun_on_a_running_job_is_refused(client, db):
    seed(db, status="analyzing")
    response = client.post(
        "/jobs/j1/rerun",
        data={"video_id": "j1v1", "scope": "all"},
        follow_redirects=True,
    )
    assert response.status_code < 500
    body = response.text.lower()
    assert "still" in body or "wait" in body or "cannot" in body or "running" in body, (
        "A rerun was accepted while the job was still analyzing."
    )
