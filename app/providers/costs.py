"""Cost estimation and the hard budget stop.

Every figure here is an **estimate** built from local, versioned assumptions in
``config/pricing.toml``. Nothing is fetched from a provider, and providers change
their prices. The interface says so wherever a number appears.

The budget stop is the part that has to be exactly right: it is checked *before*
a batch is sent, never after. Checking afterwards would mean the spend that
crossed the limit had already happened, which makes the cap decorative.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

PRICING_VERSION = 1

#: Used when a provider has no entry. Deliberately pessimistic, so an unknown
#: model over-estimates rather than quietly under-estimating and overrunning.
DEFAULT_ASSUMPTIONS = {
    "input_per_million_tokens": 5.00,
    "output_per_million_tokens": 15.00,
    "tokens_per_image": 1600,
    "output_tokens_per_image": 320,
}

BUILTIN_PRICING: dict[str, dict[str, float]] = {
    "anthropic": {
        "input_per_million_tokens": 3.00,
        "output_per_million_tokens": 15.00,
        "tokens_per_image": 1600,
        "output_tokens_per_image": 320,
    },
    "google": {
        "input_per_million_tokens": 0.30,
        "output_per_million_tokens": 2.50,
        "tokens_per_image": 1300,
        "output_tokens_per_image": 320,
    },
    "openai": {
        "input_per_million_tokens": 2.50,
        "output_per_million_tokens": 10.00,
        "tokens_per_image": 1450,
        "output_tokens_per_image": 320,
    },
    "openai_compatible": dict(DEFAULT_ASSUMPTIONS),
    # Same treatment as its OpenAI-shaped twin: an endpoint you host or proxy
    # has no published price we could know, so the estimate is explicitly an
    # assumption rather than a quote.
    "anthropic_compatible": dict(DEFAULT_ASSUMPTIONS),
}

#: Providers that run on this computer and have no provider charge at all.
NO_CHARGE_PROVIDERS = frozenset({"ollama_local"})


class BudgetExceededError(RuntimeError):
    """Raised before sending a batch that would cross the hard limit."""


@dataclass(frozen=True)
class CostEstimate:
    provider: str
    frame_count: int
    estimated_usd: float | None
    input_tokens: int
    output_tokens: int
    assumptions_version: int = PRICING_VERSION

    @property
    def label(self) -> str:
        """What the interface shows.

        A local run has no provider charge, which is a different statement from
        "it cost zero dollars" — battery, heat, memory and time are all real.
        """
        if self.estimated_usd is None:
            return "No provider API charge"
        return f"${self.estimated_usd:.2f}"

    @property
    def is_estimate(self) -> bool:
        return self.estimated_usd is not None


def load_pricing(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Load pricing assumptions, falling back to the built-in table."""
    if path is None:
        from app.core.config import repo_root

        path = repo_root() / "config" / "pricing.toml"

    pricing = {name: dict(values) for name, values in BUILTIN_PRICING.items()}

    if not Path(path).is_file():
        return pricing

    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        # A malformed pricing file must not stop a local-only job, and the
        # built-in table is a safe conservative substitute.
        logger.warning("Could not read pricing file; using built-in assumptions: %s", error)
        return pricing

    for name, values in data.items():
        if isinstance(values, dict) and name not in {"version", "currency"}:
            merged = dict(DEFAULT_ASSUMPTIONS)
            merged.update(pricing.get(name, {}))
            merged.update({k: v for k, v in values.items() if isinstance(v, (int, float))})
            pricing[name] = merged

    return pricing


def estimate_cost(
    provider: str,
    frame_count: int,
    *,
    pricing: dict[str, dict[str, float]] | None = None,
) -> CostEstimate:
    """Estimate what describing *frame_count* frames would cost."""
    if provider in NO_CHARGE_PROVIDERS:
        return CostEstimate(
            provider=provider,
            frame_count=frame_count,
            estimated_usd=None,
            input_tokens=0,
            output_tokens=0,
        )

    table = pricing if pricing is not None else load_pricing()
    assumptions = table.get(provider, DEFAULT_ASSUMPTIONS)

    input_tokens = int(frame_count * assumptions["tokens_per_image"])
    output_tokens = int(frame_count * assumptions["output_tokens_per_image"])

    cost = (
        input_tokens / 1_000_000 * assumptions["input_per_million_tokens"]
        + output_tokens / 1_000_000 * assumptions["output_per_million_tokens"]
    )

    return CostEstimate(
        provider=provider,
        frame_count=frame_count,
        estimated_usd=round(cost, 6),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


@dataclass
class BudgetTracker:
    """Enforces the hard spending limit for one job.

    The check happens before a batch is sent. Doing it afterwards would mean the
    spend that crossed the limit had already left, which makes the cap
    decorative rather than a limit.
    """

    limit_usd: float
    spent_usd: float = 0.0
    provider: str = ""

    @property
    def applies(self) -> bool:
        """False for local providers — there is no provider charge to cap."""
        return self.provider not in NO_CHARGE_PROVIDERS

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.applies and self.spent_usd >= self.limit_usd

    def would_exceed(self, next_batch_usd: float) -> bool:
        if not self.applies:
            return False
        return self.spent_usd + next_batch_usd > self.limit_usd

    def check_before_send(self, next_batch_usd: float) -> None:
        """Raise rather than send a batch that would cross the limit."""
        if not self.applies:
            return
        if self.would_exceed(next_batch_usd):
            raise BudgetExceededError(
                f"Sending the next batch would cost about ${next_batch_usd:.2f}, "
                f"taking the total past your ${self.limit_usd:.2f} limit "
                f"(${self.spent_usd:.2f} spent so far). "
                "Pictures already described are kept, and the job records exactly "
                "where it stopped."
            )

    def record(self, actual_usd: float | None) -> None:
        """Add real spend. None (a local run) adds nothing."""
        if actual_usd is not None:
            self.spent_usd = round(self.spent_usd + actual_usd, 6)

    @property
    def progress_label(self) -> str:
        if not self.applies:
            return "No provider API charge"
        return f"${self.spent_usd:.2f} of ${self.limit_usd:.2f}"
