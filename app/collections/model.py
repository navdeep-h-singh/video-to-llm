"""Collections: saved, explicitly ordered sets of processed-video versions.

A collection is local, free, and non-destructive. It reuses output that already
exists and **makes no provider calls of any kind** — no frame extraction, no
transcription, no visual analysis. Building one costs seconds and nothing else.

The defining property is **immutability of source references**. A collection
records not just which video it uses but which *version* of that video's output.
Reprocessing a source video later creates a new version and leaves every
existing collection exactly as it was. If the user wants the newer output in a
collection, they rebuild it deliberately or make a new one.

That is deliberate. A collection is a citation of specific evidence; a citation
that silently changes when the source is revised is worse than no citation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.core.db import new_id, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

COLLECTIONS_DIRNAME = "collections"


class CollectionMode(StrEnum):
    FULL = "full"
    PACKS = "packs"


class WarningState(StrEnum):
    OK = "ok"
    GAPS = "gaps"
    NO_VISUAL = "no_visual"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    MISSING_ARTIFACTS = "missing_artifacts"


#: Every warning permits inclusion. None of them blocks a build — the user is
#: told what is imperfect and decides. Refusing would strand usable work.
WARNING_LABELS = {
    WarningState.OK: "Ready",
    WarningState.GAPS: "Some pictures have no description",
    WarningState.NO_VISUAL: "No descriptions — pictures and words only",
    WarningState.PROVENANCE_MISMATCH: "Described with older wording",
    WarningState.MISSING_ARTIFACTS: "Pictures are missing",
}


@dataclass
class CollectionSource:
    """One video's place in a collection, pinned to a specific version."""

    job_video_id: str
    sequence: int
    display_name: str
    source_version: int = 1
    duration_seconds: float = 0.0
    assembled_sha256: str | None = None
    warning_state: str = WarningState.OK
    warning_detail: str = ""
    output_dir: str = ""

    @property
    def warning_label(self) -> str:
        return WARNING_LABELS.get(WarningState(self.warning_state), "Ready")

    @property
    def has_warning(self) -> bool:
        return self.warning_state != WarningState.OK


@dataclass
class Collection:
    id: str
    name: str
    description: str = ""
    mode: str = CollectionMode.FULL
    token_limit: int | None = None
    reserve_tokens: int | None = None
    target_model_label: str = ""
    allow_video_split: bool = False
    current_version: int = 0
    sources: list[CollectionSource] = field(default_factory=list)

    @property
    def total_duration_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.sources)

    @property
    def warning_count(self) -> int:
        return sum(1 for s in self.sources if s.has_warning)

    @property
    def usable_budget(self) -> int:
        """Target limit minus the reserve, in tokens."""
        if self.token_limit is None:
            return 0
        return max(0, self.token_limit - (self.reserve_tokens or 0))


def collection_dir(output_root: Path, collection_id: str, version: int) -> Path:
    """Where a build's artifacts live.

    Kept entirely separate from any processed-video directory. Merging them
    would make a collection's output look like part of a video's own archive,
    and deleting one would silently damage the other.
    """
    return Path(output_root) / COLLECTIONS_DIRNAME / collection_id / f"v{version}"


# ── Persistence ───────────────────────────────────────────────────────────


def create_collection(
    connection: sqlite3.Connection,
    *,
    name: str,
    description: str = "",
    mode: str = CollectionMode.FULL,
    token_limit: int | None = None,
    reserve_tokens: int | None = None,
    target_model_label: str = "",
    allow_video_split: bool = False,
) -> str:
    if not name.strip():
        raise ValueError("a collection needs a name")

    collection_id = new_id()
    connection.execute(
        "INSERT INTO collections (id, name, description, mode, token_limit,"
        " reserve_tokens, target_model_label, allow_video_split, current_version,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        (
            collection_id,
            name.strip(),
            description,
            str(mode),
            token_limit,
            reserve_tokens,
            target_model_label,
            int(allow_video_split),
            utc_now(),
            utc_now(),
        ),
    )
    return collection_id


def set_sources(
    connection: sqlite3.Connection,
    collection_id: str,
    sources: list[CollectionSource],
) -> None:
    """Replace the ordered source list.

    The sequence comes from the user. It is never inferred from filename, date,
    or content — two recordings from the same morning have no inherent order,
    and guessing wrong silently reverses the narrative.
    """
    connection.execute("DELETE FROM collection_sources WHERE collection_id = ?", (collection_id,))
    for position, source in enumerate(sources):
        connection.execute(
            "INSERT INTO collection_sources (id, collection_id, job_video_id,"
            " source_version, sequence, display_name, duration_seconds,"
            " assembled_sha256, warning_state, warning_detail, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                collection_id,
                source.job_video_id,
                source.source_version,
                position,
                source.display_name,
                source.duration_seconds,
                source.assembled_sha256,
                str(source.warning_state),
                source.warning_detail,
                utc_now(),
            ),
        )
    connection.execute(
        "UPDATE collections SET updated_at = ? WHERE id = ?", (utc_now(), collection_id)
    )


def load_collection(connection: sqlite3.Connection, collection_id: str) -> Collection | None:
    row = connection.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
    if row is None:
        return None

    source_rows = connection.execute(
        "SELECT cs.*, jv.output_dir FROM collection_sources cs"
        " LEFT JOIN job_videos jv ON jv.id = cs.job_video_id"
        " WHERE cs.collection_id = ? ORDER BY cs.sequence",
        (collection_id,),
    ).fetchall()

    return Collection(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        mode=row["mode"],
        token_limit=row["token_limit"],
        reserve_tokens=row["reserve_tokens"],
        target_model_label=row["target_model_label"],
        allow_video_split=bool(row["allow_video_split"]),
        current_version=row["current_version"],
        sources=[
            CollectionSource(
                job_video_id=s["job_video_id"],
                sequence=s["sequence"],
                display_name=s["display_name"],
                source_version=s["source_version"],
                duration_seconds=s["duration_seconds"] or 0.0,
                assembled_sha256=s["assembled_sha256"],
                warning_state=s["warning_state"],
                warning_detail=s["warning_detail"] or "",
                output_dir=s["output_dir"] or "",
            )
            for s in source_rows
        ],
    )


def list_collections(connection: sqlite3.Connection) -> list[Collection]:
    rows = connection.execute("SELECT id FROM collections ORDER BY updated_at DESC").fetchall()
    loaded = [load_collection(connection, row["id"]) for row in rows]
    return [c for c in loaded if c is not None]


# ── Assessing a candidate source ──────────────────────────────────────────


def assess_source(
    connection: sqlite3.Connection, job_video_id: str, output_root: Path
) -> CollectionSource | None:
    """Build a source entry, reporting anything imperfect about it.

    Every problem found here is a warning, never a refusal. A video with missing
    descriptions, older wording, or an absent picture folder is still worth
    including — the user is told and decides.
    """
    row = connection.execute("SELECT * FROM job_videos WHERE id = ?", (job_video_id,)).fetchone()
    if row is None:
        return None

    source = CollectionSource(
        job_video_id=job_video_id,
        sequence=row["sequence"],
        display_name=row["display_name"],
        source_version=row["version"],
        duration_seconds=row["duration_seconds"] or 0.0,
        output_dir=row["output_dir"] or "",
    )

    directory = (
        Path(output_root) / row["output_dir"]
        if row["output_dir"]
        else (Path(row["imported_from"]) if row["imported_from"] else None)
    )

    if directory is None or not directory.is_dir():
        source.warning_state = WarningState.MISSING_ARTIFACTS
        source.warning_detail = "the folder holding this video's output could not be found."
        return source

    assembled = directory / "assembled.txt"
    if not assembled.is_file():
        source.warning_state = WarningState.MISSING_ARTIFACTS
        source.warning_detail = "this video has no assembled document."
        return source

    from app.core.artifacts import sha256_file

    source.assembled_sha256 = sha256_file(assembled)

    frames = directory / "frames"
    if not frames.is_dir() or not any(frames.glob("*.jpg")):
        source.warning_state = WarningState.MISSING_ARTIFACTS
        source.warning_detail = "the pictures are missing. The text is fine and can still be used."
        return source

    if row["status"] == "completed_with_gaps":
        source.warning_state = WarningState.GAPS
        source.warning_detail = "some pictures in this video have no description."
        return source

    visual = directory / "visual_results.json"
    if not visual.is_file():
        source.warning_state = WarningState.NO_VISUAL
        source.warning_detail = "this video has no descriptions — pictures and words only."
        return source

    import json

    from app.providers.base import schema_hash

    try:
        payload = json.loads(visual.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source.warning_state = WarningState.MISSING_ARTIFACTS
        source.warning_detail = "the descriptions could not be read."
        return source

    descriptions = payload.get("descriptions") or []
    if descriptions:
        found = descriptions[0].get("schema_hash")
        if found and found != schema_hash():
            source.warning_state = WarningState.PROVENANCE_MISMATCH
            source.warning_detail = (
                "described with older wording, so it reads a little differently from the others."
            )

    return source


def available_sources(connection: sqlite3.Connection, output_root: Path) -> list[CollectionSource]:
    """Every processed video that could go into a collection."""
    rows = connection.execute(
        "SELECT id FROM job_videos WHERE is_active_version = 1"
        " AND status IN ('completed', 'completed_with_gaps')"
        " ORDER BY updated_at DESC"
    ).fetchall()

    found = [assess_source(connection, row["id"], output_root) for row in rows]
    return [s for s in found if s is not None]


@dataclass(frozen=True)
class SourceVersion:
    """One processed version of a video, as an option the user can pin to."""

    job_video_id: str
    version: int
    status: str
    is_active: bool
    created_at: str

    @property
    def label(self) -> str:
        suffix = " — the one in use" if self.is_active else ""
        return f"Version {self.version}{suffix}"


def versions_of(connection: sqlite3.Connection, job_video_id: str) -> list[SourceVersion]:
    """Every processed version of the same video, newest first.

    Versions of one video are the rows sharing a job and a sequence — that pair
    is what ``UNIQUE (job_id, sequence, version)`` makes the video's identity.
    Each version is a distinct row, so pinning a collection to an older version
    means referencing that row rather than annotating the newest one.
    """
    anchor = connection.execute(
        "SELECT job_id, sequence FROM job_videos WHERE id = ?", (job_video_id,)
    ).fetchone()
    if anchor is None:
        return []

    rows = connection.execute(
        "SELECT id, version, status, is_active_version, created_at FROM job_videos"
        " WHERE job_id = ? AND sequence = ? ORDER BY version DESC",
        (anchor["job_id"], anchor["sequence"]),
    ).fetchall()

    return [
        SourceVersion(
            job_video_id=row["id"],
            version=row["version"],
            status=row["status"],
            is_active=bool(row["is_active_version"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def next_version(connection: sqlite3.Connection, collection_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(collection_version), 0) + 1 AS next"
        " FROM collection_builds WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()
    return int(row["next"])
