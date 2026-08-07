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
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import Settings, assert_loopback
from app.core.db import database_path, open_database, utc_now
from app.core.locks import claim_is_stale
from app.core.logging import get_logger
from app.services.doctor import run_doctor
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

    # Settings are a frozen dataclass captured by every route closure, so a
    # save has to rebind this holder or the screens would keep showing the old
    # values until the process restarted.
    live: dict[str, Settings] = {"settings": settings}

    def current() -> Settings:
        return live["settings"]

    app = FastAPI(
        title="Video to LLM",
        description="Runs only on this computer.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def asset_version() -> str:
        """A cache key derived from the stylesheet's modification time.

        Without it, upgrading the application leaves the browser showing the
        stylesheet it cached before — the same failure mode as a running server
        serving new templates from disk, and just as confusing, because the page
        looks broken rather than out of date.
        """
        try:
            return str(int((STATIC_DIR / "tokens.css").stat().st_mtime))
        except OSError:
            return "0"

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # ── Shared context ────────────────────────────────────────────────────

    def connect() -> sqlite3.Connection | None:
        root = current().output_root
        if root is None or not database_path(root).exists():
            return None
        return open_database(root, migrate_on_open=False)

    def worker_state(connection: sqlite3.Connection | None) -> status_module.StatusPresentation:
        active = current()
        if connection is None or active.output_root is None:
            return status_module.StatusPresentation(
                "stopped", "Not started", "status-ready", "hollow square"
            )
        row = connection.execute(
            "SELECT * FROM worker_claims WHERE output_root = ?",
            (str(active.output_root),),
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
        root = current().output_root
        if root is None:
            return "No folder chosen"
        try:
            root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
        except OSError:
            return "Space unknown"
        return f"{free / 1024**3:.0f} GB free"

    def progress_fingerprint(connection: sqlite3.Connection | None) -> str:
        """A cheap value that changes exactly when a polling screen would differ.

        The screens used to reload on a fixed five-second timer regardless of
        whether anything had changed, which wiped whatever the user was in the
        middle of typing and threw the frame viewer back to the top. Comparing a
        fingerprint means a reload happens because something moved, not because
        five seconds passed.

        The job states go in individually rather than as ``MAX(updated_at)``.
        Timestamps are stored to the second, so two transitions inside one second
        — ``ready`` to ``preparing`` to ``transcribing`` is quick — leave the
        maximum unchanged, and the screen would then sit on a stale status
        indefinitely. That is worse than the reload it replaced, because a stale
        screen looks correct.

        The ``stage_runs`` sums stay aggregated: they move on every frame, so
        they never need help to change, and that table is the larger of the two.
        """
        if connection is None:
            return "none"

        from hashlib import sha256

        digest = sha256(worker_state(connection).key.encode("utf-8"))

        for row in connection.execute("SELECT id, status, updated_at FROM jobs ORDER BY id"):
            digest.update(f"|{row['id']}:{row['status']}:{row['updated_at']}".encode())

        stages = connection.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(items_done), 0) AS done,"
            " COALESCE(SUM(items_total), 0) AS total FROM stage_runs"
        ).fetchone()
        digest.update(f"|{stages['n']}:{stages['done']}:{stages['total']}".encode())

        return digest.hexdigest()[:16]

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

    def nav(screen: str, connection: sqlite3.Connection | None) -> list[NavGroup]:
        found = counts(connection)
        return [
            NavGroup(
                "Videos",
                [
                    NavItem("Dashboard", "/", found["jobs"], screen == "dashboard"),
                    NavItem("New job", "/jobs/new", "", screen == "newjob"),
                    NavItem(
                        "Bring in earlier work",
                        "/imports",
                        found["imports"],
                        screen == "imports",
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
                        screen == "collections",
                    ),
                    NavItem("New collection", "/collections/new", "", screen == "newcollection"),
                ],
            ),
            NavGroup(
                "This computer",
                [
                    NavItem("Settings & checks", "/settings", "", screen == "settings"),
                    NavItem("First-run check", "/launch", "", screen == "launch"),
                ],
            ),
        ]

    def page(
        request: Request, template: str, screen: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        connection = connect()
        try:
            running = None
            if connection is not None:
                running = connection.execute(
                    "SELECT id FROM jobs WHERE status IN"
                    " ('preparing','transcribing','analyzing','waiting_retry') LIMIT 1"
                ).fetchone()

            base = {
                "nav_groups": nav(screen, connection),
                "worker": worker_state(connection),
                "disk_label": disk_label(),
                "settings": current(),
                "status": status_module,
                # Only poll when there is something to watch; an idle screen
                # should be genuinely idle.
                "has_running": running is not None,
                "live_job_id": running["id"] if running else "",
                # Embedded so the first poll compares against what this render
                # actually showed, rather than against the poll before it.
                "progress_fingerprint": progress_fingerprint(connection),
                "asset_version": asset_version(),
            }
            base.update(context)
            return templates.TemplateResponse(
                request=request, name=template, context=base, status_code=status_code
            )
        finally:
            if connection is not None:
                connection.close()

    # ── Screens ───────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "bound_to": current().host,
                "output_root_set": current().output_root is not None,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, q: str = "", state: str = "", sort: str = "recent") -> Response:
        # A machine that has never been set up goes to the readiness screen
        # rather than an empty dashboard that explains nothing.
        if current().output_root is None:
            return RedirectResponse("/launch", status_code=303)

        connection = connect()
        jobs: list[dict[str, Any]] = []
        active: dict[str, Any] | None = None
        total_jobs = 0

        try:
            if connection is not None:
                total_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                rows = connection.execute("SELECT * FROM jobs").fetchall()

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
                        "seconds": videos["total"] or 0,
                        "length": status_module.format_duration(videos["total"]),
                        "updated": status_module.format_relative(row["updated_at"]),
                        "updated_at": row["updated_at"] or "",
                        "created_at": row["created_at"] or "",
                        "elapsed": status_module.format_elapsed(
                            row["started_at"], row["completed_at"]
                        ),
                    }
                    jobs.append(entry)
                    if active is None and status_module.is_running(row["status"]):
                        active = entry

                needle = q.strip().lower()
                if needle:
                    jobs = [j for j in jobs if needle in j["name"].lower()]
                if state == "running":
                    jobs = [j for j in jobs if status_module.is_running(j["raw_status"])]
                elif state == "attention":
                    jobs = [
                        j
                        for j in jobs
                        if j["raw_status"] in {"needs_attention", "completed_with_gaps"}
                    ]
                elif state == "finished":
                    jobs = [j for j in jobs if status_module.is_finished(j["raw_status"])]

                keys = {
                    "recent": lambda j: j["updated_at"],
                    "oldest": lambda j: j["updated_at"],
                    "name": lambda j: j["name"].lower(),
                    "longest": lambda j: j["seconds"],
                }
                jobs.sort(key=keys.get(sort, keys["recent"]), reverse=sort in {"recent", "longest"})
        finally:
            if connection is not None:
                connection.close()

        return page(
            request,
            "dashboard.html",
            "dashboard",
            jobs=jobs,
            active=active,
            q=q,
            state=state,
            sort=sort,
            total_jobs=total_jobs,
            filtered=len(jobs) != total_jobs,
        )

    @app.get("/launch", response_class=HTMLResponse)
    def launch(request: Request) -> HTMLResponse:
        from app.services.doctor import run_doctor

        report = run_doctor(current())
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
                    "stages": _stage_progress(
                        connection,
                        row["id"],
                        visual_requested=job["visual_provider"] not in (None, "", "none"),
                    ),
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
                    "time": status_module.format_moment(row["created_at"]),
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

        total_size = ""
        root = current().output_root
        if root is not None:
            from app.web.files import directory_size

            job_dir = Path(root) / job_id
            if job_dir.is_dir():
                total_bytes, _ = directory_size(job_dir)
                total_size = status_module.format_bytes(total_bytes)

        return page(
            request,
            "job.html",
            "dashboard",
            job=job,
            job_status=status_module.present(job["status"]),
            videos=videos,
            events=events,
            elapsed=status_module.format_elapsed(job["started_at"], job["completed_at"]),
            total_size=total_size,
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
    def settings_screen(request: Request) -> Response:
        return _settings_page(request)

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

    def collection_candidates(
        connection: sqlite3.Connection | None, root: Path | None
    ) -> list[dict[str, Any]]:
        """Every video that could go in a collection, with its other versions.

        The version list is usually one entry. It stops being one the moment a
        video is processed again, and a collection that could only ever pin the
        newest output would make versioning unusable from the interface.
        """
        from app.collections.model import available_sources, versions_of

        if connection is None or root is None:
            return []
        return [
            {"source": source, "versions": versions_of(connection, source.job_video_id)}
            for source in available_sources(connection, root)
        ]

    @app.get("/collections/new", response_class=HTMLResponse)
    def new_collection(request: Request) -> HTMLResponse:
        connection = connect()
        try:
            candidates = collection_candidates(connection, current().output_root)
        finally:
            if connection is not None:
                connection.close()
        return page(request, "newcollection.html", "newcollection", candidates=candidates)

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
    def review(request: Request, job_id: str, video: str = "", frame: int = 0) -> Response:
        """The frame viewer: one picture, its description, and the words around it.

        This is the screen the whole pipeline exists to feed. It reads the frame
        listing from disk and the transcript from the video's own output folder,
        so it works for any finished video without extra state.
        """
        import json as json_module

        from app.web.files import frame_listing

        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            videos = connection.execute(
                "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                " ORDER BY sequence",
                (job_id,),
            ).fetchall()
            if not videos:
                return page(request, "review.html", "dashboard", job_id=job_id, videos=[])

            chosen = next((v for v in videos if v["id"] == video), videos[0])
            root = current().output_root
            video_dir = Path(root) / chosen["output_dir"] if root and chosen["output_dir"] else None

            frames: list[dict[str, Any]] = []
            transcript: list[dict[str, Any]] = []
            descriptions: dict[int, dict[str, Any]] = {}

            if video_dir is not None and video_dir.is_dir():
                frames = frame_listing(video_dir / "frames")

                transcript_path = video_dir / "transcript.json"
                if transcript_path.is_file():
                    payload = json_module.loads(transcript_path.read_text(encoding="utf-8"))
                    transcript = payload.get("segments", [])

                visual_path = video_dir / "visual_results.json"
                if visual_path.is_file():
                    payload = json_module.loads(visual_path.read_text(encoding="utf-8"))
                    for entry in payload.get("descriptions", []):
                        descriptions[int(entry.get("index", -1))] = entry

            position = max(0, min(frame, len(frames) - 1)) if frames else 0
            active = frames[position] if frames else None

            # The transcript line nearest this frame, so the words and the
            # picture describe the same moment rather than being read separately.
            nearby = []
            if active and transcript:
                at = active["seconds"]
                for segment in transcript:
                    start = float(segment.get("start_seconds", 0))
                    if at - 45 <= start <= at + 45:
                        nearby.append(segment)

            frames_relative = (
                (Path(chosen["output_dir"]) / "frames").as_posix() if chosen["output_dir"] else ""
            )

            return page(
                request,
                "review.html",
                "dashboard",
                job_id=job_id,
                videos=videos,
                chosen=chosen,
                frames=frames,
                frame_count=len(frames),
                position=position,
                active=active,
                description=descriptions.get(active["index"]) if active else None,
                description_count=len(descriptions),
                nearby=nearby,
                transcript_count=len(transcript),
                frames_relative=frames_relative,
            )
        finally:
            connection.close()

    @app.get("/jobs/{job_id}/frames", response_class=HTMLResponse)
    def contact_sheet(request: Request, job_id: str, video: str = "", page_no: int = 0) -> Response:
        """A grid of every extracted frame, paged.

        Scanning two thousand frames for the interesting moments is not something
        a one-at-a-time viewer can do.
        """
        from app.web.files import frame_listing

        per_page = 120
        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            videos = connection.execute(
                "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                " ORDER BY sequence",
                (job_id,),
            ).fetchall()
            if not videos:
                return page(request, "frames.html", "dashboard", job_id=job_id, videos=[])

            chosen = next((v for v in videos if v["id"] == video), videos[0])
            root = current().output_root
            frames: list[dict[str, Any]] = []
            if root and chosen["output_dir"]:
                frames = frame_listing(Path(root) / chosen["output_dir"] / "frames")

            total_pages = max(1, (len(frames) + per_page - 1) // per_page)
            current_page = max(0, min(page_no, total_pages - 1))
            start = current_page * per_page

            return page(
                request,
                "frames.html",
                "dashboard",
                job_id=job_id,
                videos=videos,
                chosen=chosen,
                frames=frames[start : start + per_page],
                frame_count=len(frames),
                page_no=current_page,
                total_pages=total_pages,
                start_index=start,
                frames_relative=(
                    (Path(chosen["output_dir"]) / "frames").as_posix()
                    if chosen["output_dir"]
                    else ""
                ),
            )
        finally:
            connection.close()

    @app.get("/jobs/{job_id}/outputs", response_class=HTMLResponse)
    def outputs(request: Request, job_id: str) -> Response:
        from app.web.files import directory_size, friendly_name

        connection = connect()
        files: list[dict[str, Any]] = []
        total_bytes = 0
        root = current().output_root

        try:
            if connection is not None:
                rows = connection.execute(
                    "SELECT * FROM artifacts WHERE job_id = ? ORDER BY relative_path",
                    (job_id,),
                ).fetchall()

                for row in rows:
                    relative = row["relative_path"]
                    full = Path(root) / relative if root else None
                    is_dir = bool(full and full.is_dir())

                    size_bytes = row["size_bytes"] or 0
                    count = 0
                    if is_dir and full is not None:
                        # A dash where the size belongs hides the one number that
                        # matters when deciding what to keep — a frames folder is
                        # the largest thing this application makes.
                        size_bytes, count = directory_size(full)

                    total_bytes += size_bytes
                    files.append(
                        {
                            "name": friendly_name(relative),
                            "path": relative,
                            "kind": row["kind"],
                            "size": status_module.format_bytes(size_bytes),
                            "is_dir": is_dir,
                            "count": count,
                            "previewable": not is_dir
                            and Path(relative).suffix.lower()
                            in {".txt", ".md", ".json", ".jpg", ".jpeg", ".png"},
                        }
                    )
        finally:
            if connection is not None:
                connection.close()

        return page(
            request,
            "outputs.html",
            "dashboard",
            job_id=job_id,
            files=files,
            total_size=status_module.format_bytes(total_bytes),
        )

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
                current().visual_analysis.provider
                if current().visual_analysis.provider not in {"none", "ollama_local"}
                else "none"
            )

        try:
            result = create_job(
                connection,
                current(),
                name=name,
                paths=parse_paths(paths),
                interval_ms=interval_ms,
                provider=resolved,
                model_id=current().visual_analysis.model_id,
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

    def ordered_selection(order: str, video: list[str], version: list[str]) -> list[str]:
        """Turn the form's choices into job_video_ids, in the user's own order.

        Order comes from the explicit ``order`` field, never from the filename,
        the date, or anything about the content. Two recordings from the same
        morning have no inherent order, and guessing wrong silently reverses the
        narrative — so when the field is absent the fallback is the order the
        rows were submitted in, which is the order they were on screen, still not
        a guess about the videos themselves.

        Versions arrive as ``identity:version_row`` pairs so that a select can
        name a different row than the checkbox without the two field names
        having to be generated per video.
        """
        picked = [v for v in video if v]

        pinned: dict[str, str] = {}
        for pair in version:
            identity, _, target = pair.partition(":")
            if identity and target:
                pinned[identity] = target

        sequence = [item for item in (part.strip() for part in order.split(",")) if item in picked]
        sequence += [item for item in picked if item not in sequence]

        return [pinned.get(item, item) for item in sequence]

    def transient_collection(
        connection: sqlite3.Connection,
        root: Path,
        chosen: list[str],
        **fields: Any,
    ) -> Any:
        """A Collection assembled in memory, for previewing a build.

        ``sequence`` is overwritten with the position in the collection.
        :func:`assess_source` fills it from the video's own row, where it means
        the video's place *within its job* — and :func:`load_sources` sorts by
        it. Left alone, a preview would silently reorder the very thing the user
        just arranged by hand.
        """
        from app.collections.model import Collection, assess_source

        sources = []
        for position, video_id in enumerate(chosen):
            source = assess_source(connection, video_id, root)
            if source is None:
                continue
            source.sequence = position
            sources.append(source)

        return Collection(id="", sources=sources, **fields)

    @app.post("/api/collections/estimate")
    def estimate_collection(
        mode: str = Form("full"),
        token_limit: int = Form(200000),
        reserve_tokens: int = Form(20000),
        allow_video_split: str = Form(""),
        order: str = Form(""),
        video: list[str] = Form(default=[]),
        version: list[str] = Form(default=[]),
    ) -> JSONResponse:
        """What this collection would produce, computed before it is built.

        Runs the real loading and packing code rather than approximating it, so
        the figure on the form and the figure in the finished collection cannot
        disagree.
        """
        from app.collections.build import preview_build

        connection = connect()
        root = current().output_root
        if connection is None or root is None:
            return JSONResponse({"ready": False, "detail": "No output folder is set yet."})

        try:
            chosen = ordered_selection(order, video, version)
            if not chosen:
                return JSONResponse({"ready": False, "detail": "No videos chosen yet."})

            collection = transient_collection(
                connection,
                root,
                chosen,
                name="",
                mode=mode,
                token_limit=token_limit,
                reserve_tokens=reserve_tokens,
                allow_video_split=bool(allow_video_split),
            )
            preview = preview_build(collection, output_root=root)

            return JSONResponse(
                {
                    "ready": True,
                    "video_count": preview.video_count,
                    "duration": preview.duration_label,
                    "tokens": preview.total_tokens,
                    "token_label": preview.token_label,
                    "pack_count": preview.pack_count,
                    "warnings": preview.warnings,
                    "problem": preview.problem,
                    "destination": str(Path(root) / "collections"),
                }
            )
        finally:
            connection.close()

    @app.post("/collections")
    def create_collection_route(
        request: Request,
        name: str = Form(""),
        mode: str = Form("full"),
        token_limit: int = Form(200000),
        reserve_tokens: int = Form(20000),
        target_model_label: str = Form(""),
        allow_video_split: str = Form(""),
        order: str = Form(""),
        video: list[str] = Form(default=[]),
        version: list[str] = Form(default=[]),
    ) -> Response:
        from app.collections.build import build_collection
        from app.collections.model import (
            assess_source,
            create_collection,
            load_collection,
            set_sources,
        )

        connection = connect()
        root = current().output_root
        if connection is None or root is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            chosen = ordered_selection(order, video, version)
            if not name.strip() or not chosen:
                return page(
                    request,
                    "newcollection.html",
                    "newcollection",
                    candidates=collection_candidates(connection, root),
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
            sources = [assess_source(connection, video_id, root) for video_id in chosen]
            set_sources(connection, collection_id, [s for s in sources if s])

            collection = load_collection(connection, collection_id)
            if collection is not None:
                # Building is local, free, and takes seconds, so it happens
                # immediately rather than behind another confirmation.
                build_collection(connection, collection, output_root=root)

            return RedirectResponse(f"/collections/{collection_id}", status_code=303)
        finally:
            connection.close()

    # ── Settings ──────────────────────────────────────────────────────────

    def commit_settings(candidate: Settings) -> list[str]:
        """Validate, write, and rebind — or report why not, having changed nothing.

        Validation runs before the write so a configuration that would stop the
        application from starting is refused rather than persisted: a settings
        file that prevents start-up is far harder to recover from than a
        rejected form.
        """
        from app.core.config import NonLoopbackAddressError, save_settings

        try:
            save_settings(candidate)
        except (NonLoopbackAddressError, ValueError) as error:
            return [str(error)]
        except OSError as error:
            return [f"The settings could not be saved: {error}"]

        live["settings"] = candidate
        return []

    def output_root_blocker() -> str:
        """Why the output folder cannot be repointed right now, or an empty string.

        The worker holds a claim on the current root and the database lives
        inside it. Moving the target while either is in use would leave a job
        writing to one place and the interface reading another.
        """
        connection = connect()
        if connection is None:
            return ""
        try:
            if worker_state(connection).key == "running":
                return (
                    "Background processing is running. Stop it first — a worker "
                    "writing into the old folder while the interface reads the new "
                    "one would leave both wrong."
                )
            running = connection.execute(
                "SELECT name FROM jobs WHERE status IN"
                " ('preparing','transcribing','analyzing','waiting_retry') LIMIT 1"
            ).fetchone()
            if running is not None:
                return (
                    f"{running['name']} is still being processed. Wait for it to "
                    "finish, or stop it, before changing where output goes."
                )
        finally:
            connection.close()
        return ""

    @app.post("/settings/storage")
    def save_storage_route(
        request: Request, output_root: str = Form(""), port: int = Form(8712)
    ) -> Response:
        """Where output goes, and which port the interface answers on.

        Repointing the output folder **moves nothing**. Work already done stays
        where it is and new jobs go to the new place. Relocating a tree that can
        run to tens of gigabytes, with a live database inside it, is a different
        feature with its own failure modes — and quietly half-doing it would be
        the worst of the options.
        """
        from dataclasses import replace

        active = current()
        problems: list[str] = []
        chosen = output_root.strip()

        candidate = replace(active, port=port)

        if chosen and Path(chosen).expanduser() != active.output_root:
            blocker = output_root_blocker()
            if blocker:
                problems.append(blocker)
            else:
                try:
                    resolved = Path(chosen).expanduser().resolve()
                    resolved.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    problems.append(f"That folder could not be used: {error}")
                else:
                    candidate = candidate.with_output_root(resolved)

        if not problems:
            problems = commit_settings(candidate)

        if problems:
            return _settings_page(request, problems=problems, draft=candidate)

        # Create the database in the new folder straight away. Without it the
        # screens read as an empty first-run install until the first job is
        # made, which looks like the change lost the user's work.
        if candidate.output_root is not None:
            open_database(candidate.output_root).close()

        return RedirectResponse("/settings?saved=1#where", status_code=303)

    @app.post("/settings/transcription")
    def save_transcription_route(
        request: Request,
        backend: str = Form("auto"),
        model: str = Form("medium"),
        language: str = Form("auto"),
        silence_threshold_seconds: float = Form(3.0),
    ) -> Response:
        from dataclasses import replace

        from app.core.config import TranscriptionSettings

        active = current()
        candidate = replace(
            active,
            transcription=TranscriptionSettings(
                backend=backend.strip() or "auto",
                model=model.strip() or "medium",
                language=language.strip() or "auto",
                silence_threshold_seconds=max(0.5, min(30.0, silence_threshold_seconds)),
            ),
        )

        problems = commit_settings(candidate)
        if problems:
            return _settings_page(request, problems=problems, draft=candidate)
        return RedirectResponse("/settings?saved=1#speech", status_code=303)

    @app.post("/settings/collections")
    def save_collection_defaults_route(
        request: Request,
        default_token_limit: int = Form(200000),
        default_reserve_tokens: int = Form(20000),
        allow_video_split: str = Form(""),
    ) -> Response:
        from dataclasses import replace

        from app.core.config import CollectionSettings

        active = current()
        problems: list[str] = []
        if default_reserve_tokens >= default_token_limit:
            problems.append(
                "Holding back that much leaves nothing for the content. Lower the "
                "amount held back, or raise how much fits in one go."
            )

        candidate = replace(
            active,
            collections=CollectionSettings(
                default_token_limit=max(1000, default_token_limit),
                default_reserve_tokens=max(0, default_reserve_tokens),
                allow_video_split=bool(allow_video_split),
            ),
        )

        if not problems:
            problems = commit_settings(candidate)
        if problems:
            return _settings_page(request, problems=problems, draft=candidate)
        return RedirectResponse("/settings?saved=1#collections", status_code=303)

    @app.post("/settings/advanced")
    def save_advanced_route(
        request: Request,
        preset: str = Form("balanced"),
        custom_interval_seconds: float = Form(2.0),
        concurrency: int = Form(1),
        poll_interval_seconds: int = Form(2),
        max_retries: int = Form(3),
        backoff_base_seconds: int = Form(5),
    ) -> Response:
        from dataclasses import replace

        from app.core.config import SamplingSettings, WorkerSettings

        active = current()
        candidate = replace(
            active,
            sampling=SamplingSettings(
                preset=preset,
                custom_interval_seconds=custom_interval_seconds,
            ),
            ollama=replace(active.ollama, concurrency=max(1, min(8, concurrency))),
            worker=WorkerSettings(
                poll_interval_seconds=max(1, min(300, poll_interval_seconds)),
                max_retries=max(0, min(10, max_retries)),
                backoff_base_seconds=max(1, min(600, backoff_base_seconds)),
            ),
        )

        problems = commit_settings(candidate)
        if problems:
            return _settings_page(request, problems=problems, draft=candidate)
        return RedirectResponse("/settings?saved=1#advanced", status_code=303)

    @app.post("/settings")
    def save_settings_route(
        request: Request,
        enabled: str = Form(""),
        provider: str = Form("none"),
        model_id: str = Form(""),
        endpoint: str = Form("http://127.0.0.1:11434"),
        batch_size: int = Form(1),
        acknowledged: str = Form(""),
        hard_limit_usd: float = Form(25.0),
        max_runtime_minutes: int = Form(0),
        max_frames_per_run: int = Form(0),
    ) -> Response:
        """Save the description settings, including the spending cap.

        The cap was previously settable only by hand-editing TOML, which made
        the one number that enforces the budget the hardest one to reach. It is
        checked before a batch is sent, never after — see providers/costs.py.
        """
        from dataclasses import replace

        from app.core.config import BudgetSettings, LocalGuardSettings

        active = current()
        want_enabled = bool(enabled) and provider != "none"

        candidate = replace(
            active,
            visual_analysis=replace(
                active.visual_analysis,
                enabled=want_enabled,
                provider=provider,
                model_id=model_id.strip(),
                budget=BudgetSettings(
                    hard_limit_usd=max(0.0, hard_limit_usd),
                    # Carried through rather than offered as a choice: nothing
                    # reads it yet, and a control that changes no behaviour is
                    # the same lie as placeholder data.
                    on_limit=active.visual_analysis.budget.on_limit,
                ),
                local_guard=LocalGuardSettings(
                    max_runtime_minutes=max(0, max_runtime_minutes),
                    max_frames_per_run=max(0, max_frames_per_run),
                ),
            ),
            ollama=replace(
                active.ollama,
                endpoint=endpoint.strip() or "http://127.0.0.1:11434",
                batch_size=max(1, min(4, batch_size)),
                experimental_acknowledged=bool(acknowledged),
            ),
        )

        problems: list[str] = []
        if want_enabled and provider == "ollama_local" and not model_id.strip():
            problems.append(
                "Name the model you installed, for example qwen2.5vl:7b. "
                "Use Check local model to see what is available."
            )

        if not problems:
            problems = commit_settings(candidate)
        if problems:
            return _settings_page(request, problems=problems, draft=candidate)
        return RedirectResponse("/settings?saved=1#describing", status_code=303)

    @app.post("/settings/check-local")
    def check_local_route(request: Request) -> Response:
        """Ask the local runtime what it is and what it has.

        Health only — nothing is generated and nothing is charged.
        """
        from app.core.config import NonLoopbackAddressError
        from app.providers.ollama_local import OllamaLocalProvider

        active = current()
        try:
            provider = OllamaLocalProvider(
                endpoint=active.ollama.endpoint,
                model_id=active.visual_analysis.model_id,
            )
            health = provider.health_check()
        except NonLoopbackAddressError as error:
            return _settings_page(request, problems=[str(error)])
        except Exception as error:
            from app.core.redaction import redacted_exception_text

            return _settings_page(
                request, problems=[f"The check could not run: {redacted_exception_text(error)}"]
            )

        return _settings_page(request, health=health)

    def _settings_page(
        request: Request,
        *,
        problems: list[str] | None = None,
        health: Any = None,
        draft: Settings | None = None,
    ) -> HTMLResponse:
        from app.credentials.store import ENV_VARS, credential_status

        credentials = [
            {"provider": name, "status": credential_status(name)}
            for name in ("anthropic", "google", "openai", "openai_compatible")
        ]
        return page(
            request,
            "settings.html",
            "settings",
            report=run_doctor(current()),
            credentials=credentials,
            env_vars=ENV_VARS,
            problems=problems or [],
            health=health,
            draft=draft or current(),
            saved=request.query_params.get("saved") == "1",
            # Shown before the user tries, rather than as a rejection afterwards.
            root_blocker=output_root_blocker(),
            running_port=settings.port,
        )

    @app.exception_handler(500)
    def internal_error(request: Request, exc: Exception) -> Response:
        """Explain a failure rather than showing a bare "Internal Server Error".

        The detail goes to the log, redacted, and never to the browser: an
        exception message can carry a path, a query, or a credential, and this
        page is reachable without any authentication.
        """
        from app.core.redaction import redacted_exception_text

        logger.error(
            "Unhandled error rendering %s: %s",
            request.url.path,
            redacted_exception_text(exc),
            exc_info=True,
        )
        try:
            return page(request, "error.html", "dashboard", status_code=500)
        except Exception:
            # The shell itself is broken; anything richer would fail too.
            return HTMLResponse(
                "<h1>Something went wrong on this screen</h1>"
                "<p>Your jobs and files are unaffected. "
                "If the application was updated while this server was running, "
                "stop it and start it again.</p>",
                status_code=500,
            )

    # ── Files ─────────────────────────────────────────────────────────────

    @app.get("/files/{relative_path:path}")
    def serve_file(relative_path: str, download: int = 0) -> Response:
        """Serve a file from the output root, and nothing outside it."""
        from app.web.files import OutsideOutputRoot, resolve_within

        if settings.output_root is None:
            return PlainTextResponse("No output folder is set.", status_code=404)

        try:
            resolved = resolve_within(settings.output_root, relative_path)
        except OutsideOutputRoot:
            return PlainTextResponse("That file is outside the output folder.", status_code=403)
        except (FileNotFoundError, OSError):
            return PlainTextResponse("That file could not be found.", status_code=404)

        if resolved.is_dir:
            return PlainTextResponse("That is a folder, not a file.", status_code=400)

        disposition = (
            "attachment" if download else ("inline" if resolved.serves_inline else "attachment")
        )
        return FileResponse(
            resolved.path,
            media_type=resolved.media_type,
            filename=resolved.path.name,
            headers={"Content-Disposition": f'{disposition}; filename="{resolved.path.name}"'},
        )

    @app.get("/preview/{relative_path:path}", response_class=HTMLResponse)
    def preview_file(request: Request, relative_path: str) -> Response:
        from app.web.files import OutsideOutputRoot, read_preview, resolve_within

        if settings.output_root is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            resolved = resolve_within(settings.output_root, relative_path)
        except OutsideOutputRoot:
            return page(request, "notfound.html", "dashboard", what="file", status_code=403)
        except (FileNotFoundError, OSError):
            return page(request, "notfound.html", "dashboard", what="file", status_code=404)

        text, truncated = ("", False)
        if resolved.is_text:
            text, truncated = read_preview(resolved)

        return page(
            request,
            "preview.html",
            "dashboard",
            file=resolved,
            text=text,
            truncated=truncated,
        )

    @app.post("/reveal")
    def reveal_in_file_manager(relative_path: str = Form("")) -> Response:
        """Open the containing folder in the desktop file manager.

        Legitimate here in a way it would not be in a hosted application: this
        server runs on the user's own machine, on the loopback interface, and the
        folder being revealed is one they chose.
        """
        from app.web.files import OutsideOutputRoot, resolve_within

        if settings.output_root is None:
            return JSONResponse({"ok": False, "detail": "No output folder is set."})

        try:
            resolved = resolve_within(settings.output_root, relative_path)
        except (OutsideOutputRoot, FileNotFoundError, OSError):
            return JSONResponse({"ok": False, "detail": "That file could not be found."})

        target = resolved.path if resolved.is_dir else resolved.path.parent
        try:
            _open_in_file_manager(target)
        except OSError as error:
            return JSONResponse({"ok": False, "detail": str(error)})
        return JSONResponse({"ok": True})

    # ── Live progress ─────────────────────────────────────────────────────

    @app.get("/api/progress")
    def progress(job_id: str = "") -> JSONResponse:
        """A small snapshot the screens poll so a running job is visibly running."""
        connection = connect()
        if connection is None:
            return JSONResponse({"worker": "unknown", "jobs": [], "fingerprint": "none"})

        try:
            worker = worker_state(connection)
            # Two literal statements rather than one built by concatenation:
            # the shapes differ only by a WHERE clause, and assembling SQL from
            # pieces is how an injection gets in later even when today's inputs
            # are safe.
            if job_id:
                rows = connection.execute(
                    "SELECT id, name, status, updated_at FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, name, status, updated_at FROM jobs"
                    " ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()

            jobs = []
            for row in rows:
                # Every column is qualified: `status` exists on both stage_runs
                # and job_videos, and an unqualified reference is ambiguous.
                totals = connection.execute(
                    "SELECT COALESCE(SUM(s.items_done), 0) AS done,"
                    "       COALESCE(SUM(s.items_total), 0) AS total"
                    " FROM stage_runs s"
                    " JOIN job_videos v ON v.id = s.job_video_id"
                    " WHERE v.job_id = ? AND v.is_active_version = 1",
                    (row["id"],),
                ).fetchone()
                done = int(totals["done"] or 0)
                total = int(totals["total"] or 0)
                jobs.append(
                    {
                        "id": row["id"],
                        "status": row["status"],
                        "label": status_module.present(row["status"]).label,
                        "updated": status_module.format_relative(row["updated_at"]),
                        "done": done,
                        "total": total,
                        "running": status_module.is_running(row["status"]),
                    }
                )

            return JSONResponse(
                {
                    "worker": worker.key,
                    "worker_label": worker.label,
                    "jobs": jobs,
                    "fingerprint": progress_fingerprint(connection),
                }
            )
        finally:
            connection.close()

    # ── Worker control ────────────────────────────────────────────────────

    @app.post("/worker/start")
    def start_worker_route() -> Response:
        """Start background processing without going back to a terminal.

        The worker runs in its own process rather than a thread of this one, so
        it survives the interface being restarted — which is the whole point of
        the separation.
        """
        import subprocess
        import sys

        if settings.output_root is None:
            return RedirectResponse("/launch", status_code=303)

        connection = connect()
        try:
            if connection is not None:
                state = worker_state(connection)
                if state.key == "running":
                    return RedirectResponse("/settings", status_code=303)
                # A claim left by a process that is gone would otherwise make the
                # new worker wait out its full staleness window for no reason.
                _clear_dead_claim(connection, settings.output_root)
        finally:
            if connection is not None:
                connection.close()

        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.cli",
                "run-worker",
                "--output-root",
                str(settings.output_root),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Started a background worker on request")
        return RedirectResponse("/settings", status_code=303)

    # ── Browsing the filesystem for videos (F05) ──────────────────────────

    @app.get("/api/browse")
    def browse(path: str = "") -> JSONResponse:
        """List folders and videos at *path*, so videos can be picked not typed.

        A hosted application could not do this. Running on the user's own machine
        legitimately can, and it removes the single worst interaction in the
        product — typing absolute paths into a textarea.
        """
        from app.pipeline.probe import SUPPORTED_EXTENSIONS

        target = Path(path).expanduser() if path else Path.home()
        try:
            target = target.resolve()
            if not target.is_dir():
                target = target.parent
            entries = sorted(target.iterdir(), key=lambda e: e.name.lower())
        except (OSError, PermissionError) as error:
            return JSONResponse(
                {"ok": False, "detail": f"That folder could not be read ({error.strerror})."}
            )

        folders = []
        videos = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    folders.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                    videos.append(
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "size": status_module.format_bytes(entry.stat().st_size),
                        }
                    )
            except OSError:
                continue

        return JSONResponse(
            {
                "ok": True,
                "path": str(target),
                "parent": str(target.parent) if target.parent != target else "",
                "folders": folders[:400],
                "videos": videos[:400],
            }
        )

    # ── Job management (F11, F12, F24) ────────────────────────────────────

    @app.post("/jobs/{job_id}/rename")
    def rename_job(job_id: str, name: str = Form("")) -> Response:
        connection = connect()
        if connection is not None:
            try:
                if name.strip():
                    connection.execute(
                        "UPDATE jobs SET name = ?, updated_at = ? WHERE id = ?",
                        (name.strip(), utc_now(), job_id),
                    )
            finally:
                connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/delete")
    def delete_job(job_id: str, remove_files: str = Form("")) -> Response:
        """Delete a job. Files are removed only when explicitly asked for.

        The default keeps the output: the database row is cheap to recreate and
        the artifacts are the expensive part. Removing them has to be a separate,
        deliberate choice.
        """
        import shutil as shutil_module

        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            root = current().output_root
            if remove_files and root is not None:
                job_dir = Path(root) / job_id
                try:
                    resolved = job_dir.resolve()
                    resolved.relative_to(Path(root).resolve())
                except ValueError:
                    logger.error("Refused to delete outside the output folder")
                else:
                    if resolved.is_dir():
                        shutil_module.rmtree(resolved, ignore_errors=True)
                        logger.info("Removed the output folder for job %s", job_id[:8])

            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        finally:
            connection.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/{job_id}/describe")
    def describe_now(job_id: str, video_id: str = Form("")) -> Response:
        """Add descriptions to a video that was processed without them.

        The interface has always told the user this was possible. Until now
        nothing did it — a promise the product made and could not keep.
        """
        active = current()
        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            if not active.visual_analysis.enabled or active.visual_analysis.provider == "none":
                connection.execute(
                    "INSERT INTO events (job_id, level, kind, message, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        job_id,
                        "warning",
                        "describe_blocked",
                        "Descriptions were requested, but no description model is set up yet. "
                        "Choose one in Settings, then ask again.",
                        utc_now(),
                    ),
                )
                return RedirectResponse("/settings", status_code=303)

            # Clearing the completed visual stage is what makes the worker run it
            # again; the frames and transcript are untouched.
            if video_id:
                connection.execute(
                    "DELETE FROM stage_runs WHERE job_video_id = ? AND stage = 'visual'",
                    (video_id,),
                )
                connection.execute(
                    "UPDATE job_videos SET status = 'pending', updated_at = ? WHERE id = ?",
                    (utc_now(), video_id),
                )
            connection.execute(
                "UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?",
                (utc_now(), job_id),
            )
            connection.execute(
                "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
                (
                    job_id,
                    "info",
                    "describe_requested",
                    "Descriptions requested. The pictures and transcript are kept — "
                    "only the descriptions are produced.",
                    utc_now(),
                ),
            )
        finally:
            connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/collections/{collection_id}/delete")
    def delete_collection(collection_id: str) -> Response:
        connection = connect()
        if connection is not None:
            try:
                connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
            finally:
                connection.close()
        return RedirectResponse("/collections", status_code=303)

    # ── Credentials (F06) ─────────────────────────────────────────────────

    @app.post("/settings/key")
    def save_key(request: Request, provider: str = Form(""), api_key: str = Form("")) -> Response:
        """Store a key. Write-only: it is never rendered back, not even masked.

        A few revealed characters still narrow a search, and presence is the only
        fact the interface needs.
        """
        from app.credentials.store import CredentialError, set_credential

        try:
            set_credential(provider, api_key)
        except CredentialError as error:
            return _settings_page(request, problems=[str(error)])
        return RedirectResponse("/settings?saved=key", status_code=303)

    @app.post("/settings/key/remove")
    def remove_key(provider: str = Form("")) -> Response:
        from app.credentials.store import delete_credential

        delete_credential(provider)
        return RedirectResponse("/settings?saved=removed", status_code=303)

    return app


def _open_in_file_manager(target: Path) -> None:
    import platform
    import subprocess

    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(target)])
    elif system == "Windows":
        subprocess.Popen(["explorer", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _clear_dead_claim(connection: sqlite3.Connection, output_root: Path) -> None:
    """Remove a claim whose process is demonstrably gone on this machine.

    Only ever clears a claim made by *this* host — a claim from another machine
    cannot be checked from here, and guessing would be exactly the mistake the
    double guard exists to prevent.
    """
    import os
    import socket

    row = connection.execute(
        "SELECT hostname, pid FROM worker_claims WHERE output_root = ?",
        (str(output_root),),
    ).fetchone()
    if row is None or row["hostname"] != socket.gethostname():
        return

    try:
        os.kill(int(row["pid"]), 0)
    except ProcessLookupError:
        connection.execute("DELETE FROM worker_claims WHERE output_root = ?", (str(output_root),))
        logger.info("Cleared a claim left behind by dead pid %s", row["pid"])
    except (OSError, ValueError):
        return


def _stage_progress(
    connection: sqlite3.Connection, job_video_id: str, *, visual_requested: bool = True
) -> list[dict[str, Any]]:
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
            # "Waiting" on a finished job reads as a stalled stage. A stage that
            # was never requested is a different thing and should say so.
            detail = "Not run" if stage == "visual" and not visual_requested else "Waiting"
            progress.append({"label": label, "percent": 0, "detail": detail, "state": "ready"})
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
