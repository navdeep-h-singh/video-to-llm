"""Saying what a job will do, before it does it.

The new job screen was a form and a button. You chose an interval and a service,
pressed start, and learned what you had agreed to afterwards — how many
pictures, how much disk, how long, and whether anything was about to leave the
machine.

That last one is why this exists. The product's central claim is that nothing is
uploaded unless you ask for it, and the single moment a user is weighing whether
to believe that is the moment the interface said nothing at all.

Two properties are load-bearing:

* **Measured or absent.** Frame counts and disk come from probing real files.
  Durations come from finished stages on this machine, and with no history there
  is no figure. An invented duration is worse than none — someone plans an
  afternoon around it.
* **The sentence about what leaves is never omitted**, in either direction.
  A reassurance that only appears sometimes reads as a hedge.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database
from app.services.plan import build_plan, human_bytes, human_duration
from app.web.app import create_app
from tests.fixtures.synthetic import ffmpeg_available, make_video
from tests.loopback import LOOPBACK_BASE_URL

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="the plan probes real files, so it needs ffmpeg"
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


@pytest.fixture
def video(tmp_path):
    return make_video(tmp_path / "clip.mp4", duration_seconds=4).path


# ── The figures ───────────────────────────────────────────────────────────


def test_a_plan_counts_the_pictures_the_job_would_take(db, settings, video):
    plan = build_plan(db, settings, paths=[video], interval_ms=1000)

    assert plan.ok, plan.problems
    assert plan.video_count == 1
    # Four seconds at one picture a second. Asserted exactly: a plan that
    # disagreed with the job it previews would be worse than no plan.
    assert plan.frame_count == 4
    assert plan.disk_label is not None


def test_the_interval_changes_the_count(db, settings, video):
    """The figure has to respond to the choice above it, or it is decoration."""
    dense = build_plan(db, settings, paths=[video], interval_ms=500)
    sparse = build_plan(db, settings, paths=[video], interval_ms=2000)

    assert dense.frame_count > sparse.frame_count


def test_no_history_means_no_time_rather_than_a_guess(db, settings, video):
    """A fresh install has measured nothing. It must say so."""
    plan = build_plan(db, settings, paths=[video], interval_ms=1000)

    assert plan.time_label is None
    assert plan.time_samples == 0


def test_nothing_chosen_produces_an_empty_plan(db, settings):
    plan = build_plan(db, settings, paths=[])
    assert not plan.ok
    assert plan.frame_count == 0


def test_a_file_that_cannot_be_processed_reports_the_real_problem(db, settings, tmp_path):
    """The plan runs the same preflight the create path runs, so a problem here
    is the problem that would actually stop the job — not a second opinion."""
    missing = tmp_path / "nope.mp4"
    plan = build_plan(db, settings, paths=[missing])

    assert not plan.ok
    assert plan.problems


# ── What leaves this computer ─────────────────────────────────────────────


def test_a_local_job_says_plainly_that_nothing_leaves(db, settings, video):
    plan = build_plan(db, settings, paths=[video], interval_ms=1000, provider="none")

    assert plan.leaves_anything is False
    assert plan.leaves == "Nothing leaves this computer. No network request will be made."
    assert plan.cost_label is None


def test_a_local_model_still_counts_as_nothing_leaving(db, settings, video):
    plan = build_plan(db, settings, paths=[video], interval_ms=1000, provider="ollama_local")

    assert plan.leaves_anything is False
    assert "Nothing leaves this computer" in plan.leaves
    assert plan.cost_label is None


def test_a_service_job_says_what_is_sent_and_what_is_not(db, settings, video):
    """Both halves matter. "Pictures are sent" without "the video is not" invites
    the reader to assume the worst, and the worst is not what happens."""
    plan = build_plan(db, settings, paths=[video], interval_ms=1000, provider="anthropic")

    assert plan.leaves_anything is True
    assert plan.leaves == (
        "4 still pictures will be sent to Claude. Your video and its audio are never sent."
    )


def test_a_service_job_shows_the_cost_and_the_cap_before_it_starts(db, settings, video):
    plan = build_plan(db, settings, paths=[video], interval_ms=1000, provider="anthropic")

    assert plan.cost_label is not None and plan.cost_label.startswith("$")
    assert plan.budget_label == "$25.00"


def test_a_job_that_cannot_start_promises_nothing_will_be_sent(db, settings, tmp_path):
    """A plan for an impossible job must not describe an upload that will not
    happen. It would read as a warning about something already decided."""
    plan = build_plan(db, settings, paths=[tmp_path / "nope.mp4"], provider="anthropic")

    assert not plan.ok
    assert "0 still pictures" in plan.leaves


# ── Rounding says only what is known ──────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, None),
        (0, None),
        (45, "under 2 minutes"),
        (600, "about 10 minutes"),
        (5400, "about 1.5 hours"),
        (72000, "about 20 hours"),
    ],
)
def test_durations_round_to_the_precision_they_have(seconds, expected):
    """A median of twenty samples does not support "about 3.7 hours"."""
    assert human_duration(seconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (0, None), (2048, "2.0 KB"), (5 * 1024**2, "5.0 MB")],
)
def test_sizes_read_as_sizes(value, expected):
    assert human_bytes(value) == expected


# ── Through the route ─────────────────────────────────────────────────────


def test_the_route_reports_the_same_plan_the_screen_shows(db, settings, video):
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    response = client.post(
        "/api/plan",
        data={"paths": str(video), "interval": "1000", "provider": "none"},
        headers={"Origin": LOOPBACK_BASE_URL},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["frame_count"] == 4
    assert body["leaves_anything"] is False


def test_the_route_resolves_the_service_card_the_way_the_create_route_does(db, settings, video):
    """The form sends provider=external plus the chosen service. If the plan
    resolved that differently from the create route, the preview would describe
    a different job from the one about to run."""
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    response = client.post(
        "/api/plan",
        data={
            "paths": str(video),
            "interval": "1000",
            "provider": "external",
            "service": "anthropic",
        },
        headers={"Origin": LOOPBACK_BASE_URL},
    )

    body = response.json()
    assert body["leaves_anything"] is True
    assert "Claude" in body["leaves"]


def test_the_plan_route_is_refused_from_another_origin(db, settings, video):
    """It probes the filesystem and reports what is on this disk. That is not
    something a page the user happens to have open gets to ask for."""
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    response = client.post(
        "/api/plan",
        data={"paths": str(video)},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_the_screen_carries_the_panel_the_script_fills(db, settings):
    """The markup and the script are edited separately, and a renamed id fails
    silently in the browser — the panel simply never appears."""
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    markup = client.get("/jobs/new").text

    for element in ("plan-state", "plan-figures", "plan-leaves", "plan-problem-list"):
        assert f'id="{element}"' in markup, f"the script writes to #{element}, which is not here"
    assert "/api/plan" in markup


def test_a_job_created_after_a_plan_takes_the_pictures_the_plan_promised(db, settings, video):
    """The plan and the job must agree about the interval.

    They parse the same form field through the same helper. This is the guard on
    that: a preview that promised 4 pictures and a job that took 8 would make
    the whole panel worse than useless.
    """
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    planned = client.post(
        "/api/plan",
        data={"paths": str(video), "interval": "1000", "provider": "none"},
        headers={"Origin": LOOPBACK_BASE_URL},
    ).json()

    client.post(
        "/jobs",
        data={"name": "Planned", "paths": str(video), "interval": "1000", "provider": "none"},
        headers={"Origin": LOOPBACK_BASE_URL},
        follow_redirects=False,
    )

    connection = open_database(settings.output_root, migrate_on_open=False)
    try:
        row = connection.execute(
            "SELECT frame_interval_ms FROM jobs WHERE name = ?", ("Planned",)
        ).fetchone()
        assert row is not None, "the job was not created, so this asserts nothing"
        assert row["frame_interval_ms"] == 1000
        assert planned["frame_count"] == 4
    finally:
        connection.close()


def test_a_first_run_with_no_database_answers_rather_than_failing():
    """Before an output folder is chosen there is nothing to plan against.

    The panel has to be told that in a shape it can render. A 500 into a fetch
    shows the user an empty box and no reason for it.
    """
    client = TestClient(create_app(Settings()), base_url=LOOPBACK_BASE_URL)
    response = client.post("/api/plan", data={"paths": ""}, headers={"Origin": LOOPBACK_BASE_URL})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["problems"]
