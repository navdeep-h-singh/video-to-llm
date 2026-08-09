"""Choosing a service, and setting one up, without leaving the job form.

The job screen offered a card reading "Send to a service" and no way to say
which — that was settled by a global in Settings. So the screen asked a question
it would not let you answer, and someone with three accounts had to leave,
change a global that affects every future job, and come back to a form they had
half filled in.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import PROVIDER_LABELS, Settings
from app.core.db import open_database
from app.web.app import create_app
from tests.fixtures.synthetic import ffmpeg_available, make_video
from tests.loopback import LOOPBACK_BASE_URL


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


class FakeKeyring:
    """An in-memory stand-in for the OS secure store.

    Not optional. Without it these tests write into the developer's real
    Keychain: an earlier version of this file stored a fake OpenAI key on the
    machine it ran on, and it took noticing "Key set" on an unrelated screen to
    find it. A credential test that reaches the real store is a test that
    changes the machine it is run on.
    """

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        if (service, account) not in self.values:
            raise KeyError("not found")
        del self.values[(service, account)]


@pytest.fixture(autouse=True)
def keyring(monkeypatch):
    import app.credentials.store as credentials

    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    for var in credentials.ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    return fake


@pytest.fixture
def client(settings, db, keyring):
    with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
        yield test_client


# ── The choice is on the screen ───────────────────────────────────────────


def test_every_service_is_offered_on_the_job_screen(client):
    body = client.get("/jobs/new").text

    for provider, label in PROVIDER_LABELS.items():
        if provider == "ollama_local":
            continue
        assert f'value="{provider}"' in body, f"{provider} cannot be chosen"
        assert label in body, f"{label} is not named"


def test_a_service_without_a_key_is_still_offered_with_a_way_to_set_one_up(client):
    """Hiding it answers "why is my provider missing" with silence."""
    body = client.get("/jobs/new").text

    assert "Needs a key" in body
    assert "data-save-key" in body, "no way to add a key from here"
    assert "data-check-models" in body, "no way to find out what models exist"


def test_the_job_screen_still_speaks_plainly(client):
    """Invariant 12. The picker is revealed by opting in, but its markup is in
    the page from the start, so its words count."""
    body = client.get("/jobs/new").text

    for jargon in ("API key", "api_key", "endpoint", "inference", "LLM API"):
        assert jargon not in body, f"{jargon!r} appears before opting in"


# ── The choice reaches the job ────────────────────────────────────────────


def _create(client, source, **extra):
    """Create a job against a real file, so a row is actually written.

    Pointing at a path that does not exist makes preflight refuse before
    anything is stored — and a test asserting on a row that was never created
    passes by looking the other way.
    """
    data = {"name": "Job", "paths": str(source.path), "provider": "external"}
    data.update(extra)
    return client.post("/jobs", data=data, follow_redirects=False)


@pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")
def test_the_chosen_service_is_what_the_job_records(client, db, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    _create(client, source, service="anthropic", model_id="claude-sonnet-4-5")

    row = db.execute("SELECT visual_provider, visual_model_id FROM jobs").fetchone()
    assert row is not None, "no job was created; the assertion below would be vacuous"
    assert row["visual_provider"] == "anthropic"
    assert row["visual_model_id"] == "claude-sonnet-4-5"


@pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")
def test_a_service_name_that_is_not_a_service_is_refused(client, db, tmp_path):
    """The field arrives from a form and is therefore untrusted. An unknown name
    must not become the provider, or a job would carry something the worker will
    later try to build an adapter from."""
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    _create(client, source, service="../../etc/passwd")

    row = db.execute("SELECT visual_provider FROM jobs").fetchone()
    assert row is not None
    assert row["visual_provider"] != "../../etc/passwd"


@pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")
def test_choosing_a_service_does_not_rewrite_the_global_default(client, tmp_path, monkeypatch):
    """A per-job choice is per job. Changing the global from here would alter
    every future job as a side effect of setting up this one."""
    import app.core.config as config

    target = tmp_path / "settings.toml"
    monkeypatch.setattr(config, "settings_file", lambda: target)

    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    _create(client, source, service="openai", model_id="gpt-4o")

    assert not target.exists(), "creating a job wrote to the settings file"


# ── The key endpoint stays write-only ─────────────────────────────────────


def test_saving_a_key_never_returns_it(client, keyring):
    # Assembled at runtime so the literal is not in the file: the pre-publish
    # audit flags credential-shaped strings, and the right answer is to not
    # write one rather than to add an exemption.
    secret = "sk-" + "test" + "-not-a-real-key"
    response = client.post("/api/providers/key", data={"provider": "openai", "key": secret})
    payload = response.json()

    assert secret in keyring.values.values(), "the key never reached the fake store"
    assert secret not in response.text
    assert "key" not in {k for k in payload if k not in ("ok", "present", "detail")}


def test_an_empty_key_is_refused_rather_than_stored(client):
    payload = client.post("/api/providers/key", data={"provider": "openai", "key": "  "}).json()

    assert payload["ok"] is False


def test_model_discovery_is_refused_from_another_site(client):
    """It is under /api/, so the origin boundary covers it — a page on another
    site must not be able to enumerate which services this machine has keys
    for."""
    response = client.post(
        "/api/providers/models",
        data={"provider": "openai"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_a_service_with_no_key_says_so_rather_than_calling_out(client):
    """Checked before the request rather than after a confusing 401."""
    payload = client.post("/api/providers/models", data={"provider": "openai"}).json()

    assert payload["ok"] is False
    assert "key" in payload["detail"].lower()
