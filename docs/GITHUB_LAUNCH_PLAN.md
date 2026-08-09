# GitHub Launch & Positioning Plan — Video → LLM

**Written:** 9 August 2026
**Subject:** `video-to-llm` v1.0.0, 15,515 lines of application Python, 1,371 tests, 46 test
modules, no git remote, zero public existence.
**Method:** competitive scan of the GitHub category (star counts read live from the GitHub API
on 9 Aug 2026), a read of the repository as a stranger would find it, and a read of `HANDOFF.md`
for what is true versus what is merely built.

---

## 0. The verdict in one page

**The category is real, it is hot, and you are not in it.**

A repository created on 24 April 2026 — `bradautomates/claude-video`, a shell of ffmpeg and
Whisper wrapped as a Claude Code slash command — reached **14,711 stars in fifteen weeks**. It
does a small fraction of what you have built. It won because it shipped where the users already
were: the Agent Skills / plugin marketplace, installed with one line, running in a tool people
already had open.

Meanwhile you have a genuinely superior engine — resumable across crashes, 1,488 frames on a
49-minute video, ordered multi-video collections, budget caps checked before spend, an origin
boundary, provenance manifests, 1,371 tests — sitting on a laptop with **no remote, no PyPI
package, no way to process a video without opening a browser, and a Python version pin that
locks out everyone on 3.12 and 3.13.**

The gap is not quality. The gap is **surface area and reach**.

Three decisions make or break this launch:

1. **Position as the long-video, process-once tool.** Every competitor is built for "let Claude
   watch this ten-minute YouTube clip, once." `claude-real-video` caps at 150 frames by default.
   Nobody in the category resumes a three-hour job, and nobody assembles fifteen hours of video
   into one ordered corpus. That is your moat and nobody is contesting it.
2. **Ship two surfaces, not one.** An Agent Skill / MCP server is the *distribution*; the local
   app is the *product*. The skill is what earns 5,000 stars. The app is what makes people stay.
   Shipping only the app means competing for attention with a `pip install` you don't have.
3. **Do not market the visual descriptions yet.** Per `HANDOFF.md` §7, all 1,479 descriptions on
   the one real workload came back `confidence: Low`, and the structured fields contradict
   themselves across consecutive frames. This is the headline feature and it has never produced
   a usable result. Launching on it is a credibility bomb with a fuse of about one week.

---

## 1. The landscape

### 1a. Direct competitors — "video into something an LLM can read"

Star counts read from the GitHub API on 9 August 2026.

| Repository | Stars | Created | What it is | Where it stops |
|---|---:|---|---|---|
| [`bradautomates/claude-video`](https://github.com/bradautomates/claude-video) | **14,711** | Apr 2026 | `/watch` slash command; downloads, extracts frames, transcribes, hands to Claude | One-shot, per-question. Re-downloads and re-processes each time. Whisper via **Groq/OpenAI API** — not local. No persistence, no review, no multi-video |
| [`HKUDS/VideoRAG`](https://github.com/HKUDS/VideoRAG) | 3,265 | Feb 2025 | KDD'26 paper implementation; cross-video graph retrieval, hundreds of hours on one RTX 3090 | Research code. Needs a GPU, a knowledge-graph build, and a tolerance for research repos. No UI, no packaging, no provenance |
| [`wendy7756/AI-Video-Transcriber`](https://github.com/wendy7756/AI-Video-Transcriber) | 3,013 | Aug 2025 | Transcribe + summarise videos and podcasts | Transcript-only. No visual channel at all |
| [`HUANGCHIHHUNGLeo/claude-real-video`](https://github.com/HUANGCHIHHUNGLeo/claude-real-video) | 1,984 | Jun 2026 | `pip install`; scene-aware keyframes, sliding-window dedup, local Whisper, contact sheets | **Default cap 150 frames.** Re-running overwrites the output directory. Single video. Has a paid upsell |
| [`byjlw/video-analyzer`](https://github.com/byjlw/video-analyzer) | 1,533 | Nov 2024 | Frame-by-frame vision LLM with rolling context | Single video, no resume, no budget control, dormant since Apr 2026 |
| [`jordanrendric/claude-video-vision`](https://github.com/jordanrendric/claude-video-vision) | 1,175 | Mar 2026 | Claude Code plugin, TypeScript, ffmpeg + Gemini/Whisper/OpenAI | Plugin-only, cloud audio backends by default |
| [`Leon1207/Video-RAG-master`](https://github.com/Leon1207/Video-RAG-master) | 451 | Nov 2024 | NeurIPS'25; OCR + ASR + detection as visually-aligned aux text | Research code, benchmark-shaped |
| [`guimatheus92/mcp-video-analyzer`](https://github.com/guimatheus92/mcp-video-analyzer) | 42 | Mar 2026 | MCP server, many platforms, OCR | Early |
| [`codependentai/video-watch-mcp`](https://github.com/codependentai/video-watch-mcp) | 7 | Mar 2026 | **Cloud-hosted** MCP; container with ffmpeg + Whisper | Your exact inverse: it uploads |
| [`mugnimaestra/video-frames-skill`](https://github.com/mugnimaestra/video-frames-skill) | 8 | Feb 2026 | Frame extraction skill with model-aware tile alignment | Frames only |

**Read of the table.** The distribution of stars is not a distribution of quality. It is a
distribution of *installability*. The top four are all one-line installs into a tool the user
already runs. The bottom five are all good ideas with a `git clone` in front of them.

### 1b. Adjacent categories — proof the wedge exists

| Repository | Stars | Why it matters to you |
|---|---:|---|
| [`obra/superpowers`](https://github.com/obra/superpowers) | 269,623 | The Agent Skills ecosystem is the single largest attention pool in developer tooling right now |
| [`anthropics/skills`](https://github.com/anthropics/skills) | 167,190 | Same. A skill is a distribution channel, not a feature |
| [`openai/whisper`](https://github.com/openai/whisper) | 106,964 | Your transcription lineage; huge inbound search surface |
| [`yamadashy/repomix`](https://github.com/yamadashy/repomix) | 27,716 | **The single best template for your launch.** "Pack X into an AI-friendly file." Started as a CLI, grew CLI + web + extension + MCP. Proves the "process once → hand to any model" category tops out very high |
| [`SYSTRAN/faster-whisper`](https://github.com/SYSTRAN/faster-whisper) | 24,824 | Your dependency. Its README is the credibility bar for a local-inference tool |
| [`chidiwilliams/buzz`](https://github.com/chidiwilliams/buzz) | 20,860 | Proves people will install a **local desktop GUI** for offline transcription. Your UI is not a liability |
| [`Huanshere/VideoLingo`](https://github.com/Huanshere/VideoLingo) | 18,091 | Long-form video processing pipeline with a GUI. Same shape as you, different vertical |
| [`mufeedvh/code2prompt`](https://github.com/mufeedvh/code2prompt) | 7,543 | "Codebase → one prompt." Direct structural analogue |
| [`thewh1teagle/vibe`](https://github.com/thewh1teagle/vibe) | 7,044 | "Transcribe on your own!" — privacy-first local transcription, 7k stars on that message alone |
| [`simonw/files-to-prompt`](https://github.com/simonw/files-to-prompt) | 2,771 | Minimal tool, large reach, from a trusted voice |

**Read.** Repomix at 27.7k and Buzz at 20.9k together prove the two halves of your thesis:
people want their content packed for an LLM, and people will run a local app to keep it private.
Nobody has combined those for video at any scale. That is the hole.

### 1c. What nobody in the category has built

Cross-checking every repo above against your feature set, these are **uncontested**:

| Capability | Anyone else? |
|---|---|
| Resume a partially-described job across a crash, restart, or sleep | **No** |
| Multiple already-processed videos assembled into one ordered corpus | **No** (VideoRAG does cross-video *retrieval*, not an ordered document) |
| Spending cap enforced **before** the request is sent | **No** |
| Provenance manifest: what produced this, with which settings, when | **No** |
| Frame-by-frame review UI with timestamp jump | **No** |
| Explicit, tested refusal to auto-fall-back from local to cloud | **No** |
| Origin boundary on a localhost app | **No** — and most of them are wide open |
| Context packs sized to a specific model's window | **No** |
| Hours-long input as the design centre rather than the edge case | **No** |

That list is your entire marketing strategy. It is also, currently, invisible.

---

## 2. What a stranger finds today — an honest audit

I read this repo as a drive-by GitHub visitor would. Findings, worst first.

### Blocker 1 — There is no way to process a video from the command line

`app/cli/main.py:142` defines exactly seven subcommands: `start`, `start-ui`, `run-worker`,
`doctor`, `status`, `smoke-test`, `import`. **There is no command that creates a job.** Job
creation exists only as a web form.

The README states, at line 108: *"The whole pipeline is callable from the command line without
ever opening the interface."* That is not true today.

This is simultaneously a documentation defect, the reason you cannot ship a skill or an MCP
server, and the reason nobody can try the tool in a terminal in thirty seconds. **Fix this
first — everything else in this plan depends on it.**

### Blocker 2 — Python is pinned to 3.11 only

`pyproject.toml:6` — `requires-python = ">=3.11,<3.12"`. Python 3.13 is the default on current
Homebrew, Ubuntu 25.10, and most fresh installs. Every `uvx`, `pipx install`, and `uv tool
install` from a 3.12/3.13 machine fails at resolution with a message the user will not debug.
This silently destroys a large share of your install attempts.

### Blocker 3 — Not installable

No PyPI package. No Docker image. No `uvx`. The install path is `git clone` → `uv sync` → 2 GB
Whisper download → find FFmpeg → `uv run video-to-llm start` → open a browser. Against
`/plugin install watch@claude-video`, this loses before the comparison starts.

### Blocker 4 — No agent surface

No MCP server, no Agent Skill, no plugin manifest. The four highest-star competitors are all
distributed this way. You have written the hard part — the pipeline — and skipped the part that
gets it in front of people.

### Blocker 5 — No URL input

Every competitor accepts a YouTube/TikTok/Loom URL. You accept local files only. To you this is
a principled boundary; to a visitor scrolling the README it reads as a missing feature. See §4.5
for the recommended resolution — it does not require compromising the privacy claim.

### Blocker 6 — The README shows nothing

No badges, no screenshot, no GIF, no sample output, no comparison table. Your best asset is the
`assembled.txt` file — a chronologically interleaved transcript, silence markers, and
descriptions with timestamps — and **it does not appear anywhere in the README.** Show the
artifact. It sells itself and no competitor has one to show.

### Blocker 7 — CI has never executed

`.github/workflows/ci.yml` is well built: three operating systems, ruff, mypy, 1,371 tests, a
pre-publish audit, a synthetic no-network smoke test, and gitleaks. It has never run once.
Windows and Linux have never executed a line of this code. You cannot claim cross-platform
support until those matrices are green, and you cannot show a passing badge — which is the
single cheapest credibility signal on GitHub.

### Blocker 8 — The headline feature does not work

`HANDOFF.md` §7: 1,479 descriptions, **zero Medium, zero High**, fields disagreeing across
consecutive frames of the same chart. On that video the entire signal was in a 53 KB transcript.

Until this is understood, "structured visual descriptions" cannot be the pitch. The good news:
the tool is *still* differentiated without it, on long-form transcript assembly, collections,
resumability, and privacy. Lead with those and let descriptions be an opt-in beta.

---

## 3. Positioning

### 3.1 The wedge

> **Everyone else built "let an LLM watch a ten-minute video."
> You built "turn fifteen hours of video into a corpus you own."**

Do not fight `claude-video` for the quick-clip use case. You will lose: it is one line to
install, it handles URLs, and it is good enough. Take the segment it structurally cannot serve.

**Who that is, concretely:**

| Audience | Their video | Why the clip tools fail them | Where to find them |
|---|---|---|---|
| Course / bootcamp buyers | 10–40 hours across dozens of files | 150-frame caps; no ordering; re-processing every question | r/LocalLLaMA, r/ObsidianMD, Discord course communities |
| Researchers with interview or field recordings | Hours, confidential | **Cannot upload.** Non-negotiable | Academic Twitter, r/AskAcademia, qualitative-research forums |
| Lawyers / investigators / compliance | Depositions, bodycam, CCTV | Need provenance and citable timestamps; cannot upload | Legal-tech newsletters, r/LawFirm |
| Self-hosters and archivists | Terabytes of local video | Want everything local, batch, resumable | **r/selfhosted, r/DataHoarder** — badly under-served, ideal fit |
| Support / QA / product teams | Screen recordings, user sessions | Need reuse across many questions, not one answer | Indie Hackers, r/ExperiencedDevs |
| Conference / meetup organisers | Whole tracks of talks | Ordered multi-video output | Dev community Slacks |

**r/DataHoarder and r/selfhosted are the sleeper channels.** They are large, they are hostile
to cloud, they have enormous local video libraries, and no tool in this category is speaking to
them at all.

### 3.2 The three claims, in priority order

1. **Process once, ask forever.** Every competitor re-downloads and re-processes for each
   question. You process once and reuse the output across unlimited questions, unlimited models,
   and unlimited collections. This is the sharpest, most defensible line you have.
2. **Nothing leaves your machine — and that is enforced, not promised.** Loopback-only binding
   asserted at app construction, an origin boundary middleware, no CDN, no webfont, no
   telemetry, keys in the OS keychain and never rendered back, and a test suite that fails if any
   of that regresses. Say the mechanism, not the adjective. "Private" is a claim; "no page loads
   an off-origin resource and a test enforces it" is evidence.
3. **Built for hours, not minutes.** Resumable, crash-safe, progress measured in seconds of
   video covered, 1,488 frames on a real 49-minute job, budget-capped before spend.

Token savings — the 85% figure in `HANDOFF.md` §10 — is a **footnote, not a headline**, and the
reasoning there is correct: savings erode as context windows grow, reusability and privacy do
not. Additionally, the figure is n=1 on a chart screencast, which is close to best case. Measure
five or six videos before it goes above the fold anywhere. Never quote the transcript-only 99%.

### 3.3 Naming

Keep **`video-to-llm`** as the repository name, package name, and command.

It is not clever, and that is the point. It is what people type into GitHub search. `repomix`
and `gitingest` both succeeded on literal names. Your current folder name — "Video Processor for
LLMs", with spaces — should not survive contact with the internet.

- **Repo:** `video-to-llm`
- **Command:** `video-to-llm` (unchanged)
- **PyPI:** `video-to-llm`
- **Display name in the README H1:** `Video → LLM` (unchanged — it reads well and the arrow is
  distinctive in a search result)

### 3.4 The GitHub About field

This is the highest-leverage 120 characters in the project. It appears in search results, topic
pages, and every "awesome" list. Current: none.

> Turn hours of local video into timestamped, citable, LLM-ready text. Frames + transcript +
> descriptions. Fully offline. Resumable.

### 3.5 Topics

```
llm  video  whisper  ffmpeg  local-first  privacy  offline  rag  ai-tools
transcription  video-analysis  ollama  claude  self-hosted  context-engineering
mcp  agent-skills  faster-whisper  python  multimodal
```

### 3.6 Social preview image

Required — it is what renders when the link is posted to Hacker News, Reddit, X, or Slack.
Recommended composition, in the Modernist identity you already have (vermilion `#ec3013`, warm
greys, zero radius, grotesque type):

> **VIDEO → LLM**
> 15 hours of video. One document. Nothing uploaded.
> *[a strip of the actual `assembled.txt` beneath, timestamps visible]*

---

## 4. Product enhancements, ranked by adoption impact

Each item is scored on effort against the adoption it unlocks.

### P0 — Cannot launch without these

**P0.1 · A headless CLI that processes a video.**
`video-to-llm process <path> [--interval 2] [--describe local|none] [--out DIR] [--json]`,
running to completion and printing the output path. This closes the README's false claim, gives
every terminal user a thirty-second trial, and is the precondition for P1.1 and P1.2.
*Effort: small — the pipeline, the worker, and job creation all exist; this is a CLI entry point
over `app/services/jobs.py` plus a synchronous `run-worker --once`.*

**P0.2 · Widen the Python pin.** `requires-python = ">=3.11,<3.14"`, and add 3.12 and 3.13 to
the CI matrix. Verify `faster-whisper` and `ctranslate2` wheels on each before promising it.
*Effort: small, plus whatever the matrix surfaces.*

**P0.3 · Publish to PyPI.** Target install experience:

```bash
uvx video-to-llm process lecture.mp4
```

*Effort: small — hatchling is already configured. Add a release workflow with trusted publishing.*

**P0.4 · Get CI green on all three operating systems.** Push the remote, let the matrix run, fix
what Windows and Linux surface. `HANDOFF.md` §7 flags symlink fallback, directory fsync, keyring
backends, hard-link fallback in reruns, and the new folder-name slug as untested platform surface.
Then put the badge in the README.
*Effort: unknown until it runs — budget a full day. Do this before any public link.*

**P0.5 · Rewrite the README.** See §5. Sample output above the fold, comparison table, demo GIF,
honest limitations section.
*Effort: half a day plus asset capture.*

**P0.6 · Diagnose the Low-confidence descriptions.** `HANDOFF.md` §8.1. Either fix it or
demote it in the copy to an explicitly labelled beta. Do not ship marketing that a first-time
user's own run will contradict.
*Effort: unknown. This is the one item where "we don't know yet" is the honest answer.*

### P1 — The growth engine, ship within four weeks of launch

**P1.1 · An MCP server.**
`video-to-llm mcp` exposing four tools: `process_video`, `get_transcript`, `get_segment`,
`build_collection`. This is how the tool gets used inside Claude Code, Cursor, Cline, Continue,
and everything else, without the user ever opening your UI. Register it with the MCP registry and
the awesome-mcp-servers lists.
*This is the highest star-per-hour item in the entire plan.*

**P1.2 · An Agent Skill / plugin package.** `SKILL.md` plus a plugin manifest, installable via
`npx skills add`, the Claude Code marketplace, and the `.skill` upload path. The competitors
prove this channel converts. Your skill's differentiator over `claude-video`: **it remembers.**
Process once; every subsequent question in every subsequent session reads the cached corpus
instead of re-downloading and re-transcribing.

**P1.3 · Timestamp citations that resolve.** Every line in `assembled.txt` already carries
`[HH:MM:SS]`. Add `video-to-llm show <job> <timestamp>` which prints the surrounding transcript
and the path to the exact frame. Now "reviewable evidence" is a demonstrable claim, not an
adjective — an LLM cites `[00:12:34]`, and one command puts the frame on screen. **No competitor
can do this**, because none of them keep the artifacts. Put it in the demo GIF.

**P1.4 · Docker image.** `docker run -v $PWD:/media ghcr.io/<you>/video-to-llm process /media/x.mp4`.
Removes the FFmpeg and Python-version problem entirely and unlocks the self-hosted audience.

**P1.5 · Structured output.** `--format json|jsonl|md|srt|vtt` alongside `assembled.txt`. JSONL
of `{t, kind, text, frame_path, confidence}` makes the tool composable into other people's
pipelines, which is how tools get embedded and stay.

**P1.6 · The benchmark, done properly.** `HANDOFF.md` §8.4 already calls for this: five or six
videos of different kinds — lecture, screencast, interview, conference talk, tutorial —
measuring tokens by route and wall-clock by stage. Publish the raw numbers and the script.
This becomes the linkable artifact that earns citations for years, and it is what turns the 85%
claim from a lab result into a finding.

### P2 — Depth, months two and three

- **`video-to-llm watch <dir>`** — process anything dropped into a folder. Sells itself to
  r/DataHoarder in one sentence.
- **Context-pack presets** sized to named model windows, rather than a raw token number.
- **Search across processed jobs** — `video-to-llm search "gradient descent"` returning
  timestamps across your whole library. This is the feature that makes the corpus feel like an
  asset rather than an output folder.
- **Speaker diarisation** — the most-requested transcription feature in every adjacent repo's
  issue tracker.
- **Prune the event log** (`HANDOFF.md` §7) — it grows without bound, and a public user base
  will find that faster than you will.
- **A hosted docs site** — the `docs/` directory has twelve genuinely good files that nobody
  will ever click through GitHub's file browser to read.

### 4.5 · The URL-input question

Recommendation: **add it, behind an explicit opt-in, and frame it honestly.**

```bash
video-to-llm process --from-url <url>     # requires yt-dlp; downloads to your machine first
```

The privacy claim survives intact: yt-dlp fetches *to* your disk, and the file is then processed
exactly like any local file. Nothing about your video is uploaded, which is what the claim
actually says. Keep it an optional extra (`pip install "video-to-llm[url]"`) so the core install
carries no yt-dlp dependency and the default path remains local-only.

Without this you lose a meaningful share of visitors at the first paragraph — not because they
disagree with the boundary, but because they will read "local files only" as unfinished rather
than as deliberate.

---

## 5. The README rewrite

Structure, in order. Everything above the "Requirements" heading must fit in one screen.

### 5.1 Above the fold

```markdown
<h1 align="center">Video → LLM</h1>

<p align="center">
  <strong>Turn hours of local video into one timestamped, citable document
  your LLM can actually read.</strong><br>
  Process once. Ask forever. Nothing leaves your machine.
</p>

<p align="center">
  <a href="…/actions"><img src="…/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/tests-1371-brightgreen" alt="1371 tests">
  <a href="https://pypi.org/project/video-to-llm/"><img src="https://img.shields.io/pypi/v/video-to-llm" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/offline-100%25-ec3013" alt="Fully offline">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT">
</p>

<p align="center"><img src="docs/assets/demo.gif" width="720" alt="…"></p>
```

Then, immediately — the three-line problem statement:

> Your LLM cannot watch a 40-hour course. Uploading it is slow, expensive, and often not
> allowed. Extracting frames yourself costs 2 million tokens and loses the audio.
>
> `video-to-llm` turns each video into one chronological document — speech, silences, on-screen
> content, and section markers, all in time order with timestamps you can cite. Do it once. Ask
> as many questions as you like, of as many models as you like, forever.

### 5.2 Quickstart — one block, no prose between the lines

```bash
uvx video-to-llm process lecture.mp4
# → ~/VideoToLLM/lecture/assembled.txt
```

### 5.3 Show the output — this is the section that converts

No competitor does this. It is your single strongest asset and it currently appears nowhere.
Paste a real excerpt of `assembled.txt`:

```
[00:04:12] SPEECH  So if we look at the moving average here, you'll notice…
[00:04:18] SCREEN  Line chart, two series. Y axis 0–140. A crossover at roughly x=60.
[00:04:31] SPEECH  …and that crossover is the signal we've been waiting for.
[00:04:44] QUIET   11 seconds
[00:04:55] SPEECH  Now, the second case is harder.
```

Follow it with one sentence: *"That is the whole artifact. Plain text, chronological, timestamped
back to the source, and yours."*

### 5.4 Comparison table

Honest, and honest in both directions — including where you lose. A table that admits weakness
is believed; a table that does not is skipped.

| | video-to-llm | Clip tools (`/watch`, `claude-real-video`) | Upload the video (Gemini) |
|---|---|---|---|
| Video length | Hours. Tested at 49 min / 1,488 frames and a 15 h course | Minutes. Frame caps around 150 | Minutes to an hour, at cost |
| Reuse across questions | Process once, reuse forever | Re-processes every time | Re-uploads or re-pays every time |
| Survives a crash mid-job | Yes — resumes the exact stage | No | n/a |
| Many videos, one ordered document | Yes | No | No |
| Leaves your machine | Never (descriptions optional and opt-in) | Audio usually via a cloud API | Entirely |
| Cost control | Cap checked before each send | None | Pay per call |
| Cite a claim back to a frame | Yes — timestamp resolves to the image | No | No |
| **Setup time** | **Minutes: FFmpeg, a 2 GB model** | **Seconds** | **Seconds** |
| **Best for a quick clip** | **Overkill** | **Ideal** | **Ideal** |

### 5.5 Then, in order

6. **How it works** — the five stages, in one short paragraph each.
7. **Collections** — several processed videos, an order you set, one document or numbered parts.
   Name-drop the use case: *a whole course, in the order you took it.*
8. **Privacy, as mechanism** — loopback binding asserted at construction; an origin boundary
   before every route; no CDN and no webfont on any page; keys in the OS keychain, never
   rendered back, no plaintext fallback; no telemetry; tests that fail if any of this regresses.
9. **Optional descriptions** — clearly labelled, with the honest caveat.
10. **Requirements and setup.**
11. **Documentation table** — as it exists today, it is good.
12. **Known limitations** — see below.
13. **Licence.**

### 5.6 The limitations section is a feature

Hacker News rewards this and nothing else in a README buys as much trust for as little work.
Adapted from `HANDOFF.md` §7, publishable as-is:

```markdown
## Known limitations

- **Visual descriptions are beta.** On our one long real-world run — 1,479 frames of a chart
  screencast — every description came back low-confidence and the structured fields disagreed
  across consecutive frames. On that video the entire signal was in the transcript. We are
  investigating whether this is the prompt, the schema, the model, or the image quality.
  Descriptions are off by default. Do not build on them yet.
- **Transcription accuracy is unmeasured**, and Whisper hallucinates on quiet openings.
- **Describing locally runs at roughly 31 s/frame** on an M-series Mac. A one-hour video at one
  frame every two seconds is a very long time. Plan accordingly, or use a cloud model with a cap.
- **No cloud provider has been exercised against a live service.** Five adapters are verified
  against documented request and response shapes; local Ollama is verified live against 0.32.6
  with `qwen2.5vl:7b`.
- **The event log grows without bound.**
```

That last block is worth more than any feature paragraph you could write in its place.

---

## 6. Launch plan

Sequenced. Do not skip ahead — a Show HN before the skill exists spends your one big shot at a
tenth of its value.

### Phase 0 — Pre-flight (target: two weeks)

1. Create the repo **private**. Push. Watch CI run for the first time.
2. Fix whatever Windows and Linux surface. Green on nine cells (3 OS × 3 Python).
3. Ship P0.1 (`process` command), P0.2 (version pin), P0.3 (PyPI).
4. Investigate P0.6 (descriptions). Whatever the answer, write the honest paragraph.
5. Capture assets: a demo GIF of `process` running end to end plus a citation resolving to a
   frame; three UI screenshots (job in progress with the live bar, the frame reviewer, the
   collection builder); the social preview card.
6. Rewrite the README. Set the About field, the topics, and the social preview.
7. Add `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` (you have `docs/SECURITY.md` —
   GitHub wants one at `.github/SECURITY.md` too), and five to eight `good first issue` tickets.
   Pull them from `HANDOFF.md` §7 and §8: prune the event log, remove the unused CSS classes,
   drop the three dead schema columns, add SRT export.
8. Seed the issue tracker with your own roadmap items. An empty tracker looks abandoned; a
   tracker with fifteen well-written issues looks like a project.

### Phase 1 — Soft launch (week 3)

Go public. **Do not post to Hacker News yet.** Post narrow, get real bugs, fix them in public.

- **r/LocalLLaMA** — the single best-matched audience on the internet for this tool. Lead with
  the local Ollama path and the "nothing uploaded" mechanism.
- **r/selfhosted** — lead with Docker and the folder-watch idea.
- **r/DataHoarder** — lead with "your archive, searchable and LLM-ready, without uploading it."
- **Ollama Discord**, **LocalLLaMA Discord**.
- Submit to awesome lists: `awesome-whisper` (sindresorhus — high traffic), `awesome-selfhosted`,
  `awesome-local-ai`, `awesome-llm-apps`.

Target: 100–300 stars, and ten strangers' bug reports. Those bug reports are the actual product
of Phase 1.

### Phase 2 — The agent surface (weeks 4–6)

Ship P1.1 (MCP) and P1.2 (skill). This is the volume phase.

- MCP registry, `awesome-mcp-servers`, `mcpservers.org`, Smithery, Glama.
- Claude Code plugin marketplace, `npx skills add`, the plugin-hub directories.
- Post to **r/ClaudeAI** and **r/mcp** with the differentiator stated plainly:
  *"Unlike the other video skills, this one caches. Process a 40-hour course once; every
  question after that is instant and free."*

Target: 1,000–3,000 stars. This is where the `claude-video` audience is, and where the "it
remembers" line does the work.

### Phase 3 — Show HN (week 7–8, only when Phases 0–2 are done)

Title matters more than the post. Candidates, best first:

1. `Show HN: Turn 15 hours of local video into one document an LLM can read`
2. `Show HN: Video → LLM – process video once, ask forever, nothing uploaded`
3. `Show HN: Local-only video processing for LLMs, built for hours not minutes`

The post should be short, admit the limitations before anyone finds them, and lead with the
`assembled.txt` sample. Post Tuesday–Thursday, 08:00–10:00 ET. Be present in the thread for six
hours straight — presence in comments outperforms the submission itself.

Also: **lobste.rs** (needs an invite; arrange one in advance), and **Product Hunt** on a separate
day if you want the non-developer audience.

### Phase 4 — The durable asset (month 3)

Publish the benchmark from P1.6 as a standalone write-up with the raw data and the script:
*"We measured what it costs to give an LLM a 49-minute video, five ways."* This is the piece
that gets cited, linked from other READMEs, and surfaces in search for years. It also finally
puts the 85% claim on evidence rather than on n=1.

---

## 7. Copy bank

**One-liner (About field, docs, everywhere):**
> Turn hours of local video into timestamped, citable, LLM-ready text. Fully offline. Resumable.

**Tagline (README hero):**
> Process once. Ask forever. Nothing leaves your machine.

**The elevator paragraph:**
> Your LLM cannot watch a forty-hour course. `video-to-llm` turns each video into one
> chronological document — speech, silences, on-screen content, section markers — with
> timestamps that resolve back to the exact frame. Do it once; ask as many questions as you
> like, of as many models as you like. Everything runs on your own computer. Your videos are
> never copied, never moved, and never uploaded.

**For r/LocalLLaMA:**
> Fully local video → LLM pipeline. FFmpeg + faster-whisper + your own Ollama vision model.
> Loopback-only, no telemetry, no accounts. Built for long video: it resumes a three-hour job
> across a crash and assembles a whole course into one ordered document.

**For r/selfhosted / r/DataHoarder:**
> Point it at your video library. It produces one timestamped text document per video that any
> LLM can read, and it never phones home. Resumable, batch-friendly, SQLite + plain files on
> disk, MIT.

**For r/ClaudeAI / r/mcp:**
> An MCP server that lets Claude read your local video — and unlike the other video skills, it
> caches. Process a forty-hour course once; every question after that is instant, offline, and
> free.

**For the legal / research audience:**
> Timestamped, reviewable evidence from your own recordings, with a provenance manifest
> recording exactly what produced each output, with which settings, and when. Nothing is
> uploaded. Nothing is inferred that was not observed — unparseable values are preserved as
> `Unknown` rather than guessed.

**Lines to retire.** "Video Processor for LLMs" (a folder name, not a product name). Anything
leading with token savings. "Enterprise-grade", "revolutionary", "powered by AI" — the whole
register is wrong for a tool whose entire credibility rests on restraint.

---

## 8. Claims you may not make yet

Discipline here is what separates a project that survives its own launch from one that does not.
Each of these becomes available the moment the corresponding work is done.

| Claim | Blocked until |
|---|---|
| "Works on Windows and Linux" | CI green on both. Until then: *"Developed on macOS; Windows and Linux supported and CI-tested"* — and only once that is true |
| "Structured visual descriptions of every frame" | `HANDOFF.md` §8.1 is resolved. Until then it is a labelled beta, off by default |
| "Works with Claude, Gemini, and OpenAI" | One real call to one real service. Until then: *"Five provider adapters, verified against documented request and response shapes. Local Ollama is verified live."* |
| "85% fewer tokens" | Fine to publish **with the footnote and the method**. Not fine as a bare headline number. Never the transcript-only 99% |
| "Accurate transcription" | Never claim it. `HANDOFF.md` §7 — accuracy is unmeasured and Whisper hallucinates on quiet openings |
| "Production-ready" / "battle-tested" | It has one operator and one real workload. Say *"1,371 tests, and one 15-hour real-world corpus"* — a specific number outperforms an adjective |

The specificity is not a hedge. In this category, where the top repo is a shell script with
14,000 stars, being the project that says exactly what it has and has not verified **is** the
differentiator. Every reader who has been burned by an over-claiming README — which is all of
them — will notice.

---

## 9. What success looks like

| Milestone | Target | Signal it is working |
|---|---|---|
| Phase 1 exit | 100–300 stars | Strangers filing bugs on Windows |
| Phase 2 exit | 1,000–3,000 stars | Inbound MCP/skill installs; someone else's README links here |
| Phase 3 (Show HN) | Front page, 3,000–8,000 stars | The comment thread argues about the *approach*, not the polish |
| Month 6 | 8,000–15,000 | A second maintainer; a fork doing something you did not plan |

**Leading indicators worth more than stars:** issues opened by people who clearly ran it; a pull
request from a stranger; someone posting *their own* `assembled.txt`; and the tool being named
in a thread you did not start.

---

## 10. The next five things

1. `git remote add` and push to a **private** repo. Let CI run for the first time today. You
   have nine matrix cells that have never executed, and every downstream decision depends on
   what they say.
2. Build `video-to-llm process <path>` and delete the false sentence at `README.md:108`.
3. Widen `requires-python` to `>=3.11,<3.14` and add 3.12/3.13 to the matrix.
4. Spend one focused session on why all 1,479 descriptions came back Low. Whatever the answer,
   you can then write an honest paragraph — and an honest paragraph is publishable, whereas an
   unknown is not.
5. Capture the demo GIF. Nothing else in this plan sells the tool as well as watching a
   forty-minute video become a document, and then watching a citation resolve back to the frame
   it came from.
