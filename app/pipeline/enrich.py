"""Stage 4 — deterministic local enrichment.

Everything here is a rule, not a model. No external text service is called and
none is required: enrichment must work on a machine with no network and no
provider configured, and it must produce the same output every time so that two
runs of the same video can be compared meaningfully.

Three things are derived:

**Emphasis** — moments the reviewer should look at first. Low confidence,
unreadable values, and explicit actions are all worth surfacing; a wall of
uniform text is not reviewable.

**Timeframe switches** — points where the described timeframe or instrument
changes. These are the natural seams in a recording, and they are what pack
boundaries and segment headings are built from later.

**Time-window segments** — the recording divided into labelled stretches, so a
two-hour document has structure rather than being one undifferentiated block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.providers.base import UNKNOWN, Confidence

logger = get_logger(__name__)

#: A segment shorter than this is folded into its neighbour. Without a floor, a
#: recording that switches instrument every few seconds produces hundreds of
#: one-line headings, which is worse than none.
MIN_SEGMENT_SECONDS = 30.0

#: Emphasis reasons, in the order they are reported.
EMPHASIS_LOW_CONFIDENCE = "low confidence"
EMPHASIS_UNREADABLE = "values could not be read"
EMPHASIS_ACTION = "something happens here"
EMPHASIS_LONG_SILENCE = "a long quiet stretch"

LONG_SILENCE_SECONDS = 30.0


@dataclass
class Emphasis:
    index: int
    timestamp_seconds: float
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join(self.reasons)


@dataclass
class Segment:
    """A labelled stretch of the recording."""

    start_seconds: float
    end_seconds: float
    title: str
    instrument: str = UNKNOWN
    timeframe: str = UNKNOWN

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def contains(self, seconds: float) -> bool:
        return self.start_seconds <= seconds < self.end_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "title": self.title,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
        }


@dataclass
class Switch:
    """A point where the instrument or timeframe changed."""

    index: int
    timestamp_seconds: float
    from_instrument: str
    to_instrument: str
    from_timeframe: str
    to_timeframe: str

    @property
    def describes_instrument_change(self) -> bool:
        return self.from_instrument != self.to_instrument

    @property
    def label(self) -> str:
        if self.describes_instrument_change and self.from_timeframe != self.to_timeframe:
            return f"{self.to_instrument} · {self.to_timeframe}"
        if self.describes_instrument_change:
            return str(self.to_instrument)
        return str(self.to_timeframe)


@dataclass
class Enrichment:
    emphasis: list[Emphasis] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)

    def emphasis_for(self, index: int) -> Emphasis | None:
        return next((e for e in self.emphasis if e.index == index), None)

    def segment_for(self, seconds: float) -> Segment | None:
        return next((s for s in self.segments if s.contains(seconds)), None)


def _readable(value: Any) -> bool:
    return bool(value) and str(value) != UNKNOWN


def find_emphasis(descriptions: list[Any], silences: list[Any] | None = None) -> list[Emphasis]:
    """Mark the moments a reviewer should look at first."""
    found: list[Emphasis] = []

    for description in descriptions:
        reasons: list[str] = []

        if getattr(description, "confidence", None) == Confidence.LOW:
            reasons.append(EMPHASIS_LOW_CONFIDENCE)

        # Three or more unreadable fields means the frame told us very little,
        # whatever confidence the model claimed.
        if getattr(description, "unknown_field_count", 0) >= 3:
            reasons.append(EMPHASIS_UNREADABLE)

        action = getattr(description, "exact_action", UNKNOWN)
        if _readable(action):
            reasons.append(EMPHASIS_ACTION)

        if reasons:
            found.append(
                Emphasis(
                    index=description.index,
                    timestamp_seconds=getattr(description, "timestamp_seconds", 0.0) or 0.0,
                    reasons=reasons,
                )
            )

    for window in silences or []:
        if window.duration_seconds >= LONG_SILENCE_SECONDS:
            found.append(
                Emphasis(
                    index=-1,
                    timestamp_seconds=window.start_seconds,
                    reasons=[EMPHASIS_LONG_SILENCE],
                )
            )

    found.sort(key=lambda e: e.timestamp_seconds)
    return found


def find_switches(descriptions: list[Any]) -> list[Switch]:
    """Find the points where the instrument or timeframe changed.

    Only readable values count as a change. An `Unknown` between two readings of
    the same instrument means the model could not see it in that one frame, not
    that the user switched away and back — treating it as a switch would litter
    the document with false seams.
    """
    switches: list[Switch] = []
    last_instrument = UNKNOWN
    last_timeframe = UNKNOWN

    for description in descriptions:
        instrument = getattr(description, "currency_pair", UNKNOWN)
        timeframe = getattr(description, "timeframe", UNKNOWN)

        instrument_changed = (
            _readable(instrument) and _readable(last_instrument) and instrument != last_instrument
        )
        timeframe_changed = (
            _readable(timeframe) and _readable(last_timeframe) and timeframe != last_timeframe
        )

        # The first readable reading establishes a baseline; it is not a switch.
        first_reading = not _readable(last_instrument) and _readable(instrument)

        if instrument_changed or timeframe_changed:
            switches.append(
                Switch(
                    index=description.index,
                    timestamp_seconds=getattr(description, "timestamp_seconds", 0.0) or 0.0,
                    from_instrument=last_instrument,
                    to_instrument=instrument if _readable(instrument) else last_instrument,
                    from_timeframe=last_timeframe,
                    to_timeframe=timeframe if _readable(timeframe) else last_timeframe,
                )
            )

        if _readable(instrument):
            last_instrument = instrument
        if _readable(timeframe):
            last_timeframe = timeframe
        if first_reading:
            continue

    return switches


def build_segments(
    descriptions: list[Any],
    duration_seconds: float,
    *,
    min_segment_seconds: float = MIN_SEGMENT_SECONDS,
) -> list[Segment]:
    """Divide the recording into labelled stretches at the switch points."""
    if duration_seconds <= 0:
        return []

    switches = find_switches(descriptions)

    first_instrument = UNKNOWN
    first_timeframe = UNKNOWN
    for description in descriptions:
        if not _readable(first_instrument):
            candidate = getattr(description, "currency_pair", UNKNOWN)
            if _readable(candidate):
                first_instrument = candidate
        if not _readable(first_timeframe):
            candidate = getattr(description, "timeframe", UNKNOWN)
            if _readable(candidate):
                first_timeframe = candidate
        if _readable(first_instrument) and _readable(first_timeframe):
            break

    boundaries: list[tuple[float, str, str]] = [(0.0, first_instrument, first_timeframe)]
    for switch in switches:
        boundaries.append((switch.timestamp_seconds, switch.to_instrument, switch.to_timeframe))

    segments: list[Segment] = []
    for position, (start, instrument, timeframe) in enumerate(boundaries):
        end = boundaries[position + 1][0] if position + 1 < len(boundaries) else duration_seconds
        if end <= start:
            continue
        segments.append(
            Segment(
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                title=_segment_title(instrument, timeframe),
                instrument=instrument,
                timeframe=timeframe,
            )
        )

    return _merge_short_segments(segments, min_segment_seconds)


def _segment_title(instrument: str, timeframe: str) -> str:
    if _readable(instrument) and _readable(timeframe):
        return f"{instrument} · {timeframe}"
    if _readable(instrument):
        return str(instrument)
    if _readable(timeframe):
        return str(timeframe)
    return "Unlabelled stretch"


def _merge_short_segments(segments: list[Segment], minimum: float) -> list[Segment]:
    """Fold segments shorter than *minimum* into the one before them.

    A recording that flicks between instruments every few seconds would
    otherwise produce hundreds of one-line headings — structure that obscures
    rather than reveals.
    """
    if not segments:
        return []

    merged: list[Segment] = [segments[0]]
    for segment in segments[1:]:
        previous = merged[-1]
        if segment.duration_seconds < minimum:
            merged[-1] = Segment(
                start_seconds=previous.start_seconds,
                end_seconds=segment.end_seconds,
                title=previous.title,
                instrument=previous.instrument,
                timeframe=previous.timeframe,
            )
        else:
            merged.append(segment)

    # The first segment can also be too short once everything else settled.
    if len(merged) > 1 and merged[0].duration_seconds < minimum:
        second = merged[1]
        merged[1] = Segment(
            start_seconds=merged[0].start_seconds,
            end_seconds=second.end_seconds,
            title=second.title,
            instrument=second.instrument,
            timeframe=second.timeframe,
        )
        merged.pop(0)

    return merged


def enrich(
    descriptions: list[Any],
    duration_seconds: float,
    silences: list[Any] | None = None,
) -> Enrichment:
    """Run every deterministic enrichment over one video's descriptions."""
    return Enrichment(
        emphasis=find_emphasis(descriptions, silences),
        switches=find_switches(descriptions),
        segments=build_segments(descriptions, duration_seconds),
    )
