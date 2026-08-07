"""No-network synthetic smoke test.

Exercises the durability machinery end to end against generated data: create the
output root, migrate, take the worker lock, write an artifact atomically,
register it, simulate an interrupted write, reconcile, and verify.

Deliberately touches no network, no provider, no personal media, and no
credential store. It runs in CI on all three platforms, so it must pass on a
machine with nothing installed but Python and this project.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.artifacts import (
    TEMP_PREFIX,
    register_artifact,
    sha256_file,
    verify_artifact,
    write_json,
    write_text,
)
from app.core.config import Settings
from app.core.db import new_id, open_database, schema_version, utc_now
from app.core.locks import read_claim, worker_lock
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.worker.reconcile import reconcile

logger = get_logger(__name__)


def run_smoke_test(settings: Settings, *, root: Path | None = None) -> int:
    """Run the checks. Returns 0 on success, 1 on the first failure.

    Uses a throwaway directory rather than the real output root so the test can
    never disturb someone's actual work.
    """
    steps: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory(prefix="vtl-smoke-") as temp_dir:
        work = Path(root) if root else Path(temp_dir)
        try:
            _run_checks(work, steps)
        except Exception as error:
            steps.append(("unexpected failure", False, redacted_exception_text(error)))

    width = max(len(name) for name, _, _ in steps)
    for name, passed, detail in steps:
        marker = "OK  " if passed else "FAIL"
        line = f"[{marker}] {name.ljust(width)}"
        print(f"{line}  {detail}" if detail else line)

    failures = [name for name, passed, _ in steps if not passed]
    if failures:
        print(f"\nSmoke test FAILED: {', '.join(failures)}")
        return 1

    print(f"\nSmoke test passed — {len(steps)} checks, no network used.")
    return 0


def _run_checks(work: Path, steps: list[tuple[str, bool, str]]) -> None:
    connection = open_database(work)
    try:
        version = schema_version(connection)
        steps.append(("database and migrations", version >= 1, f"schema v{version}"))

        # Localhost boundary
        from app.core.config import BIND_HOST, is_loopback_host

        steps.append(("localhost binding", is_loopback_host(BIND_HOST), f"bound to {BIND_HOST}"))

        # Worker lock, taken and released
        with worker_lock(connection, work) as worker_id:
            held = read_claim(connection, work) is not None
            steps.append(("worker lock", held, f"claimed by {worker_id[:8]}"))
        released = read_claim(connection, work) is None
        steps.append(("worker lock released", released, ""))

        # Atomic write and checksum
        job_id = new_id()
        connection.execute(
            "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (job_id, "Smoke test", "draft", str(work), utc_now(), utc_now()),
        )
        target = work / job_id / "assembled.txt"
        digest = write_text(target, "00:00:00  synthetic line\n00:00:02  [nobody speaking]\n")
        steps.append(("atomic write", digest == sha256_file(target), f"sha256 {digest[:12]}…"))

        register_artifact(
            connection, output_root=work, path=target, kind="assembled", job_id=job_id
        )
        registered = verify_artifact(connection, work, f"{job_id}/assembled.txt")
        steps.append(("artifact registered and verified", registered, ""))

        # JSON manifests are redacted on the way to disk
        manifest = work / job_id / "provenance.json"
        write_json(manifest, {"provider": "none", "api_key": "must-not-be-written"})
        clean = "must-not-be-written" not in manifest.read_text(encoding="utf-8")
        steps.append(("secrets redacted in manifests", clean, ""))

        # An interrupted write is cleaned up, and real output survives
        (work / job_id / f"{TEMP_PREFIX}interrupted").write_bytes(b"partial")
        report = reconcile(connection, work)
        cleaned = len(report.temp_files_removed) == 1 and target.is_file()
        steps.append(("recovery after interruption", cleaned, report.summary()))

        # Reconciliation is safe to repeat
        steps.append(("recovery is idempotent", not reconcile(connection, work).changed, ""))
    finally:
        connection.close()
