"""Silence detection, backend resolution, and transcript assembly.

The property that matters throughout: **the original video timeline is
preserved**. A transcript whose times drift after the first silence looks
perfectly plausible and is completely useless, so the remapping is tested
directly rather than inferred.

faster-whisper is mocked here. A real model would make these tests slow, need a
1.5 GB download, and prove nothing about the arithmetic under test.
"""

from __future__ import annotations

import json
from itertools import pairwise

import pytest

from app.pipeline.audio import (
    SilenceWindow,
    SpeechSegment,
    close_trailing_silence,
    parse_silence_output,
    speech_segments,
    write_silence_windows,
)
from app.pipeline.transcribe import (
    TranscriptionProvenance,
    TranscriptionResult,
    TranscriptSegment,
    build_transcript,
    resolve_backend,
    silence_marker_text,
    write_transcript,
)


class FakeTranscriber:
    """Returns one utterance per window, at a fixed offset *inside* the window.

    The offset is what makes the remapping visible: if the pipeline forgot to add
    the window's start, every timestamp would come back as 0.5.
    """

    def __init__(self, offset: float = 0.5, text: str = "spoken words"):
        self.offset = offset
        self.text = text
        self.calls: list[tuple[float, float]] = []

    def transcribe_window(self, audio_path, start_seconds, end_seconds):
        self.calls.append((start_seconds, end_seconds))
        return [(self.offset, self.offset + 1.0, self.text)]


# ── Parsing FFmpeg's silencedetect output ─────────────────────────────────

SAMPLE_LOG = """
[silencedetect @ 0x7f] silence_start: 2.05
[silencedetect @ 0x7f] silence_end: 5.12 | silence_duration: 3.07
[silencedetect @ 0x7f] silence_start: 9.4
[silencedetect @ 0x7f] silence_end: 14.9 | silence_duration: 5.5
"""


def test_silence_windows_are_parsed():
    windows = parse_silence_output(SAMPLE_LOG)
    assert [(w.start_seconds, w.end_seconds) for w in windows] == [(2.05, 5.12), (9.4, 14.9)]


def test_output_with_no_silence_yields_nothing():
    assert parse_silence_output("[info] nothing to report here") == []


def test_a_negative_start_is_clamped_to_zero():
    # FFmpeg occasionally reports a small negative start on the first window.
    windows = parse_silence_output(
        "silence_start: -0.01\nsilence_end: 4.0 | silence_duration: 4.01"
    )
    assert windows[0].start_seconds == 0.0


def test_an_unclosed_window_is_ignored_by_the_parser():
    # It is completed separately, where the file duration is known.
    assert parse_silence_output("silence_start: 8.0") == []


def test_a_file_ending_mid_silence_keeps_its_final_gap():
    log = "silence_start: 2.0\nsilence_end: 5.0\nsilence_start: 8.0"
    windows = parse_silence_output(log)
    completed = close_trailing_silence(windows, log, duration_seconds=12.0)
    assert len(completed) == 2
    assert (completed[-1].start_seconds, completed[-1].end_seconds) == (8.0, 12.0)


def test_nothing_is_added_when_every_window_is_already_closed():
    windows = parse_silence_output(SAMPLE_LOG)
    assert close_trailing_silence(windows, SAMPLE_LOG, 20.0) == windows


def test_silence_window_duration():
    assert SilenceWindow(2.0, 5.5).duration_seconds == 3.5


# ── Inverting silence into speech segments ────────────────────────────────


def test_speech_segments_are_the_gaps_between_silences():
    segments = speech_segments(
        [SilenceWindow(2.0, 5.0), SilenceWindow(7.0, 9.0)],
        duration_seconds=12.0,
        padding_seconds=0.0,
    )
    assert [(s.start_seconds, s.end_seconds) for s in segments] == [
        (0.0, 2.0),
        (5.0, 7.0),
        (9.0, 12.0),
    ]


def test_a_video_with_no_silence_is_one_segment():
    segments = speech_segments([], duration_seconds=30.0, padding_seconds=0.0)
    assert [(s.start_seconds, s.end_seconds) for s in segments] == [(0.0, 30.0)]


def test_a_completely_silent_video_has_no_speech_segments():
    segments = speech_segments(
        [SilenceWindow(0.0, 20.0)], duration_seconds=20.0, padding_seconds=0.0
    )
    assert segments == []


def test_padding_extends_segments_outward():
    # A hard cut at the exact boundary clips the first and last syllable.
    segments = speech_segments(
        [SilenceWindow(5.0, 9.0)], duration_seconds=14.0, padding_seconds=0.5
    )
    assert segments[0].end_seconds == 5.5
    assert segments[1].start_seconds == 8.5


def test_padding_never_runs_past_the_start_of_the_video():
    segments = speech_segments(
        [SilenceWindow(5.0, 9.0)], duration_seconds=14.0, padding_seconds=2.0
    )
    assert segments[0].start_seconds == 0.0


def test_padding_never_runs_past_the_end_of_the_video():
    segments = speech_segments(
        [SilenceWindow(2.0, 4.0)], duration_seconds=10.0, padding_seconds=5.0
    )
    assert segments[-1].end_seconds == 10.0


def test_padded_segments_never_overlap():
    segments = speech_segments(
        [SilenceWindow(3.0, 4.0), SilenceWindow(6.0, 7.0)],
        duration_seconds=10.0,
        padding_seconds=2.0,
    )
    for earlier, later in pairwise(segments):
        assert earlier.end_seconds <= later.start_seconds


def test_overlapping_silence_windows_are_handled():
    segments = speech_segments(
        [SilenceWindow(2.0, 6.0), SilenceWindow(4.0, 8.0)],
        duration_seconds=12.0,
        padding_seconds=0.0,
    )
    assert [(s.start_seconds, s.end_seconds) for s in segments] == [(0.0, 2.0), (8.0, 12.0)]


def test_unordered_silence_windows_are_sorted_first():
    segments = speech_segments(
        [SilenceWindow(7.0, 9.0), SilenceWindow(2.0, 5.0)],
        duration_seconds=12.0,
        padding_seconds=0.0,
    )
    assert [(s.start_seconds, s.end_seconds) for s in segments] == [
        (0.0, 2.0),
        (5.0, 7.0),
        (9.0, 12.0),
    ]


def test_a_zero_length_video_has_no_segments():
    assert speech_segments([], duration_seconds=0.0) == []


# ── Backend resolution ────────────────────────────────────────────────────


def test_cpu_is_honoured_exactly():
    resolved = resolve_backend("cpu")
    assert resolved.name == "cpu"
    assert resolved.fell_back is False


def test_auto_always_resolves_to_something_usable():
    resolved = resolve_backend("auto")
    assert resolved.device in {"cpu", "cuda"}
    assert resolved.compute_type


@pytest.mark.parametrize("requested", ["metal", "vulkan"])
def test_unsupported_accelerators_fall_back_to_cpu_with_a_reason(requested):
    # Claiming an accelerator that is not actually in use would be a lie the
    # user could only discover by timing the run.
    resolved = resolve_backend(requested)
    assert resolved.device == "cpu"
    assert resolved.fell_back is True
    assert requested in resolved.reason


def test_cuda_falls_back_when_no_device_is_present(monkeypatch):
    monkeypatch.setattr("app.pipeline.transcribe._cuda_available", lambda: False)
    resolved = resolve_backend("cuda")
    assert resolved.device == "cpu"
    assert resolved.fell_back is True
    assert "CUDA" in resolved.reason


def test_cuda_is_used_when_a_device_is_present(monkeypatch):
    monkeypatch.setattr("app.pipeline.transcribe._cuda_available", lambda: True)
    resolved = resolve_backend("cuda")
    assert resolved.device == "cuda"
    assert resolved.fell_back is False


def test_an_unknown_backend_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setattr("app.pipeline.transcribe._cuda_available", lambda: False)
    resolved = resolve_backend("quantum")
    assert resolved.device == "cpu"
    assert resolved.fell_back is True


def test_probing_for_cuda_never_raises(monkeypatch):
    def explode():
        raise RuntimeError("driver missing")

    monkeypatch.setattr("app.pipeline.transcribe.ctranslate2", None, raising=False)
    # Even with a hostile environment, resolution must return something.
    assert resolve_backend("auto").device in {"cpu", "cuda"}


# ── Transcript assembly: the timeline ─────────────────────────────────────


def test_timestamps_are_remapped_onto_the_original_timeline(tmp_path):
    # The whole point. Without remapping, both utterances would report 0.5.
    transcriber = FakeTranscriber(offset=0.5)
    segments = [SpeechSegment(0.0, 2.0), SpeechSegment(10.0, 12.0)]

    result = build_transcript(tmp_path / "a.wav", segments, [], transcriber)
    spoken = [s for s in result if not s.is_silence]

    assert [s.start_seconds for s in spoken] == [0.5, 10.5]


def test_a_transcript_with_many_gaps_stays_aligned(tmp_path):
    transcriber = FakeTranscriber(offset=0.25)
    segments = [SpeechSegment(float(t), float(t + 2)) for t in (0, 30, 60, 300, 3600)]

    spoken = [
        s
        for s in build_transcript(tmp_path / "a.wav", segments, [], transcriber)
        if not s.is_silence
    ]
    assert [s.start_seconds for s in spoken] == [0.25, 30.25, 60.25, 300.25, 3600.25]


def test_silence_markers_are_woven_in_chronologically(tmp_path):
    transcriber = FakeTranscriber(offset=0.0)
    segments = [SpeechSegment(0.0, 2.0), SpeechSegment(5.0, 7.0)]
    silences = [SilenceWindow(2.0, 5.0)]

    result = build_transcript(tmp_path / "a.wav", segments, silences, transcriber)

    assert [s.start_seconds for s in result] == [0.0, 2.0, 5.0]
    assert [s.is_silence for s in result] == [False, True, False]


def test_the_transcript_is_sorted_by_time(tmp_path):
    transcriber = FakeTranscriber()
    segments = [SpeechSegment(20.0, 22.0), SpeechSegment(0.0, 2.0)]
    silences = [SilenceWindow(40.0, 45.0), SilenceWindow(2.0, 20.0)]

    result = build_transcript(tmp_path / "a.wav", segments, silences, transcriber)
    times = [s.start_seconds for s in result]
    assert times == sorted(times)


def test_a_failing_window_does_not_lose_the_rest_of_the_transcript(tmp_path):
    class PartlyBroken(FakeTranscriber):
        def transcribe_window(self, audio_path, start_seconds, end_seconds):
            if start_seconds == 10.0:
                raise RuntimeError("this stretch is unreadable")
            return super().transcribe_window(audio_path, start_seconds, end_seconds)

    segments = [SpeechSegment(0.0, 2.0), SpeechSegment(10.0, 12.0), SpeechSegment(20.0, 22.0)]
    result = build_transcript(tmp_path / "a.wav", segments, [], PartlyBroken())

    assert [s.start_seconds for s in result] == [0.5, 20.5]


def test_every_speech_segment_is_offered_to_the_transcriber(tmp_path):
    transcriber = FakeTranscriber()
    segments = [SpeechSegment(0.0, 2.0), SpeechSegment(5.0, 9.0)]
    build_transcript(tmp_path / "a.wav", segments, [], transcriber)
    assert transcriber.calls == [(0.0, 2.0), (5.0, 9.0)]


def test_silence_markers_are_written_in_plain_language():
    text = silence_marker_text(SilenceWindow(10.0, 21.0))
    assert "11 seconds" in text
    assert "nobody speaking" in text


def test_a_video_with_no_speech_still_produces_silence_markers(tmp_path):
    result = build_transcript(tmp_path / "a.wav", [], [SilenceWindow(0.0, 30.0)], FakeTranscriber())
    assert len(result) == 1
    assert result[0].is_silence is True


# ── Rendering ─────────────────────────────────────────────────────────────


def test_transcript_segment_labels_are_hhmmss():
    assert TranscriptSegment(3661.0, 3665.0, "x").timestamp_label == "01:01:01"


def test_result_text_renders_timestamps_with_the_words():
    result = TranscriptionResult(
        segments=[
            TranscriptSegment(0.0, 2.0, "first thing"),
            TranscriptSegment(2.0, 5.0, "[nobody speaking · 3 seconds]", is_silence=True),
        ]
    )
    assert "00:00:00  first thing" in result.text
    assert "00:00:02  [nobody speaking · 3 seconds]" in result.text


def test_spoken_segments_excludes_silence():
    result = TranscriptionResult(
        segments=[
            TranscriptSegment(0.0, 2.0, "words"),
            TranscriptSegment(2.0, 5.0, "quiet", is_silence=True),
        ]
    )
    assert len(result.spoken_segments) == 1


def test_written_transcript_records_provenance_including_fallback(tmp_path):
    result = TranscriptionResult(
        segments=[TranscriptSegment(0.0, 2.0, "words")],
        provenance=TranscriptionProvenance(
            requested_backend="metal",
            resolved_backend="cpu",
            fell_back=True,
            fallback_reason="metal acceleration is not available",
            model="medium",
        ),
    )
    json_path, text_path = write_transcript(tmp_path, result, source_filename="clip.mp4")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["provenance"]["fell_back"] is True
    assert payload["provenance"]["requested_backend"] == "metal"
    assert payload["provenance"]["resolved_backend"] == "cpu"
    assert payload["source_filename"] == "clip.mp4"
    assert "words" in text_path.read_text(encoding="utf-8")


def test_silence_windows_file_records_the_threshold(tmp_path):
    target = tmp_path / "silence_windows.json"
    write_silence_windows(
        target, [SilenceWindow(2.0, 6.0), SilenceWindow(10.0, 14.0)], threshold_seconds=3.0
    )
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["threshold_seconds"] == 3.0
    assert payload["count"] == 2
    assert payload["total_silent_seconds"] == 8.0
