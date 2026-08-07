"""Readiness checks.

Backs both the `doctor` command and the first-run readiness screen, so the two
can never disagree about whether this machine is ready.

Every check reports one of three states and — when something is wrong — says
what to do about it in plain language. A check that only reports failure leaves
the user stuck; the remediation is the useful half.

Nothing here is fatal on its own. Visual analysis being unconfigured is a
perfectly normal, fully supported setup, so it reports `optional`, not `fail`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import Settings, is_loopback_host
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text

logger = get_logger(__name__)

FFMPEG_TIMEOUT_SECONDS = 10
#: Below this, a job of any size will run out of room partway through.
LOW_DISK_WARNING_GB = 5.0


class CheckState(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class CheckResult:
    key: str
    title: str
    state: CheckState
    detail: str
    remediation: str = ""

    @property
    def blocking(self) -> bool:
        """True when this stops local-only processing from working at all."""
        return self.state is CheckState.FAIL


@dataclass
class DoctorReport:
    checks: list[CheckResult]

    @property
    def ready(self) -> bool:
        return not any(check.blocking for check in self.checks)

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.state is CheckState.WARN]

    def get(self, key: str) -> CheckResult | None:
        return next((c for c in self.checks if c.key == key), None)


# ── Individual checks ─────────────────────────────────────────────────────


def check_ffmpeg() -> CheckResult:
    """FFmpeg and ffprobe are the one hard requirement for local processing."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    install_hint = (
        "Install FFmpeg, then run this check again.\n"
        "  macOS:   brew install ffmpeg\n"
        "  Windows: winget install Gyan.FFmpeg\n"
        "  Linux:   sudo apt install ffmpeg"
    )

    missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
    if missing:
        return CheckResult(
            key="ffmpeg",
            title="Reading your video files",
            state=CheckState.FAIL,
            detail=f"Not found on your PATH: {', '.join(missing)}.",
            remediation=install_hint,
        )

    # `missing` above guarantees both are non-None; assert so the type
    # checker sees the narrowing too.
    assert ffmpeg is not None and ffprobe is not None

    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CheckResult(
            key="ffmpeg",
            title="Reading your video files",
            state=CheckState.FAIL,
            detail=f"FFmpeg is on your PATH but would not run: {redacted_exception_text(error)}",
            remediation=install_hint,
        )

    first_line = (result.stdout or "").splitlines()[:1]
    version = first_line[0] if first_line else "version unknown"
    return CheckResult(
        key="ffmpeg",
        title="Reading your video files",
        state=CheckState.OK,
        detail=version,
    )


def check_transcription(settings: Settings) -> CheckResult:
    """Speech-to-text availability.

    Reports the *installed* state without downloading anything. Model weights are
    fetched on first real use — pulling ~1.5 GB from a readiness check the user
    ran to see whether things work would be a rude surprise.
    """
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return CheckResult(
            key="transcription",
            title="Turning speech into text",
            state=CheckState.FAIL,
            detail="The speech-to-text engine is not installed.",
            remediation="Run `uv sync` in the project folder to install it.",
        )

    return CheckResult(
        key="transcription",
        title="Turning speech into text",
        state=CheckState.OK,
        detail=(
            f"Ready — {settings.transcription.model} model, "
            f"{settings.transcription.backend} backend. "
            "Runs on your processor; the model downloads the first time you use it."
        ),
    )


def check_output_root(settings: Settings) -> CheckResult:
    """Somewhere to write, with room to write it."""
    root = settings.output_root
    if root is None:
        return CheckResult(
            key="output_root",
            title="Somewhere to save the results",
            state=CheckState.FAIL,
            detail="No output folder has been chosen yet.",
            remediation="Choose a folder on the first-run screen, or set VIDEO_TO_LLM_OUTPUT_ROOT.",
        )

    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".vtl-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return CheckResult(
            key="output_root",
            title="Somewhere to save the results",
            state=CheckState.FAIL,
            detail=f"Cannot write to the output folder: {redacted_exception_text(error)}",
            remediation="Choose a folder you have permission to write to, or fix "
            "the permissions on this one.",
        )

    free_gb = shutil.disk_usage(root).free / 1024**3
    hours = free_gb / 2.16  # ~2.16 GB per hour at one 1280x720 frame every 2 s

    if free_gb < LOW_DISK_WARNING_GB:
        return CheckResult(
            key="output_root",
            title="Somewhere to save the results",
            state=CheckState.WARN,
            detail=f"Only {free_gb:.1f} GB free — enough for roughly {hours:.1f} hours of video.",
            remediation="Free up space, or choose an output folder on a larger drive.",
        )

    return CheckResult(
        key="output_root",
        title="Somewhere to save the results",
        state=CheckState.OK,
        detail=f"{free_gb:.0f} GB free — about {hours:.0f} hours of video.",
    )


def check_visual_analysis(settings: Settings) -> CheckResult:
    """Optional by design; never blocking.

    A first-time user should be able to process a video without meeting any of
    this, which is why it reports `optional` rather than `fail` when unset.
    """
    visual = settings.visual_analysis
    if not visual.enabled or visual.provider == "none":
        return CheckResult(
            key="visual_analysis",
            title="Describing what is on screen",
            state=CheckState.OPTIONAL,
            detail="Not set up. Jobs will produce pictures and a transcript, which "
            "is everything most work needs.",
            remediation="You can turn this on later without redoing any work.",
        )

    if visual.provider == "ollama_local":
        endpoint = settings.ollama.endpoint
        from urllib.parse import urlparse

        if not is_loopback_host(urlparse(endpoint).hostname):
            return CheckResult(
                key="visual_analysis",
                title="Describing what is on screen",
                state=CheckState.FAIL,
                detail=f"The configured endpoint {endpoint} is not on this computer.",
                remediation="This version only talks to a model on this machine. "
                "Set the endpoint back to http://127.0.0.1:11434.",
            )
        return CheckResult(
            key="visual_analysis",
            title="Describing what is on screen",
            state=CheckState.OPTIONAL,
            detail=f"Set to use a model on this computer ({visual.model_id or 'no model chosen'}). "
            "Frames stay on this device and there is no provider charge.",
            remediation="Use 'Check local model' in Settings to confirm it is answering.",
        )

    return CheckResult(
        key="visual_analysis",
        title="Describing what is on screen",
        state=CheckState.OPTIONAL,
        detail=f"Set to send pictures to {visual.provider} "
        f"({visual.model_id or 'no model chosen'}), capped at "
        f"${visual.budget.hard_limit_usd:.0f}.",
        remediation="Only the numbered still pictures are sent — never your video or its audio.",
    )


def check_worker(settings: Settings) -> CheckResult:
    """Whether a background worker currently owns the output root."""
    root = settings.output_root
    if root is None:
        return CheckResult(
            key="worker",
            title="Background processing",
            state=CheckState.OPTIONAL,
            detail="No output folder chosen yet, so no worker is running.",
        )

    from app.core.db import database_path
    from app.core.locks import claim_is_stale

    if not database_path(root).exists():
        return CheckResult(
            key="worker",
            title="Background processing",
            state=CheckState.OPTIONAL,
            detail="Not started yet.",
            remediation="Run `video-to-llm start` to begin.",
        )

    try:
        from app.core.db import connect

        connection = connect(database_path(root))
        try:
            row = connection.execute(
                "SELECT * FROM worker_claims WHERE output_root = ?", (str(root),)
            ).fetchone()
        finally:
            connection.close()
    except Exception as error:
        return CheckResult(
            key="worker",
            title="Background processing",
            state=CheckState.WARN,
            detail=f"Could not read worker state: {redacted_exception_text(error)}",
        )

    if row is None:
        return CheckResult(
            key="worker",
            title="Background processing",
            state=CheckState.OPTIONAL,
            detail="Not running.",
            remediation="Run `video-to-llm run-worker` to start it.",
        )

    if claim_is_stale(row["heartbeat_at"]):
        return CheckResult(
            key="worker",
            title="Background processing",
            state=CheckState.WARN,
            detail=f"A worker stopped without cleaning up (last seen {row['heartbeat_at']}).",
            remediation="Starting a new worker will take over safely. "
            "Unfinished work resumes; finished work is kept.",
        )

    return CheckResult(
        key="worker",
        title="Background processing",
        state=CheckState.OK,
        detail=f"Running since {row['claimed_at']} (pid {row['pid']}).",
    )


def check_localhost_binding(settings: Settings) -> CheckResult:
    """Asserts the boundary rather than assuming it."""
    if not is_loopback_host(settings.host):
        return CheckResult(
            key="binding",
            title="Runs only on this computer",
            state=CheckState.FAIL,
            detail=f"The interface would bind to {settings.host}, which is not this computer.",
            remediation="This is a defect — the address is meant to be fixed at "
            "127.0.0.1. Do not expose this application to a network.",
        )
    return CheckResult(
        key="binding",
        title="Runs only on this computer",
        state=CheckState.OK,
        detail=f"The interface is reachable only at {settings.base_url}.",
    )


def run_doctor(settings: Settings) -> DoctorReport:
    return DoctorReport(
        checks=[
            check_localhost_binding(settings),
            check_ffmpeg(),
            check_transcription(settings),
            check_output_root(settings),
            check_visual_analysis(settings),
            check_worker(settings),
        ]
    )


SYMBOLS = {
    CheckState.OK: "OK  ",
    CheckState.WARN: "WARN",
    CheckState.FAIL: "FAIL",
    CheckState.OPTIONAL: "--  ",
}


def format_report(report: DoctorReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        lines.append(f"[{SYMBOLS[check.state]}] {check.title}")
        lines.append(f"         {check.detail}")
        if check.remediation and check.state is not CheckState.OK:
            for hint in check.remediation.splitlines():
                lines.append(f"         {hint}")
        lines.append("")

    if report.ready:
        lines.append("Ready to process video on this computer.")
    else:
        blocking = [c.title for c in report.checks if c.blocking]
        lines.append(f"Not ready — resolve first: {', '.join(blocking)}")
    return "\n".join(lines)
