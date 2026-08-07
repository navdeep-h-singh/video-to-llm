"""Cloud adapters, cost estimation, the budget stop, and retry policy.

Every request is mocked. No test makes a paid call, and none requires a key.

Two invariants get the hardest testing: the budget is checked *before* a batch
is sent, and there is never an automatic fall back from a local model to a cloud
one.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.providers.base import (
    AnalysisRequest,
    AnalysisResult,
    FrameRequest,
    PermanentProviderError,
    SchemaValidationError,
    TransientProviderError,
)
from app.providers.cloud import (
    MAX_CLOUD_BATCH_FRAMES,
    AnthropicProvider,
    GoogleProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    build_provider,
)
from app.providers.costs import (
    NO_CHARGE_PROVIDERS,
    BudgetExceededError,
    BudgetTracker,
    estimate_cost,
    load_pricing,
)
from app.providers.retry import (
    SCHEMA_CORRECTION_ATTEMPTS,
    FallbackPolicy,
    RetryPolicy,
    call_with_retries,
    skips_for,
)


@pytest.fixture
def frame_image(tmp_path) -> Path:
    target = tmp_path / "000000.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
    return target


def make_request(frame_image: Path, count: int = 2) -> AnalysisRequest:
    return AnalysisRequest(
        frames=tuple(
            FrameRequest(index=i, timestamp_seconds=float(i * 2), image_path=frame_image)
            for i in range(count)
        ),
        model_id="test-model",
        prompt="describe each picture",
    )


GOOD_JSON = json.dumps(
    [
        {"index": "01", "currency_pair": "EUR/USD", "confidence": "High"},
        {"index": "02", "currency_pair": "GBP/USD", "confidence": "Medium"},
    ]
)


def client_returning(body: dict, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


ANTHROPIC_BODY = {
    "content": [{"type": "text", "text": GOOD_JSON}],
    "usage": {"input_tokens": 3200, "output_tokens": 640},
}
GOOGLE_BODY = {
    "candidates": [{"content": {"parts": [{"text": GOOD_JSON}]}}],
    "usageMetadata": {"promptTokenCount": 2600, "candidatesTokenCount": 640},
}
OPENAI_BODY = {
    "choices": [{"message": {"content": GOOD_JSON}}],
    "usage": {"prompt_tokens": 2900, "completion_tokens": 640},
}


# ── Every adapter satisfies the same contract ─────────────────────────────


@pytest.mark.parametrize(
    ("factory", "body"),
    [
        (AnthropicProvider, ANTHROPIC_BODY),
        (GoogleProvider, GOOGLE_BODY),
        (OpenAIProvider, OPENAI_BODY),
    ],
)
def test_each_adapter_normalizes_its_own_response_shape(factory, body, frame_image):
    provider = factory(model_id="m", api_key="test-key-value", client=client_returning(body))
    result = provider.describe(make_request(frame_image))

    assert [d.index for d in result.descriptions] == [0, 1]
    assert result.descriptions[0].currency_pair == "EUR/USD"
    assert result.provider == provider.name


@pytest.mark.parametrize(
    ("factory", "body"),
    [
        (AnthropicProvider, ANTHROPIC_BODY),
        (GoogleProvider, GOOGLE_BODY),
        (OpenAIProvider, OPENAI_BODY),
    ],
)
def test_each_adapter_records_provenance(factory, body, frame_image):
    provider = factory(model_id="m", api_key="test-key-value", client=client_returning(body))
    description = provider.describe(make_request(frame_image)).descriptions[0]

    assert description.provider == provider.name
    assert description.model_id == "test-model"
    assert description.prompt_hash and description.schema_hash


@pytest.mark.parametrize(
    ("factory", "body"),
    [
        (AnthropicProvider, ANTHROPIC_BODY),
        (GoogleProvider, GOOGLE_BODY),
        (OpenAIProvider, OPENAI_BODY),
    ],
)
def test_each_adapter_reports_token_usage(factory, body, frame_image):
    provider = factory(model_id="m", api_key="test-key-value", client=client_returning(body))
    result = provider.describe(make_request(frame_image))
    assert result.input_tokens and result.output_tokens


def test_cloud_batches_are_capped_at_twenty(frame_image):
    provider = AnthropicProvider(
        model_id="m", api_key="test-key-value", client=client_returning(ANTHROPIC_BODY)
    )
    with pytest.raises(PermanentProviderError, match="at most"):
        provider.describe(make_request(frame_image, count=MAX_CLOUD_BATCH_FRAMES + 1))


def test_twenty_frames_are_allowed(frame_image):
    body = {
        "content": [
            {
                "type": "text",
                "text": json.dumps([{"index": f"{i + 1:02d}"} for i in range(20)]),
            }
        ]
    }
    provider = AnthropicProvider(
        model_id="m", api_key="test-key-value", client=client_returning(body)
    )
    assert len(provider.describe(make_request(frame_image, 20)).descriptions) == 20


# ── What actually gets sent ───────────────────────────────────────────────


def test_only_images_and_the_prompt_are_sent(frame_image):
    """No filename, no path, no transcript — just bytes and instructions."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=ANTHROPIC_BODY)

    provider = AnthropicProvider(
        model_id="m",
        api_key="test-key-value",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.describe(make_request(frame_image))

    sent = captured["body"]
    assert "000000.jpg" not in sent, "a filename from the user's disk was sent"
    assert str(frame_image.parent) not in sent, "a local path was sent"
    assert "base64" in sent


def test_the_api_key_travels_in_a_header_not_the_url(frame_image):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=GOOGLE_BODY)

    provider = GoogleProvider(
        model_id="gemini-2.5-flash",
        api_key="test-key-value",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.describe(make_request(frame_image))

    # A key in a URL ends up in logs, proxies, and history.
    assert "test-key-value" not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == "test-key-value"


# ── Error classification ──────────────────────────────────────────────────


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 529])
def test_temporary_service_errors_are_transient(status, frame_image):
    provider = AnthropicProvider(
        model_id="m", api_key="k-value", client=client_returning({}, status=status)
    )
    with pytest.raises(TransientProviderError):
        provider.describe(make_request(frame_image))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_permanent(status, frame_image):
    # Retrying a bad key or unknown model just spends time and money.
    provider = AnthropicProvider(
        model_id="m", api_key="k-value", client=client_returning({}, status=status)
    )
    with pytest.raises(PermanentProviderError):
        provider.describe(make_request(frame_image))


def test_a_rejected_key_error_does_not_echo_the_key(frame_image):
    provider = AnthropicProvider(
        model_id="m", api_key="super-secret-key-value", client=client_returning({}, status=401)
    )
    with pytest.raises(PermanentProviderError) as excinfo:
        provider.describe(make_request(frame_image))
    assert "super-secret-key-value" not in str(excinfo.value)


def test_a_timeout_is_transient(frame_image):
    def handler(request):
        raise httpx.TimeoutException("slow", request=request)

    provider = AnthropicProvider(
        model_id="m",
        api_key="k-value",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TransientProviderError):
        provider.describe(make_request(frame_image))


def test_an_openai_compatible_endpoint_without_an_address_is_refused(frame_image):
    provider = OpenAICompatibleProvider(model_id="m", api_key="k-value")
    with pytest.raises(PermanentProviderError, match="No address is set"):
        provider.describe(make_request(frame_image))


def test_health_check_makes_no_paid_call():
    # A readiness check that silently bills the user would be a poor surprise.
    provider = AnthropicProvider(model_id="m", api_key="k-value")
    health = provider.health_check()
    assert health.reachable is True
    assert "costs money" in health.remediation


def test_build_provider_returns_each_adapter():
    for name in ("anthropic", "google", "openai", "openai_compatible", "ollama_local"):
        assert build_provider(name) is not None


def test_build_provider_refuses_an_unknown_name():
    with pytest.raises(PermanentProviderError, match="Unknown provider"):
        build_provider("not-a-provider")


# ── Cost estimation ───────────────────────────────────────────────────────


def test_a_local_provider_has_no_estimated_cost():
    estimate = estimate_cost("ollama_local", 1000)
    assert estimate.estimated_usd is None
    assert estimate.label == "No provider API charge"
    assert "$0.00" not in estimate.label


@pytest.mark.parametrize("provider", ["anthropic", "google", "openai"])
def test_cloud_estimates_scale_with_frame_count(provider):
    small = estimate_cost(provider, 100)
    large = estimate_cost(provider, 1000)
    assert large.estimated_usd > small.estimated_usd > 0


def test_an_unknown_provider_uses_pessimistic_assumptions():
    # Over-estimating is safe; under-estimating overruns the budget.
    unknown = estimate_cost("some-new-service", 1000)
    cheap = estimate_cost("google", 1000)
    assert unknown.estimated_usd > cheap.estimated_usd


def test_pricing_falls_back_when_the_file_is_malformed(tmp_path):
    broken = tmp_path / "pricing.toml"
    broken.write_text("this is not [valid toml", encoding="utf-8")
    pricing = load_pricing(broken)
    assert "anthropic" in pricing, "a bad pricing file must not break local jobs"


def test_pricing_file_overrides_are_applied(tmp_path):
    custom = tmp_path / "pricing.toml"
    custom.write_text("[anthropic]\ninput_per_million_tokens = 99.0\n", encoding="utf-8")
    pricing = load_pricing(custom)
    assert pricing["anthropic"]["input_per_million_tokens"] == 99.0


# ── The budget stop ───────────────────────────────────────────────────────


def test_the_budget_is_checked_before_sending_not_after():
    # Checking afterwards would mean the spend that crossed the limit had
    # already left, which makes the cap decorative.
    tracker = BudgetTracker(limit_usd=25.0, spent_usd=24.50, provider="anthropic")
    with pytest.raises(BudgetExceededError, match="would cost"):
        tracker.check_before_send(1.00)


def test_a_batch_inside_the_limit_is_allowed():
    BudgetTracker(limit_usd=25.0, spent_usd=10.0, provider="anthropic").check_before_send(1.0)


def test_the_budget_message_says_finished_work_is_kept():
    tracker = BudgetTracker(limit_usd=1.0, spent_usd=0.99, provider="anthropic")
    with pytest.raises(BudgetExceededError) as excinfo:
        tracker.check_before_send(5.0)
    assert "kept" in str(excinfo.value)
    assert "where it stopped" in str(excinfo.value)


def test_the_budget_does_not_apply_to_local_providers():
    # There is no provider charge to cap.
    tracker = BudgetTracker(limit_usd=0.0, provider="ollama_local")
    assert tracker.applies is False
    tracker.check_before_send(999.0)
    assert tracker.progress_label == "No provider API charge"


def test_spend_accumulates():
    tracker = BudgetTracker(limit_usd=25.0, provider="anthropic")
    tracker.record(1.25)
    tracker.record(2.50)
    assert tracker.spent_usd == 3.75
    assert tracker.remaining_usd == 21.25


def test_a_local_run_records_no_spend():
    tracker = BudgetTracker(limit_usd=25.0, provider="anthropic")
    tracker.record(None)
    assert tracker.spent_usd == 0.0


def test_an_exhausted_budget_is_reported():
    assert BudgetTracker(limit_usd=25.0, spent_usd=25.0, provider="anthropic").exhausted is True


def test_ollama_is_registered_as_a_no_charge_provider():
    assert "ollama_local" in NO_CHARGE_PROVIDERS


# ── Retry policy ──────────────────────────────────────────────────────────


def test_a_transient_failure_is_retried_then_succeeds(frame_image):
    attempts = {"n": 0}

    def send(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientProviderError("rate limited")
        return AnalysisResult(provider="anthropic")

    outcome = call_with_retries(
        make_request(frame_image),
        send,
        policy=RetryPolicy(base_delay_seconds=0),
        sleep=lambda _: None,
    )

    assert outcome.succeeded is True
    assert attempts["n"] == 3
    assert len(outcome.history) == 2


def test_retries_are_bounded(frame_image):
    # An unbounded retry against a paid provider is an unbounded bill.
    attempts = {"n": 0}

    def send(request):
        attempts["n"] += 1
        raise TransientProviderError("still failing")

    outcome = call_with_retries(
        make_request(frame_image),
        send,
        policy=RetryPolicy(max_attempts=3, base_delay_seconds=0),
        sleep=lambda _: None,
    )

    assert outcome.succeeded is False
    assert attempts["n"] == 3
    assert "Gave up after 3" in outcome.gave_up_reason


def test_a_permanent_failure_is_not_retried(frame_image):
    attempts = {"n": 0}

    def send(request):
        attempts["n"] += 1
        raise PermanentProviderError("the key was rejected")

    outcome = call_with_retries(make_request(frame_image), send, sleep=lambda _: None)

    assert attempts["n"] == 1, "a permanent failure must not be retried"
    assert outcome.succeeded is False


def test_invalid_json_gets_exactly_one_corrective_retry(frame_image):
    prompts: list[str] = []

    def send(request):
        prompts.append(request.prompt)
        raise SchemaValidationError("could not read the response")

    outcome = call_with_retries(
        make_request(frame_image),
        send,
        policy=RetryPolicy(max_attempts=5, base_delay_seconds=0),
        sleep=lambda _: None,
    )

    # One original attempt plus exactly one correction. A model that ignores the
    # correction once will ignore it again, and every attempt is billed.
    assert len(prompts) == 1 + SCHEMA_CORRECTION_ATTEMPTS
    assert "JSON array" in prompts[1], "the retry should tell the model what went wrong"
    assert outcome.succeeded is False
    assert "even after asking again" in outcome.gave_up_reason


def test_a_corrected_response_succeeds(frame_image):
    calls = {"n": 0}

    def send(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SchemaValidationError("prose, not JSON")
        return AnalysisResult(provider="anthropic")

    outcome = call_with_retries(
        make_request(frame_image),
        send,
        policy=RetryPolicy(base_delay_seconds=0),
        sleep=lambda _: None,
    )
    assert outcome.succeeded is True


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(base_delay_seconds=5.0, max_delay_seconds=60.0, jitter=False)
    assert policy.delay_for(1) == 5.0
    assert policy.delay_for(2) == 10.0
    assert policy.delay_for(3) == 20.0
    assert policy.delay_for(10) == 60.0


def test_jitter_spreads_retries():
    # Without it, batches that hit a rate limit together wake together and hit
    # it again.
    policy = RetryPolicy(base_delay_seconds=10.0, jitter=True)
    delays = {policy.delay_for(2) for _ in range(20)}
    assert len(delays) > 1


def test_a_failed_batch_becomes_visible_skips(frame_image):
    request = make_request(frame_image, count=3)
    skips = skips_for(request, "the service refused", attempts=3)

    assert [s.index for s in skips] == [0, 1, 2]
    assert all(s.permanent for s in skips)


def test_retry_history_is_attached_to_the_result(frame_image):
    calls = {"n": 0}

    def send(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientProviderError("rate limited")
        return AnalysisResult(provider="anthropic")

    outcome = call_with_retries(
        make_request(frame_image),
        send,
        policy=RetryPolicy(base_delay_seconds=0),
        sleep=lambda _: None,
    )
    assert outcome.result.retry_history
    assert outcome.result.retry_history[0]["kind"] == "transient"


# ── Fallback policy ───────────────────────────────────────────────────────


def test_fallbacks_are_off_by_default():
    assert FallbackPolicy().next_provider("anthropic") is None


def test_an_enabled_fallback_moves_to_the_next_cloud_provider():
    policy = FallbackPolicy(enabled=True, order=("anthropic", "openai", "google"))
    assert policy.next_provider("anthropic") == "openai"
    assert policy.next_provider("openai") == "google"


def test_the_last_provider_has_no_successor():
    policy = FallbackPolicy(enabled=True, order=("anthropic", "openai"))
    assert policy.next_provider("openai") is None


def test_local_never_falls_back_to_cloud_automatically():
    # This is the invariant that matters most: falling back would send frames
    # off the machine after the user chose to keep them on it.
    policy = FallbackPolicy(enabled=True, order=("ollama_local", "anthropic"))
    assert policy.next_provider("ollama_local") is None


def test_local_to_cloud_requires_explicit_configuration():
    policy = FallbackPolicy(
        enabled=True, order=("ollama_local", "anthropic"), allow_local_to_cloud=True
    )
    assert policy.next_provider("ollama_local") == "anthropic"


def test_an_unknown_current_provider_has_no_successor():
    policy = FallbackPolicy(enabled=True, order=("anthropic", "openai"))
    assert policy.next_provider("something-else") is None
