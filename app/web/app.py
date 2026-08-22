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

from app.core.build import current_fingerprint, source_fingerprint
from app.core.config import CUSTOM_ENDPOINT_PROVIDERS, Settings, assert_loopback
from app.core.db import database_path, open_database, utc_now
from app.core.locks import claim_is_stale
from app.core.logging import get_logger
from app.providers.cloud import PROVIDERS, build_provider
from app.services.doctor import run_doctor
from app.web import status as status_module

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: Methods that cannot change state, and so need no origin check.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: ``Sec-Fetch-Site`` values that mean "this request came from us".
#: ``none`` is a user-initiated navigation — typing the address, a bookmark.
SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


def hostname_of(value: str) -> str:
    """The host in a ``Host`` or ``Origin`` header, without scheme or port.

    Handles the bracketed IPv6 form (``[::1]:8712``) and leaves a bare IPv6
    literal alone, so ``::1`` is not mistaken for a host called ``:`` with a
    port.
    """
    candidate = value.strip()
    if "//" in candidate:
        candidate = candidate.split("//", 1)[1]
    candidate = candidate.split("/", 1)[0]

    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing != -1:
            return candidate[1:closing]

    # Exactly one colon is host:port. Two or more is a bare IPv6 literal.
    if candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    return candidate


def _with_entry(existing: dict[str, str], key: str, value: str) -> dict[str, str]:
    """A copy of *existing* with *key* set, or removed when the value is blank.

    Blank removes rather than storing an empty string, so "I cleared this field"
    and "I never set this field" stay the same state. Two spellings of absence
    is how a settings file starts disagreeing with the screen.
    """
    updated = dict(existing)
    if not key:
        return updated
    cleaned = value.strip()
    if cleaned:
        updated[key] = cleaned
    else:
        updated.pop(key, None)
    return updated


def job_folder(job: sqlite3.Row | dict[str, Any]) -> str:
    """The folder holding a job's output, by name where one was recorded.

    NULL for every job created before folders were named, so the fallback is the
    identifier — not a slug recomputed from the current name, which would point
    at a folder that does not hold that job's existing output.
    """
    try:
        recorded = job["output_dirname"]
    except (IndexError, KeyError, TypeError):
        recorded = None
    return str(recorded) if recorded else str(job["id"])


def whole_number(raw: str, fallback: int = 0) -> int:
    """A query parameter read as an integer, never as a validation error.

    Declaring these as ``int`` let FastAPI reject ``?frame=abc`` with a raw 422
    JSON body — a framework error page, in a product whose every other failure
    is a sentence in plain English. A hand-edited address, a stale bookmark, or
    a link someone pasted into a chat should land on the picture, not on a
    schema dump. Out-of-range values are already clamped by the caller, so
    coercing here is the whole fix.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


#: Bounds on the collection form's numbers. Below the minimum a pack cannot hold
#: a single section; above the maximum no model anyone is targeting has a window
#: that large, so the figure is a typo rather than an intention.
MIN_TOKEN_LIMIT = 1_000
MAX_TOKEN_LIMIT = 10_000_000


def collection_numbers(raw_limit: str, raw_reserve: str) -> tuple[list[str], int, int]:
    """Validate the token limit and reserve, returning problems and the values.

    Declared as integers, these produced a raw 422 for anything non-numeric. And
    a reserve larger than the limit was accepted: ``usable_budget`` clamps at
    zero, so nothing crashed — the build simply had no room for any section and
    produced a collection that was silently useless. A number the form cannot
    honour should be refused on the form.
    """
    problems: list[str] = []

    limit = whole_number(raw_limit, -1)
    reserve = whole_number(raw_reserve, -1)

    if limit < 0:
        problems.append("The token limit must be a whole number.")
    elif not MIN_TOKEN_LIMIT <= limit <= MAX_TOKEN_LIMIT:
        problems.append(
            f"The token limit must be between {MIN_TOKEN_LIMIT:,} and {MAX_TOKEN_LIMIT:,}."
        )

    if reserve < 0:
        problems.append("The reserve must be a whole number, and cannot be negative.")
    elif limit >= 0 and reserve >= limit:
        problems.append(
            f"The reserve ({reserve:,}) leaves no room inside the limit "
            f"({limit:,}). Lower the reserve, or raise the limit."
        )

    return problems, limit, reserve


#: Statuses in which the worker may be writing into the job's folder right now.
IN_FLIGHT_STATES = frozenset({"preparing", "transcribing", "analyzing", "waiting_retry"})


def in_flight_reason(connection: sqlite3.Connection, job_id: str) -> str:
    """Why this job cannot be disturbed right now, or an empty string.

    The worker owns a job while it runs. Deleting its folder, resetting its
    status, or queueing more work on top all reach past that ownership, and the
    worker has no way to notice — it is a separate process holding open file
    handles into a directory the interface just removed.
    """
    row = connection.execute("SELECT status, name FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] not in IN_FLIGHT_STATES:
        return ""
    return (
        f"{row['name']} is being processed right now. Stop it first — "
        "changing it while the worker is writing would leave the job and the "
        "files on disk disagreeing."
    )


def collections_using(connection: sqlite3.Connection, job_id: str) -> list[str]:
    """Names of collections that cite a video from this job.

    A collection pins the exact source version it was built from, which is why
    ``collection_sources`` holds an unqualified reference to ``job_videos``:
    reprocessing a video must never rewrite a collection that already went out.
    That same reference means deleting the job is refused by the database, so it
    has to be refused by the interface first — with the names, because "this is
    in use" without saying by what is a dead end.
    """
    rows = connection.execute(
        "SELECT DISTINCT c.name FROM collections c"
        " JOIN collection_sources cs ON cs.collection_id = c.id"
        " JOIN job_videos jv ON jv.id = cs.job_video_id"
        " WHERE jv.job_id = ? ORDER BY c.name",
        (job_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def foreign_host(request: Request) -> str:
    """Why this request's ``Host`` is not this machine, or an empty string.

    Binding to loopback stops other machines connecting directly. It does not
    stop a hostname that *resolves* to 127.0.0.1 — DNS rebinding — and a request
    arriving under an attacker's hostname is same-origin as far as the browser
    is concerned, so the response becomes readable by that attacker's page.
    Checking the name the client asked for is what closes that.
    """
    from app.core.config import is_loopback_host

    header = request.headers.get("host")
    if header is None:
        return ""
    if is_loopback_host(hostname_of(header)):
        return ""
    return (
        "This application answers only to 127.0.0.1, localhost, or ::1. "
        f"It was asked for {hostname_of(header)!r}."
    )


def foreign_origin(request: Request) -> str:
    """Why this request came from another site, or an empty string.

    Loopback binding keeps other *machines* out; it does nothing about other
    *origins*. A urlencoded form post is a CORS "simple request", so it needs no
    preflight and no consent: any page the user has open in any tab can submit a
    form to this server. Without this check every state-changing route is
    callable by any website the user visits — creating jobs, removing a stored
    key, spending money on a rerun, or deleting a job together with its files.

    A page cannot make the browser omit ``Origin`` or forge ``Sec-Fetch-Site``
    on a cross-site request. So the absence of both is read as a non-browser
    client — curl, the CLI, a test — and allowed. Treating absence as a refusal
    would break every one of those without making a browser any safer.
    """
    from app.core.config import is_loopback_host

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site.strip().lower() in SAME_ORIGIN_FETCH_SITES:
            return ""
        return (
            "This request came from another site. This application accepts "
            "actions only from its own pages."
        )

    origin = request.headers.get("origin")
    if origin is None:
        return ""
    if origin.strip().lower() == "null" or not is_loopback_host(hostname_of(origin)):
        return (
            "This request came from another site. This application accepts "
            "actions only from its own pages."
        )
    return ""


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


def _interval_from_form(interval: str) -> int | None:
    """The picture interval a form submitted, or None to use the setting.

    Shared by the create route and the plan route rather than inlined in each.
    A plan that computed a different frame count from the job it is previewing
    would be worse than no plan, and two copies of three lines is exactly how
    that happens.
    """
    try:
        return None if interval == "custom" else int(interval)
    except ValueError:
        return None


def create_app(settings: Settings) -> FastAPI:
    # Captured once, at import-and-construct time, so a later edit to any module
    # moves the fingerprint on disk past this one.
    loaded_fingerprint = source_fingerprint()

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

    # ── The origin boundary ───────────────────────────────────────────────

    @app.middleware("http")
    async def refuse_foreign_callers(request: Request, call_next: Any) -> Response:
        """Refuse requests from another hostname or another site.

        One place rather than a decorator per route: this has to cover every
        route that exists and every route added later, and a check you have to
        remember to apply is a check that will eventually be forgotten.

        ``/api/`` is held to the origin rule on reads too. Those endpoints return
        data rather than a page — the file picker lists directories — and a read
        primitive deserves the same boundary as a write.
        """
        problem = foreign_host(request)
        if problem:
            return PlainTextResponse(problem, status_code=421)

        guarded = request.method not in SAFE_METHODS or request.url.path.startswith("/api/")
        if guarded:
            problem = foreign_origin(request)
            if problem:
                return PlainTextResponse(problem, status_code=403)

        response: Response = await call_next(request)
        return response

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

    def finished_while_away(connection: sqlite3.Connection | None) -> list[dict[str, Any]]:
        """Jobs that finished and have not been seen yet.

        The durable flag is what makes this survive a restart, a closed browser,
        and an overnight suspension — which is the entire situation this is for.
        Holding it in the browser would forget the moment the user opened a
        different tab, and holding it nowhere would make the banner reappear on
        every page load until the end of time.
        """
        if connection is None:
            return []
        rows = connection.execute(
            "SELECT id, name, status, completed_at, started_at FROM jobs"
            " WHERE completion_acknowledged_at IS NULL"
            " AND status IN ('completed', 'completed_with_gaps')"
            " ORDER BY completed_at DESC LIMIT 5"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "label": status_module.present(row["status"]).label,
                "when": status_module.format_relative(row["completed_at"]),
                "took": status_module.format_elapsed(row["started_at"], row["completed_at"]),
                "with_gaps": row["status"] == "completed_with_gaps",
            }
            for row in rows
        ]

    def counts(connection: sqlite3.Connection | None) -> dict[str, str]:
        if connection is None:
            return {"jobs": "", "collections": "", "imports": "", "finished": ""}
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finished = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('completed', 'completed_with_gaps')"
        ).fetchone()[0]
        collections = connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        imports = connection.execute(
            "SELECT COUNT(*) FROM job_videos WHERE imported_from IS NOT NULL"
            " AND imported_from != ''"
        ).fetchone()[0]
        return {
            "jobs": str(jobs) if jobs else "",
            "collections": str(collections) if collections else "",
            "imports": str(imports) if imports else "",
            "finished": str(finished) if finished else "",
        }

    def nav(
        screen: str, connection: sqlite3.Connection | None, *, state: str = ""
    ) -> list[NavGroup]:
        found = counts(connection)
        return [
            NavGroup(
                "Videos",
                [
                    NavItem("Dashboard", "/", found["jobs"], screen == "dashboard" and not state),
                    NavItem("New job", "/jobs/new", "", screen == "newjob"),
                    # The dashboard has always been able to filter to finished
                    # work; nothing pointed at it. A job that has completed drops
                    # out of sight among everything else, and the only route back
                    # was to remember it and scroll — on the screen a user is
                    # most likely to want after leaving a long job running.
                    NavItem(
                        "Finished",
                        "/?state=finished",
                        found["finished"],
                        screen == "dashboard" and state == "finished",
                    ),
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
        request: Request,
        template: str,
        screen: str,
        status_code: int = 200,
        nav_state: str = "",
        **context: Any,
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
                # True when the Python on disk is newer than the Python this
                # process imported. Templates reload per request and routes do
                # not, so an updated application serves new screens from old
                # code — and every symptom of that looks like an ordinary bug.
                "restart_needed": current_fingerprint() > loaded_fingerprint,
                "nav_groups": nav(screen, connection, state=nav_state),
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
                "finished_while_away": finished_while_away(connection),
                "notifications": current().notifications,
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
            nav_state=state,
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
        return page(request, "newjob.html", "newjob", services=describable_services())

    def describable_services() -> list[dict[str, Any]]:
        """Every service that can describe pictures, and what it needs.

        The choice used to be a single card reading "Send to a service", with
        *which* service settled elsewhere in Settings — so the job screen asked a
        question it would not let you answer, and someone with three accounts had
        to leave, change a global, and come back.
        """
        from app.core.config import CUSTOM_ENDPOINT_PROVIDERS as CUSTOM
        from app.credentials.store import credential_status

        active = current()
        from app.core.config import PROVIDER_LABELS

        cloud = [n for n in PROVIDER_LABELS if n != "ollama_local"]
        return [
            {
                "provider": name,
                "label": PROVIDER_LABELS[name],
                "ready": credential_status(name).present,
                "model": active.visual_analysis.model_for(name),
                "base_url": active.visual_analysis.base_urls.get(name, ""),
                "needs_address": name in CUSTOM,
            }
            for name in cloud
        ]

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str) -> Response:
        return render_job(request, job_id)

    def render_job(
        request: Request,
        job_id: str,
        problems: list[str] | None = None,
        status_code: int = 200,
    ) -> Response:
        """The job screen, optionally carrying a refusal.

        Separate from the route so an action that cannot be carried out — a
        delete the database would reject, a rerun on a job already running — can
        say so on the screen the user pressed the button from, rather than
        redirecting to a page that looks as though it worked.
        """
        connection = connect()
        try:
            if connection is None:
                return RedirectResponse("/", status_code=303)

            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

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

            # Where this job sits in the line, and what is in front of it. Only
            # offered when there is genuinely a queue: "Run next" on the only
            # job waiting is a button that cannot do anything.
            from app.services.jobs import queue_order

            queue = queue_order(connection)
            queue_position = next(
                (index + 1 for index, row in enumerate(queue) if row["id"] == job_id), 0
            )
            queue_length = len(queue)
            ahead_name = str(queue[0]["name"]) if queue and queue_position > 1 else ""
        finally:
            if connection is not None:
                connection.close()

        total_size = ""
        root = current().output_root
        if root is not None:
            from app.web.files import directory_size

            job_dir = Path(root) / job_folder(job)
            if job_dir.is_dir():
                total_bytes, _ = directory_size(job_dir)
                total_size = status_module.format_bytes(total_bytes)

        return page(
            request,
            "job.html",
            "dashboard",
            status_code=status_code,
            job=job,
            job_status=status_module.present(job["status"]),
            videos=videos,
            events=events,
            elapsed=status_module.format_elapsed(job["started_at"], job["completed_at"]),
            total_size=total_size,
            queue_position=queue_position,
            queue_length=queue_length,
            ahead_name=ahead_name,
            problems=problems or [],
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
        # From the estimator itself, so the number quoted on screen cannot drift
        # from the one actually used to size the parts.
        from app.collections.tokens import CHARS_PER_TOKEN

        return page(
            request,
            "newcollection.html",
            "newcollection",
            candidates=candidates,
            chars_per_token=CHARS_PER_TOKEN,
        )

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
            return page(request, "notfound.html", "collections", what="collection", status_code=404)
        return page(
            request,
            "collection.html",
            "collections",
            collection=collection,
            builds=builds,
        )

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    def review(
        request: Request,
        job_id: str,
        video: str = "",
        frame: str = "",
        picture: str = "",
    ) -> Response:
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

            # `picture` is the number printed under the image and typed into the
            # jump box, counting from one. `frame` is the internal index counting
            # from zero, still accepted so older links keep working.
            requested = whole_number(picture, 1) - 1 if picture else whole_number(frame, 0)
            position = max(0, min(requested, len(frames) - 1)) if frames else 0
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

            # The numbered copies are only made when a job is going to describe
            # its pictures. Offering the toggle without them gives a broken
            # image and a caption describing something that never happened.
            has_numbered = bool(
                video_dir is not None
                and (video_dir / "frames_api").is_dir()
                and any((video_dir / "frames_api").glob("*.jpg"))
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
                # Surfaced where the problem is actually noticed. The rerun that
                # fixes it lives one click away rather than on a screen the user
                # would have to already know about.
                low_confidence_count=sum(
                    1
                    for entry in descriptions.values()
                    if str(entry.get("confidence", "")).lower() == "low"
                ),
                nearby=nearby,
                transcript_count=len(transcript),
                frames_relative=frames_relative,
                has_numbered=has_numbered,
            )
        finally:
            connection.close()

    @app.get("/jobs/{job_id}/frames", response_class=HTMLResponse)
    def contact_sheet(
        request: Request, job_id: str, video: str = "", page_no: str = "0"
    ) -> Response:
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
            current_page = max(0, min(whole_number(page_no), total_pages - 1))
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
        from app.services.receipt import Receipt, build_receipt

        files: list[dict[str, Any]] = []
        reclaimable: list[dict[str, Any]] = []
        busy = ""
        total_bytes = 0
        root = current().output_root
        receipt = Receipt()

        try:
            if connection is not None:
                receipt = build_receipt(connection, root, job_id)
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

                busy = in_flight_reason(connection, job_id)
                reclaimable = [
                    {
                        "key": group.key,
                        "label": group.label,
                        "consequence": group.consequence,
                        "remakeable": group.remakeable,
                        "size": status_module.format_bytes(group.total_bytes),
                    }
                    for group in _reclaimable_groups(connection, job_id, root)
                    if group.present
                ]
        finally:
            if connection is not None:
                connection.close()

        return page(
            request,
            "outputs.html",
            "dashboard",
            job_id=job_id,
            files=files,
            receipt=receipt,
            reclaimable=reclaimable,
            reclaim_blocked=busy,
            total_size=status_module.format_bytes(total_bytes),
        )

    def _video_dirs(connection: sqlite3.Connection, job_id: str, root: Any) -> list[Path]:
        """Every version's folder for a job, active or not.

        Not only the active version: an earlier version's pictures take exactly
        as much disk as the current one's, and a cleanup screen that silently
        skipped them would report less space than it could actually free.
        """
        if root is None:
            return []
        rows = connection.execute(
            "SELECT output_dir FROM job_videos WHERE job_id = ?", (job_id,)
        ).fetchall()
        return [Path(root) / row["output_dir"] for row in rows if row["output_dir"]]

    def _reclaimable_groups(connection: sqlite3.Connection, job_id: str, root: Any) -> list[Any]:
        from app.services.cleanup import removable_groups

        return removable_groups(_video_dirs(connection, job_id, root))

    @app.post("/jobs/{job_id}/files/remove")
    def remove_files_route(
        request: Request, job_id: str, group: list[str] = Form(default=[])
    ) -> Response:
        """Remove the chosen kinds of file, keeping the rest.

        Deleting a job used to be all-or-nothing. On a real job that made 91 MB
        of scratch audio and 185 MB of pictures the same decision as the 1 MB
        document the whole run exists to produce.
        """
        from app.services.cleanup import remove_groups

        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            busy = in_flight_reason(connection, job_id)
            if busy:
                return render_job(request, job_id, problems=[busy], status_code=409)

            chosen = {name for name in group if name}
            if not chosen:
                return RedirectResponse(f"/jobs/{job_id}/outputs", status_code=303)

            root = current().output_root
            if root is None:
                return RedirectResponse("/launch", status_code=303)
            result = remove_groups(
                _video_dirs(connection, job_id, root), chosen, output_root=Path(root)
            )

            if result.removed:
                connection.execute(
                    "INSERT INTO events (job_id, level, kind, message, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        job_id,
                        "info",
                        "files_removed",
                        f"Removed {', '.join(result.removed).lower()} to free "
                        f"{status_module.format_bytes(result.freed_bytes)}. "
                        "The job and everything else it produced are kept.",
                        utc_now(),
                    ),
                )
            for problem in result.problems:
                connection.execute(
                    "INSERT INTO events (job_id, level, kind, message, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (job_id, "warning", "files_removed", problem, utc_now()),
                )
        finally:
            connection.close()
        return RedirectResponse(f"/jobs/{job_id}/outputs", status_code=303)

    # ── Actions ───────────────────────────────────────────────────────────

    @app.post("/jobs")
    def create_job_route(
        request: Request,
        name: str = Form(""),
        paths: str = Form(""),
        interval: str = Form("2000"),
        provider: str = Form("none"),
        service: str = Form(""),
        model_id: str = Form(""),
    ) -> Response:
        from app.services.jobs import create_job, parse_paths

        connection = connect()
        if connection is None:
            return RedirectResponse("/launch", status_code=303)

        interval_ms = _interval_from_form(interval)

        # "external" is the design's grouping for "a service you have an account
        # with". Which one is now answered on this screen: the card is the
        # grouping, `service` is the answer. Falling back to the global setting
        # is kept for a submission that names no service, so an older bookmark
        # or a form without JavaScript still behaves as it used to rather than
        # silently producing a job that describes nothing.
        resolved = provider
        if provider == "external":
            chosen = service.strip()
            if chosen and chosen in PROVIDERS:
                resolved = chosen
            else:
                fallback = current().visual_analysis.provider
                resolved = fallback if fallback not in {"none", "ollama_local"} else "none"

        # The model travels with the job, so a later change to the global
        # settings cannot retroactively alter what this job was asked to use.
        chosen_model = model_id.strip() or current().visual_analysis.model_for(resolved)

        try:
            result = create_job(
                connection,
                current(),
                name=name,
                paths=parse_paths(paths),
                interval_ms=interval_ms,
                provider=resolved,
                model_id=chosen_model,
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
                services=describable_services(),
            )
        finally:
            connection.close()

    def job_control(request: Request, job_id: str, act: Any, verb: str) -> Response:
        """Run a pause/resume/cancel and report it when it does not apply.

        All three used to redirect to the job screen whether or not anything had
        happened. Pressing "Pause" on a job that had already finished looked
        exactly like pressing it on one that was running: the page reloaded and
        nothing was different, with nothing to say why. A control that reports
        nothing is indistinguishable from a control that does nothing.
        """
        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)
        try:
            job = connection.execute(
                "SELECT id, status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            if not act(connection, job_id):
                current_label = status_module.present(job["status"]).label
                return render_job(
                    request,
                    job_id,
                    problems=[f"This job cannot be {verb} — it is {current_label.lower()}."],
                    status_code=409,
                )
        finally:
            connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/pause")
    def pause_route(request: Request, job_id: str) -> Response:
        from app.services.jobs import pause_job

        return job_control(request, job_id, pause_job, "paused")

    @app.post("/jobs/{job_id}/resume")
    def resume_route(request: Request, job_id: str) -> Response:
        from app.services.jobs import resume_job

        return job_control(request, job_id, resume_job, "started again")

    @app.post("/jobs/{job_id}/cancel")
    def cancel_route(request: Request, job_id: str) -> Response:
        from app.services.jobs import cancel_job

        return job_control(request, job_id, cancel_job, "stopped")

    @app.post("/jobs/{job_id}/run-next")
    def run_next_route(request: Request, job_id: str) -> Response:
        from app.services.jobs import run_next

        return job_control(request, job_id, run_next, "moved up the queue")

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
        token_limit: str = Form("200000"),
        reserve_tokens: str = Form("20000"),
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

            problems, limit, reserve = collection_numbers(token_limit, reserve_tokens)
            if problems:
                return JSONResponse({"ready": False, "detail": " ".join(problems)})

            collection = transient_collection(
                connection,
                root,
                chosen,
                name="",
                mode=mode,
                token_limit=limit,
                reserve_tokens=reserve,
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
        token_limit: str = Form("200000"),
        reserve_tokens: str = Form("20000"),
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
        from app.collections.tokens import CHARS_PER_TOKEN

        connection = connect()
        root = current().output_root
        if connection is None or root is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            chosen = ordered_selection(order, video, version)
            problems: list[str] = []
            if not name.strip() or not chosen:
                problems.append("Give the collection a name and choose at least one video.")

            number_problems, limit, reserve = collection_numbers(token_limit, reserve_tokens)
            problems.extend(number_problems)

            # Building runs the real packer, so the form takes a moment and gets
            # submitted twice — which used to produce two identical collections
            # with the same name, and no way to tell them apart afterwards. A
            # name already in use is refused whatever caused it: a second press,
            # or genuinely reusing a name that is already taken.
            if name.strip():
                clash = connection.execute(
                    "SELECT id FROM collections WHERE name = ? LIMIT 1", (name.strip(),)
                ).fetchone()
                if clash is not None:
                    problems.append(
                        f"A collection called “{name.strip()}” already exists. "
                        "Give this one a different name, or open the existing one."
                    )

            if problems:
                return page(
                    request,
                    "newcollection.html",
                    "newcollection",
                    candidates=collection_candidates(connection, root),
                    problems=problems,
                    chars_per_token=CHARS_PER_TOKEN,
                    status_code=400,
                )

            collection_id = create_collection(
                connection,
                name=name,
                mode=mode,
                token_limit=limit,
                reserve_tokens=reserve,
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

    @app.post("/settings/notifications")
    def save_notifications_route(
        request: Request, browser: str = Form(""), terminal_bell: str = Form("")
    ) -> Response:
        """How this computer tells you a long job has finished.

        Nothing here reaches off the machine. Ticking the browser option is what
        triggers the permission prompt, in the browser, on the click — asking on
        first run instead would teach the user to press Deny before they knew
        what they were declining.
        """
        from dataclasses import replace

        from app.core.config import NotificationSettings

        candidate = replace(
            current(),
            notifications=NotificationSettings(
                browser=bool(browser), terminal_bell=bool(terminal_bell)
            ),
        )

        problems = commit_settings(candidate)
        if problems:
            return _settings_page(request, problems=problems, draft=candidate)
        return RedirectResponse("/settings?saved=1#telling-you", status_code=303)

    @app.post("/jobs/acknowledge")
    def acknowledge_finished(job_id: str = Form(""), came_from: str = Form("/")) -> Response:
        """Mark finished jobs as seen, so the banner stops.

        With no job named, everything currently finished is acknowledged: the
        banner is a single dismissal for the whole set, and making the user
        dismiss six of them one at a time would be a worse version of no banner.
        """
        connection = connect()
        if connection is not None:
            try:
                if job_id:
                    connection.execute(
                        "UPDATE jobs SET completion_acknowledged_at = ? WHERE id = ?",
                        (utc_now(), job_id),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET completion_acknowledged_at = ?"
                        " WHERE completion_acknowledged_at IS NULL"
                        " AND status IN ('completed', 'completed_with_gaps')",
                        (utc_now(),),
                    )
            finally:
                connection.close()

        # Only ever back to a path on this application: an open redirect on a
        # localhost tool is still an open redirect.
        target = came_from if came_from.startswith("/") and not came_from.startswith("//") else "/"
        return RedirectResponse(target, status_code=303)

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
        base_url: str = Form(""),
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
                # Filed under the provider being saved, leaving every other
                # service's model untouched. Saving Claude's model must not
                # silently become Gemini's.
                models=_with_entry(active.visual_analysis.models, provider, model_id),
                base_urls=_with_entry(
                    active.visual_analysis.base_urls,
                    provider if provider in CUSTOM_ENDPOINT_PROVIDERS else "",
                    base_url,
                ),
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

    @app.post("/api/providers/models")
    def list_provider_models(provider: str = Form(""), base_url: str = Form("")) -> JSONResponse:
        """Ask a service what models it offers.

        User-initiated, never on load. It sends no picture and no video — only a
        request for a catalogue — and it doubles as the key check, because a
        service that answers with a catalogue is one the key works against.

        This is the alternative to a dropdown baked into the build, which would
        be wrong the week a provider renames something, and to free text, which
        makes the user the validator. Asking is the only answer that cannot
        drift.
        """
        from app.core.redaction import redacted_exception_text
        from app.credentials.store import credential_status
        from app.providers.base import ProviderError

        active = current()

        if provider == "ollama_local":
            from app.providers.ollama_local import OllamaLocalProvider

            try:
                models = OllamaLocalProvider(endpoint=active.ollama.endpoint).list_models()
            except Exception as error:
                return JSONResponse({"ok": False, "detail": redacted_exception_text(error)})
            return JSONResponse(
                {
                    "ok": True,
                    "models": models,
                    "detail": ""
                    if models
                    else "The local runtime is reachable but has no models installed yet.",
                }
            )

        if provider not in PROVIDERS:
            return JSONResponse({"ok": False, "detail": "Unknown service."})

        if not credential_status(provider).present:
            # Checked before the call rather than after a confusing 401: the key
            # is the thing the user has to act on, and saying so costs nothing.
            return JSONResponse(
                {"ok": False, "detail": "Save a key for this service first, then check again."}
            )

        address = base_url.strip() or active.visual_analysis.base_url_for(provider)
        if provider in CUSTOM_ENDPOINT_PROVIDERS and not address:
            return JSONResponse(
                {"ok": False, "detail": "This service needs an address before it can be checked."}
            )

        try:
            adapter = build_provider(provider, **({"base_url": address} if address else {}))
            models = adapter.list_models()
        except ProviderError as error:
            return JSONResponse({"ok": False, "detail": str(error)})
        except Exception as error:
            return JSONResponse({"ok": False, "detail": redacted_exception_text(error)})

        return JSONResponse(
            {
                "ok": True,
                "models": models,
                "detail": "" if models else "The service replied but listed no models.",
            }
        )

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
        from app.core.config import CUSTOM_ENDPOINT_PROVIDERS as CUSTOM
        from app.core.config import PROVIDER_LABELS
        from app.credentials.store import ENV_VARS, credential_status, secure_store_available

        shown = draft or current()
        credentials = [
            {
                "provider": name,
                "label": PROVIDER_LABELS[name],
                "status": credential_status(name),
                "model": shown.visual_analysis.model_for(name),
                "base_url": shown.visual_analysis.base_urls.get(name, ""),
                "needs_address": name in CUSTOM,
            }
            for name in (
                "anthropic",
                "google",
                "openai",
                "openai_compatible",
                "anthropic_compatible",
            )
        ]
        return page(
            request,
            "settings.html",
            "settings",
            # Said once, for this machine, rather than enumerating both cases and
            # leaving the reader to work out which applies to them. Someone who
            # reads "or in the environment for this session only" reasonably
            # concludes they will be retyping keys every morning.
            secure_store=secure_store_available(),
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
                # Averaged per stage, never summed across them. Stages count in
                # different units — the transcript measures seconds of video and
                # the others count pictures — so adding items_done together once
                # produced a total of pictures-plus-seconds, a number that means
                # nothing and moved at two different rates.
                totals = connection.execute(
                    "SELECT AVG(CASE"
                    "   WHEN s.status IN ('completed', 'completed_with_gaps') THEN 100.0"
                    "   WHEN COALESCE(s.items_total, 0) > 0"
                    "     THEN MIN(100.0, s.items_done * 100.0 / s.items_total)"
                    "   ELSE 0.0 END) AS percent"
                    " FROM stage_runs s"
                    " JOIN job_videos v ON v.id = s.job_video_id"
                    " WHERE v.job_id = ? AND v.is_active_version = 1"
                    # Latest attempt only, for the same reason the strip orders
                    # by attempt: an abandoned earlier try sits at 0% forever and
                    # would drag the average down for the rest of the job.
                    " AND s.attempt = (SELECT MAX(s2.attempt) FROM stage_runs s2"
                    "   WHERE s2.job_video_id = s.job_video_id AND s2.stage = s.stage)",
                    (row["id"],),
                ).fetchone()
                percent = round(totals["percent"] or 0.0)
                jobs.append(
                    {
                        "id": row["id"],
                        # Named so a notification can say which job finished
                        # rather than just that one did.
                        "name": row["name"],
                        "status": row["status"],
                        "label": status_module.present(row["status"]).label,
                        "updated": status_module.format_relative(row["updated_at"]),
                        "percent": percent,
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

    # ── The sample clip (F22) ─────────────────────────────────────────────

    @app.post("/sample")
    def try_the_sample(request: Request) -> Response:
        """Draw a sample clip and start a job on it.

        A fresh install has nothing to look at and no way to see what the
        product makes without supplying a video and waiting. Nothing is
        downloaded and nothing is shipped in the repository: FFmpeg draws the
        clip here, and it is labelled as generated test footage everywhere it
        appears — presenting it as a real recording would be a claim about where
        data came from, which is worse than ordinary placeholder content.
        """
        from app.services.jobs import create_job
        from app.services.sample import SampleError, generate_sample

        active = current()
        root = active.output_root
        if root is None:
            return RedirectResponse("/launch", status_code=303)

        connection = connect()
        if connection is None:
            open_database(root).close()
            connection = connect()
        if connection is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            # Drawing the clip takes a couple of seconds with no feedback, so a
            # second press is the expected thing for a person to do — and it used
            # to make a second identical job, doubling the work on the one screen
            # a first-time user is most likely to be watching. An unfinished
            # sample already in the queue is the answer to "start the sample".
            waiting = connection.execute(
                "SELECT id FROM jobs WHERE name = ? AND status IN"
                " ('ready','preparing','transcribing','analyzing','waiting_retry')"
                " ORDER BY created_at DESC LIMIT 1",
                ("Sample — generated test footage",),
            ).fetchone()
            if waiting is not None:
                return RedirectResponse(f"/jobs/{waiting['id']}", status_code=303)

            try:
                clip = generate_sample(root)
            except SampleError as error:
                return page(
                    request,
                    "launch.html",
                    "launch",
                    report=run_doctor(active),
                    problems=[str(error)],
                )

            created = create_job(
                connection,
                active,
                name="Sample — generated test footage",
                paths=[clip.path],
                # Every three seconds, which is 20 pictures from the minute.
                # Found by running it: at one second the frames and transcript
                # still finish in under a minute, but describing 60 pictures
                # through a local model takes about eighteen, and a first
                # impression that spends a quarter of an hour on a test clip is
                # not one worth making. Twenty is still enough for the viewer
                # and the contact sheet to look like something.
                interval_ms=3000,
            )
            if created.problems:
                return page(
                    request,
                    "launch.html",
                    "launch",
                    report=run_doctor(active),
                    problems=created.problems,
                )

            connection.execute(
                "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
                (created.job_id, "info", "sample_created", clip.detail, utc_now()),
            )
            return RedirectResponse(f"/jobs/{created.job_id}", status_code=303)
        finally:
            connection.close()

    # ── Targeted reruns (F12) ─────────────────────────────────────────────

    @app.get("/jobs/{job_id}/rerun", response_class=HTMLResponse)
    def rerun_screen(request: Request, job_id: str, video: str = "") -> Response:
        """Choose what to do again, and see what it would involve first.

        The scopes are worked out from what the previous version actually
        recorded, so a choice that would select nothing says so here rather than
        producing an empty version that looks like work.
        """
        from app.pipeline.rerun import RerunError, RerunScope, plan_rerun, version_summaries

        connection = connect()
        root = current().output_root
        if connection is None or root is None:
            return RedirectResponse("/launch", status_code=303)

        try:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            videos = connection.execute(
                "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                " ORDER BY sequence",
                (job_id,),
            ).fetchall()
            if not videos:
                return page(request, "notfound.html", "dashboard", what="video", status_code=404)

            chosen = next((v for v in videos if v["id"] == video), videos[0])

            active = current()
            plans = []
            for scope in RerunScope:
                try:
                    plans.append(plan_rerun(connection, chosen["id"], root, scope=scope))
                except RerunError as error:
                    # One scope that cannot be worked out must not take the
                    # screen down with it — the others are still choosable.
                    logger.warning("Could not plan the %s rerun: %s", scope, error)

            from app.providers.costs import estimate_cost

            estimates = {
                plan.scope: estimate_cost(active.visual_analysis.provider, plan.frame_count)
                for plan in plans
            }

            # How long, alongside how much. Cost was already shown; time was not,
            # and on a local model time is the whole price — a fifteen-hundred
            # picture video costs nothing and takes most of a day. Someone should
            # learn that before pressing the button, not four hours into it.
            from app.services.estimate import estimate_stage

            durations: dict[Any, str] = {}
            for plan in plans:
                predicted = estimate_stage(
                    connection,
                    "visual",
                    plan.frame_count,
                    model_id=active.visual_analysis.model_id,
                )
                # Blank when this machine has not run enough of them to know.
                # An invented figure is worse than none: someone plans an
                # afternoon around it.
                durations[plan.scope] = (
                    status_module.format_span(predicted.seconds) if predicted.known else ""
                )

            return page(
                request,
                "rerun.html",
                "dashboard",
                job=job,
                videos=videos,
                chosen=chosen,
                plans=plans,
                estimates=estimates,
                durations=durations,
                versions=version_summaries(connection, chosen["id"], root),
                provider=active.visual_analysis.provider,
                descriptions_on=active.visual_analysis.enabled
                and active.visual_analysis.provider != "none",
                model_id=active.visual_analysis.model_id,
                budget_limit=active.visual_analysis.budget.hard_limit_usd,
            )
        finally:
            connection.close()

    @app.post("/jobs/{job_id}/rerun")
    def start_rerun_route(
        request: Request,
        job_id: str,
        video_id: str = Form(""),
        scope: str = Form("all"),
        start: str = Form(""),
        end: str = Form(""),
        confirmed: str = Form(""),
    ) -> Response:
        """Queue a new version. The previous one is never touched."""
        from app.pipeline.rerun import RerunError, plan_rerun, start_rerun

        connection = connect()
        root = current().output_root
        if connection is None or root is None:
            return RedirectResponse("/launch", status_code=303)

        active = current()
        try:
            if not active.visual_analysis.enabled or active.visual_analysis.provider == "none":
                return RedirectResponse("/settings#describing", status_code=303)

            try:
                plan = plan_rerun(
                    connection,
                    video_id,
                    root,
                    scope=scope,
                    start=int(start) if start.strip() else None,
                    end=int(end) if end.strip() else None,
                )
                # A run that costs money is confirmed against a stated figure
                # before anything is sent. A local run has no provider charge,
                # so making the user confirm one would be ceremony.
                from app.providers.costs import NO_CHARGE_PROVIDERS

                if active.visual_analysis.provider not in NO_CHARGE_PROVIDERS and not confirmed:
                    return RedirectResponse(
                        f"/jobs/{job_id}/rerun?video={video_id}", status_code=303
                    )

                start_rerun(
                    connection,
                    plan,
                    output_root=root,
                    provider=active.visual_analysis.provider,
                    model_id=active.visual_analysis.model_id,
                )
            except RerunError as error:
                from app.pipeline.rerun import version_summaries

                videos = connection.execute(
                    "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
                    " ORDER BY sequence",
                    (job_id,),
                ).fetchall()
                job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                chosen = next(
                    (v for v in videos if v["id"] == video_id), videos[0] if videos else None
                )
                return page(
                    request,
                    "rerun.html",
                    "dashboard",
                    job=job,
                    videos=videos,
                    chosen=chosen,
                    plans=[],
                    estimates={},
                    versions=(version_summaries(connection, chosen["id"], root) if chosen else []),
                    provider=active.visual_analysis.provider,
                    descriptions_on=True,
                    problems=[str(error)],
                )
        finally:
            connection.close()

        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/versions/activate")
    def activate_version_route(job_id: str, video_id: str = Form("")) -> Response:
        """Switch which version the rest of the product uses.

        Nothing is deleted and nothing is rewritten — a collection that pinned
        another version goes on pointing at exactly the same bytes.
        """
        from app.pipeline.rerun import RerunError, make_active

        connection = connect()
        if connection is None:
            return RedirectResponse("/launch", status_code=303)
        try:
            try:
                make_active(connection, video_id)
            except RerunError:
                logger.warning("Could not activate version %s", video_id)
        finally:
            connection.close()
        return RedirectResponse(f"/jobs/{job_id}/rerun?video={video_id}", status_code=303)

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

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request) -> Response:
        """Where the badge in the header is cashed.

        The badge has promised "nothing is uploaded" since the first screen was
        built, and there was nowhere to click to find out what that rested on.
        A claim with no way to check it is a slogan.

        Two halves: mechanisms, which are true of the program whatever you have
        done with it, and state, which is read live from this machine. Nothing
        here is written that cannot be checked.
        """
        from app.core.config import BIND_HOST, PROVIDER_LABELS
        from app.credentials.store import credential_status

        active = current()
        provider = active.visual_analysis.provider
        state: dict[str, Any] = {
            "describing": provider not in {"none", ""},
            "default_service": PROVIDER_LABELS.get(provider, provider),
            "bind_address": f"{BIND_HOST}:{active.port}",
            "pictures_sent": 0,
            "jobs_that_sent": 0,
            "videos_processed": 0,
            "services_with_keys": [
                PROVIDER_LABELS[name]
                for name in PROVIDER_LABELS
                if name != "ollama_local" and credential_status(name).present
            ],
        }

        connection = connect()
        if connection is not None:
            try:
                # Counted from the work that actually ran, not from what was
                # configured: a job set to a service and cancelled before its
                # first batch sent nothing, and must not be reported as if it had.
                row = connection.execute(
                    "SELECT COALESCE(SUM(s.items_done), 0) AS pictures,"
                    " COUNT(DISTINCT j.id) AS jobs FROM stage_runs s"
                    " JOIN job_videos v ON v.id = s.job_video_id"
                    " JOIN jobs j ON j.id = v.job_id"
                    " WHERE s.stage = 'visual' AND s.items_done > 0"
                    " AND j.visual_provider NOT IN ('none', '', 'ollama_local')"
                ).fetchone()
                state["pictures_sent"] = int(row["pictures"] or 0)
                state["jobs_that_sent"] = int(row["jobs"] or 0)
                state["videos_processed"] = int(
                    connection.execute("SELECT COUNT(*) AS n FROM job_videos").fetchone()["n"] or 0
                )
            except sqlite3.Error:
                # A page about trust must not fail to render because a count
                # could not be read. The mechanisms below it are the substance.
                logger.warning("Could not read privacy counters")
            finally:
                connection.close()

        return page(request, "privacy.html", "", privacy=state)

    # ── What a job would produce, before it is started ────────────────────

    @app.post("/api/plan")
    def plan_job(
        paths: str = Form(""),
        interval: str = Form("2000"),
        provider: str = Form("none"),
        service: str = Form(""),
        model_id: str = Form(""),
    ) -> JSONResponse:
        """Probe the chosen videos and report what starting would involve.

        Creates nothing and starts nothing. It runs the same preflight the create
        path runs, so a problem reported here is the problem that would actually
        stop the job rather than a second opinion.

        This exists because the screen said nothing until after the decision. The
        moment a user is weighing whether to trust "nothing is uploaded" was
        exactly the moment the interface stayed quiet about it.
        """
        from app.services.jobs import parse_paths
        from app.services.plan import build_plan

        connection = connect()
        if connection is None:
            return JSONResponse({"ok": False, "problems": ["No output folder is set yet."]})

        interval_ms = _interval_from_form(interval)

        # "external" is the card; `service` is which one. Same resolution the
        # create route does, kept in step by using the same helper.
        resolved = provider
        if provider == "external":
            chosen = service.strip()
            resolved = chosen if chosen in PROVIDERS else "none"

        try:
            plan = build_plan(
                connection,
                current(),
                paths=parse_paths(paths),
                interval_ms=interval_ms,
                provider=resolved,
                model_id=model_id.strip(),
            )
            return JSONResponse(plan.as_dict())
        finally:
            connection.close()

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
    def rename_job(request: Request, job_id: str, name: str = Form("")) -> Response:
        """Rename a job, or say why not.

        An empty name used to be dropped on the floor: the dialog closed, the
        page reloaded, the name was unchanged and nothing explained it. A refusal
        the user cannot see is the same defect as a control wired to nothing.
        """
        from app.services.jobs import MAX_NAME_LENGTH

        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)
        try:
            job = connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            wanted = name.strip()
            if not wanted:
                return render_job(
                    request,
                    job_id,
                    problems=["The job needs a name. Nothing was changed."],
                    status_code=400,
                )
            if len(wanted) > MAX_NAME_LENGTH:
                return render_job(
                    request,
                    job_id,
                    problems=[
                        f"That name is {len(wanted)} characters. Keep it to "
                        f"{MAX_NAME_LENGTH} or fewer so it stays readable in the list "
                        "and in the browser tab."
                    ],
                    status_code=400,
                )

            connection.execute(
                "UPDATE jobs SET name = ?, updated_at = ? WHERE id = ?",
                (wanted, utc_now(), job_id),
            )
        finally:
            connection.close()
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/delete")
    def delete_job(request: Request, job_id: str, remove_files: str = Form("")) -> Response:
        """Delete a job. Files are removed only when explicitly asked for.

        The default keeps the output: the database row is cheap to recreate and
        the artifacts are the expensive part. Removing them has to be a separate,
        deliberate choice.

        **The row goes first, and the files only once it has committed.** This
        used to be the other way round, which meant an expensive folder was
        erased before a delete that could still fail — and it did fail, every
        time a collection cited the job, because ``collection_sources`` holds an
        unqualified reference to ``job_videos`` on purpose. The rollback then put
        the row back, so the dashboard listed a job whose every file was gone,
        above an error page promising nothing had been affected. Ordering the
        reversible step first means the worst case is an orphaned folder, and
        "Bring in earlier work" already finds those.
        """
        import shutil as shutil_module

        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            busy = in_flight_reason(connection, job_id)
            if busy:
                return render_job(request, job_id, problems=[busy], status_code=409)

            cited_by = collections_using(connection, job_id)
            if cited_by:
                listed = ", ".join(cited_by)
                return render_job(
                    request,
                    job_id,
                    problems=[
                        f"This job's video is used by {listed}. A collection keeps the "
                        "exact version it was built from, so deleting the job would "
                        "leave it citing something that no longer exists. Delete "
                        f"{'those collections' if len(cited_by) > 1 else 'that collection'} "
                        "first, or keep this job."
                    ],
                    status_code=409,
                )

            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            connection.commit()

            root = current().output_root
            if remove_files and root is not None:
                job_dir = Path(root) / job_folder(job)
                try:
                    resolved = job_dir.resolve()
                    resolved.relative_to(Path(root).resolve())
                except ValueError:
                    logger.error("Refused to delete outside the output folder")
                else:
                    if resolved.is_dir():
                        shutil_module.rmtree(resolved, ignore_errors=True)
                        logger.info("Removed the output folder for job %s", job_id[:8])
        finally:
            connection.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/{job_id}/describe")
    def describe_now(request: Request, job_id: str, video_id: str = Form("")) -> Response:
        """Add descriptions to a video that was processed without them.

        The interface has always told the user this was possible. Until now
        nothing did it — a promise the product made and could not keep.

        Two things are checked before anything is written. The job must exist,
        because the event this route records is keyed to it and the database
        rejects an event for a job that is gone — which surfaced as a 500 rather
        than as "no such job". And the job must not be in flight, because the
        route's whole mechanism is to set the status back to ``ready``: doing
        that to a job the worker is part-way through queues it a second time, and
        on a paid provider the same frames get described — and billed — twice.
        """
        active = current()
        connection = connect()
        if connection is None:
            return RedirectResponse("/", status_code=303)

        try:
            job = connection.execute(
                "SELECT id, status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                return page(request, "notfound.html", "dashboard", what="job", status_code=404)

            busy = in_flight_reason(connection, job_id)
            if busy:
                return render_job(request, job_id, problems=[busy], status_code=409)

            was_stopped = job["status"] in ("cancelled", "paused")

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
            # The provider moves with the request. The worker honours the job's
            # own recorded choice, so a job created without descriptions would
            # otherwise be queued and then skip the only stage being asked for.
            connection.execute(
                "UPDATE jobs SET status = 'ready', visual_provider = ?, visual_model_id = ?,"
                " updated_at = ? WHERE id = ?",
                (
                    active.visual_analysis.provider,
                    active.visual_analysis.model_id,
                    utc_now(),
                    job_id,
                ),
            )
            connection.execute(
                "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
                (
                    job_id,
                    "info",
                    "describe_requested",
                    "Descriptions requested. The pictures and transcript are kept — "
                    "only the descriptions are produced."
                    + (
                        " This job had been stopped; asking for descriptions has put it "
                        "back in the queue."
                        if was_stopped
                        else ""
                    ),
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

    @app.post("/api/providers/key")
    def save_key_json(provider: str = Form(""), key: str = Form("")) -> JSONResponse:
        """Store a key and report the result as data rather than as a redirect.

        The same storage as the settings form — this is not a second way to keep
        a key, only a second way to ask. It exists so the new-job screen can
        accept a key without navigating away from a form the user has already
        half filled in, which is the situation where being sent to Settings and
        back loses the most work.

        Write-only here too: the response says whether a key is now present, and
        never what it is.
        """
        from app.credentials.store import CredentialError, credential_status, set_credential

        if not key.strip():
            return JSONResponse({"ok": False, "detail": "Paste a key first."})
        try:
            set_credential(provider, key)
        except CredentialError as error:
            return JSONResponse({"ok": False, "detail": str(error)})

        status = credential_status(provider)
        return JSONResponse({"ok": True, "present": status.present, "detail": status.detail})

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


def _stage_eta(started_at: str | None, done: int, total: int, carried: int = 0) -> str:
    """How much longer, from how long it has taken so far.

    Measured, never assumed. Rate comes from this run on this machine, so it
    reflects the actual model, the actual file and whatever else the computer is
    doing — none of which a hardcoded constant could know. Withheld until 2% is
    done, because an estimate drawn from three seconds of a nine-hour stage is
    noise presented as information.
    """
    if not started_at or not total or done <= 0:
        return ""

    # Gated on how much *evidence* there is, not on how far through the work it
    # represents. A 2% floor was fine on a twenty-picture sample and useless on
    # the stage that needs this most: 2% of 1,488 pictures is thirty of them, so
    # the number stayed hidden for the first eleven minutes of a nine-hour run —
    # exactly the stretch where someone is deciding whether to wait.
    #
    # Forty-five seconds of a stage is enough to say something honest about it.
    # The figure is coarse on purpose (see format_span) and refines every couple
    # of seconds as the page updates, so an early estimate corrects itself in
    # view rather than standing as a promise.
    elapsed = status_module.elapsed_seconds(started_at)
    if elapsed is None or elapsed < 45:
        return ""

    if done >= total:
        return ""

    # Rate comes from work this run performed, not from everything the bar
    # counts. A resumed stage inherits hundreds of pictures that were described
    # on an earlier attempt and cost this one no time at all; dividing elapsed
    # time by the inherited total said "about 36 minutes left" when the honest
    # answer was nearly eight hours. The bar is right to show the inherited work
    # — it is genuinely done — but the clock must not be measured against it.
    performed = done - max(0, carried)
    if performed <= 0:
        return ""

    seconds_each = elapsed / performed
    remaining = seconds_each * (total - done)
    if remaining < 30:
        return "nearly done"
    # No "about" here. format_span already hedges where hedging is warranted —
    # "about 7½ hours", but a flat "5 minutes" — and prepending another one
    # produced "about about 7½ hours left" on screen. The qualifier belongs
    # wherever the imprecision is decided, which is there and not here.
    return f"{status_module.format_span(remaining)} left"


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

    # Each stage counts in the unit its user thinks in. Transcription measures
    # seconds of the video covered, so its figures are clock positions rather
    # than a tally; everything else counts things.
    def as_pictures(done: int, total: int) -> str:
        return f"{done:,} of {total:,}"

    def as_clock(done: int, total: int) -> str:
        return f"{status_module.format_duration(done)} of {status_module.format_duration(total)}"

    formats = {"transcribe": as_clock}

    # Ordered by attempt, which is the sequence `_begin_stage` maintains, and
    # never by id — ids are random hex, so "the last row" was whichever attempt
    # happened to sort highest. A retried stage then showed a dead attempt: a
    # worker restarted mid-description left attempt 1 abandoned at no total, and
    # the screen reported that while attempt 2 ran correctly beside it. The
    # display was reading a corpse.
    rows = connection.execute(
        "SELECT stage, status, items_total, items_done, items_skipped, started_at"
        " FROM stage_runs"
        " WHERE job_video_id = ? ORDER BY attempt",
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
            progress.append(
                {"label": label, "percent": 0, "detail": detail, "state": "ready", "eta": ""}
            )
            continue

        total = row["items_total"] or 0
        done = row["items_done"] or 0
        eta = ""
        indeterminate = False

        if row["status"] in {"completed", "completed_with_gaps"}:
            percent, detail = 100, "Done"
        elif total:
            percent = min(100, int(done * 100 / total))
            detail = formats.get(stage, as_pictures)(done, total)
            if row["status"] == "running":
                eta = _stage_eta(row["started_at"], done, total, row["items_skipped"] or 0)
        elif row["status"] == "running":
            # Running, but nothing to divide by yet — the stage has not declared
            # its size. This used to print the raw database word "running" at 0%
            # next to properly written cells, which read as both broken and
            # finished-at-nothing. Say it is working and let the bar say it
            # cannot yet say how far.
            percent, detail, indeterminate = 0, "Working", True
        else:
            percent, detail = 0, row["status"].replace("_", " ").capitalize()

        progress.append(
            {
                "label": label,
                "percent": percent,
                "detail": detail,
                "state": row["status"],
                "eta": eta,
                "indeterminate": indeterminate,
            }
        )
    return progress
