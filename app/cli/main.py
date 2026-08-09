"""Command-line entry point.

The whole pipeline is reachable from here without ever opening the browser:
`process` creates a job and runs it to completion, `show` resolves a timestamp
back to the frame it names. The interface controls and observes jobs; it does
not own them, and it is no longer the only thing that can start one.

This claim was false for the first three sessions of this project — the README
and this docstring both made it while job creation existed solely as a web
form — which is why `tests/unit/test_cli.py` now asserts every documented
command actually parses.
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


#: Export formats, named here so building the parser costs no service imports.
#: Duplicating the list is only safe because `test_cli.py` pins it against
#: `app.services.export.EXPORTERS` — the same guard the settings template's
#: provider labels carry, and for the same reason: a choice the parser offers
#: and the exporter cannot render is a control that lies.
EXPORT_FORMATS = ("json", "jsonl", "md", "srt", "vtt")

#: What `--describe` accepts, mapped to the provider names the pipeline uses.
#: "local" rather than "ollama_local" because the flag names the *place* the
#: work happens, which is the distinction the user is making. The service names
#: match the settings file so a value copied from one to the other still works.
DESCRIBE_CHOICES = {
    "none": "none",
    "local": "ollama_local",
    "ollama_local": "ollama_local",
    "anthropic": "anthropic",
    "google": "google",
    "openai": "openai",
    "openai_compatible": "openai_compatible",
    "anthropic_compatible": "anthropic_compatible",
}


def cmd_process(args: argparse.Namespace) -> int:
    """Create a job for the given videos and run it to completion."""
    from app.core.config import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS
    from app.services.headless import process_videos

    settings = _resolve_settings(args)

    paths = [Path(p).expanduser() for p in args.videos]
    provider = DESCRIBE_CHOICES[args.describe]

    interval_ms: int | None = None
    if args.interval is not None:
        if not MIN_INTERVAL_SECONDS <= args.interval <= MAX_INTERVAL_SECONDS:
            print(
                f"--interval must be between {MIN_INTERVAL_SECONDS:g} and "
                f"{MAX_INTERVAL_SECONDS:g} seconds.",
                file=sys.stderr,
            )
            return 2
        interval_ms = round(args.interval * 1000)

    # A model is required for every service except the ones that have no model
    # to choose. Saying so here beats creating the job and failing inside the
    # stage, which leaves a half-run job behind for the user to clean up.
    model_id = (args.model or "").strip()
    if provider not in {"none"} and not model_id:
        model_id = settings.visual_analysis.model_for(provider)
    if provider != "none" and not model_id:
        print(
            f"No model is set for {provider}. Pass --model, or set one in the "
            "interface under Settings.",
            file=sys.stderr,
        )
        return 2

    result = process_videos(
        settings,
        paths=paths,
        name=args.name,
        interval_ms=interval_ms,
        provider=provider,
        model_id=model_id,
    )

    for warning in result.warnings:
        print(f"note: {warning}", file=sys.stderr)

    if result.job_id is None:
        for problem in result.problems:
            print(problem, file=sys.stderr)
        return 1

    for problem in result.problems:
        print(problem, file=sys.stderr)

    if not result.documents:
        print(f"Finished as '{result.status}' with no assembled document.", file=sys.stderr)
        return 1

    if result.status == "completed_with_gaps":
        print(
            "Finished with gaps — some pictures went undescribed. The transcript "
            "and the frames are complete.",
            file=sys.stderr,
        )

    if args.format:
        from app.services.export import ExportError, export_video_dir

        # The per-video folder holds the structured artifacts an export is built
        # from. A master document spans several of them and has no single
        # transcript, so exports are written per video and named accordingly.
        for video_dir in sorted({d.parent for d in result.documents}):
            for fmt in dict.fromkeys(args.format):
                try:
                    print(export_video_dir(video_dir, fmt))
                except ExportError as error:
                    print(f"Could not write {fmt}: {error}", file=sys.stderr)

    for document in result.documents:
        print(document)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Resolve a timestamp back to what was happening at it."""
    from app.services.citation import CitationError, format_citation, resolve_citation

    settings = _resolve_settings(args)
    try:
        citation = resolve_citation(settings, args.job, args.timestamp, window=args.window)
    except CitationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(format_citation(citation))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Re-render an already-processed job into other formats."""
    from app.core.db import database_path, open_database
    from app.services.citation import CitationError, find_job
    from app.services.citation import video_dirs as video_dirs_for
    from app.services.export import ExportError, export_video_dir

    settings = _resolve_settings(args)
    root = settings.output_root
    if root is None or not database_path(Path(root)).exists():
        print("No jobs yet. Run `video-to-llm process <video>` first.", file=sys.stderr)
        return 1

    connection = open_database(Path(root), migrate_on_open=False)
    try:
        job = find_job(connection, args.job)
        video_dirs = video_dirs_for(connection, Path(root), job)
    except CitationError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        connection.close()

    if not video_dirs:
        print(f"'{args.job}' has no output on disk yet.", file=sys.stderr)
        return 1

    wrote = False
    for video_dir in video_dirs:
        for fmt in dict.fromkeys(args.format):
            try:
                print(export_video_dir(video_dir, fmt))
                wrote = True
            except ExportError as error:
                print(f"{video_dir.name}: {error}", file=sys.stderr)
    return 0 if wrote else 1


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

    process = sub.add_parser(
        "process",
        help="Process one or more videos now and print where the document went",
        description="Create a job for these videos, run it to completion, and print the "
        "path of each assembled document. Nothing is uploaded unless --describe names "
        "a service.",
    )
    process.add_argument("videos", nargs="+", help="Paths to the video files, in order")
    process.add_argument("--name", default=None, help="Job name (default: the first filename)")
    process.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Seconds between pictures, 0.5 to 10 (default: the interval in your settings)",
    )
    process.add_argument(
        "--describe",
        choices=sorted(DESCRIBE_CHOICES),
        default="none",
        help="Describe the pictures with a vision model. 'local' uses your own Ollama; "
        "everything else sends pictures to that service (default: none)",
    )
    process.add_argument(
        "--model", default=None, help="Model to describe with (default: the one in settings)"
    )
    process.add_argument(
        "--format",
        action="append",
        default=None,
        choices=sorted(EXPORT_FORMATS),
        metavar="FORMAT",
        help=f"Also write this format beside the document ({', '.join(sorted(EXPORT_FORMATS))}). "
        "Repeatable.",
    )
    process.set_defaults(func=cmd_process)

    export = sub.add_parser(
        "export",
        help="Write another format from a video that has already been processed",
        description="Re-render an already-processed video into another format. Reads the "
        "structured artifacts, so it never re-extracts, re-transcribes, or re-describes.",
    )
    export.add_argument("job", help="Job name or identifier")
    export.add_argument(
        "--format",
        action="append",
        required=True,
        choices=sorted(EXPORT_FORMATS),
        metavar="FORMAT",
        help=f"One of: {', '.join(sorted(EXPORT_FORMATS))}. Repeatable.",
    )
    export.set_defaults(func=cmd_export)

    show = sub.add_parser(
        "show",
        help="Show what was happening at a timestamp, and the picture from it",
        description="Resolve a citation. Given a job and a timestamp, print the "
        "surrounding transcript and the path to the frame taken at that moment.",
    )
    show.add_argument("job", help="Job name or identifier")
    show.add_argument("timestamp", help="HH:MM:SS, MM:SS, or seconds")
    show.add_argument(
        "--window",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="How much either side to include (default: 15)",
    )
    show.set_defaults(func=cmd_show)

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
