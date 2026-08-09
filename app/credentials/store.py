"""Credential storage.

Two places a key may live, in order of preference:

1. the operating system's secure store — macOS Keychain, Windows Credential
   Manager, or a Linux Secret Service keyring;
2. a process-scoped environment variable, only where no secure store exists.

There is no third place. **A plaintext on-disk fallback is never created**, not
as a convenience and not temporarily. If neither is available the external
providers simply stay unavailable, and local-only processing — the default —
carries on working. That is a better outcome than a key sitting in a file
someone later commits.

Every value read here is registered with the redaction module immediately, so
that even a key we do not recognise the shape of is masked everywhere
afterwards. See ``docs/SECURITY.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text, register_secret

logger = get_logger(__name__)

KEYRING_SERVICE = "video-to-llm"

#: Environment variable per provider, used only as the no-secure-store fallback.
ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "anthropic_compatible": "ANTHROPIC_COMPATIBLE_API_KEY",
}

#: Local Ollama has no credential of any kind. Not an empty one — none.
NO_CREDENTIAL_PROVIDERS = frozenset({"ollama_local"})


class CredentialSource(StrEnum):
    KEYRING = "keyring"
    ENVIRONMENT = "environment"
    NONE = "none"


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialStatus:
    """What the interface is allowed to know about a stored key.

    Note what is absent: the value, and any prefix or suffix of it. A stored key
    is never displayed back, not even partially masked — a few revealed
    characters still narrow a search, and there is no situation where the user
    needs them.
    """

    provider: str
    present: bool
    source: CredentialSource
    detail: str

    @property
    def requires_credential(self) -> bool:
        return self.provider not in NO_CREDENTIAL_PROVIDERS


def _keyring():
    try:
        import keyring

        return keyring
    except ImportError:  # pragma: no cover - keyring is a hard dependency
        return None


def secure_store_available() -> bool:
    """True when the OS secure store can actually be used.

    Probed rather than assumed: `keyring` imports fine on a headless Linux box
    and then fails at the first call, and the difference decides whether we are
    allowed to fall back to the environment.
    """
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception as error:
        logger.debug("Secure store unavailable: %s", redacted_exception_text(error))
        return False

    name = type(backend).__name__
    # The fail/null backends are what keyring installs when nothing real is
    # present. Writing to them appears to succeed and stores nothing.
    return "Fail" not in name and "Null" not in name


def get_credential(provider: str) -> str | None:
    """Return the key for *provider*, or None.

    Never raises for a missing key: absence is an ordinary, supported state.
    """
    if provider in NO_CREDENTIAL_PROVIDERS:
        return None

    keyring = _keyring()
    if keyring is not None and secure_store_available():
        try:
            value = keyring.get_password(KEYRING_SERVICE, provider)
        except Exception as error:
            logger.warning(
                "Could not read from the secure store: %s", redacted_exception_text(error)
            )
        else:
            if value:
                register_secret(value)
                return value

    env_var = ENV_VARS.get(provider)
    if env_var:
        value = os.environ.get(env_var)
        if value:
            register_secret(value)
            return value

    return None


def set_credential(provider: str, value: str) -> CredentialSource:
    """Store a key in the secure store. Refuses when there is nowhere safe.

    Deliberately has no fallback path. Being told "there is nowhere safe to keep
    this on this machine" is a better outcome than a key written to a file.
    """
    if provider in NO_CREDENTIAL_PROVIDERS:
        raise CredentialError(
            f"{provider} runs on this computer and does not use an API key. "
            "There is nothing to store."
        )

    if not value or not value.strip():
        raise CredentialError("An empty key cannot be stored.")

    keyring = _keyring()
    if keyring is None or not secure_store_available():
        raise CredentialError(
            "This computer has no secure place to keep an API key "
            "(no Keychain, Credential Manager, or Secret Service keyring). "
            f"Set the {ENV_VARS.get(provider, 'provider')} environment variable "
            "for this session instead. A key is never written to a file."
        )

    try:
        keyring.set_password(KEYRING_SERVICE, provider, value.strip())
    except Exception as error:
        raise CredentialError(
            f"Could not save the key: {redacted_exception_text(error)}"
        ) from error

    register_secret(value.strip())
    logger.info("Stored a credential for %s in the secure store", provider)
    return CredentialSource.KEYRING


def delete_credential(provider: str) -> bool:
    """Remove a stored key. True when one was removed."""
    keyring = _keyring()
    if keyring is None or not secure_store_available():
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, provider)
    except Exception:
        return False
    logger.info("Removed the stored credential for %s", provider)
    return True


def credential_status(provider: str) -> CredentialStatus:
    """Describe whether a key is available, without revealing it."""
    if provider in NO_CREDENTIAL_PROVIDERS:
        return CredentialStatus(
            provider=provider,
            present=True,
            source=CredentialSource.NONE,
            detail="Runs on this computer. No key is needed and none is stored.",
        )

    keyring = _keyring()
    if keyring is not None and secure_store_available():
        try:
            if keyring.get_password(KEYRING_SERVICE, provider):
                return CredentialStatus(
                    provider,
                    True,
                    CredentialSource.KEYRING,
                    "Kept in this computer's secure store.",
                )
        except Exception as error:
            logger.debug("Secure store read failed: %s", redacted_exception_text(error))

    env_var = ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return CredentialStatus(
            provider,
            True,
            CredentialSource.ENVIRONMENT,
            f"Read from the {env_var} environment variable for this session only.",
        )

    if not secure_store_available():
        return CredentialStatus(
            provider,
            False,
            CredentialSource.NONE,
            "No key set, and this computer has no secure store. "
            f"Set {env_var} in the environment to use this provider.",
        )

    return CredentialStatus(provider, False, CredentialSource.NONE, "No key set.")


def require_credential(provider: str) -> str:
    """Return the key, or explain clearly why the provider cannot be used."""
    value = get_credential(provider)
    if value:
        return value

    env_var = ENV_VARS.get(provider, "the provider's environment variable")
    raise CredentialError(
        f"No API key is set for {provider}. Add one in Settings, or set "
        f"{env_var} in the environment. Processing on this computer needs no key "
        "and continues to work."
    )
