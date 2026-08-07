#!/usr/bin/env python3
"""Check this computer can run the application.

Deliberately dependency-free and standard-library only, so it works *before*
anything is installed — a checker that needs the project's own dependencies
cannot tell you they are missing.

This is not an installer. It reports what is present, what is not, and the exact
command to fix each gap. Installing software on someone's machine without asking
is not this script's job.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

MINIMUM_PYTHON = (3, 11)

INSTALL_HINTS = {
    "Darwin": {
        "ffmpeg": "brew install ffmpeg",
        "uv": "brew install uv",
        "ollama": "brew install --cask ollama-app   (optional)",
    },
    "Windows": {
        "ffmpeg": "winget install Gyan.FFmpeg",
        "uv": "winget install astral-sh.uv",
        "ollama": "winget install Ollama.Ollama   (optional)",
    },
    "Linux": {
        "ffmpeg": "sudo apt install ffmpeg    (or your distribution's equivalent)",
        "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "ollama": "curl -fsSL https://ollama.com/install.sh | sh   (optional)",
    },
}

OK, WARN, FAIL = "OK  ", "--  ", "FAIL"


def hint(tool: str) -> str:
    return INSTALL_HINTS.get(platform.system(), INSTALL_HINTS["Linux"]).get(tool, "")


def check_python() -> tuple[str, str, str]:
    version = sys.version_info
    label = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= MINIMUM_PYTHON:
        return OK, "Python", label
    return (
        FAIL,
        "Python",
        f"{label} — this project needs {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer. "
        f"{hint('uv')} then: uv python install 3.11",
    )


def check_tool(name: str, *, required: bool, version_args: list[str]) -> tuple[str, str, str]:
    found = shutil.which(name)
    if not found:
        return (
            FAIL if required else WARN,
            name,
            f"not found on your PATH. {hint(name)}",
        )
    try:
        result = subprocess.run(
            [found, *version_args], capture_output=True, text=True, timeout=15, check=False
        )
        first = (result.stdout or result.stderr or "").splitlines()
        return OK, name, first[0][:90] if first else "present"
    except (OSError, subprocess.SubprocessError) as error:
        return FAIL if required else WARN, name, f"present but would not run: {error}"


def check_disk() -> tuple[str, str, str]:
    from pathlib import Path

    try:
        free_gb = shutil.disk_usage(Path.home()).free / 1024**3
    except OSError as error:
        return WARN, "Free space", f"could not be read: {error}"

    hours = free_gb / 2.16
    if free_gb < 5:
        return WARN, "Free space", f"{free_gb:.1f} GB — about {hours:.1f} hours of video"
    return OK, "Free space", f"{free_gb:.0f} GB — about {hours:.0f} hours of video"


def main() -> int:
    print(f"Checking this computer ({platform.system()} {platform.machine()})\n")

    checks = [
        check_python(),
        check_tool("ffmpeg", required=True, version_args=["-version"]),
        check_tool("ffprobe", required=True, version_args=["-version"]),
        check_tool("uv", required=True, version_args=["--version"]),
        check_tool("ollama", required=False, version_args=["--version"]),
        check_disk(),
    ]

    width = max(len(name) for _, name, _ in checks)
    for marker, name, detail in checks:
        print(f"[{marker}] {name.ljust(width)}  {detail}")

    failures = [name for marker, name, _ in checks if marker == FAIL]
    print()
    if failures:
        print(f"Not ready yet — install: {', '.join(failures)}")
        print("Then run this script again.")
        return 1

    optional = [name for marker, name, _ in checks if marker == WARN]
    if optional:
        print(f"Ready. Optional and not installed: {', '.join(optional)}")
    else:
        print("Ready.")
    print("\nNext:  uv sync  then  uv run video-to-llm doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
