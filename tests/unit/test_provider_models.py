"""A model belongs to the service that offers it.

The defect this pins: ``visual_analysis.model_id`` was one string shared by every
provider, so the model was a property of the *application* rather than of the
service. Setting ``gemini-2.5-flash`` and then switching to Claude asked
Anthropic for a Gemini model. Google quietly defaulted and covered it up; the
others failed at request time with a message about an unrecognised model, which
is a confusing way to learn that a settings screen has no memory.

A failure here is a regression.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    CUSTOM_ENDPOINT_PROVIDERS,
    Settings,
    VisualAnalysisSettings,
    load_settings,
    render_settings_toml,
    save_settings,
)
from app.core.db import open_database
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def client(settings, tmp_path, monkeypatch):
    import app.core.config as config

    target = tmp_path / "settings.toml"
    monkeypatch.setattr(config, "settings_file", lambda: target)
    connection = open_database(settings.output_root)
    try:
        with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
            yield test_client
    finally:
        connection.close()


# ── The map itself ────────────────────────────────────────────────────────


def test_each_service_keeps_its_own_model():
    visual = VisualAnalysisSettings(
        provider="anthropic",
        models={"anthropic": "claude-sonnet-4-5", "google": "gemini-2.5-flash"},
    )

    assert visual.model_id == "claude-sonnet-4-5"
    assert visual.model_for("google") == "gemini-2.5-flash"


def test_switching_provider_switches_the_model_with_it():
    """The whole point. Selecting a different service must not carry the last
    service's model across to it."""
    from dataclasses import replace

    visual = VisualAnalysisSettings(
        provider="google",
        models={"anthropic": "claude-sonnet-4-5", "google": "gemini-2.5-flash"},
    )
    switched = replace(visual, provider="anthropic")

    assert switched.model_id == "claude-sonnet-4-5", (
        "switching to Claude carried the Gemini model across"
    )


def test_a_service_with_no_model_chosen_reports_nothing_rather_than_guessing():
    """Invariant 6: Unknown is preserved, never guessed. Borrowing another
    service's model would be a guess dressed as a setting."""
    visual = VisualAnalysisSettings(provider="openai", models={"google": "gemini-2.5-flash"})

    assert visual.model_id == ""


def test_an_address_is_only_meaningful_for_a_compatible_endpoint():
    """The named services live at fixed addresses. Letting a stored base URL
    apply to them would be a way to redirect Anthropic traffic elsewhere."""
    visual = VisualAnalysisSettings(
        provider="anthropic",
        base_urls={"anthropic": "https://not-anthropic.example"},
    )

    assert visual.base_url_for() == ""
    assert "anthropic_compatible" in CUSTOM_ENDPOINT_PROVIDERS


# ── Carried across from an older file ─────────────────────────────────────


def test_an_older_files_single_model_is_kept_for_its_own_provider(tmp_path):
    target = tmp_path / "settings.toml"
    target.write_text('[visual_analysis]\nprovider = "google"\nmodel_id = "gemini-2.5-flash"\n')

    loaded = load_settings(path=target)

    assert loaded.visual_analysis.models == {"google": "gemini-2.5-flash"}
    assert loaded.visual_analysis.model_for("anthropic") == "", (
        "the old shared model was applied to every provider, which is the bug"
    )


def test_the_map_survives_a_save_and_reload(tmp_path):
    target = tmp_path / "settings.toml"
    original = Settings(
        visual_analysis=VisualAnalysisSettings(
            provider="anthropic_compatible",
            models={"anthropic": "claude-sonnet-4-5", "anthropic_compatible": "internal-vision"},
            base_urls={"anthropic_compatible": "https://gateway.internal/api"},
        )
    )
    save_settings(original, path=target)
    reloaded = load_settings(path=target)

    assert reloaded.visual_analysis.models == original.visual_analysis.models
    assert reloaded.visual_analysis.base_urls == original.visual_analysis.base_urls


def test_an_empty_model_is_removed_rather_than_stored_blank(client, tmp_path):
    """Two spellings of absence is how a file starts disagreeing with a screen."""
    client.post(
        "/settings",
        data={"enabled": "1", "provider": "openai", "model_id": "gpt-4o", "acknowledged": "1"},
        follow_redirects=False,
    )
    client.post(
        "/settings",
        data={"enabled": "1", "provider": "openai", "model_id": "   ", "acknowledged": "1"},
        follow_redirects=False,
    )

    written = (tmp_path / "settings.toml").read_text()
    assert 'openai = ""' not in written


def test_saving_one_services_model_leaves_the_others_alone(client, tmp_path):
    for provider, model in (("openai", "gpt-4o"), ("anthropic", "claude-sonnet-4-5")):
        client.post(
            "/settings",
            data={"enabled": "1", "provider": provider, "model_id": model, "acknowledged": "1"},
            follow_redirects=False,
        )

    reloaded = load_settings(path=tmp_path / "settings.toml")
    assert reloaded.visual_analysis.models == {
        "openai": "gpt-4o",
        "anthropic": "claude-sonnet-4-5",
    }


def test_no_model_is_invented_in_the_rendered_file():
    """An empty table rather than commented placeholders: a key with a made-up
    value is the kind of thing someone uncomments without meaning to."""
    rendered = render_settings_toml(Settings())

    body = rendered[rendered.index("[visual_analysis.models]") :]
    body = body[: body.index("[visual_analysis.base_urls]")]
    assert not [line for line in body.splitlines()[1:] if line.strip() and "=" in line]


# ── Every service is identifiable on screen ───────────────────────────────


def test_each_service_is_named_on_the_settings_screen(client):
    """Five identical grey boxes each reading only "No key set." give no way to
    tell which pair of fields belongs to which service."""
    from app.core.config import PROVIDER_LABELS

    body = client.get("/settings").text

    for provider, label in PROVIDER_LABELS.items():
        if provider == "ollama_local":
            continue
        assert label in body, f"{provider} is not named on the settings screen"


def test_the_fallback_name_matches_the_shared_label_for_every_service():
    """Templates are re-read from disk per request; route code is not.

    So a server started before `label` existed serves this template against a
    context without it. That really happened: every service rendered as an
    unnamed box. The template therefore carries its own fallback — and a second
    copy of the names is a second place for them to drift, which was proved the
    moment "Google Gemini" became "Google" and both had to be edited.

    Rendering each provider with no label and comparing against PROVIDER_LABELS
    catches that drift without parsing the template.
    """
    from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

    from app.core.config import PROVIDER_LABELS
    from app.credentials.store import CredentialSource, CredentialStatus
    from app.web.app import TEMPLATE_DIR

    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(),
        undefined=ChainableUndefined,
    )
    template = environment.get_template("settings.html")

    for provider, label in PROVIDER_LABELS.items():
        if provider == "ollama_local":
            continue
        stale = [
            {
                "provider": provider,
                "status": CredentialStatus(
                    provider=provider,
                    present=False,
                    source=CredentialSource.NONE,
                    detail="No key set.",
                ),
                "model": "",
                "base_url": "",
                "needs_address": False,
            }
        ]
        rendered = template.render(credentials=stale, secure_store=True)

        # The exact heading, not a substring. "Google Gemini" contains "Google",
        # so a containment check passed against the very drift this exists to
        # catch — the same imprecision that let a doubled "about" through
        # earlier in this codebase.
        heading = re.search(r'<div class="key-name">\s*<strong>(.*?)</strong>', rendered, re.S)
        assert heading, f"{provider} rendered no heading at all"
        assert " ".join(heading.group(1).split()) == label, (
            f"{provider} falls back to {heading.group(1).strip()!r}, but the shared "
            f"label is {label!r} — the template's copy has drifted"
        )


def test_the_screen_says_to_save_the_key_before_checking(client):
    """The order is not guessable and the failure is misleading.

    Check reads the *stored* key, so a key typed but not saved gets a refusal
    that reads like a rejected key. The sequence is stated once at the top
    rather than explained inside each row's error.
    """
    body = client.get("/settings").text

    assert "Save a key first" in body
    assert "which models it offers" in body


def test_the_model_box_says_what_would_fill_it(client):
    """ "Not chosen yet" restates what an empty box already shows. The useful
    thing to say is the next step."""
    body = client.get("/settings").text

    assert "Save a key first, then press Check" in body
    assert "Not chosen yet" not in body


def test_the_job_screen_explains_the_same_sequence(client):
    body = client.get("/jobs/new").text

    assert "Save key" in body
    assert "which models it offers" in body
