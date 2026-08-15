# Handoff — Video → LLM

**For:** a fresh session with full context.
**Repo:** `~/My Builds/Video Processor for LLMs`
**State:** 1,440 tests passing · ruff, mypy, audit and smoke all clean · nothing
half-built. Confirm with `git log --oneline -1` and the commands in §2 — this
file deliberately carries no hash of its own commit, because writing one changes
it.

**Session four's work is on the `launch-prep` branch, not `main`.** It is a
clean fast-forward. See §5H for what it contains and
`docs/LAUNCH_CHECKLIST.md` for what it deliberately left undone.

---

## 0. Before anything else

**The application now tells you when it is out of date.** A banner appears on
every screen when the Python on disk is newer than the running process. Believe
it: templates reload per request and route code does not, so an updated
application serves new screens from old routes. That drift cost three separate
debugging sessions before the banner existed — see §6.

```bash
pkill -f "video-to-llm start" && uv run video-to-llm start
```

**`~/Documents/VideoToLLM` is production.** It holds a 13-video / 15h36m course
and `Trendlines Video` — 49:39, 1,488 pictures, now `completed_with_gaps` with
1,479 described. Never point a test or an experiment at it. Use `tmp_path` or a
scratch root.

**Settings moved.** They are no longer in the repo. The live file is:

```
~/Library/Application Support/VideoToLLM/settings.toml
```

`config/settings.toml` still exists and is still git-ignored, but nothing reads
it once the new file exists — it was copied across, byte-identical, on first
load. Tests monkeypatch `app.core.config.settings_file`; anything else that
saves settings writes to the user location. `VIDEO_TO_LLM_CONFIG_FILE` overrides
it, which is how a scratch instance avoids the real one entirely.

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

**Immediate goal:** an investor demo, and the operator now intends to take it to
production for a public audience. Everything is judged against "does this land
in a live demo" *and* "would this survive a stranger using it".

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

**The pipeline no longer needs the browser.** Since session four:

```bash
uv run video-to-llm process video.mp4 --interval 2 --format jsonl
uv run video-to-llm show "job name" 00:12:34   # → transcript + the frame's path
uv run video-to-llm export "job name" --format srt
uv run video-to-llm mcp                        # stdio MCP server, needs [mcp]
```

Note the gate above: **run each check as its own command, not through a pipe.**
`uv run ruff check . | tail -1` reports `tail`'s exit status, and a commit was
made against three unfixed findings that way in session four.

A throwaway instance that cannot touch the real settings or output:

```bash
SCRATCH=/tmp/vtl-scratch && rm -rf $SCRATCH && mkdir -p $SCRATCH
VIDEO_TO_LLM_CONFIG_FILE=$SCRATCH/settings.toml \
  uv run video-to-llm --output-root $SCRATCH start-ui --port 8799
VIDEO_TO_LLM_CONFIG_FILE=$SCRATCH/settings.toml \
  uv run video-to-llm --output-root $SCRATCH run-worker --once
```

Then press "Try it with a generated sample".

---

## 3. Architecture, briefly

```
Browser (127.0.0.1 only)
   └── FastAPI + Jinja2 + plain CSS   app/web/
         ├── origin boundary          one middleware, before every route
         ├── SQLite WAL               app/core/db.py, migrations/ (001–003)
         ├── artifacts on disk        app/core/artifacts.py
         └── separate worker process  app/worker/
               ├── FFmpeg + faster-whisper
               └── provider adapters  app/providers/
```

| Area | Where |
|---|---|
| Config | `app/core/config.py`, `config/settings.example.toml` |
| Staleness | `app/core/build.py` — is the running code older than the files |
| Redaction | `app/core/redaction.py` — single home, applied at format time |
| Locks | `app/core/locks.py` — file lock **and** DB claim |
| Pipeline | `app/pipeline/` — probe, preflight, frames, audio, transcribe, visual, enrich, assemble, archive, finalize, rerun, **progress** |
| Providers | `app/providers/` — base, ollama_local, cloud, costs, retry |
| Collections | `app/collections/` — model, tokens, build |
| Services | `app/services/` — jobs, sample, doctor, smoke, importer, **estimate**, **cleanup** |
| Web | `app/web/app.py` (routes), `templates/` (18), `static/tokens.css`, `files.py`, `status.py` |
| Docs | `docs/` — 12 files incl. `FINAL_BUILD_REPORT.md`, `SECURITY.md` |

The database is authoritative for *state*; artifacts on disk are authoritative
for *evidence*. When they disagree, reconciliation trusts the artifact.

---

## 4. Invariants — do not break these

Load-bearing, each tested, several are the product's actual pitch.

1. **Localhost only.** Binding is asserted at app construction. No page loads an
   off-origin resource — no CDN, no webfont. The header badge promises "nothing
   is uploaded" and that must stay literally true.
2. **Only our own pages can act.** One middleware refuses a foreign `Host` (421)
   and a foreign origin on every write and every `/api/` read (403). See §5F.
3. **A completed provider batch is never re-sent — across attempts, not just
   within one.** This was false until this session and cost three hours of real
   work. See §6.
4. **The budget is checked *before* sending**, never after.
5. **Local never auto-falls-back to cloud.**
6. **Stopping never destroys finished work.** Cancel ≠ undo.
7. **`Unknown` is preserved, never guessed.** Unparseable confidence → Low.
8. **Order is never inferred** from filename, date, or content.
9. **Collection source versions are immutable.**
10. **A stored key is never rendered back** — not the value, not a prefix. Fields
    are write-only. Storage refuses rather than falling back to a file.
11. **Nothing is invented in the UI.** Empty states say so; no placeholder data.
    A control that changes no behaviour counts as invented.
12. **Status = text + shape + colour**, never colour alone.
13. **No API terminology before the user opts in.** Enforced by a test over
    `/`, `/launch`, `/jobs/new`, `/imports`, `/collections`; it has caught
    "endpoint" and `api_key` leaking into markup.
14. **A model belongs to the service that offers it**, never to the application.
15. **An estimate is measured or absent, never guessed.**

Contrast: the design's `#ec3013` is 3.76:1 on the page ground — fine for borders
and marks, below the 4.5:1 a 14px label needs. Filled buttons use `accent-700`
(6.41:1). Muted text is 65%, not 55%. Tests compute the ratios.

---

## 5. What has been built

Sessions one and two are in `git log`; their summaries are unchanged and the
commit messages explain *why* rather than *what*. **Session three** (everything
from `81bcf7a` onward) was a production-readiness audit followed by the fixes it
found, then four rounds of feature work driven by the operator using the app.

### A. The audit (`81bcf7a`)

A full use-case map, an ideal-UX definition, and an end-to-end test plan with
acceptance criteria, executed against a scratch root. Nineteen findings, all
fixed in that commit. The three that mattered:

- **F-01/04/05 — no origin boundary.** Loopback binding keeps other *machines*
  out and did nothing about other *origins*. A urlencoded form post is a CORS
  "simple request": any page the user had open could delete a job with its
  files, remove a stored key, or start a rerun that spends money. A hostname
  rebound to 127.0.0.1 could read the replies too. `docs/SECURITY.md` had
  documented the absence of authentication as safe *because* there was no remote
  surface, which was the wrong premise.
- **F-02 — deleting could destroy what it then refused to delete.** The route
  removed the folder and *then* the row. `collection_sources` holds an
  unqualified reference to `job_videos` on purpose, so the DELETE raised whenever
  a collection cited the job — after the frames, transcript and assembled
  document were already gone. The rollback restored the row, leaving the
  dashboard listing a job whose every file had been erased, above an error page
  promising nothing had been affected.
- **F-03 — no in-flight guards.** Delete, describe and rerun all reached past a
  running worker.

The other sixteen: 404s answered 200; the picture jump box was one behind the
caption under the image; hand-edited query parameters returned raw framework
422s; an empty rename was swallowed; a reserve larger than the token limit was
accepted and silently packed nothing; settings lived inside the installation.

### B. Live progress (`81bcf7a`, `722d000`, `73535e1`, `b17cbd3`)

`items_total` and `items_done` were both written when a stage *finished*, so a
stage in flight had no denominator and the bar sat at exactly 0% for its whole
run. On the operator's 49-minute video that was twenty minutes indistinguishable
from a hang; on 1,488 pictures it was most of a day.

`app/pipeline/progress.py` publishes the size before the work starts and reports
as it goes, throttled to two seconds and best-effort — a dropped tick costs a
moment of staleness, raising out of a nine-hour transcription costs the
transcription. **Progress is measured in the unit the user experiences**:
transcription counts seconds of video covered, not speech segments, because
segments run from a second to several minutes and any estimate built on them is
wrong.

Three defects were shipped inside this feature and found by the operator looking
at the screen, not by the suite. See §6.

### C. Resume across attempts (`569adc2`)

`completed_batch_indexes` was scoped to one `stage_run_id`, but `_begin_stage`
mints a fresh row per attempt — so a restarted worker saw none of the previous
attempt's work. It discarded **562 completed pictures, three hours**, on the
operator's real job, on advice from this session that the restart was safe.

Now scoped to every attempt of the same stage on the same video, and skipped
batches carry their descriptions back from disk rather than only incrementing a
counter. A batch whose artifact will not load is described again rather than
silently omitted.

### D. Providers (`ad9ec32`, `ab3e769`, `2c6fff3`, `fab89e3`)

**The model was one string shared by every provider**, so setting a Gemini model
and switching to Claude asked Anthropic for `gemini-2.5-flash`. Now
`visual_analysis.models` maps provider → model and `base_urls` does the same for
the two endpoints that live wherever you put them. `model_id` survives as a
read-only property; making it read-only is how mypy found both places still
assigning it.

**Anthropic-compatible endpoints** were added alongside OpenAI-compatible ones.
Five services total. **Models are discovered, not typed**: a Check button asks
the service what it offers, which doubles as the key check.

**The service is chosen where the job is created**, with inline key setup, and
the chosen model travels with the job. Labels live in one place
(`PROVIDER_LABELS`) and the settings template's fallback copy is pinned against
it by a test.

### E. Staleness detection (`7bb87e1`)

`app/core/build.py` compares the newest `.py` mtime against the fingerprint
captured at `create_app()`. Only Python counts — templates and CSS are *meant*
to change under a running server, and flagging those would advise a restart that
changes nothing.

### F. Finding, naming, and reclaiming (`6c45af1`)

- A **Finished** sidebar entry, pointing at the `?state=finished` filter that had
  existed all along with nothing linking to it.
- **Output folders are named after the job** — `trendlines-video/`, not a UUID.
  Slugified conservatively, checked against Windows reserved names on every
  platform, falling back to the identifier when nothing usable survives, with a
  counted suffix on collision. Stored in `jobs.output_dirname` (migration 003)
  and never recomputed, because deriving it from the current name would move the
  folder on rename. NULL means an older job keeping its identifier-named folder.
- **Selective file removal** by kind, with sizes and what each removal costs.
  Removing a job was all-or-nothing, which made 91 MB of scratch audio and the
  1 MB document the run exists to produce the same decision.

### G. Collection explainability (`598e59e`)

The three numbers in "Choose the shape" had no units and no reason given. They
now explain themselves, and a disclosure explains that the size figure is a
**raw** estimate of the whole document — not a real tokenisation, and not the
amount a model has to read. See §10 for the arithmetic behind that claim.

---

### H. Session four — the launch build (`8c683c6` onward)

A competitive scan of the GitHub category, then the work it implied. The scan is
in `docs/GITHUB_LAUNCH_PLAN.md`; what could not be done from a laptop is in
`docs/LAUNCH_CHECKLIST.md`.

- **The schema shipped outside the package.** `migrations_dir()` resolved to
  `repo_root() / "migrations"`, which from `site-packages/app/core/config.py`
  points at `site-packages/migrations`. **A pip-installed copy could not create
  its own database.** Every test passed because every test ran from the
  checkout, and the tool had never once been installed from a wheel. Now
  `app/migrations/`, resolved against the package, with a CI job that installs
  the built artifact and runs it from `/tmp`.
- **Python was pinned to 3.11 only**, so every install from a 3.12 or 3.13
  machine — the default on current Homebrew and Ubuntu — failed at resolution.
  Now `>=3.11,<3.14`, verified by installing and importing faster-whisper and
  ctranslate2 on 3.13 before widening rather than after.
- **There was no LICENSE file**, though the README and the metadata had claimed
  MIT since the first commit.
- **`process`, `show`, `export`, `mcp`.** The README's claim that the pipeline
  was callable from the command line is finally true. `show` resolves a
  timestamp to the surrounding transcript and the exact frame's path, which is
  what makes "reviewable evidence" a demonstrable thing rather than an adjective.
- **A one-shot worker took the oldest ready job**, so `process foo.mp4` on a
  machine with anything queued ran something else — and, where that job named a
  cloud service, spent money doing it. The worker can be scoped to one job now.
- **Four MCP tools**, with `process_video` idempotent on the source paths, and a
  refusal to let an agent select a paid provider.
- **The confidence prompt.** See §7 and `docs/DESCRIPTION_QUALITY.md`.
- **Guards on the documentation itself**: every command in the README and in
  `SKILL.md` must parse, `--format` choices must match the exporters, and four
  claims are held under embargo until they are earned.

## 6. The bug classes this build keeps producing

**Controls wired to nothing** was session two's class and still applies. Session
three added three more, and all three were found by the operator looking at a
screen while the full suite was green.

### The display reads the wrong row

`_stage_progress` selected a stage's state with `ORDER BY id`, keeping the last.
Ids are random hex, so "last" meant whichever attempt sorted highest — not a
fact about time. A restarted worker left attempt 1 abandoned at no total, and
the screen reported *that* while attempt 2 ran correctly beside it. The data was
right and the read was wrong.

Now ordered by `attempt`, and `/api/progress` averages only the latest attempt
per stage.

### A containment assertion is not an equality assertion

Three separate times a test passed against the exact defect it was written to
catch:

- `" left"` in the body passed against `"about about 7½ hours left"`.
- `"https://"` anywhere on the page flagged a legitimate placeholder while the
  test's real subject was notification paths.
- `label in rendered` passed against the drift `"Google" → "Google Gemini"`,
  because one contains the other.

**Treat substring assertions as suspect.** Match the whole rendered value.

### A test that never reached its subject

Two tests created jobs against `/nonexistent/a.mp4`; preflight refused before
any row was written, so they asserted on nothing. Two more read a form that only
renders when there is something to collect, on an empty install. And one test
wrote a real key into the operator's macOS Keychain, found only by noticing "Key
set" on an unrelated screen.

**Assert that the thing under test actually happened before asserting about it.**

### The checkout hides packaging bugs

Everything resolves relative to the repository when you run from a source tree,
so a file that never made it into the wheel is still found. `migrations_dir()`
pointed outside the package for four sessions and 1,371 green tests. **A source
tree cannot catch this class at all** — only building the artifact, installing
it somewhere else, and running it there can. CI does that now.

### The documentation is not covered by the tests unless you cover it

The README promised a command that did not exist for three sessions. A skill
file is worse: an agent executes it literally, so a wrong command becomes a loop
of failing shell calls on a stranger's machine. Both are now parsed and checked
against the real parser.

### The one that keeps costing hours

**Templates reload from disk; route code does not.** Three incidents this
session: every service on the settings screen lost its name; a Check button
reported "Not Found"; and a restart recommended in good faith destroyed three
hours of description work. §5E is the mitigation, not a cure.

### Guards worth keeping

| Guard | Where |
|---|---|
| Cross-origin writes and `/api/` reads are refused | `test_origin_boundary.py` |
| A failed delete never destroys artifacts first | `test_delete_safety.py` |
| A retried stage resumes rather than restarting | `test_resume_across_attempts.py` |
| A stage in flight is visibly working | `test_stage_progress.py` |
| An estimate ignores work this run did not do | `test_stage_progress.py` |
| A model belongs to its own provider | `test_provider_models.py` |
| The template's label fallback matches the shared one | `test_provider_models.py` |
| A stale build says so, and a template edit does not | `test_stale_build.py` |
| Folder names are safe on every platform | `test_job_files.py` |
| A job's output stays in one folder | `test_job_files.py` |
| No screen has markup in its `<title>` | `test_accessibility.py` |
| Every screen renders with no videos / no artifacts / empty install | `test_web_ui.py` |
| The schema is inside the installed package | `test_db.py` |
| Every documented CLI command actually parses | `test_headless_cli.py` |
| A `--format` choice matches an exporter that exists | `test_headless_cli.py` |
| A citation resolves to a video, not to `analysis_input/` | `test_headless_cli.py` |
| Exports never parse the rendered document | `test_headless_cli.py` |
| An agent cannot select a paid provider | `test_mcp_tools.py` |
| Processing the same files twice reuses the finished job | `test_mcp_tools.py` |
| Every command the skill names exists | `test_skill_and_plugin.py` |
| Every command the README shows parses | `test_readme_claims.py` |
| The README makes no claim that is not yet earned | `test_readme_claims.py` |
| The prompt defines what `confidence` measures | `test_visual_prompt.py` |

---

## 7. Known limitations — carry these forward honestly

- ~~**The descriptions on the one real run were worthless.**~~ **Answered — see
  `docs/DESCRIPTION_QUALITY.md`.** They were not. The content was largely correct
  (right instrument, right timeframe, real values read off the screen); only
  `confidence` was useless, and only because the prompt defined it as a feeling
  — "Set confidence to Low whenever you are unsure", which a careful model
  satisfies by answering Low every time. A legibility rubric moved the same five
  sampled frames to four Medium and one High. Still open: **accuracy is
  unmeasured**.
- **The description schema stays as it is — decided, not pending.** Five of the
  eight content fields (`timeframe`, `currency_pair`, `indicators_and_states`,
  `exact_action`, `setup_type`) describe trading charts, because the build spec
  was written around a trading course. Generalising them into per-job profiles
  was proposed, costed, and **declined by the operator on 15 August 2026**.
  Do not re-open it without being asked.

  What that means in practice, so nobody rediscovers it as a bug: on video that
  is not a chart those five fields come back `Unknown`. The model declines
  rather than inventing — verified against frames of a colour test pattern — so
  this is wasted prompt and wasted columns, not wrong output. `visible_text`,
  `visual_description` and `confidence` are true of any video and carry the
  value on general content.

  The reasoning against it is still in `docs/DESCRIPTION_QUALITY.md`; treat that
  section as a record of a road not taken rather than a plan.
- **CI has never executed.** No git remote, so GitHub Actions has never run.
  The matrix is now nine cells (3 OS × 3 Python) plus an installed-wheel job and
  a container build, none of which has run either.
  **Windows and Linux are untested.** Everything was built on macOS (Apple
  Silicon). Platform-specific paths — symlink fallback, directory fsync, keyring
  backends, the hard-link fallback in reruns, the new folder-name slug — are
  written defensively and unit-tested, nothing more.
- **No cloud provider has ever been called.** By design: no test makes a paid
  call. Five adapters are mock-verified against documented request/response
  shapes. **Local Ollama *is* verified live** against Ollama 0.32.6 +
  `qwen2.5vl:7b`. The new `list_models` paths for cloud providers are therefore
  unexercised against a real service.
- **Transcription accuracy is unmeasured**, and Whisper hallucinates on the
  sample's tone — the transcript opens with "Thanks for watching!".
- **Describing locally runs at ~31 s/picture** on this machine, measured over
  1,488 of them. The sample job therefore declines descriptions.
- **`drawtext` is absent from this machine's FFmpeg**, so the sample carries no
  on-screen text and `visible_text` comes back Unknown.
- **The event log grows without bound.** Progress events are rate-limited to one
  per ten minutes precisely so this feature does not make it worse, but the
  underlying growth is unaddressed.
- **`/settings` shells out to `ffmpeg -version` on every render** (capped at
  10 s), so it can be slow under heavy load.
- **One job at a time, and no way to jump the queue.** `claim_next_job` takes
  the single oldest `ready` job (`app/worker/runner.py:178`) and `process_job`
  does not return until every video in it is done. A job queued behind a long
  one waits for the whole thing. Observed 2026-08-12: a 13-video job sat at
  `ready` with `started_at` NULL while a one-video job ahead of it ground
  through local descriptions, and only started when that job left the loop.
  This is the pause bug's twin — both come from the worker treating a claimed
  job as uninterruptible. See §8.
- **Pausing a running job does nothing until its current video ends.**
  `pause_job` writes `status='paused'` to `jobs` and `job_videos`
  (`app/services/jobs.py:186-197`) and its docstring promises to "stop a job
  after the current step". Nothing implements that promise: neither
  `process_job` nor `process_video` re-reads `jobs.status` after claiming, and
  the only in-loop check is `self.stopping` (`app/worker/runner.py:203`), which
  is worker shutdown, not job pause. `grep -n paused app/worker/runner.py`
  returns nothing. Two consequences, both seen live: frames keep going to the
  model after the user asked it to stop — on a cloud provider that keeps
  spending against the budget past the stop request — and the next
  `_set_job_status` writes `analyzing` straight over `paused`
  (`app/worker/runner.py:284-285`), so the pause silently evaporates and the
  interface shows Paused over work that is still running.
- **Deleting a job the worker is mid-flight on races the worker.** Observed
  2026-08-12: deleting the running job removed its output folder, the next
  stage write hit `IntegrityError: FOREIGN KEY constraint failed`, and the job
  settled as `needs_attention` before the loop moved on. It self-recovered and
  the queue drained correctly, so this is untidy rather than dangerous — but
  the error is the worker discovering the deletion by crashing into it, not by
  being told. Fixing the pause check above fixes this too, since both need the
  same "re-read the row before writing to it" discipline.
- **Latent, deliberately left alone:** eleven unused decorative classes in
  `tokens.css`, and three schema columns nothing reads (`jobs.settings_json`,
  `stage_runs.output_version`, `events.detail_json`). `stage_runs.items_skipped`
  was the fourth and now carries the carried-forward count.

---

## 8. Where to pick up

`docs/LAUNCH_CHECKLIST.md` is the full list, sequenced. The short version, in
order of value:

1. **Make pause actually pause.** The worst thing in this list, and the only
   *defect* among mostly gaps: the interface reports Paused over work that is
   still running, and on a cloud provider it keeps spending past the stop
   request. A control that reports a state it has not reached is worse than one
   that is missing. Item 8 below specifies the fix; it also resolves the
   delete-race and unblocks the queue, because all three are the same worker
   assumption. Do it before anything else here.
2. **Get a git remote and let CI run.** The highest-value thing nobody can do
   from a laptop. Nine test cells, an installed-wheel job, and a container build
   have never executed. Expect Windows to surface something.
3. **Exercise one cloud provider for real**, on a small job with a low cap. The
   adapters, the Check button, and the budget path have never met a real service.
   `test_readme_claims.py` refuses to let the README say otherwise until this
   happens.
4. **Run `scripts/benchmark.py` over five or six real videos** before any of §10
   goes on a website. n=1, and a chart screencast is close to the best case. The
   harness exists now and prints median and range; it just needs real material
   and a few hours of wall clock.
5. **Measure description accuracy.** The confidence field varies now. Whether a
   `High` reading is *correct* is untested.
6. **Prune the event log.**
7. **Decide on the unused design-system CSS and the three dead columns** (§7).
8. **Make the worker interruptible, then let several jobs run at once.** Two
   steps, in this order — the second is unsafe without the first.

   **Step one: honour the control the interface already offers.** The worker
   treats a claimed job as uninterruptible, which is why pause does nothing and
   why a deletion arrives as a foreign-key crash (§7). Re-read `jobs.status`
   between videos in `process_job` and between stages in `process_video`, and
   make `_set_job_status` refuse to write over a `paused` or `cancelled` row
   rather than clobbering it. The visual stage runs in frame batches, so check
   there too — that is what makes a pause feel immediate on the long jobs this
   product is built for, instead of arriving thirty seconds later. Worth a test
   that pauses a job mid-stage and asserts both that the work stops and that
   the row still reads `paused` afterwards; today neither holds.

   **Step two: more than one job at a time.** The obvious shape is N worker
   loops over the same output root, but the claim is the hard part, not the
   loop. `claim_next_job` reads and the caller writes `preparing` in a separate
   statement, so two workers can select the same row — it needs to become one
   atomic claim (`UPDATE ... WHERE status='ready'` returning the claimed id)
   under the existing `BEGIN IMMEDIATE` discipline. Then the ownership model
   has to change with it: `worker.lock` currently claims *the output root*, and
   the takeover-a-stale-claim path (`app/core/locks.py`) is written on the
   assumption that a second live worker is the thing it exists to prevent.
   Per-job claims with their own heartbeats, not one root-level claim.

   Two constraints that are easy to lose here. Concurrency must stay bounded by
   something real — local description already runs at ~31 s/picture on one job,
   and two jobs against the same Ollama endpoint will contend for it rather
   than go twice as fast, so the useful default is probably still 1 with an
   explicit opt-in. And the budget cap is per-job today; two jobs billing the
   same provider in parallel need a shared ceiling, or the cap means less than
   it says it does.

### Kept for near-term consideration, not scheduled

Raised, thought about, and deliberately not started. Recorded here so the next
session inherits the thinking rather than the idea — and so none of them is
mistaken for a defect.

| | What | Why it is worth doing | Why it is not started |
|---|---|---|---|
| **Interface** | Stage progress in the user's words — "Reading the audio, 12 of 40 minutes covered" rather than a stage name | The numbers are already published; only the wording is missing. Cheapest remaining interface win | Proposed alongside the pre-run panel and the receipt; those two were chosen first |
| **Interface** | `/settings` caches `ffmpeg -version` instead of shelling out per render | It is capped at 10 s, so a slow machine makes the settings screen feel broken | Small, uncontroversial, nobody has been blocked by it |
| **Input** | Optional URL input via `yt-dlp`, behind an extra | Every comparable tool takes a URL; "local files only" reads as unfinished rather than deliberate to a drive-by visitor. The privacy claim survives — yt-dlp fetches *to* your disk | Cut from scope deliberately. Revisit only if the launch shows people bouncing off it |
| **Feature** | `video-to-llm watch <dir>` — process anything dropped into a folder | One sentence sells it to the self-hosting audience | P2. Better as an issue that attracts a contributor than as code nobody asked for |
| **Feature** | `video-to-llm search "<phrase>"` across processed jobs | Turns an output folder into a library. Arguably the feature that makes the corpus feel like an asset | P2, and the largest of these |
| **Feature** | Speaker diarisation | The most-requested feature in every adjacent project's tracker | P2, and a real dependency decision |
| **Feature** | Context-pack presets named for model windows rather than a raw token number | The number is already explained; the preset is a nicety | P2 |
| **Docs** | Host `docs/` as a site | Twelve genuinely good files nobody will click through GitHub's file browser to read | Needs the repository to exist first |

Three more live in `docs/LAUNCH_CHECKLIST.md` rather than here, because none of
them is a code change: the `OWNER` placeholder in seven files, the repository
settings to paste in, and the demo recording. The container image is there too —
it has never been built, because this machine has no Docker.

---

## 9. Working agreements that have served this build well

- **Run it, don't just read it.** Every defect in session three that mattered was
  found by using the app. The suite was green for all of them.
- **When you find a bug, write the test that catches its class**, not the
  instance. Then revert the fix and watch the test fail. This session that
  practice caught four tests that would otherwise have passed vacuously,
  including two written minutes earlier.
- **Assert the whole value, not a substring.** See §6.
- **Assert that the subject exists before asserting about it.** See §6.
- **Never let a test touch the real machine.** Fake the keyring, redirect the
  settings file, use `tmp_path`. One test stored a credential in the operator's
  Keychain.
- **Tests assert behaviour and say why.** Comments explain the failure being
  prevented, not what the line does.
- **Commit at each coherent milestone** with a message explaining *why*, and the
  bugs found along the way. Run format, lint, mypy, tests, audit, smoke first.
- **Never weaken a check to make it pass.** When a check fires on something
  legitimate, make the check *more precise* rather than more permissive — the
  `https://` placeholder became a match on fetch-causing positions, not a
  removed assertion.
- **A control that changes no behaviour is a lie**, and it remains the defect
  this codebase produces most often.

---

## 10. The token arithmetic, and what it is worth

Measured on `video02trendlines.webm` — 49:39, 1360×768, 1,488 frames.

| Route | Tokens |
|---|---:|
| Send the video itself (Gemini-style, 1 fps + audio) | 863,910 |
| Extract frames yourself, send 1,488 images | 2,072,784 |
| **This app — assembled document** | **316,503** |
| This app — transcript only | 14,868 |

Against sending frames — the realistic alternative, since Claude and OpenAI
accept no video — the saving is **85%**. Against native video it is 63%, but the
app samples at 2s where Gemini samples at 1s, so that comparison is not
like-for-like and should not be quoted without the caveat.

**Positioning advice given, and the reasoning:** lead with *process once, ask
forever, nothing leaves your machine* rather than with token savings. Savings
erode as context windows grow and video pricing falls; reusability, privacy and
citable evidence do not. Use only the 85% figure, footnote the arithmetic, and
avoid the transcript-only 99% — it is real, but it argues that the product's
headline feature can be switched off with no loss, which is a conversation to
have internally (§7, §8.1) before having it with customers.

---

## 11. First message to send

> Read `HANDOFF.md`. The working tree is clean and everything is committed —
> start by telling me what you understand the current state to be, and what you
> would do first.
