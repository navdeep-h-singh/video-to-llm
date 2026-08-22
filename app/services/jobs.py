"""Creating and controlling jobs.

The interface calls into here; so does the CLI. Keeping the logic in one place
means the two cannot drift into disagreeing about what "pause" does.

Pause, resume, and cancel all **preserve valid output**. Cancelling a job does
not delete the frames, transcripts, or descriptions it already produced — those
cost time and possibly money, and the user asked to stop, not to undo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings
from app.core.db import new_id, utc_now
from app.core.logging import get_logger
from app.pipeline.preflight import MAX_VIDEOS_PER_JOB, PreflightReport, preflight

logger = get_logger(__name__)

#: States a job can be paused from. A finished job has nothing to pause.
PAUSABLE = frozenset({"ready", "preparing", "transcribing", "analyzing", "waiting_retry"})
RESUMABLE = frozenset({"paused"})
CANCELLABLE = frozenset(
    {"draft", "ready", "preparing", "transcribing", "analyzing", "waiting_retry", "paused"}
)

#: States a job still has work ahead of it in, and can therefore be moved up the
#: queue from. A finished job has no place in a queue, and a paused one is not
#: waiting for the worker — it is waiting for the user.
QUEUEABLE = ("ready", "preparing", "transcribing", "analyzing", "waiting_retry")

#: The same list as SQL, written out so the queries below stay plain literals.
#: `test_worker_queue.py` asserts the two agree.
_QUEUEABLE_SQL = "('ready', 'preparing', 'transcribing', 'analyzing', 'waiting_retry')"


#: Longest job name accepted. Names are shown in the list, on the job screen,
#: in the collection picker, and in the browser tab title — an uncapped name
#: turned a two-thousand-character paste into a two-thousand-character tab.
MAX_NAME_LENGTH = 120


class JobError(RuntimeError):
    pass


@dataclass
class JobCreation:
    job_id: str | None = None
    report: PreflightReport | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.job_id is not None and not self.problems


def parse_paths(raw: str) -> list[Path]:
    """One path per line, blanks and comments ignored.

    Surrounding quotes are stripped because dragging a file into a terminal —
    the way most people get a path with spaces — wraps it in them.
    """
    paths: list[Path] = []
    for line in (raw or "").splitlines():
        candidate = line.strip().strip("'\"")
        if not candidate or candidate.startswith("#"):
            continue
        paths.append(Path(candidate).expanduser())
    return paths


def create_job(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    name: str,
    paths: list[Path],
    interval_ms: int | None = None,
    provider: str = "none",
    model_id: str = "",
    start_immediately: bool = True,
) -> JobCreation:
    """Preflight, then register the job. Nothing is processed here.

    The job is created in `ready` and the worker picks it up. Creating and
    running are deliberately separate: the interface stays responsive and the
    worker keeps ownership of the work.
    """
    result = JobCreation()

    if not name.strip():
        result.problems.append("The job needs a name.")
        return result

    if len(name.strip()) > MAX_NAME_LENGTH:
        result.problems.append(
            f"That name is {len(name.strip())} characters. Keep it to "
            f"{MAX_NAME_LENGTH} or fewer so it stays readable in the list and in "
            "the browser tab."
        )
        return result

    if len(paths) > MAX_VIDEOS_PER_JOB:
        result.problems.append(
            f"{len(paths)} videos were listed, which is more than the "
            f"{MAX_VIDEOS_PER_JOB} a single job takes."
        )
        return result

    report = preflight(paths, settings, connection=connection, interval_ms=interval_ms)
    result.report = report

    if not report.ok:
        result.problems.extend(report.problems)
        return result

    job_id = new_id()
    resolved_interval = interval_ms or settings.sampling.interval_ms()

    # Decided once, here, and never again. Deriving it from the name at write
    # time would move the folder the moment a job is renamed, orphaning
    # everything already produced — and a rename should stay as cheap as
    # changing a label.
    dirname = unique_dirname(
        Path(settings.output_root) if settings.output_root else Path(),
        slugify_dirname(name),
        fallback=job_id,
    )

    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, visual_model_id, budget_limit_usd, budget_on_limit,"
        " output_dirname, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            name.strip(),
            "ready" if start_immediately else "draft",
            str(settings.output_root),
            resolved_interval,
            provider,
            model_id,
            settings.visual_analysis.budget.hard_limit_usd
            if provider not in {"none", "ollama_local"}
            else None,
            settings.visual_analysis.budget.on_limit,
            dirname,
            utc_now(),
            utc_now(),
        ),
    )

    # The order is the order the user listed them in. Never sorted, never
    # inferred — two recordings from the same morning have no inherent sequence.
    for sequence, check in enumerate(report.accepted):
        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name,"
            " source_sha256, duration_seconds, container, width, height, sequence,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                job_id,
                str(check.path),
                check.path.name,
                check.sha256,
                check.info.duration_seconds if check.info else None,
                check.info.container if check.info else None,
                check.info.width if check.info else None,
                check.info.height if check.info else None,
                sequence,
                "pending",
                utc_now(),
                utc_now(),
            ),
        )

    _record(
        connection,
        job_id,
        f"Job created with {len(report.accepted)} video(s), "
        f"a picture every {resolved_interval / 1000:g} seconds.",
    )
    for warning in report.warnings:
        _record(connection, job_id, warning, level="warning", kind="preflight_warning")

    result.job_id = job_id
    logger.info("Created job %s with %d video(s)", job_id[:8], len(report.accepted))
    return result


def pause_job(connection: sqlite3.Connection, job_id: str) -> bool:
    """Ask a job to stop after the current step. Finished work is kept."""
    row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] not in PAUSABLE:
        return False

    connection.execute(
        "UPDATE jobs SET status = 'paused', updated_at = ? WHERE id = ?",
        (utc_now(), job_id),
    )
    connection.execute(
        "UPDATE job_videos SET status = 'paused', updated_at = ? WHERE job_id = ?"
        " AND status IN ('pending', 'preparing', 'transcribing', 'analyzing')",
        (utc_now(), job_id),
    )
    _record(
        connection,
        job_id,
        "Paused. Everything finished so far is kept, and the job picks up from where it stopped.",
    )
    return True


def run_next(connection: sqlite3.Connection, job_id: str) -> bool:
    """Move a job to the front of the queue. False when it has no queue to be in.

    The worker runs one job at a time and, until this existed, took the oldest
    `ready` job and did not come back until every video in it was finished. That
    is fine when the queue is short and ruinous when it is not: a thirteen-video
    job sat untouched for hours behind a single video being described locally,
    and there was no way to say "do mine first" short of cancelling.

    Already at the front is success, not a failure. The user asked for this job
    to run next; it is running next. Reporting "cannot" for a request that is
    already satisfied is the kind of answer that makes people press the button
    again.
    """
    row = connection.execute("SELECT status, priority FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] not in QUEUEABLE:
        return False

    highest = connection.execute(
        "SELECT COALESCE(MAX(priority), 0) AS top FROM jobs WHERE id <> ? AND status IN"
        " ('ready', 'preparing', 'transcribing', 'analyzing', 'waiting_retry')",
        (job_id,),
    ).fetchone()

    # Strictly above everything else waiting, so ties never leave the answer to
    # `created_at` when the user has been explicit.
    if row["priority"] > int(highest["top"]):
        return True

    connection.execute(
        "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?",
        (int(highest["top"]) + 1, utc_now(), job_id),
    )
    _record(connection, job_id, "Moved to the front of the queue. This job runs next.")
    return True


def queue_order(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Jobs waiting for or holding the worker, in the order they will run."""
    return connection.execute(
        "SELECT id, name, status, priority FROM jobs WHERE status IN"
        " ('ready', 'preparing', 'transcribing', 'analyzing', 'waiting_retry')"
        " ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def resume_job(connection: sqlite3.Connection, job_id: str) -> bool:
    """Put a paused job back in the queue."""
    row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] not in RESUMABLE:
        return False

    connection.execute(
        "UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?",
        (utc_now(), job_id),
    )
    connection.execute(
        "UPDATE job_videos SET status = 'pending', updated_at = ? WHERE job_id = ?"
        " AND status = 'paused'",
        (utc_now(), job_id),
    )
    _record(connection, job_id, "Started again from where it stopped.")
    return True


def cancel_job(connection: sqlite3.Connection, job_id: str) -> bool:
    """Stop a job for good, keeping everything it already produced.

    Cancelling is not undoing. Frames, transcripts, and descriptions already on
    disk cost time and possibly money; they stay, and the job records that it
    was stopped deliberately.
    """
    row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["status"] not in CANCELLABLE:
        return False

    connection.execute(
        "UPDATE jobs SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE id = ?",
        (utc_now(), utc_now(), job_id),
    )
    connection.execute(
        "UPDATE job_videos SET status = 'cancelled', updated_at = ? WHERE job_id = ?"
        " AND status NOT IN ('completed', 'completed_with_gaps')",
        (utc_now(), job_id),
    )
    _record(
        connection,
        job_id,
        "Cancelled. Everything already finished has been kept — nothing was thrown away.",
    )
    return True


def _record(
    connection: sqlite3.Connection,
    job_id: str,
    message: str,
    *,
    level: str = "info",
    kind: str = "job_control",
) -> None:
    connection.execute(
        "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
        (job_id, level, kind, message, utc_now()),
    )


# ── Where a job's files live ──────────────────────────────────────────────

#: Names Windows refuses outright, whatever the extension. Checked on every
#: platform: an output root on a shared drive, or a folder later copied to a
#: Windows machine, should not be the moment this is discovered.
RESERVED_DIRNAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

#: Long enough to stay recognisable, short enough that the full path to a frame
#: inside it clears Windows' 260-character limit with room to spare.
MAX_DIRNAME_LENGTH = 60


def slugify_dirname(name: str) -> str:
    """A folder name derived from a job name, safe on every platform.

    Deliberately conservative. The set of characters that are legal in a path is
    much larger than the set that behaves predictably in a shell, a zip file, a
    backup tool, and three filesystems — and this name is going to be handled by
    all of those. Letters, digits, dash and underscore only.

    Returns an empty string when nothing usable survives, which the caller reads
    as "fall back to the identifier" rather than inventing a name.
    """
    import unicodedata

    # Decompose so accented Latin letters keep their base form rather than
    # vanishing: "Sesión" should become "sesion", not "sesin".
    decomposed = unicodedata.normalize("NFKD", name)
    kept = []
    for character in decomposed:
        if unicodedata.combining(character):
            continue
        if character.isascii() and (character.isalnum() or character in "-_"):
            kept.append(character)
        elif character.isspace() or character in "-_.":
            kept.append("-")
        # Anything else — punctuation, emoji, a path separator — is dropped.

    slug = "".join(kept).strip("-_.").lower()
    while "--" in slug:
        slug = slug.replace("--", "-")

    slug = slug[:MAX_DIRNAME_LENGTH].strip("-_.")

    if not slug or slug in RESERVED_DIRNAMES:
        return ""
    return slug


def unique_dirname(root: Path, wanted: str, *, fallback: str) -> str:
    """*wanted*, or *wanted* with a suffix, or *fallback* if it is unusable.

    Two jobs may reasonably share a name — "Tuesday review" happens twice — and
    the second must not be written into the first one's folder. The suffix
    counts up rather than using the identifier, so the common case stays
    readable and only a genuine clash pays for itself.
    """
    if not wanted:
        return fallback

    candidate = wanted
    for attempt in range(2, 100):
        if not (root / candidate).exists():
            return candidate
        candidate = f"{wanted}-{attempt}"

    # A hundred folders of the same name is not a naming problem any more.
    return fallback
