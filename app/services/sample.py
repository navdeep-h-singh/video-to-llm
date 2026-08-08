"""A sample clip, generated on this computer.

A fresh install shows an empty dashboard and a readiness checklist. There is no
way to see what the product produces without supplying your own video and
waiting, which makes the first minute of the product the least convincing one.

The clip is **generated, never shipped**. The repository has to stay publishable
with no personal media and no large binaries, and a bundled "representative"
screen recording would need a licence story that a demo does not justify. So
FFmpeg draws one, here, from nothing: a chart that moves, and a tone with
deliberate gaps so the transcript has silences to mark.

It is labelled as generated test footage wherever it appears. Presenting it as a
real recording would be exactly the placeholder-data problem the rest of the
interface is careful to avoid — worse, in fact, because it would be a claim
about where the data came from.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

SAMPLES_DIRNAME = "samples"
SAMPLE_FILENAME = "sample_chart_clip.mp4"

#: Long enough to produce a real timeline with several silences, short enough
#: that the whole run finishes while someone is still looking at it.
SAMPLE_DURATION_SECONDS = 60.0
SAMPLE_WIDTH = 960
SAMPLE_HEIGHT = 540
SAMPLE_FPS = 10

#: Generation is bounded so a wedged FFmpeg cannot hang the interface.
FFMPEG_TIMEOUT_SECONDS = 180

#: Where the tone plays. Everything outside these is true digital silence, which
#: is what gives the transcript silence markers to find — one of the details
#: that distinguishes this from a naive transcript, and therefore worth showing.
SPEECH_WINDOWS: tuple[tuple[float, float], ...] = (
    (0.0, 9.0),
    (13.0, 24.0),
    (29.0, 41.0),
    (46.0, 58.0),
)

#: The design's vermilion and its lighter partner.
BAR_COLOURS = ("0xec3013", "0xe15b47")
BACKGROUND = "0x14120f"


@dataclass(frozen=True)
class SampleClip:
    path: Path
    duration_seconds: float
    #: "chart" when the animated chart was drawn, "plain" for the fallback.
    kind: str
    detail: str

    @property
    def display_name(self) -> str:
        return self.path.name


class SampleError(RuntimeError):
    """Raised when no sample could be produced at all."""


def sample_path(output_root: Path) -> Path:
    return Path(output_root) / SAMPLES_DIRNAME / SAMPLE_FILENAME


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _audio_filter(duration: float) -> str:
    """A tone that plays inside the speech windows and is silent outside them."""
    inside = "+".join(f"between(t,{start},{end})" for start, end in SPEECH_WINDOWS)
    return (
        f"sine=frequency=320:duration={duration},"
        f"volume=enable='{inside}':volume=0.6,"
        f"volume=enable='not({inside})':volume=0"
    )


#: The chart is drawn this many screens wide and then scrolled across, which is
#: what gives every frame a different picture.
CANVAS_MULTIPLIER = 3


def _bar_height(index: int, height: int) -> int:
    """One bar's height, worked out here rather than in a filter expression.

    Computed in Python and emitted as a literal. The obvious approach — a
    time-varying expression in ``drawbox`` — does not work, and fails silently:
    in ``drawbox`` the variable ``t`` is the box *thickness*, not the timestamp,
    so ``sin(t*0.6)`` is a constant and the whole chart comes out as a still
    image. That produced a sixty-second clip whose twenty sampled frames were
    two distinct pictures, and nothing anywhere reported a problem.
    """
    from math import sin

    mix = abs(sin(index * 0.5)) * 0.62 + abs(sin(index * 0.9 + 1.3)) * 0.38
    return int(mix * height * 0.42) + int(height * 0.05)


def _chart_filter(width: int, height: int) -> str:
    """A scrolling bar chart: a wide static drawing, moved past the window.

    Deliberately not ``testsrc2``. Colour bars are recognisably a test pattern
    and give a description model nothing to describe, so a demo built on them
    shows the pipeline running and the output saying nothing.

    The movement comes from ``crop``, whose ``t`` really is the timestamp. The
    chart is drawn once across a canvas several screens wide and the window
    travels along it, which is both cheap and what a live chart actually looks
    like.
    """
    canvas = width * CANVAS_MULTIPLIER
    baseline = int(height * 0.78)
    slot = max(24, int(width * 0.055))
    bar_width = max(8, int(slot * 0.62))
    bars = canvas // slot

    parts = [f"drawgrid=w={slot}:h={int(height / 11)}:t=1:color=0xffffff@0.07"]

    for index in range(bars):
        magnitude = _bar_height(index, height)
        parts.append(
            f"drawbox=x={index * slot}:y={baseline - magnitude}:"
            f"w={bar_width}:h={magnitude}:"
            f"color={BAR_COLOURS[index % len(BAR_COLOURS)]}@0.85:t=fill"
        )

    # Travel the whole extra width exactly once over the clip, so nothing
    # repeats and the last frame is as different from the first as possible.
    travel = canvas - width
    parts.append(f"crop={width}:{height}:x='min(t/{SAMPLE_DURATION_SECONDS},1)*{travel}':y=0")

    # Drawn after the crop so they stay put on screen while the chart moves.
    parts.append(f"drawbox=x=0:y={baseline}:w={width}:h=2:color=0xffffff@0.35:t=fill")
    parts.append(f"drawbox=x={width // 2}:y=0:w=2:h={height}:color=0xffffff@0.5:t=fill")
    return ",".join(parts)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_SECONDS,
        check=False,
    )


def _encode(destination: Path, video_input: list[str], video_filter: str | None) -> None:
    duration = SAMPLE_DURATION_SECONDS
    args = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        *video_input,
        "-f",
        "lavfi",
        "-i",
        _audio_filter(duration),
    ]
    if video_filter:
        args += ["-vf", video_filter]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        "-t",
        str(duration),
        str(destination),
    ]

    result = _run(args)
    if result.returncode != 0:
        raise SampleError(result.stderr[-1500:] or "ffmpeg gave no reason")


def generate_sample(output_root: Path, *, force: bool = False) -> SampleClip:
    """Draw the sample clip, reusing one that already exists.

    Two attempts, in order of how much they are worth showing. The fallback is
    not a nicety: ``drawbox`` expressions and the filters behind them vary
    between FFmpeg builds, and a first-run button that fails on somebody's
    machine is worse than one that shows them a plainer clip.
    """
    if not ffmpeg_available():
        raise SampleError(
            "FFmpeg is not installed, so a sample cannot be made. "
            "The readiness check explains how to install it."
        )

    destination = sample_path(output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.is_file() and destination.stat().st_size > 0 and not force:
        return SampleClip(
            path=destination,
            duration_seconds=SAMPLE_DURATION_SECONDS,
            kind="chart",
            detail="A sample already generated on this computer.",
        )

    source = [
        "-f",
        "lavfi",
        "-i",
        # Wide, because the chart is drawn once and scrolled past the window.
        f"color=c={BACKGROUND}:s={SAMPLE_WIDTH * CANVAS_MULTIPLIER}x{SAMPLE_HEIGHT}"
        f":r={SAMPLE_FPS}:d={SAMPLE_DURATION_SECONDS}",
    ]

    try:
        _encode(destination, source, _chart_filter(SAMPLE_WIDTH, SAMPLE_HEIGHT))
        return SampleClip(
            path=destination,
            duration_seconds=SAMPLE_DURATION_SECONDS,
            kind="chart",
            detail=(
                "Generated on this computer by FFmpeg: a moving chart with a tone "
                "that stops and starts. It is test footage, not a recording of "
                "anything."
            ),
        )
    except (SampleError, subprocess.SubprocessError, OSError) as error:
        logger.warning("Could not draw the chart sample, falling back: %s", error)

    try:
        _encode(
            destination,
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={SAMPLE_WIDTH}x{SAMPLE_HEIGHT}"
                f":rate={SAMPLE_FPS}:duration={SAMPLE_DURATION_SECONDS}",
            ],
            None,
        )
    except (SampleError, subprocess.SubprocessError, OSError) as error:
        raise SampleError(f"A sample clip could not be made on this computer: {error}") from error

    return SampleClip(
        path=destination,
        duration_seconds=SAMPLE_DURATION_SECONDS,
        kind="plain",
        detail=(
            "Generated on this computer by FFmpeg: a standard test pattern with a "
            "tone that stops and starts. The richer sample needs filters this "
            "FFmpeg build does not have."
        ),
    )
