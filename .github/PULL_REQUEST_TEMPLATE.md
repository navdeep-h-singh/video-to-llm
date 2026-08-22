## What this changes, and why

<!-- The why matters more than the what — the diff already says what. -->

## How you know it works

<!--
Not "tests pass". What did you run, and what did you see? A behaviour change
that no test would have caught before this PR needs a test that catches it now.
-->

## Checklist

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy app`
- [ ] `uv run pytest -q --deselect tests/integration/test_live_ollama.py`
- [ ] `uv run video-to-llm smoke-test`

Run each as its own command, not through a pipe — `ruff check . | tail -1`
reports `tail`'s exit status, and that has already let three unfixed findings
through here once.

## If this touches a claim

The README, the skill, and `server.json` are all tested against the software
they describe. If this changes a command, a flag, or a promise, the file that
states it changes in the same commit.

- [ ] Nothing here makes the README, `SKILL.md`, or the docs untrue
