"""One worker per output root.

Two workers sharing an output root would interleave writes to the same
artifacts, double-spend a provider budget, and each mark the other's batches as
abandoned. The guard is deliberately doubled:

*A filesystem lock* — an exclusive OS-level lock on a file in the output root.
This is the primary guard. It is released by the kernel when the holding process
dies, however it dies, so a crash never leaves the root permanently locked.

*A database claim* — a row naming the worker, its host, its PID, and a
heartbeat. This is what the interface reads to report worker health, and it
catches the case the file lock cannot: an output root on a network filesystem
where advisory locking is unreliable or silently ignored.

The file lock alone would be enough on a local disk. The claim alone would be
enough if processes always exited cleanly. Together they cover both.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock, Timeout

from app.core.db import new_id, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

LOCK_FILENAME = "worker.lock"

#: A claim whose heartbeat is older than this is considered abandoned. Set well
#: above the heartbeat interval so a briefly suspended laptop — closing the lid
#: mid-job is normal — is not mistaken for a dead worker.
STALE_CLAIM_SECONDS = 120

HEARTBEAT_INTERVAL_SECONDS = 15


class WorkerAlreadyRunningError(RuntimeError):
    """Raised when another worker already owns this output root."""


def lock_path(output_root: Path) -> Path:
    return Path(output_root) / LOCK_FILENAME


def worker_identity() -> tuple[str, str, int]:
    """Return ``(worker_id, hostname, pid)`` for this process."""
    return new_id(), socket.gethostname(), os.getpid()


# ── Database claim ────────────────────────────────────────────────────────


def _parse(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def claim_is_stale(heartbeat_at: str | None, *, now: datetime | None = None) -> bool:
    parsed = _parse(heartbeat_at)
    if parsed is None:
        # An unparseable heartbeat is treated as stale rather than as a
        # permanent lock — the alternative is an output root nobody can use.
        return True
    reference = now or datetime.now(UTC)
    return reference - parsed > timedelta(seconds=STALE_CLAIM_SECONDS)


def read_claim(connection: sqlite3.Connection, output_root: Path) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM worker_claims WHERE output_root = ?", (str(output_root),)
    ).fetchone()


def acquire_claim(
    connection: sqlite3.Connection,
    output_root: Path,
    *,
    worker_id: str,
    hostname: str,
    pid: int,
) -> None:
    """Take the database claim, or refuse if a live worker holds it."""
    root = str(output_root)
    now = utc_now()

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT * FROM worker_claims WHERE output_root = ?", (root,)
        ).fetchone()

        if existing is not None and not claim_is_stale(existing["heartbeat_at"]):
            if existing["worker_id"] != worker_id:
                raise WorkerAlreadyRunningError(
                    f"another worker already owns this output root "
                    f"(host {existing['hostname']}, pid {existing['pid']}, "
                    f"last seen {existing['heartbeat_at']}). "
                    "Only one worker may run per output root."
                )

        if existing is not None:
            logger.info(
                "Taking over a stale worker claim from pid %s (last seen %s)",
                existing["pid"],
                existing["heartbeat_at"],
            )

        connection.execute(
            """
            INSERT INTO worker_claims (output_root, worker_id, hostname, pid,
                                       heartbeat_at, claimed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (output_root) DO UPDATE SET
                worker_id    = excluded.worker_id,
                hostname     = excluded.hostname,
                pid          = excluded.pid,
                heartbeat_at = excluded.heartbeat_at,
                claimed_at   = excluded.claimed_at
            """,
            (root, worker_id, hostname, pid, now, now),
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def heartbeat(connection: sqlite3.Connection, output_root: Path, worker_id: str) -> bool:
    """Refresh the claim. False when this worker no longer owns it.

    A false return means another worker took over — normally because this one
    was suspended long enough to look dead. The caller must stop rather than
    keep writing.
    """
    cursor = connection.execute(
        "UPDATE worker_claims SET heartbeat_at = ? WHERE output_root = ? AND worker_id = ?",
        (utc_now(), str(output_root), worker_id),
    )
    return cursor.rowcount > 0


def release_claim(connection: sqlite3.Connection, output_root: Path, worker_id: str) -> None:
    """Give up the claim, but only if we still hold it."""
    connection.execute(
        "DELETE FROM worker_claims WHERE output_root = ? AND worker_id = ?",
        (str(output_root), worker_id),
    )


# ── Combined guard ────────────────────────────────────────────────────────


@contextmanager
def worker_lock(
    connection: sqlite3.Connection,
    output_root: Path,
    *,
    timeout_seconds: float = 0.0,
) -> Iterator[str]:
    """Hold both guards for the duration of the block, yielding the worker id.

    The file lock is taken first: it is the cheaper check and the one that is
    correct without any cleanup after a crash.
    """
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    worker_id, hostname, pid = worker_identity()
    file_lock = FileLock(str(lock_path(output_root)), timeout=timeout_seconds)

    try:
        file_lock.acquire()
    except Timeout as error:
        raise WorkerAlreadyRunningError(
            f"another worker already holds the lock on {output_root}. "
            "Only one worker may run per output root — stop the other one first."
        ) from error

    try:
        acquire_claim(connection, output_root, worker_id=worker_id, hostname=hostname, pid=pid)
        logger.info("Worker %s claimed the output root (pid %s)", worker_id[:8], pid)
        yield worker_id
    finally:
        try:
            release_claim(connection, output_root, worker_id)
        except sqlite3.Error as error:
            # Never let claim cleanup mask the original failure, and never block
            # release of the file lock — that would strand the output root.
            logger.warning("Could not release the worker claim: %s", error)
        file_lock.release()
        logger.info("Worker %s released the output root", worker_id[:8])
