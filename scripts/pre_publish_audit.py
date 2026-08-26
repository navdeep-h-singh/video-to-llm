#!/usr/bin/env python3
"""Audit the repository for anything that must not be published.

Run before making this repository public, and on every commit through the
pre-commit hook. Exits non-zero on any finding.

This is a *tracked-files* audit. It inspects what git would publish, not what
happens to be sitting in the working tree — an ignored artifact directory full of
someone's frames is fine, the same directory accidentally staged is not.

Checked:

1. No file whose extension marks it as a credential, a database, a log, source
   media, or an image.
2. No real configuration file where only a template belongs.
3. No credential-shaped literal in any tracked text file.
4. No absolute path into a user's home directory.
5. No non-loopback bind address or Ollama endpoint.
6. Every template that must exist does exist.
7. `.gitignore` still covers each category the specification requires.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ── Rules ─────────────────────────────────────────────────────────────────

FORBIDDEN_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
    ".der",
    ".keystore",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".log",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".m4v",
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".aac",
    ".ogg",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".webp",
}

FORBIDDEN_PATHS = {
    ".env",
    "config/settings.toml",
    "config/pricing.toml",
    "config/providers.toml",
}

REQUIRED_TEMPLATES = [
    ".env.example",
    "config/settings.example.toml",
    "config/pricing.example.toml",
    "config/providers.example.toml",
    ".gitignore",
    ".gitleaks.toml",
]

REQUIRED_IGNORE_PATTERNS = [
    (".env", "environment file"),
    ("*.key", "private keys"),
    ("*.db", "database"),
    ("*.log", "logs"),
    ("models/", "model weights"),
    ("*.mp4", "source media"),
    ("*.jpg", "extracted images"),
    ("collections/", "collection output"),
    (".DS_Store", "OS files"),
]

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9]{32,}")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{20,}")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Credentials in URL", re.compile(r"://[^:/@\s]+:[^@/\s]{6,}@")),
]

HOME_PATH = re.compile(r"(?:/Users/|/home/|C:\\Users\\)(?!runner\b)[A-Za-z0-9._\-]+[/\\]")

NON_LOOPBACK_BIND = re.compile(
    r"""(?ix)
    (?: host \s* [:=] \s* ["']  0\.0\.0\.0  ["']
      | \b uvicorn \b .* --host \s+ 0\.0\.0\.0
      | ollama[_\-]?(?:endpoint|host|url) \s* [:=] \s* ["']?
        https?://(?! 127\.0\.0\.1 | localhost | \[::1\] ) [^\s"']+
    )
    """
)

# Files exempt from a given class of check, with the reason.
SECRET_SCAN_EXEMPT = {
    "scripts/pre_publish_audit.py",  # contains the patterns themselves
    ".gitleaks.toml",  # ditto
    "app/core/redaction.py",  # ditto
    "tests/unit/test_redaction.py",  # synthetic shapes, asserted synthetic
    "docs/SECURITY.md",
    "docs/SECURE_GITHUB_EXPORT.md",
    "uv.lock",
}

HOME_PATH_EXEMPT = {
    "scripts/pre_publish_audit.py",
    ".gitleaks.toml",
}

BIND_SCAN_EXEMPT = {
    "scripts/pre_publish_audit.py",
    ".gitleaks.toml",
    "docs/SECURITY.md",
}

# The bind check alone is skipped under tests/. A test that proves "0.0.0.0" is
# rejected has to name "0.0.0.0", so flagging it would mean deleting the test
# that enforces the boundary. Scoped deliberately: the secret and home-path
# scans still apply to tests, because there is no comparable reason for a test
# to contain a real key or someone's home directory.
BIND_SCAN_EXEMPT_PREFIXES = ("tests/",)

TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".ps1",
    ".example",
    "",
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def is_text(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_SUFFIXES


#: Where the README's own images live. The image suffixes above exist to keep a
#: user's video and its extracted frames out of the repository, and they caught
#: the demo recording and the screenshots too — which are deliberate, reviewed,
#: and the whole reason anyone can see what this produces without installing it.
#: Only images, and only here: a video committed anywhere is still a finding.
ARTWORK_DIR = "docs/assets/"
ARTWORK_SUFFIXES = {".png", ".gif", ".jpg", ".jpeg", ".webp"}

#: A published asset is downloaded by everyone who clones. Two megabytes is
#: generous for a screenshot and tight enough to notice a mistake.
MAX_ARTWORK_BYTES = 2_000_000


def _is_published_artwork(path: str, suffix: str) -> bool:
    return path.startswith(ARTWORK_DIR) and suffix in ARTWORK_SUFFIXES


def audit() -> list[str]:
    findings: list[str] = []
    files = tracked_files()

    if not files:
        return ["No tracked files found — is this a git repository with a commit?"]

    # 1 & 2 — file identity
    for path in files:
        suffix = Path(path).suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES and not _is_published_artwork(path, suffix):
            findings.append(f"{path}: tracked file has forbidden extension '{suffix}'")
        if _is_published_artwork(path, suffix):
            size = (REPO / path).stat().st_size
            if size > MAX_ARTWORK_BYTES:
                findings.append(
                    f"{path}: published artwork is {size / 1_000_000:.1f} MB, over the "
                    f"{MAX_ARTWORK_BYTES / 1_000_000:.0f} MB cap — every clone pays for it"
                )
        if path in FORBIDDEN_PATHS:
            findings.append(
                f"{path}: real configuration file is tracked; only the template belongs here"
            )

    # 3, 4, 5 — content
    for path in files:
        if not is_text(path):
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if path not in SECRET_SCAN_EXEMPT:
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    line = text[: match.start()].count("\n") + 1
                    findings.append(f"{path}:{line}: possible {label}")

        if path not in HOME_PATH_EXEMPT:
            match = HOME_PATH.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                findings.append(
                    f"{path}:{line}: absolute path into a home directory ({match.group(0)!r})"
                )

        if path not in BIND_SCAN_EXEMPT and not path.startswith(BIND_SCAN_EXEMPT_PREFIXES):
            match = NON_LOOPBACK_BIND.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                findings.append(f"{path}:{line}: non-loopback bind address or endpoint")

    # 6 — templates present
    for template in REQUIRED_TEMPLATES:
        if not (REPO / template).exists():
            findings.append(f"{template}: required template is missing")

    # 7 — .gitignore coverage
    gitignore = REPO / ".gitignore"
    if gitignore.exists():
        ignored = gitignore.read_text(encoding="utf-8")
        for pattern, label in REQUIRED_IGNORE_PATTERNS:
            if pattern not in ignored:
                findings.append(f".gitignore: no rule covering {label} ('{pattern}')")

    return findings


#: Assembled rather than written out, so this file does not match its own check.
OWNER_PLACEHOLDER = "github.com/" + "OWNER" + "/"


def unresolved_placeholders() -> list[str]:
    """Where the repository still says OWNER instead of an account name.

    There is no git remote yet, so the package metadata, the plugin manifest and
    the README all carry a placeholder where the account belongs. Each one is a
    dead link the moment anything ships: a PyPI page pointing at a 404, a plugin
    whose homepage does not exist, README badges that never render.

    Deliberately **not** part of `audit()`. Every day this sits unpublished is a
    day the placeholder is the honest value, and failing the ordinary build over
    it would leave CI permanently red for a condition nobody can fix until the
    remote exists. It blocks the release instead, where it is genuinely fatal —
    see `--release` and the publish workflow.
    """
    found: list[str] = []
    for path in tracked_files():
        if not is_text(path):
            continue
        try:
            content = (REPO / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if OWNER_PLACEHOLDER in line:
                found.append(f"{path}:{number}: still says OWNER where the account name goes")
    return found


def main(argv: list[str] | None = None) -> int:
    releasing = "--release" in (argv if argv is not None else sys.argv[1:])

    findings = audit()
    placeholders = unresolved_placeholders()
    if releasing:
        findings.extend(placeholders)

    if findings:
        print("Pre-publish audit FAILED\n", file=sys.stderr)
        for finding in findings:
            print(f"  ✗ {finding}", file=sys.stderr)
        print(f"\n{len(findings)} finding(s). Resolve before publishing.", file=sys.stderr)
        return 1

    print(f"Pre-publish audit passed — {len(tracked_files())} tracked files, no findings.")
    if placeholders:
        # Not a failure yet, but it must be impossible to forget. `--release`
        # turns every one of these into a blocking finding.
        print(
            f"\nNote: {len(placeholders)} unresolved OWNER placeholder(s). "
            "Set the real account before publishing — `--release` refuses while they remain."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
