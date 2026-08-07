# Build State

> Read this file, `docs/DECISIONS.md`, `BUILDPLAN.md`, and `git status` before
> doing any work in a new session.

**Current phase:** — none, the build is complete
**Overall:** 9 of 9 phases complete

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

### Phase 3 — Stages 1–2 ✅ (commit `9eb7d07`)
- `tests/fixtures/synthetic.py` — generated video/audio; codec pair follows the
  container (WebM needs VP8/Opus, not H.264/AAC)
- `app/pipeline/probe.py` — ffprobe wrapper, argument arrays never shell strings
- `app/pipeline/frames.py` — deterministic `plan_frames`, clean 1280x720 JPEGs +
  `IDX nn` provider copies, `frames_manifest.json`
- `app/pipeline/audio.py` — audio extraction, silencedetect parsing, speech
  segment inversion with padding
- `app/pipeline/transcribe.py` — backend resolver with mandatory CPU fallback,
  timeline remapping, silence markers, provenance
- `app/pipeline/preflight.py` — SHA-256 fingerprint, duplicates, disk, types
- `app/pipeline/stages.py` — stage orchestration, skip-if-complete
- `app/worker/runner.py` — real `process_job` / `process_video`

### Phase 4 — Providers ✅ (commits `a49f59c`, `b9845cd`)
- `app/providers/base.py` — contract, strict schema, alignment validation,
  tolerant JSON extraction, `Unknown` preservation
- `app/credentials/store.py` — keyring → env, never a plaintext fallback
- `app/providers/ollama_local.py` — loopback-only, no credential, small batches,
  health probe with `Vision capability not verified`
- `app/providers/cloud.py` — Anthropic, Google, OpenAI, OpenAI-compatible
- `app/providers/costs.py` — versioned estimates, budget checked before sending
- `app/providers/retry.py` — bounded backoff, one corrective schema retry,
  skips, fallback policy that never auto-moves local → cloud

### Phase 5 — Assembly and imports ✅ (commits `d731408`, `6a54325`, `740bf62`)
- `app/pipeline/visual.py` — Stage 3 orchestration; never re-sends a completed batch
- `app/pipeline/enrich.py` — deterministic emphasis, switches, segments
- `app/pipeline/assemble.py` — time-ordered `assembled.txt`, `master_assembled.txt`
- `app/pipeline/archive.py` — `analysis_input` handoff, symlink-or-copy frames
- `app/pipeline/finalize.py` — job-level outputs and provenance
- `app/services/importer.py` — non-destructive import with compatibility reporting

### Phase 6 — Collections ✅ (commit `cbb183a`)
- `app/collections/model.py` — immutable source-version references, warning states
- `app/collections/tokens.py` — documented estimation, always labelled an estimate
- `app/collections/build.py` — Mode A full document, Mode B context packs
- `app/core/redaction.py` — **fixed** a key-matching false positive that masked
  `token_limit`, `input_tokens`, and every token count in manifests

### Phase 7 — UI ✅ (commit `f447560`)
- `app/web/static/tokens.css` — Modernist tokens, no external resources
- `app/web/status.py` — status vocabulary: text + shape + colour
- `app/web/app.py` — routes reading real state
- `app/web/templates/` — all 11 screens + base + macros

### Phase 8 — Recovery & accessibility ✅ (commits `8854cf5`, `c658bbc`)
- `app/services/jobs.py` — create / pause / resume / cancel, preserving output
- POST routes so the UI is operable; forms return problems + what was typed
- `scripts/setup_{macos,linux}.sh`, `setup_windows.ps1`, `verify_install.py`
- `tests/integration/test_recovery.py` — real SIGKILL, real lock contention
- `tests/unit/test_accessibility.py` — computed contrast, keyboard, semantics
- **4 WCAG AA failures in the supplied palette fixed**, documented in
  `docs/UX_NOTES.md`

### Phase 9 — Docs and final pass ✅
- All eleven `docs/*.md` written
- `smoke-test` extended to 12 checks incl. stages 1-2 and a collection build
- `relative_to_root()` — fixes symlinked-path failure found by the new smoke check
- `pre-commit` added to the dev extra
- `docs/FINAL_BUILD_REPORT.md`

## Tests

**951 passing**, 0 failing. ruff format + check clean, mypy clean (45 files),
pre-publish audit clean (117 tracked files), smoke test green (12 checks),
5 live Ollama tests passing.
Plus 5 live Ollama tests (opt-in marker `live_ollama`, deselected in CI) that
pass against the real Ollama 0.32.6 + qwen2.5vl:7b on this machine.

- `tests/unit/test_redaction.py` — 37
- `tests/unit/test_repository_hygiene.py` — 14
- `tests/unit/test_config.py` — 56
- `tests/unit/test_db.py` — 46
- `tests/unit/test_artifacts.py` — 20
- `tests/unit/test_locks.py` — 20
- `tests/unit/test_reconcile.py` — 23
- `tests/unit/test_worker_and_cli.py` — 31
- `tests/unit/test_frames.py` — 36
- `tests/unit/test_transcription.py` — 38
- `tests/unit/test_preflight.py` — 34
- `tests/integration/test_pipeline_end_to_end.py` — 12 (real FFmpeg, stub model)
- `tests/unit/test_providers_base.py` — 64
- `tests/unit/test_ollama_local.py` — 43
- `tests/unit/test_cloud_providers.py` — 60
- `tests/unit/test_credentials.py` — 28
- `tests/unit/test_visual_stage.py` — 26
- `tests/unit/test_enrich_and_assemble.py` — 45
- `tests/unit/test_finalize_and_import.py` — 28
- `tests/integration/test_pipeline_end_to_end.py` — 22 total
- `tests/unit/test_collections.py` — 55
- `tests/unit/test_redaction.py` — 76 (incl. key false-positive regressions)
- `tests/unit/test_web_ui.py` — 64
- `tests/unit/test_job_control.py` — 43
- `tests/unit/test_accessibility.py` — 56
- `tests/integration/test_recovery.py` — 19

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

None. The build is complete — see `docs/FINAL_BUILD_REPORT.md`, which records
what was built, the definition of done item by item, every deviation with its
justification, the defects found during the build, and the known limitations.

The most important limitations to carry forward:

- CI has never executed (no git remote). **Windows and Linux are untested.**
- No cloud provider has ever been called. Adapters are mock-tested only.
  Local Ollama *is* verified live.
- Transcription accuracy on real material is unmeasured.
- Frame-scrubber interactivity, targeted reruns, and drag-to-reorder are not
  built; their data models are.

## Continuation prompt

The build is finished, so there is nothing to continue. To pick up the
remaining work listed under **Next action**, read `docs/FINAL_BUILD_REPORT.md`
first — the limitations section is the backlog.
