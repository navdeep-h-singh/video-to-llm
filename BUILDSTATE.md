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

## Tests

**51 passing**, 0 failing. Lint clean, format clean, pre-publish audit clean.

- `tests/unit/test_redaction.py` — 37 tests
- `tests/unit/test_repository_hygiene.py` — 14 tests

## Failures / blockers

None.

## Notes carried forward

- Redaction must be applied at **format time**; the filter alone is not
  sufficient (interpolation-created secrets). Use `install_redaction(handler)`.
- The pre-publish audit rejects absolute home paths in tracked files. Keep paths
  relative in all docs and code.

## Next action

Phase 2. Build in this order:

1. `app/core/config.py` — settings load/merge (defaults → toml → env), loopback
   binding enforced in code, output-root resolution.
2. `app/core/logging.py` — structured logging wired through `install_redaction`.
3. `app/core/db.py` + `migrations/` — SQLite WAL, forward-only migrations, the
   ten tables from `BUILDPLAN.md` Phase 2.
4. `app/core/artifacts.py` — atomic write (temp sibling → fsync → rename → state
   transaction), checksum helper.
5. `app/core/locks.py` — global output-root lock + SQLite worker claim.
6. `app/worker/` — durable loop skeleton, startup reconciliation.
7. `app/cli/main.py` — `start`, `start-ui`, `run-worker`, `doctor`, `smoke-test`,
   `status`, `import`.

Then: tests for migrations, atomic-write crash safety, lock exclusivity, and
loopback binding. Commit at the phase boundary.

## Continuation prompt

> Continue the autonomous build of the Video-to-LLM Pipeline from this
> repository root. Read `BUILDSTATE.md`, `docs/DECISIONS.md`, `BUILDPLAN.md`
> and `git status`, then continue from **Next action** above. Commit tested
> work at each phase boundary.
