"""A long stage must be visibly working while it works.

The defect these pin: ``items_total`` and ``items_done`` were both written when
a stage *finished*, so a stage in flight had no denominator, the bar sat at
exactly 0% for its whole run, and the only text on screen was the raw database
word "running". On a fifty-minute transcript that is an hour indistinguishable
from a hang; on a local description run it is most of a day of it.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.pipeline.progress import StageProgress, format_clock
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


def seed(connection, *, stage="transcribe", status="running", total=None, done=0):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("j1", "Course", "transcribing", "/out", 2000, "none", utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, output_dir, is_active_version, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("j1v1", "j1", "/src/a.mp4", "a.mp4", 0, "transcribing", "j1/v1", 1, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, items_total,"
        " items_done, started_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("s1", "j1v1", stage, status, total, done, utc_now(), utc_now(), utc_now()),
    )
    connection.commit()


# ── The writer ────────────────────────────────────────────────────────────


def test_the_total_is_published_before_any_work_is_done(db):
    """Until a denominator exists the interface has nothing to draw. Declaring
    it at the start is what stops a long stage looking like a stalled one."""
    seed(db)
    progress = StageProgress(db, "s1")
    progress.set_total(2978)

    row = db.execute("SELECT items_total, items_done FROM stage_runs WHERE id='s1'").fetchone()
    assert row["items_total"] == 2978
    assert row["items_done"] == 0


def test_progress_is_throttled_but_never_lost(db):
    """Writing on every segment would hammer SQLite while the worker heartbeat
    writes on another connection. The last value must still land."""
    seed(db)
    ticks = iter([0.0, 0.1, 0.2, 0.3, 100.0])
    progress = StageProgress(db, "s1", clock=lambda: next(ticks))
    progress.set_total(1000)

    progress.advance_to(10)  # throttled away
    progress.advance_to(20)  # throttled away
    progress.advance_to(30)  # clock has jumped; this one writes

    row = db.execute("SELECT items_done FROM stage_runs WHERE id='s1'").fetchone()
    assert row["items_done"] == 30


def test_finish_flushes_a_value_the_throttle_would_have_dropped(db):
    seed(db)
    progress = StageProgress(db, "s1", clock=lambda: 0.0)
    progress.set_total(100)
    progress.advance_to(99)
    progress.finish()

    row = db.execute("SELECT items_done FROM stage_runs WHERE id='s1'").fetchone()
    assert row["items_done"] == 99


def test_a_database_error_never_escapes_into_the_stage(db):
    """A dropped progress tick costs a moment of staleness. Raising out of a
    nine-hour transcription costs the transcription."""
    seed(db)
    progress = StageProgress(db, "s1")
    db.close()  # every later write will fail

    progress.set_total(10)
    progress.advance_to(5)
    progress.finish()  # must not raise


def test_progress_never_exceeds_the_total(db):
    """A rounding slip that reported 101% would render a bar past its own end."""
    seed(db)
    progress = StageProgress(db, "s1", clock=lambda: 0.0)
    progress.set_total(100)
    progress.advance_to(500)
    progress.finish()

    row = db.execute("SELECT items_done FROM stage_runs WHERE id='s1'").fetchone()
    assert row["items_done"] == 100


def test_the_still_working_event_is_rate_limited(db):
    """The event log already grows without bound. A progress line every few
    seconds through an eleven-hour stage is what would make that matter."""
    seen: list[tuple[int, int]] = []
    seed(db)
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10_000.0, 10_001.0])
    progress = StageProgress(
        db, "s1", on_event=lambda d, t: seen.append((d, t)), clock=lambda: next(ticks)
    )
    progress.set_total(1000)
    for value in (100, 200, 300):
        progress.advance_to(value)

    assert len(seen) <= 1, f"emitted {len(seen)} events for three quick ticks"


# ── What the screen shows ─────────────────────────────────────────────────


def test_a_running_stage_with_no_total_says_working_not_running(client, db):
    """ "running" is a database enum. Every other cell on this strip is written
    English — Done, Waiting, Not run — so the raw word read as a bug, and at 0%
    it also read as finished-at-nothing."""
    seed(db, total=None, done=0)
    body = client.get("/jobs/j1").text

    assert "Working" in body
    assert ">running<" not in body


def test_a_running_transcript_reports_clock_positions_not_a_tally(client, db):
    """Transcription measures seconds of video covered. "900 of 1,800" is a pair
    of meaningless numbers; "15:00 of 30:00" is a place in the video."""
    seed(db, stage="transcribe", total=1800, done=900)
    body = client.get("/jobs/j1").text

    assert "15:00 of 30:00" in body
    assert "900 of 1,800" not in body


def test_a_running_description_stage_counts_pictures(client, db):
    seed(db, stage="visual", total=1488, done=312)
    body = client.get("/jobs/j1").text

    assert "312 of 1,488" in body


def test_the_bar_actually_moves(client, db):
    """The whole point. A stage at 900 of 1,800 must render a half-full bar and
    not the 0% that every in-flight stage used to show."""
    seed(db, stage="transcribe", total=1800, done=900)
    body = client.get("/jobs/j1").text

    assert "width: 50%" in body


def test_format_clock_and_format_duration_agree(db):
    """Two spellings of the same number on two screens is how a user starts
    wondering which one is lying."""
    from app.web.status import format_duration

    for seconds in (0, 59, 60, 611, 3600, 4530):
        assert format_clock(seconds) == format_duration(seconds)


# ── Retried stages ────────────────────────────────────────────────────────


def _add_attempt(connection, *, run_id, stage, attempt, status, total, done):
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, attempt, status, items_total,"
        " items_done, started_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "j1v1", stage, attempt, status, total, done, utc_now(), utc_now(), utc_now()),
    )
    connection.commit()


def test_a_retried_stage_shows_the_live_attempt_not_the_abandoned_one(client, db):
    """Killing a worker mid-stage leaves the old attempt behind at no total.

    The row ids are random hex, so ordering by id picked whichever attempt
    happened to sort highest — and on a real machine that was the dead one. The
    screen reported "Pending, 0%" for hours while the live attempt ran correctly
    beside it. Ordering by attempt is the only ordering that means anything.

    The ids here are chosen so the abandoned attempt sorts *after* the live one,
    which is exactly the case that used to fail.
    """
    seed(db, stage="frames", status="completed", total=10, done=10)
    _add_attempt(
        db, run_id="zzz_dead", stage="visual", attempt=1, status="pending", total=None, done=0
    )
    _add_attempt(
        db, run_id="aaa_live", stage="visual", attempt=2, status="running", total=1488, done=744
    )

    body = client.get("/jobs/j1").text

    assert "744 of 1,488" in body, "the screen is reading the abandoned attempt"
    assert "width: 50%" in body


def test_the_percentage_ignores_an_abandoned_attempt(client, db):
    """An earlier try stuck at 0% would drag the average down for the whole job."""
    seed(db, stage="frames", status="completed", total=10, done=10)
    _add_attempt(
        db, run_id="zzz_dead", stage="visual", attempt=1, status="pending", total=None, done=0
    )
    _add_attempt(
        db, run_id="aaa_live", stage="visual", attempt=2, status="running", total=100, done=50
    )

    payload = client.get("/api/progress").json()
    # 100% of frames and 50% of the live visual attempt. The dead one is not a
    # third data point.
    assert payload["jobs"][0]["percent"] == 75


# ── When the estimate appears ─────────────────────────────────────────────


def _running_since(connection, *, seconds_ago, total, done):
    import datetime

    started = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()
    connection.execute(
        "UPDATE stage_runs SET started_at = ?, items_total = ?, items_done = ? WHERE id = 's1'",
        (started, total, done),
    )
    connection.commit()


def test_the_estimate_appears_early_on_a_very_long_stage(client, db):
    """Gated on evidence, not on fraction of the work.

    A 2% floor meant nothing appeared until 30 of 1,488 pictures were done —
    eleven minutes into a nine-hour run, which is precisely the stretch where
    someone is deciding whether to wait. Two minutes and 6 pictures is a
    thinner sample but an honest one, and it refines in view.
    """
    seed(db, stage="visual")
    _running_since(db, seconds_ago=120, total=1488, done=6)

    body = client.get("/jobs/j1").text
    assert " left</p>" in body, "no estimate after two minutes and six pictures"


def test_no_estimate_is_offered_in_the_first_seconds(client, db):
    """A rate from three seconds of a nine-hour stage is noise presented as
    information. Saying nothing is the honest output."""
    seed(db, stage="visual")
    _running_since(db, seconds_ago=8, total=1488, done=1)

    body = client.get("/jobs/j1").text
    assert " left</p>" not in body
    assert "nearly done" not in body


def test_the_estimate_reads_as_one_sentence(client, db):
    """It rendered "about about 7½ hours left" on a real screen for hours.

    format_span decides where a hedge belongs — "about 7½ hours", but a flat
    "5 minutes" — and the caller prepended a second "about" on top. The earlier
    test only checked that the phrase ended in " left", which a doubled word
    sails straight through. Assert the whole sentence.
    """
    import re

    seed(db, stage="visual")
    _running_since(db, seconds_ago=600, total=1488, done=53)

    body = client.get("/jobs/j1").text
    rendered = re.search(r">([^<>]*\bleft)</p>", body)
    assert rendered, "no estimate rendered"

    phrase = rendered.group(1)
    assert "about about" not in phrase, f"doubled qualifier: {phrase!r}"
    assert re.fullmatch(r"(about .+|under a minute|\d+ minutes) left", phrase), (
        f"estimate does not read as a sentence: {phrase!r}"
    )


def test_no_duration_phrase_doubles_its_qualifier():
    """Every value format_span can produce, through the caller that wraps it."""
    from app.web.status import format_span

    for seconds in (45, 120, 300, 3600, 7200, 27000, 40000, 400000):
        phrase = f"{format_span(seconds)} left"
        assert "about about" not in phrase, f"{seconds}s -> {phrase!r}"


# ── The estimate on a resumed stage ───────────────────────────────────────


def test_a_resumed_stage_does_not_count_inherited_work_as_speed(client, db):
    """The real numbers, from the run that exposed this.

    Attempt 4 inherited 570 pictures described on earlier attempts, then spent
    27 minutes describing 50 more. The screen read "about 36 minutes left"
    because it divided 27 minutes by all 620 — a rate thirteen times faster than
    anything happening. The honest remainder was nearly eight hours.

    The bar is right to show the inherited work; it is genuinely done. The clock
    must not be measured against it.
    """
    seed(db, stage="visual")
    _running_since(db, seconds_ago=27 * 60, total=1488, done=620)
    db.execute("UPDATE stage_runs SET items_skipped = 570 WHERE id = 's1'")
    db.commit()

    body = client.get("/jobs/j1").text

    assert "620 of 1,488" in body, "the bar should still show the inherited work"
    assert "36 minutes" not in body and "minutes left" not in body, (
        "the estimate is still measuring against work this run never performed"
    )
    assert "hours left" in body, "no hours-scale estimate for a stage with ~8 left"


def test_a_resume_offers_no_estimate_until_it_has_done_something_itself(client, db):
    """Inherited work alone is no evidence of speed, however much of it there is."""
    seed(db, stage="visual")
    _running_since(db, seconds_ago=300, total=1488, done=570)
    db.execute("UPDATE stage_runs SET items_skipped = 570 WHERE id = 's1'")
    db.commit()

    body = client.get("/jobs/j1").text
    assert " left</p>" not in body
