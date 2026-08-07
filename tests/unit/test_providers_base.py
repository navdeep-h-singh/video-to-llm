"""The provider contract: schema normalization and alignment validation.

Alignment is the reason the numbered frame copies exist. A model handed twenty
pictures will sometimes answer about them out of order, skip one, or invent an
extra. Accepting any of that attaches a description to the wrong moment in the
video, which is worse than having no description — it looks like evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.providers.base import (
    UNKNOWN,
    AlignmentError,
    AnalysisRequest,
    AnalysisResult,
    Confidence,
    FrameDescription,
    FrameRequest,
    SchemaValidationError,
    SkipRecord,
    extract_json,
    normalize_batch,
    parse_description,
    prompt_hash,
    schema_hash,
    validate_alignment,
)


def make_request(count: int = 3, start: int = 0) -> AnalysisRequest:
    return AnalysisRequest(
        frames=tuple(
            FrameRequest(
                index=start + i,
                timestamp_seconds=float((start + i) * 2),
                image_path=Path(f"/frames/{start + i:06d}.jpg"),
            )
            for i in range(count)
        ),
        model_id="test-model",
        prompt="describe each picture",
    )


def entry(label, **overrides):
    payload = {
        "index": label,
        "timeframe": "15 min",
        "currency_pair": "EUR/USD",
        "indicators_and_states": "RSI 58",
        "exact_action": "price approaching prior high",
        "visible_text": "1.0842",
        "visual_description": "a chart",
        "setup_type": "Trend continuation",
        "confidence": "High",
    }
    payload.update(overrides)
    return payload


# ── Labels ────────────────────────────────────────────────────────────────


def test_idx_labels_are_one_based_and_padded():
    # The stamp drawn on the image reads "IDX 01" for frame index 0.
    request = make_request(3)
    assert [f.idx_label for f in request.frames] == ["01", "02", "03"]


def test_labels_stay_aligned_for_a_later_batch():
    request = make_request(2, start=40)
    assert [f.idx_label for f in request.frames] == ["41", "42"]


def test_a_request_needs_at_least_one_frame():
    with pytest.raises(ValueError, match="at least one frame"):
        AnalysisRequest(frames=(), model_id="m", prompt="p")


# ── Alignment ─────────────────────────────────────────────────────────────


def test_matching_labels_validate():
    validate_alignment(make_request(3), ["01", "02", "03"])


def test_order_does_not_matter_as_long_as_the_set_matches():
    validate_alignment(make_request(3), ["03", "01", "02"])


def test_an_invented_index_is_refused():
    with pytest.raises(AlignmentError, match="were not sent"):
        validate_alignment(make_request(3), ["01", "02", "03", "04"])


def test_a_missing_index_is_refused():
    # Accepting the rest positionally would shift every later description.
    with pytest.raises(AlignmentError, match="missing"):
        validate_alignment(make_request(3), ["01", "03"])


def test_a_duplicated_index_is_refused():
    with pytest.raises(AlignmentError, match="more than once"):
        validate_alignment(make_request(3), ["01", "02", "02"])


def test_an_entirely_wrong_set_is_refused():
    with pytest.raises(AlignmentError):
        validate_alignment(make_request(3), ["07", "08", "09"])


# ── Normalizing a batch ───────────────────────────────────────────────────


def test_a_well_formed_batch_normalizes_in_request_order():
    request = make_request(3)
    raw = [entry("03"), entry("01"), entry("02")]

    descriptions = normalize_batch(request, raw)

    assert [d.index for d in descriptions] == [0, 1, 2]
    assert all(d.currency_pair == "EUR/USD" for d in descriptions)


def test_descriptions_carry_their_frame_timestamp():
    request = make_request(2)
    descriptions = normalize_batch(request, [entry("01"), entry("02")])
    assert [d.timestamp_seconds for d in descriptions] == [0.0, 2.0]


@pytest.mark.parametrize("label", [1, "1", "01", "IDX 01", "idx_1", " 01 "])
def test_index_labels_are_accepted_in_the_forms_models_actually_emit(label):
    request = make_request(1)
    descriptions = normalize_batch(request, [entry(label)])
    assert descriptions[0].index == 0


@pytest.mark.parametrize("key", ["frames", "results", "descriptions", "data", "items", "output"])
def test_a_batch_wrapped_in_a_key_is_unwrapped(key):
    request = make_request(2)
    descriptions = normalize_batch(request, {key: [entry("01"), entry("02")]})
    assert len(descriptions) == 2


def test_a_single_object_for_a_single_frame_is_accepted():
    request = make_request(1)
    assert len(normalize_batch(request, entry("01"))) == 1


def test_a_non_list_response_is_refused():
    with pytest.raises(SchemaValidationError, match="expected a list"):
        normalize_batch(make_request(2), "not json at all")


def test_an_entry_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaValidationError, match="not an object"):
        normalize_batch(make_request(1), ["just a string"])


def test_an_entry_with_no_index_is_refused():
    with pytest.raises(SchemaValidationError, match="which picture"):
        normalize_batch(make_request(1), [{"visual_description": "a chart"}])


# ── Preserving Unknown ────────────────────────────────────────────────────


def test_unknown_is_preserved_rather_than_guessed():
    # A model that cannot read a value must be believed.
    description = parse_description(
        {"currency_pair": "Unknown", "exact_action": "Unknown"}, index=0
    )
    assert description.currency_pair == UNKNOWN
    assert description.exact_action == UNKNOWN


def test_a_missing_field_becomes_unknown_not_empty():
    # An empty string looks like an answer nobody typed; Unknown says nothing
    # was read.
    description = parse_description({}, index=0)
    for name in ("timeframe", "currency_pair", "visible_text", "setup_type"):
        assert getattr(description, name) == UNKNOWN


@pytest.mark.parametrize("value", [None, "", "   "])
def test_null_and_blank_values_become_unknown(value):
    assert parse_description({"timeframe": value}, index=0).timeframe == UNKNOWN


def test_a_list_value_is_joined_rather_than_dropped():
    description = parse_description(
        {"indicators_and_states": ["EMA 20 above EMA 50", "RSI 58"]}, index=0
    )
    assert "EMA 20" in description.indicators_and_states
    assert "RSI 58" in description.indicators_and_states


def test_unknown_field_count_reports_how_much_was_unreadable():
    assert parse_description({}, index=0).unknown_field_count == 7


# ── Confidence ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("High", Confidence.HIGH),
        ("high", Confidence.HIGH),
        ("certain", Confidence.HIGH),
        ("Medium", Confidence.MEDIUM),
        ("moderate", Confidence.MEDIUM),
        ("Low", Confidence.LOW),
        ("unsure", Confidence.LOW),
        (0.95, Confidence.HIGH),
        (0.6, Confidence.MEDIUM),
        (0.2, Confidence.LOW),
        (95, Confidence.HIGH),
        (20, Confidence.LOW),
    ],
)
def test_confidence_wordings_map_onto_three_levels(value, expected):
    assert parse_description({"confidence": value}, index=0).confidence == expected


@pytest.mark.parametrize("value", [None, "", "banana", {"nested": 1}])
def test_unrecognisable_confidence_becomes_low_not_high(value):
    # Promoting an unparseable confidence to High would turn a guess into
    # evidence.
    assert parse_description({"confidence": value}, index=0).confidence == Confidence.LOW


def test_low_confidence_is_flagged_for_review():
    assert parse_description({"confidence": "Low"}, index=0).is_low_confidence is True


# ── Tolerant JSON extraction ──────────────────────────────────────────────


def test_plain_json_is_read():
    assert extract_json('[{"index": 1}]') == [{"index": 1}]


def test_json_inside_a_code_fence_is_read():
    assert extract_json('```json\n[{"index": 1}]\n```') == [{"index": 1}]


def test_json_inside_an_unlabelled_fence_is_read():
    assert extract_json('```\n{"index": 1}\n```') == {"index": 1}


def test_json_surrounded_by_prose_is_read():
    # Models do this constantly regardless of what the prompt says. Recovering
    # here turns an avoidable paid retry into a success.
    text = 'Certainly! Here are the descriptions:\n[{"index": 1}]\nLet me know.'
    assert extract_json(text) == [{"index": 1}]


@pytest.mark.parametrize("text", ["", "   ", "no json here at all"])
def test_unreadable_responses_raise(text):
    with pytest.raises(SchemaValidationError):
        extract_json(text)


# ── Hashes and results ────────────────────────────────────────────────────


def test_schema_hash_is_stable():
    assert schema_hash() == schema_hash()


def test_prompt_hash_changes_with_the_prompt():
    assert prompt_hash("one") != prompt_hash("two")


def test_a_local_result_reports_no_provider_charge_not_zero_dollars():
    # "$0.00" would imply the run was free. It costs battery, heat and time.
    result = AnalysisResult(provider="ollama_local", cost_usd=None)
    assert result.cost_label == "No provider API charge"
    assert "$0.00" not in result.cost_label


def test_a_cloud_result_reports_a_figure():
    assert AnalysisResult(provider="anthropic", cost_usd=0.0042).cost_label == "$0.0042"


def test_skips_are_recorded_rather_than_dropped():
    result = AnalysisResult(skips=[SkipRecord(index=4, reason="unreadable")])
    assert result.has_gaps is True


def test_a_description_serialises_with_its_provenance():
    description = FrameDescription(index=0, provider="ollama_local", model_id="qwen2.5vl:7b")
    payload = description.as_dict()
    assert payload["provider"] == "ollama_local"
    assert payload["model_id"] == "qwen2.5vl:7b"
    assert payload["schema_version"] == 1
