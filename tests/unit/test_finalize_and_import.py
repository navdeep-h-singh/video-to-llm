"""Job finalisation and importing earlier work.

Import is non-destructive: nothing in the imported folder is moved, rewritten,
or renamed. Several tests assert exactly that, because an import that damages
the thing it is reading is unrecoverable.
"""

from __future__ import annotations

import json

import pytest

from app.core.db import open_database, utc_now
from app.pipeline.finalize import collect_sources, finalize_job
from app.providers.base import schema_hash
from app.services.importer import (
    discover,
    import_candidates,
    import_processed_output,
)


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "out")
    yield connection
    connection.close()


def _make_processed_video(
    directory,
    *,
    name="clip.mp4",
    frames=True,
    descriptions=True,
    old_schema=False,
    duration=100.0,
):
    """Write a directory that looks like finished output."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "assembled.txt").write_text(f"assembled content for {name}\n", "utf-8")

    (directory / "frames_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_filename": name,
                "duration_seconds": duration,
                "frame_count": 3,
                "frame_interval_ms": 2000,
                "frames": [],
            }
        ),
        "utf-8",
    )
    (directory / "transcript.json").write_text(json.dumps({"version": 1, "segments": []}), "utf-8")

    if frames:
        frame_dir = directory / "frames"
        frame_dir.mkdir(exist_ok=True)
        for index in range(3):
            (frame_dir / f"{index:06d}_t000000.jpg").write_bytes(b"\xff\xd8 fake")

    if descriptions:
        (directory / "visual_results.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "descriptions": [
                        {
                            "index": 0,
                            "schema_hash": "0000oldschema00" if old_schema else schema_hash(),
                        }
                    ],
                }
            ),
            "utf-8",
        )

    return directory


# ── Discovery ─────────────────────────────────────────────────────────────


def test_a_processed_video_is_discovered(tmp_path):
    _make_processed_video(tmp_path / "run" / "video_one")
    found = discover(tmp_path / "run")

    assert len(found) == 1
    assert found[0].display_name == "clip.mp4"
    assert found[0].frame_count == 3
    assert found[0].duration_seconds == 100.0


def test_several_videos_are_discovered(tmp_path):
    for index in range(3):
        _make_processed_video(tmp_path / "run" / f"video_{index}", name=f"clip{index}.mp4")
    assert len(discover(tmp_path / "run")) == 3


def test_copies_in_the_handoff_folder_are_not_discovered_twice(tmp_path):
    # analysis_input holds copies. Importing them would register the same video
    # under two paths, and a collection would silently include it twice.
    _make_processed_video(tmp_path / "run" / "video_one")
    handoff = tmp_path / "run" / "analysis_input"
    handoff.mkdir(parents=True)
    (handoff / "assembled.txt").write_text("a copy\n", "utf-8")

    assert len(discover(tmp_path / "run")) == 1


def test_a_folder_with_nothing_processed_yields_nothing(tmp_path):
    (tmp_path / "empty").mkdir()
    assert discover(tmp_path / "empty") == []


def test_a_missing_folder_yields_nothing(tmp_path):
    assert discover(tmp_path / "not-there") == []


# ── Compatibility reporting ───────────────────────────────────────────────


def test_complete_output_reports_as_compatible(tmp_path):
    _make_processed_video(tmp_path / "run" / "v")
    assert discover(tmp_path / "run")[0].compatibility == "ok"


def test_output_with_no_descriptions_is_usable_and_says_so(tmp_path):
    # Pictures and words only is the local-only default, not a defect.
    _make_processed_video(tmp_path / "run" / "v", descriptions=False)
    candidate = discover(tmp_path / "run")[0]

    assert candidate.compatibility == "no_visual"
    assert candidate.compatibility_label == "No descriptions"


def test_output_described_under_an_older_schema_is_flagged_not_refused(tmp_path):
    # Refusing it would strand work that is fine for most purposes.
    _make_processed_video(tmp_path / "run" / "v", old_schema=True)
    candidate = discover(tmp_path / "run")[0]

    assert candidate.compatibility == "provenance_mismatch"
    assert "older wording" in " ".join(candidate.warnings)


def test_missing_pictures_are_reported(tmp_path):
    _make_processed_video(tmp_path / "run" / "v", frames=False)
    candidate = discover(tmp_path / "run")[0]

    assert candidate.compatibility == "missing_artifacts"
    assert "text is fine" in " ".join(candidate.warnings) or "pictures are missing" in " ".join(
        candidate.warnings
    )


# ── Importing ─────────────────────────────────────────────────────────────


def test_importing_registers_the_videos(db, tmp_path):
    _make_processed_video(tmp_path / "run" / "v1", name="one.mp4")
    _make_processed_video(tmp_path / "run" / "v2", name="two.mp4")

    report = import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")

    assert report.imported == 2
    rows = db.execute("SELECT display_name, imported_from FROM job_videos").fetchall()
    assert {r["display_name"] for r in rows} == {"one.mp4", "two.mp4"}
    assert all(r["imported_from"] for r in rows)


def test_importing_never_modifies_the_imported_folder(db, tmp_path):
    """An import that damages what it reads is unrecoverable."""
    source = _make_processed_video(tmp_path / "run" / "v1")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}

    import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")

    after = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert after == before, "the imported folder was modified"


def test_importing_the_same_folder_twice_does_not_duplicate(db, tmp_path):
    # A second row pointing at the same output would make a collection include
    # the video twice without saying so.
    _make_processed_video(tmp_path / "run" / "v1")

    import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")
    second = import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")

    assert second.imported == 0
    assert second.skipped == 1
    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 1


def test_warnings_are_recorded_where_the_user_will_see_them(db, tmp_path):
    _make_processed_video(tmp_path / "run" / "v1", frames=False)
    import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")

    row = db.execute("SELECT message FROM events WHERE kind = 'import_warning'").fetchone()
    assert row is not None
    assert "clip.mp4" in row["message"]


def test_imported_videos_keep_their_discovered_order(db, tmp_path):
    for index in range(3):
        _make_processed_video(tmp_path / "run" / f"v{index}", name=f"clip{index}.mp4")

    import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")
    rows = db.execute("SELECT display_name, sequence FROM job_videos ORDER BY sequence").fetchall()
    assert [r["sequence"] for r in rows] == [0, 1, 2]


def test_importing_nothing_leaves_no_empty_job_behind(db, tmp_path):
    report = import_candidates(db, [], output_root=tmp_path / "out")
    assert report.problems
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_video_with_warnings_is_marked_completed_with_gaps(db, tmp_path):
    _make_processed_video(tmp_path / "run" / "v1", frames=False)
    import_candidates(db, discover(tmp_path / "run"), output_root=tmp_path / "out")

    status = db.execute("SELECT status FROM job_videos").fetchone()["status"]
    assert status == "completed_with_gaps"


# ── The CLI path ──────────────────────────────────────────────────────────


def test_the_import_command_succeeds(tmp_path, capsys):
    from app.core.config import Settings

    settings = Settings().with_output_root(tmp_path / "out")
    _make_processed_video(tmp_path / "run" / "v1")

    assert import_processed_output(settings, tmp_path / "run") == 0
    out = capsys.readouterr().out
    assert "Found 1 processed video" in out
    assert "Brought in 1" in out


def test_the_import_command_explains_an_empty_folder(tmp_path):
    from app.core.config import Settings

    settings = Settings().with_output_root(tmp_path / "out")
    (tmp_path / "empty").mkdir()
    assert import_processed_output(settings, tmp_path / "empty") == 1


def test_the_import_command_rejects_a_file(tmp_path):
    from app.core.config import Settings

    settings = Settings().with_output_root(tmp_path / "out")
    target = tmp_path / "a-file.txt"
    target.write_text("x", "utf-8")
    assert import_processed_output(settings, target) == 1


# ── Job finalisation ──────────────────────────────────────────────────────


def _seed_job(connection, tmp_path, video_count=1):
    root = tmp_path / "out"
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("j1", "Session review", "completed", str(root), 2000, utc_now(), utc_now()),
    )
    for index in range(video_count):
        video_dir = root / "j1" / f"v{index}"
        video_dir.mkdir(parents=True, exist_ok=True)
        (video_dir / "assembled.txt").write_text(f"assembled content {index}\n", "utf-8")
        frames = video_dir / "frames"
        frames.mkdir(exist_ok=True)
        (frames / "000000_t000000.jpg").write_bytes(b"\xff\xd8")

        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " status, frame_count, duration_seconds, output_dir, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"v{index}",
                "j1",
                f"/src/clip{index}.mp4",
                f"clip{index}.mp4",
                index,
                "completed",
                1,
                100.0,
                f"j1/v{index}",
                utc_now(),
                utc_now(),
            ),
        )
    return root


def test_a_single_video_job_gets_no_master_document(db, tmp_path):
    # A second file with identical content invites the reader to wonder which is
    # authoritative.
    root = _seed_job(db, tmp_path, video_count=1)
    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    assert result.master_path is None
    assert not (root / "j1" / "master_assembled.txt").exists()


def test_a_multi_video_job_gets_a_master_document(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=3)
    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    assert result.master_path is not None
    content = result.master_path.read_text(encoding="utf-8")
    assert "Videos            3" in content
    for index in range(3):
        assert f"assembled content {index}" in content


def test_the_handoff_folder_is_built(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=2)
    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    assert result.handoff_dir is not None
    assert (result.handoff_dir / "README.md").is_file()
    assert len(list(result.handoff_dir.glob("*_assembled.txt"))) >= 2


def test_provenance_records_the_settings_used(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=1)
    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    payload = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert payload["frame_interval_ms"] == 2000
    assert payload["video_count"] == 1
    assert payload["videos"][0]["display_name"] == "clip0.mp4"


def test_provenance_does_not_record_absolute_source_paths(db, tmp_path):
    # The layout of someone's disk is not part of the evidence and should not
    # travel with an export.
    root = _seed_job(db, tmp_path, video_count=1)
    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    text = result.provenance_path.read_text(encoding="utf-8")
    assert "/src/clip0.mp4" not in text
    assert "clip0.mp4" in text, "the filename itself is still useful"


def test_finalizing_is_safe_to_repeat(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=2)
    first = finalize_job(db, job_id="j1", job_name="Session", output_root=root)
    second = finalize_job(db, job_id="j1", job_name="Session", output_root=root)

    assert first.master_path.read_text("utf-8") == second.master_path.read_text("utf-8")
    assert (
        db.execute("SELECT COUNT(*) FROM artifacts WHERE kind = 'master_assembled'").fetchone()[0]
        == 1
    )


def test_a_video_without_an_assembled_document_is_reported_not_silently_dropped(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=2)
    (root / "j1" / "v1" / "assembled.txt").unlink()

    result = finalize_job(db, job_id="j1", job_name="Session", output_root=root)
    assert any("clip1.mp4" in w for w in result.warnings)


def test_a_job_with_no_finished_videos_finalizes_harmlessly(db, tmp_path):
    root = tmp_path / "out"
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Empty", "completed", str(root), utc_now(), utc_now()),
    )
    result = finalize_job(db, job_id="j1", job_name="Empty", output_root=root)
    assert result.video_count == 0
    assert result.master_path is None


def test_sources_are_collected_in_confirmed_order(db, tmp_path):
    root = _seed_job(db, tmp_path, video_count=3)
    sources = collect_sources(db, "j1", root)
    assert [row["sequence"] for row, _ in sources] == [0, 1, 2]
