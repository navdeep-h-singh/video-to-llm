"""The README must not promise something the software does not do.

This project shipped a README for three sessions claiming "the whole pipeline is
callable from the command line without ever opening the interface" while job
creation existed only as a web form. Nobody noticed, because no test read the
README.

Two kinds of check here, and the split matters:

* **Mechanical.** Every command and flag shown in a shell block has to parse.
  This is checkable, so it is checked.
* **Claims under embargo.** A short list of things that are not true yet — CI
  has never run, no cloud provider has been called live. These are guarded by
  asserting the README does *not* assert them. When one becomes true, delete the
  guard in the same commit that earns it.

A failure here is a regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.cli.main import build_parser

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"


def _readme() -> str:
    assert README.exists()
    return README.read_text(encoding="utf-8")


def _shell_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)


def _invocations(text: str) -> list[list[str]]:
    """Every `video-to-llm …` line in a shell block, as argv."""
    found: list[list[str]] = []
    for block in _shell_blocks(text):
        for raw in block.splitlines():
            line = raw.split("#")[0].strip()
            if not line.startswith("video-to-llm "):
                continue
            # Quoted arguments are just placeholders here; the parser only needs
            # to see that the shape is right.
            found.append([part.strip('"') for part in line.split()[1:]])
    return found


def test_the_readme_shows_commands_at_all():
    """Guards against every other test in this file passing vacuously."""
    assert len(_invocations(_readme())) >= 8


@pytest.mark.parametrize("argv", _invocations(README.read_text(encoding="utf-8")))
def test_every_command_shown_in_the_readme_parses(argv):
    """The exact defect this file exists for."""
    build_parser().parse_args(argv)


#: Things the README must not claim while they remain untrue. Each entry is
#: (pattern, why it is embargoed). Delete an entry in the commit that earns it.
EMBARGOED = [
    (
        re.compile(r"\b(?:verified|tested) (?:live )?against (?:Claude|OpenAI|Gemini)\b", re.I),
        "no cloud provider has ever been called against a real service",
    ),
    (
        re.compile(r"\bproduction[- ]ready\b", re.IGNORECASE),
        "one operator and one real workload is not production-ready",
    ),
    (
        re.compile(r"\baccurate transcription\b|\btranscription is accurate\b", re.IGNORECASE),
        "transcription accuracy is unmeasured and Whisper hallucinates on silence",
    ),
]


@pytest.mark.parametrize(("pattern", "reason"), EMBARGOED)
def test_the_readme_does_not_claim_what_is_not_true_yet(pattern, reason):
    match = pattern.search(_readme())
    assert match is None, f"README claims {match.group(0)!r}, but {reason}"


def test_the_limitations_section_survives():
    """The most valuable section in the file, and the first one a keen editor
    deletes. It is what makes everything above it believable."""
    text = _readme()
    assert "## Known limitations" in text
    for required in (
        "Transcription accuracy is unmeasured",
        # Was "never executed this code" until CI ran green on all three
        # operating systems. The limitation did not disappear when that
        # happened, it narrowed: a clean runner passing is not somebody's real
        # machine passing, and the README has to keep saying so.
        "Nobody has used this on Windows or Linux",
        "No cloud provider has been exercised against a live service",
    ):
        assert required in text, f"the limitations section no longer mentions: {required}"


def test_the_privacy_section_states_mechanisms_not_adjectives():
    """ "Private" is a claim. "Binds 127.0.0.1, asserted at construction" is a
    fact someone can check, and it is the one this project can actually make."""
    text = _readme()
    assert "asserted at application construction" in text
    assert "No plaintext fallback is ever created" in text
