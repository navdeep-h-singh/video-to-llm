<h1 align="center">Video → LLM</h1>

<p align="center">
  <strong>Turn hours of local video into one timestamped, citable document
  your LLM can actually read.</strong><br>
  Process once. Ask forever. Nothing leaves your machine.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue" alt="Python 3.11 to 3.13">
  <img src="https://img.shields.io/badge/tests-1%2C575-brightgreen" alt="1,575 tests">
  <img src="https://img.shields.io/badge/network-not%20required-ec3013" alt="Runs offline">
  <img src="https://img.shields.io/badge/licence-MIT-lightgrey" alt="MIT licence">
</p>

---

Your LLM cannot watch a forty-hour course. Uploading it is slow, often
expensive, and frequently not allowed. Extracting the frames yourself costs
millions of tokens and throws away the audio.

`video-to-llm` turns each video into **one chronological document** — speech,
marked silences, section headings, and optionally what was on screen — all in
the order it happened, every line carrying a timestamp that resolves back to the
exact frame it came from.

Do it once. Ask as many questions as you like, of as many models as you like,
for as long as you keep the folder.

## Quickstart

```bash
uvx video-to-llm process lecture.mp4 --transcribe-model tiny
```

That is the fast way to see it work: the `tiny` speech model is a **75 MB**
download rather than the default's **1.4 GB**, so you get a real document in
about a minute instead of waiting on a download to find out whether you like
the output. Once you do, drop the flag:

```bash
uvx video-to-llm process lecture.mp4
```

> **Not on PyPI yet.** Until the first release lands, clone the repository and
> run `uv sync`, then `uv run video-to-llm process lecture.mp4`.

You need [FFmpeg](https://ffmpeg.org) on your `PATH` — version 4 through 9, all
fine. The speech model downloads once, on first use, and after that nothing
touches the network again.

```bash
video-to-llm doctor          # check this machine is ready
```

`tiny` is quick and noticeably less accurate; `medium` is the default because it
is the smallest one worth keeping a transcript from. `--transcribe-model` also
takes `base`, `small`, and `large-v3`.

## What you get

This is the actual output — real lines from a real 49-minute recording, in the
default configuration:

```
00:01:30  [nobody speaking · 11 seconds]
00:01:40  Yep, just so that you know, the first thing you'll do is you'll take everything out of
00:01:48  your chart.
00:01:49  When you're going to analyze the chart.
00:01:54  [nobody speaking · 4 seconds]
00:01:58  I think all of you know what trends are, right?
00:02:01  You're either going up or you're either coming down. Very simple, right?
00:02:05  But when you look at trends in terms of structure, what we are looking for is the basic.
```

That is the whole artifact: plain text, chronological, timestamped to the
source, and yours. Hand it to any model. Grep it. Keep it for a decade.

### Check any line

Every timestamp resolves back to the picture behind it:

```bash
video-to-llm show lecture 00:02:05
```

```
lecture.mp4 — 00:02:05
in job 'lecture'

  00:02:01  You're either going up or you're either coming down. Very simple, right?
> 00:02:05  But when you look at trends in terms of structure, what we are looking for is the basic.

Picture: ~/Documents/VideoToLLM/lecture/…/frames/000062_t000124.jpg
```

A claim your model makes is only evidence if you can check it. This is how.

## How it compares

Honest in both directions — including where this is the wrong tool.

| | video-to-llm | Clip tools (`/watch`, `claude-real-video`) | Upload the video |
|---|---|---|---|
| Video length | Hours. Tested on 49 min / 1,488 frames and a 15 h course | Minutes; frame caps of 50–150 by default | Minutes to an hour, at cost |
| Asking a second question | Free and instant — reuses the document | Re-downloads and re-processes | Re-uploads or re-pays |
| Survives a crash mid-job | Yes, resumes the exact stage | No | n/a |
| Many videos, one ordered document | Yes | No | No |
| Leaves your machine | Never, unless you opt in per job | Varies — `claude-real-video` transcribes locally; `/watch` sends audio to Groq or OpenAI when a video has no captions | Entirely |
| Spoken language | Any — detected, or named with `--language` | Varies; `/watch` requests English captions | Any |
| Cost control | Cap checked before each request | None | Pay per call |
| Cite a claim back to a frame | Yes | No | No |
| **Setup time** | **Minutes: FFmpeg and a 75 MB model** | **Seconds** | **Seconds** |
| **One quick clip** | **Overkill — use something else** | **Ideal** | **Ideal** |

Checked against those projects' own documentation on 21 August 2026. They move;
if a row here has gone stale, that is a bug worth reporting.

## Using it

### From the command line

```bash
video-to-llm process lecture.mp4                      # one video
video-to-llm process w1.mp4 w2.mp4 --name "Course"    # several, in your order
video-to-llm process talk.mp4 --interval 5            # fewer pictures, faster
video-to-llm process demo.mp4 --describe local        # add screen descriptions
video-to-llm process talk.mp4 --format jsonl          # also emit structured data
video-to-llm show "Course" 01:12:30                   # resolve a citation
video-to-llm export "Course" --format srt             # subtitles, no reprocessing
video-to-llm status                                   # what is done, what is running
video-to-llm run-next "Course"                        # jump the queue
```

### From an agent

```bash
video-to-llm mcp        # MCP server on stdio; needs the [mcp] extra
```

Four tools — `process_video`, `list_videos`, `get_transcript`, `get_segment`.
`process_video` is idempotent: asked for a video it has already done, it returns
the existing document instead of doing the work again. That is the whole point.
Process a forty-hour course once; every question after that is instant, offline,
and free.

An agent cannot choose a paid description service through these tools. That
decision belongs on the settings screen, where the estimate and the spending cap
are visible.

There is also a skill, which teaches an agent to check what is already processed
before doing anything expensive:

```bash
npx skills add navdeep-h-singh/video-to-llm -g
```

That works in Codex, Cursor, Copilot, Gemini CLI, and anything else that reads
Agent Skills. For Claude Code, the plugin registers the skill and the MCP server
together:

```bash
/plugin marketplace add navdeep-h-singh/video-to-llm
/plugin install video-to-llm@video-to-llm
```

### From the browser

```bash
video-to-llm start
```

An interface on `127.0.0.1` with live progress, a frame reviewer, job control,
and the collection builder. Closing the browser does not stop a job.

## Collections

Several already-processed videos, in an order you set explicitly, assembled into
one document or into numbered parts sized to fit a context window. Building a
collection **re-uses existing output** — it never re-extracts a frame,
re-transcribes audio, or re-runs a description.

Order is never inferred from filename, date, or content. Two recordings from the
same morning have no inherent sequence, so you say what it is.

## Privacy, as mechanism

Not a promise — a set of properties with tests that fail when they regress.

- The interface binds `127.0.0.1`, asserted at application construction.
- One middleware refuses a foreign `Host` and a foreign origin on every write.
- No page loads an off-origin resource. No CDN, no web font, no analytics.
- Your source videos are never copied, never moved, never uploaded.
- Keys live in the OS keychain — macOS Keychain, Windows Credential Manager,
  Linux Secret Service. **No plaintext fallback is ever created**, and a stored
  key is never rendered back to you, not even a prefix.
- There are no accounts, no telemetry, and nothing to sign in to.
- Descriptions are off by default. A job that leaves them off makes no network
  request at all.
- Local never silently falls back to cloud.

## Optional: screen descriptions

Off by default. When you turn them on you choose between your own
[Ollama](https://ollama.com) model — frames stay on the device, no charge — and
a service (Claude, Gemini, OpenAI, or any OpenAI- or Anthropic-compatible
endpoint), which receives **only the numbered still pictures**, never the video
and never the audio. You see what will be sent and roughly what it costs before
anything leaves, and processing stops at a cap you set.

## Requirements

- Python 3.11, 3.12, or 3.13
- FFmpeg with `ffprobe` on your `PATH`
- Room for the speech model — 75 MB for `tiny`, 1.4 GB for the default
  `medium` — plus room for frames: a 2-hour video at one picture every
  2 seconds is roughly 2 GB

A GPU is optional everywhere. Transcription runs on CPU on every platform.

**FFmpeg 9 is fine.** It removed `-vsync`, which every FFmpeg before 5 needed;
extraction reads the version once and asks for whichever flag that build
accepts, so 4.x through 9.x all work. `video-to-llm doctor` prints the version it
found and the flag it will use. Development here was against 8.1.2; the rest is
covered by tests on the argument list and by the CI matrix.

### Other ways to install

```bash
uv tool install video-to-llm            # or: pipx install video-to-llm
uv sync                                 # from a clone
docker build -t video-to-llm .          # command line only, see the Dockerfile
```

## Known limitations

Carried here deliberately rather than left for you to discover.

- **Screen descriptions are beta, and their structured fields are written for
  one domain.** They work — on a 49-minute chart recording the model read the
  right instrument, the right timeframe, and real values off the screen — but
  five of the eight fields (`timeframe`, `currency_pair`,
  `indicators_and_states`, `exact_action`, `setup_type`) describe trading
  charts, and that is a deliberate choice rather than an oversight. **On any
  other kind of video those five come back `Unknown`.** The model declines
  rather than inventing, so the result is thin rather than wrong, and
  `visible_text`, `visual_description` and `confidence` still apply to anything.
  Accuracy is unmeasured either way. See
  [`docs/DESCRIPTION_QUALITY.md`](docs/DESCRIPTION_QUALITY.md) for the
  experiment behind this.

  The transcript, the timeline and the citations are the parts that work on
  every video. Judge the tool on those.
- **Transcription accuracy is unmeasured**, and Whisper sometimes writes a line
  over silence — a transcript opening with "Thanks for watching!" is the model,
  not your video.
- **Local descriptions are slow**: roughly 31 s/picture on an Apple Silicon Mac,
  measured over 1,488 of them. Use a coarser `--interval`, or a service with a cap.
- **No cloud provider has been exercised against a live service.** Five adapters
  are verified against documented request and response shapes; local Ollama is
  verified live against 0.32.6 with `qwen2.5vl:7b`.
- **Windows and Linux have never executed this code.** Development was entirely
  on macOS (Apple Silicon). The CI matrix covers all three operating systems and
  all three Python versions, but it has not run yet — there is no remote. Paths,
  keyring backends, symlink and hard-link fallbacks are written defensively and
  unit-tested, and nothing more than that should be assumed.
- **The container ships the command line, not the interface** — the interface
  binds loopback, which inside a container is unreachable from the host.
- **No URL downloading.** Local files only. Fetch it yourself first.
- **The event log grows without bound.**

## Documentation

| Document | Covers |
|---|---|
| [`docs/DESCRIPTION_QUALITY.md`](docs/DESCRIPTION_QUALITY.md) | What the description model actually produces, and what it does not |
| [`docs/PIPELINE_CONTRACT.md`](docs/PIPELINE_CONTRACT.md) | Stage inputs, outputs, guarantees |
| [`docs/COLLECTIONS.md`](docs/COLLECTIONS.md) | Collections and context packs |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Secret handling and the localhost boundary |
| [`docs/LOCAL_OLLAMA.md`](docs/LOCAL_OLLAMA.md) | Running descriptions on this computer |
| [`docs/RECOVERY.md`](docs/RECOVERY.md) | After a crash, a sleep, or a cancel |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Running, pausing, monitoring |
| [`docs/IMPORT_EXPORT.md`](docs/IMPORT_EXPORT.md) | Bringing earlier work in, taking output out |
| [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) | Per-platform setup |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Choices made at build time, and why |

## Contributing

Bug reports from real use are worth more than anything else right now,
especially on Windows and Linux. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Licence

MIT. See [`LICENSE`](LICENSE).
