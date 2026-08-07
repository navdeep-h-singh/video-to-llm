"""Preflight and source fingerprinting.

Preflight exists so failures land before the expensive work, not forty minutes
into it. These tests push the failure cases hard, because the success case is
the one that gets exercised by every other test anyway.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, VisualAnalysisSettings
from app.core.db import open_database, utc_now
from app.pipeline.preflight import (
    MAX_VIDEOS_PER_JOB,
    fingerprint,
    format_preflight,
    preflight,
)
from tests.fixtures.synthetic import ffmpeg_available, make_corrupt_file, make_video

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


# ── Fingerprinting ────────────────────────────────────────────────────────


def test_identical_content_fingerprints_identically(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"same content")
    second.write_bytes(b"same content")
    assert fingerprint(first) == fingerprint(second)


def test_different_content_fingerprints_differently(tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"one thing")
    second.write_bytes(b"another thing")
    assert fingerprint(first) != fingerprint(second)


def test_renaming_does_not_change_the_fingerprint(tmp_path):
    original = tmp_path / "before.bin"
    original.write_bytes(b"content that matters")
    digest = fingerprint(original)

    renamed = tmp_path / "after.bin"
    original.rename(renamed)
    assert fingerprint(renamed) == digest


def test_a_large_file_fingerprints_correctly(tmp_path):
    import hashlib

    payload = b"y" * (3 * 1024 * 1024 + 11)
    target = tmp_path / "big.bin"
    target.write_bytes(payload)
    assert fingerprint(target) == hashlib.sha256(payload).hexdigest()


# ── Basic refusals ────────────────────────────────────────────────────────


def test_an_empty_selection_is_refused(settings):
    report = preflight([], settings)
    assert not report.ok
    assert "No videos" in report.problems[0]


def test_more_than_twenty_videos_is_refused(settings, tmp_path):
    paths = [tmp_path / f"v{i}.mp4" for i in range(MAX_VIDEOS_PER_JOB + 1)]
    report = preflight(paths, settings)
    assert not report.ok
    assert "more than the 20" in report.problems[0]


def test_a_missing_file_is_refused(settings, tmp_path):
    report = preflight([tmp_path / "nope.mp4"], settings)
    assert not report.ok
    assert "could not be found" in report.problems[0]


def test_a_folder_is_refused(settings, tmp_path):
    folder = tmp_path / "a_folder.mp4"
    folder.mkdir()
    report = preflight([folder], settings)
    assert not report.ok
    assert "folder" in report.problems[0]


@pytest.mark.parametrize("suffix", [".avi", ".mkv", ".txt", ".wmv", ""])
def test_unsupported_file_types_are_refused(settings, tmp_path, suffix):
    target = tmp_path / f"clip{suffix}"
    target.write_bytes(b"data")
    report = preflight([target], settings)
    assert not report.ok
    assert "not supported" in report.problems[0]


@pytest.mark.parametrize("suffix", [".mp4", ".mov", ".webm", ".MP4", ".MOV"])
@needs_ffmpeg
def test_supported_file_types_are_accepted(settings, tmp_path, suffix):
    source = make_video(tmp_path / f"clip{suffix}", duration_seconds=2.0)
    report = preflight([source.path], settings)
    assert report.ok, report.problems


@needs_ffmpeg
def test_a_corrupt_file_is_refused_cleanly(settings, tmp_path):
    # It must fail here rather than deep inside frame extraction.
    corrupt = make_corrupt_file(tmp_path / "broken.mp4")
    report = preflight([corrupt], settings)
    assert not report.ok
    assert len(report.problems) == 1


def test_no_output_root_is_refused(tmp_path):
    report = preflight([tmp_path / "a.mp4"], Settings())
    assert not report.ok
    assert "output folder" in report.problems[0]


# ── Duplicates ────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_same_file_twice_in_one_job_is_refused(settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    copy = tmp_path / "clip_copy.mp4"
    copy.write_bytes(source.path.read_bytes())

    report = preflight([source.path, copy], settings)
    assert not report.ok
    assert "same file" in report.problems[0]


@needs_ffmpeg
def test_a_previously_processed_video_warns_but_is_allowed(settings, db, tmp_path):
    # Reprocessing with different settings is legitimate — it makes a new
    # version and keeps the old one — so this must not be a refusal.
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    digest = fingerprint(source.path)

    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Earlier", "completed", str(settings.output_root), utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, source_sha256,"
        " sequence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v1", "j1", str(source.path), "clip.mp4", digest, 0, utc_now(), utc_now()),
    )

    report = preflight([source.path], settings, connection=db)
    assert report.ok
    assert any("processed before" in w for w in report.warnings)
    assert report.videos[0].duplicate_of == "clip.mp4"


@needs_ffmpeg
def test_fingerprinting_can_be_skipped_for_speed(settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)
    report = preflight([source.path], settings, compute_fingerprints=False)
    assert report.ok
    assert report.videos[0].sha256 is None


# ── Estimates ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_frame_and_duration_estimates_are_reported(settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=10.0)
    report = preflight([source.path], settings, interval_ms=2000)

    assert report.total_frames == 5
    assert report.total_duration_seconds == pytest.approx(10.0, abs=0.5)
    assert report.estimated_bytes > 0


@needs_ffmpeg
@pytest.mark.parametrize(("interval_ms", "expected"), [(500, 20), (1000, 10), (2000, 5), (5000, 2)])
def test_estimates_track_the_chosen_interval(settings, tmp_path, interval_ms, expected):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=10.0)
    report = preflight([source.path], settings, interval_ms=interval_ms)
    assert report.total_frames == expected


@needs_ffmpeg
def test_batch_counts_follow_the_batch_size(settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=10.0)
    report = preflight([source.path], settings, interval_ms=1000)

    assert report.batch_count(20) == 1  # 10 frames, one cloud batch
    assert report.batch_count(1) == 10  # the Local Ollama default


@needs_ffmpeg
def test_two_videos_have_their_totals_summed(settings, tmp_path):
    first = make_video(tmp_path / "one.mp4", duration_seconds=4.0)
    second = make_video(tmp_path / "two.mp4", duration_seconds=6.0)

    report = preflight([first.path, second.path], settings, interval_ms=2000)
    assert len(report.accepted) == 2
    assert report.total_frames == 5


# ── Audio and provider configuration ──────────────────────────────────────


@needs_ffmpeg
def test_a_video_with_no_sound_is_accepted_with_a_warning(settings, tmp_path):
    source = make_video(tmp_path / "silent.mp4", duration_seconds=3.0, with_audio=False)
    report = preflight([source.path], settings)

    assert report.ok
    assert any("no sound" in w for w in report.warnings)


@needs_ffmpeg
def test_descriptions_on_with_no_provider_is_refused(tmp_path):
    settings = Settings(
        visual_analysis=VisualAnalysisSettings(enabled=True, provider="none")
    ).with_output_root(tmp_path / "out")
    source = make_video(tmp_path / "clip.mp4", duration_seconds=2.0)

    report = preflight([source.path], settings)
    assert not report.ok
    assert any("no provider" in p for p in report.problems)


# ── Reporting ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_report_is_printable(settings, tmp_path):
    source = make_video(tmp_path / "clip.mp4", duration_seconds=4.0)
    text = format_preflight(preflight([source.path], settings))

    assert "Pictures to make" in text
    assert "Ready to start." in text


def test_a_failing_report_says_it_cannot_start(settings):
    assert "Cannot start yet." in format_preflight(preflight([], settings))
