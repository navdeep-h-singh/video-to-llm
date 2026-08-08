"""Targeted reruns.

The schema has always supported versioned output — ``job_videos.version``,
``is_active_version``, and per-batch records — and the rule that prior expensive
output is never overwritten has always been tested. None of it was reachable
from the interface, which for the person using the product is the same as it not
existing.

A rerun **creates a new version and leaves the old one exactly as it is**. The
previous version keeps its directory, its descriptions, and its assembled
document, and any collection that pinned it goes on pointing at the same bytes.
That is the whole point: a collection is a citation, and a citation that
silently changes when the source is revised is worse than no citation at all.

Two things make this more than "run it again":

1. **Frames and the transcript are carried over, not recomputed.** They are
   deterministic from the same source at the same interval, and re-transcribing
   an hour of audio on a processor to redo a handful of descriptions would be a
   remarkable waste of somebody's afternoon.
2. **Descriptions outside the chosen scope are carried over too.** They were
   already paid for. Re-sending them would charge for them a second time to
   arrive at the same answer, which is precisely what the batch-durability rule
   exists to prevent.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.core.db import new_id, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Written into the new version's directory. The visual stage reads it to learn
#: which frames to send and which to carry over. A file rather than a column:
#: it belongs to that one build of that one version, and it travels with the
#: directory if the output is ever moved or imported elsewhere.
RERUN_PLAN_FILENAME = "rerun.json"

VISUAL_RESULTS_FILENAME = "visual_results.json"
MANIFEST_FILENAME = "frames_manifest.json"


class RerunScope(StrEnum):
    ALL = "all"
    LOW_CONFIDENCE = "low_confidence"
    FALLBACK = "fallback"
    RANGE = "range"


SCOPE_LABELS = {
    RerunScope.ALL: "Every picture",
    RerunScope.LOW_CONFIDENCE: "Only the ones marked low confidence",
    RerunScope.FALLBACK: "Only the ones that came back unusable",
    RerunScope.RANGE: "A chosen range of pictures",
}


class RerunError(RuntimeError):
    """Raised when a rerun cannot be set up. Nothing is changed when it is."""


@dataclass
class RerunPlan:
    """What a rerun would do, worked out before anything is created."""

    scope: str
    #: Frame indices to describe again.
    indices: list[int] = field(default_factory=list)
    #: Descriptions kept verbatim from the previous version.
    carried_over: int = 0
    previous_version: int = 1
    previous_job_video_id: str = ""
    display_name: str = ""
    total_frames: int = 0

    @property
    def label(self) -> str:
        return SCOPE_LABELS.get(RerunScope(self.scope), self.scope)

    @property
    def frame_count(self) -> int:
        return len(self.indices)

    @property
    def is_empty(self) -> bool:
        return not self.indices


# ── Reading what the previous version produced ────────────────────────────


def _previous_descriptions(directory: Path) -> list[dict]:
    path = Path(directory) / VISUAL_RESULTS_FILENAME
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # An unreadable results file means we cannot tell which frames were
        # low confidence. Reporting none is honest; guessing would not be.
        logger.warning("Could not read previous descriptions at %s", path)
        return []
    found = payload.get("descriptions")
    return found if isinstance(found, list) else []


def _frame_indices(directory: Path) -> list[int]:
    path = Path(directory) / MANIFEST_FILENAME
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    frames = payload.get("frames") or []
    return [int(frame["index"]) for frame in frames if "index" in frame]


def plan_rerun(
    connection: sqlite3.Connection,
    job_video_id: str,
    output_root: Path,
    *,
    scope: str = RerunScope.ALL,
    start: int | None = None,
    end: int | None = None,
) -> RerunPlan:
    """Work out which frames a rerun would send, without changing anything.

    Every scope is derived from what the previous version actually recorded.
    A scope that turns out to select nothing is reported as selecting nothing —
    the caller then tells the user there is nothing to redo, rather than
    creating an empty version that looks like work.
    """
    row = connection.execute("SELECT * FROM job_videos WHERE id = ?", (job_video_id,)).fetchone()
    if row is None:
        raise RerunError("That video could not be found.")

    directory = Path(output_root) / (row["output_dir"] or "")
    all_indices = _frame_indices(directory)
    descriptions = _previous_descriptions(directory)

    plan = RerunPlan(
        scope=str(scope),
        previous_version=row["version"],
        previous_job_video_id=job_video_id,
        display_name=row["display_name"],
        total_frames=len(all_indices),
    )

    if scope == RerunScope.ALL:
        plan.indices = list(all_indices)
    elif scope == RerunScope.LOW_CONFIDENCE:
        plan.indices = sorted(
            int(entry["index"])
            for entry in descriptions
            if "index" in entry and str(entry.get("confidence", "")).lower() == "low"
        )
    elif scope == RerunScope.FALLBACK:
        # Frames the manifest lists but the results never described: a batch
        # that failed, was skipped, or came back unparseable.
        described = {int(entry["index"]) for entry in descriptions if "index" in entry}
        plan.indices = [index for index in all_indices if index not in described]
    elif scope == RerunScope.RANGE:
        low = 0 if start is None else int(start)
        high = (max(all_indices) if all_indices else 0) if end is None else int(end)
        if low > high:
            low, high = high, low
        plan.indices = [index for index in all_indices if low <= index <= high]
    else:
        raise RerunError(f"Unknown rerun scope {scope!r}.")

    targeted = set(plan.indices)
    plan.carried_over = sum(
        1 for entry in descriptions if "index" in entry and int(entry["index"]) not in targeted
    )
    return plan


# ── Carrying the expensive parts forward ──────────────────────────────────


def _link_or_copy(source: Path, destination: Path) -> None:
    """Hard-link a file, falling back to a copy.

    Frames are identical bytes between versions — they came from the same source
    at the same interval — so a link keeps a second copy of 2,000-odd JPEGs off
    the disk. A link is not always available (a different filesystem, or a
    platform that refuses), and a copy is correct everywhere, so failure falls
    back rather than propagating.
    """
    try:
        os.link(source, destination)
    except (OSError, NotImplementedError, AttributeError):
        shutil.copy2(source, destination)


def _carry_directory(previous: Path, target: Path, name: str) -> int:
    source = previous / name
    if not source.is_dir():
        return 0

    destination = target / name
    destination.mkdir(parents=True, exist_ok=True)

    carried = 0
    for entry in sorted(source.iterdir()):
        if entry.is_file():
            _link_or_copy(entry, destination / entry.name)
            carried += 1
    return carried


def seed_new_version(previous_dir: Path, target_dir: Path) -> None:
    """Populate a new version's directory from the one before it.

    Frames and the transcript are deterministic from the same source at the same
    interval. Recomputing them would mean re-transcribing an hour of audio on a
    processor in order to redo a handful of descriptions.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    _carry_directory(previous_dir, target_dir, "frames")
    _carry_directory(previous_dir, target_dir, "frames_api")

    for name in (
        MANIFEST_FILENAME,
        "transcript.json",
        "transcript.txt",
        "transcript_raw.json",
        "silence_windows.json",
        "audio.wav",
    ):
        source = previous_dir / name
        if source.is_file():
            _link_or_copy(source, target_dir / name)


def _mark_stage_carried(
    connection: sqlite3.Connection, job_video_id: str, stage: str, source_version: int
) -> None:
    """Record a stage as complete without having run it.

    The stage functions skip work whose stage_run is already completed, so this
    is what stops a rerun re-extracting frames and re-transcribing audio. The
    provenance says plainly where the output came from: a run that claims to
    have done work it inherited would make the record useless for exactly the
    question it exists to answer.
    """
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, attempt, status,"
        " provenance_json, started_at, finished_at, created_at, updated_at)"
        " VALUES (?,?,?,1,'completed',?,?,?,?,?)",
        (
            new_id(),
            job_video_id,
            stage,
            json.dumps({"carried_over_from_version": source_version}),
            utc_now(),
            utc_now(),
            utc_now(),
            utc_now(),
        ),
    )


def start_rerun(
    connection: sqlite3.Connection,
    plan: RerunPlan,
    *,
    output_root: Path,
    provider: str = "",
    model_id: str = "",
) -> str:
    """Create the next version and queue it. Returns the new job_video id.

    The previous version is left completely intact — its row, its directory, and
    every collection that references it. It is only marked as no longer the
    active one.
    """
    if plan.is_empty:
        raise RerunError(
            "Nothing matches that choice, so there is nothing to do again. "
            "The previous version is untouched."
        )

    previous = connection.execute(
        "SELECT * FROM job_videos WHERE id = ?", (plan.previous_job_video_id,)
    ).fetchone()
    if previous is None:
        raise RerunError("That video could not be found.")

    next_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM job_videos"
            " WHERE job_id = ? AND sequence = ?",
            (previous["job_id"], previous["sequence"]),
        ).fetchone()["next"]
    )

    new_video_id = new_id()
    relative = f"{previous['job_id']}/{new_video_id}_v{next_version}"
    target_dir = Path(output_root) / relative
    previous_dir = Path(output_root) / (previous["output_dir"] or "")

    if not previous_dir.is_dir():
        raise RerunError(
            "The previous version's folder could not be found, so there is "
            "nothing to build on. Nothing has been changed."
        )

    seed_new_version(previous_dir, target_dir)

    (target_dir / RERUN_PLAN_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "scope": str(plan.scope),
                "from_version": plan.previous_version,
                "from_job_video_id": plan.previous_job_video_id,
                "from_output_dir": previous["output_dir"],
                "indices": plan.indices,
                "carried_over": plan.carried_over,
                "created_at": utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, source_sha256,"
        " duration_seconds, container, width, height, sequence, version,"
        " is_active_version, status, frame_count, output_dir, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'pending',?,?,?,?)",
        (
            new_video_id,
            previous["job_id"],
            previous["source_path"],
            previous["display_name"],
            previous["source_sha256"],
            previous["duration_seconds"],
            previous["container"],
            previous["width"],
            previous["height"],
            previous["sequence"],
            next_version,
            previous["frame_count"],
            relative,
            utc_now(),
            utc_now(),
        ),
    )

    # The previous version stops being the active one. It is not deleted, not
    # altered, and not detached from any collection that cites it.
    connection.execute(
        "UPDATE job_videos SET is_active_version = 0, updated_at = ? WHERE id = ?",
        (utc_now(), plan.previous_job_video_id),
    )

    _mark_stage_carried(connection, new_video_id, "frames", plan.previous_version)
    _mark_stage_carried(connection, new_video_id, "transcribe", plan.previous_version)

    # The job's own description choice has to move with the rerun. The worker
    # honours what the job recorded rather than the global setting, so a job
    # created with descriptions off would queue this new version and then
    # silently skip the one stage the rerun exists to run.
    if provider:
        connection.execute(
            "UPDATE jobs SET status = 'ready', visual_provider = ?, visual_model_id = ?,"
            " updated_at = ? WHERE id = ?",
            (provider, model_id, utc_now(), previous["job_id"]),
        )
    else:
        connection.execute(
            "UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?",
            (utc_now(), previous["job_id"]),
        )
    connection.execute(
        "INSERT INTO events (job_id, job_video_id, level, kind, message, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            previous["job_id"],
            new_video_id,
            "info",
            "rerun_requested",
            f"Version {next_version} of {plan.display_name} was queued: "
            f"{plan.label.lower()} ({plan.frame_count:,} to do, "
            f"{plan.carried_over:,} kept from version {plan.previous_version}). "
            f"Version {plan.previous_version} is untouched.",
            utc_now(),
        ),
    )

    logger.info(
        "Queued version %d of %s: %d frame(s) to redo, %d carried over",
        next_version,
        plan.display_name,
        plan.frame_count,
        plan.carried_over,
    )
    return new_video_id


# ── What the visual stage needs to know ───────────────────────────────────


@dataclass(frozen=True)
class LoadedRerunPlan:
    scope: str
    indices: frozenset[int]
    previous_dir: str
    from_version: int


def load_rerun_plan(output_dir: Path) -> LoadedRerunPlan | None:
    """Read the plan a rerun left in this directory, if there is one."""
    path = Path(output_dir) / RERUN_PLAN_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring an unreadable rerun plan at %s", path)
        return None

    return LoadedRerunPlan(
        scope=str(payload.get("scope", RerunScope.ALL)),
        indices=frozenset(int(index) for index in payload.get("indices", [])),
        previous_dir=str(payload.get("from_output_dir", "")),
        from_version=int(payload.get("from_version", 1)),
    )


def carried_descriptions(plan: LoadedRerunPlan, output_root: Path) -> list[dict]:
    """The previous version's descriptions for frames this rerun is not redoing.

    These were already produced and, on a paid provider, already charged for.
    Sending them again would spend money to arrive at the same answer.
    """
    if not plan.previous_dir:
        return []
    previous = _previous_descriptions(Path(output_root) / plan.previous_dir)
    return [
        entry for entry in previous if "index" in entry and int(entry["index"]) not in plan.indices
    ]


# ── Presenting versions ───────────────────────────────────────────────────


@dataclass(frozen=True)
class VersionSummary:
    job_video_id: str
    version: int
    is_active: bool
    status: str
    created_at: str
    scope_label: str
    described: int
    low_confidence: int
    model_id: str

    @property
    def label(self) -> str:
        return f"Version {self.version}"


def version_summaries(
    connection: sqlite3.Connection, job_video_id: str, output_root: Path
) -> list[VersionSummary]:
    """Every version of one video, newest first, with what it produced."""
    anchor = connection.execute(
        "SELECT job_id, sequence FROM job_videos WHERE id = ?", (job_video_id,)
    ).fetchone()
    if anchor is None:
        return []

    rows = connection.execute(
        "SELECT * FROM job_videos WHERE job_id = ? AND sequence = ? ORDER BY version DESC",
        (anchor["job_id"], anchor["sequence"]),
    ).fetchall()

    summaries: list[VersionSummary] = []
    for row in rows:
        directory = Path(output_root) / (row["output_dir"] or "")
        descriptions = _previous_descriptions(directory)
        loaded = load_rerun_plan(directory)

        model = connection.execute(
            "SELECT model_id FROM stage_runs WHERE job_video_id = ? AND stage = 'visual'"
            " AND model_id IS NOT NULL AND model_id != '' ORDER BY attempt DESC LIMIT 1",
            (row["id"],),
        ).fetchone()

        summaries.append(
            VersionSummary(
                job_video_id=row["id"],
                version=row["version"],
                is_active=bool(row["is_active_version"]),
                status=row["status"],
                created_at=row["created_at"],
                scope_label=(
                    SCOPE_LABELS.get(RerunScope(loaded.scope), "")
                    if loaded is not None
                    else "First run"
                ),
                described=len(descriptions),
                low_confidence=sum(
                    1 for entry in descriptions if str(entry.get("confidence", "")).lower() == "low"
                ),
                model_id=(model["model_id"] if model else ""),
            )
        )
    return summaries


def make_active(connection: sqlite3.Connection, job_video_id: str) -> None:
    """Switch which version the rest of the product uses.

    Nothing is deleted and nothing is rewritten. Collections keep pointing at
    whichever version they pinned, whether or not it is the active one.
    """
    row = connection.execute(
        "SELECT job_id, sequence FROM job_videos WHERE id = ?", (job_video_id,)
    ).fetchone()
    if row is None:
        raise RerunError("That version could not be found.")

    connection.execute(
        "UPDATE job_videos SET is_active_version = 0, updated_at = ?"
        " WHERE job_id = ? AND sequence = ?",
        (utc_now(), row["job_id"], row["sequence"]),
    )
    connection.execute(
        "UPDATE job_videos SET is_active_version = 1, updated_at = ? WHERE id = ?",
        (utc_now(), job_video_id),
    )
