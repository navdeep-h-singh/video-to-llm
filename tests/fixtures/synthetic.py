"""Synthetic media generation for tests.

No test may use personal media (spec §10, §11), so every video the suite touches
is generated here from FFmpeg's own sources: colour patterns for video, tones and
silence for audio. Files are tiny — a few seconds at a low resolution — because
the tests care about frame *counts*, *timestamps*, and *mapping*, not about
picture quality.

Generated files land in ``tests/fixtures/generated/``, which is git-ignored.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFMPEG_TIMEOUT_SECONDS = 120


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@dataclass(frozen=True)
class SyntheticVideo:
    path: Path
    duration_seconds: float
    width: int
    height: int
    has_audio: bool
    #: Windows of deliberate silence, as (start, end) in seconds.
    silence_windows: tuple[tuple[float, float], ...] = ()


def _codecs_for(destination: Path) -> tuple[str, str]:
    """Pick a codec pair the container can actually hold."""
    if destination.suffix.lower() == ".webm":
        # VP8 rather than VP9: an order of magnitude faster to encode, and these
        # fixtures are a few seconds of colour bars. Opus rather than Vorbis
        # because libvorbis is absent from several common FFmpeg builds, and
        # these fixtures have to generate on whatever CI provides.
        return "libvpx", "libopus"
    return "libx264", "aac"


def _video_args(destination: Path) -> list[str]:
    codec = _codecs_for(destination)[0]
    args = ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if codec == "libx264":
        args += ["-preset", "ultrafast"]
    return args


def _run(args: list[str]) -> None:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")


def make_video(
    destination: Path,
    *,
    duration_seconds: float = 6.0,
    width: int = 320,
    height: int = 180,
    fps: int = 10,
    with_audio: bool = True,
    container: str | None = None,
) -> SyntheticVideo:
    """Generate a small video with a continuous tone, or no audio at all.

    The picture is FFmpeg's `testsrc2`, which stamps a running frame counter into
    the image — useful when a failure needs eyeballing, because the frame that
    was extracted is identifiable from the picture itself.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_seconds}",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_seconds}"]

    # WebM only accepts VP8/VP9/AV1 video and Vorbis/Opus audio, so the codec
    # pair has to follow the container rather than being fixed.
    args += _video_args(destination)
    if with_audio:
        args += ["-c:a", _codecs_for(destination)[1], "-shortest"]
    else:
        args += ["-an"]

    args += ["-t", str(duration_seconds), str(destination)]
    _run(args)

    return SyntheticVideo(
        path=destination,
        duration_seconds=duration_seconds,
        width=width,
        height=height,
        has_audio=with_audio,
    )


def make_video_with_silence(
    destination: Path,
    *,
    speech_segments: tuple[tuple[float, float], ...] = ((0.0, 2.0), (5.0, 7.0)),
    duration_seconds: float = 9.0,
    width: int = 320,
    height: int = 180,
    fps: int = 10,
) -> SyntheticVideo:
    """Generate a video whose audio alternates between tone and true silence.

    Used to prove the transcript preserves the original timeline: a stage that
    concatenated only the non-silent chunks would shift every later timestamp
    earlier, and the resulting transcript would be quietly, uselessly wrong.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Build the audio as a tone gated to the speech windows. Everything outside
    # them is digital silence, which is what the silence detector must find.
    between = "+".join(f"between(t,{start},{end})" for start, end in speech_segments)
    audio_filter = (
        f"sine=frequency=440:duration={duration_seconds},"
        f"volume=enable='{between}':volume=1,"
        f"volume=enable='not({between})':volume=0"
    )

    _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_seconds}",
            "-f",
            "lavfi",
            "-i",
            audio_filter,
            *_video_args(destination),
            "-c:a",
            _codecs_for(destination)[1],
            "-shortest",
            "-t",
            str(duration_seconds),
            str(destination),
        ]
    )

    silences: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in speech_segments:
        if start > cursor:
            silences.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_seconds:
        silences.append((cursor, duration_seconds))

    return SyntheticVideo(
        path=destination,
        duration_seconds=duration_seconds,
        width=width,
        height=height,
        has_audio=True,
        silence_windows=tuple(silences),
    )


def make_corrupt_file(destination: Path) -> Path:
    """A file with a video extension that is not a video.

    Preflight has to reject this cleanly rather than failing somewhere deep in
    frame extraction.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"this is definitely not an mp4" * 40)
    return destination
