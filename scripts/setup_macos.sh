#!/usr/bin/env bash
# Checks and guidance for macOS. Not an installer — it tells you what to run and
# lets you decide. Nothing is installed without you typing the command yourself.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Video to LLM — macOS setup check"
echo

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is not installed. It is the easiest way to get the rest:"
  echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  echo
fi

missing=()
command -v ffmpeg  >/dev/null 2>&1 || missing+=("ffmpeg")
command -v ffprobe >/dev/null 2>&1 || missing+=("ffmpeg")
command -v uv      >/dev/null 2>&1 || missing+=("uv")

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing. Run:"
  printf '  brew install %s\n' "$(printf '%s\n' "${missing[@]}" | sort -u | tr '\n' ' ')"
  echo
  echo "Then run this script again."
  exit 1
fi

echo "Installing this project's dependencies with uv..."
uv sync

echo
echo "Checking..."
uv run video-to-llm doctor
