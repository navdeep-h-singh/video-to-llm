"""Atomic artifact writing.

Artifacts are the authoritative record of evidence, so a file that exists must
be complete. The sequence is always: write to a temporary sibling, flush, fsync,
atomically rename into place, then fsync the containing directory.

Why a *sibling* and not the system temp directory: `os.replace` is only atomic
within a filesystem. A temp file on another volume turns the rename into a
copy-then-delete, which is exactly the non-atomic write this module exists to
avoid — and output roots on external drives are a normal setup here.

Why fsync the directory: on POSIX, the rename itself is atomic but the directory
entry is not durable until the directory is synced. Without it, a power loss can
leave the file's contents on disk with no name pointing at them.

A partially written temp file is always recoverable: it has a distinctive prefix,
it is never registered in the database, and startup reconciliation sweeps it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.db import new_id, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Marks an in-flight write. Anything with this prefix is unreferenced by
#: definition and safe for reconciliation to delete.
TEMP_PREFIX = ".vtl-tmp-"

CHECKSUM_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHECKSUM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(directory: Path) -> None:
    """Make a directory entry durable. A no-op where the platform disallows it."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        # Windows cannot open a directory this way. NTFS metadata journalling
        # covers the rename, so this is a real no-op rather than a silent gap.
        return
    try:
        os.fsync(fd)
    except OSError:
        logger.debug("Directory fsync unsupported for %s", directory)
    finally:
        os.close(fd)


@contextmanager
def atomic_write(
    destination: Path,
    *,
    mode: str = "wb",
    encoding: str | None = None,
) -> Iterator[Any]:
    """Yield a handle whose contents land at *destination* atomically, or not at all.

    On any exception the temporary file is removed and *destination* is left
    exactly as it was — a failed write never truncates a previous good artifact.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=TEMP_PREFIX, dir=str(destination.parent), suffix=destination.suffix
    )
    temp_path = Path(temp_name)

    try:
        with os.fdopen(fd, mode, encoding=encoding) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())

        # Path.replace is os.replace underneath: atomic within a filesystem,
        # and it overwrites an existing destination rather than failing.
        temp_path.replace(destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_bytes(destination: Path, data: bytes) -> str:
    """Write *data* atomically. Returns the SHA-256 of what was written."""
    with atomic_write(destination) as handle:
        handle.write(data)
    return sha256_bytes(data)


def write_text(destination: Path, text: str) -> str:
    return write_bytes(destination, text.encode("utf-8"))


def write_json(destination: Path, payload: Any, *, indent: int = 2) -> str:
    """Write JSON atomically, with secrets stripped first.

    Redaction is applied here rather than at each call site because manifests and
    provenance records are assembled from provider responses and configuration
    dictionaries, and one forgetful caller is all it takes.
    """
    from app.core.redaction import redact_structure

    serialised = json.dumps(
        redact_structure(payload), indent=indent, ensure_ascii=False, sort_keys=False
    )
    return write_text(destination, serialised + "\n")


def cleanup_temp_files(root: Path) -> list[Path]:
    """Remove every in-flight temp file under *root*. Returns what was removed.

    Called at startup. A temp file can only exist because a process died between
    creating it and renaming it, so it is unreferenced by construction and there
    is nothing to preserve.
    """
    removed: list[Path] = []
    root = Path(root)
    if not root.exists():
        return removed

    for path in root.rglob(f"{TEMP_PREFIX}*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as error:
            logger.warning("Could not remove stale temp file %s: %s", path, error)
        else:
            removed.append(path)

    if removed:
        logger.info("Removed %d stale temp file(s) from an interrupted write", len(removed))
    return removed


# ── Registration ──────────────────────────────────────────────────────────


def register_artifact(
    connection: sqlite3.Connection,
    *,
    output_root: Path,
    path: Path,
    kind: str,
    job_id: str | None = None,
    job_video_id: str | None = None,
    collection_build_id: str | None = None,
    sha256: str | None = None,
) -> str:
    """Record an artifact that is already durably on disk.

    Call this *after* the write, never before: a row pointing at a file that does
    not exist is worse than a file no row knows about, because reconciliation can
    find the second but has to treat the first as data loss.

    Paths are stored relative to the output root so the whole tree can be moved
    without invalidating every row, and so no absolute path — which would carry
    the user's directory layout — is ever written to the database.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"refusing to register {path} — the artifact does not exist on disk"
        )

    relative = path.relative_to(Path(output_root)).as_posix()
    checksum = sha256 if sha256 is not None else (sha256_file(path) if path.is_file() else None)
    size = path.stat().st_size if path.is_file() else None
    artifact_id = new_id()

    connection.execute(
        """
        INSERT INTO artifacts
            (id, job_id, job_video_id, collection_build_id, kind,
             relative_path, size_bytes, sha256, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (relative_path) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            sha256     = excluded.sha256,
            created_at = excluded.created_at
        """,
        (
            artifact_id,
            job_id,
            job_video_id,
            collection_build_id,
            kind,
            relative,
            size,
            checksum,
            utc_now(),
        ),
    )
    return artifact_id


def verify_artifact(connection: sqlite3.Connection, output_root: Path, relative_path: str) -> bool:
    """True when the registered artifact exists and still matches its checksum."""
    row = connection.execute(
        "SELECT sha256 FROM artifacts WHERE relative_path = ?", (relative_path,)
    ).fetchone()
    if row is None:
        return False

    path = Path(output_root) / relative_path
    if not path.is_file():
        return False
    if row["sha256"] is None:
        return True
    return sha256_file(path) == row["sha256"]
