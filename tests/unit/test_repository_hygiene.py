"""The repository must stay safe to publish at every commit, not only at the end.

These tests encode the specification's export rules (§10) so a regression shows
up as a failing test rather than as a discovery after the repository is public.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO / "scripts"))
from pre_publish_audit import audit  # noqa: E402


def test_pre_publish_audit_reports_no_findings():
    findings = audit()
    assert findings == [], "Pre-publish audit findings:\n" + "\n".join(f"  - {f}" for f in findings)


def test_pre_publish_audit_script_exits_zero():
    result = subprocess.run(
        [sys.executable, "scripts/pre_publish_audit.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "template",
    [
        ".env.example",
        "config/settings.example.toml",
        "config/pricing.example.toml",
        "config/providers.example.toml",
    ],
)
def test_safe_templates_exist(template):
    assert (REPO / template).is_file()


@pytest.mark.parametrize(
    "real_file",
    [".env", "config/settings.toml", "config/pricing.toml", "config/providers.toml"],
)
def test_real_configuration_files_are_never_tracked(real_file):
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", real_file],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"{real_file} is tracked by git"


def test_gitignore_covers_every_required_category():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    required = [
        ".env",
        "*.key",
        "*.pem",
        "*.db",
        "*.db-wal",
        "*.log",
        "models/",
        "*.mp4",
        "*.mov",
        "*.webm",
        "*.wav",
        "*.jpg",
        "*.png",
        "output/",
        "exports/",
        "collections/",
        ".DS_Store",
        "__pycache__/",
    ]
    missing = [pattern for pattern in required if pattern not in ignored]
    assert not missing, f".gitignore is missing: {missing}"


def test_env_example_is_not_ignored():
    # The negation rule has to survive edits to the surrounding patterns.
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=REPO,
        capture_output=True,
    )
    assert result.returncode != 0, ".env.example is being ignored; the '!' rule broke"


def test_env_file_is_ignored():
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO, capture_output=True)
    assert result.returncode == 0, ".env is NOT ignored"


def test_no_tracked_file_carries_a_real_api_key_shape():
    # Belt and braces alongside the audit: this asserts on git's own file list.
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    )
    exempt = {
        "app/core/redaction.py",
        "tests/unit/test_redaction.py",
        "tests/unit/test_repository_hygiene.py",
        "scripts/pre_publish_audit.py",
        ".gitleaks.toml",
        "uv.lock",
    }
    offenders = []
    for path in (p for p in result.stdout.split("\0") if p):
        if path in exempt or Path(path).suffix.lower() not in {
            ".py",
            ".toml",
            ".md",
            ".yaml",
            ".yml",
            ".json",
            ".html",
            ".css",
            ".js",
            ".sh",
            ".ps1",
        }:
            continue
        text = (REPO / path).read_text(encoding="utf-8", errors="ignore")
        if "sk-ant-api" in text or "BEGIN RSA PRIVATE KEY" in text:
            offenders.append(path)
    assert not offenders, f"credential-shaped literals in: {offenders}"


# ── Every module that ships has to be in the repository ───────────────────
#
# `.gitignore` carried `collections/` to keep processed output out of the
# repository. Unanchored, it also matched `app/collections/`, so the four source
# files that build collections were never committed and never shipped in the
# wheel. Nothing noticed: the whole suite passes locally because the files are
# on disk, and it took the container job on the first CI run to surface
# `ModuleNotFoundError: No module named 'app.collections'` from an installed
# copy. These assert the property that was silently false.


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


def test_every_python_package_under_app_is_tracked():
    """A package git does not know about cannot be installed by anyone else."""
    tracked = _tracked_files()
    missing = []
    for init in sorted((REPO / "app").rglob("__init__.py")):
        if "__pycache__" in init.parts:
            continue
        relative = init.relative_to(REPO).as_posix()
        if relative not in tracked:
            missing.append(relative)
    assert not missing, (
        "these packages exist on disk but are not in the repository, so an "
        f"installed copy will not have them: {missing}"
    )


def test_no_source_file_under_app_is_ignored():
    """Catches the pattern rather than the one instance of it.

    An output-directory name that also names a source directory is an easy
    mistake to repeat — `frames/`, `exports/` and `analysis_input/` are all in
    `.gitignore` for the same reason `collections/` was.
    """
    sources = [
        path
        for path in (REPO / "app").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".html", ".css", ".sql"}
        and "__pycache__" not in path.parts
    ]
    assert sources, "no source files found — the glob is wrong, not the repository"

    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO,
        input="\n".join(str(p.relative_to(REPO).as_posix()) for p in sources),
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = [line for line in result.stdout.split() if line]
    assert not ignored, f"source files under app/ that git is ignoring: {ignored}"


def test_the_collections_module_is_importable_by_name():
    """The specific regression. `app.collections` is a headline feature and was
    absent from every installed copy."""
    import importlib

    for name in ("app.collections", "app.collections.build", "app.collections.model"):
        assert importlib.import_module(name) is not None
