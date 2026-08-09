"""Finding a finished job, finding its files, and reclaiming space from them.

Three defects, all about a job the user has stopped watching:

* nothing in the sidebar pointed at finished work, though the dashboard could
  always filter to it — so the route back to a completed job was to remember it
  and scroll;
* output folders were named with the job's UUID, so the output root was a list
  of hex strings and finding a job's files outside this application meant
  reading the database;
* removing files was all-or-nothing, which made 91 MB of scratch audio and the
  1 MB document the run exists to produce the same decision.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.services.cleanup import removable_groups, remove_groups
from app.services.jobs import RESERVED_DIRNAMES, slugify_dirname, unique_dirname
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
        yield test_client


def seed_job(connection, *, job_id="j1", name="Course", status="completed", dirname="course"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, output_dirname, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, name, status, "/out", 2000, "none", dirname, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, output_dir, is_active_version, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"{job_id}v1",
            job_id,
            "/src/a.mp4",
            "a.mp4",
            0,
            "completed",
            f"{dirname}/v1",
            1,
            utc_now(),
            utc_now(),
        ),
    )
    connection.commit()


def make_artifacts(root, relative="course/v1"):
    """A video folder shaped like a real one, with the real size ordering."""
    directory = root / relative
    (directory / "frames").mkdir(parents=True, exist_ok=True)
    (directory / "frames_api").mkdir(parents=True, exist_ok=True)
    (directory / "batches").mkdir(parents=True, exist_ok=True)
    (directory / "frames" / "000001.jpg").write_bytes(b"x" * 5000)
    (directory / "frames_api" / "000001.jpg").write_bytes(b"x" * 3000)
    (directory / "batches" / "000000_batch.json").write_text("{}")
    (directory / "audio.wav").write_bytes(b"x" * 9000)
    (directory / "transcript.txt").write_text("words")
    (directory / "visual_results.json").write_text("{}")
    (directory / "assembled.txt").write_text("the point of the whole run")
    return directory


# ── Finding a finished job ────────────────────────────────────────────────


def test_the_sidebar_points_at_finished_work(client, db):
    seed_job(db)
    body = client.get("/").text

    assert "Finished" in body
    assert "/?state=finished" in body


def test_the_finished_filter_shows_the_job(client, db):
    seed_job(db, name="Fifteen hour course")
    body = client.get("/?state=finished").text

    assert "Fifteen hour course" in body


def test_a_running_job_is_not_listed_as_finished(client, db):
    """The filter governs the list.

    A running job still appears in the "what is happening now" callout above it,
    which is deliberate and separate — someone checking finished work is still
    better off knowing something is running. So this asserts the *list* is
    empty, not that the name is absent from the page, which would have been
    testing the callout by accident.
    """
    seed_job(db, name="Still going", status="transcribing", dirname="still-going")
    body = client.get("/?state=finished").text

    assert "No jobs match that." in body, "a running job was listed under Finished"


# ── The folder carries the job's name ─────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Trendlines Video", "trendlines-video"),
        ("Session review — 14 Feb", "session-review-14-feb"),
        ("Sesión de práctica", "sesion-de-practica"),
        ("  ..hidden  ", "hidden"),
        ("my/../etc/passwd", "my-etcpasswd"),
    ],
)
def test_a_name_becomes_a_safe_folder(name, expected):
    assert slugify_dirname(name) == expected


@pytest.mark.parametrize("name", ["日本語", "", "   ", "!!!", "..."])
def test_a_name_with_nothing_usable_falls_back_rather_than_inventing(name):
    """An empty string is the caller's signal to use the identifier. Inventing
    something would put a job's files somewhere its name does not suggest."""
    assert slugify_dirname(name) == ""


@pytest.mark.parametrize("name", sorted(RESERVED_DIRNAMES))
def test_names_windows_refuses_are_not_used(name):
    """Checked on every platform. A folder later copied to a Windows machine
    should not be where this is discovered."""
    assert slugify_dirname(name) == ""
    assert slugify_dirname(name.upper()) == ""


def test_a_long_name_is_trimmed_and_left_tidy():
    slug = slugify_dirname("A very long name " * 20)
    assert len(slug) <= 60
    assert not slug.endswith("-")


def test_two_jobs_with_one_name_get_separate_folders(tmp_path):
    """ "Tuesday review" happens twice, and the second must not be written into
    the first one's folder."""
    (tmp_path / "tuesday-review").mkdir()

    assert unique_dirname(tmp_path, "tuesday-review", fallback="id") == "tuesday-review-2"


def test_the_folder_is_recorded_at_creation_not_derived_later(client, db, tmp_path):
    """Deriving it from the current name would move where the worker writes the
    moment a job is renamed, orphaning everything produced up to that point."""
    seed_job(db, name="Original name", dirname="original-name")

    client.post("/jobs/j1/rename", data={"name": "Something else"}, follow_redirects=False)

    row = db.execute("SELECT name, output_dirname FROM jobs WHERE id='j1'").fetchone()
    assert row["name"] == "Something else"
    assert row["output_dirname"] == "original-name", "renaming moved the output folder"


def test_a_job_from_before_folders_had_names_still_works(client, db):
    """Its column is NULL, and the fallback is the identifier — not a slug
    computed now, which would point at a folder holding none of its output."""
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("old", "Made earlier", "completed", "/out", 2000, "none", utc_now(), utc_now()),
    )
    db.commit()

    from app.web.app import job_folder

    row = db.execute("SELECT * FROM jobs WHERE id='old'").fetchone()
    assert job_folder(row) == "old"
    assert client.get("/jobs/old").status_code == 200


# ── Reclaiming space, by kind ─────────────────────────────────────────────


def test_each_kind_is_offered_with_its_size(settings, tmp_path):
    directory = make_artifacts(settings.output_root)
    groups = {g.key: g for g in removable_groups([directory])}

    assert groups["audio"].total_bytes == 9000
    assert groups["frames"].total_bytes == 5000
    assert groups["api_frames"].total_bytes == 3000
    assert all(g.consequence for g in groups.values()), "a kind with no stated cost"


def test_the_assembled_document_is_never_offered(settings):
    """It is what the run exists to produce. Offering it beside a scratch file
    would present them as comparable choices."""
    directory = make_artifacts(settings.output_root)
    groups = removable_groups([directory])

    assert not any("assembled" in g.key for g in groups)
    assert (directory / "assembled.txt").exists()


def test_removing_one_kind_leaves_the_others(settings):
    directory = make_artifacts(settings.output_root)

    result = remove_groups([directory], {"audio"}, output_root=settings.output_root)

    assert not (directory / "audio.wav").exists()
    assert result.freed_bytes == 9000
    assert (directory / "frames" / "000001.jpg").exists()
    assert (directory / "assembled.txt").exists()
    assert (directory / "transcript.txt").exists()


def test_nothing_outside_the_output_folder_is_ever_removed(settings, tmp_path):
    """This deletes directories recursively. A path assembled from a database
    row is still a path, and the check costs nothing."""
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "audio.wav").write_bytes(b"x" * 10)

    result = remove_groups([outside], {"audio"}, output_root=settings.output_root)

    assert (outside / "audio.wav").exists()
    assert result.problems


def test_removal_is_refused_while_the_worker_is_running(client, db, settings):
    make_artifacts(settings.output_root)
    seed_job(db, status="analyzing")

    response = client.post("/jobs/j1/files/remove", data={"group": "audio"}, follow_redirects=False)

    assert response.status_code == 409
    assert (settings.output_root / "course/v1/audio.wav").exists()


def test_removing_nothing_is_not_an_error(client, db, settings):
    make_artifacts(settings.output_root)
    seed_job(db)

    response = client.post("/jobs/j1/files/remove", data={}, follow_redirects=False)

    assert response.status_code == 303
    assert (settings.output_root / "course/v1/audio.wav").exists()


def test_what_was_removed_is_recorded_in_the_log(client, db, settings):
    make_artifacts(settings.output_root)
    seed_job(db)

    client.post("/jobs/j1/files/remove", data={"group": "audio"}, follow_redirects=False)

    row = db.execute(
        "SELECT message FROM events WHERE kind = 'files_removed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "space was freed and nothing recorded it"
    assert "extracted audio" in row["message"].lower()


def test_a_job_keeps_all_of_its_output_in_one_folder(settings, db, tmp_path):
    """The job-level package must land beside the per-video output it describes.

    It did not: the video folders were named after the job while finalize still
    built its path from the identifier, so one job's output was split across two
    directories — worse than either scheme on its own. No test noticed, because
    none exercised finalize against a named folder.
    """
    from app.pipeline.finalize import finalize_job

    seed_job(db, name="Trendlines Video", dirname="trendlines-video")
    video_dir = make_artifacts(settings.output_root, "trendlines-video/v1")
    (video_dir / "frames_manifest.json").write_text('{"frames": []}')

    finalize_job(
        db,
        job_id="j1",
        job_name="Trendlines Video",
        output_root=settings.output_root,
    )

    stray = [
        path.name
        for path in settings.output_root.iterdir()
        if path.is_dir() and path.name not in {"trendlines-video", "collections", "samples"}
    ]
    assert not stray, f"the job's output was split across folders: {stray}"
