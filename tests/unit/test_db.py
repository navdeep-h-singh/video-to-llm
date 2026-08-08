"""Database, pragmas, and migrations.

The schema constraints are tested directly rather than through a repository
layer: a CHECK that silently stops matching the specification is a defect that
only shows up as bad data much later.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.db import (
    applied_versions,
    connect,
    database_path,
    discover_migrations,
    migrate,
    new_id,
    open_database,
    schema_version,
    split_statements,
    transaction,
    utc_now,
)


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    yield connection
    connection.close()


# ── Connection and pragmas ────────────────────────────────────────────────


def test_wal_mode_is_enabled(tmp_path):
    connection = connect(database_path(tmp_path))
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    connection.close()


def test_foreign_keys_are_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (new_id(), "no-such-job", "/x.mp4", "x", 0, utc_now(), utc_now()),
        )


def test_database_is_created_under_the_output_root(tmp_path):
    connection = open_database(tmp_path / "nested" / "root")
    assert (tmp_path / "nested" / "root" / "state.db").is_file()
    connection.close()


# ── Migrations ────────────────────────────────────────────────────────────


def test_migrations_are_discovered_in_order():
    found = discover_migrations()
    assert found, "no migrations found"
    versions = [version for version, _, _ in found]
    assert versions == sorted(versions)
    assert versions[0] == 1


def test_migrate_applies_and_records(tmp_path):
    # Compared against what is on disk rather than a literal: a test that has to
    # be edited every time a migration is added stops testing the mechanism and
    # starts testing the number.
    expected = [version for version, _, _ in discover_migrations()]

    connection = connect(database_path(tmp_path))
    assert schema_version(connection) == 0

    applied = migrate(connection)
    assert applied == expected
    assert schema_version(connection) == max(expected)
    assert applied_versions(connection) == set(expected)
    connection.close()


def test_migrate_is_idempotent(tmp_path):
    latest = max(version for version, _, _ in discover_migrations())

    connection = connect(database_path(tmp_path))
    migrate(connection)
    assert migrate(connection) == [], "a second run must apply nothing"
    assert schema_version(connection) == latest
    connection.close()


def test_every_required_table_exists(db):
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {row["name"] for row in rows}
    required = {
        "jobs",
        "job_videos",
        "stage_runs",
        "batches",
        "artifacts",
        "events",
        "collections",
        "collection_sources",
        "collection_builds",
        "schema_migrations",
        "worker_claims",
    }
    assert required <= names, f"missing tables: {required - names}"


def test_badly_named_migration_is_refused(tmp_path):
    (tmp_path / "nope.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        discover_migrations(tmp_path)


def test_duplicate_migration_version_is_refused(tmp_path):
    (tmp_path / "002_alpha.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "002_beta.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_failed_migration_is_rolled_back_and_not_recorded(tmp_path):
    # Both statements parse; the second fails at execution because the table
    # already exists. This is the case that matters — a migration that gets
    # partway through and then dies. Everything it did must be undone, or the
    # database is left in a shape no version number describes.
    connection = connect(database_path(tmp_path))
    (tmp_path / "001_broken.sql").write_text(
        "CREATE TABLE fine (id TEXT);\nCREATE TABLE fine (id TEXT);\n",
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.Error):
        migrate(connection, directory=tmp_path)

    assert applied_versions(connection) == set()
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "fine" not in tables, "a failed migration left its first table behind"
    connection.close()


def test_incomplete_migration_is_refused_before_execution(tmp_path):
    connection = connect(database_path(tmp_path))
    (tmp_path / "001_truncated.sql").write_text(
        "CREATE TABLE fine (id TEXT);\nCREATE TABLE (", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="incomplete statement"):
        migrate(connection, directory=tmp_path)
    assert applied_versions(connection) == set()
    connection.close()


# ── Statement splitting ───────────────────────────────────────────────────


def test_semicolon_inside_a_string_literal_does_not_split():
    statements = split_statements("INSERT INTO t (a) VALUES ('one; two');")
    assert len(statements) == 1


def test_semicolon_inside_a_comment_does_not_split():
    statements = split_statements("CREATE TABLE t (\n  a TEXT -- note; here\n);")
    assert len(statements) == 1


def test_multiple_statements_are_separated():
    statements = split_statements("SELECT 1;\nSELECT 2;\nSELECT 3;\n")
    assert len(statements) == 3


def test_the_real_migration_splits_cleanly():
    from pathlib import Path

    from app.core.db import migrations_dir

    sql = (Path(migrations_dir()) / "001_initial.sql").read_text(encoding="utf-8")
    statements = split_statements(sql)
    assert len(statements) > 20
    assert all(s.strip() for s in statements)


# ── Transactions ──────────────────────────────────────────────────────────


def _insert_job(connection, job_id="j1", status="draft"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (job_id, "Test job", status, "/out", utc_now(), utc_now()),
    )


def test_transaction_commits_on_success(db):
    with transaction(db):
        _insert_job(db)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_transaction_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError):
        with transaction(db):
            _insert_job(db)
            raise RuntimeError("boom")
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# ── Schema constraints ────────────────────────────────────────────────────


def test_job_status_is_constrained_to_the_documented_vocabulary(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(db, status="whatever")


@pytest.mark.parametrize(
    "status",
    [
        "draft",
        "ready",
        "preparing",
        "transcribing",
        "analyzing",
        "waiting_retry",
        "paused",
        "needs_attention",
        "completed",
        "completed_with_gaps",
        "cancelled",
    ],
)
def test_every_documented_job_status_is_accepted(db, status):
    _insert_job(db, job_id=new_id(), status=status)


@pytest.mark.parametrize("interval", [499, 10001, 0, -1])
def test_frame_interval_outside_the_supported_range_is_refused(db, interval):
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET frame_interval_ms = ? WHERE id = 'j1'", (interval,))


@pytest.mark.parametrize("interval", [500, 1000, 2000, 3000, 5000, 10000])
def test_supported_frame_intervals_are_accepted(db, interval):
    _insert_job(db)
    db.execute("UPDATE jobs SET frame_interval_ms = ? WHERE id = 'j1'", (interval,))


def test_cascade_delete_removes_dependent_rows(db):
    _insert_job(db)
    video_id = new_id()
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (video_id, "j1", "/a.mp4", "a.mp4", 0, utc_now(), utc_now()),
    )
    db.execute("DELETE FROM jobs WHERE id = 'j1'")
    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 0


def test_video_sequence_and_version_are_unique_together(db):
    _insert_job(db)
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (new_id(), "j1", "/a.mp4", "a.mp4", 0, 1, utc_now(), utc_now()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), "j1", "/b.mp4", "b.mp4", 0, 1, utc_now(), utc_now()),
        )


def test_a_new_version_may_reuse_a_sequence(db):
    # Reruns create a new version at the same position, which must be allowed.
    _insert_job(db)
    for version in (1, 2, 3):
        db.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (new_id(), "j1", "/a.mp4", "a.mp4", 0, version, utc_now(), utc_now()),
        )
    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 3


def test_artifact_paths_are_unique(db):
    _insert_job(db)
    db.execute(
        "INSERT INTO artifacts (id, job_id, kind, relative_path, created_at) VALUES (?,?,?,?,?)",
        (new_id(), "j1", "assembled", "j1/v1/assembled.txt", utc_now()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO artifacts (id, job_id, kind, relative_path, created_at)"
            " VALUES (?,?,?,?,?)",
            (new_id(), "j1", "assembled", "j1/v1/assembled.txt", utc_now()),
        )


def test_unknown_artifact_kind_is_refused(db):
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO artifacts (id, job_id, kind, relative_path, created_at)"
            " VALUES (?,?,?,?,?)",
            (new_id(), "j1", "mystery", "j1/x", utc_now()),
        )


def test_collection_sequence_is_unique_within_a_collection(db):
    db.execute(
        "INSERT INTO collections (id, name, created_at, updated_at) VALUES (?,?,?,?)",
        ("c1", "Week 6", utc_now(), utc_now()),
    )
    _insert_job(db)
    video_id = new_id()
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (video_id, "j1", "/a.mp4", "a.mp4", 0, utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO collection_sources (id, collection_id, job_video_id, source_version,"
        " sequence, display_name, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id(), "c1", video_id, 1, 0, "a.mp4", utc_now()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO collection_sources (id, collection_id, job_video_id, source_version,"
            " sequence, display_name, created_at) VALUES (?,?,?,?,?,?,?)",
            (new_id(), "c1", video_id, 1, 0, "a.mp4", utc_now()),
        )


def test_collection_build_versions_are_unique(db):
    db.execute(
        "INSERT INTO collections (id, name, created_at, updated_at) VALUES (?,?,?,?)",
        ("c1", "Week 6", utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO collection_builds (id, collection_id, collection_version, mode,"
        " created_at) VALUES (?,?,?,?,?)",
        (new_id(), "c1", 1, "full", utc_now()),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO collection_builds (id, collection_id, collection_version, mode,"
            " created_at) VALUES (?,?,?,?,?)",
            (new_id(), "c1", 1, "packs", utc_now()),
        )
