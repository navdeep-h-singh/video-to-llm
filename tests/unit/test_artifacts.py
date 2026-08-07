"""Atomic writes.

The guarantee under test: a file that exists at its destination is complete. A
job that was interrupted must never resume against a half-written manifest and
treat it as finished work — particularly when the work cost money.
"""

from __future__ import annotations

import json

import pytest

from app.core.artifacts import (
    TEMP_PREFIX,
    atomic_write,
    cleanup_temp_files,
    register_artifact,
    sha256_bytes,
    sha256_file,
    verify_artifact,
    write_bytes,
    write_json,
    write_text,
)
from app.core.db import open_database, utc_now


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Test", "draft", str(tmp_path), utc_now(), utc_now()),
    )
    yield connection
    connection.close()


# ── Atomicity ─────────────────────────────────────────────────────────────


def test_a_successful_write_lands_at_the_destination(tmp_path):
    target = tmp_path / "assembled.txt"
    write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_a_failed_write_leaves_no_destination_file(tmp_path):
    target = tmp_path / "assembled.txt"
    with pytest.raises(RuntimeError):
        with atomic_write(target) as handle:
            handle.write(b"partial")
            raise RuntimeError("interrupted")
    assert not target.exists()


def test_a_failed_write_does_not_damage_an_existing_artifact(tmp_path):
    # The case that matters most: a rerun that dies partway must not destroy the
    # good output from the previous run.
    target = tmp_path / "assembled.txt"
    write_text(target, "the good previous version")

    with pytest.raises(RuntimeError):
        with atomic_write(target) as handle:
            handle.write(b"a broken new version")
            raise RuntimeError("interrupted")

    assert target.read_text(encoding="utf-8") == "the good previous version"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path):
    with pytest.raises(RuntimeError):
        with atomic_write(tmp_path / "x.txt") as handle:
            handle.write(b"partial")
            raise RuntimeError("interrupted")
    assert list(tmp_path.glob(f"{TEMP_PREFIX}*")) == []


def test_the_temp_file_is_a_sibling_of_the_destination(tmp_path):
    # os.replace is only atomic within one filesystem. A temp file elsewhere
    # would silently degrade the rename into copy-then-delete.
    seen: list = []
    nested = tmp_path / "deep" / "nested"
    with atomic_write(nested / "out.txt") as handle:
        seen.extend(nested.glob(f"{TEMP_PREFIX}*"))
        handle.write(b"data")
    assert len(seen) == 1, "expected exactly one temp file beside the destination"


def test_parent_directories_are_created(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "out.txt"
    write_text(target, "x")
    assert target.is_file()


def test_writing_over_an_existing_file_replaces_it(tmp_path):
    target = tmp_path / "out.txt"
    write_text(target, "first")
    write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


# ── Checksums ─────────────────────────────────────────────────────────────


def test_write_returns_the_checksum_of_what_landed(tmp_path):
    target = tmp_path / "out.bin"
    returned = write_bytes(target, b"payload")
    assert returned == sha256_file(target) == sha256_bytes(b"payload")


def test_checksum_of_a_large_file_is_correct(tmp_path):
    # Exercises the chunked read path.
    payload = b"x" * (3 * 1024 * 1024 + 17)
    target = tmp_path / "big.bin"
    assert write_bytes(target, payload) == sha256_bytes(payload)


# ── JSON ──────────────────────────────────────────────────────────────────


def test_json_round_trips(tmp_path):
    target = tmp_path / "manifest.json"
    write_json(target, {"frames": 1265, "interval_ms": 2000})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "frames": 1265,
        "interval_ms": 2000,
    }


def test_json_is_redacted_before_it_reaches_disk(tmp_path):
    # Manifests are assembled from provider responses and configuration, so
    # redaction belongs here rather than at every call site.
    target = tmp_path / "provenance.json"
    write_json(
        target,
        {
            "provider": "anthropic",
            "api_key": "should-never-be-written",
            "nested": {"Authorization": "Bearer also-not-this"},
        },
    )
    written = target.read_text(encoding="utf-8")
    assert "should-never-be-written" not in written
    assert "also-not-this" not in written
    assert "anthropic" in written


# ── Temp cleanup ──────────────────────────────────────────────────────────


def test_cleanup_removes_stale_temp_files(tmp_path):
    stale = tmp_path / f"{TEMP_PREFIX}abc123"
    stale.write_bytes(b"interrupted")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / f"{TEMP_PREFIX}def456").write_bytes(b"also interrupted")

    removed = cleanup_temp_files(tmp_path)
    assert len(removed) == 2
    assert not stale.exists()


def test_cleanup_leaves_real_artifacts_alone(tmp_path):
    keeper = tmp_path / "assembled.txt"
    keeper.write_text("real output", encoding="utf-8")
    (tmp_path / f"{TEMP_PREFIX}x").write_bytes(b"junk")

    cleanup_temp_files(tmp_path)
    assert keeper.read_text(encoding="utf-8") == "real output"


def test_cleanup_on_a_missing_root_is_harmless(tmp_path):
    assert cleanup_temp_files(tmp_path / "not-there") == []


# ── Registration ──────────────────────────────────────────────────────────


def test_registering_stores_a_relative_path(db, tmp_path):
    target = tmp_path / "j1" / "v1" / "assembled.txt"
    write_text(target, "content")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")

    row = db.execute("SELECT relative_path, sha256, size_bytes FROM artifacts").fetchone()
    assert row["relative_path"] == "j1/v1/assembled.txt"
    assert not row["relative_path"].startswith("/")
    assert row["sha256"] == sha256_file(target)
    assert row["size_bytes"] == len("content")


def test_registering_a_missing_file_is_refused(db, tmp_path):
    # A row pointing at nothing is worse than a file no row knows about:
    # reconciliation can find the second, but must treat the first as data loss.
    with pytest.raises(FileNotFoundError, match="does not exist on disk"):
        register_artifact(
            db,
            output_root=tmp_path,
            path=tmp_path / "never-written.txt",
            kind="assembled",
            job_id="j1",
        )


def test_re_registering_the_same_path_updates_rather_than_duplicates(db, tmp_path):
    target = tmp_path / "out.txt"
    write_text(target, "first")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")
    write_text(target, "second, longer")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")

    rows = db.execute("SELECT sha256, size_bytes FROM artifacts").fetchall()
    assert len(rows) == 1
    assert rows[0]["sha256"] == sha256_file(target)


def test_verify_detects_a_deleted_artifact(db, tmp_path):
    target = tmp_path / "out.txt"
    write_text(target, "content")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")
    assert verify_artifact(db, tmp_path, "out.txt") is True

    target.unlink()
    assert verify_artifact(db, tmp_path, "out.txt") is False


def test_verify_detects_a_modified_artifact(db, tmp_path):
    target = tmp_path / "out.txt"
    write_text(target, "content")
    register_artifact(db, output_root=tmp_path, path=target, kind="assembled", job_id="j1")

    target.write_text("tampered with", encoding="utf-8")
    assert verify_artifact(db, tmp_path, "out.txt") is False


def test_verify_returns_false_for_an_unregistered_path(db, tmp_path):
    assert verify_artifact(db, tmp_path, "never-registered.txt") is False


# ── Paths under a symlink ─────────────────────────────────────────────────
#
# Found by the smoke test: on macOS /var is a symlink to /private/var, so a
# temporary output root gave `relative_to` one resolved path and one unresolved
# path and it raised. The same happens with a symlinked home directory or an
# external mount, so this is a portability bug rather than a test artefact.


def test_a_path_under_a_symlinked_root_resolves(tmp_path):
    from app.core.artifacts import relative_to_root

    real_root = tmp_path / "real"
    real_root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_root, target_is_directory=True)

    target = real_root / "job" / "assembled.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    # The root is reached through the symlink, the file through the real path.
    assert relative_to_root(target, link) == "job/assembled.txt"


def test_a_root_reached_directly_and_a_path_through_a_symlink_also_resolves(tmp_path):
    real_root = tmp_path / "real"
    (real_root / "job").mkdir(parents=True)
    target = real_root / "job" / "assembled.txt"
    target.write_text("x", encoding="utf-8")

    link = tmp_path / "link"
    link.symlink_to(real_root, target_is_directory=True)

    from app.core.artifacts import relative_to_root

    assert relative_to_root(link / "job" / "assembled.txt", real_root) == "job/assembled.txt"


def test_registering_through_a_symlinked_root_stores_a_clean_relative_path(db, tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_root, target_is_directory=True)

    target = real_root / "out.txt"
    write_text(target, "content")
    register_artifact(db, output_root=link, path=target, kind="assembled", job_id="j1")

    stored = db.execute("SELECT relative_path FROM artifacts").fetchone()["relative_path"]
    assert stored == "out.txt"
    assert not stored.startswith("/")


def test_a_path_genuinely_outside_the_root_is_still_refused(tmp_path):
    from app.core.artifacts import relative_to_root

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "file.txt"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not inside the output folder"):
        relative_to_root(outside, root)
