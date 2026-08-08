"""Probe suite B — input validation, state machine, and rendering integrity.

A failure here is a regression. Nothing here touches the operator's real output root.
"""

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


def seed_job(connection, job_id="j1", name="Course", status="completed"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, name, status, "/out", 2000, "none", utc_now(), utc_now()),
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
    return job_id


# ── B1. Malformed query parameters must not 500 or 422 into raw JSON ──────
# AC: a hand-edited or stale URL shows a human page, never a framework error.


@pytest.mark.parametrize(
    "url",
    [
        "/jobs/j1/frames?page_no=-5",
        "/jobs/j1/frames?page_no=999999",
        "/jobs/j1/frames?page_no=abc",
        "/jobs/j1/review?frame=-1",
        "/jobs/j1/review?frame=999999",
        "/jobs/j1/review?frame=abc",
        "/jobs/j1/review?video=../../etc",
        "/?sort=nonsense",
        "/?state=nonsense",
    ],
)
def test_b1_bad_query_params_render_a_human_page(client, db, url):
    seed_job(db)
    response = client.get(url)
    assert response.status_code < 500, f"{url} returned {response.status_code}"
    assert response.status_code != 422, (
        f"{url} returned a raw framework validation error (422), not a page."
    )


# ── B2. Job creation validation ───────────────────────────────────────────
# AC: every rejection explains itself on the form; nothing crashes.


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "paths": "/tmp/x.mp4"},
        {"name": "   ", "paths": "/tmp/x.mp4"},
        {"name": "ok", "paths": ""},
        {"name": "ok", "paths": "/nonexistent/file.mp4"},
        {"name": "x" * 5000, "paths": "/tmp/x.mp4"},
        {"name": "ok", "paths": "/tmp/x.mp4", "interval": "0"},
        {"name": "ok", "paths": "/tmp/x.mp4", "interval": "-100"},
        {"name": "ok", "paths": "/tmp/x.mp4", "interval": "99999999"},
        {"name": "ok", "paths": "/tmp/x.mp4", "interval": "abc"},
        {"name": "ok", "paths": "/tmp/x.mp4", "provider": "bogus_provider"},
    ],
)
def test_b2_job_creation_never_crashes(client, db, payload):
    response = client.post("/jobs", data=payload, follow_redirects=False)
    assert response.status_code < 500, f"{payload} returned {response.status_code}"


def test_b2_absurd_interval_is_rejected_not_stored(client, db):
    """AC: an interval outside the documented 0.5 to 10s range is refused.

    A 0ms interval asks FFmpeg for infinite frames; a negative one is nonsense.
    Neither should reach a job row.
    """
    client.post(
        "/jobs",
        data={"name": "bad", "paths": "/tmp/x.mp4", "interval": "0"},
        follow_redirects=False,
    )
    rows = db.execute("SELECT frame_interval_ms FROM jobs").fetchall()
    bad = [r["frame_interval_ms"] for r in rows if r["frame_interval_ms"] <= 0]
    assert not bad, f"A job was stored with a nonsensical frame interval: {bad}"


# ── B3. Rename ────────────────────────────────────────────────────────────
# AC: an empty rename is refused visibly, not swallowed.


def test_b3_empty_rename_tells_the_user(client, db):
    seed_job(db, name="Original")
    response = client.post("/jobs/j1/rename", data={"name": "   "}, follow_redirects=True)
    name = db.execute("SELECT name FROM jobs WHERE id='j1'").fetchone()["name"]
    assert name == "Original"
    body = response.text.lower()
    assert "name" in body and ("cannot be empty" in body or "needs a name" in body), (
        "An empty rename was silently ignored — the dialog closes and nothing changes, "
        "with no message explaining why."
    )


# ── B4. Escaping ──────────────────────────────────────────────────────────
# AC: a job name is data. It must never become markup or script.


def test_b4_job_name_is_escaped_everywhere(client, db):
    payload = "<script>alert(1)</script>"
    seed_job(db, name=payload)
    for url in ("/", "/jobs/j1", "/jobs/j1/outputs", "/collections/new"):
        response = client.get(url)
        assert payload not in response.text, f"Unescaped job name rendered at {url}"


def test_b4_job_name_cannot_break_out_of_the_title(client, db):
    seed_job(db, name="</title><script>alert(1)</script>")
    response = client.get("/jobs/j1")
    head = response.text.split("</head>")[0]
    assert "<script>" not in head, "A job name injected a script tag into <head>."


# ── B5. State machine ─────────────────────────────────────────────────────
# AC: an action that cannot apply says so; it does not silently no-op.


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_b5_invalid_transition_is_reported(client, db, action):
    seed_job(db, status="completed")
    response = client.post(f"/jobs/j1/{action}", follow_redirects=True)
    assert response.status_code < 500
    body = response.text.lower()
    assert "cannot" in body or "already" in body or "finished" in body, (
        f"'{action}' on a completed job redirected with no explanation — the button "
        "appears to work and nothing happens."
    )


def test_b5_actions_on_a_missing_job_are_reported(client):
    for action in ("pause", "resume", "cancel", "rename", "delete"):
        response = client.post(f"/jobs/ghost/{action}", data={"name": "x"}, follow_redirects=False)
        assert response.status_code < 500, f"{action} on a missing job: {response.status_code}"
        assert response.status_code == 404 or "notfound" in response.text.lower(), (
            f"'{action}' on a nonexistent job redirected as though it had worked."
        )


# ── B6. Collection budget arithmetic ──────────────────────────────────────
# AC: a reserve larger than the limit is refused, not turned into a negative
# budget that packs zero (or every) source.


def test_b6_reserve_larger_than_limit_is_refused(client, db):
    seed_job(db)
    response = client.post(
        "/collections",
        data={
            "name": "c",
            "mode": "full",
            "token_limit": "1000",
            "reserve_tokens": "999999",
            "video": ["j1v1"],
            "order": "j1v1",
        },
        follow_redirects=True,
    )
    assert response.status_code < 500
    body = response.text.lower()
    assert "reserve" in body or "limit" in body, (
        "A reserve larger than the token limit was accepted, producing a negative budget."
    )


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "999999999999999999999999"])
def test_b6_bad_token_limit_is_a_page_not_a_422(client, db, bad):
    seed_job(db)
    response = client.post(
        "/collections",
        data={"name": "c", "token_limit": bad, "video": ["j1v1"], "order": "j1v1"},
        follow_redirects=False,
    )
    assert response.status_code != 422, (
        f"token_limit={bad!r} returned a raw 422 JSON error instead of a form message."
    )
    assert response.status_code < 500


def test_b6_estimate_endpoint_survives_bad_input(client, db):
    seed_job(db)
    response = client.post(
        "/api/collections/estimate",
        data={"token_limit": "abc", "reserve_tokens": "-5", "video": ["j1v1"]},
    )
    assert response.status_code < 500, f"estimate returned {response.status_code}"


# ── B7. Deleting a job that a collection depends on ───────────────────────
# AC: a collection whose sources vanished must still render, and must say so.


def test_b7_collection_survives_its_source_being_deleted(client, db):
    seed_job(db)
    client.post(
        "/collections",
        data={"name": "c", "video": ["j1v1"], "order": "j1v1"},
        follow_redirects=False,
    )
    row = db.execute("SELECT id FROM collections").fetchone()
    assert row is not None, "collection was not created"
    client.post("/jobs/j1/delete", data={}, follow_redirects=False)

    response = client.get(f"/collections/{row['id']}")
    assert response.status_code < 500, (
        f"A collection whose source job was deleted returned {response.status_code}."
    )
    assert client.get("/collections").status_code < 500


# ── B8. Deleting a running job ────────────────────────────────────────────
# AC: deleting a job the worker is mid-way through must be refused or must
# stop the work first; it must not pull the files out from under the worker.


def test_b8_deleting_a_running_job_is_refused(client, db):
    seed_job(db, status="transcribing")
    response = client.post("/jobs/j1/delete", data={"remove_files": "1"}, follow_redirects=False)
    still = db.execute("SELECT id FROM jobs WHERE id='j1'").fetchone()
    assert still is not None, (
        "A job being processed right now was deleted, files and all, while the worker "
        f"was still writing into that folder (status {response.status_code})."
    )
