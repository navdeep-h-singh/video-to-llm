"""Probe suite F — remaining robustness and honesty checks."""

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


def seed(connection, job_id="j1", name="Course", status="completed"):
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


# ── F1. The 500 page must not promise something untrue ────────────────────


def test_f1_error_page_does_not_claim_files_are_safe(client, db, settings):
    """AC: the generic error page is shown after a delete that already erased
    the artifacts. It must not assert that files are unaffected."""
    import re
    from pathlib import Path

    raw = Path("app/web/templates/error.html").read_text()
    # Assert on what is rendered, not on the source: a Jinja comment explaining
    # why the promise was removed is not shown to anyone.
    template = re.sub(r"\{#.*?#\}", "", raw, flags=re.S).lower()
    assert "unaffected" not in template and "are safe" not in template, (
        "The error page tells the user 'your jobs and files are unaffected'. "
        "The delete route reaches this page *after* removing the job folder, so "
        "the one time it is most likely to be shown, it is false."
    )


# ── F2. Names at the edges ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "x" * 2000,
        "日本語のビデオ講座",
        "emoji 🎬🔥 job",
        "  leading and trailing  ",
        "line\nbreak",
        "tab\there",
        "../../etc/passwd",
        "%00null",
    ],
)
def test_f2_unusual_names_render_safely(client, db, name):
    seed(db, name=name)
    for url in ("/", "/jobs/j1"):
        response = client.get(url)
        assert response.status_code < 500, f"{name!r} broke {url}"


def test_f3_very_long_name_does_not_break_the_title(client, db):
    seed(db, name="x" * 2000)
    response = client.get("/jobs/j1")
    title = response.text.split("<title>")[1].split("</title>")[0]
    assert len(title) < 300, (
        f"A 2000-character job name produced a {len(title)}-character browser tab title."
    )


# ── F4. Contact sheet paging ──────────────────────────────────────────────


@pytest.mark.parametrize("page_no", ["-1", "0", "9999"])
def test_f4_contact_sheet_paging(client, db, page_no):
    seed(db)
    response = client.get(f"/jobs/j1/frames?page_no={page_no}")
    assert response.status_code < 500, f"page_no={page_no} → {response.status_code}"


# ── F5. Empty install ─────────────────────────────────────────────────────


def test_f5_every_screen_renders_on_a_fresh_install(tmp_path):
    """AC: a first-run user must never meet a stack trace."""
    fresh = Settings().with_output_root(tmp_path / "brand-new")
    with TestClient(create_app(fresh), base_url=LOOPBACK_BASE_URL) as c:
        for url in (
            "/",
            "/launch",
            "/jobs/new",
            "/imports",
            "/settings",
            "/collections",
            "/collections/new",
        ):
            response = c.get(url)
            assert response.status_code < 500, f"{url} → {response.status_code} on a fresh install"


# ── F6. Health endpoint leaks nothing ─────────────────────────────────────


def test_f6_health_does_not_leak_paths(client):
    body = client.get("/health").text
    assert "/Users" not in body and "/home" not in body, f"/health leaks a filesystem path: {body}"


# ── F7. Progress endpoint on a missing job ────────────────────────────────


def test_f7_progress_survives_a_bad_job_id(client):
    for bad in ("ghost", "../../etc", "%00", "x" * 500):
        response = client.get("/api/progress", params={"job_id": bad})
        assert response.status_code < 500, f"progress?job_id={bad!r} → {response.status_code}"


# ── F8. Reveal is constrained ─────────────────────────────────────────────


def test_f8_reveal_refuses_paths_outside_the_root(client):
    response = client.post("/reveal", data={"relative_path": "../../../../etc"})
    assert response.json().get("ok") is False, (
        "The 'show in file manager' control opened a folder outside the output root."
    )
