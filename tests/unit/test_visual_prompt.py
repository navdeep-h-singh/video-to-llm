"""The prompt has to tell the model what `confidence` measures.

The first real workload produced 1,479 descriptions, every one of them Low —
zero Medium, zero High — and that reading was taken as evidence the whole
description feature had failed. It had not. Re-reading the stored batches showed
the extracted content was largely right: the correct instrument, the correct
timeframe, real price levels, and legible on-screen text. Only `confidence` was
worthless.

It was worthless because the prompt defined it as a feeling. "Set confidence to
Low whenever you are unsure" is a rule a careful model satisfies by answering
Low every time, since it is never fully sure of anything. The field carried no
information and, worse, made good output look like bad output.

Anchoring the rubric to legibility — something the model can assess from the
image in front of it — moved the same frames, on the same model at the same
temperature, to Medium and High.

These tests do not call a model. They assert the prompt still contains a
definition of what confidence measures, because that is the property that was
missing, and a prompt is exactly the kind of text that gets tidied by someone
who does not know what one paragraph of it is load-bearing.

A failure here is a regression.
"""

from __future__ import annotations

from app.pipeline.visual import DEFAULT_PROMPT
from app.providers.base import Confidence, _coerce_confidence


def test_the_prompt_defines_every_level_it_offers():
    """Offering three levels and defining one is what produced the flat result."""
    for level in ("High", "Medium", "Low"):
        assert level in DEFAULT_PROMPT, f"{level} is offered but never explained"


def test_confidence_is_anchored_to_something_observable():
    """The rubric has to point at the picture, not at the model's feelings.

    Read as a whole sentence rather than a keyword: 'legibility' appearing
    somewhere would also match the old prompt if someone pasted the word in
    without the rule that gives it force.
    """
    assert "Confidence describes legibility, not how certain you feel in general." in DEFAULT_PROMPT


def test_the_prompt_no_longer_asks_for_low_whenever_unsure():
    """The exact instruction that made every frame Low.

    A model that is never fully sure satisfies this by always answering Low,
    which is what happened 1,479 times out of 1,479.
    """
    assert "Set confidence to Low whenever you are unsure" not in DEFAULT_PROMPT


def test_being_unfamiliar_with_the_subject_does_not_lower_confidence():
    """A clear screenshot of an unfamiliar subject is still a clear screenshot.

    Without this, a model handed a domain it does not know discounts itself for
    the wrong reason — and every video is an unfamiliar domain to a 7B model.
    """
    assert "even if you are unfamiliar with" in DEFAULT_PROMPT


def test_honest_unknowns_are_still_demanded():
    """The rubric change must not have relaxed the rule it sits next to.

    Invariant: `Unknown` is preserved, never guessed. Making confidence more
    generous while also making values more generous would trade one bad signal
    for a worse one.
    """
    assert 'an honest "Unknown" is far more useful than a plausible' in DEFAULT_PROMPT


def test_an_unreadable_confidence_is_still_treated_as_low():
    """The parser's side of the invariant, unchanged by the prompt work.

    Promoting an unparseable confidence to High would quietly turn a guess into
    evidence, which is the failure the coercion exists to prevent.
    """
    assert _coerce_confidence("banana") == Confidence.LOW
    assert _coerce_confidence(None) == Confidence.LOW
    assert _coerce_confidence("High") == Confidence.HIGH
    assert _coerce_confidence("medium") == Confidence.MEDIUM
