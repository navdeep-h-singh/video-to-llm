"""The visual-analysis provider contract.

One normalized request goes in, one normalized schema comes out. Everything a
particular service needs — its SDK, its auth, its image encoding, its retry
quirks — stays inside its adapter. The pipeline never learns which provider it
is talking to beyond the label it records in provenance.

Two rules run through all of this:

**Alignment is verified, never assumed.** Frames are sent carrying a visible
``IDX nn`` stamp, and every returned description must claim an index that was
actually in the request. A model handed twenty pictures will occasionally answer
about them in the wrong order or invent a twenty-first; silently accepting that
attaches a description to the wrong moment, which is worse than having no
description at all.

**`Unknown` is a real answer.** A model that cannot read a value must say so and
be believed. Filling in a plausible guess produces evidence that looks solid and
is fiction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
UNKNOWN = "Unknown"


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


#: The exact output fields required by the specification, in order.
REQUIRED_FIELDS = (
    "index",
    "timeframe",
    "currency_pair",
    "indicators_and_states",
    "exact_action",
    "visible_text",
    "visual_description",
    "setup_type",
    "confidence",
)


class ProviderError(RuntimeError):
    """Base for adapter failures."""


class TransientProviderError(ProviderError):
    """Worth retrying: a timeout, a rate limit, a 5xx."""


class PermanentProviderError(ProviderError):
    """Not worth retrying: bad credentials, an unknown model, a refusal."""


class SchemaValidationError(ProviderError):
    """The response was not the shape we asked for."""


class AlignmentError(ProviderError):
    """Returned indexes do not match what was sent."""


# ── Request and response ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FrameRequest:
    """One frame offered for description."""

    index: int
    timestamp_seconds: float
    #: The watermarked provider copy. The clean frame is never sent.
    image_path: Path

    @property
    def idx_label(self) -> str:
        """Matches the stamp drawn on the image: 1-based, zero-padded."""
        return f"{self.index + 1:02d}"


@dataclass(frozen=True)
class AnalysisRequest:
    frames: tuple[FrameRequest, ...]
    model_id: str
    prompt: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("an analysis request needs at least one frame")

    @property
    def expected_labels(self) -> set[str]:
        return {f.idx_label for f in self.frames}

    def frame_for_label(self, label: str) -> FrameRequest | None:
        return next((f for f in self.frames if f.idx_label == label), None)


@dataclass
class FrameDescription:
    """One structured description, normalized across every provider."""

    index: int
    timeframe: str = UNKNOWN
    currency_pair: str = UNKNOWN
    indicators_and_states: str = UNKNOWN
    exact_action: str = UNKNOWN
    visible_text: str = UNKNOWN
    visual_description: str = UNKNOWN
    setup_type: str = UNKNOWN
    confidence: str = Confidence.LOW

    # Enrichment, filled in by the pipeline rather than the provider.
    timestamp_seconds: float | None = None
    clean_filename: str | None = None
    api_filename: str | None = None
    batch_id: str | None = None
    provider: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None
    schema_hash: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence == Confidence.LOW

    @property
    def unknown_field_count(self) -> int:
        return sum(
            1
            for name in (
                "timeframe",
                "currency_pair",
                "indicators_and_states",
                "exact_action",
                "visible_text",
                "visual_description",
                "setup_type",
            )
            if getattr(self, name) == UNKNOWN
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkipRecord:
    """A frame that could not be described, and why.

    Kept rather than dropped: a gap the user can see is recoverable, a gap that
    is silently absent is not.
    """

    index: int
    reason: str
    attempts: int = 0
    permanent: bool = True


@dataclass
class AnalysisResult:
    descriptions: list[FrameDescription] = field(default_factory=list)
    skips: list[SkipRecord] = field(default_factory=list)
    provider: str = ""
    model_id: str = ""
    #: None for local runs. See `cost_label` — "$0.00" would be a lie.
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    retry_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_gaps(self) -> bool:
        return bool(self.skips)

    @property
    def cost_label(self) -> str:
        """What the interface shows.

        A local run has no provider charge at all, which is a different
        statement from "it cost zero dollars" — local compute, battery, heat and
        time are all real. The specification requires this exact wording.
        """
        if self.cost_usd is None:
            return "No provider API charge"
        return f"${self.cost_usd:.4f}"


# ── The protocol ──────────────────────────────────────────────────────────


class VisualAnalysisProvider(Protocol):
    """What every adapter implements."""

    name: str
    requires_api_key: bool
    max_batch_frames: int

    def describe(self, request: AnalysisRequest) -> AnalysisResult:
        """Describe every frame in the request."""
        ...

    def health_check(self) -> ProviderHealth:
        """Report reachability and capability without doing paid work."""
        ...


@dataclass
class ProviderHealth:
    reachable: bool
    detail: str
    runtime_version: str | None = None
    model_available: bool | None = None
    vision_capable: bool | None = None
    remediation: str = ""
    advisory: str = ""

    @property
    def vision_verified(self) -> bool:
        return self.vision_capable is True

    @property
    def vision_status_label(self) -> str:
        if self.vision_capable is True:
            return "Can read pictures"
        if self.vision_capable is False:
            return "Cannot read pictures"
        # Deliberately not "probably fine". An unverified capability is stated
        # as unverified, and the UI requires an explicit acknowledgement.
        return "Vision capability not verified"


# ── Schema handling ───────────────────────────────────────────────────────


def schema_hash() -> str:
    """Stable hash of the output contract.

    Recorded with every description so a later collection can tell whether two
    videos were described under the same schema, and warn when they were not.
    """
    payload = json.dumps({"version": SCHEMA_VERSION, "fields": REQUIRED_FIELDS}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _coerce_text(value: Any) -> str:
    """Normalize whatever a model returned into a non-empty string.

    Missing, null, or empty becomes ``Unknown`` rather than an empty string: the
    reviewer needs to see that nothing was read, not a blank that looks like an
    answer nobody typed.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else UNKNOWN
    if isinstance(value, (list, tuple)):
        joined = "; ".join(str(v).strip() for v in value if str(v).strip())
        return joined or UNKNOWN
    if isinstance(value, dict):
        joined = "; ".join(f"{k}: {v}" for k, v in value.items())
        return joined or UNKNOWN
    return str(value)


def _coerce_confidence(value: Any) -> str:
    """Map a model's confidence wording onto the three levels we record.

    Anything unrecognised becomes Low. Treating an unparseable confidence as
    High would quietly promote a guess into evidence.
    """
    if value is None:
        return Confidence.LOW

    text = str(value).strip().lower()
    if text in {"high", "certain", "confident", "very high"}:
        return Confidence.HIGH
    if text in {"medium", "moderate", "fairly sure", "fair"}:
        return Confidence.MEDIUM
    if text in {"low", "unsure", "uncertain", "very low"}:
        return Confidence.LOW

    try:
        numeric = float(text)
    except ValueError:
        return Confidence.LOW
    if numeric > 1.0:
        numeric /= 100.0
    if numeric >= 0.8:
        return Confidence.HIGH
    if numeric >= 0.5:
        return Confidence.MEDIUM
    return Confidence.LOW


def parse_description(payload: dict[str, Any], *, index: int) -> FrameDescription:
    """Turn one raw provider object into a normalized description."""
    return FrameDescription(
        index=index,
        timeframe=_coerce_text(payload.get("timeframe")),
        currency_pair=_coerce_text(payload.get("currency_pair")),
        indicators_and_states=_coerce_text(payload.get("indicators_and_states")),
        exact_action=_coerce_text(payload.get("exact_action")),
        visible_text=_coerce_text(payload.get("visible_text")),
        visual_description=_coerce_text(payload.get("visual_description")),
        setup_type=_coerce_text(payload.get("setup_type")),
        confidence=_coerce_confidence(payload.get("confidence")),
        schema_hash=schema_hash(),
    )


def extract_json(text: str) -> Any:
    """Pull JSON out of a model response.

    Models wrap JSON in prose and code fences no matter how firmly the prompt
    asks them not to. Being tolerant here converts a large class of avoidable
    retries — each of which costs money on a cloud provider — into successes.
    """
    if not text or not text.strip():
        raise SchemaValidationError("the response was empty")

    stripped = text.strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # ```json ... ``` fences
    if "```" in stripped:
        for chunk in stripped.split("```")[1:]:
            candidate = chunk.removeprefix("json").removeprefix("JSON").strip()
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # A bare array or object embedded in prose.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise SchemaValidationError("the response did not contain readable JSON")


def validate_alignment(request: AnalysisRequest, returned_labels: list[str]) -> None:
    """Refuse a response whose indexes do not match what was sent.

    Both directions matter. An index we never sent means the model invented a
    frame. A missing index means one was dropped, and if we accepted the rest
    positionally every later description would attach to the wrong moment.
    """
    expected = request.expected_labels
    returned = set(returned_labels)

    unexpected = returned - expected
    missing = expected - returned

    if len(returned_labels) != len(returned):
        duplicates = sorted(
            {label for label in returned_labels if returned_labels.count(label) > 1}
        )
        raise AlignmentError(
            f"the same picture number came back more than once: {', '.join(duplicates)}"
        )

    if unexpected:
        raise AlignmentError(
            f"the answer mentions picture numbers that were not sent: "
            f"{', '.join(sorted(unexpected))}"
        )

    if missing:
        raise AlignmentError(f"the answer is missing picture numbers: {', '.join(sorted(missing))}")


def normalize_batch(request: AnalysisRequest, raw: Any) -> list[FrameDescription]:
    """Validate a whole batch response and return descriptions in request order."""
    if isinstance(raw, dict):
        # Models commonly wrap the array in a single key.
        for key in ("frames", "results", "descriptions", "data", "items", "output"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
        else:
            raw = [raw]

    if not isinstance(raw, list):
        raise SchemaValidationError(f"expected a list of descriptions, got {type(raw).__name__}")

    labels: list[str] = []
    by_label: dict[str, dict[str, Any]] = {}

    for entry in raw:
        if not isinstance(entry, dict):
            raise SchemaValidationError("one of the descriptions was not an object")
        label = _normalize_label(entry.get("index"))
        if label is None:
            raise SchemaValidationError("a description did not say which picture it was about")
        labels.append(label)
        by_label[label] = entry

    validate_alignment(request, labels)

    descriptions: list[FrameDescription] = []
    for frame in request.frames:
        description = parse_description(by_label[frame.idx_label], index=frame.index)
        description.timestamp_seconds = frame.timestamp_seconds
        description.api_filename = frame.image_path.name
        descriptions.append(description)

    return descriptions


def _normalize_label(value: Any) -> str | None:
    """Accept ``1``, ``"01"``, ``"IDX 01"``, ``"idx_1"`` as the same label."""
    if value is None:
        return None
    text = str(value).strip().upper().removeprefix("IDX").strip(" _-:")
    if not text:
        return None
    try:
        return f"{int(text):02d}"
    except ValueError:
        return None
