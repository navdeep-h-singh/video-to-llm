"""Command-line entry point.

The whole pipeline is reachable from here without ever opening the browser. The
interface controls and observes jobs; it does not own them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import Settings, default_output_root, load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _resolve_settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    root = getattr(args, "output_root", None)
    if root:
        settings = settings.with_output_root(Path(root))
    elif settings.output_root is None:
        settings = settings.with_output_root(default_output_root())
    if getattr(args, "port", None):
        from dataclasses import replace

        settings = replace(settings, port=args.port)
    return settings


# ── Commands ──────────────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> int:
    from app.services.doctor import format_report, run_doctor

    settings = _resolve_settings(args)
    report = run_doctor(settings)
    print(format_report(report))
    return 0 if report.ready else 1


def cmd_status(args: argparse.Namespace) -> int:
    from app.core.db import database_path, open_database
    from app.core.locks import claim_is_stale

    settings = _resolve_settings(args)
    root = settings.output_root
    assert root is not None

    if not database_path(root).exists():
        print("No jobs yet. Run `video-to-llm start` to begin.")
        return 0

    connection = open_database(root, migrate_on_open=False)
    try:
        claim = connection.execute(
            "SELECT * FROM worker_claims WHERE output_root = ?", (str(root),)
        ).fetchone()
        if claim is None:
            print("Background processing: not running")
        elif claim_is_stale(claim["heartbeat_at"]):
            print(
                f"Background processing: stopped without cleaning up "
                f"(last seen {claim['heartbeat_at']})"
            )
        else:
            print(f"Background processing: running (pid {claim['pid']})")

        rows = connection.execute(
            "SELECT id, name, status, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        if not rows:
            print("\nNo jobs yet.")
            return 0

        print(f"\n{'JOB':<34} {'STATUS':<22} UPDATED")
        for row in rows:
            name = row["name"][:32]
            print(f"{name:<34} {row['status']:<22} {row['updated_at']}")
    finally:
        connection.close()
    return 0


def cmd_run_worker(args: argparse.Namespace) -> int:
    from app.worker.runner import run_worker

    settings = _resolve_settings(args)
    return run_worker(settings, once=getattr(args, "once", False))


def cmd_start_ui(args: argparse.Namespace) -> int:
    settings = _resolve_settings(args)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run `uv sync`.", file=sys.stderr)
        return 1

    from app.web.app import create_app

    print(f"Interface at {settings.base_url}")
    print("Reachable only from this computer. Close the browser freely — the worker keeps going.")
    # The host is taken from the settings property, which is the BIND_HOST
    # constant. It is never read from user input.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start the worker in the background, then the interface in the foreground."""
    import threading

    from app.worker.runner import run_worker

    settings = _resolve_settings(args)

    worker_thread = threading.Thread(
        target=run_worker, args=(settings,), kwargs={"once": False}, daemon=True
    )
    worker_thread.start()
    return cmd_start_ui(args)


def cmd_smoke_test(args: argparse.Namespace) -> int:
    from app.services.smoke import run_smoke_test

    settings = _resolve_settings(args)
    return run_smoke_test(settings)


def cmd_import(args: argparse.Namespace) -> int:
    from app.services.importer import import_processed_output

    settings = _resolve_settings(args)
    return import_processed_output(settings, Path(args.path))


# ── Parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-to-llm",
        description="Turn local videos into timestamped, reviewable, LLM-ready evidence. "
        "Runs entirely on this computer.",
    )
    parser.add_argument("--output-root", help="Folder that holds everything this makes")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, or ERROR")

    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Start the interface and the worker together")
    start.add_argument("--port", type=int, default=None)
    start.set_defaults(func=cmd_start)

    start_ui = sub.add_parser("start-ui", help="Start only the interface")
    start_ui.add_argument("--port", type=int, default=None)
    start_ui.set_defaults(func=cmd_start_ui)

    worker = sub.add_parser("run-worker", help="Start only the background worker")
    worker.add_argument("--once", action="store_true", help="Process what is waiting, then exit")
    worker.set_defaults(func=cmd_run_worker)

    sub.add_parser("doctor", help="Check this computer is ready").set_defaults(func=cmd_doctor)
    sub.add_parser("status", help="Show jobs and worker health").set_defaults(func=cmd_status)
    sub.add_parser("smoke-test", help="End-to-end run on generated media, no network").set_defaults(
        func=cmd_smoke_test
    )

    importer = sub.add_parser("import", help="Bring previously processed output under management")
    importer.add_argument("path", help="Folder holding previously processed output")
    importer.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(level=args.log_level or settings.log_level)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nStopped. Finished work is saved.", file=sys.stderr)
        return 130
    except Exception as error:
        from app.core.redaction import redacted_exception_text

        logger.error("Command failed: %s", redacted_exception_text(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
