"""Configuring the on-device model through the interface.

The Settings screen was previously read-only: it displayed the Ollama
configuration but offered no way to change it, and "Check local model" was a
link back to the same page. These tests exist so that cannot regress into a
display surface again.

No network call is made here — the health check is mocked. The live variant is
in `tests/integration/test_live_ollama.py`.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import Settings, load_settings, render_settings_toml, save_settings
from app.core.db import open_database
from app.providers.base import ProviderHealth
from app.web.app import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    """Redirect saves to a temporary file so tests never touch the real one."""
    target = tmp_path / "config" / "settings.toml"
    monkeypatch.setattr("app.core.config.settings_file", lambda: target)
    return target


@pytest.fixture
def client(settings, settings_path):
    connection = open_database(settings.output_root)
    connection.close()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ── Saving to disk ────────────────────────────────────────────────────────


def test_settings_round_trip_through_a_file(tmp_path, monkeypatch):
    target = tmp_path / "settings.toml"
    original = Settings().with_output_root(tmp_path / "out")

    from dataclasses import replace

    configured = replace(
        original,
        visual_analysis=replace(
            original.visual_analysis,
            enabled=True,
            provider="ollama_local",
            model_id="qwen2.5vl:7b",
        ),
    )
    save_settings(configured, path=target)

    monkeypatch.setattr("app.core.config.settings_file", lambda: target)
    reloaded = load_settings()

    assert reloaded.visual_analysis.enabled is True
    assert reloaded.visual_analysis.provider == "ollama_local"
    assert reloaded.visual_analysis.model_id == "qwen2.5vl:7b"


def test_the_output_root_survives_a_save(tmp_path):
    # Losing it would silently reset the application to first-run.
    target = tmp_path / "settings.toml"
    original = Settings().with_output_root(tmp_path / "chosen")
    save_settings(original, path=target)

    assert str(tmp_path / "chosen") in target.read_text(encoding="utf-8")


def test_an_invalid_configuration_is_refused_before_it_is_written(tmp_path):
    """A settings file that stops the application starting is hard to recover
    from. Validation runs before the write, not after."""
    from dataclasses import replace

    from app.core.config import NonLoopbackAddressError

    target = tmp_path / "settings.toml"
    original = Settings().with_output_root(tmp_path / "out")
    broken = replace(
        original, ollama=replace(original.ollama, endpoint="http://192.168.1.50:11434")
    )

    with pytest.raises(NonLoopbackAddressError):
        save_settings(broken, path=target)
    assert not target.exists(), "an invalid configuration must not reach disk"


def test_the_written_file_keeps_its_comments(tmp_path):
    # This is the file someone opens when the interface is not in front of them.
    rendered = render_settings_toml(Settings().with_output_root(tmp_path / "out"))
    assert "# Written by Video to LLM" in rendered
    assert "Loopback hosts only" in rendered


def test_booleans_render_as_toml_booleans(tmp_path):
    from dataclasses import replace

    original = Settings()
    configured = replace(original, visual_analysis=replace(original.visual_analysis, enabled=True))
    assert "enabled = true" in render_settings_toml(configured)


# ── Saving through the interface ──────────────────────────────────────────


def test_the_on_device_model_can_be_configured(client, settings_path):
    response = client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
            "acknowledged": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    written = settings_path.read_text(encoding="utf-8")
    assert 'provider = "ollama_local"' in written
    assert 'model_id = "qwen2.5vl:7b"' in written
    assert "enabled = true" in written


def test_a_saved_setting_is_visible_immediately(client, settings_path):
    """Settings are a frozen dataclass captured by the route closures.

    Without rebinding, a save would land on disk and every screen would keep
    showing the old values until the process restarted.
    """
    client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "2",
        },
    )
    body = client.get("/settings").text
    assert "qwen2.5vl:7b" in body


def test_a_new_job_uses_the_saved_provider(client, settings_path):
    client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
        },
    )
    # The new-job screen offers the local option; the saved model is what a job
    # will actually use.
    assert "On this computer" in client.get("/jobs/new").text


def test_turning_descriptions_on_without_naming_a_model_is_refused(client, settings_path):
    response = client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "   ",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Name the model you installed" in response.text
    assert not settings_path.exists(), "an incomplete configuration must not be saved"


def test_a_non_loopback_endpoint_is_refused_with_an_explanation(client, settings_path):
    response = client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://192.168.1.50:11434",
            "batch_size": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "not on this computer" in response.text.lower() or "loopback" in response.text.lower()
    assert not settings_path.exists()


def test_the_batch_size_is_clamped_rather_than_trusted(client, settings_path):
    # A cloud-sized batch against a local model exhausts memory minutes in.
    client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "20",
        },
    )
    assert "batch_size = 4" in settings_path.read_text(encoding="utf-8")


def test_choosing_none_turns_descriptions_off(client, settings_path):
    client.post(
        "/settings",
        data={"enabled": "1", "provider": "none", "endpoint": "http://127.0.0.1:11434"},
    )
    assert "enabled = false" in settings_path.read_text(encoding="utf-8")


def test_a_successful_save_is_confirmed(client, settings_path):
    body = client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
        },
        follow_redirects=True,
    ).text
    assert "Saved" in body


# ── Check local model ─────────────────────────────────────────────────────


def _mock_health(monkeypatch, health: ProviderHealth):
    monkeypatch.setattr(
        "app.providers.ollama_local.OllamaLocalProvider.health_check", lambda self: health
    )


def test_the_check_reports_a_working_model(client, monkeypatch):
    _mock_health(
        monkeypatch,
        ProviderHealth(
            reachable=True,
            detail="Ollama 0.32.6 is answering and qwen2.5vl:7b is installed.",
            runtime_version="0.32.6",
            model_available=True,
            vision_capable=True,
        ),
    )
    body = client.post("/settings/check-local", follow_redirects=False).text

    assert "0.32.6" in body
    assert "Found" in body
    assert "Can read pictures" in body


def test_the_check_reports_nothing_answering(client, monkeypatch):
    _mock_health(
        monkeypatch,
        ProviderHealth(
            reachable=False,
            detail="Nothing is answering on this computer at http://127.0.0.1:11434.",
            remediation="Install Ollama from https://ollama.com and start it.",
        ),
    )
    body = client.post("/settings/check-local", follow_redirects=False).text

    assert "Nothing is answering" in body
    assert "ollama.com" in body


def test_the_check_reports_a_missing_model_with_the_pull_command(client, monkeypatch):
    _mock_health(
        monkeypatch,
        ProviderHealth(
            reachable=True,
            detail="Ollama is answering, but qwen2.5vl:7b is not installed.",
            runtime_version="0.32.6",
            model_available=False,
            remediation="Install it, then check again:\n  ollama pull qwen2.5vl:7b",
        ),
    )
    body = client.post("/settings/check-local", follow_redirects=False).text

    assert "Not installed" in body
    assert "ollama pull qwen2.5vl:7b" in body


def test_unverified_vision_is_reported_honestly(client, monkeypatch):
    # Not "probably fine". The spec requires this exact wording.
    _mock_health(
        monkeypatch,
        ProviderHealth(
            reachable=True,
            detail="Installed.",
            runtime_version="0.32.6",
            model_available=True,
            vision_capable=None,
        ),
    )
    body = client.post("/settings/check-local", follow_redirects=False).text
    assert "Vision capability not verified" in body


def test_a_failing_check_does_not_break_the_screen(client, monkeypatch):
    def explode(self):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr("app.providers.ollama_local.OllamaLocalProvider.health_check", explode)
    response = client.post("/settings/check-local", follow_redirects=False)

    assert response.status_code == 200
    assert "could not run" in response.text


def test_the_check_button_posts_rather_than_linking_nowhere(client):
    # It used to be an anchor back to /settings, which did nothing at all.
    body = client.get("/settings").text
    assert 'action="/settings/check-local"' in body


# ── The screen is still honest about keys ─────────────────────────────────


def test_the_form_never_renders_a_stored_key(client, monkeypatch):
    from app.credentials import store

    secret = "-".join(["sk", "ant", "api03", "SETTINGS", "FORM", "SECRET", "0123456789"])

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
    assert "SETTINGS" not in body
    assert "Set" in body


def test_the_local_disclosures_survive_the_rewrite(client):
    body = client.get("/settings").text
    assert "Frames stay on this device" in body
    assert "No provider API charge" in body
    assert "$0.00" not in body
    assert "tiny text" in body
    assert "Review low-confidence results" in body


# ── Surviving an upgrade while the server is running ──────────────────────
#
# Jinja loads templates from disk on every request; route code lives in the
# running process's memory. Upgrading a checkout under a running server
# therefore renders new templates against an old context. This produced a bare
# "Internal Server Error" on /settings, on a machine where the server had been
# up for five hours.


def test_the_settings_screen_renders_without_the_new_context(tmp_path):
    """The exact stale-process case, reproduced directly.

    `settings` is passed by every route in every version, so the form falls
    back to it rather than depending on a variable an older route never sent.
    """
    from jinja2 import Environment, FileSystemLoader

    from app.services.doctor import run_doctor
    from app.web.app import TEMPLATE_DIR

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    active = Settings().with_output_root(tmp_path / "out")

    rendered = env.get_template("settings.html").render(
        settings=active,
        report=run_doctor(active),
        credentials=[],
        env_vars={},
        nav_groups=[],
        worker=None,
        disk_label="",
        status=None,
        request=None,
    )
    assert "Describing what is on screen" in rendered


def test_an_unhandled_error_explains_itself_instead_of_saying_nothing():
    from app.web.app import create_app

    app = create_app(Settings())

    @app.get("/deliberately-broken")
    def broken(request: Request) -> None:
        raise RuntimeError("a synthetic failure")

    with TestClient(app, raise_server_exceptions=False) as broken_client:
        response = broken_client.get("/deliberately-broken")

    assert response.status_code == 500
    assert "Your jobs and files are unaffected" in response.text
    assert "start it again" in response.text


def test_the_error_page_does_not_leak_the_exception():
    """This page is reachable without authentication.

    An exception message can carry a path, a query, or a credential, so the
    detail goes to the log and never to the browser.
    """
    from app.web.app import create_app

    app = create_app(Settings())
    secret = "-".join(["sk", "ant", "api03", "LEAKED", "THROUGH", "AN", "ERROR"])

    @app.get("/deliberately-broken")
    def broken(request: Request) -> None:
        raise RuntimeError(f"failed while using {secret}")

    with TestClient(app, raise_server_exceptions=False) as broken_client:
        response = broken_client.get("/deliberately-broken")

    assert secret not in response.text
    assert "LEAKED" not in response.text
    assert "Traceback" not in response.text


# ── The rest of the configuration ─────────────────────────────────────────
#
# Most of what the product can do was previously reachable only by hand-editing
# TOML — including where output goes and the cap on what a service can charge.


CONFIGURABLE_FIELDS = [
    "output_root",
    "port",
    "model",  # transcription
    "language",
    "silence_threshold_seconds",
    "backend",
    "hard_limit_usd",  # the spending cap
    "max_runtime_minutes",  # local guard
    "max_frames_per_run",
    "default_token_limit",  # collection defaults
    "default_reserve_tokens",
    "allow_video_split",
    "preset",  # sampling default
    "custom_interval_seconds",
    "concurrency",
    "poll_interval_seconds",
    "max_retries",
    "backoff_base_seconds",
]


@pytest.mark.parametrize("field", CONFIGURABLE_FIELDS)
def test_every_setting_is_reachable_without_editing_a_file(client, field):
    """A setting only the TOML can reach is a setting most users do not have."""
    assert f'name="{field}"' in client.get("/settings").text


def test_saving_one_section_leaves_the_others_alone(client, settings_path):
    """Each form carries only its own fields, and each route replaces only what
    it was given. A route that rebuilt the whole config from its own form
    defaults would silently reset every setting the user could not see."""
    client.post(
        "/settings/transcription",
        data={
            "backend": "auto",
            "model": "large-v3",
            "language": "en",
            "silence_threshold_seconds": "5",
        },
    )
    client.post(
        "/settings/collections",
        data={"default_token_limit": "500000", "default_reserve_tokens": "50000"},
    )
    client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
            "hard_limit_usd": "10",
            "max_runtime_minutes": "0",
            "max_frames_per_run": "0",
        },
    )

    written = settings_path.read_text(encoding="utf-8")
    assert 'model = "large-v3"' in written, "the describing form reset the speech model"
    assert "default_token_limit = 500000" in written, (
        "the describing form reset the collection defaults"
    )
    assert 'model_id = "qwen2.5vl:7b"' in written


def test_the_spending_cap_can_be_set_from_the_interface(client, settings_path):
    """It is the one number that enforces the budget, and it was the hardest one
    to reach."""
    client.post(
        "/settings",
        data={
            "enabled": "1",
            "provider": "ollama_local",
            "model_id": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434",
            "batch_size": "1",
            "hard_limit_usd": "7.5",
            "max_runtime_minutes": "0",
            "max_frames_per_run": "0",
        },
    )
    assert "hard_limit_usd = 7.5" in settings_path.read_text(encoding="utf-8")


def test_a_setting_nothing_reads_is_not_offered_as_a_choice(client, settings_path):
    """`on_limit` is stored but no code path consults it. A control that changes
    no behaviour is the same lie as placeholder data, so it is carried through
    rather than presented — and carrying it through means saving must not
    quietly rewrite it either."""
    assert 'name="on_limit"' not in client.get("/settings").text

    client.post(
        "/settings",
        data={
            "enabled": "",
            "provider": "none",
            "model_id": "",
            "batch_size": "1",
            "endpoint": "http://127.0.0.1:11434",
            "hard_limit_usd": "25",
            "max_runtime_minutes": "0",
            "max_frames_per_run": "0",
        },
    )
    assert 'on_limit = "stop_and_ask"' in settings_path.read_text(encoding="utf-8")


def test_the_collection_reserve_cannot_swallow_the_whole_budget(client, settings_path):
    response = client.post(
        "/settings/collections",
        data={"default_token_limit": "10000", "default_reserve_tokens": "10000"},
    )
    assert "nothing for the content" in response.text


# ── The output folder ─────────────────────────────────────────────────────


def test_the_output_folder_can_be_repointed(client, settings_path, tmp_path):
    elsewhere = tmp_path / "somewhere-else"

    client.post("/settings/storage", data={"output_root": str(elsewhere), "port": "8712"})

    assert f'output_root = "{elsewhere}"' in settings_path.read_text(encoding="utf-8")


def test_repointing_the_output_folder_moves_nothing(client, settings, tmp_path):
    """Relocating a tree that can run to tens of gigabytes, with a live database
    inside it, is a different feature. Half-doing it silently is the worst
    option available."""
    original = settings.output_root
    (original / "j1").mkdir(parents=True, exist_ok=True)
    (original / "j1" / "assembled.txt").write_text("real work", "utf-8")

    client.post(
        "/settings/storage",
        data={"output_root": str(tmp_path / "elsewhere"), "port": "8712"},
    )

    assert (original / "j1" / "assembled.txt").read_text("utf-8") == "real work"


def test_the_new_output_folder_is_usable_straight_away(client, settings_path, tmp_path):
    """Without a database there, every screen reads as a fresh install — which
    looks exactly like the change having thrown the user's work away."""
    from app.core.db import database_path

    elsewhere = tmp_path / "fresh"
    client.post("/settings/storage", data={"output_root": str(elsewhere), "port": "8712"})

    assert database_path(elsewhere).exists()


def test_the_output_folder_is_not_repointed_while_a_job_runs(client, settings, tmp_path):
    """The worker holds a claim on the current root and the database lives
    inside it. Moving the target mid-job leaves the worker writing to one place
    and the interface reading another."""
    from app.core.db import open_database, utc_now

    connection = open_database(settings.output_root)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Long job", "transcribing", str(settings.output_root), utc_now(), utc_now()),
    )
    connection.commit()
    connection.close()

    response = client.post(
        "/settings/storage",
        data={"output_root": str(tmp_path / "elsewhere"), "port": "8712"},
    )

    # The refusal names the job that is in the way, and nothing was created at
    # the new location.
    assert "Long job" in response.text
    assert not (tmp_path / "elsewhere").exists()


def test_a_port_change_says_it_needs_a_restart(client, settings_path):
    """`server.port` cannot take effect without one, and a screen that implied
    otherwise would send the user to an address nothing is listening on."""
    client.post("/settings/storage", data={"output_root": "", "port": "9001"})

    body = client.get("/settings").text
    assert "9001" in body
    assert "started again" in body or "next time you start" in body


# ── Notifications ─────────────────────────────────────────────────────────


def test_notification_preferences_are_saved(client, settings_path):
    client.post("/settings/notifications", data={"browser": "1", "terminal_bell": "1"})

    written = settings_path.read_text(encoding="utf-8")
    assert "[notifications]" in written
    assert "browser = true" in written
    assert "terminal_bell = true" in written


def test_notifications_default_to_the_ones_that_need_no_permission(tmp_path):
    """Browser notifications off, terminal bell on. The prompt is the cost, and
    nobody should pay it before they have seen the product do anything."""
    settings = Settings()
    assert settings.notifications.browser is False
    assert settings.notifications.terminal_bell is True


def test_saving_notifications_leaves_the_rest_of_the_configuration_alone(client, settings_path):
    client.post(
        "/settings/transcription",
        data={
            "backend": "auto",
            "model": "large-v3",
            "language": "en",
            "silence_threshold_seconds": "3",
        },
    )
    client.post("/settings/notifications", data={"browser": "1"})

    assert 'model = "large-v3"' in settings_path.read_text(encoding="utf-8")


def test_no_notification_setting_can_reach_off_the_machine(client):
    """The specification excludes OS notification registration, launchd,
    systemd, telemetry, and any outbound call. This is the feature most likely
    to grow one by accident."""
    body = client.get("/settings").text

    assert "no push service" in body
    for outbound in ("serviceWorker", "pushManager", "mailto:", "webhook"):
        assert outbound not in body
