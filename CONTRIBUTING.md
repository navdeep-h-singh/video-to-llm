# Contributing

The most useful thing you can send right now is **a bug report from actually
running this**, especially on Windows or Linux. The entire project was built on
macOS and, at the time of writing, no other platform has executed a line of it.

## Getting set up

```bash
git clone <this repository>
cd video-to-llm
uv sync --extra dev
uv run video-to-llm doctor
```

Then the whole gate, which is what CI runs:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app
uv run pytest -q --deselect tests/integration/test_live_ollama.py
uv run python scripts/pre_publish_audit.py
uv run video-to-llm smoke-test
```

`smoke-test` runs the pipeline end to end on generated media and touches no
network.

## How this codebase works

A few conventions that are not obvious from reading one file, and that reviews
will hold you to. They exist because each one was learned the expensive way.

**Run it, don't just read it.** Every defect that mattered in this project's
history was found by using the application, with the full suite green. If you
change behaviour, drive it.

**Write the test that catches the class, not the instance.** Then revert your
fix and watch the test fail. This practice has repeatedly caught tests that
would otherwise have passed vacuously.

**Assert the whole value, not a substring.** `"left" in body` passed against
`"about about 7½ hours left"`. `label in rendered` passed against a drift from
`"Google"` to `"Google Gemini"`, because one contains the other.

**Assert the subject exists before asserting about it.** Two tests once created
jobs against a nonexistent path; preflight refused before any row was written,
so they asserted on nothing at all.

**Never let a test touch the real machine.** Fake the keyring, redirect the
settings file with `VIDEO_TO_LLM_CONFIG_FILE`, use `tmp_path`. One early test
wrote a real credential into the author's login keychain.

**Never weaken a check to make it pass.** When a check fires on something
legitimate, make it *more precise*. A guard relaxed once stops being a guard.

**A control that changes no behaviour is a lie.** Empty states say they are
empty; nothing is invented to fill a screen.

Comments explain *why* — the failure being prevented — not what the line does.
Commit messages do the same.

## Things that are load-bearing

Several properties are the product, not implementation details. Each is tested.
If your change touches one, say so explicitly in the pull request.

1. The interface binds loopback only, asserted at application construction.
2. Cross-origin writes and `/api/` reads are refused.
3. A completed provider batch is never re-sent, across attempts and not merely
   within one.
4. The budget is checked *before* a request is sent, never after.
5. Local never automatically falls back to a cloud provider.
6. Stopping never destroys finished work. Cancel is not undo.
7. `Unknown` is preserved, never guessed.
8. Order is never inferred from filename, date, or content.
9. A stored key is never rendered back, not even a prefix.
10. Status is text *and* shape *and* colour — never colour alone.

## Pull requests

Small and focused beats large and comprehensive. Explain the failure your change
prevents. Run the gate before pushing.

If you are adding a command, add it to `DOCUMENTED_COMMANDS` in
`tests/unit/test_headless_cli.py` — the parser and the documentation are pinned
to each other on purpose, because they drifted for three sessions once.

## Reporting a bug

Include the output of `video-to-llm doctor`, your operating system and Python
version, and what you expected instead. If it involves a specific video, its
length and container matter more than its contents.

Security issues: see [`SECURITY.md`](.github/SECURITY.md) — please do not open a
public issue.
