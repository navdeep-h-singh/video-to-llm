"""Local Ollama — labelled **Local / Experimental** everywhere it appears.

Frames stay on this device and there is no provider charge. Local compute,
battery, heat, memory use, and processing time apply instead — which is why the
interface says "No provider API charge" rather than "$0.00".

Three constraints make this adapter different from the cloud ones:

**Loopback only.** ``127.0.0.1``, ``localhost``, and ``::1`` are the only hosts
accepted, checked numerically. A "local" model that is quietly on another
machine would ship frames off this computer while the interface still said they
were staying put.

**No credential, at all.** Not an optional one, not an empty one. There is no
key field for Ollama in the interface and no environment variable for it.

**Small batches.** One frame per request by default, two after a successful
preflight, three or four only as an advanced override. Cloud-style 20-frame
batching against a 7B model on a laptop exhausts memory and degrades alignment
accuracy long before it saves any time.

Ollama itself is never installed, started, updated, or bundled by this
application. It is entirely user-managed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import is_loopback_host
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.providers.base import (
    AlignmentError,
    AnalysisRequest,
    AnalysisResult,
    PermanentProviderError,
    ProviderHealth,
    SchemaValidationError,
    SkipRecord,
    TransientProviderError,
    extract_json,
    normalize_batch,
    prompt_hash,
    schema_hash,
)

logger = get_logger(__name__)

PROVIDER_NAME = "ollama_local"
DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
SUGGESTED_MODEL = "qwen2.5vl:7b"

DEFAULT_BATCH_SIZE = 1
PREFLIGHT_BATCH_SIZE = 2
MAX_ADVANCED_BATCH_SIZE = 4

HEALTH_TIMEOUT_SECONDS = 10.0
#: Generous: a 7B vision model on a laptop CPU can take minutes for one frame.
GENERATE_TIMEOUT_SECONDS = 600.0

DISCLOSURE = (
    "Frames stay on this device. No provider API charge. "
    "Local compute, battery, heat, memory use, and processing time apply."
)

RELIABILITY_WARNING = (
    "Local models may be less reliable for tiny text, dense labels, exact "
    "values, and strict structured extraction. Review low-confidence results."
)

APPLE_SILICON_ADVISORY = (
    "On Apple Silicon with about 24 GB of memory, start Qwen2.5-VL 7B with "
    "1 to 2 images per request, a 4K-8K context where that is configurable, and "
    "one request at a time. This is guidance, not a performance guarantee."
)


class NonLoopbackEndpointError(PermanentProviderError):
    """The configured endpoint is not on this machine."""


def assert_loopback_endpoint(endpoint: str) -> str:
    """Refuse any endpoint that is not on the loopback interface."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise NonLoopbackEndpointError(f"The endpoint must be an http address — got {endpoint!r}.")
    if not is_loopback_host(parsed.hostname):
        raise NonLoopbackEndpointError(
            f"{endpoint} is not on this computer. This version only talks to a "
            "model running locally (127.0.0.1, localhost, or ::1). "
            "Set the endpoint back to http://127.0.0.1:11434."
        )
    return endpoint


def resolve_batch_size(requested: int, *, preflight_passed: bool, advanced: bool = False) -> int:
    """Clamp the batch size to what is actually safe.

    Silently accepting 20 here would produce an out-of-memory failure minutes
    into a run, after the user had been told it would work.
    """
    if requested < 1:
        return DEFAULT_BATCH_SIZE
    if advanced:
        return min(requested, MAX_ADVANCED_BATCH_SIZE)
    if preflight_passed:
        return min(requested, PREFLIGHT_BATCH_SIZE)
    return DEFAULT_BATCH_SIZE


@dataclass
class OllamaLocalProvider:
    """The Local Ollama adapter."""

    endpoint: str = DEFAULT_ENDPOINT
    model_id: str = ""
    batch_size: int = DEFAULT_BATCH_SIZE
    concurrency: int = 1
    client: httpx.Client | None = None

    name: str = PROVIDER_NAME
    #: There is no key for this provider and none will ever be requested.
    requires_api_key: bool = False
    max_batch_frames: int = MAX_ADVANCED_BATCH_SIZE

    def __post_init__(self) -> None:
        # Checked at construction, so a bad endpoint fails before any frame is
        # encoded rather than partway through a run.
        assert_loopback_endpoint(self.endpoint)

    def _client(self, timeout: float) -> httpx.Client:
        if self.client is not None:
            return self.client
        return httpx.Client(timeout=timeout, follow_redirects=False)

    # ── Health ────────────────────────────────────────────────────────────

    def health_check(self) -> ProviderHealth:
        """Ask the local runtime what it is and what it has, without generating.

        Reports honestly at each level: reachable or not, which version, whether
        the exact model is installed, and whether vision can be *confirmed* —
        never guessed.
        """
        assert_loopback_endpoint(self.endpoint)
        client = self._client(HEALTH_TIMEOUT_SECONDS)
        close = self.client is None

        try:
            try:
                version_response = client.get(f"{self.endpoint}/api/version")
                version_response.raise_for_status()
                runtime_version = str(version_response.json().get("version", "")) or None
            except httpx.HTTPError as error:
                return ProviderHealth(
                    reachable=False,
                    detail=f"Nothing is answering on this computer at {self.endpoint}.",
                    remediation=(
                        "Install Ollama from https://ollama.com and start it, then "
                        "check again.\n"
                        f"  ollama serve\n  ollama pull {SUGGESTED_MODEL}"
                    ),
                    advisory=redacted_exception_text(error),
                )

            try:
                tags = client.get(f"{self.endpoint}/api/tags")
                tags.raise_for_status()
                installed = [m.get("name", "") for m in tags.json().get("models", [])]
            except httpx.HTTPError:
                installed = []

            wanted = self.model_id or SUGGESTED_MODEL
            model_available = any(
                name == wanted or name.split(":")[0] == wanted.split(":")[0] for name in installed
            )

            if not model_available:
                return ProviderHealth(
                    reachable=True,
                    runtime_version=runtime_version,
                    model_available=False,
                    vision_capable=None,
                    detail=f"Ollama {runtime_version or ''} is answering, but "
                    f"{wanted} is not installed.",
                    remediation=f"Install it, then check again:\n  ollama pull {wanted}",
                    advisory=APPLE_SILICON_ADVISORY,
                )

            vision_capable = self._probe_vision(client, wanted)

            return ProviderHealth(
                reachable=True,
                runtime_version=runtime_version,
                model_available=True,
                vision_capable=vision_capable,
                detail=(f"Ollama {runtime_version or ''} is answering and {wanted} is installed."),
                remediation=(
                    ""
                    if vision_capable
                    else "This model's details do not confirm it can read pictures. "
                    "You can still try it, but you will be asked to acknowledge "
                    "that it is experimental."
                ),
                advisory=APPLE_SILICON_ADVISORY,
            )
        finally:
            if close:
                client.close()

    def list_models(self) -> list[str]:
        """Models actually installed here, from the runtime's own catalogue.

        The local counterpart to the cloud adapters' :meth:`list_models`. Same
        contract, and the honest one: this asks what is installed rather than
        offering a list of models the machine may not have pulled.
        """
        with self._client(HEALTH_TIMEOUT_SECONDS) as client:
            try:
                response = client.get(f"{self.endpoint}/api/tags")
                response.raise_for_status()
            except httpx.HTTPError as error:
                raise PermanentProviderError(
                    f"Could not reach the local runtime. {redacted_exception_text(error)}"
                ) from error
            names = [str(m.get("name", "")) for m in response.json().get("models", [])]
        return sorted(n for n in names if n)

    def _probe_vision(self, client: httpx.Client, model: str) -> bool | None:
        """Try to confirm image support from the model's own metadata.

        Returns None when it cannot be confirmed — which is reported as
        "Vision capability not verified", never as a cheerful assumption.
        """
        try:
            response = client.post(f"{self.endpoint}/api/show", json={"name": model})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        families = payload.get("details", {}).get("families") or []
        if any("clip" in str(f).lower() or "vision" in str(f).lower() for f in families):
            return True

        capabilities = payload.get("capabilities") or []
        if any("vision" in str(c).lower() for c in capabilities):
            return True

        blob = " ".join(
            str(payload.get(key, "")) for key in ("template", "modelfile", "parameters")
        ).lower()
        if "image" in blob or "vision" in blob:
            return True

        return None

    # ── Description ───────────────────────────────────────────────────────

    def describe(self, request: AnalysisRequest) -> AnalysisResult:
        """Describe a batch. Local batches are small by design."""
        assert_loopback_endpoint(self.endpoint)

        if len(request.frames) > MAX_ADVANCED_BATCH_SIZE:
            raise PermanentProviderError(
                f"{len(request.frames)} pictures were offered at once, but a model on "
                f"this computer takes at most {MAX_ADVANCED_BATCH_SIZE}. "
                "Cloud-sized batches exhaust memory on a local model."
            )

        client = self._client(GENERATE_TIMEOUT_SECONDS)
        close = self.client is None
        started = time.monotonic()

        try:
            images = [_encode_image(frame.image_path) for frame in request.frames]
            model_name: str = request.model_id or self.model_id or SUGGESTED_MODEL
            payload: dict[str, Any] = {
                "model": model_name,
                "prompt": request.prompt,
                "images": images,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
            }

            try:
                response = client.post(f"{self.endpoint}/api/generate", json=payload)
            except httpx.TimeoutException as error:
                raise TransientProviderError(
                    "The model on this computer took too long to answer. "
                    "Fewer pictures per request may help."
                ) from error
            except httpx.HTTPError as error:
                raise TransientProviderError(
                    f"Could not reach the model on this computer: {redacted_exception_text(error)}"
                ) from error

            if response.status_code == 404:
                raise PermanentProviderError(
                    f"{payload['model']} is not installed. Run: ollama pull {payload['model']}"
                )
            if response.status_code >= 500:
                raise TransientProviderError(
                    f"The local model returned an error ({response.status_code})."
                )
            if response.status_code >= 400:
                raise PermanentProviderError(
                    f"The local model refused the request ({response.status_code})."
                )

            body = response.json().get("response", "")
            descriptions = normalize_batch(request, extract_json(body))

            for description in descriptions:
                description.provider = PROVIDER_NAME
                description.model_id = model_name
                description.prompt_hash = prompt_hash(request.prompt)
                description.schema_hash = schema_hash()

            return AnalysisResult(
                descriptions=descriptions,
                provider=PROVIDER_NAME,
                model_id=model_name,
                # None, not 0.0 — there is no provider charge to report.
                cost_usd=None,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if close:
                client.close()

    def describe_with_skips(self, request: AnalysisRequest) -> AnalysisResult:
        """Describe, converting a permanent failure into skip records.

        A batch that cannot be described leaves visible gaps rather than
        failing the whole video — the frames and transcript are still worth
        having.
        """
        try:
            return self.describe(request)
        except (SchemaValidationError, AlignmentError, PermanentProviderError) as error:
            reason = redacted_exception_text(error)
            logger.warning("Local model could not describe a batch: %s", reason)
            return AnalysisResult(
                skips=[SkipRecord(index=f.index, reason=reason) for f in request.frames],
                provider=PROVIDER_NAME,
                model_id=request.model_id or self.model_id,
                cost_usd=None,
            )


def _encode_image(path) -> str:
    import base64
    from pathlib import Path

    return base64.b64encode(Path(path).read_bytes()).decode("ascii")
