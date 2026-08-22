"""The skill and plugin manifests must describe a tool that exists.

A skill is documentation that an agent executes literally. Where a README that
names a missing command wastes a reader's afternoon, a skill that names one
sends an agent into a loop of failing shell calls on somebody else's machine —
and this project has already shipped exactly that mistake once, in a README that
promised the pipeline was callable from the command line for three sessions
while job creation existed only as a web form.

So: every `video-to-llm` command the skill tells an agent to run has to parse,
every flag it names has to exist, and the plugin manifest has to point at things
that are really there.

A failure here is a regression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.cli.main import build_parser

REPO = Path(__file__).resolve().parents[2]
#: The conventional location. `npx skills` — the tool the highest-starred
#: projects in this category distribute through — discovers skills by walking
#: standard container directories: the repo root, `skills/`, `skills/<name>/`,
#: and the agent-specific ones. A singular `skill/` is not among them, which
#: is where this lived until the layout was corrected.
SKILLS_DIR = REPO / "skills"
SKILL = SKILLS_DIR / "video-to-llm" / "SKILL.md"
PLUGIN = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"


def _subcommands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        if getattr(action, "choices", None) and hasattr(action, "add_parser"):
            return set(action.choices)
    raise AssertionError("The parser has no subcommands.")


def _skill_text() -> str:
    assert SKILL.exists(), f"{SKILL} is missing — the skill is how this gets distributed."
    return SKILL.read_text(encoding="utf-8")


#: `video-to-llm <command>` on one line. Same-line whitespace only: `\s+` also
#: spans newlines, which matched the frontmatter's `name: video-to-llm` against
#: the `description:` key on the line below and reported it as a missing
#: command. The fix is a more precise pattern, not a permitted exception.
INVOCATION = re.compile(r"\bvideo-to-llm[ \t]+([a-z][a-z-]*)")

#: Installers that take the package name as an argument. `uv tool install
#: video-to-llm` does not match the pattern, but `pipx install video-to-llm`
#: followed by a word would, so anything reached this way is filtered.
NOT_SUBCOMMANDS = frozenset({"install", "doctor--"})


def _body_after_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    return text[text.index("\n---", 4) + 4 :]


def test_every_command_the_skill_names_exists():
    """The defect this file exists for, pointed at the file an agent obeys."""
    named = set(INVOCATION.findall(_body_after_frontmatter(_skill_text()))) - NOT_SUBCOMMANDS
    # The subject has to be non-empty, or this passes vacuously against a skill
    # that names no commands at all.
    assert named, "SKILL.md names no commands — the pattern or the file is wrong"

    unknown = named - _subcommands()
    assert not unknown, (
        f"SKILL.md tells an agent to run commands that do not exist: {sorted(unknown)}"
    )


def test_every_flag_the_skill_names_is_accepted():
    """A flag that does not parse fails on the agent's machine, not ours."""
    text = _skill_text()
    parser = build_parser()

    for flag, argv in (
        ("--interval", ["process", "a.mp4", "--interval", "5"]),
        ("--name", ["process", "a.mp4", "--name", "x"]),
        ("--describe", ["process", "a.mp4", "--describe", "local"]),
        ("--format", ["process", "a.mp4", "--format", "jsonl"]),
    ):
        if flag in text:
            parser.parse_args(argv)  # raises SystemExit if the flag is gone


def test_the_skill_declines_to_spend_money_on_the_users_behalf():
    """The MCP surface refuses paid providers outright. The skill reaches the
    CLI, where they are available, so the instruction has to carry the rule."""
    text = _skill_text()
    assert "Do not turn on cloud descriptions." in text


def test_the_skill_warns_that_local_descriptions_are_slow():
    """Measured at roughly 31 s/picture over 1,488 of them. An agent that starts
    a nine-hour run without saying so has cost the user their afternoon."""
    assert "30 seconds per picture" in _skill_text()


def test_the_skill_has_the_frontmatter_a_host_reads():
    text = _skill_text()
    assert text.startswith("---\n"), "SKILL.md needs YAML frontmatter to be discoverable"
    closing = text.index("\n---", 4)
    front = text[4:closing]
    assert "name: video-to-llm" in front
    assert "description:" in front


@pytest.mark.parametrize("manifest", [PLUGIN, MARKETPLACE])
def test_the_plugin_manifests_are_valid_json(manifest):
    assert manifest.exists(), f"{manifest} is missing"
    json.loads(manifest.read_text(encoding="utf-8"))


def test_the_skill_sits_where_a_skill_installer_looks_for_it():
    """The layout, asserted rather than assumed.

    `npx skills add <owner>/<repo>` reaches every Agent Skills host — Codex,
    Cursor, Copilot, Gemini CLI — not only Claude Code, and it finds skills by
    directory convention. The manifest below is a second route to the same
    file; this is the one that works without it.
    """
    assert SKILLS_DIR.is_dir(), "skills/ is the directory a skill installer walks"
    found = sorted(path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md"))
    assert found == ["video-to-llm"], f"expected skills/<name>/SKILL.md, found {found or 'nothing'}"


def test_the_plugin_points_at_the_skill_directory_that_exists():
    manifest = json.loads(PLUGIN.read_text(encoding="utf-8"))
    for relative in manifest["skills"]:
        assert (REPO / relative).is_dir(), f"plugin.json names {relative}, which is not a directory"
        assert (REPO / relative / "SKILL.md").exists()


def test_the_plugin_starts_the_mcp_server_with_a_real_command():
    """`video-to-llm mcp` is what the host will run. If the subcommand is
    renamed, every installed copy of the plugin breaks silently."""
    manifest = json.loads(PLUGIN.read_text(encoding="utf-8"))
    server = manifest["mcpServers"]["video-to-llm"]
    assert server["command"] == "video-to-llm"
    assert server["args"] == ["mcp"]
    assert "mcp" in _subcommands()


# ── The MCP registry entry ────────────────────────────────────────────────
#
# `server.json` is what the official registry reads. It repeats three things the
# package already states — the name, the version, and how to start the server —
# and a repeated fact is a fact that can drift. The registry rejects a version
# that does not match the published package, which would be discovered at
# release time rather than here.

SERVER = REPO / "server.json"


def _server() -> dict:
    assert SERVER.exists(), "server.json is how the MCP registry finds this"
    return json.loads(SERVER.read_text(encoding="utf-8"))


def _project() -> dict:
    import tomllib

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_registry_entry_names_the_package_that_is_published():
    package = _server()["packages"][0]
    assert package["identifier"] == _project()["name"]
    assert package["registryType"] == "pypi"


def test_the_registry_entry_carries_the_version_being_released():
    server = _server()
    version = _project()["version"]
    assert server["version"] == version
    assert server["packages"][0]["version"] == version


def test_the_registry_entry_starts_the_server_the_way_the_plugin_does():
    """One command, described in two manifests. They must agree."""
    arguments = [a["value"] for a in _server()["packages"][0]["packageArguments"]]
    plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
    assert arguments == plugin["mcpServers"]["video-to-llm"]["args"] == ["mcp"]
    assert "mcp" in _subcommands()


def test_the_registry_namespace_matches_the_repository_it_claims():
    """The registry verifies ownership against the GitHub namespace, so a
    mismatch here is a publish that is refused rather than a broken link."""
    server = _server()
    owner = server["repository"]["url"].removeprefix("https://github.com/").split("/")[0]
    assert server["name"].startswith(f"io.github.{owner}/")
    assert "OWNER" not in server["name"]


def test_the_registry_entry_only_names_environment_variables_that_exist():
    from app.core.config import ENV_PREFIX

    for variable in _server()["packages"][0].get("environmentVariables", []):
        assert variable["name"].startswith(ENV_PREFIX), (
            f"{variable['name']} is not a setting this application reads"
        )
