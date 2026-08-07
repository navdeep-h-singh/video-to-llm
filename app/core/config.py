"""Settings resolution and the localhost boundary.

Settings come from three sources, later ones overriding earlier:

1. the defaults in this module,
2. ``config/settings.toml`` if present,
3. environment variables prefixed ``VIDEO_TO_LLM_``.

The bind host is not among them. It is a constant, and :func:`assert_loopback`
is the only way an address reaches the server. See ``docs/SECURITY.md``.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# ── The localhost boundary ────────────────────────────────────────────────

#: The only address this application ever binds. Not configurable, by design.
BIND_HOST = "127.0.0.1"

#: Hostnames accepted as loopback for an outbound local-model endpoint.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost"})


class NonLoopbackAddressError(ValueError):
    """Raised when an address outside the loopback interface is supplied."""


def is_loopback_host(host: str | None) -> bool:
    """True when *host* unambiguously names this machine's loopback interface.

    Literal addresses are checked numerically rather than by string comparison,
    so the many spellings of loopback — ``127.0.0.1``, ``127.0.0.2``,
    ``0x7f.1``, ``::1``, ``::ffff:127.0.0.1`` — are all recognised, and a name
    that merely starts with "localhost" (``localhost.evil.example``) is not.
    """
    if not host:
        return False

    candidate = host.strip().strip("[]").lower()
    if not candidate:
        return False

    # Strip a zone identifier: fe80::1%en0
    candidate = candidate.split("%", 1)[0]

    if candidate in LOOPBACK_HOSTNAMES:
        return True

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False

    if address.is_loopback:
        return True

    # IPv4-mapped IPv6, e.g. ::ffff:127.0.0.1
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def assert_loopback(host: str | None, *, context: str = "address") -> str:
    """Return *host* if it is loopback, otherwise refuse.

    Used both for the server bind address and for the Ollama endpoint. A
    "local" model that is quietly on another machine would send your frames off
    this computer while the interface still said they were staying put.
    """
    if not is_loopback_host(host):
        raise NonLoopbackAddressError(
            f"{context} must be on the loopback interface "
            f"(127.0.0.1, localhost, or ::1) — got {host!r}. "
            "This version is localhost-only and does not offer network access."
        )
    return host  # type: ignore[return-value]


# ── Settings ──────────────────────────────────────────────────────────────

SAMPLING_PRESETS: dict[str, float] = {
    "detailed": 1.0,
    "balanced": 2.0,
    "economy": 3.0,
}

MIN_INTERVAL_SECONDS = 0.5
MAX_INTERVAL_SECONDS = 10.0
INTERVAL_STEP_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class SamplingSettings:
    preset: str = "balanced"
    custom_interval_seconds: float = 2.0

    def interval_seconds(self) -> float:
        if self.preset == "custom":
            return self.custom_interval_seconds
        return SAMPLING_PRESETS.get(self.preset, 2.0)

    def interval_ms(self) -> int:
        return round(self.interval_seconds() * 1000)

    def validate(self) -> None:
        if self.preset not in {*SAMPLING_PRESETS, "custom"}:
            raise ValueError(f"unknown sampling preset {self.preset!r}")
        if self.preset == "custom":
            value = self.custom_interval_seconds
            if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
                raise ValueError(
                    f"custom interval must be between {MIN_INTERVAL_SECONDS} and "
                    f"{MAX_INTERVAL_SECONDS} seconds — got {value}"
                )
            steps = value / INTERVAL_STEP_SECONDS
            if abs(steps - round(steps)) > 1e-9:
                raise ValueError(
                    f"custom interval must be a multiple of {INTERVAL_STEP_SECONDS} s — got {value}"
                )


@dataclass(frozen=True, slots=True)
class TranscriptionSettings:
    backend: str = "auto"
    model: str = "medium"
    silence_threshold_seconds: float = 3.0
    language: str = "auto"


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    hard_limit_usd: float = 25.0
    on_limit: str = "stop_and_ask"


@dataclass(frozen=True, slots=True)
class LocalGuardSettings:
    max_runtime_minutes: int = 0
    max_frames_per_run: int = 0


@dataclass(frozen=True, slots=True)
class VisualAnalysisSettings:
    enabled: bool = False
    provider: str = "none"
    model_id: str = ""
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    local_guard: LocalGuardSettings = field(default_factory=LocalGuardSettings)


@dataclass(frozen=True, slots=True)
class OllamaSettings:
    endpoint: str = "http://127.0.0.1:11434"
    batch_size: int = 1
    concurrency: int = 1
    experimental_acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    poll_interval_seconds: int = 2
    max_retries: int = 3
    backoff_base_seconds: int = 5


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    default_token_limit: int = 200_000
    default_reserve_tokens: int = 20_000
    allow_video_split: bool = False


@dataclass(frozen=True, slots=True)
class Settings:
    output_root: Path | None = None
    port: int = 8712
    log_level: str = "INFO"
    sampling: SamplingSettings = field(default_factory=SamplingSettings)
    transcription: TranscriptionSettings = field(default_factory=TranscriptionSettings)
    visual_analysis: VisualAnalysisSettings = field(default_factory=VisualAnalysisSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    collections: CollectionSettings = field(default_factory=CollectionSettings)

    # The bind host is a constant, exposed as a property so nothing can assign it.
    @property
    def host(self) -> str:
        return BIND_HOST

    @property
    def base_url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}"

    def validate(self) -> None:
        self.sampling.validate()
        assert_loopback(BIND_HOST, context="server bind host")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port out of range: {self.port}")
        # Rejected here rather than at request time, so a bad endpoint is a
        # start-up error instead of a surprise mid-job.
        _assert_loopback_url(self.ollama.endpoint)

    def is_first_run(self) -> bool:
        return self.output_root is None

    def with_output_root(self, root: Path) -> Settings:
        return replace(self, output_root=Path(root).expanduser().resolve())


def _assert_loopback_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NonLoopbackAddressError(f"Ollama endpoint must be an http(s) URL — got {url!r}")
    assert_loopback(parsed.hostname, context="Ollama endpoint")
    return url


# ── Loading ───────────────────────────────────────────────────────────────

DEFAULT_OUTPUT_ROOT_NAME = "VideoToLLM"
ENV_PREFIX = "VIDEO_TO_LLM_"


def default_output_root() -> Path:
    return Path.home() / "Documents" / DEFAULT_OUTPUT_ROOT_NAME


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def settings_file() -> Path:
    return repo_root() / "config" / "settings.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def load_settings(
    *,
    path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Build a validated :class:`Settings` from file and environment."""
    environ = os.environ if env is None else env
    data = _read_toml(path if path is not None else settings_file())

    general = data.get("general", {})
    server = data.get("server", {})
    sampling = data.get("sampling", {})
    transcription = data.get("transcription", {})
    visual = data.get("visual_analysis", {})
    ollama = data.get("ollama", {})
    worker = data.get("worker", {})
    collections = data.get("collections", {})

    def env_value(name: str, fallback: Any) -> Any:
        raw = environ.get(ENV_PREFIX + name)
        return _coerce(raw) if raw is not None else fallback

    raw_root = env_value("OUTPUT_ROOT", general.get("output_root") or None)
    output_root = Path(str(raw_root)).expanduser().resolve() if raw_root else None

    settings = Settings(
        output_root=output_root,
        port=int(env_value("PORT", server.get("port", 8712))),
        log_level=str(env_value("LOG_LEVEL", data.get("log_level", "INFO"))).upper(),
        sampling=SamplingSettings(
            preset=str(sampling.get("preset", "balanced")),
            custom_interval_seconds=float(sampling.get("custom_interval_seconds", 2.0)),
        ),
        transcription=TranscriptionSettings(
            backend=str(transcription.get("backend", "auto")),
            model=str(transcription.get("model", "medium")),
            silence_threshold_seconds=float(transcription.get("silence_threshold_seconds", 3.0)),
            language=str(transcription.get("language", "auto")),
        ),
        visual_analysis=VisualAnalysisSettings(
            enabled=bool(visual.get("enabled", False)),
            provider=str(visual.get("provider", "none")),
            model_id=str(visual.get("model_id", "")),
            budget=BudgetSettings(
                hard_limit_usd=float(visual.get("budget", {}).get("hard_limit_usd", 25.0)),
                on_limit=str(visual.get("budget", {}).get("on_limit", "stop_and_ask")),
            ),
            local_guard=LocalGuardSettings(
                max_runtime_minutes=int(
                    visual.get("local_guard", {}).get("max_runtime_minutes", 0)
                ),
                max_frames_per_run=int(visual.get("local_guard", {}).get("max_frames_per_run", 0)),
            ),
        ),
        ollama=OllamaSettings(
            endpoint=str(
                environ.get("OLLAMA_ENDPOINT") or ollama.get("endpoint", "http://127.0.0.1:11434")
            ),
            batch_size=int(ollama.get("batch_size", 1)),
            concurrency=int(ollama.get("concurrency", 1)),
            experimental_acknowledged=bool(ollama.get("experimental_acknowledged", False)),
        ),
        worker=WorkerSettings(
            poll_interval_seconds=int(worker.get("poll_interval_seconds", 2)),
            max_retries=int(worker.get("max_retries", 3)),
            backoff_base_seconds=int(worker.get("backoff_base_seconds", 5)),
        ),
        collections=CollectionSettings(
            default_token_limit=int(collections.get("default_token_limit", 200_000)),
            default_reserve_tokens=int(collections.get("default_reserve_tokens", 20_000)),
            allow_video_split=bool(collections.get("allow_video_split", False)),
        ),
    )
    settings.validate()
    return settings


# ── Saving ────────────────────────────────────────────────────────────────


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_settings_toml(settings: Settings) -> str:
    """Render settings as TOML.

    Written by hand rather than with a serialiser so the file keeps its
    comments. A settings file a person can read and edit is worth more here
    than one a library round-trips perfectly — this is the file someone opens
    when the interface is not in front of them.

    ``output_root`` is deliberately included: it is the one path the user chose
    and losing it on save would silently reset the application to first-run.
    """
    visual = settings.visual_analysis
    return f"""# Written by Video to LLM. Safe to edit by hand.
# Anything omitted falls back to the shipped default.

[general]
output_root = {_toml_value(str(settings.output_root) if settings.output_root else "")}

[server]
# The bind host is fixed to the loopback interface in code and is not
# configurable. There is no LAN mode.
port = {settings.port}

[sampling]
preset = {_toml_value(settings.sampling.preset)}
custom_interval_seconds = {settings.sampling.custom_interval_seconds}

[transcription]
backend = {_toml_value(settings.transcription.backend)}
model = {_toml_value(settings.transcription.model)}
silence_threshold_seconds = {settings.transcription.silence_threshold_seconds}
language = {_toml_value(settings.transcription.language)}

[visual_analysis]
# Off by default. A local-only job never touches this section.
enabled = {_toml_value(visual.enabled)}
# none | ollama_local | anthropic | google | openai | openai_compatible
provider = {_toml_value(visual.provider)}
# Free text. Never validated against a fixed catalogue.
model_id = {_toml_value(visual.model_id)}

[visual_analysis.budget]
# External providers only. A model on this computer has no provider charge.
hard_limit_usd = {visual.budget.hard_limit_usd}
on_limit = {_toml_value(visual.budget.on_limit)}

[visual_analysis.local_guard]
max_runtime_minutes = {visual.local_guard.max_runtime_minutes}
max_frames_per_run = {visual.local_guard.max_frames_per_run}

[ollama]
# Loopback hosts only. Anything else is rejected outright.
endpoint = {_toml_value(settings.ollama.endpoint)}
batch_size = {settings.ollama.batch_size}
concurrency = {settings.ollama.concurrency}
experimental_acknowledged = {_toml_value(settings.ollama.experimental_acknowledged)}

[worker]
poll_interval_seconds = {settings.worker.poll_interval_seconds}
max_retries = {settings.worker.max_retries}
backoff_base_seconds = {settings.worker.backoff_base_seconds}

[collections]
default_token_limit = {settings.collections.default_token_limit}
default_reserve_tokens = {settings.collections.default_reserve_tokens}
allow_video_split = {_toml_value(settings.collections.allow_video_split)}
"""


def save_settings(settings: Settings, *, path: Path | None = None) -> Path:
    """Validate, then write settings to disk atomically.

    Validation runs first so an unusable configuration is refused rather than
    written — a settings file that stops the application from starting is much
    harder to recover from than a rejected form.
    """
    settings.validate()

    target = Path(path) if path is not None else settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Imported here rather than at module scope: artifacts imports the logging
    # module, which imports redaction, and config is loaded before either.
    from app.core.artifacts import write_text

    write_text(target, render_settings_toml(settings))
    return target
