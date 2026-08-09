"""The application should notice when it is older than its own files.

Templates are read from disk per request; route code is imported once. Editing
the application under a running server therefore produces new screens served by
old routes, and the symptoms never look like what they are:

* every service on the settings screen lost its name, because the template asked
  for a value the running route did not supply;
* a button reported "Not Found", because the route it called had been added
  after the server started.

Both cost real time to diagnose, and the diagnosis was the same sentence each
time. A program that can detect this should say so.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.build import RECHECK_INTERVAL_SECONDS, current_fingerprint, reset_cache
from app.core.config import Settings
from app.core.db import open_database
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL

BANNER = "This application was updated"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
        yield test_client


def test_an_unchanged_application_says_nothing(client):
    """A warning that appears when nothing is wrong is one people learn to
    ignore, which costs more than never having shown it."""
    assert BANNER not in client.get("/").text


def test_editing_python_under_a_running_server_is_reported(client, monkeypatch):
    """The whole point. The running process is now older than the code."""
    import app.core.build as build

    later = current_fingerprint() + 10_000
    reset_cache()
    monkeypatch.setattr(build, "source_fingerprint", lambda: later)

    assert BANNER in client.get("/").text


def test_the_banner_appears_on_every_screen(client, monkeypatch, db):
    """The symptom can surface anywhere, so the explanation has to be anywhere.
    It lives in the shared layout for exactly that reason."""
    import app.core.build as build

    later = current_fingerprint() + 10_000
    reset_cache()
    monkeypatch.setattr(build, "source_fingerprint", lambda: later)

    for screen in ("/", "/settings", "/jobs/new", "/collections", "/imports"):
        assert BANNER in client.get(screen).text, f"{screen} does not explain itself"


def test_a_template_edit_does_not_ask_for_a_restart(client, monkeypatch):
    """Templates and CSS are re-read per request — changing them is *supposed* to
    take effect immediately. Advising a restart for those would be advising a
    restart that changes nothing."""
    import app.web.app as web

    template = web.TEMPLATE_DIR / "dashboard.html"
    template.touch()
    reset_cache()

    assert BANNER not in client.get("/").text


def test_the_check_is_throttled(monkeypatch):
    """It walks the source tree. Cheap, but not cheap enough to repeat for every
    request on a page that is polling every two seconds."""
    import app.core.build as build

    calls = {"n": 0}

    def counting() -> float:
        calls["n"] += 1
        return 1.0

    monkeypatch.setattr(build, "source_fingerprint", counting)
    reset_cache()

    for _ in range(20):
        current_fingerprint(now=0.0)

    assert calls["n"] == 1, f"walked the tree {calls['n']} times for twenty checks"

    current_fingerprint(now=RECHECK_INTERVAL_SECONDS + 1)
    assert calls["n"] == 2, "the fingerprint was never refreshed"
