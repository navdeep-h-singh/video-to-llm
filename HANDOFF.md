# Handoff — Video → LLM

**For:** a fresh session with full context.
**Repo:** `~/My Builds/Video Processor for LLMs`
**HEAD:** `ba6aca8` · 986 tests passing · ruff, mypy, audit, smoke all clean.

---

## Start here

Five pieces were deliberately left unbuilt because they need **design decisions,
not just implementation**. Do not start coding them.

**Your first task: present options for each of the five below — pros, cons, and a
recommended path — and wait for the operator to choose.** Then build.

They are listed in §6. Everything before that is the context you need to make
those recommendations well.

---

## 1. What this is

A **localhost-only** tool that turns local video files into timestamped,
reviewable evidence, and later combines already-processed videos into ordered,
LLM-ready **Collections**.

Per video it produces: sampled frames, a local transcript with silence markers,
optional structured visual descriptions, an enriched chronological
`assembled.txt`, manifests, and provenance. Collections reuse those outputs
without re-running anything.

It is **not** a cloud product, a chatbot, a video editor, or a reasoning engine.

Built to `localhost_video_to_llm_with_ollama_and_collections_build_spec.md`
(in `~/Downloads/`), against a supplied Claude Design output. The design's
identity is "Modernist": vermilion `#ec3013`, warm greys, zero border-radius,
Archivo-style grotesque.

**Immediate goal:** an investor demo. Everything is judged against "does this
land in a live demo".

---

## 2. Run it

```bash
cd ~/"My Builds/Video Processor for LLMs"
uv run video-to-llm start          # interface + worker, http://127.0.0.1:8712
uv run video-to-llm doctor         # readiness
uv run video-to-llm smoke-test     # 12 checks, no network
uv run pytest -q --deselect tests/integration/test_live_ollama.py
uv run ruff format --check . && uv run ruff check . && uv run mypy app
uv run python scripts/pre_publish_audit.py
```

Operator's real output root: `~/Documents/VideoToLLM`. It contains real jobs —
a 13-video / 15h36m course and a 1h17m video with 2,307 frames. **Treat that
data as production.** Use `tmp_path` or a scratch root for experiments.

**If you change code while a server is running, restart it.** Templates load
from disk per request but route code lives in process memory; the two drifting
apart already caused one 500 and one silently-broken layout.

---

## 3. Architecture, briefly

```
Browser (127.0.0.1 only)
   └── FastAPI + Jinja2 + plain CSS   app/web/
         ├── SQLite WAL               app/core/db.py, migrations/
         ├── artifacts on disk        app/core/artifacts.py
         └── separate worker process  app/worker/
               ├── FFmpeg + faster-whisper
               └── provider adapters  app/providers/
```

| Area | Where |
|---|---|
| Config | `app/core/config.py`, `config/settings.example.toml` |
| Redaction | `app/core/redaction.py` — single home, applied at format time |
| Locks | `app/core/locks.py` — file lock **and** DB claim |
| Pipeline | `app/pipeline/` — probe, preflight, frames, audio, transcribe, visual, enrich, assemble, archive, finalize |
| Providers | `app/providers/` — base, ollama_local, cloud, costs, retry |
| Collections | `app/collections/` — model, tokens, build |
| Web | `app/web/app.py` (routes), `templates/` (17), `static/tokens.css`, `files.py`, `status.py` |
| Docs | `docs/` — 12 files incl. `FINAL_BUILD_REPORT.md` |

---

## 4. Invariants — do not break these

These are load-bearing, each tested, and several are the product's actual pitch.

1. **Localhost only.** Binding is asserted at app construction. No page loads an
   off-origin resource — no CDN, no webfont. The header badge promises "nothing
   is uploaded" and that must stay literally true.
2. **A completed provider batch is never re-sent.** On a cloud provider this is
   the difference between resuming and paying twice.
3. **The budget is checked *before* sending**, never after.
4. **Local never auto-falls-back to cloud.** That would ship frames off the
   machine after the user chose to keep them on it.
5. **Stopping never destroys finished work.** Cancel ≠ undo.
6. **`Unknown` is preserved, never guessed.** Unparseable confidence → Low.
7. **Order is never inferred** from filename, date, or content.
8. **Collection source versions are immutable.** Reprocessing leaves existing
   collections untouched.
9. **A stored key is never rendered back** — not the value, not a prefix. Fields
   are write-only. Storage refuses rather than falling back to a file.
10. **Nothing is invented in the UI.** Empty states say so; no placeholder data.
11. **Status = text + shape + colour**, never colour alone.
12. **No API terminology before the user opts in.**

Contrast: the design's `#ec3013` is 3.76:1 on the page ground — fine for borders
and marks, below the 4.5:1 a 14px label needs. Filled buttons use `accent-700`
(6.41:1). Muted text is 65%, not 55%. Tests compute the ratios.

---

## 5. What was just done (audit remediation)

A UX audit found 24 issues. **19 are fixed** across commits `5a11a04` → `ba6aca8`.

Headline fixes:

- **Frame viewer** — the review screen was empty; 2,307 frames on disk and none
  visible. Now: the frame, its description, transcript lines within ±45s with
  the current line highlighted, arrow-key navigation, contact sheet.
- **File serving** (`app/web/files.py`) — nothing was previewable or
  downloadable. Containment resolves *both* sides (a symlinked root would fail a
  naive prefix check; a symlink inside the root pointing out would pass one).
- **Live progress** — every screen was static. Now polls, but only while
  something runs.
- **Worker recovery — a real bug.** `run_worker` returned `1` permanently on a
  claim conflict, so in `start` mode the worker thread died and the UI ran on
  with no worker. The operator's 13-video job sat idle **nine hours** behind a
  claim held by a dead PID, 172× past its 120s staleness threshold. Now retries
  every 20s; `--once` still fails fast.
- **File picker** — job creation required typing absolute paths.
- **API key entry** — there was *no field*; four cloud adapters were unreachable.
- **Search/filter/sort, rename, delete, "add descriptions later"** (a promise the
  UI had been making with nothing behind it), elapsed times, dated logs, real
  folder sizes, friendly filenames, "Not run" instead of a permanent "Waiting".

Bugs found by *running* it, not reading it:

- `/api/progress` 500 — `status` exists on both `stage_runs` and `job_videos`;
  the unqualified join column was ambiguous.
- The progress query was assembled by concatenating a WHERE clause (ruff caught
  it). Now two literal statements.
- **The stylesheet had no cache key**, so an upgrade served a stale copy and the
  page rendered wrong with nothing pointing at why. Now versioned by mtime.

Full report with severities: `docs/` and the published audit artifact.

---

## 6. THE FIVE REMAINING PIECES — your first task

For **each** of the five: give the operator **2–3 concrete options**, the honest
pros and cons of each, and **your recommendation with reasoning**. Then wait.

Bias the recommendations toward **what makes the investor demo land**, while
respecting the invariants in §4.

---

### A. Collection wizard: stepper, ordering, and pre-build estimate (F08 + F09)

**Current state.** `app/web/templates/newcollection.html` renders a five-step
header — *1 Choose videos · 2 Set the order · 3 Choose versions · 4 Choose the
shape · 5 Check and build* — that is **purely decorative**. The page is one flat
form. Two advertised steps have no interface at all:

- **"Set the order"** — no drag, no move controls, no numbering. Order is
  whatever sequence the checkboxes happen to produce.
- **"Choose versions"** — the version column is read-only text.

There is also **no estimate before building**: total duration, estimated tokens,
resulting pack count, per-source warnings, and output location are all
computable from data already in memory when the form renders, and none is shown.

**Why it matters for the demo.** Ordering is the thing the specification is most
emphatic about, and "drag your videos into the order you want, see the estimate
update, press build, done in seconds" is one of the strongest live moments
available. Right now it is a checkbox list.

**Constraints.** Order must be explicit and **keyboard-operable** (spec requires
parity — drag alone is not acceptable). Collection building must stay free,
local, and non-destructive, so it should *not* acquire heavy confirmation.

**Existing pieces you can build on:** `app/collections/model.py`
(`set_sources` already takes an ordered list, `assess_source` computes warning
states), `app/collections/tokens.py` (estimation, already labelled an estimate),
`app/collections/build.py` (`build_packs` can be dry-run for a pack count).

**Design axes to present options across:**
- Real multi-page stepper vs. progressive single page vs. keep flat + add the
  two missing controls.
- Drag-and-drop + keyboard, or keyboard-only move controls (simpler, no
  library, fully accessible by construction).
- Estimate computed server-side on each step, or live in the browser.

---

### B. The remaining settings (F07)

**Current state.** Settings exposes six fields: `enabled`, `provider`,
`model_id`, `endpoint`, `batch_size`, `experimental_acknowledged` — plus the new
write-only key entry. The config file defines roughly twenty more. Verified
missing from the UI:

```
[general]          output_root
[server]           port
[sampling]         preset, custom_interval_seconds
[transcription]    backend, model, silence_threshold_seconds, language
[visual_analysis]  budget (hard_limit_usd, on_limit), local_guard
[ollama]           concurrency
[worker]           poll_interval_seconds, max_retries, backoff_base_seconds
[collections]      default_token_limit, default_reserve_tokens, allow_video_split
```

So a user must hand-edit TOML for most of what the product can do — including
**where output goes** and **the spending cap**.

**Constraints.** `output_root` changing at runtime is the delicate one: the
worker holds a claim on the current root and the database lives inside it.
`server.port` cannot take effect without a restart. Some settings are safe to
change live; others are not, and the UI should not pretend otherwise.

**Design axes:** expose everything vs. a curated set with an "advanced"
disclosure; how to handle settings that need a restart; whether changing the
output root should offer to move existing work or simply point elsewhere.

---

### C. Targeted reruns (F12)

**Current state.** Versioned reruns are designed and schema-backed —
`job_videos.version`, `is_active_version`, and per-batch records exist, and
"never overwrite prior expensive output" is tested. **None of it is reachable
from the UI.** The only rerun path built is the blunt "describe this whole video
now" added during the audit fixes.

**Why it matters.** This is arguably the strongest *technical* story in the
product: *we never overwrite what you paid for, we version it.* A technical
investor will ask what happens when a model gets better, and the answer is good.
Right now it is invisible.

**Scopes the spec calls for:** a whole video; a batch range; low-confidence
frames only; frames that produced fallback output; a new prompt/schema/model.

**Design axes:** where the control lives (review screen per-frame? job screen
per-video? a dedicated rerun screen?); how scope is chosen; how versions are
presented afterwards and how the user switches the active one; whether a rerun
that would cost money needs a confirmation distinct from a local one — note
invariant 3 and the existing budget machinery.

---

### D. Notifications when a long job finishes (F21)

**Current state.** Nothing. The product is explicitly built for jobs that run
for hours while you do something else, and it cannot tell you it is done. Before
the audit fixes the page did not even refresh itself.

**Constraints — read carefully.** The spec **excludes** OS notification
registration, `launchd`, Task Scheduler, `systemd`, and any telemetry or
outbound call. So: no push service, no email, no menu-bar agent.

That leaves in-page options: Web Notifications API (browser permission, works
only while a tab is open), a `<title>` badge, an audible chime, a persistent
in-app "finished while you were away" banner, or a terminal bell from the
worker.

**Design axes:** which of those, in what combination; whether to ask for browser
notification permission at all (a permission prompt on first run is a poor
first impression); how "finished while you were away" is tracked across restarts.

---

### E. A bundled sample clip (F22)

**Current state.** A fresh install shows an empty dashboard and a readiness
checklist. There is no way to see what the product produces without supplying
your own video and waiting.

**Why it matters for the demo.** A one-minute end-to-end run — pick the sample,
watch the bars move, open the viewer, read `assembled.txt` — is the entire pitch
in sixty seconds, with nothing to prepare and nothing to type.

**Constraint that shapes this.** The repo must stay publishable: **no personal
media, no large binaries.** `tests/fixtures/synthetic.py` already generates
video from FFmpeg's own `testsrc2` + a tone — colour bars, not a realistic
screen recording.

**Design axes:** ship a generated synthetic clip (safe, publishable, visually
uninteresting) vs. generate something more chart-like at first run vs. ship a
tiny genuinely-representative clip (needs a licence story) vs. ship a
pre-computed *example output* with no video at all so the viewer has something
to show instantly. Consider that the demo needs the *pipeline* to visibly run,
not just the output to exist.

---

## 7. Known limitations — carry these forward honestly

- **CI has never executed.** The three-OS workflow exists and its steps are
  verified locally, but there is no git remote, so GitHub Actions has never run.
  **Windows and Linux are untested.** Everything was built on macOS (Apple
  Silicon). Platform-specific paths — symlink fallback, directory fsync, keyring
  backends — are written defensively and unit-tested, nothing more.
- **No cloud provider has ever been called.** By design: no test makes a paid
  call. The four adapters are mock-verified against documented request/response
  shapes. **Local Ollama *is* verified live** against Ollama 0.32.6 +
  `qwen2.5vl:7b` on this machine.
- **Transcription accuracy is unmeasured.** The pipeline is tested end to end
  with a stub speech model; real runs use faster-whisper `medium` on CPU.
- **The event log grows without bound.** Fine now; would want pruning on a
  machine processing thousands of videos.

---

## 8. Working agreements that have served this build well

- **Run it, don't just read it.** Three real bugs in the last session were found
  by using the app and none by reading code.
- **Tests assert behaviour and say why.** Comments explain the failure being
  prevented, not what the line does.
- **Verify against real artifacts** where possible — a real JPEG served, a real
  traversal refused, a real job created — not only mocks.
- **Commit at each coherent milestone** with a message explaining *why*, and the
  bugs found along the way. Run format, lint, mypy, tests, audit, smoke first.
- **Never weaken a check to make it pass.** When the pre-publish audit flagged a
  credential-shaped literal in a test, the string was assembled at runtime rather
  than adding the file to an exemption list.
- **Masking ordinary data is not a safe failure.** A redaction false positive
  once blanked every token count in every manifest.

---

## 9. First message to send

> Read `HANDOFF.md`. Then, for each of the five items in §6, give me 2–3 options
> with honest pros and cons and your recommendation. Don't write any code yet.
