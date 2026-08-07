"""Cloud adapters: Anthropic Claude, Google Gemini, OpenAI, and any
OpenAI-compatible endpoint.

What is sent, and only this: the small numbered still pictures, as image
payloads. Never the video file. Never the audio. Never the transcript. Never a
path or filename from the user's disk — the images are sent as bytes, so nothing
about the local directory layout travels with them.

Each adapter owns its own auth header, request shape, and image encoding. The
pipeline above never learns which one it is talking to beyond the label recorded
in provenance.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.credentials.store import require_credential
from app.providers.base import (
    AnalysisRequest,
    AnalysisResult,
    PermanentProviderError,
    ProviderHealth,
    TransientProviderError,
    extract_json,
    normalize_batch,
    prompt_hash,
    schema_hash,
)
from app.providers.costs import estimate_cost

logger = get_logger(__name__)

MAX_CLOUD_BATCH_FRAMES = 20
REQUEST_TIMEOUT_SECONDS = 180.0

#: Status codes worth retrying. 408/429 and 5xx are transient by definition;
#: 401/403/404 are not, and retrying them just burns time.
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def encode_image(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


@dataclass
class CloudProvider:
    """Shared behaviour for every HTTP provider."""

    model_id: str = ""
    api_key: str | None = None
    client: httpx.Client | None = None
    base_url: str = ""

    name: str = "cloud"
    requires_api_key: bool = True
    max_batch_frames: int = MAX_CLOUD_BATCH_FRAMES
    _prompt_suffix: str = field(default="", repr=False)

    def _client(self) -> httpx.Client:
        if self.client is not None:
            return self.client
        return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    def _key(self) -> str:
        return self.api_key or require_credential(self.name)

    # Subclasses supply these three.
    def _endpoint(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _headers(self, key: str) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _payload(self, request: AnalysisRequest) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _extract_text(self, body: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _usage(self, body: dict[str, Any]) -> tuple[int | None, int | None]:
        return None, None

    # ── The shared request path ───────────────────────────────────────────

    def describe(self, request: AnalysisRequest) -> AnalysisResult:
        if len(request.frames) > self.max_batch_frames:
            raise PermanentProviderError(
                f"{len(request.frames)} pictures were offered at once, but this "
                f"service takes at most {self.max_batch_frames}."
            )

        key = self._key()
        client = self._client()
        close = self.client is None
        started = time.monotonic()

        try:
            try:
                response = client.post(
                    self._endpoint(),
                    headers=self._headers(key),
                    json=self._payload(request),
                )
            except httpx.TimeoutException as error:
                raise TransientProviderError("The service took too long to answer.") from error
            except httpx.HTTPError as error:
                raise TransientProviderError(
                    f"Could not reach the service: {redacted_exception_text(error)}"
                ) from error

            self._raise_for_status(response)

            body = response.json()
            descriptions = normalize_batch(request, extract_json(self._extract_text(body)))

            model = request.model_id or self.model_id
            for description in descriptions:
                description.provider = self.name
                description.model_id = model
                description.prompt_hash = prompt_hash(request.prompt)
                description.schema_hash = schema_hash()

            input_tokens, output_tokens = self._usage(body)
            estimate = estimate_cost(self.name, len(request.frames))

            return AnalysisResult(
                descriptions=descriptions,
                provider=self.name,
                model_id=model,
                cost_usd=estimate.estimated_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if close:
                client.close()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return

        if response.status_code in TRANSIENT_STATUS:
            raise TransientProviderError(
                f"The service returned a temporary error ({response.status_code})."
            )

        if response.status_code in {401, 403}:
            # Deliberately says nothing about the key itself.
            raise PermanentProviderError("The service rejected the API key. Check it in Settings.")
        if response.status_code == 404:
            raise PermanentProviderError(
                f"The service does not recognise the model {self.model_id!r}. Check the model name."
            )
        raise PermanentProviderError(f"The service refused the request ({response.status_code}).")

    def health_check(self) -> ProviderHealth:
        """Report configuration state without making a paid call.

        No live request by default: a readiness check that silently bills the
        user would be a poor surprise. The interface offers a separately
        labelled, explicitly confirmed paid test.
        """
        from app.credentials.store import credential_status

        # An explicitly supplied key counts, exactly as it does in describe().
        # Reporting "no key set" while describe() would happily send would make
        # the readiness screen disagree with what the job actually does.
        status = credential_status(self.name)
        have_key = bool(self.api_key) or status.present
        source = "supplied directly" if self.api_key else status.detail

        if not have_key:
            return ProviderHealth(
                reachable=False,
                detail=f"No API key is set for {self.name}.",
                remediation="Add one in Settings. Processing on this computer "
                "needs no key and keeps working.",
            )

        return ProviderHealth(
            reachable=True,
            detail=f"A key is configured ({source}) and the model is "
            f"{self.model_id or 'not chosen yet'}.",
            model_available=bool(self.model_id),
            vision_capable=None,
            remediation="Not tested against the service — a live test costs money "
            "and is confirmed separately.",
        )


# ── Anthropic ─────────────────────────────────────────────────────────────


@dataclass
class AnthropicProvider(CloudProvider):
    name: str = "anthropic"
    base_url: str = "https://api.anthropic.com"
    api_version: str = "2023-06-01"

    def _endpoint(self) -> str:
        return f"{self.base_url}/v1/messages"

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "x-api-key": key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

    def _payload(self, request: AnalysisRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for frame in request.frames:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encode_image(frame.image_path),
                    },
                }
            )
        content.append({"type": "text", "text": request.prompt})

        return {
            "model": request.model_id or self.model_id,
            "max_tokens": 4096,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
        }

    def _extract_text(self, body: dict[str, Any]) -> str:
        blocks = body.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def _usage(self, body: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = body.get("usage", {})
        return usage.get("input_tokens"), usage.get("output_tokens")


# ── Google Gemini ─────────────────────────────────────────────────────────


@dataclass
class GoogleProvider(CloudProvider):
    name: str = "google"
    base_url: str = "https://generativelanguage.googleapis.com"

    def _endpoint(self) -> str:
        model = self.model_id or "gemini-2.5-flash"
        return f"{self.base_url}/v1beta/models/{model}:generateContent"

    def _headers(self, key: str) -> dict[str, str]:
        # Header rather than a query parameter: a key in a URL ends up in logs,
        # proxies, and browser history.
        return {"x-goog-api-key": key, "content-type": "application/json"}

    def _payload(self, request: AnalysisRequest) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": encode_image(frame.image_path),
                }
            }
            for frame in request.frames
        ]
        parts.append({"text": request.prompt})

        return {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }

    def _extract_text(self, body: dict[str, Any]) -> str:
        candidates = body.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    def _usage(self, body: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = body.get("usageMetadata", {})
        return usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


# ── OpenAI, and anything speaking its shape ───────────────────────────────


@dataclass
class OpenAIProvider(CloudProvider):
    name: str = "openai"
    base_url: str = "https://api.openai.com"

    def _endpoint(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def _headers(self, key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {key}", "content-type": "application/json"}

    def _payload(self, request: AnalysisRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encode_image(frame.image_path)}"},
            }
            for frame in request.frames
        ]
        content.append({"type": "text", "text": request.prompt})

        return {
            "model": request.model_id or self.model_id,
            "temperature": 0,
            "messages": [{"role": "user", "content": content}],
        }

    def _extract_text(self, body: dict[str, Any]) -> str:
        choices = body.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""

    def _usage(self, body: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = body.get("usage", {})
        return usage.get("prompt_tokens"), usage.get("completion_tokens")


@dataclass
class OpenAICompatibleProvider(OpenAIProvider):
    """Any endpoint speaking the OpenAI chat-completions shape.

    The base URL is required and comes from the user's own configuration, so
    there is no default to accidentally send frames to.
    """

    name: str = "openai_compatible"
    base_url: str = ""

    def _endpoint(self) -> str:
        if not self.base_url:
            raise PermanentProviderError("No address is set for this service. Add one in Settings.")
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"


PROVIDERS: dict[str, type[CloudProvider]] = {
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "openai": OpenAIProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def build_provider(name: str, **kwargs: Any) -> Any:
    """Construct an adapter by name, local or cloud."""
    if name == "ollama_local":
        from app.providers.ollama_local import OllamaLocalProvider

        return OllamaLocalProvider(**kwargs)

    if name not in PROVIDERS:
        raise PermanentProviderError(f"Unknown provider {name!r}.")
    return PROVIDERS[name](**kwargs)
