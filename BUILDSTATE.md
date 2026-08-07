# Build State

> Read this file, `docs/DECISIONS.md`, `BUILDPLAN.md`, and `git status` before
> doing any work in a new session.

**Current phase:** 3 — Stages 1–2 (frames + local transcription) on a synthetic CPU path
**Overall:** 2 of 9 phases complete

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

### Phase 2 — Core ✅ (commits `6e9dbcc`, `c196698`, `9993f5f`)
- `app/core/config.py` — settings resolution, `BIND_HOST` constant, numeric
  loopback checking, sampling validation
- `app/core/logging.py` — redacting handlers, third-party loggers reparented
- `migrations/001_initial.sql` — 10 tables with status vocabularies as CHECKs
- `app/core/db.py` — WAL, forward-only migrations, `complete_statement` splitting
- `app/core/artifacts.py` — atomic write, checksums, temp cleanup, registration
- `app/core/locks.py` — file lock + DB claim, 120 s stale window
- `app/worker/reconcile.py` — startup repair; never resets a completed batch
- `app/worker/runner.py` — durable loop, heartbeat, per-job error isolation
- `app/services/doctor.py` — six checks with plain-language remediation
- `app/services/smoke.py` — 9-check no-network end-to-end
- `app/cli/main.py` — all seven commands
- `app/web/app.py` — app factory asserting the loopback boundary (screens: Phase 7)

## Tests

**246 passing**, 0 failing. ruff clean, mypy clean (22 files), pre-publish
audit clean (51 tracked files), smoke test green (9 checks).

- `tests/unit/test_redaction.py` — 37
- `tests/unit/test_repository_hygiene.py` — 14
- `tests/unit/test_config.py` — 56
- `tests/unit/test_db.py` — 46
- `tests/unit/test_artifacts.py` — 20
- `tests/unit/test_locks.py` — 20
- `tests/unit/test_reconcile.py` — 23
- `tests/unit/test_worker_and_cli.py` — 30

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

Phase 3 — Stages 1 and 2 on a synthetic CPU path. Build in this order:

1. `tests/fixtures/synthetic.py` — generate tiny videos with FFmpeg (colour
   patterns + tones, known duration) so every later test has real media without
   any personal file entering the repo.
2. `app/pipeline/probe.py` — ffprobe wrapper: duration, container, dimensions,
   audio-stream presence. Subprocess argument arrays, never shell strings.
3. `app/pipeline/fingerprint.py` — SHA-256 of the source, computed before
   acceptance so duplicates are caught before expensive work starts.
4. `app/pipeline/preflight.py` — readable input, supported type, duration, disk
   headroom, duplicate check, output root, tool availability, interval,
   expected frame/batch counts, provider config.
5. `app/pipeline/frames.py` — fixed-interval extraction to clean 1280x720 JPEGs
   (`000047_t092000.jpg`), separate small top-left `IDX 01` provider copies,
   `frames_manifest.json`, and `frame_interval_ms` made immutable once started.
6. `app/pipeline/audio.py` — FFmpeg audio extraction + silence detection (>3 s),
   `silence_windows.json`.
7. `app/pipeline/transcribe.py` — backend resolver (`auto`/`cpu`/`metal`/`cuda`/
   `vulkan`) with mandatory CPU fallback, faster-whisper over non-silent chunks
   with padding, timestamps remapped onto the original timeline, silence markers
   inserted, `transcript.json` + provenance.
8. Wire both stages into `Worker.process_job`, replacing the placeholder.

Test intervals 0.5/1/2/3/5/10, frame index/timestamp mapping, duplicate
handling, CPU fallback when an accelerator is claimed but absent, and timeline
preservation across silence. Keep faster-whisper mocked in unit tests; one
`@pytest.mark.slow` integration test may use the real tiny model.

## Continuation prompt

> Continue the autonomous build of the Video-to-LLM Pipeline from this
> repository root. Read `BUILDSTATE.md`, `docs/DECISIONS.md`, `BUILDPLAN.md`
> and `git status`, then continue from **Next action** above. Commit tested
> work at each phase boundary.
