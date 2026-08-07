#!/usr/bin/env bash
# Checks and guidance for Linux. Not an installer, and it never uses sudo — it
# prints the command for your distribution and lets you run it yourself.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Video to LLM — Linux setup check"
echo

install_hint() {
  if   command -v apt    >/dev/null 2>&1; then echo "sudo apt install $1"
  elif command -v dnf    >/dev/null 2>&1; then echo "sudo dnf install $1"
  elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S $1"
  elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install $1"
  else echo "install $1 with your package manager"
  fi
}

ready=1

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "FFmpeg is missing. Run:"
  echo "  $(install_hint ffmpeg)"
  ready=0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is missing. Run:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  ready=0
fi

# A secure store is preferred but not required: without one the application
# falls back to process-scoped environment variables and refuses to write a
# key to a file. Local-only processing needs no key at all.
if ! command -v secret-tool >/dev/null 2>&1; then
  echo
  echo "Note: no Secret Service keyring was found."
  echo "  Local processing needs no API key and works without one."
  echo "  For a service you have an account with, set the key in the environment"
  echo "  for the session instead. A key is never written to a file."
fi

if [ "$ready" -eq 0 ]; then
  echo
  echo "Install the above, then run this script again."
  exit 1
fi

echo "Installing this project's dependencies with uv..."
uv sync

echo
echo "Checking..."
uv run video-to-llm doctor
