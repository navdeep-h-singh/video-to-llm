# Handoff — Video → LLM

**For:** a fresh session with full context.
**Repo:** `~/My Builds/Video Processor for LLMs`
**HEAD:** `1b62e9b` · 1,122 tests passing · ruff, mypy, audit, smoke all clean.

---

## Start here

The five pieces that were left for design decisions are **now built** — the
collection wizard, the rest of the settings, targeted reruns, notifications, and
a generated sample clip. §6 records what was built and, more usefully, what was
deliberately *not*.

Read §4 (invariants) and §7 (limitations) before changing anything.

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
         ├── SQLite WAL               app/core/db.py, migrations/ (002)
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
| Pipeline | `app/pipeline/` — probe, preflight, frames, audio, transcribe, visual, enrich, assemble, archive, finalize, rerun |
| Providers | `app/providers/` — base, ollama_local, cloud, costs, retry |
| Collections | `app/collections/` — model, tokens, build |
| Web | `app/web/app.py` (routes), `templates/` (18), `static/tokens.css`, `files.py`, `status.py` |
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

## 5. What was just done

Two sessions. The first fixed 19 of 24 UX-audit findings (`5a11a04` → `ba6aca8`):
the frame viewer, file serving with both-sides symlink resolution, live
progress, the file picker, API key entry, search/rename/delete, and a real
worker-recovery bug that had left the operator's 13-video job idle for nine
hours behind a claim held by a dead PID.

The second built the five pieces below (`f1d5a32` → `1b62e9b`).

Bugs found by *running* it, across both:

- `/api/progress` 500 — `status` exists on both `stage_runs` and `job_videos`;
  the unqualified join column was ambiguous.
- The stylesheet had no cache key, so an upgrade served a stale copy.
- **Every screen hard-reloaded every 5 s** whenever any job ran anywhere, wiping
  forms mid-edit and resetting the frame viewer. Now compares a server
  fingerprint, skips dirty forms, and the review screen opts out.
- The fingerprint could not be `MAX(updated_at)`: timestamps are second-
  resolution, so two transitions in one second left it unchanged.
- **`assembled.txt` reported "Pictures 0"** with 20 on disk — the header labelled
  `len(descriptions)` as "Pictures". The two counts are separate now.
- **The per-job description choice was decorative.** `jobs.visual_provider` was
  recorded and displayed but the worker read the *global* setting, so a job
  created with "skip descriptions" described everything anyway — on a paid
  provider, money spent on work the user explicitly declined.
- **The worker's heartbeat only beat between jobs.** `beat()` was called at the
  top of the run loop, so it did not run at all for the duration of a job — and
  the long jobs are the entire point. Caught live: a worker seven hours into
  describing 2,371 frames still held a heartbeat from before the job started, so
  the header read "Stopped unexpectedly" while it was working perfectly, and a
  second worker would have judged it dead and taken over the same output root.
  Now a daemon thread with its own connection (sharing the worker's would
  interleave with the stages' explicit transactions).
- `HANDOFF.md` itself carried absolute home paths and had been failing the
  pre-publish audit since it was committed.

---

## 6. The five pieces — what was built, and what was not

### A. Collection wizard (F08 + F09) — built

Real numbered sections replace the decorative stepper. Ordering is keyboard-first
(the move buttons are the mechanism; drag is layered on top), announced through a
live region, with focus following the moved item. Order travels in an explicit
field. A version select per source makes pinning older output reachable. The
pre-build estimate runs the **real** `load_sources` + `build_packs` and writes
nothing — deliberately not a browser-side approximation, which would be a second
implementation of the packing rule.

### B. The rest of the settings (F07) — built

Grouped by decision, with worker/concurrency knobs behind an Advanced
disclosure. `output_root` **repoints and moves nothing**, says so, and is refused
outright while a worker claim is held or a job is mid-flight. `port` saves and
states that it needs a restart.

**Not built, on purpose:** `on_limit` is not offered as a control. It is stored
but no code path consults it, and a control that changes no behaviour is the
same lie as placeholder data. A test pins that a save does not rewrite it.

### C. Targeted reruns (F12) — built

Four scopes: every picture, low confidence only, unusable only, a range. Frames
and the transcript are carried forward (hard-linked where the filesystem
allows), and descriptions outside the scope are kept verbatim — they were
already paid for. A version strip shows what each version produced and switches
the active one. Offered from the review screen, where low confidence is noticed.

**Not built, on purpose:** "a new prompt/schema" as a rerun scope. Changing the
prompt changes what every field means, so mixing carried-forward descriptions
with newly-prompted ones inside one version would produce a document whose rows
are not comparable. It needs a whole-video rerun and a schema-change story of
its own.

### D. Notifications (F21) — built

Always on, no permission: a `<title>` badge and a "finished while you were away"
banner backed by `jobs.completion_acknowledged_at` (migration 002). Opt-in:
browser notifications, with permission requested from the tick itself, never on
load; and a terminal bell from the worker, stderr only when it is a tty.

**Not built, correctly:** no push service, no email, no menu-bar agent, no
`launchd`. The spec excludes them and the localhost promise forbids them. A test
asserts no notification path contains `serviceWorker`, `pushManager`, `mailto:`,
or an `https://` URL.

### E. Sample clip (F22) — built

`app/services/sample.py` draws a 60 s animated bar chart in the product's
vermilion plus a tone with four deliberate silences. 478 KB, under a second to
draw, nothing tracked in the repository. Labelled generated test footage
everywhere, via one shared macro.

Deliberately not `testsrc2`: colour bars give a description model nothing to say.
The local model does describe the chart, at Low confidence — which makes the
low-confidence rerun demonstrable on the sample itself.

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
- **Describing the sample locally takes about ten minutes**, not the thirty
  seconds the rest of it takes. `qwen2.5vl:7b` runs at roughly 28 s/frame on this
  machine, so 20 frames is ~10 min. The sample job therefore declines
  descriptions and finishes in ~27 s; adding them is a deliberate second step via
  "Describe again". Worth knowing before demoing that button live.
- **Whisper hallucinates on the sample's tone.** The transcript opens with
  "Thanks for watching!" — a known artefact of running a speech model on
  non-speech audio. It is honest output (the model did report that) and the
  silence markers around it are correct, but it looks odd on a demo screen.
  Fixing it means either synthesising speech or suppressing low-probability
  segments, and both are larger than they sound.
- **`drawtext` is not in this machine's FFmpeg build** (Homebrew 8.1.2, no
  libfreetype), so the sample has no on-screen text and `visible_text` comes back
  Unknown. The chart still varies per frame. A build with libfreetype would allow
  a richer sample; the generator does not currently attempt it.
- **`jobs.visual_provider` is `NOT NULL DEFAULT 'none'`**, so there is no way to
  distinguish "deliberately none" from "never set". Jobs made through
  `create_job` always carry the right value; anything inserting jobs by raw SQL
  must set it or the worker will skip descriptions.

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

## 9. Where to pick up

Nothing is half-built. The most useful next moves, roughly in order:

1. **Rehearse the demo end to end on a clean root.** `rm -rf` a scratch folder,
   point `--output-root` at it, press "Try it with a generated sample", and walk
   dashboard → job → review → collection. Read §7 first so the sample's two
   cosmetic quirks do not surprise you live.
2. **Get a git remote and let CI run.** Windows and Linux have still never
   executed a line of this. That is the largest untested surface by far.
3. **Prune the event log**, if any machine is going to process thousands of
   videos.
4. **A rerun with a changed prompt or schema**, if the versioning story needs to
   go further — see §6C for why it was left out rather than bolted on.

Templates load from disk per request; route code does not. **Restart the server
after changing Python.**
