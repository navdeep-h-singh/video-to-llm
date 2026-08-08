"""Deterministic enrichment and chronological assembly.

Enrichment is rules, not a model — so these test real arithmetic and real
decisions, and the same input must always produce the same output.

Assembly's organising principle is time order. A transcript line and a frame
description from the same moment belong next to each other; grouping by source
would preserve every fact and destroy what makes them useful together.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.pipeline.assemble import (
    MasterSource,
    assemble_master,
    assemble_video,
    build_entries,
    describe_frame,
    format_timestamp,
)
from app.pipeline.audio import SilenceWindow
from app.pipeline.enrich import (
    EMPHASIS_ACTION,
    EMPHASIS_LONG_SILENCE,
    EMPHASIS_LOW_CONFIDENCE,
    EMPHASIS_UNREADABLE,
    build_segments,
    enrich,
    find_emphasis,
    find_switches,
)
from app.pipeline.transcribe import TranscriptSegment
from app.providers.base import UNKNOWN, Confidence, FrameDescription


def description(
    index,
    *,
    pair=UNKNOWN,
    timeframe=UNKNOWN,
    action=UNKNOWN,
    confidence=Confidence.HIGH,
    seconds=None,
    **extra,
):
    return FrameDescription(
        index=index,
        currency_pair=pair,
        timeframe=timeframe,
        exact_action=action,
        confidence=confidence,
        timestamp_seconds=seconds if seconds is not None else float(index * 2),
        **extra,
    )


# ── Emphasis ──────────────────────────────────────────────────────────────


def test_low_confidence_is_emphasised():
    found = find_emphasis([description(0, confidence=Confidence.LOW)])
    assert EMPHASIS_LOW_CONFIDENCE in found[0].reasons


def test_high_confidence_with_nothing_notable_is_not_emphasised():
    frame = description(
        0,
        pair="EUR/USD",
        timeframe="15 min",
        indicators_and_states="RSI 58",
        visible_text="1.0842",
        visual_description="a chart",
        setup_type="Trend",
    )
    assert find_emphasis([frame]) == []


def test_a_mostly_unreadable_frame_is_emphasised_whatever_it_claims():
    # Three or more unreadable fields means the frame told us very little, even
    # if the model reported High confidence.
    frame = description(0, pair="EUR/USD", confidence=Confidence.HIGH)
    found = find_emphasis([frame])
    assert EMPHASIS_UNREADABLE in found[0].reasons


def test_a_readable_action_is_emphasised():
    frame = description(
        0,
        pair="EUR/USD",
        timeframe="15 min",
        action="position closed",
        indicators_and_states="RSI 41",
        visible_text="+41 pips",
        visual_description="panel empty",
        setup_type="Exit",
    )
    assert EMPHASIS_ACTION in find_emphasis([frame])[0].reasons


def test_a_long_silence_is_emphasised():
    found = find_emphasis([], [SilenceWindow(120.0, 200.0)])
    assert EMPHASIS_LONG_SILENCE in found[0].reasons


def test_a_short_silence_is_not_emphasised():
    assert find_emphasis([], [SilenceWindow(10.0, 14.0)]) == []


def test_emphasis_is_returned_in_time_order():
    found = find_emphasis(
        [description(5, confidence=Confidence.LOW), description(1, confidence=Confidence.LOW)],
        [SilenceWindow(60.0, 120.0)],
    )
    assert [e.timestamp_seconds for e in found] == sorted(e.timestamp_seconds for e in found)


def test_several_reasons_are_collected_together():
    frame = description(0, confidence=Confidence.LOW, action="entry marked")
    reasons = find_emphasis([frame])[0].reasons
    assert EMPHASIS_LOW_CONFIDENCE in reasons
    assert EMPHASIS_ACTION in reasons


# ── Switches ──────────────────────────────────────────────────────────────


def test_an_instrument_change_is_a_switch():
    switches = find_switches(
        [
            description(0, pair="EUR/USD"),
            description(1, pair="EUR/USD"),
            description(2, pair="GBP/USD"),
        ]
    )
    assert len(switches) == 1
    assert switches[0].index == 2
    assert switches[0].to_instrument == "GBP/USD"


def test_a_timeframe_change_is_a_switch():
    switches = find_switches(
        [
            description(0, pair="EUR/USD", timeframe="15 min"),
            description(1, pair="EUR/USD", timeframe="1 hour"),
        ]
    )
    assert len(switches) == 1
    assert switches[0].to_timeframe == "1 hour"


def test_the_first_reading_is_not_a_switch():
    assert find_switches([description(0, pair="EUR/USD")]) == []


def test_an_unknown_between_two_identical_readings_is_not_a_switch():
    # The model could not see it in that one frame; the user did not switch
    # away and back. Treating it as a switch would litter the document with
    # false seams.
    switches = find_switches(
        [
            description(0, pair="EUR/USD"),
            description(1, pair=UNKNOWN),
            description(2, pair="EUR/USD"),
        ]
    )
    assert switches == []


def test_a_run_of_unknowns_does_not_produce_switches():
    assert find_switches([description(i, pair=UNKNOWN) for i in range(5)]) == []


def test_several_switches_are_all_found():
    switches = find_switches(
        [
            description(0, pair="EUR/USD"),
            description(1, pair="GBP/USD"),
            description(2, pair="USD/JPY"),
        ]
    )
    assert [s.to_instrument for s in switches] == ["GBP/USD", "USD/JPY"]


def test_a_switch_label_names_what_was_moved_to():
    switches = find_switches(
        [
            description(0, pair="EUR/USD", timeframe="15 min"),
            description(1, pair="GBP/USD", timeframe="1 hour"),
        ]
    )
    assert switches[0].label == "GBP/USD · 1 hour"


# ── Segments ──────────────────────────────────────────────────────────────


def test_a_recording_with_no_switches_is_one_segment():
    segments = build_segments(
        [description(i, pair="EUR/USD", seconds=float(i * 60)) for i in range(5)],
        duration_seconds=300.0,
    )
    assert len(segments) == 1
    assert segments[0].start_seconds == 0.0
    assert segments[0].end_seconds == 300.0


def test_segments_split_at_switch_points():
    segments = build_segments(
        [
            description(0, pair="EUR/USD", seconds=0.0),
            description(1, pair="EUR/USD", seconds=60.0),
            description(2, pair="GBP/USD", seconds=120.0),
            description(3, pair="GBP/USD", seconds=180.0),
        ],
        duration_seconds=240.0,
    )
    assert len(segments) == 2
    assert segments[0].end_seconds == 120.0
    assert segments[1].instrument == "GBP/USD"


def test_very_short_segments_are_folded_into_their_neighbour():
    # A recording that flicks between instruments would otherwise produce
    # hundreds of one-line headings.
    segments = build_segments(
        [
            description(0, pair="EUR/USD", seconds=0.0),
            description(1, pair="GBP/USD", seconds=100.0),
            description(2, pair="EUR/USD", seconds=105.0),
            description(3, pair="GBP/USD", seconds=110.0),
        ],
        duration_seconds=300.0,
        min_segment_seconds=30.0,
    )
    assert len(segments) <= 2, [s.title for s in segments]


def test_segments_are_contiguous_and_cover_the_whole_recording():
    segments = build_segments(
        [
            description(0, pair="EUR/USD", seconds=0.0),
            description(1, pair="GBP/USD", seconds=120.0),
            description(2, pair="USD/JPY", seconds=240.0),
        ],
        duration_seconds=360.0,
    )
    assert segments[0].start_seconds == 0.0
    assert segments[-1].end_seconds == 360.0
    for earlier, later in pairwise(segments):
        assert earlier.end_seconds == later.start_seconds


def test_a_zero_length_recording_has_no_segments():
    assert build_segments([description(0)], duration_seconds=0.0) == []


def test_a_segment_knows_which_moments_it_contains():
    segments = build_segments([description(0, pair="EUR/USD", seconds=0.0)], duration_seconds=100.0)
    assert segments[0].contains(50.0) is True
    assert segments[0].contains(150.0) is False


def test_an_unlabelled_stretch_says_so():
    segments = build_segments([description(0)], duration_seconds=100.0)
    assert segments[0].title == "Unlabelled stretch"


# ── Determinism ───────────────────────────────────────────────────────────


def test_enrichment_is_deterministic():
    # Two runs of the same video must be comparable.
    frames = [
        description(0, pair="EUR/USD", seconds=0.0),
        description(1, pair="GBP/USD", seconds=120.0, confidence=Confidence.LOW),
    ]
    first = enrich(frames, 300.0)
    second = enrich(frames, 300.0)

    assert [s.as_dict() for s in first.segments] == [s.as_dict() for s in second.segments]
    assert [e.reasons for e in first.emphasis] == [e.reasons for e in second.emphasis]


def test_enrichment_needs_no_network_or_provider():
    # Everything here is a rule. Nothing to mock, nothing to reach.
    result = enrich([description(0, pair="EUR/USD")], 100.0)
    assert result.segments


# ── Assembly ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00"), (61, "00:01:01"), (3661, "01:01:01"), (7325, "02:02:05")],
)
def test_timestamps_render_as_hhmmss(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_transcript_and_descriptions_are_interleaved_by_time():
    """The organising principle. Grouping by source would destroy it."""
    transcript = [
        TranscriptSegment(0.0, 2.0, "first words"),
        TranscriptSegment(10.0, 12.0, "later words"),
    ]
    descriptions = [description(0, seconds=4.0), description(1, seconds=20.0)]

    entries = build_entries(transcript, descriptions)
    assert [e.seconds for e in entries] == [0.0, 4.0, 10.0, 20.0]


def test_a_heading_precedes_what_it_introduces():
    from app.pipeline.enrich import Enrichment, Segment

    enrichment = Enrichment(segments=[Segment(10.0, 60.0, "EUR/USD")])
    entries = build_entries([TranscriptSegment(10.0, 12.0, "words")], [], enrichment)
    assert entries[0].kind == "heading"


def test_silence_markers_appear_in_place():
    transcript = [
        TranscriptSegment(0.0, 2.0, "words"),
        TranscriptSegment(2.0, 30.0, "[nobody speaking · 28 seconds]", is_silence=True),
        TranscriptSegment(30.0, 32.0, "more words"),
    ]
    entries = build_entries(transcript, [])
    assert [e.kind for e in entries] == ["speech", "silence", "speech"]


def test_unreadable_fields_are_omitted_not_printed_as_unknown():
    # A column of "Unknown" is noise; the count tells the reader the frame was
    # seen and mostly unreadable.
    rendered = describe_frame(description(0, pair="EUR/USD"))
    assert "EUR/USD" in rendered
    assert "Unknown" not in rendered
    assert "could not be read" in rendered


def test_a_fully_readable_frame_prints_every_field():
    rendered = describe_frame(
        description(
            0,
            pair="EUR/USD",
            timeframe="15 min",
            action="entry marked",
            indicators_and_states="RSI 58",
            visible_text="1.0842",
            visual_description="a chart",
            setup_type="Breakout",
        )
    )
    for value in ("EUR/USD", "15 min", "entry marked", "RSI 58", "1.0842", "Breakout"):
        assert value in rendered
    assert "could not be read" not in rendered


def test_pictures_are_numbered_the_way_the_user_sees_them():
    # 1-based: internal index 0 is picture 1.
    assert "picture 1" in describe_frame(description(0))
    assert "picture 42" in describe_frame(description(41))


def test_emphasis_is_marked_on_the_frame():
    from app.pipeline.enrich import Emphasis

    rendered = describe_frame(description(0), Emphasis(0, 0.0, ["low confidence"]))
    assert "[low confidence]" in rendered


def test_the_assembled_header_summarises_the_video():
    content = assemble_video(
        display_name="capture_0914.mp4",
        duration_seconds=2530.0,
        transcript_segments=[TranscriptSegment(0.0, 2.0, "words")],
        descriptions=[description(0)],
        interval_ms=2000,
    )
    assert "capture_0914.mp4" in content
    assert "00:42:10" in content
    assert "Picture every     2 seconds" in content


def test_gaps_are_stated_in_the_header():
    content = assemble_video(
        display_name="clip.mp4",
        duration_seconds=100.0,
        transcript_segments=[],
        descriptions=[],
        gap_count=4,
    )
    assert "4 picture(s) have no description" in content
    assert "gaps.txt" in content


def test_a_video_with_no_descriptions_still_assembles():
    # The local-only default: transcript and pictures, no descriptions.
    content = assemble_video(
        display_name="clip.mp4",
        duration_seconds=10.0,
        transcript_segments=[TranscriptSegment(0.0, 2.0, "spoken words")],
        descriptions=[],
    )
    assert "spoken words" in content


def test_a_video_with_no_audio_still_assembles():
    content = assemble_video(
        display_name="silent.mp4",
        duration_seconds=10.0,
        transcript_segments=[],
        descriptions=[description(0, pair="EUR/USD")],
    )
    assert "EUR/USD" in content


# ── Master assembly ───────────────────────────────────────────────────────


def _source(sequence, name, seconds=100.0):
    return MasterSource(
        sequence=sequence,
        display_name=name,
        duration_seconds=seconds,
        assembled_text=f"content of {name}",
        job_video_id=f"v{sequence}",
        version=1,
    )


def test_videos_appear_in_the_confirmed_order():
    content = assemble_master("Session", [_source(1, "second.mp4"), _source(0, "first.mp4")])
    assert content.index("first.mp4") < content.index("second.mp4")


def test_order_is_never_inferred_from_the_name():
    # "alpha" sorts before "zulu" alphabetically, but the user put zulu first.
    content = assemble_master("Session", [_source(0, "zulu.mp4"), _source(1, "alpha.mp4")])
    assert content.index("zulu.mp4") < content.index("alpha.mp4")


def test_each_video_carries_a_strong_boundary_with_provenance():
    content = assemble_master("Session", [_source(0, "first.mp4")])
    assert '<video sequence="1"' in content
    assert 'source_video_id="v0"' in content
    assert 'processed_version="1"' in content
    assert "</video>" in content


def test_the_master_header_totals_the_job():
    content = assemble_master("Session", [_source(0, "a.mp4", 3600.0), _source(1, "b.mp4", 1800.0)])
    assert "Videos            2" in content
    assert "01:30:00" in content


def test_every_video_body_is_present():
    content = assemble_master(
        "Session", [_source(0, "a.mp4"), _source(1, "b.mp4"), _source(2, "c.mp4")]
    )
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        assert f"content of {name}" in content


def test_writing_assembled_produces_a_file(tmp_path):
    from app.pipeline.assemble import write_assembled

    path = write_assembled(tmp_path, "some assembled content\n")
    assert path.name == "assembled.txt"
    assert path.read_text(encoding="utf-8") == "some assembled content\n"


# ── The header counts pictures, not descriptions ──────────────────────────


def test_the_header_reports_the_pictures_that_exist(tmp_path):
    """Found by running a sample job with descriptions off: the document opened
    by announcing "Pictures 0" with twenty of them on disk, because the label
    said pictures and the number was descriptions."""
    content = assemble_video(
        display_name="clip.mp4",
        duration_seconds=60.0,
        transcript_segments=[],
        descriptions=[],
        interval_ms=3000,
        frame_count=20,
    )

    assert "Pictures          20" in content
    assert "Described         0" in content


def test_the_two_counts_are_reported_separately_when_they_differ(tmp_path):
    """They answer different questions and are routinely different — a job with
    gaps has more pictures than descriptions, and that is worth seeing."""
    described = [
        FrameDescription(index=i, timestamp_seconds=float(i), visual_description="a chart")
        for i in range(3)
    ]

    content = assemble_video(
        display_name="clip.mp4",
        duration_seconds=60.0,
        transcript_segments=[],
        descriptions=described,
        frame_count=20,
    )

    assert "Pictures          20" in content
    assert "Described         3" in content


def test_an_unknown_picture_count_is_not_invented(tmp_path):
    """No manifest means the number is genuinely unknown. Reporting the
    description count under a "Pictures" label is how this went wrong before."""
    content = assemble_video(
        display_name="clip.mp4",
        duration_seconds=60.0,
        transcript_segments=[],
        descriptions=[],
        frame_count=None,
    )

    assert "Pictures          " not in content
    assert "Described         0 picture(s)" in content
