# Build Plan

Ordered phases, following spec §12. Each phase ends with: tests green, docs and
`BUILDSTATE.md` updated, and a local git commit. No phase is marked complete on
partial work.

---

## Phase 1 — Bootstrap, security, CI, docs, state
- `pyproject.toml`, `uv.lock`, `.python-version` (3.11).
- Conservative `.gitignore` (spec §10): `.env` except `.env.example`, keys/certs,
  db/WAL, logs/runtime, models/cache, source media, images/audio,
  processed/output/export/collection data, OS files.
- Safe templates only: `.env.example`, `config/settings.example.toml`,
  `config/pricing.example.toml`, `config/providers.example.toml`.
- Central secret redaction module + its tests.
- gitleaks config, pre-commit hooks, `scripts/pre_publish_audit.py`.
- GitHub Actions 3-OS × py3.11 matrix workflow.
- `README.md`, `docs/` skeleton, `BUILDSTATE.md`.

## Phase 2 — Core: models, migrations, logging, artifacts, locks, worker, doctor
- SQLite WAL + forward-only migrations. Tables: `jobs`, `job_videos`,
  `stage_runs`, `batches`, `artifacts`, `events`, `collections`,
  `collection_sources`, `collection_builds`, `schema_migrations`.
- Atomic artifact writer: temp sibling → flush/fsync → atomic rename → state txn.
- Global output-root lock + SQLite worker claim (one worker per output root).
- Structured logging with redaction filter wired in.
- Durable worker loop skeleton; startup reconciliation of state vs artifacts.
- `video-to-llm` CLI: `start`, `start-ui`, `run-worker`, `doctor`, `smoke-test`,
  `status`, `import <path>`.

## Phase 3 — Stages 1–2 (synthetic CPU path)
- Stage 1: preflight, SHA-256 fingerprint + duplicate detection, ffprobe probing,
  fixed-interval extraction, clean 1280×720 JPEGs, separate `IDX nn` provider
  copies, `frames_manifest.json`, immutable `frame_interval_ms`.
- Stage 2: FFmpeg audio extract, silence detection (>3 s), backend resolver
  (`auto`/`cpu`/`metal`/`cuda`/`vulkan`) with mandatory CPU fallback,
  faster-whisper transcription preserving the original timeline, silence markers,
  `silence_windows.json`, `transcript.json`, full provenance.
- Synthetic fixture generator; end-to-end CPU run on generated media.

## Phase 4 — Provider protocol and adapters
- `VisualAnalysisProvider` protocol: normalized request in, normalized schema out.
- Adapters: Anthropic Claude, Google Gemini, OpenAI, OpenAI-compatible advanced,
  Local Ollama (loopback-only, no credential field, `Local / Experimental`).
- Strict output schema, exact IDX alignment validation, batch policies
  (cloud ≤20, local default 1 / max 2 after preflight / 3–4 advanced override).
- Credential store abstraction (Keychain / Credential Manager / Secret Service),
  env fallback, never a plaintext fallback.
- Cost estimation, hard budget stop, bounded retry/backoff, one corrective
  schema retry, skip records, `Completed with gaps`.
- "Check local model" health probe; `Vision capability not verified` path.
- All tests against mocks; live Ollama check as a separate opt-in verification.

## Phase 5 — Enrichment, assembly, archives, imports, reruns
- Deterministic local enrichment: emphasis, timeframe switches, time-window
  segment classification. No external text model.
- Chronological assembly → `assembled.txt`; `master_assembled.txt` only for
  multi-video jobs in confirmed order.
- Per-video permanent archive; per-job `analysis_input` with frame-mapping README.
- Import of earlier processed output; versioned explicit reruns that never
  overwrite prior expensive output.

## Phase 6 — Collections
- `collections` domain model, immutable source-version references.
- Mode A: full `collection_assembled.txt` with `<video sequence=…>` boundaries,
  `collection_manifest.json`, `collection_readme.md`, frame references.
- Mode B: context-window packs, whole-video-first boundaries, opt-in splitting at
  segment boundaries with explicit overlap, `collection-pack-manifest.json`.
- Checksums throughout; zero Stage 3 calls; symlink/reference reuse of frames.

## Phase 7 — UI
- Port Modernist tokens; implement all 11 screens from the supplied design:
  first-run readiness, dashboard, new job, job detail, review workspace,
  outputs/export, imports, settings & diagnostics, collection list,
  create-collection flow, collection detail / output packs.
- Plain-language copy from the design (no API terminology before opt-in).
- Status vocabulary: text + icon + colour for all ten states.
- Never reveal stored keys.

## Phase 8 — Recovery, accessibility, cross-platform, smoke
- Pause/resume/cancel, crash/sleep recovery, safe temp cleanup.
- WCAG 2.2 AA, full keyboard operation, 1024 px graceful degradation.
- `setup_macos.sh`, `setup_windows.ps1`, `setup_linux.sh`, `verify_install.py`.
- No-network synthetic smoke suite.

## Phase 9 — Quality, security, docs, final report
- Format/lint/type clean, full test suite, secret scan, pre-publish audit.
- All `docs/` written, `docs/FINAL_BUILD_REPORT.md`, final commit.
