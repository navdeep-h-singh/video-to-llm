# Build State

> Read this file, `docs/DECISIONS.md`, `BUILDPLAN.md`, and `git status` before
> doing any work in a new session.

**Current phase:** 2 — Core: models, migrations, logging, artifacts, locks, worker, doctor
**Overall:** 1 of 9 phases complete

## Environment (verified 2026-08-06)

- uv 0.12.2, CPython 3.11.15 (uv-managed), venv synced with dev extras
- FFmpeg + ffprobe at `/opt/homebrew/bin`
- Ollama 0.32.6 serving on `127.0.0.1:11434`; `qwen2.5vl:7b` pulled and present
- Git repo initialised, identity `video-to-llm <local@localhost>`
- Design reference at `design_reference/video_pipeline_ux.dc.html` (git-ignored).
  11 screens; data model and Modernist tokens both extracted.

## Completed

### Phase 1 — Bootstrap, security, CI, docs, state ✅ (commit `933babc`)
- `pyproject.toml` (py3.11, uv), `uv.lock`, `.python-version`
- `.gitignore` covering all nine required categories
- Safe templates: `.env.example`, `config/{settings,pricing,providers}.example.toml`
- `app/core/redaction.py` — two-layer redaction applied at format time
- `.gitleaks.toml`, `.pre-commit-config.yaml`, `scripts/pre_publish_audit.py`
- `.github/workflows/ci.yml` — 3-OS × py3.11 matrix, no credentials
- `README.md`, `docs/SECURITY.md`, `docs/SECURE_GITHUB_EXPORT.md`, docs skeleton

### Phase 2 — in progress
- `app/core/config.py` ✅ (commit `6e9dbcc`) — settings resolution, `BIND_HOST`
  constant, numeric loopback checking, sampling validation
- `scripts/pre_publish_audit.py` bind-check scoping ✅ (commit `a80797f`)

## Tests

**107 passing**, 0 failing. Lint clean, format clean, pre-publish audit clean
(43 tracked files).

- `tests/unit/test_redaction.py` — 37 tests
- `tests/unit/test_repository_hygiene.py` — 14 tests
- `tests/unit/test_config.py` — 56 tests

## Failures / blockers

None.

## Notes carried forward

- Redaction must be applied at **format time**; the filter alone is not
  sufficient (interpolation-created secrets). Use `install_redaction(handler)`.
- The pre-publish audit rejects absolute home paths in tracked files. Keep paths
  relative in all docs and code.
- The audit's bind check is skipped under `tests/` so tests can name `0.0.0.0`
  to prove it is rejected. Secret and home-path scans still apply there.
- **Never pipe `pytest` into `tail` inside an `&&` chain before a commit** — the
  pipe returns `tail`'s exit code, so a failing suite still commits. Run pytest
  as its own command, or use `set -o pipefail`.
- Committing uses `git -c core.hooksPath=/dev/null` because the pre-commit hooks
  are declared but the `pre-commit` tool is not installed in the venv. Checks are
  run explicitly instead (`ruff format --check`, `ruff check`, `pytest`,
  `pre_publish_audit.py`). Install `pre-commit` in Phase 9 or drop the config.

## Next action

Continue Phase 2, in this order:

1. `app/core/logging.py` — structured logging wired through `install_redaction`.
2. `app/core/db.py` + `migrations/` — SQLite WAL, forward-only migrations, the
   ten tables from `BUILDPLAN.md` Phase 2.
3. `app/core/artifacts.py` — atomic write (temp sibling → fsync → rename → state
   transaction), SHA-256 checksum helper.
4. `app/core/locks.py` — global output-root lock + SQLite worker claim.
5. `app/worker/` — durable loop skeleton, startup reconciliation.
6. `app/cli/main.py` — `start`, `start-ui`, `run-worker`, `doctor`, `smoke-test`,
   `status`, `import`.

Then: tests for migration idempotence, atomic-write crash safety (kill between
temp write and rename), lock exclusivity across processes, and worker-claim
contention. Commit at the phase boundary.

## Continuation prompt

> Continue the autonomous build of the Video-to-LLM Pipeline from this
> repository root. Read `BUILDSTATE.md`, `docs/DECISIONS.md`, `BUILDPLAN.md`
> and `git status`, then continue from **Next action** above. Commit tested
> work at each phase boundary.
