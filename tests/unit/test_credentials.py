"""Credential storage.

The properties under test are all negative ones: a key is never written to a
file, never returned to the interface, and never survives in a log. Those are
easy to break by accident and expensive to discover late.

No test touches a real keyring — the backend is faked, so this suite passes in
CI on a machine with no GUI credential store (spec §11).
"""

from __future__ import annotations

import io
import logging

import pytest

from app.core.logging import install_redaction
from app.core.redaction import MASK, clear_registered_secrets
from app.credentials import store as credentials
from app.credentials.store import (
    ENV_VARS,
    NO_CREDENTIAL_PROVIDERS,
    CredentialError,
    CredentialSource,
    credential_status,
    delete_credential,
    get_credential,
    require_credential,
    secure_store_available,
    set_credential,
)


class FakeKeyring:
    """An in-memory stand-in for the OS secure store."""

    def __init__(self, *, working: bool = True):
        self.working = working
        self.values: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self if self.working else FailBackend()

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        if (service, account) not in self.values:
            raise KeyError("not found")
        del self.values[(service, account)]


class FailBackend:
    """What keyring installs when no real store exists."""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    clear_registered_secrets()
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    yield
    clear_registered_secrets()


@pytest.fixture
def keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    return fake


@pytest.fixture
def no_secure_store(monkeypatch):
    monkeypatch.setattr(credentials, "_keyring", lambda: None)


# ── Secure store detection ────────────────────────────────────────────────


def test_a_working_backend_is_detected(keyring):
    assert secure_store_available() is True


def test_a_fail_backend_is_not_treated_as_available(monkeypatch):
    # keyring imports fine on headless Linux and then silently stores nothing.
    # Trusting the import would mean writing keys into a void.
    monkeypatch.setattr(credentials, "_keyring", lambda: FakeKeyring(working=False))
    assert secure_store_available() is False


def test_a_missing_keyring_module_is_not_available(no_secure_store):
    assert secure_store_available() is False


def test_a_backend_that_raises_is_not_available(monkeypatch):
    class Exploding:
        def get_keyring(self):
            raise RuntimeError("no dbus session")

    monkeypatch.setattr(credentials, "_keyring", lambda: Exploding())
    assert secure_store_available() is False


# ── Storing and reading ───────────────────────────────────────────────────


def test_a_key_round_trips_through_the_secure_store(keyring):
    assert set_credential("anthropic", "test-key-abcdef123456") == CredentialSource.KEYRING
    assert get_credential("anthropic") == "test-key-abcdef123456"


def test_a_missing_key_returns_none_rather_than_raising(keyring):
    # Absence is an ordinary, supported state — local-only processing needs none.
    assert get_credential("anthropic") is None


def test_the_environment_is_used_when_the_store_has_nothing(keyring, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment-123456")
    assert get_credential("anthropic") == "from-the-environment-123456"


def test_the_secure_store_wins_over_the_environment(keyring, monkeypatch):
    keyring.set_password("video-to-llm", "anthropic", "from-the-store-123456")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment-123456")
    assert get_credential("anthropic") == "from-the-store-123456"


def test_an_empty_key_is_refused(keyring):
    with pytest.raises(CredentialError, match="empty key"):
        set_credential("anthropic", "   ")


def test_a_key_is_stripped_before_storing(keyring):
    set_credential("anthropic", "  padded-key-value-123456  ")
    assert get_credential("anthropic") == "padded-key-value-123456"


def test_deleting_removes_the_key(keyring):
    set_credential("anthropic", "test-key-abcdef123456")
    assert delete_credential("anthropic") is True
    assert get_credential("anthropic") is None


def test_deleting_a_missing_key_is_harmless(keyring):
    assert delete_credential("anthropic") is False


# ── No plaintext fallback, ever ───────────────────────────────────────────


def test_storing_is_refused_when_there_is_nowhere_safe(no_secure_store):
    # Being told there is nowhere safe is a better outcome than a key in a file
    # that someone later commits.
    with pytest.raises(CredentialError) as excinfo:
        set_credential("anthropic", "test-key-abcdef123456")

    message = str(excinfo.value)
    assert "no secure place" in message
    assert "never written to a file" in message
    assert "ANTHROPIC_API_KEY" in message, "the message should say what to do instead"


def test_a_refused_store_writes_nothing_to_disk(no_secure_store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CredentialError):
        set_credential("anthropic", "test-key-abcdef123456")

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == [], f"files were created: {written}"


def test_the_environment_still_works_with_no_secure_store(no_secure_store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment-123456")
    assert get_credential("anthropic") == "from-the-environment-123456"


# ── Never revealing a stored key ──────────────────────────────────────────


def test_status_reports_presence_without_the_value(keyring):
    set_credential("anthropic", "test-key-abcdef123456")
    status = credential_status("anthropic")

    assert status.present is True
    # Not even a prefix or suffix: a few revealed characters still narrow a
    # search, and there is no situation where the user needs them.
    assert "test-key-abcdef123456" not in status.detail
    assert "test-key" not in status.detail
    assert "123456" not in status.detail


def test_status_says_where_a_key_came_from(keyring, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-environment-123456")
    status = credential_status("anthropic")
    assert status.source == CredentialSource.ENVIRONMENT
    assert "this session only" in status.detail


def test_status_explains_the_missing_store_case(no_secure_store):
    status = credential_status("anthropic")
    assert status.present is False
    assert "no secure store" in status.detail
    assert "ANTHROPIC_API_KEY" in status.detail


def test_a_read_key_is_registered_for_redaction(keyring):
    set_credential("anthropic", "unmistakable-key-value-987654")
    clear_registered_secrets()
    get_credential("anthropic")

    stream = io.StringIO()
    logger = logging.getLogger("test.credentials.redaction")
    logger.handlers = [install_redaction(logging.StreamHandler(stream))]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("calling with unmistakable-key-value-987654")

    assert "unmistakable-key-value-987654" not in stream.getvalue()
    assert MASK in stream.getvalue()


def test_an_environment_key_is_also_registered_for_redaction(keyring, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-value-13579246")
    get_credential("anthropic")

    stream = io.StringIO()
    logger = logging.getLogger("test.credentials.env.redaction")
    logger.handlers = [install_redaction(logging.StreamHandler(stream))]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("value is env-key-value-13579246")

    assert "env-key-value-13579246" not in stream.getvalue()


# ── Providers that need no credential ─────────────────────────────────────


def test_ollama_needs_no_credential():
    assert "ollama_local" in NO_CREDENTIAL_PROVIDERS
    assert credential_status("ollama_local").requires_credential is False


def test_ollama_has_no_environment_variable():
    # There is no key for this provider, so there is nothing to read from.
    assert "ollama_local" not in ENV_VARS


# ── Requiring a credential ────────────────────────────────────────────────


def test_require_returns_the_key_when_present(keyring):
    set_credential("anthropic", "test-key-abcdef123456")
    assert require_credential("anthropic") == "test-key-abcdef123456"


def test_require_explains_clearly_when_absent(keyring):
    with pytest.raises(CredentialError) as excinfo:
        require_credential("anthropic")

    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    # The user must be told local processing still works, not just that
    # something failed.
    assert "no key" in message.lower()


@pytest.mark.parametrize("provider", ["anthropic", "google", "openai", "openai_compatible"])
def test_every_cloud_provider_has_an_environment_variable(provider):
    assert provider in ENV_VARS
