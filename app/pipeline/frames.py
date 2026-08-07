"""Stage 1 — fixed-interval frame extraction.

Two sets of pictures come out of this stage, and keeping them apart matters:

*Clean frames* (``frames/``) are 1280x720 JPEGs, exactly as captured. These are
what the user reviews and what ends up in an export.

*Provider frames* (``frames_api/``) are smaller copies carrying a small ``IDX nn``
stamp in the top-left corner. Only these are ever sent to a description model.
The stamp is what lets a returned description be matched back to the right
moment — a model given twenty unlabelled pictures can and does answer about them
out of order, and without the stamp there is no way to detect that.

Sampling is a fixed interval only. There is no scene detection: a fixed grid
means frame *n* is always at a predictable timestamp, which is what makes the
frame map, the reruns, and the collection references reproducible.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.artifacts import atomic_write, write_json
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.pipeline.probe import VideoInfo

logger = get_logger(__name__)

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

#: Provider copies are deliberately smaller. They exist for alignment, not for
#: detail, and a smaller image costs fewer tokens on every request.
API_FRAME_WIDTH = 768

FRAMES_DIRNAME = "frames"
API_FRAMES_DIRNAME = "frames_api"
MANIFEST_FILENAME = "frames_manifest.json"

EXTRACT_TIMEOUT_SECONDS = 3600


class FrameExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrameRecord:
    index: int
    timestamp_seconds: float
    clean_filename: str
    api_filename: str
    batch_id: str
    batch_index: int

    @property
    def timestamp_label(self) -> str:
        total = int(self.timestamp_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class ExtractionResult:
    frames: list[FrameRecord]
    frames_dir: Path
    api_frames_dir: Path
    manifest_path: Path
    interval_ms: int


def timestamp_token(seconds: float) -> str:
    """Format a timestamp for a filename: ``092000`` for 09:20:00.

    Colons are illegal in Windows filenames and awkward everywhere else, so the
    separators are dropped rather than substituted.
    """
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}{minutes:02d}{secs:02d}"


def frame_filename(index: int, seconds: float) -> str:
    """``000047_t092000.jpg`` — sortable by index, readable as a time."""
    return f"{index:06d}_t{timestamp_token(seconds)}.jpg"


def plan_frames(
    duration_seconds: float, interval_ms: int, *, batch_size: int = 20
) -> list[FrameRecord]:
    """Compute every frame's index, timestamp, filenames, and batch placement.

    Pure and deterministic: the same duration and interval always produce the
    same plan. That is what lets a rerun target an exact frame range, and what
    lets a collection reference a frame by index years later.
    """
    if interval_ms <= 0:
        raise ValueError("frame interval must be positive")
    if batch_size <= 0:
        raise ValueError("batch size must be positive")

    interval_seconds = interval_ms / 1000.0
    records: list[FrameRecord] = []

    index = 0
    timestamp = 0.0
    while timestamp < duration_seconds:
        batch_index = index // batch_size
        records.append(
            FrameRecord(
                index=index,
                timestamp_seconds=round(timestamp, 3),
                clean_filename=frame_filename(index, timestamp),
                api_filename=frame_filename(index, timestamp),
                batch_id=f"batch_{batch_index:04d}",
                batch_index=batch_index,
            )
        )
        index += 1
        # Multiplied from the index rather than accumulated, so floating-point
        # error cannot drift across a long video.
        timestamp = round(index * interval_seconds, 6)

    return records


def expected_frame_count(duration_seconds: float, interval_ms: int) -> int:
    if duration_seconds <= 0 or interval_ms <= 0:
        return 0
    import math

    return max(1, math.ceil(duration_seconds / (interval_ms / 1000.0)))


def _ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise FrameExtractionError("ffmpeg was not found on your PATH.")
    return found


def _run_ffmpeg(args: list[str], *, what: str) -> None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise FrameExtractionError(f"{what} took too long and was stopped.") from error
    except OSError as error:
        raise FrameExtractionError(
            f"{what} could not be started — {redacted_exception_text(error)}"
        ) from error

    if result.returncode != 0:
        raise FrameExtractionError(f"{what} failed:\n{result.stderr[-1500:]}")


def extract_frames(
    info: VideoInfo,
    output_dir: Path,
    *,
    interval_ms: int,
    batch_size: int = 20,
    make_api_copies: bool = True,
) -> ExtractionResult:
    """Extract clean frames and their provider copies, then write the manifest.

    The manifest is written last and atomically, so its presence means the whole
    stage finished. A crash partway leaves loose JPEGs and no manifest, which
    reconciliation reads correctly as unfinished work.
    """
    output_dir = Path(output_dir)
    frames_dir = output_dir / FRAMES_DIRNAME
    api_dir = output_dir / API_FRAMES_DIRNAME
    frames_dir.mkdir(parents=True, exist_ok=True)
    if make_api_copies:
        api_dir.mkdir(parents=True, exist_ok=True)

    plan = plan_frames(info.duration_seconds, interval_ms, batch_size=batch_size)
    if not plan:
        raise FrameExtractionError(
            f"{info.path.name}: the video is too short to take any pictures from."
        )

    fps_expression = f"1/{interval_ms / 1000.0}"
    ffmpeg = _ffmpeg_path()

    logger.info(
        "Extracting %d frames from %s every %d ms",
        len(plan),
        info.path.name,
        interval_ms,
    )

    # `-vsync vfr` with an fps filter gives one frame per interval without
    # duplicating frames when the source rate is lower than the sample rate.
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(info.path),
            "-vf",
            f"fps={fps_expression},scale={FRAME_WIDTH}:{FRAME_HEIGHT}"
            f":force_original_aspect_ratio=decrease,"
            f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-vsync",
            "vfr",
            "-q:v",
            "3",
            str(frames_dir / "%06d.jpg"),
        ],
        what="Taking pictures from the video",
    )

    produced = sorted(frames_dir.glob("*.jpg"))
    if not produced:
        raise FrameExtractionError(f"{info.path.name}: no pictures could be taken from this video.")

    # FFmpeg numbers from 1 and stops when the source runs out, which can differ
    # from the plan by one frame at the very end. Trim the plan to what actually
    # exists rather than inventing records for frames that were never written.
    records = plan[: len(produced)]
    for record, temporary in zip(records, produced, strict=False):
        temporary.rename(frames_dir / record.clean_filename)

    if make_api_copies:
        _write_api_copies(records, frames_dir, api_dir)

    manifest_path = output_dir / MANIFEST_FILENAME
    write_json(
        manifest_path,
        {
            "version": 1,
            "source_filename": info.path.name,
            "duration_seconds": info.duration_seconds,
            "frame_interval_ms": interval_ms,
            "frame_count": len(records),
            "batch_size": batch_size,
            "clean_dimensions": [FRAME_WIDTH, FRAME_HEIGHT],
            "api_dimensions": [API_FRAME_WIDTH, None],
            "extraction_source": "ffmpeg fixed interval",
            "frames": [
                {
                    "index": r.index,
                    "timestamp_seconds": r.timestamp_seconds,
                    "timestamp": r.timestamp_label,
                    "clean_filename": r.clean_filename,
                    "api_filename": r.api_filename,
                    "batch_id": r.batch_id,
                    "batch_index": r.batch_index,
                }
                for r in records
            ],
        },
    )

    logger.info("Extracted %d frames to %s", len(records), frames_dir)
    return ExtractionResult(
        frames=records,
        frames_dir=frames_dir,
        api_frames_dir=api_dir,
        manifest_path=manifest_path,
        interval_ms=interval_ms,
    )


def _write_api_copies(records: list[FrameRecord], frames_dir: Path, api_dir: Path) -> None:
    """Write the numbered provider copies.

    The number is drawn in the top-left with a contrasting background box, so it
    survives on charts, dark screen recordings, and light documents alike. Its
    only job is to make misalignment detectable.
    """
    from PIL import Image, ImageDraw
    from PIL.Image import Resampling

    for record in records:
        source = frames_dir / record.clean_filename
        if not source.is_file():
            continue

        with Image.open(source) as opened:
            rgb = opened.convert("RGB")
            ratio = API_FRAME_WIDTH / rgb.width
            resized = rgb.resize(
                (API_FRAME_WIDTH, max(1, int(rgb.height * ratio))), Resampling.LANCZOS
            )

            draw = ImageDraw.Draw(resized)
            label = f"IDX {record.index + 1:02d}"
            box = draw.textbbox((0, 0), label)
            padding = 6
            draw.rectangle([0, 0, box[2] + padding * 2, box[3] + padding * 2], fill=(0, 0, 0))
            draw.text((padding, padding), label, fill=(255, 255, 255))

            with atomic_write(api_dir / record.api_filename) as handle:
                resized.save(handle, format="JPEG", quality=85)
