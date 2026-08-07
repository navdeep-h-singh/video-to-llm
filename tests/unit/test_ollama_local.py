"""The Local Ollama adapter.

Three properties are load-bearing and each is tested directly:

1. frames never leave this computer — non-loopback endpoints are refused;
2. there is no credential of any kind for this provider;
3. batches stay small, because cloud-sized batches exhaust a local model.

Every network call is mocked. The live variant lives in
`tests/integration/test_live_ollama.py` behind the `live_ollama` marker.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.credentials.store import (
    NO_CREDENTIAL_PROVIDERS,
    CredentialError,
    credential_status,
    get_credential,
    set_credential,
)
from app.providers.base import (
    AnalysisRequest,
    FrameRequest,
    PermanentProviderError,
    TransientProviderError,
)
from app.providers.ollama_local import (
    APPLE_SILICON_ADVISORY,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ENDPOINT,
    DISCLOSURE,
    MAX_ADVANCED_BATCH_SIZE,
    RELIABILITY_WARNING,
    SUGGESTED_MODEL,
    NonLoopbackEndpointError,
    OllamaLocalProvider,
    assert_loopback_endpoint,
    resolve_batch_size,
)


@pytest.fixture
def frame_image(tmp_path) -> Path:
    target = tmp_path / "000000.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg bytes")
    return target


def make_request(frame_image: Path, count: int = 1) -> AnalysisRequest:
    return AnalysisRequest(
        frames=tuple(
            FrameRequest(index=i, timestamp_seconds=float(i * 2), image_path=frame_image)
            for i in range(count)
        ),
        model_id=SUGGESTED_MODEL,
        prompt="describe this picture",
    )


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── Loopback enforcement ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
        "http://127.0.0.2:11434",
    ],
)
def test_loopback_endpoints_are_accepted(endpoint):
    assert assert_loopback_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.1.50:11434",
        "http://10.0.0.5:11434",
        "http://ollama.example.com:11434",
        "https://someone-elses-box.invalid",
        "http://0.0.0.0:11434",
        # Merely starting with "localhost" is not enough.
        "http://localhost.evil.example:11434",
    ],
)
def test_non_loopback_endpoints_are_refused(endpoint):
    # A "local" model on another machine would ship frames off this computer
    # while the interface still said they were staying put.
    with pytest.raises(NonLoopbackEndpointError, match="not on this computer"):
        assert_loopback_endpoint(endpoint)


def test_a_non_http_endpoint_is_refused():
    with pytest.raises(NonLoopbackEndpointError, match="http address"):
        assert_loopback_endpoint("ftp://127.0.0.1:11434")


def test_the_provider_refuses_a_bad_endpoint_at_construction():
    # Fails before any frame is encoded, not partway through a run.
    with pytest.raises(NonLoopbackEndpointError):
        OllamaLocalProvider(endpoint="http://192.168.1.50:11434")


def test_the_default_endpoint_is_loopback():
    assert DEFAULT_ENDPOINT == "http://127.0.0.1:11434"


# ── No credential, ever ───────────────────────────────────────────────────


def test_ollama_is_registered_as_needing_no_credential():
    assert "ollama_local" in NO_CREDENTIAL_PROVIDERS


def test_the_provider_declares_it_needs_no_api_key():
    assert OllamaLocalProvider().requires_api_key is False


def test_reading_a_credential_for_ollama_returns_nothing():
    assert get_credential("ollama_local") is None


def test_storing_a_credential_for_ollama_is_refused():
    # There is no key field for Ollama in the interface, and nothing to store.
    with pytest.raises(CredentialError, match="does not use an API key"):
        set_credential("ollama_local", "anything-at-all")


def test_credential_status_says_no_key_is_needed():
    status = credential_status("ollama_local")
    assert status.present is True
    assert status.requires_credential is False
    assert "no key" in status.detail.lower()


# ── Batch sizing ──────────────────────────────────────────────────────────


def test_the_default_batch_is_one_frame():
    assert DEFAULT_BATCH_SIZE == 1
    assert resolve_batch_size(20, preflight_passed=False) == 1


def test_two_frames_are_allowed_after_a_successful_preflight():
    assert resolve_batch_size(2, preflight_passed=True) == 2


def test_a_cloud_sized_batch_is_clamped_not_accepted():
    # Silently accepting 20 produces an out-of-memory failure minutes into a run.
    assert resolve_batch_size(20, preflight_passed=True) == 2
    assert resolve_batch_size(20, preflight_passed=True, advanced=True) == 4


def test_the_advanced_override_stops_at_four():
    assert resolve_batch_size(99, preflight_passed=True, advanced=True) == MAX_ADVANCED_BATCH_SIZE


def test_a_nonsense_batch_size_falls_back_to_the_default():
    assert resolve_batch_size(0, preflight_passed=True) == DEFAULT_BATCH_SIZE
    assert resolve_batch_size(-5, preflight_passed=True) == DEFAULT_BATCH_SIZE


def test_describing_too_many_frames_at_once_is_refused(frame_image):
    provider = OllamaLocalProvider(client=mock_client(lambda r: httpx.Response(200)))
    with pytest.raises(PermanentProviderError, match="at most"):
        provider.describe(make_request(frame_image, count=MAX_ADVANCED_BATCH_SIZE + 1))


# ── Health checks ─────────────────────────────────────────────────────────


def _health_handler(*, version=True, models=None, show=None, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/version":
            if not version:
                return httpx.Response(500)
            return httpx.Response(200, json={"version": "0.32.6"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": models or []})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=show or {})
        return httpx.Response(404)

    return handler


def test_health_reports_when_nothing_is_answering():
    provider = OllamaLocalProvider(client=mock_client(_health_handler(fail=True)))
    health = provider.health_check()

    assert health.reachable is False
    assert "Nothing is answering" in health.detail
    assert "ollama pull" in health.remediation


def test_health_reports_the_runtime_version():
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(_health_handler(models=[{"name": SUGGESTED_MODEL}])),
    )
    assert provider.health_check().runtime_version == "0.32.6"


def test_health_reports_a_missing_model_with_the_exact_pull_command():
    provider = OllamaLocalProvider(
        model_id="qwen2.5vl:7b", client=mock_client(_health_handler(models=[]))
    )
    health = provider.health_check()

    assert health.reachable is True
    assert health.model_available is False
    assert "ollama pull qwen2.5vl:7b" in health.remediation


def test_vision_is_confirmed_from_model_families():
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(
            _health_handler(
                models=[{"name": SUGGESTED_MODEL}],
                show={"details": {"families": ["qwen2vl", "clip"]}},
            )
        ),
    )
    health = provider.health_check()
    assert health.vision_capable is True
    assert health.vision_verified is True
    assert health.vision_status_label == "Can read pictures"


def test_unconfirmable_vision_says_so_plainly():
    # Not "probably fine" — the spec requires this exact wording, and the UI
    # requires an experimental acknowledgement when it appears.
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(
            _health_handler(models=[{"name": SUGGESTED_MODEL}], show={"details": {}})
        ),
    )
    health = provider.health_check()

    assert health.vision_capable is None
    assert health.vision_verified is False
    assert health.vision_status_label == "Vision capability not verified"
    assert "experimental" in health.remediation.lower()


def test_health_carries_the_memory_advisory():
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(_health_handler(models=[{"name": SUGGESTED_MODEL}])),
    )
    assert provider.health_check().advisory == APPLE_SILICON_ADVISORY


def test_a_model_matching_on_the_base_name_counts_as_installed():
    provider = OllamaLocalProvider(
        model_id="qwen2.5vl:7b",
        client=mock_client(_health_handler(models=[{"name": "qwen2.5vl:latest"}])),
    )
    assert provider.health_check().model_available is True


# ── Describing ────────────────────────────────────────────────────────────


def _generate_handler(body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"response": body})

    return handler


def test_a_good_response_is_normalized(frame_image):
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(
            _generate_handler('[{"index": "01", "currency_pair": "EUR/USD", "confidence": "High"}]')
        ),
    )
    result = provider.describe(make_request(frame_image))

    assert len(result.descriptions) == 1
    assert result.descriptions[0].currency_pair == "EUR/USD"
    assert result.descriptions[0].provider == "ollama_local"


def test_a_local_result_reports_no_charge(frame_image):
    provider = OllamaLocalProvider(client=mock_client(_generate_handler('[{"index": 1}]')))
    result = provider.describe(make_request(frame_image))

    assert result.cost_usd is None
    assert result.cost_label == "No provider API charge"


def test_provenance_is_recorded_on_every_description(frame_image):
    provider = OllamaLocalProvider(
        model_id=SUGGESTED_MODEL,
        client=mock_client(_generate_handler('[{"index": 1}]')),
    )
    description = provider.describe(make_request(frame_image)).descriptions[0]

    assert description.provider == "ollama_local"
    assert description.model_id == SUGGESTED_MODEL
    assert description.prompt_hash
    assert description.schema_hash


def test_a_missing_model_is_a_permanent_failure(frame_image):
    provider = OllamaLocalProvider(client=mock_client(_generate_handler("", status=404)))
    with pytest.raises(PermanentProviderError, match="ollama pull"):
        provider.describe(make_request(frame_image))


def test_a_server_error_is_transient(frame_image):
    provider = OllamaLocalProvider(client=mock_client(_generate_handler("", status=503)))
    with pytest.raises(TransientProviderError):
        provider.describe(make_request(frame_image))


def test_a_timeout_is_transient_with_useful_advice(frame_image):
    def handler(request):
        raise httpx.TimeoutException("too slow", request=request)

    provider = OllamaLocalProvider(client=mock_client(handler))
    with pytest.raises(TransientProviderError, match="Fewer pictures"):
        provider.describe(make_request(frame_image))


def test_misaligned_answers_become_skips_rather_than_wrong_descriptions(frame_image):
    # The alternative is attaching a description to the wrong moment.
    provider = OllamaLocalProvider(client=mock_client(_generate_handler('[{"index": "07"}]')))
    result = provider.describe_with_skips(make_request(frame_image))

    assert result.descriptions == []
    assert len(result.skips) == 1
    assert result.has_gaps is True


def test_unreadable_output_becomes_skips(frame_image):
    provider = OllamaLocalProvider(
        client=mock_client(_generate_handler("I'm sorry, I can't help with that."))
    )
    result = provider.describe_with_skips(make_request(frame_image))
    assert len(result.skips) == 1


def test_skips_still_report_no_provider_charge(frame_image):
    provider = OllamaLocalProvider(client=mock_client(_generate_handler("nonsense")))
    assert result_cost(provider, frame_image) == "No provider API charge"


def result_cost(provider, frame_image):
    return provider.describe_with_skips(make_request(frame_image)).cost_label


# ── Disclosures ───────────────────────────────────────────────────────────


def test_the_disclosure_states_frames_stay_on_this_device():
    assert "Frames stay on this device" in DISCLOSURE
    assert "No provider API charge" in DISCLOSURE
    assert "battery" in DISCLOSURE


def test_the_reliability_warning_names_what_local_models_struggle_with():
    for phrase in ("tiny text", "dense labels", "exact values", "low-confidence"):
        assert phrase in RELIABILITY_WARNING


def test_the_advisory_is_guidance_not_a_promise():
    assert "not a performance guarantee" in APPLE_SILICON_ADVISORY
