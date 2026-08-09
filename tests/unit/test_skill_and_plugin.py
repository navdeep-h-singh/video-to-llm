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
SKILL = REPO / "skill" / "SKILL.md"
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
