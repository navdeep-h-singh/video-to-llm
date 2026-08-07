# Final build report

**Video → LLM** — a localhost-only pipeline turning local videos into
timestamped, reviewable evidence and ordered, LLM-ready collections.

Built in nine phases against `localhost_video_to_llm_with_ollama_and_collections_build_spec.md`,
using the supplied Claude Design output as the visual source of truth.

---

## State at completion

| | |
|---|---|
| Tests | **951 passing**, 0 failing, 5 deselected (opt-in live Ollama) |
| Format | `ruff format --check` — 87 files clean |
| Lint | `ruff check` — clean |
| Types | `mypy app` — clean, 45 source files |
| Publishing audit | Clean, 117 tracked files |
| Smoke test | 12 checks, no network |
| Live Ollama | 5 tests passing against Ollama 0.32.6 + qwen2.5vl:7b |
| Commits | 22, each with tests green |

Reproduce all of it:

```bash
uv sync --extra dev
uv run ruff format --check . && uv run ruff check . && uv run mypy app
uv run pytest -q --deselect tests/integration/test_live_ollama.py
uv run python scripts/pre_publish_audit.py
uv run video-to-llm smoke-test
```

---

## Definition of done — spec §13, item by item

| Requirement | Status | How to verify |
|---|---|---|
| Localhost-only binding | ✅ | `tests/unit/test_config.py`; verified live — the interface answers on `127.0.0.1` and refuses the machine's LAN address |
| Three-OS CI | ✅ written, ⚠️ never executed | `.github/workflows/ci.yml` — macOS/Windows/Ubuntu × 3.11. No git remote exists, so it has never run. See *Limitations*. |
| Local-only processing without an API key | ✅ | `smoke-test`; `test_a_local_only_job_makes_no_provider_call` monkeypatches `build_provider` to raise |
| Optional secure cloud providers | ✅ | `tests/unit/test_cloud_providers.py`, `test_credentials.py` |
| Local Ollama, loopback-only, no key, experimental | ✅ | `tests/unit/test_ollama_local.py` (43); `tests/integration/test_live_ollama.py` (5, live) |
| Durable recovery | ✅ | `tests/integration/test_recovery.py` — real `SIGKILL`, real cross-process lock contention |
| Collection assembly and packs | ✅ | `tests/unit/test_collections.py` (55) |
| Clear provenance / gap / recovery UI | ✅ | `tests/unit/test_web_ui.py` (64) |
| Design and accessibility compliance | ✅ with 4 documented fixes | `tests/unit/test_accessibility.py` (56); `docs/UX_NOTES.md` |
| Secret safety | ✅ | `tests/unit/test_redaction.py` (76), `test_repository_hygiene.py` |
| Tests, scans, smoke checks | ✅ | commands above |
| `docs/FINAL_BUILD_REPORT.md` | ✅ | this file |

---

## Deviations from the supplied design

The specification sets the precedence: the design governs unless it conflicts
with security, reliability, accessibility, localhost-only, or functional
requirements. Four departures, each in one of those categories, each tested so it
cannot quietly revert. Full detail in `docs/UX_NOTES.md`.

1. **No web font.** The design imports Archivo from a font service. Every page
   promises "nothing is uploaded"; a CDN request would contradict that on every
   page load. Falls back to the system UI font.

2. **Filled buttons use `accent-700`.** `#ec3013` on the page background is
   3.76:1 — fine for a border or a mark, below the 4.5:1 a 14px label needs.
   `accent-700` gives 6.41:1. The brand accent keeps every non-text role.

3. **Muted text at 65%, not 55%.** 55% is 3.66:1. Muted text is still text.

4. **Hollow status markers use `neutral-600`.** `neutral-500` is 2.59:1 against
   a 3:1 requirement for a mark that carries meaning.

Contrast is computed from the palette in the tests, and one test asserts the
stylesheet still defines the exact colours those tests assume — so the two cannot
drift apart.

---

## Defects found and fixed during the build

Each was found by a check rather than by reading the code.

**Redaction masked ordinary data.** Sensitive key names were matched by bare
substring, so `token` also matched `token_limit`, `input_tokens`, and
`token_method_version` — every token count written into a manifest came out as
`[redacted]`. Masking ordinary data is not a safe failure: it silently corrupts
output while protecting nothing. Matching is now anchored to name segments, with
bare `token` handled as an exact-whole-key match. 16 regression tests for keys
that must survive, 22 for keys that must still be masked.

**Redaction at filter time was insufficient.** `logger.info("key=sk-ant-%s",
tail)` holds no secret in the format string and none in the argument — only in
the interpolation. Redaction moved to format time, which also covers exception
messages and tracebacks.

**`executescript()` broke migration atomicity.** It issues an implicit `COMMIT`
before running, silently ending the surrounding transaction. A migration dying
halfway would leave a shape no version number describes. Statements are now split
with SQLite's own parser and run inside one transaction with their record.

**Paths under a symlink failed.** `relative_to` raised when one side was resolved
and the other was not — on macOS `/var` is a symlink to `/private/var`, and a
symlinked home directory or external mount does the same anywhere. Found by the
extended smoke test. Both sides are now resolved.

**`health_check()` disagreed with `describe()`.** The readiness screen consulted
only the credential store while `describe()` also honoured a directly-supplied
key, so a provider that would have worked was reported as unconfigured.

**WebM fixtures used codecs the container cannot hold,** and `libvorbis` is
absent from several common FFmpeg builds including CI's. Now VP8/Opus.

---

## What was built

**Security first.** `app/core/redaction.py` is the single home for redaction,
applied at format time. `scripts/pre_publish_audit.py` runs as a pre-commit hook,
a CI job, and a test; it caught absolute home paths in this build's own docs
before they were committed, and a credential-shaped literal in a test.

**Durability.** SQLite in WAL with forward-only migrations. Artifacts written
temp-sibling → fsync → atomic rename → fsync directory. One worker per output
root, guarded by an OS lock *and* a database claim.

**The pipeline.** Fixed-interval frames with immutable interval and deterministic
timestamps; local transcription with the timeline preserved across silences and a
mandatory CPU fallback; optional descriptions through five adapters; deterministic
enrichment; time-ordered assembly; a handoff folder.

**Collections.** Immutable source-version references, full documents and context
packs, whole videos kept together unless splitting is permitted.

**The interface.** All 11 screens, server-rendered, no external resources, keys
never displayed, no API terminology before opt-in.

---

## Rules that survived every phase

- **A completed batch is never re-sent.** With a cloud provider that is the
  difference between resuming a job and paying twice.
- **The budget is checked before sending.** Checking after would mean the spend
  that crossed the limit had already left.
- **Stopping never destroys finished work.** Cancelling is not undoing.
- **`Unknown` is preserved.** A guess that looks like evidence is worse than an
  admission.
- **Local never falls back to cloud automatically.** That would send frames off
  the machine after the user chose to keep them on it.
- **Nothing is invented in the interface.** Empty states say so.
- **Order is never inferred.** Two recordings from the same morning have no
  inherent sequence.

---

## Limitations — read before relying on this

**CI has never run.** The three-OS workflow is written and its steps are verified
locally, but no git remote exists, so GitHub Actions has never executed it.
**Windows and Linux are untested.** Everything was built and verified on macOS
(Apple Silicon). Platform-specific code paths — symlink fallback, directory
fsync, keyring backends — are written defensively and unit-tested, but no test
has run on those operating systems. Treat first-run on Windows or Linux as
unverified.

**No cloud provider has ever been called.** By design: no test makes a paid call
and none requires a key. The four cloud adapters are tested against mocks
covering the documented request and response shapes. Their behaviour against the
live services is unverified. Local Ollama *is* verified live.

**Transcription quality is unverified.** The pipeline is tested end to end with a
stub speech model. Real transcription runs faster-whisper `medium` on CPU; its
accuracy on your material has not been measured.

**Some review-workspace interactivity is simplified.** The frame scrubber,
per-frame keyboard navigation, and mark-as-checked from the design are not built.
The clean/numbered distinction and its explanatory copy are present, and the
underlying data supports the rest.

**Versioned reruns are partially built.** The schema, version pinning, and
"never overwrite prior output" guarantee are in place and tested. There is no UI
to trigger a targeted rerun of a batch range or of low-confidence frames.

**Drag-to-reorder is not implemented.** Order is set by list position in the
form. Keyboard-operable, but not the drag interaction the design shows.

**`git gc` and long-term database growth are unmanaged.** The event log grows
without bound; on a machine processing thousands of videos it would want pruning.

---

## Where to look

| Question | File |
|---|---|
| How do I install it? | `docs/LOCAL_SETUP.md` |
| What does each stage promise? | `docs/PIPELINE_CONTRACT.md` |
| Descriptions on this computer | `docs/LOCAL_OLLAMA.md` |
| Collections and packs | `docs/COLLECTIONS.md` |
| Running it day to day | `docs/OPERATIONS.md` |
| Something went wrong | `docs/RECOVERY.md` |
| Bringing work in, taking output out | `docs/IMPORT_EXPORT.md` |
| Secrets and the localhost boundary | `docs/SECURITY.md` |
| Publishing this repository | `docs/SECURE_GITHUB_EXPORT.md` |
| Design decisions and departures | `docs/UX_NOTES.md` |
| Choices made at build time | `docs/DECISIONS.md` |
