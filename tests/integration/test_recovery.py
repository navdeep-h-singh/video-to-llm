"""Recovery from the failures that actually happen.

Not hypothetical ones. A laptop lid closes mid-job, an external drive is
unplugged, a process is killed, two workers race for the same folder. Each of
these is tested by causing it, not by simulating a flag.

The property throughout: **valid work survives, and nothing expensive is
repeated.**
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.core.artifacts import TEMP_PREFIX, register_artifact, write_text
from app.core.config import Settings
from app.core.db import database_path, new_id, open_database, utc_now
from app.core.locks import WorkerAlreadyRunningError, worker_lock
from app.core.logging import configure_logging
from app.worker.reconcile import reconcile
from app.worker.runner import run_worker


@pytest.fixture(autouse=True)
def _quiet():
    configure_logging(level="CRITICAL", force=True)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


def seed_running_job(connection, root, *, job_status="analyzing"):
    """A job that looks like it was interrupted mid-flight."""
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("j1", "Interrupted", job_status, str(root), 2000, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, output_dir, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("v1", "j1", "/src/a.mp4", "a.mp4", 0, "analyzing", "j1/v1", utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("s1", "v1", "visual", "running", utc_now(), utc_now()),
    )


# ── A process killed mid-write ────────────────────────────────────────────


def test_a_half_written_file_never_becomes_an_artifact(settings, db, tmp_path):
    """Kill a process between the temp write and the rename.

    Done with a real subprocess and a real SIGKILL, because the whole point of
    the atomic-write design is surviving a death the process cannot handle.
    """
    root = settings.output_root
    root.mkdir(parents=True, exist_ok=True)
    target = root / "assembled.txt"
    write_text(target, "the good previous version")

    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path

        sys.path.insert(0, sys.argv[1])
        from app.core.artifacts import atomic_write

        with atomic_write(Path(sys.argv[2])) as handle:
            handle.write(b"a half written replacement")
            handle.flush()
            os.kill(os.getpid(), 9)   # die before the rename
        """
    )
    repo_root = str(Path(__file__).resolve().parents[2])
    subprocess.run(
        [sys.executable, "-c", script, repo_root, str(target)],
        capture_output=True,
        timeout=30,
        check=False,
    )

    # The previous good version is untouched...
    assert target.read_text(encoding="utf-8") == "the good previous version"
    # ...and the abandoned temp file is recognisable and unreferenced.
    leftovers = list(root.glob(f"{TEMP_PREFIX}*"))
    assert leftovers, "expected an abandoned temp file to prove the kill landed mid-write"

    report = reconcile(db, root)
    assert len(report.temp_files_removed) == len(leftovers)
    assert target.read_text(encoding="utf-8") == "the good previous version"


def test_reconciliation_clears_interrupted_state_and_keeps_finished_work(settings, db, tmp_path):
    root = settings.output_root
    seed_running_job(db, root)

    # One batch finished and was paid for; one was in flight.
    db.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, cost_usd, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'completed',?,?,?)",
        (new_id(), "s1", 0, 0, 19, 20, 0.042, utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'running',?,?)",
        (new_id(), "s1", 1, 20, 39, 20, utc_now(), utc_now()),
    )

    reconcile(db, root)

    states = {
        row["batch_index"]: (row["status"], row["cost_usd"])
        for row in db.execute("SELECT batch_index, status, cost_usd FROM batches")
    }
    assert states[0] == ("completed", 0.042), "paid work must survive a crash"
    assert states[1][0] == "pending", "in-flight work must be retried"
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "ready"


def test_recovery_is_explained_to_the_user(settings, db):
    seed_running_job(db, settings.output_root)
    reconcile(db, settings.output_root)

    row = db.execute(
        "SELECT message FROM events WHERE kind='recovered' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "kept" in row["message"].lower()
    # Plain language, not an error code.
    assert "traceback" not in row["message"].lower()


# ── A laptop that went to sleep ───────────────────────────────────────────


def test_a_suspended_worker_that_lost_its_claim_stops_rather_than_writing(settings, db):
    """Two workers writing the same artifacts is the failure to avoid."""
    from app.worker.runner import Worker

    with worker_lock(db, settings.output_root):
        # This worker's claim was taken over while it was suspended.
        stranded = Worker(settings, db, worker_id="the-suspended-one")
        assert stranded.beat() is False
        assert stranded.stopping is True


def test_a_stale_claim_is_taken_over_on_the_next_start(settings, db):
    from datetime import UTC, datetime, timedelta

    from app.core.locks import STALE_CLAIM_SECONDS, acquire_claim, read_claim

    acquire_claim(db, settings.output_root, worker_id="dead", hostname="h", pid=1)
    stale = (datetime.now(UTC) - timedelta(seconds=STALE_CLAIM_SECONDS + 60)).isoformat()
    db.execute("UPDATE worker_claims SET heartbeat_at = ?", (stale,))

    assert run_worker(settings, once=True) == 0
    assert read_claim(db, settings.output_root) is None, "the claim should be released"


# ── An unplugged drive ────────────────────────────────────────────────────


def test_a_missing_output_root_does_not_crash_reconciliation(settings, db, tmp_path):
    # An external drive that is no longer mounted must not take the worker down.
    report = reconcile(db, tmp_path / "not-mounted")
    assert report.temp_files_removed == []


def test_artifacts_that_vanished_are_de_registered(settings, db):
    root = settings.output_root
    seed_running_job(db, root)

    target = root / "j1" / "v1" / "assembled.txt"
    write_text(target, "content")
    register_artifact(db, output_root=root, path=target, kind="assembled", job_id="j1")

    target.unlink()  # the drive came back with the file gone
    report = reconcile(db, root)

    assert "j1/v1/assembled.txt" in report.missing_artifacts
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


# ── Two workers ───────────────────────────────────────────────────────────


def test_a_second_worker_is_refused_across_processes(settings, db, tmp_path):
    """Two `run-worker` invocations are separate processes.

    An in-process guard would not catch them, so the guard has to be a real OS
    lock and this has to be a real subprocess.
    """
    from app.core.locks import lock_path

    settings.output_root.mkdir(parents=True, exist_ok=True)
    target = str(lock_path(settings.output_root))

    script = textwrap.dedent(
        """
        import sys
        from filelock import FileLock, Timeout
        try:
            FileLock(sys.argv[1], timeout=0).acquire()
        except Timeout:
            print("REFUSED")
        else:
            print("ACQUIRED")
        """
    )

    with worker_lock(db, settings.output_root):
        result = subprocess.run(
            [sys.executable, "-c", script, target],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert "REFUSED" in result.stdout

    # Once released, another process may take it.
    result = subprocess.run(
        [sys.executable, "-c", script, target],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "ACQUIRED" in result.stdout


def test_the_second_worker_reports_the_conflict_clearly(settings, db):
    with worker_lock(db, settings.output_root):
        assert run_worker(settings, once=True) == 1


def test_worker_lock_errors_name_the_remedy(settings, db):
    with worker_lock(db, settings.output_root):
        with pytest.raises(WorkerAlreadyRunningError) as excinfo:
            with worker_lock(db, settings.output_root):
                pass
    assert "stop the other one" in str(excinfo.value).lower()


# ── A busy database ───────────────────────────────────────────────────────


def test_a_concurrent_writer_does_not_lose_data(settings, db):
    """WAL plus a busy timeout is what lets the UI read while the worker writes."""
    second = open_database(settings.output_root, migrate_on_open=False)
    try:
        db.execute(
            "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("j1", "First", "draft", "/out", utc_now(), utc_now()),
        )
        second.execute(
            "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("j2", "Second", "draft", "/out", utc_now(), utc_now()),
        )
        assert second.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        second.close()


def test_a_reader_sees_the_writers_committed_work(settings, db):
    reader = open_database(settings.output_root, migrate_on_open=False)
    try:
        db.execute(
            "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("j1", "Written", "draft", "/out", utc_now(), utc_now()),
        )
        assert reader.execute("SELECT name FROM jobs").fetchone()["name"] == "Written"
    finally:
        reader.close()


# ── Repeatability ─────────────────────────────────────────────────────────


def test_reconciliation_can_run_many_times_safely(settings, db):
    seed_running_job(db, settings.output_root)

    first = reconcile(db, settings.output_root)
    assert first.changed is True

    for _ in range(5):
        assert reconcile(db, settings.output_root).changed is False


def test_a_paused_job_is_not_resurrected_by_recovery(settings, db):
    # Pause is a deliberate decision. Recovery must not override it.
    seed_running_job(db, settings.output_root, job_status="paused")
    db.execute("UPDATE job_videos SET status='paused'")

    reconcile(db, settings.output_root)

    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "paused"
    assert db.execute("SELECT status FROM job_videos WHERE id='v1'").fetchone()["status"] == (
        "paused"
    )


def test_a_cancelled_job_stays_cancelled(settings, db):
    seed_running_job(db, settings.output_root, job_status="cancelled")
    reconcile(db, settings.output_root)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "cancelled"


def test_the_database_survives_being_reopened(settings, db, tmp_path):
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Persisted", "completed", "/out", utc_now(), utc_now()),
    )
    db.close()

    reopened = open_database(settings.output_root)
    try:
        assert reopened.execute("SELECT name FROM jobs").fetchone()["name"] == "Persisted"
    finally:
        reopened.close()
    # Reopen again so the fixture teardown has a live connection.
    settings.output_root  # noqa: B018


def test_migrations_are_not_reapplied_on_reopen(settings, tmp_path):
    from app.core.db import applied_versions, migrate

    first = open_database(tmp_path / "fresh")
    applied_first = applied_versions(first)
    first.close()

    second = open_database(tmp_path / "fresh")
    try:
        assert migrate(second) == []
        assert applied_versions(second) == applied_first
    finally:
        second.close()


def test_a_corrupt_database_file_is_reported_not_silently_replaced(tmp_path):
    """Overwriting a corrupt database would destroy the record of paid work."""
    root = tmp_path / "corrupt"
    root.mkdir()
    database_path(root).write_bytes(b"this is definitely not a sqlite file" * 40)

    with pytest.raises(sqlite3.DatabaseError):
        connection = open_database(root)
        connection.execute("SELECT COUNT(*) FROM jobs").fetchone()


def test_provenance_survives_a_restart(settings, db, tmp_path):
    root = settings.output_root
    seed_running_job(db, root)
    target = root / "j1" / "v1" / "provenance.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "frame_interval_ms": 2000}), "utf-8")
    register_artifact(db, output_root=root, path=target, kind="provenance", job_id="j1")

    reconcile(db, root)

    assert target.is_file()
    assert json.loads(target.read_text("utf-8"))["frame_interval_ms"] == 2000
