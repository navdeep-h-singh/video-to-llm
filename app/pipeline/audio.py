"""Stage 2a — audio extraction and silence detection.

Silence is evidence, not absence. A three-minute gap where nobody speaks is a
meaningful part of the record, so it is detected, measured, and later written
into the transcript as an explicit marker rather than being closed up.

That is also why transcription works from the original timeline: if the
non-silent chunks were simply concatenated, every timestamp after the first gap
would be wrong, and a transcript with plausible-looking but wrong times is worse
than no transcript at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.artifacts import write_json
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text

logger = get_logger(__name__)

AUDIO_FILENAME = "audio.wav"
SILENCE_FILENAME = "silence_windows.json"

#: 16 kHz mono is what the speech model wants; resampling once here avoids
#: doing it per chunk later.
SAMPLE_RATE = 16_000

#: dBFS below which audio counts as silence. -35 dB is quiet enough to ignore
#: room tone and fan noise without swallowing a softly spoken sentence.
SILENCE_THRESHOLD_DB = -35.0

AUDIO_TIMEOUT_SECONDS = 3600

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class SilenceWindow:
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class SpeechSegment:
    """A stretch worth transcribing, with padding already applied."""

    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioError("ffmpeg was not found on your PATH.")
    return found


def extract_audio(source: Path, destination: Path) -> Path:
    """Pull the audio track out as 16 kHz mono WAV."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    args = [
        _ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=AUDIO_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise AudioError("Extracting the audio took too long and was stopped.") from error
    except OSError as error:
        raise AudioError(f"Could not run ffmpeg — {redacted_exception_text(error)}") from error

    if result.returncode != 0 or not destination.is_file():
        raise AudioError(f"Could not extract audio:\n{result.stderr[-1500:]}")

    return destination


def detect_silence(
    audio_path: Path,
    *,
    threshold_seconds: float = 3.0,
    threshold_db: float = SILENCE_THRESHOLD_DB,
) -> list[SilenceWindow]:
    """Find stretches of quiet longer than *threshold_seconds*."""
    args = [
        _ffmpeg(),
        "-loglevel",
        "info",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={threshold_db}dB:d={threshold_seconds}",
        "-f",
        "null",
        "-",
    ]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=AUDIO_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise AudioError(
            f"Could not look for quiet stretches — {redacted_exception_text(error)}"
        ) from error

    return parse_silence_output(result.stderr)


def parse_silence_output(stderr: str) -> list[SilenceWindow]:
    """Turn FFmpeg's silencedetect log into windows.

    Parsed defensively: a `silence_start` with no matching `silence_end` is
    normal when the file ends during a quiet stretch, and it must not be dropped
    or the final gap disappears from the transcript.
    """
    windows: list[SilenceWindow] = []
    pending_start: float | None = None

    for line in stderr.splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            pending_start = max(0.0, float(start_match.group(1)))
            continue

        end_match = _SILENCE_END.search(line)
        if end_match and pending_start is not None:
            end = float(end_match.group(1))
            if end > pending_start:
                windows.append(SilenceWindow(round(pending_start, 3), round(end, 3)))
            pending_start = None

    return windows


def close_trailing_silence(
    windows: list[SilenceWindow], stderr: str, duration_seconds: float
) -> list[SilenceWindow]:
    """Add the final window when the file ends mid-silence."""
    starts = [float(m) for m in _SILENCE_START.findall(stderr)]
    ends = [float(m) for m in _SILENCE_END.findall(stderr)]
    if len(starts) > len(ends) and starts:
        last_start = max(0.0, starts[-1])
        if duration_seconds > last_start:
            windows = [*windows, SilenceWindow(round(last_start, 3), round(duration_seconds, 3))]
    return windows


def speech_segments(
    silences: list[SilenceWindow],
    duration_seconds: float,
    *,
    padding_seconds: float = 0.25,
) -> list[SpeechSegment]:
    """Invert the silence windows into the stretches worth transcribing.

    Each segment is padded outward, because a speech model given a hard cut at
    the exact silence boundary reliably clips the first and last syllable. The
    padding is trimmed back against neighbours so segments never overlap.
    """
    if duration_seconds <= 0:
        return []

    ordered = sorted(silences, key=lambda w: w.start_seconds)
    segments: list[SpeechSegment] = []
    cursor = 0.0

    for window in ordered:
        if window.start_seconds > cursor:
            segments.append(SpeechSegment(cursor, min(window.start_seconds, duration_seconds)))
        cursor = max(cursor, window.end_seconds)

    if cursor < duration_seconds:
        segments.append(SpeechSegment(cursor, duration_seconds))

    padded: list[SpeechSegment] = []
    for index, segment in enumerate(segments):
        start = max(0.0, segment.start_seconds - padding_seconds)
        end = min(duration_seconds, segment.end_seconds + padding_seconds)

        # Do not let padding run into the previous segment.
        if padded and start < padded[-1].end_seconds:
            start = padded[-1].end_seconds
        # Or past the start of the next one.
        if index + 1 < len(segments):
            start = min(start, segments[index + 1].start_seconds)

        if end > start:
            padded.append(SpeechSegment(round(start, 3), round(end, 3)))

    return padded


def write_silence_windows(
    destination: Path, windows: list[SilenceWindow], *, threshold_seconds: float
) -> str:
    return write_json(
        destination,
        {
            "version": 1,
            "threshold_seconds": threshold_seconds,
            "threshold_db": SILENCE_THRESHOLD_DB,
            "count": len(windows),
            "total_silent_seconds": round(sum(w.duration_seconds for w in windows), 3),
            "windows": [
                {
                    "start_seconds": w.start_seconds,
                    "end_seconds": w.end_seconds,
                    "duration_seconds": round(w.duration_seconds, 3),
                }
                for w in windows
            ],
        },
    )
