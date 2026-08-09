"""SQLite state: connections, pragmas, and forward-only migrations.

The database is the authoritative record of *state*. Artifacts on disk are the
authoritative record of *evidence*. When they disagree, reconciliation trusts
the artifact — see ``migrations/001_initial.sql`` for why.

WAL mode is not optional here. The controller reads while the worker writes, and
without WAL the reader blocks the writer and a busy UI can stall a job.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

DB_FILENAME = "state.db"
MIGRATIONS_DIRNAME = "migrations"

#: Wait rather than fail when the other process holds a write lock. Contention
#: here is normal and brief: one controller and one worker.
BUSY_TIMEOUT_MS = 10_000

_MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def utc_now() -> str:
    """Current time as ISO-8601 UTC, the format every timestamp column uses."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def database_path(output_root: Path) -> Path:
    return Path(output_root) / DB_FILENAME


def migrations_dir() -> Path:
    """Where the schema lives — inside the package, not beside it.

    This used to resolve to ``repo_root() / "migrations"``, which is correct for
    a source checkout and wrong for every installed copy: from
    ``site-packages/app/core/db.py`` that points at ``site-packages/migrations``,
    which does not exist, so a pip-installed build could not create its own
    database. The tool had only ever been run from a checkout, so nothing caught
    it. Resolving against the package means the schema travels with the code
    that reads it, wherever that code was installed from.
    """
    return Path(__file__).resolve().parents[1] / MIGRATIONS_DIRNAME


# ── Connections ───────────────────────────────────────────────────────────

_local = threading.local()


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the pragmas this application depends on."""
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # explicit transactions; see the transaction() helper
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # NORMAL is the right trade with WAL: durable across process crashes, which
    # is the failure this application actually has to survive. FULL would cost a
    # sync per commit and only adds protection against OS-level crashes, where
    # the artifact reconciliation on startup already covers us.
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one immediate transaction, rolling back on any exception.

    ``BEGIN IMMEDIATE`` rather than the default deferred begin: it takes the
    write lock up front, so two writers contend at the start of the transaction
    where the busy timeout can absorb it, instead of at the first write where
    one of them would have to abort work already done.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


# ── Migrations ────────────────────────────────────────────────────────────


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )


def discover_migrations(directory: Path | None = None) -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` for every migration, in order.

    Refuses duplicate version numbers rather than picking one: two migrations
    claiming the same version means two branches were merged carelessly, and
    silently applying one of them would leave databases in different shapes
    depending on filesystem ordering.
    """
    directory = directory or migrations_dir()
    found: dict[int, tuple[int, str, Path]] = {}

    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise ValueError(
                f"migration {path.name!r} does not match the required NNN_lower_snake_case.sql form"
            )
        version = int(match.group(1))
        if version in found:
            raise ValueError(
                f"duplicate migration version {version:03d}: "
                f"{found[version][2].name} and {path.name}"
            )
        found[version] = (version, match.group(2), path)

    return [found[key] for key in sorted(found)]


def split_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    Uses ``sqlite3.complete_statement`` — SQLite's own parser — rather than
    splitting on semicolons, so a semicolon inside a string literal, a comment,
    or a trigger body does not cut a statement in half.
    """
    statements: list[str] = []
    buffer = ""

    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""

    trailing = buffer.strip()
    if trailing:
        # An incomplete tail means the file is malformed. Surface it here rather
        # than letting SQLite report a confusing error mid-migration.
        raise ValueError(f"migration ends with an incomplete statement: {trailing[:80]!r}")

    return statements


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    _ensure_migration_table(connection)
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row["version"]) for row in rows}


def migrate(connection: sqlite3.Connection, *, directory: Path | None = None) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied.

    Forward-only: there is no down-migration and no rollback path. Each
    migration runs inside a transaction together with the row recording it, so a
    migration either fully applied and is recorded, or did neither.
    """
    _ensure_migration_table(connection)
    already = applied_versions(connection)
    newly_applied: list[int] = []

    for version, name, path in discover_migrations(directory):
        if version in already:
            continue

        sql = path.read_text(encoding="utf-8")
        logger.info("Applying migration %03d_%s", version, name)

        # Statements are executed individually rather than through
        # executescript(), which issues an implicit COMMIT before it runs and
        # would therefore end the transaction we are relying on. SQLite's DDL is
        # transactional, so running the statements inside one BEGIN IMMEDIATE
        # alongside the schema_migrations insert gives the property that
        # matters: a migration either fully applied and is recorded, or did
        # neither. A half-applied, unrecorded migration is unrecoverable without
        # manual surgery, which is exactly what this avoids.
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in split_statements(sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utc_now()),
            )
        except BaseException:
            connection.execute("ROLLBACK")
            logger.error("Migration %03d_%s failed and was rolled back", version, name)
            raise
        else:
            connection.execute("COMMIT")
            newly_applied.append(version)

    if newly_applied:
        logger.info("Applied %d migration(s): %s", len(newly_applied), newly_applied)
    return newly_applied


def schema_version(connection: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 on an empty database."""
    _ensure_migration_table(connection)
    row = connection.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def open_database(output_root: Path, *, migrate_on_open: bool = True) -> sqlite3.Connection:
    """Open — creating if needed — the database for an output root."""
    connection = connect(database_path(output_root))
    if migrate_on_open:
        migrate(connection)
    return connection
