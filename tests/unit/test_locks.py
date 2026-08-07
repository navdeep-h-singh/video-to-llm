"""One worker per output root.

Two workers on one root would interleave artifact writes, double-spend a budget,
and each treat the other's in-flight batches as abandoned. Both guards are
tested independently, because each covers a failure the other does not.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from app.core.db import open_database, utc_now
from app.core.locks import (
    STALE_CLAIM_SECONDS,
    WorkerAlreadyRunningError,
    acquire_claim,
    claim_is_stale,
    heartbeat,
    lock_path,
    read_claim,
    release_claim,
    worker_lock,
)


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    yield connection
    connection.close()


# ── Staleness ─────────────────────────────────────────────────────────────


def test_a_fresh_heartbeat_is_not_stale():
    assert claim_is_stale(utc_now()) is False


def test_an_old_heartbeat_is_stale():
    old = (datetime.now(UTC) - timedelta(seconds=STALE_CLAIM_SECONDS + 30)).isoformat()
    assert claim_is_stale(old) is True


def test_a_heartbeat_just_inside_the_window_is_not_stale():
    recent = (datetime.now(UTC) - timedelta(seconds=STALE_CLAIM_SECONDS - 10)).isoformat()
    assert claim_is_stale(recent) is False


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp", "2026-13-45"])
def test_an_unreadable_heartbeat_is_treated_as_stale(value):
    # The alternative is an output root that nobody can ever claim again.
    assert claim_is_stale(value) is True


def test_a_suspended_laptop_does_not_look_dead():
    # Closing the lid mid-job is normal. The window has to be wide enough that a
    # brief suspend is not mistaken for a crashed worker.
    assert STALE_CLAIM_SECONDS >= 60


# ── Database claim ────────────────────────────────────────────────────────


def test_a_claim_can_be_taken_on_a_free_root(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    row = read_claim(db, tmp_path)
    assert row["worker_id"] == "w1"
    assert row["pid"] == 111


def test_a_second_live_worker_is_refused(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    with pytest.raises(WorkerAlreadyRunningError, match="already owns"):
        acquire_claim(db, tmp_path, worker_id="w2", hostname="host", pid=222)


def test_the_same_worker_may_reclaim_its_own_root(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    assert read_claim(db, tmp_path)["worker_id"] == "w1"


def test_a_stale_claim_is_taken_over(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    stale = (datetime.now(UTC) - timedelta(seconds=STALE_CLAIM_SECONDS + 60)).isoformat()
    db.execute("UPDATE worker_claims SET heartbeat_at = ?", (stale,))

    acquire_claim(db, tmp_path, worker_id="w2", hostname="host", pid=222)
    assert read_claim(db, tmp_path)["worker_id"] == "w2"


def test_different_roots_do_not_contend(db, tmp_path):
    acquire_claim(db, tmp_path / "a", worker_id="w1", hostname="h", pid=1)
    acquire_claim(db, tmp_path / "b", worker_id="w2", hostname="h", pid=2)
    assert read_claim(db, tmp_path / "a")["worker_id"] == "w1"
    assert read_claim(db, tmp_path / "b")["worker_id"] == "w2"


# ── Heartbeat ─────────────────────────────────────────────────────────────


def test_heartbeat_refreshes_the_claim(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    old = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    db.execute("UPDATE worker_claims SET heartbeat_at = ?", (old,))

    assert heartbeat(db, tmp_path, "w1") is True
    assert claim_is_stale(read_claim(db, tmp_path)["heartbeat_at"]) is False


def test_heartbeat_reports_loss_of_ownership(db, tmp_path):
    # A worker that was suspended long enough to be taken over must find out and
    # stop, rather than carry on writing alongside its replacement.
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    db.execute("UPDATE worker_claims SET worker_id = 'w2'")
    assert heartbeat(db, tmp_path, "w1") is False


def test_release_only_affects_our_own_claim(db, tmp_path):
    acquire_claim(db, tmp_path, worker_id="w1", hostname="host", pid=111)
    release_claim(db, tmp_path, "someone-else")
    assert read_claim(db, tmp_path) is not None

    release_claim(db, tmp_path, "w1")
    assert read_claim(db, tmp_path) is None


# ── Combined guard ────────────────────────────────────────────────────────


def test_worker_lock_takes_and_releases_both_guards(db, tmp_path):
    with worker_lock(db, tmp_path) as worker_id:
        assert read_claim(db, tmp_path)["worker_id"] == worker_id
        assert lock_path(tmp_path).exists()
    assert read_claim(db, tmp_path) is None


def test_worker_lock_releases_both_guards_on_exception(db, tmp_path):
    with pytest.raises(RuntimeError):
        with worker_lock(db, tmp_path):
            raise RuntimeError("worker crashed")
    assert read_claim(db, tmp_path) is None


def test_a_second_lock_in_the_same_process_is_refused(db, tmp_path):
    with worker_lock(db, tmp_path):
        with pytest.raises(WorkerAlreadyRunningError):
            with worker_lock(db, tmp_path):
                pass


def test_the_file_lock_excludes_a_separate_process(tmp_path):
    """The guard has to hold across processes — that is the real scenario.

    Two `video-to-llm run-worker` invocations are separate processes, so an
    in-process check would not catch them.
    """
    script = textwrap.dedent(
        """
        import sys, time
        from filelock import FileLock, Timeout
        lock = FileLock(sys.argv[1], timeout=0)
        try:
            lock.acquire()
        except Timeout:
            print("REFUSED")
        else:
            print("ACQUIRED")
            lock.release()
        """
    )
    target = str(lock_path(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)

    from filelock import FileLock

    holder = FileLock(target, timeout=0)
    holder.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, target],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "REFUSED" in result.stdout, result.stdout + result.stderr
    finally:
        holder.release()

    # And once released, another process can take it.
    result = subprocess.run(
        [sys.executable, "-c", script, target], capture_output=True, text=True, timeout=30
    )
    assert "ACQUIRED" in result.stdout
