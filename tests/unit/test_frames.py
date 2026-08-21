"""Frame planning and extraction.

The frame plan is pure and deterministic, so most of this tests real arithmetic
rather than mocks. Extraction itself runs against generated media.
"""

from __future__ import annotations

import json

import pytest

from app.pipeline.frames import (
    API_FRAMES_DIRNAME,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    FRAMES_DIRNAME,
    FrameExtractionError,
    expected_frame_count,
    extract_frames,
    frame_filename,
    frame_rate_mode_args,
    parse_ffmpeg_major,
    plan_frames,
    timestamp_token,
)
from app.pipeline.probe import probe
from tests.fixtures.synthetic import ffmpeg_available, make_video

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


# ── Filenames ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "000000"), (12, "000012"), (92, "000132"), (5520, "013200"), (33600, "092000")],
)
def test_timestamp_token_formats_as_hhmmss(seconds, expected):
    assert timestamp_token(seconds) == expected


def test_frame_filename_matches_the_documented_shape():
    # The spec gives 000047_t092000.jpg as the example.
    assert frame_filename(47, 33600) == "000047_t092000.jpg"


def test_filenames_sort_in_index_order():
    names = [frame_filename(i, i * 2) for i in range(0, 1200, 97)]
    assert names == sorted(names)


# ── Planning ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("interval_ms", "expected"),
    [(500, 20), (1000, 10), (2000, 5), (3000, 4), (5000, 2), (10000, 1)],
)
def test_every_supported_interval_plans_the_right_count(interval_ms, expected):
    assert len(plan_frames(10.0, interval_ms)) == expected


def test_timestamps_land_on_the_interval_grid():
    plan = plan_frames(10.0, 2000)
    assert [r.timestamp_seconds for r in plan] == [0.0, 2.0, 4.0, 6.0, 8.0]


def test_indexes_are_contiguous_from_zero():
    plan = plan_frames(30.0, 1000)
    assert [r.index for r in plan] == list(range(30))


def test_long_videos_do_not_accumulate_floating_point_drift():
    # Timestamps are multiplied from the index rather than accumulated, so the
    # last frame of a long video is still exactly on the grid.
    plan = plan_frames(7200.0, 500)  # two hours at half-second sampling
    assert plan[-1].timestamp_seconds == pytest.approx(7199.5, abs=1e-6)
    assert len(plan) == 14400


def test_batches_group_frames_in_order():
    plan = plan_frames(100.0, 1000, batch_size=20)
    assert plan[0].batch_index == 0
    assert plan[19].batch_index == 0
    assert plan[20].batch_index == 1
    assert plan[99].batch_index == 4


def test_a_batch_size_of_one_puts_every_frame_in_its_own_batch():
    # This is the Local Ollama default.
    plan = plan_frames(5.0, 1000, batch_size=1)
    assert [r.batch_index for r in plan] == [0, 1, 2, 3, 4]


def test_a_very_short_video_still_yields_one_frame():
    assert len(plan_frames(0.5, 2000)) == 1


def test_a_zero_length_video_yields_nothing():
    assert plan_frames(0.0, 2000) == []


@pytest.mark.parametrize("interval", [0, -1000])
def test_a_non_positive_interval_is_refused(interval):
    with pytest.raises(ValueError, match="interval must be positive"):
        plan_frames(10.0, interval)


def test_a_non_positive_batch_size_is_refused():
    with pytest.raises(ValueError, match="batch size must be positive"):
        plan_frames(10.0, 2000, batch_size=0)


def test_planning_is_deterministic():
    first = plan_frames(123.4, 1500)
    second = plan_frames(123.4, 1500)
    assert [r.clean_filename for r in first] == [r.clean_filename for r in second]


@pytest.mark.parametrize(
    ("duration", "interval_ms", "expected"),
    [(10.0, 2000, 5), (10.5, 2000, 6), (0.0, 2000, 0), (1.0, 2000, 1)],
)
def test_expected_frame_count_rounds_up(duration, interval_ms, expected):
    assert expected_frame_count(duration, interval_ms) == expected


# ── Extraction ────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_extraction_produces_clean_and_provider_frames(tmp_path):
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
    info = probe(source.path)

    result = extract_frames(info, tmp_path / "out", interval_ms=2000)

    clean = sorted((tmp_path / "out" / FRAMES_DIRNAME).glob("*.jpg"))
    api = sorted((tmp_path / "out" / API_FRAMES_DIRNAME).glob("*.jpg"))
    assert len(clean) == len(result.frames) >= 3
    assert len(api) == len(clean)


@needs_ffmpeg
def test_clean_frames_are_the_documented_size(tmp_path):
    from PIL import Image

    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=2000)

    with Image.open(result.frames_dir / result.frames[0].clean_filename) as image:
        assert image.size == (FRAME_WIDTH, FRAME_HEIGHT)


@needs_ffmpeg
def test_provider_frames_are_smaller_than_clean_frames(tmp_path):
    from PIL import Image

    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=2000)
    record = result.frames[0]

    with Image.open(result.api_frames_dir / record.api_filename) as api_image:
        assert api_image.width < FRAME_WIDTH


@needs_ffmpeg
def test_provider_and_clean_frames_are_different_images(tmp_path):
    # The numbered copy must never be mistaken for, or substituted for, the
    # clean picture that ends up in an export.
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=2000)
    record = result.frames[0]

    clean_bytes = (result.frames_dir / record.clean_filename).read_bytes()
    api_bytes = (result.api_frames_dir / record.api_filename).read_bytes()
    assert clean_bytes != api_bytes


@needs_ffmpeg
def test_manifest_records_everything_needed_to_rebuild_the_mapping(tmp_path):
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=2000)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["frame_interval_ms"] == 2000
    assert manifest["frame_count"] == len(result.frames)
    assert manifest["source_filename"] == "clip.mp4"

    first = manifest["frames"][0]
    assert set(first) == {
        "index",
        "timestamp_seconds",
        "timestamp",
        "clean_filename",
        "api_filename",
        "batch_id",
        "batch_index",
    }


@needs_ffmpeg
def test_manifest_frame_entries_match_the_files_on_disk(tmp_path):
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=6.0)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=1000)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["frames"]:
        assert (result.frames_dir / entry["clean_filename"]).is_file()
        assert (result.api_frames_dir / entry["api_filename"]).is_file()


@needs_ffmpeg
def test_a_video_with_no_audio_still_yields_frames(tmp_path):
    source = make_video(tmp_path / "src" / "silent.mp4", duration_seconds=4.0, with_audio=False)
    result = extract_frames(probe(source.path), tmp_path / "out", interval_ms=2000)
    assert len(result.frames) >= 2


@needs_ffmpeg
def test_provider_copies_can_be_skipped(tmp_path):
    # A local-only job with descriptions off has no use for them.
    source = make_video(tmp_path / "src" / "clip.mp4", duration_seconds=4.0)
    result = extract_frames(
        probe(source.path), tmp_path / "out", interval_ms=2000, make_api_copies=False
    )
    assert list(result.api_frames_dir.glob("*.jpg")) == []
    assert len(result.frames) >= 2


# ── Which FFmpeg is in front of us ────────────────────────────────────────
#
# FFmpeg 9.0 removed `-vsync` outright. Its replacement, `-fps_mode`, does not
# exist before 5.0 — and 4.x is still what Ubuntu 22.04 ships. Neither spelling
# is safe to hardcode, so extraction asks first.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('ffmpeg version 9.0 "Lei" Copyright (c) 2000-2026', 9),
        ("ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers", 8),
        ("ffmpeg version n7.1 Copyright (c) 2000-2024", 7),
        ("ffmpeg version 6.0-6ubuntu1 Copyright (c) 2000-2023", 6),
        ("ffmpeg version 5.0 Copyright (c)", 5),
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright (c) 2000-2021", 4),
    ],
)
def test_the_major_version_is_read_from_the_banner(line, expected):
    assert parse_ffmpeg_major(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not ffmpeg at all",
        # A self-compiled build with no version number to read.
        "ffmpeg version git-2020-08-01-abcdef Copyright (c)",
    ],
)
def test_an_unreadable_banner_reports_nothing_rather_than_guessing(line):
    assert parse_ffmpeg_major(line) is None


@pytest.mark.parametrize("major", [5, 6, 7, 8, 9, 10])
def test_modern_ffmpeg_gets_the_flag_that_still_exists(major):
    assert frame_rate_mode_args(major) == ["-fps_mode", "vfr"]


@pytest.mark.parametrize("major", [2, 3, 4])
def test_old_ffmpeg_gets_the_only_flag_it_knows(major):
    assert frame_rate_mode_args(major) == ["-vsync", "vfr"]


def test_an_unknown_version_takes_the_older_spelling():
    # Wrong this way fails loudly at the start of extraction on a new FFmpeg.
    # Wrong the other way fails on an old one, which is the machine least likely
    # to have another option available.
    assert frame_rate_mode_args(None) == ["-vsync", "vfr"]


def _fake_info(tmp_path):
    from app.pipeline.probe import VideoInfo

    return VideoInfo(
        path=tmp_path / "clip.mp4",
        duration_seconds=6.0,
        width=1920,
        height=1080,
        container="mp4",
        video_codec="h264",
        has_audio=True,
        audio_codec="aac",
        size_bytes=1024,
    )


@pytest.mark.parametrize(
    ("major", "wanted", "removed"),
    [(9, "-fps_mode", "-vsync"), (4, "-vsync", "-fps_mode")],
)
def test_extraction_asks_for_the_flag_this_ffmpeg_accepts(
    tmp_path, monkeypatch, major, wanted, removed
):
    """The regression itself: `-vsync` on FFmpeg 9 extracts zero frames.

    Asserted on the argument list rather than on a successful run, so it holds
    whichever FFmpeg the machine running the tests happens to have — including
    on CI, where macOS and Windows now install 9.x and Ubuntu still does not.
    """
    captured: dict = {}

    def capture(args, *, what):
        captured["args"] = args
        # Extraction reads the directory afterwards; failing there is fine, the
        # argument list is already what this test came for.
        raise FrameExtractionError("not actually running ffmpeg")

    monkeypatch.setattr("app.pipeline.frames._run_ffmpeg", capture)
    monkeypatch.setattr("app.pipeline.frames._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr("app.pipeline.frames.ffmpeg_major", lambda _path: major)

    with pytest.raises(FrameExtractionError):
        extract_frames(_fake_info(tmp_path), tmp_path / "out", interval_ms=2000)

    args = captured["args"]
    assert wanted in args
    assert removed not in args
    assert args[args.index(wanted) + 1] == "vfr"


def test_the_version_is_only_asked_for_once_per_ffmpeg(monkeypatch):
    # A job of fifteen videos does not need fifteen extra subprocesses to learn
    # the same answer.
    from app.pipeline import frames as frames_module

    calls = {"n": 0}

    class Result:
        stdout = "ffmpeg version 8.1.2 Copyright (c)"

    def fake_run(*_args, **_kwargs):
        calls["n"] += 1
        return Result()

    monkeypatch.setattr(frames_module.subprocess, "run", fake_run)
    monkeypatch.setattr(frames_module, "_ffmpeg_major_cache", {})

    assert frames_module.ffmpeg_major("/usr/bin/ffmpeg") == 8
    assert frames_module.ffmpeg_major("/usr/bin/ffmpeg") == 8
    assert calls["n"] == 1
