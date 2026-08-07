"""Retries, the corrective schema retry, and fallback policy.

Three distinct behaviours, deliberately kept apart because they fail for
different reasons and deserve different treatment:

**Transient errors** — timeouts, rate limits, 5xx — are retried with bounded
exponential backoff. Bounded, because an unbounded retry against a paid provider
is an unbounded bill.

**Invalid JSON** gets exactly *one* corrective retry that shows the model what
it did wrong. One, not many: a model that ignores the correction once will
ignore it again, and every attempt is billed.

**Permanent failures** — bad credentials, an unknown model, a refusal — are not
retried at all. They produce skip records, so the gap is visible in the review
workspace and the job can still complete as `Completed with gaps`.

Fallbacks are off by default. When enabled they are explicit and ordered, and
**there is never an automatic fall back from a local model to a cloud one** —
that would send frames off the machine after the user chose to keep them on it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.providers.base import (
    AlignmentError,
    AnalysisRequest,
    AnalysisResult,
    PermanentProviderError,
    SchemaValidationError,
    SkipRecord,
    TransientProviderError,
)

logger = get_logger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 120.0

#: Exactly one. See the module docstring.
SCHEMA_CORRECTION_ATTEMPTS = 1

CORRECTION_SUFFIX = (
    "\n\nYour previous reply could not be read. Reply with ONLY a JSON array — "
    "no prose, no code fences, no explanation. Every object must contain an "
    '"index" field whose value is one of the picture numbers shown in the '
    "top-left corner of the images you were given."
)


@dataclass
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = MAX_DELAY_SECONDS
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with jitter.

        Jitter matters when several batches hit a rate limit at once: without
        it they all wake together and trip the same limit again.
        """
        delay = min(self.base_delay_seconds * (2 ** max(0, attempt - 1)), self.max_delay_seconds)
        if self.jitter:
            delay *= 0.5 + random.random()  # noqa: S311 - backoff timing, not security
        return round(min(delay, self.max_delay_seconds), 3)


@dataclass
class AttemptRecord:
    attempt: int
    error: str
    kind: str
    delay_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "error": self.error,
            "kind": self.kind,
            "delay_seconds": self.delay_seconds,
        }


@dataclass
class RetryOutcome:
    result: AnalysisResult | None = None
    history: list[AttemptRecord] = field(default_factory=list)
    gave_up_reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result is not None


def call_with_retries(
    request: AnalysisRequest,
    send: Callable[[AnalysisRequest], AnalysisResult],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RetryOutcome:
    """Send a batch, retrying transient failures and correcting bad JSON once."""
    policy = policy or RetryPolicy()
    outcome = RetryOutcome()
    schema_corrections = 0
    current = request

    for attempt in range(1, policy.max_attempts + 1):
        try:
            outcome.result = send(current)
        except (SchemaValidationError, AlignmentError) as error:
            message = redacted_exception_text(error)

            if schema_corrections >= SCHEMA_CORRECTION_ATTEMPTS:
                # A model that ignored the correction once will ignore it again,
                # and every attempt is billed.
                outcome.history.append(AttemptRecord(attempt, message, "schema"))
                outcome.gave_up_reason = (
                    f"The answer could not be read even after asking again. {message}"
                )
                return outcome

            schema_corrections += 1
            outcome.history.append(AttemptRecord(attempt, message, "schema"))
            logger.info("Asking the model to correct its output format (attempt %d)", attempt)

            from dataclasses import replace

            current = replace(current, prompt=request.prompt + CORRECTION_SUFFIX)
            continue

        except TransientProviderError as error:
            message = redacted_exception_text(error)
            if attempt >= policy.max_attempts:
                outcome.history.append(AttemptRecord(attempt, message, "transient"))
                outcome.gave_up_reason = f"Gave up after {attempt} attempts. {message}"
                return outcome

            delay = policy.delay_for(attempt)
            outcome.history.append(AttemptRecord(attempt, message, "transient", delay))
            logger.info("Retrying in %.1fs after a temporary problem (attempt %d)", delay, attempt)
            sleep(delay)
            continue

        except PermanentProviderError as error:
            # Retrying a bad key or an unknown model just spends time and money.
            message = redacted_exception_text(error)
            outcome.history.append(AttemptRecord(attempt, message, "permanent"))
            outcome.gave_up_reason = message
            return outcome

        else:
            outcome.result.retry_history = [r.as_dict() for r in outcome.history]
            return outcome

    outcome.gave_up_reason = outcome.gave_up_reason or "Ran out of attempts."
    return outcome


def skips_for(request: AnalysisRequest, reason: str, attempts: int) -> list[SkipRecord]:
    """Turn a failed batch into visible gaps rather than silent absence."""
    return [
        SkipRecord(index=frame.index, reason=reason, attempts=attempts, permanent=True)
        for frame in request.frames
    ]


# ── Fallback policy ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FallbackPolicy:
    """Off by default; never silent; never local-to-cloud automatically."""

    enabled: bool = False
    order: tuple[str, ...] = ()
    allow_local_to_cloud: bool = False

    def next_provider(self, current: str) -> str | None:
        """The next provider to try, or None when there is none.

        Returns None for a local-to-cloud move unless the user has explicitly
        configured it. Falling back automatically would send frames off the
        machine after the user chose to keep them on it — a silent reversal of
        the one decision this provider exists to honour.
        """
        if not self.enabled or not self.order:
            return None

        from app.providers.costs import NO_CHARGE_PROVIDERS

        try:
            position = self.order.index(current)
        except ValueError:
            return None

        for candidate in self.order[position + 1 :]:
            moving_off_device = (
                current in NO_CHARGE_PROVIDERS and candidate not in NO_CHARGE_PROVIDERS
            )
            if moving_off_device and not self.allow_local_to_cloud:
                logger.info(
                    "Not falling back from %s to %s: that would send frames off this "
                    "computer, which needs to be turned on deliberately.",
                    current,
                    candidate,
                )
                return None
            return candidate

        return None
