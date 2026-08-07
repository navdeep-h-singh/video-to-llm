# Setting up

Three things are required: **Python 3.11**, **`uv`**, and **FFmpeg**. Everything
else is optional.

A GPU is not needed. Transcription runs on the processor on every supported
platform, and that path is tested on all three.

---

## Check first

```bash
python3 scripts/verify_install.py
```

It uses only the standard library, so it works before anything is installed — a
checker that needed the project's own dependencies could not tell you they were
missing. It reports what is present, what is not, and the exact command for your
platform. It installs nothing.

## macOS

```bash
brew install uv ffmpeg
./scripts/setup_macos.sh
```

## Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install ffmpeg          # or your distribution's equivalent
./scripts/setup_linux.sh
```

The setup script never runs `sudo` itself. It prints the command for your
distribution and lets you decide.

If there is no Secret Service keyring on the machine, nothing breaks: local
processing needs no API key at all. For an external service, set the key in the
environment for the session instead. A key is never written to a file.

## Windows

```powershell
winget install astral-sh.uv Gyan.FFmpeg
.\scripts\setup_windows.ps1
```

Open a new terminal after installing so `PATH` updates.

Symlinks need Developer Mode or an elevated shell. Without them the handoff
folder copies the pictures instead of linking to them — it works either way, it
just uses more disk.

---

## Then

```bash
uv sync
uv run video-to-llm doctor
```

`doctor` checks the loopback binding, FFmpeg, speech-to-text, the output folder
and its free space, optional descriptions, and the background worker. Anything
not ready comes with the command to fix it.

## Running

```bash
uv run video-to-llm start
```

Starts the interface and the background worker together, then prints the address.
Closing the browser does not stop a job.

## What gets downloaded, and when

Nothing during setup. The speech-to-text model (about 1.5 GB for the `medium`
default) is fetched the first time you actually transcribe something — pulling
it during a readiness check you ran to see whether things work would be a rude
surprise. Choose a smaller model in Settings if you would rather trade accuracy
for size.

## Optional: descriptions on this computer

Install [Ollama](https://ollama.com) yourself and pull a vision model:

```bash
ollama pull qwen2.5vl:7b
```

This application never installs, starts, updates, or bundles Ollama. See
[LOCAL_OLLAMA.md](LOCAL_OLLAMA.md).
