#!/usr/bin/env python3
"""Fail when the README's test-count badge disagrees with the suite.

A number in a badge is a claim like any other, and this project's argument is
that its claims are checkable. The count went stale once — the badge said 1,421
while the suite collected 1,498 — which is how a badge quietly stops meaning
anything. Now CI counts and compares, so it cannot drift again.

Counts by collecting, not by running: collection is fast and the number in the
badge is "how many tests there are", not "how many passed".

    uv run python scripts/check_test_badge.py           # verify
    uv run python scripts/check_test_badge.py --write   # verify, updating README
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

#: The badge, with the count as its one capture group. Shields.io writes a comma
#: as `%2C`, so the digits are not contiguous in the URL.
BADGE = re.compile(r"(?P<prefix>badge/tests-)(?P<count>[\d%2C]+?)(?P<suffix>-brightgreen)")

COLLECTED = re.compile(r"^(\d+) tests collected", re.MULTILINE)


def collected_count() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    match = COLLECTED.search(result.stdout)
    if match is None:
        print("Could not read a test count from pytest:", file=sys.stderr)
        print(result.stdout[-2000:], file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(2)
    return int(match.group(1))


def badged(text: str) -> tuple[int, re.Match[str]]:
    match = BADGE.search(text)
    if match is None:
        print("No test-count badge found in README.md.", file=sys.stderr)
        raise SystemExit(2)
    return int(match.group("count").replace("%2C", "")), match


def main(argv: list[str]) -> int:
    writing = "--write" in argv
    text = README.read_text(encoding="utf-8")
    claimed, match = badged(text)
    actual = collected_count()

    if claimed == actual:
        print(f"README badge agrees with the suite: {actual:,} tests.")
        return 0

    formatted = f"{actual:,}".replace(",", "%2C")
    if not writing:
        print(
            f"README badge says {claimed:,} tests; the suite collects {actual:,}.\n"
            f"Run `uv run python scripts/check_test_badge.py --write` to correct it.",
            file=sys.stderr,
        )
        return 1

    updated = text[: match.start()] + f"badge/tests-{formatted}-brightgreen" + text[match.end() :]
    updated = updated.replace(f'alt="{claimed:,} tests"', f'alt="{actual:,} tests"')
    README.write_text(updated, encoding="utf-8")
    print(f"README badge updated: {claimed:,} → {actual:,} tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
