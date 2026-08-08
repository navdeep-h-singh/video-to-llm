# Handoff — Video → LLM

**For:** a fresh session with full context.
**Repo:** `~/My Builds/Video Processor for LLMs`
**State:** 1,174 tests passing · ruff, mypy, audit and smoke all clean · working
tree clean · nothing half-built. Confirm with `git log --oneline -1` and the
commands in §2 — this file deliberately carries no hash of its own commit,
because writing one changes it.

---

## 0. Before anything else

**The operator's server is running an older build.** It was started before the
last several commits, and route code lives in process memory while templates
reload from disk — so it is currently serving new templates against old routes.
Restart it before judging anything by what the screen shows:

```bash
pkill -f "video-to-llm start" && uv run video-to-llm start
```

**`~/Documents/VideoToLLM` is production.** It holds a 13-video / 15h36m course
and a 1h17m video with 2,307 frames, and a job may be mid-flight. Never point a
test or an experiment at it. Use `tmp_path` or a scratch root.

**`config/settings.toml` is the operator's real configuration** and is
git-ignored, so it cannot be restored from git. Tests monkeypatch
`app.core.config.settings_file`; anything else that saves settings will
overwrite it. Back it up before touching a settings save by hand.

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

Built to `localhost_video_to_llm_with_ollama_and_collections_build_spec.md` (in
`~/Downloads/`), against a supplied Claude Design output. The design's identity
is "Modernist": vermilion `#ec3013`, warm greys, zero border-radius,
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

To try it end to end on a throwaway root:

```bash
uv run video-to-llm --output-root /tmp/scratch start-ui   # then press "Try it with a generated sample"
uv run video-to-llm --output-root /tmp/scratch run-worker --once
```

**If you change Python while a server is running, restart it.** Templates load
from disk per request; route code does not. That drift has already caused one
500 and one silently-broken layout.

---

## 3. Architecture, briefly

```
Browser (127.0.0.1 only)
   └── FastAPI + Jinja2 + plain CSS   app/web/
         ├── SQLite WAL               app/core/db.py, migrations/ (001, 002)
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
| Sample clip | `app/services/sample.py` |
| Web | `app/web/app.py` (routes), `templates/` (18), `static/tokens.css`, `files.py`, `status.py` |
| Docs | `docs/` — 12 files incl. `FINAL_BUILD_REPORT.md` |

The database is authoritative for *state*; artifacts on disk are authoritative
for *evidence*. When they disagree, reconciliation trusts the artifact.

---

## 4. Invariants — do not break these

Load-bearing, each tested, several are the product's actual pitch.

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
    A control that changes no behaviour counts as invented.
11. **Status = text + shape + colour**, never colour alone.
12. **No API terminology before the user opts in.**

Contrast: the design's `#ec3013` is 3.76:1 on the page ground — fine for borders
and marks, below the 4.5:1 a 14px label needs. Filled buttons use `accent-700`
(6.41:1). Muted text is 65%, not 55%. Tests compute the ratios.

---

## 5. What has been built

**Session one** fixed 19 of 24 UX-audit findings (`5a11a04` → `ba6aca8`): the
frame viewer, file serving with both-sides symlink resolution, live progress,
the file picker, API key entry, search/rename/delete, and a worker-recovery bug
that had left the 13-video job idle for nine hours behind a claim held by a dead
PID.

**Session two** (everything from `f1d5a32` onward) built the five pieces that had
been left for design decisions, then swept the app for more of the same class of
defect. `git log --oneline f1d5a32~1..` is the whole story, and each message
explains why rather than what.

### A. Collection wizard (F08 + F09)

Real numbered sections replace what was a decorative five-step header. Ordering
is keyboard-first — the move buttons are the mechanism and drag is layered on
top, so there is no path the mouse can take that the keyboard cannot. Moves are
announced through a live region and focus follows the moved item. Order travels
in an explicit field, never inferred. A version select per source makes pinning
older output reachable.

The pre-build estimate runs the **real** `load_sources` + `build_packs` and
writes nothing. Deliberately not a browser-side approximation: that would be a
second implementation of the packing rule, and the day the two disagreed the
form would be promising something the build then contradicted.

### B. The rest of the settings (F07)

Roughly twenty settings that existed only in TOML, grouped by what the person is
deciding, with worker and concurrency knobs behind an Advanced disclosure.

`output_root` **repoints and moves nothing**, says so, and is refused outright
while a worker claim is held or a job is mid-flight. The database is created in
the new folder immediately, or every screen reads as a fresh install. `port`
saves and states plainly that it needs a restart.

**Not built, on purpose:** `on_limit` is not offered as a control. It is stored
but no code path consults it. A test pins that saving does not rewrite it.

### C. Targeted reruns (F12)

Four scopes: every picture, low confidence only, unusable only, a range. Frames
and the transcript are carried forward (hard-linked where the filesystem
allows); descriptions outside the scope are kept verbatim, because they were
already paid for. A version strip shows what each version produced and switches
the active one. Offered from the review screen, where low confidence is noticed.

**Not built, on purpose:** "a new prompt or schema" as a scope. Changing the
prompt changes what every field means, so mixing carried-forward descriptions
with newly-prompted ones inside one version produces a document whose rows are
not comparable. It needs a whole-video rerun and a schema-change story of its
own.

### D. Notifications (F21)

Always on, no permission: a `<title>` badge, and a "finished while you were
away" banner backed by `jobs.completion_acknowledged_at` (migration 002) so it
survives a restart and an overnight suspension. Opt-in: browser notifications,
with permission requested from the tick itself and never on load; and a terminal
bell from the worker, written to stderr only when stderr is a tty.

**Not built, correctly:** no push service, no email, no menu-bar agent, no
`launchd`. The spec excludes them and the localhost promise forbids them. A test
asserts no notification path contains `serviceWorker`, `pushManager`, `mailto:`,
or an `https://` URL.

### E. Sample clip (F22)

`app/services/sample.py` draws a 60-second scrolling bar chart in the product's
vermilion, plus a tone with four deliberate silences so the transcript has
silence markers to show. ~1.3 MB, ~2.4 s to draw, **nothing tracked in the
repository**. Labelled generated test footage everywhere, via one shared macro.

Deliberately not `testsrc2`: colour bars give a description model nothing to
say. The local model does describe the chart, at Low confidence — which
conveniently makes the low-confidence rerun demonstrable on the sample itself.

Fresh install → press the button → 27 seconds → frames, transcript with silence
markers, assembled document, viewer, contact sheet.

---

## 6. The bug class this build keeps producing

**Controls that look live and are wired to nothing.** Every one of these was
invisible to the type checker, the linter, and the existing tests, and several
were invisible on screen too. If you change UI here, assume this is the failure
mode.

Found and fixed:

- **`.pick` choice cards showed no selection.** The only selected-state rule
  keyed off `[aria-pressed="true"]`, which nothing ever set — dead from when the
  element was a `<button>`. Three screens; picking an option changed nothing.
- **The job screen had no stop, rename, or delete.** An unclosed `{% if %}`
  inside `{% block title %}` swallowed three panels into `<title>` — 1,705
  characters of markup in a tab title. Every route existed and was tested at the
  route level; none was reachable.
- **The per-job description choice was decorative.** `jobs.visual_provider` was
  recorded and displayed, but the worker read the *global* setting — so a job
  created with "skip descriptions" described everything anyway. On a paid
  provider that is money spent on work the user explicitly declined.
- **The worker heartbeat only beat between jobs**, so it never beat during one.
  A worker seven hours into 2,371 frames held a heartbeat from before the job
  started: the header read "Stopped unexpectedly" while it worked perfectly, and
  a second worker would have judged it dead and taken over the same output root.
- **The sample clip was a still image.** `drawbox`'s `t` is the box *thickness*,
  not the timestamp, so the "animated" bars were constants. Twenty sampled
  frames, two distinct pictures, and no error anywhere.
- **"Numbered copy" was offered when no numbered copies exist** (the default) —
  broken image plus a caption about sending pictures to a model that never ran.
- **The contact sheet crashed** on a job with no videos: the route's own
  empty-state branch passed the template none of the values it needed.
- **Every screen hard-reloaded every 5 s** whenever any job ran anywhere, wiping
  forms mid-edit and resetting the frame viewer.
- **`assembled.txt` reported "Pictures 0"** with 20 on disk — the header
  labelled `len(descriptions)` as "Pictures".
- Two `notfound.html` call sites did not pass `what`: "That  could not be
  found".

**The tests added pin the class, not the instance.** Keep them:

| Guard | Where |
|---|---|
| No screen has markup in its `<title>` | `test_accessibility.py` |
| Selection is expressible, and not by colour alone | `test_accessibility.py` |
| Every screen renders with no videos / no artifacts / empty install | `test_web_ui.py` |
| The sample must be seen to change over its length | `test_sample_clip.py` |
| Saving one settings section leaves the others alone | `test_settings_screen.py` |
| A rerun only sends the frames it was asked to | `test_rerun.py` |
| The claim is refreshed while a stage runs | `test_worker_and_cli.py` |

Swept and clean: no form posts to a nonexistent route, no label points at a
missing id, no button lacks an accessible name, no link goes nowhere, every
image resolves, traversal is refused, all 12 screens return 200.

---

## 7. Known limitations — carry these forward honestly

- **CI has never executed.** The three-OS workflow exists and its steps are
  verified locally, but there is no git remote, so GitHub Actions has never run.
  **Windows and Linux are untested.** Everything was built on macOS (Apple
  Silicon). Platform-specific paths — symlink fallback, directory fsync, keyring
  backends, the hard-link fallback in reruns — are written defensively and
  unit-tested, nothing more. This is the largest untested surface by far.
- **No cloud provider has ever been called.** By design: no test makes a paid
  call. The four adapters are mock-verified against documented request/response
  shapes. **Local Ollama *is* verified live** against Ollama 0.32.6 +
  `qwen2.5vl:7b`.
- **Transcription accuracy is unmeasured.** The pipeline is tested end to end
  with a stub speech model; real runs use faster-whisper `medium` on CPU.
- **Describing the sample locally takes ~10 minutes**, not the ~27 seconds the
  rest takes: `qwen2.5vl:7b` runs at roughly 28 s/frame here, and the sample is
  20 frames. The sample job therefore *declines* descriptions; adding them is a
  deliberate second step via "Describe again". Know this before demoing that
  button live.
- **Whisper hallucinates on the sample's tone.** The transcript opens with
  "Thanks for watching!" — a known artefact of a speech model on non-speech
  audio. Honest output, correct silence markers around it, but it looks odd on a
  demo screen. Fixing it means synthesising speech or suppressing
  low-probability segments; both are larger than they sound.
- **`drawtext` is absent from this machine's FFmpeg** (Homebrew 8.1.2, no
  libfreetype), so the sample carries no on-screen text and `visible_text` comes
  back Unknown. `drawbox` here also has no `eval` option, and its `t` is
  thickness — the scroll is done with `crop`, whose `t` really is time.
- **`jobs.visual_provider` is `NOT NULL DEFAULT 'none'`**, so "deliberately
  none" and "never set" are indistinguishable. Jobs created through `create_job`
  always carry the right value; anything inserting jobs by raw SQL must set it
  or the worker will skip descriptions.
- **The event log grows without bound.** Fine now; would want pruning on a
  machine processing thousands of videos.
- **`/settings` shells out to `ffmpeg -version` on every render** (capped at
  10 s), so it can be slow under heavy load. Not a defect — the readiness check
  is meant to be live — but it is why that page can stall during a busy job.
- **Latent, deliberately left alone:** eleven unused decorative classes in
  `tokens.css` from the supplied design system, and four schema columns nothing
  reads (`jobs.settings_json`, `stage_runs.output_version`,
  `stage_runs.items_skipped`, `events.detail_json`).

---

## 8. Where to pick up

Roughly in order of value:

1. **Rehearse the demo end to end on a clean root.** `rm -rf` a scratch folder,
   point `--output-root` at it, press "Try it with a generated sample", then
   walk dashboard → job → review → contact sheet → collection. Read §7 first so
   the sample's two cosmetic quirks do not surprise you live.
2. **Get a git remote and let CI run.** Windows and Linux have never executed a
   line of this.
3. **Prune the event log**, if any machine will process thousands of videos.
4. **A rerun with a changed prompt or schema**, if the versioning story needs to
   go further — see §5C for why it was left out rather than bolted on.
5. **Decide on the unused design-system CSS and the four dead columns** (§7).
   Both are judgement calls that belong to the operator, not to a cleanup pass.

---

## 9. Working agreements that have served this build well

- **Run it, don't just read it.** Almost every real bug in both sessions was
  found by using the app or by scanning for a *class* of defect — none by
  reading code linearly.
- **When you find a bug, write the test that catches its class**, not the
  instance. Then revert the fix and watch the test fail. Twice in this session a
  test passed vacuously and only the revert exposed it.
- **Tests assert behaviour and say why.** Comments explain the failure being
  prevented, not what the line does.
- **Verify against real artifacts** — a real JPEG served, a real traversal
  refused, a real job created — not only mocks.
- **Commit at each coherent milestone** with a message explaining *why*, and the
  bugs found along the way. Run format, lint, mypy, tests, audit, smoke first.
- **Never weaken a check to make it pass.** When the pre-publish audit flagged a
  credential-shaped literal in a test, the string was assembled at runtime
  rather than adding the file to an exemption list.
- **Masking ordinary data is not a safe failure.** A redaction false positive
  once blanked every token count in every manifest.
- **A control that changes no behaviour is a lie**, and it is the defect this
  codebase produces most often. See §6.

---

## 10. First message to send

> Read `HANDOFF.md`. The working tree is clean and everything is committed —
> start by telling me what you understand the current state to be, and what you
> would do first.
