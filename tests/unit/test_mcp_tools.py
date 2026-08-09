"""The MCP surface, tested without the transport.

The tool functions take and return plain data on purpose: the behaviour worth
guarding is what they decide, not how a JSON-RPC frame reaches them, and keeping
them free of MCP types means these tests run without the optional `mcp` extra
installed.

Two properties here are the reason the surface exists at all:

* **Processing is idempotent.** The pitch against every other video tool in this
  category is that they re-download and re-process for each question. An agent
  calling `process_video` twice on the same files must get the first result back
  rather than a second forty-minute run.
* **An agent cannot spend money.** Descriptions through a paid service are a
  decision the person makes in the interface, where the estimate and the cap are
  on screen. A tool call the user never saw must not be able to make it.

A failure here is a regression.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.db import new_id, open_database, utc_now
from app.mcp.server import (
    AGENT_DESCRIBE,
    TOOLS,
    tool_get_segment,
    tool_get_transcript,
    tool_list_videos,
    tool_process_video,
)
from app.pipeline.frames import FRAMES_DIRNAME, MANIFEST_FILENAME
from app.pipeline.transcribe import TRANSCRIPT_FILENAME


@pytest.fixture
def processed(tmp_path, monkeypatch):
    """A finished job, and settings pointed at the scratch root that holds it."""
    root = tmp_path / "out"
    settings = Settings().with_output_root(root)
    monkeypatch.setattr("app.mcp.server.load_settings", lambda: settings)

    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"not really a video")

    connection = open_database(root)
    job_id, video_id = new_id(), new_id()
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, output_dirname, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (job_id, "lecture", "completed", str(root), "lecture", utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, duration_seconds,"
        " sequence, version, is_active_version, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            job_id,
            str(source),
            "lecture.mp4",
            6.0,
            0,
            1,
            1,
            "completed",
            utc_now(),
            utc_now(),
        ),
    )

    video_dir = root / "lecture" / f"{video_id}_v1"
    (video_dir / FRAMES_DIRNAME).mkdir(parents=True)
    (video_dir / FRAMES_DIRNAME / "000001_t000002.jpg").write_bytes(b"jpg")
    (video_dir / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "source_filename": "lecture.mp4",
                "duration_seconds": 6.0,
                "frames": [{"timestamp_seconds": 2.0, "clean_filename": "000001_t000002.jpg"}],
            }
        ),
        encoding="utf-8",
    )
    (video_dir / TRANSCRIPT_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "source_filename": "lecture.mp4",
                "segments": [
                    {"start_seconds": 0.0, "end_seconds": 2.0, "text": "One", "is_silence": False},
                    {"start_seconds": 4.0, "end_seconds": 6.0, "text": "Two", "is_silence": False},
                ],
            }
        ),
        encoding="utf-8",
    )

    assembled = video_dir / "assembled.txt"
    assembled.write_text("00:00:00  One\n00:00:04  Two\n", encoding="utf-8")
    connection.execute(
        "INSERT INTO artifacts (id, job_id, job_video_id, kind, relative_path, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            new_id(),
            job_id,
            video_id,
            "assembled",
            str(assembled.relative_to(root)),
            utc_now(),
        ),
    )
    connection.close()

    return settings, source, video_dir


def test_the_four_tools_are_all_wired():
    """A tool named in the documentation and absent from the registry is the
    same class of defect as a CLI command that does not parse."""
    assert set(TOOLS) == {"process_video", "list_videos", "get_transcript", "get_segment"}


def test_an_agent_cannot_choose_a_paid_service(processed):
    """The refusal that keeps a tool call from spending the user's money.

    Asserted against the registry rather than a hardcoded list, so adding a
    provider to `AGENT_DESCRIBE` without thinking about this fails here.
    """
    assert set(AGENT_DESCRIBE) == {"none", "local"}

    _, source, _ = processed
    result = tool_process_video([str(source)], describe="anthropic")
    assert "error" in result
    assert "paid service" in result["error"]


def test_processing_the_same_files_again_reuses_the_finished_job(processed):
    """The whole reason to expose this over MCP.

    A second call must not re-run the pipeline. `reused` says so explicitly, and
    the returned document is the one already on disk.
    """
    _, source, video_dir = processed

    result = tool_process_video([str(source)], name="whatever the agent calls it")

    assert result["reused"] is True
    assert result["status"] == "completed"
    assert result["documents"] == [str(video_dir / "assembled.txt")]


def test_a_missing_file_is_reported_before_any_work_starts(processed, tmp_path):
    result = tool_process_video([str(tmp_path / "nope.mp4")])
    assert "error" in result
    assert "No such file" in result["error"]


def test_listing_shows_what_can_be_asked_about(processed):
    videos = tool_list_videos()["videos"]
    assert [v["file"] for v in videos] == ["lecture.mp4"]
    assert videos[0]["job"] == "lecture"
    assert videos[0]["duration_seconds"] == 6.0


def test_a_transcript_can_be_read_in_a_time_range(processed):
    everything = tool_get_transcript("lecture")
    assert [e["text"] for e in everything["entries"]] == ["One", "Two"]

    later = tool_get_transcript("lecture", start_seconds=3.0)
    assert [e["text"] for e in later["entries"]] == ["Two"]


def test_a_segment_names_the_frame_behind_the_claim(processed):
    """An agent that can name the picture can hand it back to be checked."""
    _, _, video_dir = processed
    segment = tool_get_segment("lecture", "00:00:02")

    assert segment["frame_path"] == str(video_dir / FRAMES_DIRNAME / "000001_t000002.jpg")
    assert segment["video"] == "lecture.mp4"


def test_an_unknown_job_returns_an_error_rather_than_raising(processed):
    """A tool that raises gives the host a stack trace; one that returns an
    error gives the agent something it can act on."""
    assert "error" in tool_get_transcript("no-such-job")
    assert "error" in tool_get_segment("no-such-job", "00:00:01")


def test_the_server_advertises_every_tool_it_registers():
    """The transport, not just the functions.

    The SDK renamed its high-level server class between 1.x (`FastMCP`) and 2.x
    (`MCPServer`), and the first attempt here imported the name that no longer
    exists — caught by running it, not by the suite. This asserts the wiring
    survives whichever version is installed.
    """
    import asyncio

    mcp_sdk = pytest.importorskip("mcp", reason="the [mcp] extra is not installed")
    assert mcp_sdk is not None

    from app.mcp.server import build_server

    advertised = {tool.name for tool in asyncio.run(build_server().list_tools())}
    assert advertised == set(TOOLS)
