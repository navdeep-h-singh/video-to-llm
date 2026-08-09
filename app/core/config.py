"""Settings resolution and the localhost boundary.

Settings come from three sources, later ones overriding earlier:

1. the defaults in this module,
2. the user's settings file if present — see :func:`settings_file`, which is in
   the platform's application-support directory, not inside the installation,
3. environment variables prefixed ``VIDEO_TO_LLM_``.

The bind host is not among them. It is a constant, and :func:`assert_loopback`
is the only way an address reaches the server. See ``docs/SECURITY.md``.
"""

from __future__ import annotations

import ipaddress
import os
import sys
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


#: Services that speak a documented API shape at an address you supply. Both
#: exist because "OpenAI-compatible" and "Anthropic-compatible" are the two
#: shapes the industry actually settled on, and a great deal of what people run
#: — a gateway, a proxy, a hosted open model, a company's own deployment —
#: presents itself as one or the other.
CUSTOM_ENDPOINT_PROVIDERS = frozenset({"openai_compatible", "anthropic_compatible"})

#: Every provider that describes pictures, in the order they are offered.
DESCRIPTION_PROVIDERS: tuple[str, ...] = (
    "ollama_local",
    "anthropic",
    "google",
    "openai",
    "openai_compatible",
    "anthropic_compatible",
)


@dataclass(frozen=True, slots=True)
class VisualAnalysisSettings:
    enabled: bool = False
    provider: str = "none"

    #: Which model each service should use, keyed by provider.
    #:
    #: This was a single string shared by every provider, which meant the model
    #: was a property of the *application* rather than of the service — so
    #: setting a Gemini model and then switching to Claude asked Anthropic for
    #: ``gemini-2.5-flash``. Google quietly defaulted and the others failed at
    #: request time with a confusing message. A model only means anything
    #: relative to the service that offers it, so it is stored that way.
    models: dict[str, str] = field(default_factory=dict)

    #: Where a compatible endpoint lives, keyed by provider. Only meaningful for
    #: :data:`CUSTOM_ENDPOINT_PROVIDERS`; the named services have fixed
    #: addresses that are not the user's to change.
    base_urls: dict[str, str] = field(default_factory=dict)

    budget: BudgetSettings = field(default_factory=BudgetSettings)
    local_guard: LocalGuardSettings = field(default_factory=LocalGuardSettings)

    def model_for(self, provider: str | None = None) -> str:
        """The model chosen for *provider*, or for the active one."""
        return self.models.get(provider or self.provider, "")

    def base_url_for(self, provider: str | None = None) -> str:
        name = provider or self.provider
        if name not in CUSTOM_ENDPOINT_PROVIDERS:
            return ""
        return self.base_urls.get(name, "")

    @property
    def model_id(self) -> str:
        """The active provider's model.

        Kept as a read-only property so the many call sites that ask a settings
        object for "the model" keep working and keep meaning the right thing.
        Assigning it is deliberately impossible: a single writable model is the
        bug this map replaced.
        """
        return self.model_for()


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
class NotificationSettings:
    """How this computer tells you a long job has finished.

    Everything here happens on this machine. The specification excludes OS
    notification registration, launchd, systemd, and any outbound call, so there
    is deliberately no push service, no email, and no menu-bar agent — which
    suits a product whose whole promise is that nothing leaves the computer.

    The title badge and the "finished while you were away" banner are always on:
    neither asks permission and neither can interrupt anything.
    """

    #: Browser notifications. Off until asked for — a permission prompt on first
    #: run is a poor first impression and teaches the user to click Deny.
    browser: bool = False
    #: A terminal bell from the worker. Free, needs no permission, and reaches
    #: someone who started this from a shell and switched away.
    terminal_bell: bool = True


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
    notifications: NotificationSettings = field(default_factory=NotificationSettings)

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


def user_config_dir() -> Path:
    """Where this machine expects a user's application configuration to live."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "VideoToLLM"
        return Path.home() / "AppData" / "Roaming" / "VideoToLLM"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VideoToLLM"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "video-to-llm"


def legacy_settings_file() -> Path:
    """Where settings used to be written: inside the application itself.

    Kept only so an existing installation's configuration can be carried across
    once. Nothing writes here any more.
    """
    return repo_root() / "config" / "settings.toml"


def default_settings_file() -> Path:
    return user_config_dir() / "settings.toml"


def settings_file() -> Path:
    """The file settings are read from and written to.

    This used to be ``config/settings.toml`` inside the application directory,
    which made the user's configuration a property of the *installation* rather
    than of the *user*. Three consequences, all real: an application installed
    somewhere read-only could not save at all; two instances pointed at
    different output roots silently shared one file, so configuring one
    reconfigured the other; and upgrading by replacing the folder threw the
    configuration away.

    Overridable so a test never has to go near the real one.
    """
    override = os.environ.get(ENV_PREFIX + "CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return default_settings_file()


def adopt_legacy_settings(target: Path) -> None:
    """Copy an in-application settings file to the user location, once.

    Deliberately a copy and not a move: if someone downgrades, or this turns out
    to be the wrong call, the original is still where it was. The old file stops
    being read as soon as the new one exists.

    Only ever runs for the real default location, so a test pointing
    :func:`settings_file` at a temporary path cannot pull the operator's own
    configuration into its fixture.
    """
    if target != default_settings_file() or target.exists():
        return

    legacy = legacy_settings_file()
    if not legacy.is_file():
        return

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        # Not fatal: the defaults still load, and the next save will try again.
        return


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


def _provider_models(visual: dict[str, Any]) -> dict[str, str]:
    """Read the per-provider model map, carrying an older file's single value.

    ``model_id`` was one string for every service. Anyone upgrading has one in
    their file and it belongs to whichever provider was selected when they wrote
    it — so it is filed under that provider rather than discarded, and rather
    than being applied to all of them, which would recreate the bug the map
    exists to fix.
    """
    models = {
        str(name): str(value)
        for name, value in (visual.get("models") or {}).items()
        if str(value).strip()
    }

    legacy = str(visual.get("model_id", "")).strip()
    active = str(visual.get("provider", "none"))
    if legacy and active not in {"none", ""} and active not in models:
        models[active] = legacy
    return models


def load_settings(
    *,
    path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Build a validated :class:`Settings` from file and environment."""
    environ = os.environ if env is None else env

    source = path if path is not None else settings_file()
    if path is None:
        # Carry an older installation's configuration across the first time it
        # is missing from the user location. No-op afterwards, and never for a
        # path a test has redirected.
        adopt_legacy_settings(source)
    data = _read_toml(source)

    general = data.get("general", {})
    server = data.get("server", {})
    sampling = data.get("sampling", {})
    transcription = data.get("transcription", {})
    visual = data.get("visual_analysis", {})
    ollama = data.get("ollama", {})
    worker = data.get("worker", {})
    collections = data.get("collections", {})
    notifications = data.get("notifications", {})

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
            models=_provider_models(visual),
            base_urls={
                str(k): str(v)
                for k, v in (visual.get("base_urls") or {}).items()
                if k in CUSTOM_ENDPOINT_PROVIDERS and str(v).strip()
            },
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
        notifications=NotificationSettings(
            browser=bool(notifications.get("browser", False)),
            terminal_bell=bool(notifications.get("terminal_bell", True)),
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


def _toml_table(values: dict[str, str]) -> str:
    """Render a flat string table, or nothing at all.

    An empty table is written as no lines rather than as commented placeholders:
    a key with an invented value is the kind of thing someone later uncomments
    without meaning to.
    """
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in sorted(values.items()))


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
#      | anthropic_compatible
provider = {_toml_value(visual.provider)}

[visual_analysis.models]
# One model per service, because a model name only means anything to the
# service that offers it. Free text: never validated against a fixed
# catalogue, which would go stale the week a provider renames something.
{_toml_table(visual.models)}
[visual_analysis.base_urls]
# Only for the two "compatible" services, which by definition live wherever
# you put them. The named services have fixed addresses.
{_toml_table(visual.base_urls)}
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

[notifications]
# Everything here happens on this computer. There is no push service, no email,
# and no outbound call of any kind.
browser = {_toml_value(settings.notifications.browser)}
terminal_bell = {_toml_value(settings.notifications.terminal_bell)}
"""


#: Top-level keys this version models. Anything else in the file belongs to a
#: newer version, or to the user, and is not ours to discard.
KNOWN_SECTIONS = frozenset(
    {
        "general",
        "server",
        "sampling",
        "transcription",
        "visual_analysis",
        "ollama",
        "worker",
        "collections",
        "notifications",
        "log_level",
    }
)


def render_unknown_toml(data: dict[str, Any]) -> str:
    """Re-render the parts of a settings file this version does not model.

    Saving used to rewrite the file from the dataclass alone, so any key the
    running version did not know about vanished the first time the user pressed
    Save on an unrelated section. That silently punishes anyone who edited the
    file by hand, and it makes downgrading destructive: run an older build once
    and the newer build's configuration is gone.

    Kept verbatim rather than merged, and clearly labelled, so it is obvious
    these lines were not written by this version.
    """
    leftovers = {key: value for key, value in data.items() if key not in KNOWN_SECTIONS}
    if not leftovers:
        return ""

    lines = [
        "",
        "# ── Kept from the existing file ───────────────────────────────────",
        "# This version of the application does not use these settings. They are",
        "# preserved so that editing anything above does not discard them.",
    ]
    for key, value in sorted(leftovers.items()):
        if isinstance(value, dict):
            lines.append(f"\n[{key}]")
            for inner_key, inner_value in value.items():
                lines.append(f"{inner_key} = {_toml_value(inner_value)}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def save_settings(settings: Settings, *, path: Path | None = None) -> Path:
    """Validate, then write settings to disk atomically.

    Validation runs first so an unusable configuration is refused rather than
    written — a settings file that stops the application from starting is much
    harder to recover from than a rejected form.

    Whatever the existing file held that this version does not model is carried
    across; see :func:`render_unknown_toml`.
    """
    settings.validate()

    target = Path(path) if path is not None else settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)

    preserved = render_unknown_toml(_read_toml(target))

    # Imported here rather than at module scope: artifacts imports the logging
    # module, which imports redaction, and config is loaded before either.
    from app.core.artifacts import write_text

    write_text(target, render_settings_toml(settings) + preserved)
    return target
