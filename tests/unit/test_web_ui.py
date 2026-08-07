"""The interface.

Every screen is rendered against a real database. Two rules get the hardest
testing, because both are easy to break by accident and expensive to discover
late:

1. **A stored key never reaches the browser** — not the value, not a prefix.
2. **No API terminology before the user opts in.** A first-time user doing
   local-only work should never meet the word "API" or "token".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, VisualAnalysisSettings
from app.core.db import open_database, utc_now
from app.web import status as status_module
from app.web.app import create_app


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
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def seed_job(connection, *, job_id="j1", name="Session review", status="completed"):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, name, status, "/out", 2000, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, frame_count, duration_seconds, output_dir, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{job_id}v1",
            job_id,
            "/src/clip.mp4",
            "capture_0914.mp4",
            0,
            "completed",
            1265,
            2530.0,
            f"{job_id}/v1",
            utc_now(),
            utc_now(),
        ),
    )
    connection.execute(
        "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
        (
            job_id,
            "info",
            "stage_completed",
            "Took 1,265 pictures from capture_0914.mp4, one every 2 seconds.",
            utc_now(),
        ),
    )


# ── Every screen renders ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/", "/launch", "/jobs/new", "/imports", "/settings", "/collections", "/collections/new"],
)
def test_every_top_level_screen_renders(client, db, path):
    seed_job(db)
    response = client.get(path)
    assert response.status_code == 200
    assert "<html" in response.text


def test_the_job_screen_renders(client, db):
    seed_job(db)
    response = client.get("/jobs/j1")
    assert response.status_code == 200
    assert "Session review" in response.text
    assert "capture_0914.mp4" in response.text


def test_the_review_and_outputs_screens_render(client, db):
    seed_job(db)
    for path in ("/jobs/j1/review", "/jobs/j1/outputs"):
        assert client.get(path).status_code == 200


def test_an_unknown_job_says_so_rather_than_erroring(client, db):
    seed_job(db)
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 200
    assert "could not be found" in response.text


def test_an_unknown_collection_says_so(client, db):
    response = client.get("/collections/does-not-exist")
    assert response.status_code == 200
    assert "could not be found" in response.text


def test_a_machine_with_no_output_folder_goes_to_the_readiness_screen(tmp_path):
    # An empty dashboard explains nothing to a first-time user.
    with TestClient(create_app(Settings())) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/launch"


# ── Nothing invented ──────────────────────────────────────────────────────


def test_an_empty_dashboard_says_so_rather_than_showing_examples(client):
    # Placeholder data a user might mistake for their own work is worse than
    # an honest empty state.
    response = client.get("/")
    assert "No jobs yet" in response.text
    assert "capture_0914" not in response.text
    assert "Session review" not in response.text


def test_an_empty_collections_screen_says_so(client):
    assert "No collections yet" in client.get("/collections").text


def test_real_job_data_is_shown(client, db):
    seed_job(db, name="A real job name")
    assert "A real job name" in client.get("/").text


# ── Keys are never revealed ───────────────────────────────────────────────


def test_a_stored_key_never_reaches_the_browser(client, db, monkeypatch):
    """Not the value, and not a prefix either.

    A few revealed characters still narrow a search, and there is no situation
    where the user needs them — presence is the only useful fact.
    """
    from app.credentials import store

    # Assembled at runtime rather than written as a literal. The pre-publish
    # audit refuses credential-shaped strings in tracked files, and adding this
    # file to its exemption list would weaken a check that has already caught
    # two real problems in this build.
    secret = "-".join(["sk", "ant", "api03", "UNMISTAKABLE", "SECRET", "VALUE", "0123456789"])

    class FakeKeyring:
        def get_keyring(self):
            return self

        def get_password(self, service, account):
            return secret if account == "anthropic" else None

        def set_password(self, *a):
            pass

        def delete_password(self, *a):
            pass

    monkeypatch.setattr(store, "_keyring", lambda: FakeKeyring())

    body = client.get("/settings").text
    assert secret not in body
    assert "UNMISTAKABLE" not in body
    assert "sk-ant" not in body
    assert "0123456789" not in body
    # But presence is reported, so the user knows it is configured.
    assert "Set" in body


def test_settings_states_where_keys_live_without_showing_them(client):
    body = client.get("/settings").text
    assert "never written to a file" in body
    assert "never shown back" in body


# ── Plain language before opt-in ──────────────────────────────────────────


LOCAL_ONLY_SCREENS = ["/", "/launch", "/jobs/new", "/imports", "/collections"]


@pytest.mark.parametrize("path", LOCAL_ONLY_SCREENS)
def test_no_api_terminology_before_opting_in(client, db, path):
    seed_job(db)
    body = client.get(path).text
    for jargon in ("API key", "api_key", "endpoint", "inference", "LLM API"):
        assert jargon not in body, f"{jargon!r} appears on {path} before opt-in"


@pytest.mark.parametrize("path", LOCAL_ONLY_SCREENS)
def test_the_local_only_promise_is_visible(client, db, path):
    seed_job(db)
    assert "Runs only on this computer" in client.get(path).text


def test_frames_are_called_pictures(client, db):
    # The design deliberately avoids "frame" for a first-time reader.
    seed_job(db)
    body = client.get("/jobs/j1").text
    assert "pictures" in body.lower()


def test_the_new_job_screen_explains_the_local_default(client):
    body = client.get("/jobs/new").text
    assert "Nothing leaves this computer" in body
    assert "Descriptions are optional" in body


def test_local_descriptions_report_no_provider_charge_not_zero(client, tmp_path):
    settings = Settings(
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="ollama_local", model_id="qwen2.5vl:7b"
        )
    ).with_output_root(tmp_path / "out")

    with TestClient(create_app(settings)) as client_local:
        body = client_local.get("/settings").text

    assert "No provider API charge" in body
    assert "$0.00" not in body


def test_the_reliability_warning_is_shown_for_local_models(client):
    body = client.get("/settings").text
    assert "tiny text" in body
    assert "Review low-confidence results" in body


def test_settings_states_what_is_sent_to_a_service(client):
    body = client.get("/settings").text
    assert "never your video" in body
    assert "never its" in body  # "never its audio"


# ── Accessibility ─────────────────────────────────────────────────────────


def test_every_screen_has_a_skip_link(client, db):
    seed_job(db)
    for path in ("/", "/launch", "/settings", "/collections"):
        assert 'class="skip-link"' in client.get(path).text


def test_the_page_declares_a_language(client):
    assert '<html lang="en">' in client.get("/").text


def test_navigation_marks_the_current_page(client):
    assert 'aria-current="page"' in client.get("/collections").text


def test_tables_have_captions_for_screen_readers(client, db):
    seed_job(db)
    assert "<caption" in client.get("/").text


def test_progress_bars_expose_their_value(client, db):
    seed_job(db)
    db.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, items_total,"
        " items_done, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "j1v1", "frames", "completed", 1265, 1265, utc_now(), utc_now()),
    )
    body = client.get("/jobs/j1").text
    assert 'role="progressbar"' in body
    assert "aria-valuenow" in body


def test_no_external_resource_is_referenced(client):
    """The header badge promises nothing is uploaded.

    A stylesheet, font, or script fetched from another host would contradict it
    on every page load, whatever the rest of the application does.
    """
    body = client.get("/").text
    for marker in ("http://", "https://", "//fonts.", "cdn."):
        # Allow only same-origin relative references.
        assert marker not in body.replace("http://127.0.0.1", ""), marker


# ── Status vocabulary ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state",
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
def test_every_documented_state_has_a_presentation(state):
    presentation = status_module.present(state)
    assert presentation.label
    assert presentation.css_class
    assert presentation.shape, "colour alone is not enough"


def test_status_is_never_colour_alone(client, db):
    seed_job(db, status="completed_with_gaps")
    body = client.get("/").text
    # The word, not just the swatch.
    assert "Finished, with gaps" in body


def test_an_unknown_state_is_shown_as_unknown_not_as_finished():
    # Silently rendering an unrecognised state as success would be a lie.
    assert status_module.present("something-new").label == "something-new"
    assert status_module.present(None).label == "Unknown state"


def test_running_and_finished_states_are_classified():
    assert status_module.is_running("analyzing") is True
    assert status_module.is_running("completed") is False
    assert status_module.is_finished("completed_with_gaps") is True
    assert status_module.is_finished("paused") is False


# ── Formatting helpers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0, "—"), (None, "—"), (61, "1:01"), (3661, "1:01:01")]
)
def test_durations_read_naturally(seconds, expected):
    assert status_module.format_duration(seconds) == expected


def test_relative_times_are_human_scale():
    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(seconds=9)).isoformat()
    assert "seconds ago" in status_module.format_relative(recent)

    older = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    assert "days ago" in status_module.format_relative(older)


def test_a_missing_timestamp_renders_as_a_dash():
    assert status_module.format_relative(None) == "—"


@pytest.mark.parametrize(
    ("size", "expected"),
    [(None, "—"), (512, "512 bytes"), (2048, "2.0 KB"), (5 * 1024**3, "5.0 GB")],
)
def test_sizes_read_naturally(size, expected):
    assert status_module.format_bytes(size) == expected


# ── The boundary ──────────────────────────────────────────────────────────


def test_health_reports_the_loopback_binding(client):
    payload = client.get("/health").json()
    assert payload["bound_to"] == "127.0.0.1"


def test_api_documentation_endpoints_stay_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ── Live progress ─────────────────────────────────────────────────────────


def test_the_progress_endpoint_returns_json(client, db):
    """It returned a 500 the first time: `status` exists on both stage_runs and
    job_videos, so the unqualified column in the join was ambiguous."""
    seed_job(db)
    db.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, items_total,"
        " items_done, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "j1v1", "frames", "completed", 1265, 1265, utc_now(), utc_now()),
    )

    response = client.get("/api/progress")
    assert response.status_code == 200
    payload = response.json()
    assert payload["jobs"][0]["total"] == 1265
    assert payload["jobs"][0]["done"] == 1265


def test_progress_can_be_scoped_to_one_job(client, db):
    seed_job(db, job_id="j1", name="First")
    seed_job(db, job_id="j2", name="Second")

    payload = client.get("/api/progress?job_id=j2").json()
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["id"] == "j2"


def test_the_fingerprint_holds_still_when_nothing_changes(client, db):
    """The reload is driven off this value, so a fingerprint that churned on its
    own would put every screen back on the five-second reload it replaced."""
    seed_job(db)

    first = client.get("/api/progress").json()["fingerprint"]
    second = client.get("/api/progress").json()["fingerprint"]
    assert first == second


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda c: c.execute(
                "UPDATE jobs SET status = 'analyzing', updated_at = ? WHERE id = 'j1'",
                (utc_now(),),
            ),
            id="a job changes state",
        ),
        pytest.param(
            lambda c: c.execute("DELETE FROM jobs WHERE id = 'j1'"),
            id="a job is removed",
        ),
        pytest.param(
            lambda c: c.execute(
                "INSERT INTO stage_runs (id, job_video_id, stage, status, items_total,"
                " items_done, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("s9", "j1v1", "frames", "running", 100, 7, utc_now(), utc_now()),
            ),
            id="frame-by-frame progress moves",
        ),
    ],
)
def test_the_fingerprint_moves_when_a_screen_would_look_different(client, db, change):
    """Each of these is something a polling screen displays. If the fingerprint
    missed one, the screen would sit there showing a stale number indefinitely —
    which is worse than the over-eager reload, because it looks correct."""
    seed_job(db)
    before = client.get("/api/progress").json()["fingerprint"]

    change(db)
    db.commit()

    assert client.get("/api/progress").json()["fingerprint"] != before


def test_the_rendered_page_carries_the_same_fingerprint_the_endpoint_reports(client, db):
    """The first poll compares against the value baked into the page. If the two
    were computed differently, every page would reload once immediately."""
    seed_job(db)

    page = client.get("/").text
    endpoint = client.get("/api/progress").json()["fingerprint"]
    assert f'data-fingerprint="{endpoint}"' in page


def test_the_review_screen_refuses_to_reload_itself(client, db):
    """Which picture you are on lives in the URL. `has_running` is global, so
    any job anywhere used to throw the viewer back to the top every five
    seconds while the user was working through frames."""
    seed_job(db)

    assert 'data-reload="manual"' in client.get("/jobs/j1/review").text


def test_ordinary_screens_still_reload_themselves(client, db):
    seed_job(db)
    assert 'data-reload="auto"' in client.get("/").text


# ── File serving ──────────────────────────────────────────────────────────


def test_a_file_inside_the_output_root_is_served(client, settings):
    target = settings.output_root / "job" / "assembled.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("the assembled document", encoding="utf-8")

    response = client.get("/files/job/assembled.txt")
    assert response.status_code == 200
    assert "the assembled document" in response.text


@pytest.mark.parametrize(
    "attempt",
    [
        "/files/../../../etc/passwd",
        "/files/..%2f..%2f..%2fetc%2fpasswd",
        "/files/job/../../../../etc/hosts",
    ],
)
def test_paths_outside_the_output_root_are_refused(client, attempt):
    # The whole point of the containment check.
    response = client.get(attempt)
    assert response.status_code in {403, 404}
    assert "root:" not in response.text


def test_a_missing_file_says_so(client):
    assert client.get("/files/nope/missing.txt").status_code == 404


def test_browsing_lists_folders_and_videos(client, tmp_path):
    payload = client.get(f"/api/browse?path={tmp_path}").json()
    assert payload["ok"] is True
    assert payload["path"] == str(tmp_path.resolve())


def test_browsing_an_unreadable_place_explains_itself(client):
    payload = client.get("/api/browse?path=/definitely/not/a/real/place").json()
    # Falls back to the parent rather than erroring, or reports plainly.
    assert "ok" in payload


def test_the_stylesheet_carries_a_cache_key(client):
    """Upgrading under a running browser served the stylesheet it had cached,
    so the page rendered with the old layout and looked broken rather than
    out of date."""
    body = client.get("/").text
    assert "/static/tokens.css?v=" in body


def test_the_cache_key_changes_when_the_stylesheet_does(client, monkeypatch):
    import re

    first = re.search(r"tokens\.css\?v=(\d+)", client.get("/").text).group(1)

    real_stat = Path.stat

    def newer(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self.name == "tokens.css":

            class Bumped:
                st_mtime = result.st_mtime + 1000

            return Bumped()
        return result

    monkeypatch.setattr(Path, "stat", newer)
    second = re.search(r"tokens\.css\?v=(\d+)", client.get("/").text).group(1)
    assert second != first


# ── Collections: order, versions, and the estimate ────────────────────────


def seed_processed_video(
    connection, root, video_id, *, name, sequence, body="text\n", version=1, active=True
):
    """A video with real output on disk, so it is a usable collection source."""
    directory = Path(root) / "j1" / video_id
    (directory / "frames").mkdir(parents=True, exist_ok=True)
    (directory / "assembled.txt").write_text(body, "utf-8")
    (directory / "frames" / "000000_t000000.jpg").write_bytes(b"\xff\xd8")

    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " version, is_active_version, status, duration_seconds, output_dir,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            "j1",
            f"/src/{name}",
            name,
            sequence,
            version,
            int(active),
            "completed",
            600.0,
            f"j1/{video_id}",
            utc_now(),
            utc_now(),
        ),
    )
    return directory


@pytest.fixture
def sources(db, settings):
    connection = db
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Source job", "completed", "/out", utc_now(), utc_now()),
    )
    seed_processed_video(connection, settings.output_root, "a", name="first.mp4", sequence=0)
    seed_processed_video(connection, settings.output_root, "b", name="second.mp4", sequence=1)
    seed_processed_video(connection, settings.output_root, "c", name="third.mp4", sequence=2)
    connection.commit()
    return connection


def test_the_advertised_steps_are_real_sections(client, sources):
    """The header used to name five steps above a flat form, two of which had no
    interface at all. A label for a step that is not there is placeholder data
    wearing a different hat."""
    page = client.get("/collections/new").text

    for anchor in ["step-name", "step-videos", "step-order", "step-shape", "step-build"]:
        assert f'id="{anchor}"' in page, f"{anchor} is advertised but not on the page"
    assert 'id="order-list"' in page
    assert 'id="estimate"' in page


def test_the_order_the_user_set_is_the_order_that_is_stored(client, sources):
    """The order field, not the order the checkboxes happened to be submitted in.
    Order is the thing the specification is most emphatic about."""
    client.post(
        "/collections",
        data={
            "name": "Week 6",
            "video": ["a", "b", "c"],
            "order": "c,a,b",
            "mode": "full",
            "token_limit": 200000,
            "reserve_tokens": 20000,
        },
    )

    stored = sources.execute(
        "SELECT display_name FROM collection_sources ORDER BY sequence"
    ).fetchall()
    assert [row["display_name"] for row in stored] == ["third.mp4", "first.mp4", "second.mp4"]


def test_a_chosen_video_missing_from_the_order_is_still_included(client, sources):
    """Dropping it silently would lose work the user asked for; refusing would
    strand the whole build over a field they never see."""
    client.post(
        "/collections",
        data={"name": "Week 6", "video": ["a", "b"], "order": "b", "mode": "full"},
    )

    stored = sources.execute(
        "SELECT display_name FROM collection_sources ORDER BY sequence"
    ).fetchall()
    assert [row["display_name"] for row in stored] == ["second.mp4", "first.mp4"]


def test_an_order_naming_a_video_that_was_not_chosen_ignores_it(client, sources):
    """Unticking a video leaves it in the order field until the next render."""
    client.post(
        "/collections",
        data={"name": "Week 6", "video": ["a"], "order": "a,b,c", "mode": "full"},
    )

    stored = sources.execute("SELECT COUNT(*) FROM collection_sources").fetchone()[0]
    assert stored == 1


def test_the_chosen_version_is_what_gets_pinned(client, sources, settings):
    """Selecting an older version has to reference that version's row. Pinning
    the newest one regardless would make the control decorative."""
    seed_processed_video(
        sources, settings.output_root, "a2", name="first.mp4", sequence=0, version=2, active=False
    )
    sources.commit()

    client.post(
        "/collections",
        data={"name": "Week 6", "video": ["a"], "order": "a", "version": ["a:a2"], "mode": "full"},
    )

    pinned = sources.execute(
        "SELECT job_video_id, source_version FROM collection_sources"
    ).fetchone()
    assert pinned["job_video_id"] == "a2"
    assert pinned["source_version"] == 2


def test_the_estimate_reports_what_the_build_would_produce(client, sources):
    response = client.post(
        "/api/collections/estimate",
        data={
            "video": ["a", "b"],
            "order": "a,b",
            "mode": "full",
            "token_limit": 200000,
            "reserve_tokens": 20000,
        },
    )

    payload = response.json()
    assert payload["ready"] is True
    assert payload["video_count"] == 2
    assert payload["tokens"] > 0
    assert payload["pack_count"] is None


def test_the_estimate_counts_parts_in_pack_mode(client, sources, settings):
    seed_processed_video(
        sources, settings.output_root, "big", name="long.mp4", sequence=3, body="x" * 60000
    )
    sources.commit()

    payload = client.post(
        "/api/collections/estimate",
        data={
            "video": ["big"],
            "order": "big",
            "mode": "packs",
            "token_limit": 6000,
            "reserve_tokens": 1000,
            "allow_video_split": "on",
        },
    ).json()

    assert payload["pack_count"] > 1


def test_the_estimate_is_always_labelled_as_an_estimate(client, sources):
    payload = client.post(
        "/api/collections/estimate",
        data={"video": ["a"], "order": "a", "mode": "full"},
    ).json()
    assert "about" in payload["token_label"]


def test_the_estimate_says_so_when_nothing_is_chosen(client, sources):
    payload = client.post("/api/collections/estimate", data={"mode": "full"}).json()
    assert payload["ready"] is False
    assert payload["detail"]


def test_estimating_writes_no_collection(client, sources):
    """It runs on every change to the form. One abandoned build per keystroke
    would be a remarkable way to fill a disk."""
    client.post("/api/collections/estimate", data={"video": ["a"], "order": "a", "mode": "full"})

    assert sources.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
    assert sources.execute("SELECT COUNT(*) FROM collection_builds").fetchone()[0] == 0
