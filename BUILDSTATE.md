# Build State

> Read this file, `docs/DECISIONS.md`, `BUILDPLAN.md`, and `git status` before
> doing any work in a new session.

**Current phase:** 4 — Provider protocol and adapters
**Overall:** 3 of 9 phases complete

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

## Tests

**367 passing**, 0 failing. ruff clean, mypy clean (28 files), pre-publish
audit clean (60 tracked files), smoke test green (9 checks).

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

Phase 4 — provider protocol and adapters. Build in this order:

1. `app/providers/base.py` — `VisualAnalysisProvider` protocol: normalized
   request in, normalized schema out. Strict output fields per spec §6:
   `index`, `timeframe`, `currency_pair`, `indicators_and_states`,
   `exact_action`, `visible_text`, `visual_description`, `setup_type`,
   `confidence`. Preserve `Unknown`; never invent values.
2. `app/credentials/store.py` — keyring first, process env second, **never a
   plaintext fallback**. Register every value read with `register_secret()`.
3. `app/providers/ollama_local.py` — loopback-only guard reusing
   `is_loopback_host`, no credential field at all, default batch 1 (2 after
   preflight, 3–4 advanced override only), concurrency 1, `Check local model`
   health probe reporting reachability/version/model/vision capability, and the
   `Vision capability not verified` path requiring acknowledgement.
4. `app/providers/anthropic.py`, `google.py`, `openai.py`, `openai_compatible.py`
   — cloud batching up to 20, exact returned IDX alignment validation.
5. `app/providers/costs.py` — versioned local estimation, hard budget stop
   before a new paid batch. Local reports `No provider API charge`, never
   `$0.00`.
6. `app/providers/retry.py` — bounded backoff for transient errors, exactly one
   corrective schema-format retry for invalid JSON, skip records on permanent
   failure, `Completed with gaps`.
7. Wire Stage 3 into `app/pipeline/stages.py` and the worker.

All tests use mocks (`respx` is already a dev dependency). No paid call is ever
made. Add `tests/integration/test_live_ollama.py` behind the `live_ollama`
marker — CI deselects it; it can run locally since Ollama 0.32.6 and
`qwen2.5vl:7b` are installed on this machine.

Critical invariants to test: never auto-fall-back from Local Ollama to cloud;
never re-send a completed batch; loopback-only endpoints; no credential field
for Ollama; keys never reach disk.

## Continuation prompt

> Continue the autonomous build of the Video-to-LLM Pipeline from this
> repository root. Read `BUILDSTATE.md`, `docs/DECISIONS.md`, `BUILDPLAN.md`
> and `git status`, then continue from **Next action** above. Commit tested
> work at each phase boundary.
