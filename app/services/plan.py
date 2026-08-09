"""What a job will produce, before it is started.

The new job screen used to be a form with a button. You chose an interval and a
service, pressed start, and found out what you had agreed to afterwards — how
many pictures, how much disk, how long, and whether anything was about to leave
the machine.

That last one is the point. This application's central claim is that nothing is
uploaded unless you ask, and the moment a user is deciding whether to trust that
is exactly the moment it said nothing at all.

**Every number here is measured or absent.** Frame counts and disk come from
probing the actual files, which preflight already does. Durations come from
finished stages on this machine, and where there is no history there is no
figure — an invented duration is worse than none, because someone plans an
afternoon around it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import PROVIDER_LABELS, Settings
from app.core.logging import get_logger
from app.pipeline.preflight import preflight
from app.providers.costs import CostEstimate, estimate_cost
from app.services.estimate import Estimate, estimate_by_video_seconds, estimate_stage

logger = get_logger(__name__)

#: Providers that make no network request at all.
STAYS_HERE = frozenset({"none", "ollama_local"})


def human_duration(seconds: float | None) -> str | None:
    """A rounded, honestly imprecise duration. `None` stays `None`."""
    if seconds is None or seconds <= 0:
        return None
    if seconds < 90:
        return "under 2 minutes"
    minutes = seconds / 60
    if minutes < 60:
        return f"about {round(minutes)} minutes"
    hours = minutes / 60
    if hours < 10:
        # Halves, because "about 3.7 hours" claims a precision that a median of
        # twenty samples does not have.
        return f"about {round(hours * 2) / 2:g} hours"
    return f"about {round(hours)} hours"


def human_bytes(value: float | None) -> str | None:
    if value is None or value <= 0:
        return None
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{int(value)} bytes"


@dataclass
class Plan:
    """What pressing start would do. `known_time` is deliberately tri-state."""

    ok: bool = False
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    video_count: int = 0
    duration_label: str = ""
    frame_count: int = 0

    disk_label: str | None = None
    free_label: str | None = None

    time_label: str | None = None
    time_samples: int = 0

    #: The sentence about what leaves this computer. Never omitted, because
    #: silence here is what the screen used to do.
    leaves: str = ""
    leaves_anything: bool = False

    cost_label: str | None = None
    budget_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "warnings": self.warnings,
            "video_count": self.video_count,
            "duration_label": self.duration_label,
            "frame_count": self.frame_count,
            "disk_label": self.disk_label,
            "free_label": self.free_label,
            "time_label": self.time_label,
            "time_samples": self.time_samples,
            "leaves": self.leaves,
            "leaves_anything": self.leaves_anything,
            "cost_label": self.cost_label,
            "budget_label": self.budget_label,
        }


def _leaves_sentence(provider: str, frame_count: int) -> tuple[str, bool]:
    """What will and will not be sent, in one sentence.

    Stated positively for the local cases rather than as an absence. "Nothing
    leaves this computer" is the claim the product is built on and the reason
    someone chose it; burying it in a missing row would waste it.
    """
    if provider == "none":
        return ("Nothing leaves this computer. No network request will be made.", False)
    if provider == "ollama_local":
        return (
            "Nothing leaves this computer. The pictures go to the model you "
            "installed, on this machine.",
            False,
        )
    label = PROVIDER_LABELS.get(provider, provider)
    return (
        f"{frame_count:,} still pictures will be sent to {label}. "
        "Your video and its audio are never sent.",
        True,
    )


def build_plan(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    paths: list[Path],
    interval_ms: int | None = None,
    provider: str = "none",
    model_id: str = "",
) -> Plan:
    """Probe the files and report what a job over them would involve.

    Creates nothing. Preflight is the same check the create path runs, so a plan
    that reports a problem is reporting the problem that would actually stop the
    job — not a second opinion that might disagree with it.
    """
    plan = Plan()
    if not paths:
        return plan

    report = preflight(paths, settings, connection=connection, interval_ms=interval_ms)
    plan.problems = list(report.problems)
    plan.warnings = list(report.warnings)
    plan.ok = report.ok
    plan.video_count = len(report.accepted)
    plan.duration_label = report.duration_label
    plan.frame_count = report.total_frames
    plan.disk_label = human_bytes(report.estimated_bytes)
    plan.free_label = human_bytes(report.free_bytes)

    if not report.ok:
        # A plan for a job that cannot start would be a prediction about nothing.
        plan.leaves, plan.leaves_anything = _leaves_sentence(provider, 0)
        return plan

    plan.leaves, plan.leaves_anything = _leaves_sentence(provider, report.total_frames)

    total = 0.0
    samples: list[int] = []
    for estimate in (
        estimate_stage(connection, "frames", report.total_frames),
        estimate_by_video_seconds(connection, "transcribe", report.total_duration_seconds),
        estimate_stage(connection, "visual", report.total_frames, model_id=model_id or None)
        if provider != "none"
        else Estimate(seconds=0.0, samples=0),
    ):
        if estimate.seconds is None:
            # One unknown stage makes the total unknown. Adding up the stages we
            # happen to have measured and presenting the sum as the whole would
            # under-report, and describing is by far the slowest of the three.
            plan.time_label = None
            plan.time_samples = 0
            break
        total += estimate.seconds
        samples.append(estimate.samples)
    else:
        plan.time_label = human_duration(total)
        plan.time_samples = min(samples) if samples else 0

    if provider not in STAYS_HERE:
        cost: CostEstimate = estimate_cost(provider, report.total_frames)
        plan.cost_label = cost.label
        limit = settings.visual_analysis.budget.hard_limit_usd
        if limit:
            plan.budget_label = f"${limit:,.2f}"

    return plan
