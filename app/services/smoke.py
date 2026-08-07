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

        _pipeline_checks(connection, work, job_id, steps)
    finally:
        connection.close()


def _pipeline_checks(connection, work: Path, job_id: str, steps: list) -> None:
    """Stages 1-2 on generated media, then a collection built from the result.

    Everything is synthesised here: FFmpeg draws the video from a colour pattern
    and a tone. No personal media, no network, no provider, no credential.
    """
    import shutil

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        steps.append(("pipeline on generated media", True, "skipped - FFmpeg not installed"))
        return

    from app.core.config import Settings
    from app.pipeline.stages import StageContext, run_assembly_stage, run_frames_stage

    settings = Settings().with_output_root(work)
    source = _generate_clip(work / "source" / "smoke.mp4")
    if source is None:
        steps.append(("pipeline on generated media", False, "could not generate a test clip"))
        return

    video_id = new_id()
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, duration_seconds, output_dir, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            job_id,
            str(source),
            "smoke.mp4",
            0,
            "pending",
            4.0,
            f"{job_id}/{video_id}",
            utc_now(),
            utc_now(),
        ),
    )

    context = StageContext(
        connection=connection,
        settings=settings,
        job_id=job_id,
        job_video_id=video_id,
        source_path=source,
        output_dir=work / job_id / video_id,
        interval_ms=2000,
    )

    try:
        frames = run_frames_stage(context, make_api_copies=False)
    except Exception as error:
        steps.append(("frames from generated media", False, redacted_exception_text(error)))
        return
    steps.append(("frames from generated media", frames >= 2, f"{frames} pictures"))

    try:
        assembled = run_assembly_stage(context, display_name="smoke.mp4")
    except Exception as error:
        steps.append(("assembled document", False, redacted_exception_text(error)))
        return
    steps.append(("assembled document", assembled.is_file(), assembled.name))

    # A collection built from that output - local, free, and no provider call.
    try:
        from app.collections.build import FULL_FILENAME, build_collection
        from app.collections.model import (
            assess_source,
            create_collection,
            load_collection,
            set_sources,
        )

        connection.execute("UPDATE job_videos SET status = 'completed' WHERE id = ?", (video_id,))
        collection_id = create_collection(connection, name="Smoke collection")
        source_entry = assess_source(connection, video_id, work)
        set_sources(connection, collection_id, [source_entry] if source_entry else [])
        collection = load_collection(connection, collection_id)
        if collection is None:
            steps.append(("collection built from that output", False, "collection not found"))
            return
        result = build_collection(connection, collection, output_root=work)
    except Exception as error:
        steps.append(("collection built from that output", False, redacted_exception_text(error)))
        return

    built = (result.directory / FULL_FILENAME).is_file()
    steps.append(
        ("collection built from that output", built, f"about {result.total_tokens:,} tokens")
    )


def _generate_clip(destination: Path) -> Path | None:
    """A few seconds of colour pattern and tone, drawn by FFmpeg itself."""
    import shutil
    import subprocess

    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            "-t",
            "4",
            str(destination),
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    return destination if result.returncode == 0 and destination.is_file() else None
