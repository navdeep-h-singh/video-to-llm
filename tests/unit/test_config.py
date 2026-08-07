"""Settings, and the two boundaries that must never move: the bind address and
what counts as a loopback endpoint."""

from __future__ import annotations

import pytest

from app.core.config import (
    BIND_HOST,
    NonLoopbackAddressError,
    SamplingSettings,
    Settings,
    assert_loopback,
    is_loopback_host,
    load_settings,
)

# ── The loopback rule ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",  # the whole 127/8 block is loopback
        "127.255.255.254",
        "localhost",
        "LOCALHOST",
        "::1",
        "[::1]",
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6
        " 127.0.0.1 ",
    ],
)
def test_loopback_spellings_are_all_accepted(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "192.168.1.10",
        "10.0.0.1",
        "8.8.8.8",
        "example.com",
        # Names that merely start with, or contain, "localhost" are not loopback.
        "localhost.evil.example",
        "notlocalhost",
        "localhost.",
        "2001:4860:4860::8888",
        "",
        None,
    ],
)
def test_non_loopback_hosts_are_rejected(host):
    assert is_loopback_host(host) is False


def test_assert_loopback_raises_with_a_useful_message():
    with pytest.raises(NonLoopbackAddressError) as excinfo:
        assert_loopback("0.0.0.0", context="server bind host")
    message = str(excinfo.value)
    assert "server bind host" in message
    assert "localhost-only" in message


def test_bind_host_is_the_loopback_constant():
    assert BIND_HOST == "127.0.0.1"
    assert is_loopback_host(BIND_HOST)


def test_settings_host_is_read_only():
    # `host` is a property on a frozen, slotted dataclass, so assignment is
    # refused — the exact exception type depends on which of those two
    # mechanisms rejects it first, and either is fine. What matters is that no
    # assignment succeeds and the value is unchanged afterwards.
    settings = Settings()
    assert settings.host == BIND_HOST
    with pytest.raises((AttributeError, TypeError)):
        settings.host = "0.0.0.0"  # type: ignore[misc]
    assert settings.host == BIND_HOST


def test_base_url_is_always_loopback():
    assert Settings(port=9000).base_url == "http://127.0.0.1:9000"


# ── Ollama endpoint ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.1.50:11434",
        "http://ollama.example.com:11434",
        "https://someone-elses-machine.invalid",
        "http://0.0.0.0:11434",
    ],
)
def test_non_loopback_ollama_endpoint_is_refused_at_load(endpoint, tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(f'[ollama]\nendpoint = "{endpoint}"\n', encoding="utf-8")
    with pytest.raises(NonLoopbackAddressError):
        load_settings(path=config, env={})


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"],
)
def test_loopback_ollama_endpoints_load(endpoint, tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(f'[ollama]\nendpoint = "{endpoint}"\n', encoding="utf-8")
    assert load_settings(path=config, env={}).ollama.endpoint == endpoint


def test_ollama_endpoint_must_be_http(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text('[ollama]\nendpoint = "ftp://127.0.0.1:11434"\n', encoding="utf-8")
    with pytest.raises(NonLoopbackAddressError):
        load_settings(path=config, env={})


# ── Sampling ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("preset", "expected_ms"),
    [("detailed", 1000), ("balanced", 2000), ("economy", 3000)],
)
def test_presets_map_to_intervals(preset, expected_ms):
    assert SamplingSettings(preset=preset).interval_ms() == expected_ms


def test_balanced_is_the_default():
    assert SamplingSettings().interval_ms() == 2000


@pytest.mark.parametrize("value", [0.5, 1.0, 2.5, 5.0, 10.0])
def test_valid_custom_intervals_are_accepted(value):
    SamplingSettings(preset="custom", custom_interval_seconds=value).validate()


@pytest.mark.parametrize("value", [0.4, 0.0, 10.5, 11.0, -1.0])
def test_out_of_range_custom_intervals_are_rejected(value):
    with pytest.raises(ValueError, match="between"):
        SamplingSettings(preset="custom", custom_interval_seconds=value).validate()


@pytest.mark.parametrize("value", [0.7, 1.3, 2.25])
def test_custom_interval_must_land_on_a_half_second_step(value):
    with pytest.raises(ValueError, match="multiple"):
        SamplingSettings(preset="custom", custom_interval_seconds=value).validate()


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="unknown sampling preset"):
        SamplingSettings(preset="scene-detection").validate()


# ── Loading and precedence ────────────────────────────────────────────────


def test_defaults_when_nothing_is_configured(tmp_path):
    settings = load_settings(path=tmp_path / "absent.toml", env={})
    assert settings.port == 8712
    assert settings.output_root is None
    assert settings.is_first_run() is True
    assert settings.sampling.preset == "balanced"
    assert settings.transcription.backend == "auto"
    assert settings.transcription.model == "medium"
    assert settings.transcription.silence_threshold_seconds == 3.0
    assert settings.visual_analysis.enabled is False
    assert settings.visual_analysis.provider == "none"
    assert settings.visual_analysis.budget.hard_limit_usd == 25.0
    assert settings.ollama.batch_size == 1
    assert settings.ollama.concurrency == 1


def test_environment_overrides_the_file(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text("[server]\nport = 9001\n", encoding="utf-8")
    settings = load_settings(path=config, env={"VIDEO_TO_LLM_PORT": "9999"})
    assert settings.port == 9999


def test_file_is_used_when_the_environment_is_silent(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text("[server]\nport = 9001\n", encoding="utf-8")
    assert load_settings(path=config, env={}).port == 9001


def test_output_root_is_expanded_and_absolute(tmp_path):
    settings = load_settings(
        path=tmp_path / "absent.toml",
        env={"VIDEO_TO_LLM_OUTPUT_ROOT": str(tmp_path / "out")},
    )
    assert settings.output_root == (tmp_path / "out").resolve()
    assert settings.is_first_run() is False


def test_visual_analysis_stays_off_unless_explicitly_enabled(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text("[visual_analysis]\nmodel_id = 'qwen2.5vl:7b'\n", encoding="utf-8")
    assert load_settings(path=config, env={}).visual_analysis.enabled is False


def test_out_of_range_port_is_rejected():
    with pytest.raises(ValueError, match="port out of range"):
        Settings(port=70000).validate()
