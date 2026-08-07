"""The localhost interface.

Controls and observes jobs; does not own them. Closing the browser never stops
work, and every screen reads the same database the worker writes to.

Everything rendered here comes from real state. There is no placeholder data: a
screen with nothing to show says so plainly rather than displaying a plausible
example that the user might mistake for their own work.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import Settings, assert_loopback
from app.core.db import database_path, open_database
from app.core.locks import claim_is_stale
from app.core.logging import get_logger
from app.web import status as status_module

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class NavItem:
    label: str
    href: str
    count: str = ""
    current: bool = False


@dataclass
class NavGroup:
    title: str
    items: list[NavItem]


def create_app(settings: Settings) -> FastAPI:
    # Asserted at construction rather than trusted. The boundary is the one
    # property of this application that must never quietly change.
    assert_loopback(settings.host, context="server bind host")

    app = FastAPI(
        title="Video to LLM",
        description="Runs only on this computer.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # ── Shared context ────────────────────────────────────────────────────

    def connect() -> sqlite3.Connection | None:
        root = settings.output_root
        if root is None or not database_path(root).exists():
            return None
        return open_database(root, migrate_on_open=False)

    def worker_state(connection: sqlite3.Connection | None) -> status_module.StatusPresentation:
        if connection is None or settings.output_root is None:
            return status_module.StatusPresentation(
                "stopped", "Not started", "status-ready", "hollow square"
            )
        row = connection.execute(
            "SELECT * FROM worker_claims WHERE output_root = ?",
            (str(settings.output_root),),
        ).fetchone()
        if row is None:
            return status_module.StatusPresentation(
                "stopped", "Not running", "status-ready", "hollow square"
            )
        if claim_is_stale(row["heartbeat_at"]):
            return status_module.StatusPresentation(
                "stale", "Stopped unexpectedly", "status-attention", "turned square"
            )
        return status_module.StatusPresentation(
            "running", "Running", "status-running", "filled square"
        )

    def disk_label() -> str:
        root = settings.output_root
        if root is None:
            return "No folder chosen"
        try:
            root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
        except OSError:
            return "Space unknown"
        return f"{free / 1024**3:.0f} GB free"

    def counts(connection: sqlite3.Connection | None) -> dict[str, str]:
        if connection is None:
            return {"jobs": "", "collections": "", "imports": ""}
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        collections = connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        imports = connection.execute(
            "SELECT COUNT(*) FROM job_videos WHERE imported_from IS NOT NULL"
            " AND imported_from != ''"
        ).fetchone()[0]
        return {
            "jobs": str(jobs) if jobs else "",
            "collections": str(collections) if collections else "",
            "imports": str(imports) if imports else "",
        }

    def nav(current: str, connection: sqlite3.Connection | None) -> list[NavGroup]:
        found = counts(connection)
        return [
            NavGroup(
                "Videos",
                [
                    NavItem("Dashboard", "/", found["jobs"], current == "dashboard"),
                    NavItem("New job", "/jobs/new", "", current == "newjob"),
                    NavItem(
                        "Bring in earlier work",
                        "/imports",
                        found["imports"],
                        current == "imports",
                    ),
                ],
            ),
            NavGroup(
                "Collections",
                [
                    NavItem(
                        "Collections",
                        "/collections",
                        found["collections"],
                        current == "collections",
                    ),
                    NavItem("New collection", "/collections/new", "", current == "newcollection"),
                ],
            ),
            NavGroup(
                "This computer",
                [
                    NavItem("Settings & checks", "/settings", "", current == "settings"),
                    NavItem("First-run check", "/launch", "", current == "launch"),
                ],
            ),
        ]

    def page(request: Request, template: str, screen: str, **context: Any) -> HTMLResponse:
        connection = connect()
        try:
            base = {
                "nav_groups": nav(screen, connection),
                "worker": worker_state(connection),
                "disk_label": disk_label(),
                "settings": settings,
                "status": status_module,
            }
            base.update(context)
            return templates.TemplateResponse(request=request, name=template, context=base)
        finally:
            if connection is not None:
                connection.close()

    # ── Screens ───────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "bound_to": settings.host,
                "output_root_set": settings.output_root is not None,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        # A machine that has never been set up goes to the readiness screen
        # rather than an empty dashboard that explains nothing.
        if settings.output_root is None:
            return RedirectResponse("/launch", status_code=303)

        connection = connect()
        jobs: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        try:
            if connection is not None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()
                for row in rows:
                    videos = connection.execute(
                        "SELECT COUNT(*) AS n, COALESCE(SUM(duration_seconds), 0) AS total"
                        " FROM job_videos WHERE job_id = ? AND is_active_version = 1",
                        (row["id"],),
                    ).fetchone()
                    entry = {
                        "id": row["id"],
                        "name": row["name"],
                        "status": status_module.present(row["status"]),
                        "raw_status": row["status"],
                        "videos": videos["n"],
                        "length": status_module.format_duration(videos["total"]),
                        "updated": status_module.format_relative(row["updated_at"]),
                    }
                    jobs.append(entry)
                    if active is None and status_module.is_running(row["status"]):
                        active = entry
        finally:
            if connection is not None:
                connection.close()

        return page(request, "dashboard.html", "dashboard", jobs=jobs, active=active)

    @app.get("/launch", response_class=HTMLResponse)
    def launch(request: Request) -> HTMLResponse:
        from app.services.doctor import run_doctor

        report = run_doctor(settings)
        return page(request, "launch.html", "launch", report=report)

    @app.get("/jobs/new", response_class=HTMLResponse)
    def new_job(request: Request) -> HTMLResponse:
        return page(request, "newjob.html", "newjob")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str) -> Response:
        connection = connect()
        try:
            if connection is None:
                return RedirectResponse("/", status_code=303)

            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job")

            videos = [
                {
                    "id": row["id"],
                    "number": row["sequence"] + 1,
                    "name": row["display_name"],
                    "status": status_module.present(row["status"]),
                    "length": status_module.format_duration(row["duration_seconds"]),
                    "frames": row["frame_count"] or 0,
                    "stages": _stage_progress(connection, row["id"]),
                    "error": row["error_message"],
                }
                for row in connection.execute(
                    "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                    " ORDER BY sequence",
                    (job_id,),
                ).fetchall()
            ]

            events = [
                {
                    "time": (row["created_at"] or "")[11:19],
                    "text": row["message"],
                    "level": row["level"],
                }
                for row in connection.execute(
                    "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 40",
                    (job_id,),
                ).fetchall()
            ]
        finally:
            if connection is not None:
                connection.close()

        return page(
            request,
            "job.html",
            "dashboard",
            job=job,
            job_status=status_module.present(job["status"]),
            videos=videos,
            events=events,
        )

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request) -> HTMLResponse:
        connection = connect()
        rows: list[dict[str, Any]] = []
        try:
            if connection is not None:
                rows = [
                    {
                        "name": row["display_name"],
                        "version": row["version"],
                        "status": status_module.present(row["status"]),
                        "from": row["imported_from"],
                        "when": status_module.format_relative(row["created_at"]),
                    }
                    for row in connection.execute(
                        "SELECT * FROM job_videos WHERE imported_from IS NOT NULL"
                        " AND imported_from != '' ORDER BY created_at DESC"
                    ).fetchall()
                ]
        finally:
            if connection is not None:
                connection.close()
        return page(request, "imports.html", "imports", imports=rows)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_screen(request: Request) -> HTMLResponse:
        from app.credentials.store import ENV_VARS, credential_status
        from app.services.doctor import run_doctor

        # Presence only. A stored key is never sent to the browser, not even
        # partially masked — a few revealed characters still narrow a search.
        credentials = [
            {"provider": name, "status": credential_status(name)}
            for name in ("anthropic", "google", "openai", "openai_compatible")
        ]
        return page(
            request,
            "settings.html",
            "settings",
            report=run_doctor(settings),
            credentials=credentials,
            env_vars=ENV_VARS,
        )

    @app.get("/collections", response_class=HTMLResponse)
    def collections_screen(request: Request) -> HTMLResponse:
        from app.collections.model import list_collections

        connection = connect()
        found: list[Any] = []
        try:
            if connection is not None:
                found = list_collections(connection)
        finally:
            if connection is not None:
                connection.close()
        return page(request, "collections.html", "collections", collections=found)

    @app.get("/collections/new", response_class=HTMLResponse)
    def new_collection(request: Request) -> HTMLResponse:
        from app.collections.model import available_sources

        connection = connect()
        sources: list[Any] = []
        try:
            if connection is not None and settings.output_root is not None:
                sources = available_sources(connection, settings.output_root)
        finally:
            if connection is not None:
                connection.close()
        return page(request, "newcollection.html", "newcollection", sources=sources)

    @app.get("/collections/{collection_id}", response_class=HTMLResponse)
    def collection_detail(request: Request, collection_id: str) -> HTMLResponse:
        from app.collections.model import load_collection

        connection = connect()
        collection = None
        builds: list[Any] = []
        try:
            if connection is not None:
                collection = load_collection(connection, collection_id)
                if collection is not None:
                    builds = connection.execute(
                        "SELECT * FROM collection_builds WHERE collection_id = ?"
                        " ORDER BY collection_version DESC",
                        (collection_id,),
                    ).fetchall()
        finally:
            if connection is not None:
                connection.close()

        if collection is None:
            return page(request, "notfound.html", "collections", what="collection")
        return page(
            request,
            "collection.html",
            "collections",
            collection=collection,
            builds=builds,
        )

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    def review(request: Request, job_id: str) -> HTMLResponse:
        connection = connect()
        videos: list[Any] = []
        try:
            if connection is not None:
                videos = connection.execute(
                    "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                    " ORDER BY sequence",
                    (job_id,),
                ).fetchall()
        finally:
            if connection is not None:
                connection.close()
        return page(request, "review.html", "dashboard", job_id=job_id, videos=videos)

    @app.get("/jobs/{job_id}/outputs", response_class=HTMLResponse)
    def outputs(request: Request, job_id: str) -> HTMLResponse:
        connection = connect()
        files: list[dict[str, Any]] = []
        try:
            if connection is not None:
                files = [
                    {
                        "path": row["relative_path"],
                        "kind": row["kind"],
                        "size": status_module.format_bytes(row["size_bytes"]),
                    }
                    for row in connection.execute(
                        "SELECT * FROM artifacts WHERE job_id = ? ORDER BY relative_path",
                        (job_id,),
                    ).fetchall()
                ]
        finally:
            if connection is not None:
                connection.close()
        return page(request, "outputs.html", "dashboard", job_id=job_id, files=files)

    # ── Actions ───────────────────────────────────────────────────────────

    @app.post("/jobs")
    def create_job_route(
        request: Request,
        name: str = Form(""),
        paths: str = Form(""),
        interval: str = Form("2000"),
        provider: str = Form("none"),
    ) -> Response:
        from app.services.jobs import create_job, parse_paths

        connection = connect()
        if connection is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            interval_ms = None if interval == "custom" else int(interval)
        except ValueError:
            interval_ms = None

        # "external" is the design's grouping for "a service you have an
        # account with". The specific service is chosen in Settings, so an
        # unconfigured choice becomes none rather than a broken job.
        resolved = provider
        if provider == "external":
            resolved = (
                settings.visual_analysis.provider
                if settings.visual_analysis.provider not in {"none", "ollama_local"}
                else "none"
            )

        try:
            result = create_job(
                connection,
                settings,
                name=name,
                paths=parse_paths(paths),
                interval_ms=interval_ms,
                provider=resolved,
                model_id=settings.visual_analysis.model_id,
            )
            if result.ok:
                return RedirectResponse(f"/jobs/{result.job_id}", status_code=303)
            return page(
                request,
                "newjob.html",
                "newjob",
                problems=result.problems,
                submitted_name=name,
                submitted_paths=paths,
            )
        finally:
            connection.close()

    @app.post("/jobs/{job_id}/pause")
    def pause_route(job_id: str) -> Response:
        from app.services.jobs import pause_job

        connection = connect()
        if connection is not None:
            try:
                pause_job(connection, job_id)
            finally:
                connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/resume")
    def resume_route(job_id: str) -> Response:
        from app.services.jobs import resume_job

        connection = connect()
        if connection is not None:
            try:
                resume_job(connection, job_id)
            finally:
                connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_route(job_id: str) -> Response:
        from app.services.jobs import cancel_job

        connection = connect()
        if connection is not None:
            try:
                cancel_job(connection, job_id)
            finally:
                connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/collections")
    def create_collection_route(
        request: Request,
        name: str = Form(""),
        mode: str = Form("full"),
        token_limit: int = Form(200000),
        reserve_tokens: int = Form(20000),
        target_model_label: str = Form(""),
        allow_video_split: str = Form(""),
        video: list[str] = Form(default=[]),
    ) -> Response:
        from app.collections.build import build_collection
        from app.collections.model import (
            assess_source,
            create_collection,
            load_collection,
            set_sources,
        )

        connection = connect()
        if connection is None or settings.output_root is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            chosen = [v for v in video if v]
            if not name.strip() or not chosen:
                from app.collections.model import available_sources

                return page(
                    request,
                    "newcollection.html",
                    "newcollection",
                    sources=available_sources(connection, settings.output_root),
                    problems=["Give the collection a name and choose at least one video."],
                )

            collection_id = create_collection(
                connection,
                name=name,
                mode=mode,
                token_limit=token_limit,
                reserve_tokens=reserve_tokens,
                target_model_label=target_model_label,
                allow_video_split=bool(allow_video_split),
            )
            sources = [
                assess_source(connection, video_id, settings.output_root) for video_id in chosen
            ]
            set_sources(connection, collection_id, [s for s in sources if s])

            collection = load_collection(connection, collection_id)
            if collection is not None:
                # Building is local, free, and takes seconds, so it happens
                # immediately rather than behind another confirmation.
                build_collection(connection, collection, output_root=settings.output_root)

            return RedirectResponse(f"/collections/{collection_id}", status_code=303)
        finally:
            connection.close()

    return app


def _stage_progress(connection: sqlite3.Connection, job_video_id: str) -> list[dict[str, Any]]:
    """Per-stage progress for one video, in pipeline order."""
    labels = {
        "frames": "Pictures",
        "transcribe": "Transcript",
        "visual": "Descriptions",
        "assemble": "Put together",
    }
    rows = connection.execute(
        "SELECT stage, status, items_total, items_done FROM stage_runs"
        " WHERE job_video_id = ? ORDER BY id",
        (job_video_id,),
    ).fetchall()

    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[row["stage"]] = row

    progress = []
    for stage, label in labels.items():
        row = latest.get(stage)
        if row is None:
            progress.append({"label": label, "percent": 0, "detail": "Waiting", "state": "ready"})
            continue

        total = row["items_total"] or 0
        done = row["items_done"] or 0
        if row["status"] in {"completed", "completed_with_gaps"}:
            percent, detail = 100, "Done"
        elif total:
            percent = min(100, int(done * 100 / total))
            detail = f"{done:,} of {total:,}"
        else:
            percent, detail = 0, row["status"].replace("_", " ")

        progress.append(
            {
                "label": label,
                "percent": percent,
                "detail": detail,
                "state": row["status"],
            }
        )
    return progress
