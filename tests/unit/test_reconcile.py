"""Startup reconciliation.

The property under test: recovery never destroys valid work and never repeats
work that already completed — especially work that cost money.
"""

from __future__ import annotations

import pytest

from app.core.artifacts import TEMP_PREFIX, register_artifact, write_text
from app.core.db import open_database, utc_now
from app.worker.reconcile import reconcile


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Session review", "analyzing", str(tmp_path), utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v1", "j1", "/a.mp4", "a.mp4", 0, "analyzing", utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("s1", "v1", "visual", "running", utc_now(), utc_now()),
    )
    yield connection
    connection.close()


def _add_batch(connection, batch_id, index, status, cost=None):
    connection.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, cost_usd, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            "s1",
            index,
            index * 20,
            index * 20 + 19,
            20,
            status,
            cost,
            utc_now(),
            utc_now(),
        ),
    )


# ── Interrupted writes ────────────────────────────────────────────────────


def test_interrupted_writes_are_cleaned_up(db, tmp_path):
    (tmp_path / f"{TEMP_PREFIX}half-written").write_bytes(b"partial")
    report = reconcile(db, tmp_path)
    assert len(report.temp_files_removed) == 1
    assert list(tmp_path.glob(f"{TEMP_PREFIX}*")) == []


def test_real_artifacts_survive_reconciliation(db, tmp_path):
    keeper = tmp_path / "assembled.txt"
    write_text(keeper, "finished work")
    register_artifact(db, output_root=tmp_path, path=keeper, kind="assembled", job_id="j1")

    reconcile(db, tmp_path)
    assert keeper.read_text(encoding="utf-8") == "finished work"
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


# ── Missing artifacts ─────────────────────────────────────────────────────


def test_a_row_pointing_at_a_deleted_file_is_removed(db, tmp_path):
    target = tmp_path / "gone.txt"
    write_text(target, "x")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")
    target.unlink()

    report = reconcile(db, tmp_path)
    assert report.missing_artifacts == ["gone.txt"]
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


# ── Batches: the money-critical case ──────────────────────────────────────


def test_a_completed_batch_is_never_reset(db, tmp_path):
    # This is the one that costs real money if it goes wrong. A batch is marked
    # completed only after its artifact is durably persisted, so re-running it
    # would re-send frames to a provider and be billed a second time.
    _add_batch(db, "b1", 0, "completed", cost=0.042)
    reconcile(db, tmp_path)

    row = db.execute("SELECT status, cost_usd FROM batches WHERE id = 'b1'").fetchone()
    assert row["status"] == "completed"
    assert row["cost_usd"] == 0.042


def test_a_running_batch_is_returned_to_pending(db, tmp_path):
    _add_batch(db, "b2", 1, "running")
    report = reconcile(db, tmp_path)
    assert "b2" in report.batches_reset
    assert db.execute("SELECT status FROM batches WHERE id='b2'").fetchone()["status"] == "pending"


@pytest.mark.parametrize("status", ["failed", "skipped", "cancelled"])
def test_terminal_batch_states_are_left_alone(db, tmp_path, status):
    _add_batch(db, "b3", 2, status)
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM batches WHERE id='b3'").fetchone()["status"] == status


def test_mixed_batches_reset_only_the_running_one(db, tmp_path):
    _add_batch(db, "done", 0, "completed", cost=0.1)
    _add_batch(db, "live", 1, "running")
    _add_batch(db, "todo", 2, "pending")
    reconcile(db, tmp_path)

    states = {row["id"]: row["status"] for row in db.execute("SELECT id, status FROM batches")}
    assert states == {"done": "completed", "live": "pending", "todo": "pending"}


# ── Stage runs, videos, jobs ──────────────────────────────────────────────


def test_a_running_stage_run_is_reset(db, tmp_path):
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM stage_runs WHERE id='s1'").fetchone()["status"] == (
        "pending"
    )


def test_a_completed_stage_run_is_not_reset(db, tmp_path):
    db.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("s2", "v1", "frames", "completed", utc_now(), utc_now()),
    )
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM stage_runs WHERE id='s2'").fetchone()["status"] == (
        "completed"
    )


def test_an_interrupted_video_becomes_pending(db, tmp_path):
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM job_videos WHERE id='v1'").fetchone()["status"] == (
        "pending"
    )


def test_an_interrupted_job_becomes_ready_not_needs_attention(db, tmp_path):
    # An interruption is the ordinary consequence of closing a laptop. Marking
    # it as needing attention would train the user to ignore that status.
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "ready"


def test_recovery_is_recorded_in_plain_language(db, tmp_path):
    reconcile(db, tmp_path)
    row = db.execute(
        "SELECT message, kind FROM events WHERE job_id='j1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "recovered"
    assert "kept" in row["message"].lower()


@pytest.mark.parametrize("status", ["completed", "completed_with_gaps", "cancelled", "paused"])
def test_settled_jobs_are_not_disturbed(db, tmp_path, status):
    db.execute("UPDATE jobs SET status = ? WHERE id='j1'", (status,))
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == status


def test_a_paused_job_stays_paused(db, tmp_path):
    # Pause is a deliberate user decision; recovery must not override it.
    db.execute("UPDATE jobs SET status='paused' WHERE id='j1'")
    db.execute("UPDATE job_videos SET status='paused' WHERE id='v1'")
    reconcile(db, tmp_path)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "paused"
    assert db.execute("SELECT status FROM job_videos WHERE id='v1'").fetchone()["status"] == (
        "paused"
    )


# ── Idempotence and reporting ─────────────────────────────────────────────


def test_reconciliation_is_idempotent(db, tmp_path):
    first = reconcile(db, tmp_path)
    assert first.changed is True

    second = reconcile(db, tmp_path)
    assert second.changed is False
    assert second.summary() == "State and artifacts agree; nothing to repair."


def test_a_clean_database_reports_no_changes(tmp_path):
    connection = open_database(tmp_path / "fresh")
    report = reconcile(connection, tmp_path / "fresh")
    assert report.changed is False
    connection.close()


def test_summary_describes_what_happened(db, tmp_path):
    (tmp_path / f"{TEMP_PREFIX}x").write_bytes(b"partial")
    _add_batch(db, "b9", 5, "running")
    summary = reconcile(db, tmp_path).summary()
    assert "interrupted write" in summary
    assert "batch" in summary
    assert summary.endswith(".")


def test_reconciliation_survives_a_missing_output_root(db, tmp_path):
    # An unmounted external drive must not crash the worker at startup.
    report = reconcile(db, tmp_path / "not-mounted")
    assert report.temp_files_removed == []
