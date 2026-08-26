"""The command line as a first-class way in.

For three sessions the README and the CLI's own docstring both said "the whole
pipeline is callable from the command line without ever opening the interface",
and it was not true: `build_parser` offered seven subcommands and not one of
them created a job. Job creation existed solely as a web form. Nothing caught
it because nothing asserted that a documented command exists.

The tests here cover four failures, each of which actually happened while this
was being built:

* a command named in the documentation that the parser does not have;
* a `--format` choice the exporter cannot render;
* a citation resolving against `analysis_input/`, because listing a job folder
  returns the handoff package too and it sorts above a hex identifier;
* a one-shot run taking the oldest waiting job instead of the one just created.

A failure here is a regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.main import DESCRIBE_CHOICES, EXPORT_FORMATS, build_parser
from app.core.config import PROVIDER_LABELS, Settings
from app.core.db import new_id, open_database, utc_now
from app.pipeline.frames import FRAMES_DIRNAME, MANIFEST_FILENAME
from app.pipeline.transcribe import TRANSCRIPT_FILENAME
from app.pipeline.visual import VISUAL_RESULTS_FILENAME
from app.services.citation import (
    CitationError,
    find_job,
    parse_timestamp,
    resolve_citation,
    video_dirs,
)
from app.services.export import EXPORTERS, ExportError, export_video_dir, read_timeline
from tests.fixtures.synthetic import ffmpeg_available, make_video

# ── The parser offers what the documentation promises ─────────────────────


#: Every command this project's documentation tells a reader to run. Adding a
#: command to the README without adding it here — or removing one from the
#: parser without removing it from the README — fails.
DOCUMENTED_COMMANDS = {
    "process",
    "show",
    "export",
    "mcp",
    "config",
    "start",
    "start-ui",
    "run-worker",
    "doctor",
    "status",
    "run-next",
    "smoke-test",
    "import",
}


def _subcommands(parser) -> set[str]:
    for action in parser._actions:
        if getattr(action, "choices", None) and hasattr(action, "add_parser"):
            return set(action.choices)
    raise AssertionError("The parser has no subcommands at all.")


def test_every_documented_command_exists():
    """The exact defect this file was created for.

    Equality, not containment: a parser that gained a command nobody documented
    is as much a drift as one that lost a documented one, and `issubset` would
    hide the first.
    """
    assert _subcommands(build_parser()) == DOCUMENTED_COMMANDS


def test_process_parses_a_full_invocation():
    args = build_parser().parse_args(
        ["process", "a.mp4", "b.mp4", "--interval", "2", "--describe", "local", "--format", "jsonl"]
    )
    assert args.videos == ["a.mp4", "b.mp4"]
    assert args.interval == 2.0
    assert args.describe == "local"
    assert args.format == ["jsonl"]


def test_process_needs_at_least_one_video():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["process"])


def test_format_choices_match_the_exporters_exactly():
    """A choice the parser offers and the exporter cannot render is a lie.

    `EXPORT_FORMATS` is duplicated in the CLI so that building the parser costs
    no service imports. This is the guard that makes the duplication safe — the
    same arrangement the settings template's provider labels carry.
    """
    assert set(EXPORT_FORMATS) == set(EXPORTERS)


def test_describe_choices_name_real_providers():
    """`--describe local` has to reach the provider the settings file calls
    `ollama_local`, and every service name has to be one the app knows."""
    assert DESCRIBE_CHOICES["local"] == "ollama_local"
    for value in DESCRIBE_CHOICES.values():
        if value == "none":
            continue
        assert value in PROVIDER_LABELS, f"{value} is not a provider this app offers"


# ── Per-run overrides ─────────────────────────────────────────────────────
#
# Everything the settings screen offers per job was, until now, unreachable from
# a terminal. The spending cap is the one that mattered: a terminal-only user
# ran under whatever number happened to be in a file they may never have opened.


def _parsed(*argv: str):
    return build_parser().parse_args(["process", "a.mp4", *argv])


def test_a_run_without_flags_changes_nothing():
    """The subject of every test below: with no flags, the saved settings are
    what runs. An override that applied by default would be worse than none."""
    from app.cli.main import _with_run_overrides

    base = Settings()
    assert _with_run_overrides(base, _parsed()) == base


def test_the_spending_cap_can_be_set_for_one_run():
    from app.cli.main import _with_run_overrides

    overridden = _with_run_overrides(Settings(), _parsed("--budget", "5"))
    assert overridden.visual_analysis.budget.hard_limit_usd == 5.0


@pytest.mark.skipif(not ffmpeg_available(), reason="needs ffmpeg to make a real video")
def test_the_spending_cap_reaches_the_job_row(tmp_path):
    """Overriding the settings object is not the point.

    The number the worker checks against before every request is the one stored
    on the job. This goes through `create_job` with a real video so the whole
    path is covered — an override that stopped at the settings object would look
    correct in every other test here and protect nobody.
    """
    from app.cli.main import _with_run_overrides
    from app.services.jobs import create_job

    source = make_video(tmp_path / "clip.mp4", duration_seconds=2)

    settings = _with_run_overrides(
        Settings().with_output_root(tmp_path / "out"), _parsed("--budget", "7.5")
    )
    connection = open_database(settings.output_root)
    try:
        created = create_job(
            connection,
            settings,
            name="clip",
            paths=[source.path],
            provider="anthropic",
            model_id="claude-x",
        )
        assert created.ok, created.problems

        stored = connection.execute(
            "SELECT budget_limit_usd FROM jobs WHERE id = ?", (created.job_id,)
        ).fetchone()["budget_limit_usd"]
        assert stored == 7.5
    finally:
        connection.close()


def test_the_speech_model_and_language_can_be_set_for_one_run():
    from app.cli.main import _with_run_overrides

    overridden = _with_run_overrides(
        Settings(), _parsed("--language", "es", "--transcribe-model", "small")
    )
    assert overridden.transcription.language == "es"
    assert overridden.transcription.model == "small"


def test_the_runaway_guards_can_be_set_for_one_run():
    from app.cli.main import _with_run_overrides

    overridden = _with_run_overrides(
        Settings(), _parsed("--max-frames", "200", "--max-minutes", "30")
    )
    assert overridden.visual_analysis.local_guard.max_frames_per_run == 200
    assert overridden.visual_analysis.local_guard.max_runtime_minutes == 30


def test_setting_one_guard_leaves_the_other_alone():
    """`replace` on a nested dataclass is easy to write so that it resets the
    sibling field to its default. Zero means "no limit", so that failure would
    silently remove a limit the user had set."""
    from dataclasses import replace

    from app.cli.main import _with_run_overrides

    plain = Settings()
    base = replace(
        plain,
        visual_analysis=replace(
            plain.visual_analysis,
            local_guard=replace(plain.visual_analysis.local_guard, max_runtime_minutes=45),
        ),
    )

    overridden = _with_run_overrides(base, _parsed("--max-frames", "10"))
    assert overridden.visual_analysis.local_guard.max_frames_per_run == 10
    assert overridden.visual_analysis.local_guard.max_runtime_minutes == 45


def test_overrides_never_write_the_settings_file(tmp_path, monkeypatch):
    """A flag changes this run and nothing else. Persisting it would surprise
    the next run and would fight with a running interface holding the same file."""
    from app.cli.main import _with_run_overrides
    from app.core import config as config_module

    target = tmp_path / "settings.toml"
    monkeypatch.setattr(config_module, "settings_file", lambda: target)

    _with_run_overrides(Settings(), _parsed("--budget", "1", "--language", "fr"))
    assert not target.exists()


# ── Timestamps ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [("00:00:00", 0.0), ("754", 754.0), ("12:34", 754.0), ("01:02:03", 3723.0), ("9.5", 9.5)],
)
def test_timestamps_parse(text, expected):
    assert parse_timestamp(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-5", "1:90", "1:2:75"])
def test_impossible_timestamps_are_refused(text):
    """Refused rather than coerced. A timestamp quietly resolved to the wrong
    second produces a citation that looks checked and is not."""
    with pytest.raises(CitationError):
        parse_timestamp(text)


# ── A citation resolves against a video, not the handoff folder ───────────


@pytest.fixture
def job_on_disk(tmp_path):
    """A finished single-video job, with the folders a real run leaves behind."""
    settings = Settings().with_output_root(tmp_path / "out")
    connection = open_database(settings.output_root)

    job_id, video_id = new_id(), new_id()
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, output_dirname, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            job_id,
            "lecture",
            "completed",
            str(settings.output_root),
            "lecture",
            utc_now(),
            utc_now(),
        ),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence, version,"
        " is_active_version, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            job_id,
            "/src/lecture.mp4",
            "lecture.mp4",
            0,
            1,
            1,
            "completed",
            utc_now(),
            utc_now(),
        ),
    )

    video_dir = settings.output_root / "lecture" / f"{video_id}_v1"
    (video_dir / FRAMES_DIRNAME).mkdir(parents=True)
    for index, second in enumerate((0, 2, 4)):
        (video_dir / FRAMES_DIRNAME / f"{index:06d}_t{second:06d}.jpg").write_bytes(b"jpg")

    (video_dir / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "source_filename": "lecture.mp4",
                "duration_seconds": 6.0,
                "frames": [
                    {"timestamp_seconds": s, "clean_filename": f"{i:06d}_t{s:06d}.jpg"}
                    for i, s in enumerate((0, 2, 4))
                ],
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
                    {
                        "start_seconds": 0.0,
                        "end_seconds": 2.0,
                        "text": "First",
                        "is_silence": False,
                    },
                    {
                        "start_seconds": 4.0,
                        "end_seconds": 6.0,
                        "text": "Second",
                        "is_silence": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # The handoff package. This is the folder that broke every citation: it
    # holds no transcript and, starting with "a", sorts above a hex identifier.
    (settings.output_root / "lecture" / "analysis_input").mkdir()

    yield settings, connection, job_id, video_dir
    connection.close()


def test_the_handoff_folder_is_never_mistaken_for_a_video(job_on_disk):
    """The bug this fixture's `analysis_input` exists to reproduce.

    Listing the job folder returned it, `reverse=True` put it first, and every
    citation resolved against a directory with no transcript in it. The folders
    come from `job_videos` now, so the filesystem's opinion does not matter.
    """
    settings, connection, _, video_dir = job_on_disk
    job = find_job(connection, "lecture")

    assert video_dirs(connection, settings.output_root, job) == [video_dir]


def test_a_citation_names_the_picture_from_that_moment(job_on_disk):
    settings, _, _, video_dir = job_on_disk
    citation = resolve_citation(settings, "lecture", "00:00:04")

    assert citation.frame_path == video_dir / FRAMES_DIRNAME / "000002_t000004.jpg"
    assert citation.frame_path.exists()
    assert citation.frame_seconds == 4.0
    assert [entry.text for entry in citation.entries] == ["First", "Second"]


def test_a_deleted_picture_is_reported_missing_rather_than_named(job_on_disk):
    """Space can be reclaimed by kind, so the manifest outlives the pictures.

    Naming a path that is not there would send the user to open a file that
    does not exist — worse than saying it is gone.
    """
    settings, _, _, video_dir = job_on_disk
    (video_dir / FRAMES_DIRNAME / "000002_t000004.jpg").unlink()

    citation = resolve_citation(settings, "lecture", "00:00:04")
    assert citation.frame_path is None
    assert citation.frame_seconds == 4.0


def test_a_timestamp_past_the_end_is_refused(job_on_disk):
    settings, _, _, _ = job_on_disk
    with pytest.raises(CitationError) as error:
        resolve_citation(settings, "lecture", "00:01:00")
    assert "past the end" in str(error.value)


def test_an_unfinished_video_says_so_rather_than_raising_an_export_error(job_on_disk):
    """A caller asking for a citation should not have to catch another module's
    exception to learn the video is not ready."""
    settings, _, _, video_dir = job_on_disk
    (video_dir / TRANSCRIPT_FILENAME).unlink()

    with pytest.raises(CitationError):
        resolve_citation(settings, "lecture", "00:00:04")


# ── Exports read the artifacts, never the rendered document ───────────────


def test_exports_do_not_need_the_assembled_document(job_on_disk):
    """The structural guarantee, asserted structurally.

    `assembled.txt` is deliberately absent from the fixture. An exporter that
    quietly started parsing it — the drift this module's docstring warns about —
    would fail here rather than in six months against a changed header.
    """
    _, _, _, video_dir = job_on_disk
    assert not (video_dir / "assembled.txt").exists()

    written = export_video_dir(video_dir, "jsonl")
    records = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    assert [r["text"] for r in records] == ["First", "Second"]
    assert [r["timestamp"] for r in records] == ["00:00:00", "00:00:04"]


def test_subtitles_carry_speech_and_nothing_else(job_on_disk):
    """A description has no spoken duration. Writing one into a caption track
    would put words on screen that nobody said."""
    _, _, _, video_dir = job_on_disk
    (video_dir / VISUAL_RESULTS_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "descriptions": [
                    {
                        "index": 0,
                        "timestamp_seconds": 2.0,
                        "visual_description": "A chart appears",
                        "confidence": "High",
                        "clean_filename": "000001_t000002.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # The description must reach the timeline, so this test is about the
    # subtitle renderer's choice and not about an empty input.
    assert any(e.kind == "description" for e in read_timeline(video_dir))

    srt = export_video_dir(video_dir, "srt").read_text(encoding="utf-8")
    assert "A chart appears" not in srt
    assert "First" in srt and "Second" in srt


def test_speech_precedes_the_picture_taken_at_the_same_second(job_on_disk):
    """Two renderings of one timeline that disagreed about order would be a bug
    in whichever the user was not looking at. `assemble.py` puts speech first."""
    _, _, _, video_dir = job_on_disk
    (video_dir / VISUAL_RESULTS_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "descriptions": [
                    {"index": 0, "timestamp_seconds": 0.0, "visual_description": "Title slide"}
                ],
            }
        ),
        encoding="utf-8",
    )

    kinds = [entry.kind for entry in read_timeline(video_dir) if entry.seconds == 0.0]
    assert kinds == ["speech", "description"]


def test_an_unprocessed_folder_refuses_to_export(tmp_path):
    with pytest.raises(ExportError):
        export_video_dir(tmp_path, "json")


# ── The path people paste into issues ─────────────────────────────────────
#
# `show` prints the frame path as the last line, which is the line that ends up
# in bug reports, forum posts and screen recordings. An absolute path carries
# the operator's account name into all three for no benefit.


def test_a_frame_path_inside_home_is_shortened(monkeypatch, tmp_path):
    from app.services.citation import shorten_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert shorten_home(tmp_path / "VideoToLLM" / "lecture.jpg") == "~/VideoToLLM/lecture.jpg"


def test_a_path_outside_home_is_left_alone(monkeypatch, tmp_path):
    from app.services.citation import shorten_home

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert shorten_home("/mnt/archive/lecture.jpg") == "/mnt/archive/lecture.jpg"


def test_a_sibling_of_home_is_not_mistaken_for_being_inside_it(monkeypatch, tmp_path):
    """A home of `.../nav` must not swallow `.../navdeep`. The separator is the
    guard, and a prefix match without it would rewrite somebody else's path.

    Built from `tmp_path` rather than a literal `/Users/<name>` — the
    pre-publish audit forbids an absolute home path in a tracked file, and it
    caught this test when it was written the obvious way."""
    from app.services.citation import shorten_home

    home = tmp_path / "nav"
    sibling = tmp_path / "navdeep" / "clip.jpg"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert shorten_home(sibling) == str(sibling)


def test_an_unresolvable_home_does_not_break_printing_a_result(monkeypatch):
    """A stripped environment is not a reason to raise while rendering
    something the user asked for."""
    from app.services.citation import shorten_home

    def no_home(cls):
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    assert shorten_home("/anywhere/clip.jpg") == "/anywhere/clip.jpg"


def test_the_paths_a_script_captures_are_not_shortened():
    """`process` and `export` print paths for machines, `show` for people.

    Issue #8 proposes `--quiet` so a script can capture the output path, and
    `cd $(video-to-llm export ...)` does not expand a `~` that arrives inside a
    variable. So the shortening stays on the human renderer only.
    """
    from pathlib import Path as _Path

    source = (_Path(__file__).resolve().parents[2] / "app" / "cli" / "main.py").read_text()
    assert "shorten_home(export_video_dir" not in source
    assert "shorten_home(str(document" not in source
