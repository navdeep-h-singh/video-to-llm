"""Probe suite A — trust boundary, CSRF, and the filesystem surface.

Each test states the acceptance criterion it is asserting. Nothing in this file touches the operator's real
output root or the real settings file.
"""

from __future__ import annotations

from pathlib import Path

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
    connection.commit()
    return job_id


# ── A1. Cross-site request forgery ────────────────────────────────────────
# AC: a state-changing POST carrying a foreign Origin must be refused.
# Any web page the user visits can submit a form to 127.0.0.1 without CORS
# preflight, because urlencoded form posts are "simple requests".

CROSS_ORIGIN = {"Origin": "https://evil.example", "Referer": "https://evil.example/x"}


def test_a1_cross_origin_post_cannot_delete_a_job(client, db, tmp_path):
    seed_job(db)
    job_dir = Path(client.app.state.root) if False else None  # noqa: F841
    response = client.post("/jobs/j1/delete", data={"remove_files": "1"}, headers=CROSS_ORIGIN)
    still_there = db.execute("SELECT id FROM jobs WHERE id = 'j1'").fetchone()
    assert still_there is not None, (
        f"CSRF: a cross-origin POST deleted a job (status {response.status_code}). "
        "Any site the user visits can destroy their processed output."
    )


def test_a1_cross_origin_post_cannot_create_a_job(client, db):
    before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    client.post("/sample", headers=CROSS_ORIGIN)
    after = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert after == before, "CSRF: a cross-origin POST started work on this machine."


def test_a1_cross_origin_post_cannot_rename_a_job(client, db):
    seed_job(db)
    client.post("/jobs/j1/rename", data={"name": "owned"}, headers=CROSS_ORIGIN)
    name = db.execute("SELECT name FROM jobs WHERE id = 'j1'").fetchone()
    assert name is None or name["name"] != "owned", "CSRF: cross-origin rename succeeded."


def test_a1_cross_origin_post_cannot_remove_a_stored_key(client):
    response = client.post(
        "/settings/key/remove", data={"provider": "anthropic"}, headers=CROSS_ORIGIN
    )
    assert response.status_code in (400, 403), (
        f"CSRF: cross-origin key removal was accepted ({response.status_code})."
    )


# ── A2. Host header / DNS rebinding ───────────────────────────────────────
# AC: a request whose Host is not loopback must be refused, or a rebound DNS
# name lets a remote page read responses, not merely write.


def test_a2_foreign_host_header_is_refused(client):
    response = client.get("/health", headers={"Host": "evil.example"})
    assert response.status_code in (400, 403, 421), (
        f"DNS rebinding: Host 'evil.example' was served normally ({response.status_code}). "
        "A rebound hostname makes every GET readable cross-origin."
    )


# ── A3. Filesystem enumeration ────────────────────────────────────────────
# AC: /api/browse is a file picker. It should not be a general read primitive
# for anything that can reach the port.


def test_a3_browse_does_not_serve_cross_origin(client):
    response = client.get("/api/browse", headers=CROSS_ORIGIN)
    assert response.status_code in (400, 403), (
        "A cross-origin request enumerated the filesystem via /api/browse."
    )


def test_a3_browse_is_refused_under_a_rebound_hostname(client):
    """The picker may walk this user's disk — that is what a file picker is, and
    a local process could already read it. What must not happen is a *remote*
    page reaching it, which needs either a foreign origin or a hostname rebound
    to loopback. Both are refused."""
    rebound = client.get("/api/browse", headers={"Host": "evil.example"})
    assert rebound.status_code == 421, (
        f"A rebound hostname reached the file picker ({rebound.status_code})."
    )
    cross = client.get("/api/browse", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403, (
        f"A cross-site request reached the file picker ({cross.status_code})."
    )


# ── A4. Path containment on /files ────────────────────────────────────────
# AC: nothing outside the output root is ever served.


@pytest.mark.parametrize(
    "attack",
    [
        "../../../../etc/passwd",
        "..%2f..%2f..%2f..%2fetc%2fpasswd",
        "....//....//etc/passwd",
        "/etc/passwd",
    ],
)
def test_a4_traversal_is_refused(client, attack):
    response = client.get(f"/files/{attack}")
    assert response.status_code in (400, 403, 404), (
        f"Traversal {attack!r} returned {response.status_code}."
    )
    assert b"root:" not in response.content, f"Traversal {attack!r} served /etc/passwd."


def test_a4_symlink_inside_root_pointing_out_is_refused(client, settings, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    link = settings.output_root / "escape.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    response = client.get("/files/escape.txt")
    assert response.status_code in (400, 403, 404), (
        "A symlink inside the output root served a file outside it."
    )


# ── A5. Not-found semantics ───────────────────────────────────────────────
# AC: a missing resource returns 404, so the browser, history, and any script
# can tell "gone" from "here".


def test_a5_missing_job_returns_404(client):
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 404, (
        f"A missing job rendered the not-found page with status {response.status_code}."
    )


def test_a5_missing_collection_returns_404(client):
    response = client.get("/collections/does-not-exist")
    assert response.status_code == 404, f"A missing collection returned {response.status_code}."
