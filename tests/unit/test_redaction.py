"""Redaction is the single control standing between a live credential and a log
file, a manifest, an export, or an error shown to the user. These tests are the
reason it can be trusted, so they are deliberately unforgiving.

Every literal below is a synthetic string with the right *shape* and no value.
None of them is, or ever was, a real credential.
"""

from __future__ import annotations

import io
import logging

import pytest

from app.core.redaction import (
    MASK,
    RedactingFilter,
    clear_registered_secrets,
    install_redaction,
    redact,
    redact_structure,
    redacted_exception_text,
    register_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# ── Registered literal values ─────────────────────────────────────────────


def test_registered_value_is_masked_anywhere_in_the_text():
    register_secret("unremarkable-looking-value-9f2a")
    out = redact("connecting with unremarkable-looking-value-9f2a now")
    assert "unremarkable-looking-value-9f2a" not in out
    assert MASK in out


def test_registered_value_is_masked_even_without_word_boundaries():
    register_secret("abcdefghijkl")
    assert "abcdefghijkl" not in redact("xxabcdefghijklyy")


def test_short_values_are_not_registered():
    # Masking a 3-character string would blank out unrelated text everywhere.
    register_secret("abc")
    assert redact("abc def") == "abc def"


def test_empty_and_none_registrations_are_ignored():
    register_secret(None)
    register_secret("")
    register_secret("   ")
    assert redact("nothing to hide here") == "nothing to hide here"


def test_longest_registered_value_wins_when_one_contains_another():
    register_secret("aaaaaaaaaaaa")
    register_secret("aaaaaaaaaaaaBBBBBBBB")
    out = redact("key=aaaaaaaaaaaaBBBBBBBB end")
    assert out.count(MASK) == 1
    assert "BBBBBBBB" not in out


def test_clearing_the_registry_stops_masking():
    register_secret("some-registered-value-1234")
    clear_registered_secrets()
    assert "some-registered-value-1234" in redact("v=some-registered-value-1234")


# ── Shape patterns: unregistered keys still never escape ──────────────────


@pytest.mark.parametrize(
    "sample",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "GOCSPX-AAAAAAAAAAAAAAAAAAAA",
        "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "AKIAAAAAAAAAAAAAAAAA",
        "hf_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "xoxb-AAAAAAAAAAAA-AAAAAAAAAAAA",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.AAAAAAAAAAAAAAAA",
    ],
)
def test_known_key_shapes_are_masked_without_registration(sample):
    out = redact(f"request failed using {sample} against the endpoint")
    assert sample not in out
    assert MASK in out
    # The rest of the line survives, so the log stays diagnosable.
    assert "request failed using" in out
    assert "against the endpoint" in out


def test_pem_private_key_block_is_masked_whole():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact(f"loaded:\n{pem}\ndone")
    assert "AAAAAAAA" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert out.startswith("loaded:")
    assert out.endswith("done")


def test_credentials_in_a_url_are_masked_but_the_host_survives():
    out = redact("connecting to https://someuser:s3cr3t-p4ssw0rd@example.invalid/v1")
    assert "s3cr3t-p4ssw0rd" not in out
    assert "someuser" in out
    assert "example.invalid" in out


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: Bearer AAAAAAAAAAAAAAAAAAAAAAAA",
        "authorization=AAAAAAAAAAAAAAAAAAAAAAAA",
        "x-api-key: AAAAAAAAAAAAAAAAAAAAAAAA",
        "X-Goog-Api-Key: AAAAAAAAAAAAAAAAAAAAAAAA",
        "api_key=AAAAAAAAAAAAAAAAAAAAAAAA",
        "password: AAAAAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_credential_bearing_headers_and_pairs_are_masked(line):
    out = redact(line)
    assert "AAAAAAAAAAAAAAAAAAAAAAAA" not in out
    assert MASK in out


def test_non_secret_text_is_left_completely_alone():
    line = "extracted 1,265 frames at 2000 ms from capture_0914.mp4 in 41.2 s"
    assert redact(line) == line


def test_non_string_values_pass_through_unchanged():
    for value in (None, 42, 3.5, True, b"bytes"):
        assert redact(value) is value


# ── Structures ────────────────────────────────────────────────────────────


def test_sensitive_mapping_keys_are_masked_whatever_the_value_looks_like():
    payload = {
        "model": "qwen2.5vl:7b",
        "api_key": "totally-unrecognisable-format",
        "nested": {"Authorization": "anything at all", "batch_size": 1},
    }
    out = redact_structure(payload)
    assert out["model"] == "qwen2.5vl:7b"
    assert out["api_key"] == MASK
    assert out["nested"]["Authorization"] == MASK
    assert out["nested"]["batch_size"] == 1


def test_none_valued_sensitive_key_stays_none():
    # Distinguishing "no key configured" from "key withheld" matters in diagnostics.
    assert redact_structure({"api_key": None})["api_key"] is None


def test_secrets_inside_lists_and_tuples_are_masked():
    register_secret("registered-secret-value")
    out = redact_structure(["registered-secret-value", ("nested", "registered-secret-value")])
    assert "registered-secret-value" not in str(out)


def test_structure_types_are_preserved():
    out = redact_structure({"a": [1, 2], "b": ("x", "y"), "c": {"d": 1}})
    assert isinstance(out["a"], list)
    assert isinstance(out["b"], tuple)
    assert isinstance(out["c"], dict)


def test_deeply_nested_structure_terminates():
    deep: dict = {}
    node = deep
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    redact_structure(deep)  # must not recurse without bound


# ── Logging integration ───────────────────────────────────────────────────


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = install_redaction(logging.StreamHandler(stream))
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_logging_masks_registered_value_and_known_shapes():
    logger, stream = _capture_logger("test.redaction.basic")
    register_secret("registered-key-value-abcdef")
    logger.info("calling with %s", "registered-key-value-abcdef")

    written = stream.getvalue()
    assert "registered-key-value-abcdef" not in written
    assert MASK in written


def test_secret_created_only_by_interpolation_is_still_masked():
    # Neither the format string nor the argument is a secret on its own. Only
    # the interpolated result is — which is why redaction happens at format time.
    logger, stream = _capture_logger("test.redaction.interpolation")
    logger.info("authenticating with sk-ant-api03-%s", "A" * 40)

    written = stream.getvalue()
    assert "sk-ant-api03-A" not in written
    assert MASK in written


def test_logging_masks_exception_message_and_traceback():
    logger, stream = _capture_logger("test.redaction.traceback")
    register_secret("registered-key-value-abcdef")
    try:
        raise RuntimeError("failed for registered-key-value-abcdef")
    except RuntimeError:
        logger.exception("provider call failed")

    written = stream.getvalue()
    assert "registered-key-value-abcdef" not in written
    # The traceback itself must still be there — redaction removes the secret,
    # not the diagnostic.
    assert "RuntimeError" in written
    assert "Traceback" in written


def test_install_redaction_preserves_an_existing_format():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
    install_redaction(handler)

    logger = logging.getLogger("test.redaction.format")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("plain message")

    assert stream.getvalue().strip() == "INFO|plain message"


def test_filter_alone_still_masks_what_it_can():
    # Defence in depth: a handler someone else configured, without our formatter.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test.redaction.filteronly")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    register_secret("registered-key-value-abcdef")
    logger.info("value %s", "registered-key-value-abcdef")
    assert "registered-key-value-abcdef" not in stream.getvalue()


def test_exception_text_helper_masks_the_message():
    register_secret("registered-key-value-abcdef")
    exc = ValueError("bad key registered-key-value-abcdef supplied")
    out = redacted_exception_text(exc)
    assert "registered-key-value-abcdef" not in out
    assert "ValueError" in out
