"""Token estimation.

Every number this produces is an **estimate**, and the interface says so
wherever one appears. Real tokenisation is provider- and model-specific, and a
figure presented as exact would be trusted for packing decisions it cannot
support.

The method is deliberately simple and documented rather than clever: a character
ratio, calibrated for English prose mixed with structured field text, which is
what an assembled document actually contains. A more elaborate estimator would
still be wrong for some model and would be harder to explain when a pack turned
out slightly over.
"""

from __future__ import annotations

from dataclasses import dataclass

ESTIMATION_METHOD = "characters/3.6 (English prose with structured fields)"
ESTIMATION_VERSION = 1

#: Characters per token. GPT-family tokenisers average ~4.0 for plain English
#: prose; assembled documents run denser because of timestamps, numbers, and
#: repeated field labels, so 3.6 errs slightly high. Over-estimating makes packs
#: a little smaller than they need to be, which is the safe direction — the
#: alternative is a pack that will not fit.
CHARS_PER_TOKEN = 3.6

#: Rough per-video overhead for the boundary markers and metadata a pack adds.
PACK_OVERHEAD_TOKENS = 200


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    characters: int
    method: str = ESTIMATION_METHOD
    version: int = ESTIMATION_VERSION

    @property
    def label(self) -> str:
        return f"about {self.tokens:,} tokens"

    @property
    def disclaimer(self) -> str:
        return (
            "Sizes are estimates. The exact count depends on the model you use, "
            "so treat these as a guide rather than a guarantee."
        )


def estimate_tokens(text: str) -> TokenEstimate:
    characters = len(text)
    return TokenEstimate(
        tokens=max(0, round(characters / CHARS_PER_TOKEN)),
        characters=characters,
    )


def estimate_for_texts(texts: list[str]) -> TokenEstimate:
    total = sum(len(t) for t in texts)
    return TokenEstimate(tokens=max(0, round(total / CHARS_PER_TOKEN)), characters=total)


def fits(estimate: TokenEstimate | int, budget: int) -> bool:
    tokens = estimate.tokens if isinstance(estimate, TokenEstimate) else estimate
    return tokens <= budget
