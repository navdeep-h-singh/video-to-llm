"""Reading what a video actually is, via ffprobe.

Every subprocess call in this package passes an argument *array*, never a shell
string. Filenames arrive from the user's disk and routinely contain spaces,
quotes, and semicolons; shell interpolation would turn an ordinary filename into
command execution.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text

logger = get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 60

#: The containers the specification supports. Checked by probed format as well as
#: by extension, so a mislabelled file is caught.
SUPPORTED_EXTENSIONS = frozenset({".mp4", ".webm", ".mov"})


class ProbeError(RuntimeError):
    """Raised when a file cannot be read as video."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration_seconds: float
    width: int
    height: int
    container: str
    video_codec: str
    has_audio: bool
    audio_codec: str | None
    size_bytes: int

    @property
    def duration_label(self) -> str:
        total = round(self.duration_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ffprobe_path() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise ProbeError("ffprobe was not found on your PATH. Install FFmpeg and try again.")
    return found


def probe(path: Path) -> VideoInfo:
    """Read a video's shape. Raises :class:`ProbeError` if it is not readable."""
    path = Path(path)
    if not path.exists():
        raise ProbeError(f"{path.name}: the file does not exist.")
    if not path.is_file():
        raise ProbeError(f"{path.name}: that is a folder, not a video file.")

    args = [
        ffprobe_path(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeError(f"{path.name}: reading the file timed out.") from error
    except OSError as error:
        raise ProbeError(
            f"{path.name}: could not run ffprobe — {redacted_exception_text(error)}"
        ) from error

    if result.returncode != 0:
        raise ProbeError(
            f"{path.name}: this does not look like a video file that can be read. "
            "It may be corrupt, incomplete, or in an unsupported format."
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeError(f"{path.name}: ffprobe returned output that could not be read.") from error

    streams = payload.get("streams", [])
    fmt = payload.get("format", {})

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        raise ProbeError(f"{path.name}: there is no video track in this file.")

    video = video_streams[0]
    duration = _resolve_duration(fmt, video)
    if duration <= 0:
        raise ProbeError(
            f"{path.name}: the length of this file could not be determined. "
            "It may still be being written, or it may be truncated."
        )

    return VideoInfo(
        path=path,
        duration_seconds=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        container=str(fmt.get("format_name", "")),
        video_codec=str(video.get("codec_name", "")),
        has_audio=bool(audio_streams),
        audio_codec=str(audio_streams[0].get("codec_name")) if audio_streams else None,
        size_bytes=int(fmt.get("size") or path.stat().st_size),
    )


def _resolve_duration(fmt: dict, video_stream: dict) -> float:
    """Prefer the container duration, fall back to the video stream's.

    Some `.mov` files written by screen recorders carry no container duration,
    and WebM from certain encoders reports it only on the stream. Falling back
    matters: without a duration we cannot compute frame counts, and refusing the
    file outright would reject perfectly good recordings.
    """
    for candidate in (fmt.get("duration"), video_stream.get("duration")):
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def is_supported_extension(path: Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
