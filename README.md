# Video → LLM

Turn local video files into timestamped, reviewable, reusable evidence — then
combine independently processed videos into ordered, LLM-ready **Collections**.

Everything runs on your own computer. The interface is served on the loopback
interface only. Your source videos are never copied, never moved, and never
uploaded.

---

## What it does

For each video you give it, the pipeline produces:

- **Clean sampled frames** at a fixed interval you choose (½ – 10 seconds), plus
  a separate set of small numbered copies used only for alignment when an
  optional description model is involved.
- **A local transcript** with the original timeline preserved and stretches of
  quiet marked explicitly.
- **Optional structured visual descriptions** of each frame — off by default,
  and never required.
- **`assembled.txt`** — the transcript, the quiet stretches, the descriptions and
  the section markers woven together in chronological order.
- **Manifests and provenance** recording exactly what produced the output, with
  which settings, and when.
- **An `analysis_input` folder** ready to hand to whatever you use next.

Later, a **Collection** takes several already-processed videos, puts them in an
order you set explicitly, and assembles them — either as one long document or as
numbered parts sized to fit a context window. Building a collection re-uses the
existing output; it never re-extracts frames, re-transcribes audio, or re-runs
visual analysis.

## What it is not

Not a cloud product, a chatbot, a video editor, a remote-access system, a
reasoning engine, or an automated decision system. There are no accounts, no
telemetry, no uploads, and nothing to sign in to.

---

## Requirements

- **Python 3.11** and [`uv`](https://docs.astral.sh/uv/)
- **FFmpeg** (with `ffprobe`) on your `PATH`
- Roughly 2 GB of disk for the speech-to-text model, plus room for the frames
  you extract — a 2-hour video at one picture every 2 seconds is about 2 GB.

A GPU is optional everywhere. Transcription runs on the CPU on every supported
platform. macOS, Windows, and Linux are supported equally.

## Setup

```bash
uv sync
```

Platform helper scripts live in `scripts/` (`setup_macos.sh`,
`setup_windows.ps1`, `setup_linux.sh`). They check your environment and tell you
what is missing — they are not installers, and they do not modify your system
without asking.

Verify the result:

```bash
uv run video-to-llm doctor
```

## Running

```bash
uv run video-to-llm start
```

This starts the local interface and the background worker together, then prints
the address to open. The worker is independent: **closing the browser does not
stop a job**, and a job resumes safely after a restart or a sleep.

To run the two separately:

```bash
uv run video-to-llm start-ui
uv run video-to-llm run-worker
```

Other commands:

| Command | What it does |
|---|---|
| `doctor` | Checks FFmpeg, transcription, output root, disk, and worker state |
| `status` | Prints current jobs, stages, and worker health |
| `smoke-test` | End-to-end run on generated synthetic media, no network |
| `import <path>` | Brings previously processed output back under management |

The whole pipeline is callable from the command line without ever opening the
interface.

---

## Optional: visual descriptions

Off by default. A job that never turns this on produces frames, a transcript, and
an assembled document without any network access at all.

When you do turn it on, you choose between:

**On this computer** — a vision model you have installed yourself, through
[Ollama](https://ollama.com). Frames stay on this device and there is no provider
charge; local compute, battery, heat, memory, and time apply instead. The
application never installs, starts, updates, or bundles Ollama — that stays
entirely under your control. Only loopback endpoints (`127.0.0.1`, `localhost`,
`::1`) are accepted.

> Local models are less reliable for tiny text, dense labels, exact values, and
> strict structured extraction. Review low-confidence results.

**Send to a service** — Anthropic Claude, Google Gemini, OpenAI, or any
OpenAI-compatible endpoint. Only the numbered still pictures are sent, never your
video and never its audio. You see what will be sent and roughly what it costs
before anything leaves the machine, and processing stops at a spending cap you
set. Model identifiers are free text; there is no fixed catalogue.

There is never an automatic fall back from a local model to a cloud one.

## Where your keys live

In your operating system's secure store — macOS Keychain, Windows Credential
Manager, or a Linux Secret Service keyring. Where no secure store exists, a
process-scoped environment variable is accepted instead. **No plaintext fallback
is ever created**, and a stored key is never displayed back to you, written into
the database, the logs, a manifest, an artifact, an export, or a collection.

---

## Documentation

| Document | Covers |
|---|---|
| `docs/DECISIONS.md` | Choices made at build time and why |
| `docs/LOCAL_SETUP.md` | Per-platform setup detail |
| `docs/PIPELINE_CONTRACT.md` | Stage inputs, outputs, and guarantees |
| `docs/LOCAL_OLLAMA.md` | Running descriptions on this computer |
| `docs/COLLECTIONS.md` | Building collections and context packs |
| `docs/OPERATIONS.md` | Running, pausing, monitoring |
| `docs/RECOVERY.md` | What happens after a crash, a sleep, or a cancel |
| `docs/IMPORT_EXPORT.md` | Bringing earlier work in, taking output out |
| `docs/SECURITY.md` | Secret handling and the localhost boundary |
| `docs/UX_NOTES.md` | How the supplied design maps onto the implementation |
| `docs/SECURE_GITHUB_EXPORT.md` | Publishing this repository safely |

## Licence

MIT.
