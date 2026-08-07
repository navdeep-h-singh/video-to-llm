"""Live verification against a real local Ollama.

Opt-in. CI deselects this file, and it skips itself when nothing is answering on
the loopback interface, so a machine without Ollama is never a failing build.

Run it deliberately:

    uv run pytest tests/integration/test_live_ollama.py -m live_ollama -v

This exists because the mocked tests prove the adapter handles the shapes we
*expect*, and this proves those are the shapes a real runtime actually sends.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import AnalysisRequest, FrameRequest
from app.providers.ollama_local import (
    DEFAULT_ENDPOINT,
    SUGGESTED_MODEL,
    OllamaLocalProvider,
)

pytestmark = pytest.mark.live_ollama


def _ollama_running() -> bool:
    try:
        response = httpx.get(f"{DEFAULT_ENDPOINT}/api/version", timeout=3.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


needs_ollama = pytest.mark.skipif(
    not _ollama_running(), reason="no local Ollama is answering on 127.0.0.1:11434"
)


@pytest.fixture
def provider() -> OllamaLocalProvider:
    return OllamaLocalProvider(model_id=SUGGESTED_MODEL)


@pytest.fixture
def api_frame(tmp_path):
    """A real numbered frame, produced by the real pipeline."""
    from app.pipeline.frames import extract_frames
    from app.pipeline.probe import probe
    from tests.fixtures.synthetic import ffmpeg_available, make_video

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not installed")

    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=2.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=1000)
    record = result.frames[0]
    return result.api_frames_dir / record.api_filename


@needs_ollama
def test_the_real_runtime_reports_its_version(provider):
    health = provider.health_check()
    assert health.reachable is True
    assert health.runtime_version, "a live runtime should report a version"


@needs_ollama
def test_the_real_model_is_detected_as_installed(provider):
    health = provider.health_check()
    assert health.model_available is True, (
        f"{SUGGESTED_MODEL} is not installed — run: ollama pull {SUGGESTED_MODEL}"
    )


@needs_ollama
def test_vision_capability_is_reported_honestly(provider):
    # Either answer is acceptable. What must not happen is a cheerful assumption
    # when the runtime did not actually confirm it.
    health = provider.health_check()
    assert health.vision_capable in {True, None}
    assert health.vision_status_label in {"Can read pictures", "Vision capability not verified"}


@needs_ollama
def test_a_missing_model_is_reported_against_the_real_runtime():
    provider = OllamaLocalProvider(model_id="definitely-not-a-real-model:99b")
    health = provider.health_check()

    assert health.reachable is True
    assert health.model_available is False
    assert "ollama pull definitely-not-a-real-model:99b" in health.remediation


@needs_ollama
@pytest.mark.slow
def test_a_real_frame_is_described_and_stays_aligned(provider, api_frame):
    """The end-to-end local path, for real.

    Slow by nature: a 7B vision model on a laptop takes a while for one frame.
    """
    request = AnalysisRequest(
        frames=(FrameRequest(index=0, timestamp_seconds=0.0, image_path=api_frame),),
        model_id=SUGGESTED_MODEL,
        prompt=(
            "Describe this picture. Reply with ONLY a JSON array containing one "
            "object with keys: index, timeframe, currency_pair, "
            "indicators_and_states, exact_action, visible_text, "
            "visual_description, setup_type, confidence. "
            'Set index to "01". Use "Unknown" for anything you cannot read.'
        ),
    )

    result = provider.describe_with_skips(request)

    # A local model may legitimately fail to produce clean JSON — that is what
    # the reliability warning is about, and a skip is the correct outcome. What
    # must never happen is a description attached to the wrong frame.
    if result.descriptions:
        assert result.descriptions[0].index == 0
        assert result.descriptions[0].provider == "ollama_local"
        assert result.descriptions[0].model_id == SUGGESTED_MODEL
    else:
        assert result.skips and result.skips[0].index == 0

    assert result.cost_usd is None
    assert result.cost_label == "No provider API charge"
