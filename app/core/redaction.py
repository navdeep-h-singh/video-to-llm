"""Central secret redaction.

Every path that can emit text a human or a file might see — logging, error
messages, manifests, provenance, exports, API responses — goes through this
module. It is deliberately the only place redaction rules live, so the rules can
be tested once and trusted everywhere.

Two mechanisms work together:

*Registered values* — when a credential is read out of the secure store or the
environment, its literal value is registered here. Any later text containing it
is redacted by exact match. This catches keys whose shape we do not recognise.

*Shape patterns* — well-known credential formats and credential-bearing headers
are redacted even if the value was never registered, so a key pasted into the
wrong field, or one belonging to a provider we do not adapt, still never reaches
a log line.

Redaction is one-way. There is no unredact.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

MASK = "[redacted]"

# A registered value shorter than this is not masked. Short strings produce
# catastrophic false positives (a two-character "key" would blank out half of
# every message) and are not plausible credentials.
_MIN_REGISTERED_LENGTH = 8


class _SecretRegistry:
    """Thread-safe set of literal secret values seen this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if not value:
            return
        stripped = value.strip()
        if len(stripped) < _MIN_REGISTERED_LENGTH:
            return
        with self._lock:
            self._values.add(stripped)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def snapshot(self) -> list[str]:
        with self._lock:
            # Longest first, so a key that contains another registered value as a
            # substring is masked whole rather than in pieces.
            return sorted(self._values, key=len, reverse=True)


_registry = _SecretRegistry()


def register_secret(value: str | None) -> None:
    """Record a literal credential value so it is masked wherever it appears."""
    _registry.register(value)


def clear_registered_secrets() -> None:
    """Forget every registered value. Used by tests and on credential rotation."""
    _registry.clear()


# ── Shape patterns ────────────────────────────────────────────────────────
#
# Ordered most specific first. Each pattern masks the credential itself while
# keeping enough surrounding text that a log line stays diagnosable.

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic: sk-ant-api03-… / sk-ant-…
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), MASK),
    # OpenAI and the many services that copied its prefix: sk-…, sk-proj-…
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}"), MASK),
    # Google API keys
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"), MASK),
    # Google OAuth client secrets
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{16,}"), MASK),
    # GitHub tokens
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), MASK),
    # AWS access key IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    # Hugging Face
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), MASK),
    # Slack
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), MASK),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), MASK),
    # PEM private key blocks, masked whole
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        MASK,
    ),
    # Credentials embedded in a URL: scheme://user:secret@host
    (re.compile(r"://([^:/@\s]+):([^@/\s]+)@"), r"://\1:" + MASK + "@"),
    # Authorization / api-key headers, in header dumps or curl lines
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization)\b(\s*[:=]\s*)"
            r"(?:bearer\s+|basic\s+|token\s+)?[^\s,;'\"]+"
        ),
        r"\1\2" + MASK,
    ),
    (
        re.compile(r"(?i)\b(x-api-key|x-goog-api-key|api[-_]?key|apikey)\b(\s*[:=]\s*)[^\s,;'\"]+"),
        r"\1\2" + MASK,
    ),
    # key=value forms in query strings and config dumps
    (
        re.compile(
            r"(?i)\b([a-z0-9_\-]*(?:api[-_]?key|secret|token|password|passwd|credential)"
            r"[a-z0-9_\-]*)(\s*[:=]\s*)(\"?)([^\s,;&'\"]{6,})(\"?)"
        ),
        r"\1\2\3" + MASK + r"\5",
    ),
)

# Mapping keys whose value is always masked, whatever it looks like.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[-_]?key|apikey|secret|token|password|passwd|credential|authorization|"
    r"auth|bearer|private[-_]?key|access[-_]?key|session[-_]?id|cookie)"
)


def redact(value: Any) -> Any:
    """Redact secrets from a string, leaving every other type untouched.

    Not recursive — use :func:`redact_structure` for containers.
    """
    if not isinstance(value, str) or not value:
        return value

    text = value
    for secret in _registry.snapshot():
        if secret in text:
            text = text.replace(secret, MASK)

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)

    return text


def redact_structure(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside mappings and sequences.

    Mapping entries whose *key* names a credential are masked regardless of what
    the value looks like, so an unrecognised key format still never escapes.
    """
    if _depth > 12:  # guards against pathological or cyclic structures
        return value

    if isinstance(value, str):
        return redact(value)

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                out[key] = MASK if item is not None else None
            else:
                out[key] = redact_structure(item, _depth + 1)
        return out

    if isinstance(value, (list, tuple, set)) and not isinstance(value, (str, bytes)):
        redacted = [redact_structure(item, _depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(redacted)
        if isinstance(value, set):
            return set(redacted)
        return redacted

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [redact_structure(item, _depth + 1) for item in value]

    return value


class RedactingFilter(logging.Filter):
    """Redacts a record's message and arguments before formatting.

    This is a first line of defence, not the guarantee. It cannot be the
    guarantee: a format string and its arguments are individually harmless in
    cases where their *interpolation* is not — ``"key=sk-ant-%s" % tail`` holds
    no secret in either half. It also runs before the traceback exists, so
    ``exc_info`` is still unformatted here.

    :class:`RedactingFormatter` closes both gaps. Use them together via
    :func:`install_redaction`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        elif record.msg is not None:
            record.msg = redact_structure(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_structure(record.args)
            else:
                record.args = tuple(redact_structure(arg) for arg in record.args)

        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the finished log line.

    This is where the guarantee actually lives. By the time ``format`` returns,
    the format string, its arguments, the exception message, and the full
    traceback have all been rendered into one string — so redacting that string
    covers every route a credential has into a log file, including the ones
    interpolation and traceback rendering create on the way.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_redaction(handler: logging.Handler, fmt: str | None = None) -> logging.Handler:
    """Fit a handler with both redaction layers. Returns the same handler."""
    handler.addFilter(RedactingFilter())
    existing = handler.formatter
    handler.setFormatter(
        RedactingFormatter(
            fmt or (existing._fmt if existing else None),
            datefmt=existing.datefmt if existing else None,
        )
    )
    return handler


def redacted_exception_text(exc: BaseException) -> str:
    """Render an exception for display with secrets removed.

    Provider SDKs routinely put the offending request — headers included — into
    the exception string. Never format a caught exception for a user, a log, or
    an artifact without going through this.
    """
    return redact(f"{type(exc).__name__}: {exc}")
