"""Stage 2b — local transcription.

Runs entirely on this computer. The backend resolver may prefer an accelerator,
but CPU is mandatory on every platform and is always the fallback: a machine
without Metal, CUDA, or Vulkan must still produce a transcript, and a machine
that *claims* an accelerator which then fails must fall back rather than fail
the job.

Timestamps are remapped onto the original video timeline. Each speech segment is
transcribed in isolation, so the model reports times relative to that segment;
adding the segment's own offset puts them back where they belong. Skipping that
step is the single most damaging thing this stage could get wrong — every
timestamp after the first silence would be early, and a transcript with
plausible but wrong times is worse than none.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.core.artifacts import write_json
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.pipeline.audio import SilenceWindow, SpeechSegment

logger = get_logger(__name__)

TRANSCRIPT_FILENAME = "transcript.json"
TRANSCRIPT_TEXT_FILENAME = "transcript.txt"
RAW_TRANSCRIPT_FILENAME = "transcript_raw.json"

VALID_BACKENDS = ("auto", "cpu", "metal", "cuda", "vulkan")


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptSegment:
    """One utterance, on the original video timeline."""

    start_seconds: float
    end_seconds: float
    text: str
    is_silence: bool = False

    @property
    def timestamp_label(self) -> str:
        total = int(self.start_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass
class TranscriptionProvenance:
    requested_backend: str
    resolved_backend: str
    fell_back: bool
    fallback_reason: str = ""
    model: str = ""
    compute_type: str = ""
    device: str = ""
    language: str = ""
    runtime_seconds: float = 0.0
    segment_count: int = 0
    silence_marker_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_backend": self.requested_backend,
            "resolved_backend": self.resolved_backend,
            "fell_back": self.fell_back,
            "fallback_reason": self.fallback_reason,
            "model": self.model,
            "compute_type": self.compute_type,
            "device": self.device,
            "language": self.language,
            "runtime_seconds": round(self.runtime_seconds, 3),
            "segment_count": self.segment_count,
            "silence_marker_count": self.silence_marker_count,
        }


@dataclass
class TranscriptionResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    provenance: TranscriptionProvenance | None = None

    @property
    def spoken_segments(self) -> list[TranscriptSegment]:
        return [s for s in self.segments if not s.is_silence]

    @property
    def text(self) -> str:
        return "\n".join(f"{s.timestamp_label}  {s.text}" for s in self.segments)


# ── Backend resolution ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedBackend:
    name: str
    device: str
    compute_type: str
    fell_back: bool
    reason: str = ""


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_backend(requested: str = "auto") -> ResolvedBackend:
    """Choose a compute backend, falling back to CPU whenever unsure.

    CPU is never a failure state here — it is the documented, supported, and on
    most machines entirely adequate path. The fallback is recorded in provenance
    so the user can see *why* a run used the CPU rather than guessing.
    """
    requested = (requested or "auto").lower()
    if requested not in VALID_BACKENDS:
        return ResolvedBackend(
            "cpu", "cpu", "int8", True, f"unknown backend {requested!r}; used the processor"
        )

    cpu = ResolvedBackend("cpu", "cpu", "int8", False)

    if requested == "cpu":
        return cpu

    if requested in {"metal", "cuda", "vulkan"}:
        if requested == "cuda":
            if _cuda_available():
                return ResolvedBackend("cuda", "cuda", "float16", False)
            return ResolvedBackend(
                "cpu",
                "cpu",
                "int8",
                True,
                "no CUDA device was available, so the processor was used instead",
            )
        # CTranslate2 has no Metal or Vulkan path today. Saying so plainly beats
        # letting the user believe an accelerator is in use when it is not.
        return ResolvedBackend(
            "cpu",
            "cpu",
            "int8",
            True,
            f"{requested} acceleration is not available for transcription; "
            "the processor was used instead",
        )

    # auto
    if _cuda_available():
        return ResolvedBackend("cuda", "cuda", "float16", False)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        # Apple Silicon CPUs run int8 CTranslate2 well; there is no Metal path.
        return ResolvedBackend("cpu", "cpu", "int8", False)
    return cpu


# ── Transcriber protocol ──────────────────────────────────────────────────


class SegmentTranscriber(Protocol):
    """What the pipeline needs from a speech model.

    Narrow on purpose: it keeps faster-whisper out of every unit test, and it is
    the seam a different engine would be swapped in at.
    """

    def transcribe_window(
        self, audio_path: Path, start_seconds: float, end_seconds: float
    ) -> list[tuple[float, float, str]]:
        """Return ``(start, end, text)`` **relative to the window's start**."""
        ...


class FasterWhisperTranscriber:
    """faster-whisper backed transcriber."""

    def __init__(
        self,
        *,
        model: str = "medium",
        backend: ResolvedBackend | None = None,
        language: str = "auto",
    ):
        self.model_name = model
        self.backend = backend or resolve_backend("auto")
        self.language = language
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as error:
                raise TranscriptionError(
                    "The speech-to-text engine is not installed. Run `uv sync`."
                ) from error

            logger.info(
                "Loading %s on %s (%s)",
                self.model_name,
                self.backend.device,
                self.backend.compute_type,
            )
            self._model = WhisperModel(
                self.model_name,
                device=self.backend.device,
                compute_type=self.backend.compute_type,
            )
        return self._model

    def transcribe_window(
        self, audio_path: Path, start_seconds: float, end_seconds: float
    ) -> list[tuple[float, float, str]]:
        model = self._load()
        segments, _info = model.transcribe(
            str(audio_path),
            language=None if self.language == "auto" else self.language,
            clip_timestamps=[start_seconds, end_seconds],
            vad_filter=False,
        )
        # clip_timestamps yields times already on the source timeline, so
        # subtract the window start to satisfy the protocol's relative contract.
        return [
            (max(0.0, s.start - start_seconds), max(0.0, s.end - start_seconds), s.text.strip())
            for s in segments
            if s.text and s.text.strip()
        ]


# ── Assembly ──────────────────────────────────────────────────────────────


def silence_marker_text(window: SilenceWindow) -> str:
    seconds = round(window.duration_seconds)
    return f"[nobody speaking · {seconds} seconds]"


def build_transcript(
    audio_path: Path,
    segments: list[SpeechSegment],
    silences: list[SilenceWindow],
    transcriber: SegmentTranscriber,
) -> list[TranscriptSegment]:
    """Transcribe each speech segment and weave the silences back in.

    The remapping is the point: a model given a window reports times relative to
    it, and the segment's own offset is what puts them back on the video's
    timeline.
    """
    collected: list[TranscriptSegment] = []

    for segment in segments:
        try:
            pieces = transcriber.transcribe_window(
                audio_path, segment.start_seconds, segment.end_seconds
            )
        except Exception as error:
            # One unreadable stretch must not lose the rest of the transcript.
            logger.warning(
                "Could not transcribe %.1fs to %.1fs: %s",
                segment.start_seconds,
                segment.end_seconds,
                redacted_exception_text(error),
            )
            continue

        for relative_start, relative_end, text in pieces:
            collected.append(
                TranscriptSegment(
                    start_seconds=round(segment.start_seconds + relative_start, 3),
                    end_seconds=round(segment.start_seconds + relative_end, 3),
                    text=text,
                )
            )

    for window in silences:
        collected.append(
            TranscriptSegment(
                start_seconds=window.start_seconds,
                end_seconds=window.end_seconds,
                text=silence_marker_text(window),
                is_silence=True,
            )
        )

    collected.sort(key=lambda s: (s.start_seconds, s.is_silence))
    return collected


def write_transcript(
    output_dir: Path, result: TranscriptionResult, *, source_filename: str
) -> tuple[Path, Path]:
    """Write the structured transcript and its plain-text rendering."""
    output_dir = Path(output_dir)
    json_path = output_dir / TRANSCRIPT_FILENAME
    text_path = output_dir / TRANSCRIPT_TEXT_FILENAME

    write_json(
        json_path,
        {
            "version": 1,
            "source_filename": source_filename,
            "provenance": result.provenance.as_dict() if result.provenance else {},
            "segment_count": len(result.segments),
            "segments": [
                {
                    "start_seconds": s.start_seconds,
                    "end_seconds": s.end_seconds,
                    "timestamp": s.timestamp_label,
                    "text": s.text,
                    "is_silence": s.is_silence,
                }
                for s in result.segments
            ],
        },
    )

    from app.core.artifacts import write_text

    write_text(text_path, result.text + "\n")
    return json_path, text_path
