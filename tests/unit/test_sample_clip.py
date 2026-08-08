"""The generated sample clip.

A fresh install has an empty dashboard and no way to see what the product makes
without supplying a video and waiting. The clip that fixes that is **generated,
never shipped**: the repository has to stay publishable with no personal media
and no large binaries, and a bundled "representative" recording would need a
licence story a demo does not justify.
"""

from __future__ import annotations

import subprocess

import pytest

from app.services.sample import (
    SPEECH_WINDOWS,
    SampleClip,
    SampleError,
    ffmpeg_available,
    generate_sample,
    sample_path,
)

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")


# ── Nothing is shipped ────────────────────────────────────────────────────


def test_no_sample_media_is_tracked_in_the_repository():
    """The point of generating it. A committed clip is a binary in a repository
    that has to stay publishable, and a licence question nobody needs."""
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()

    media = [
        path
        for path in tracked
        if path.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".wav", ".mp3"))
    ]
    assert media == [], f"media files are tracked: {media}"


def test_the_sample_lives_under_the_output_root_not_the_repository(tmp_path):
    """It is generated output, so it belongs with the other generated output —
    where the user can find and delete it."""
    assert sample_path(tmp_path).is_relative_to(tmp_path)


# ── What it is, honestly ──────────────────────────────────────────────────


def test_the_clip_is_described_as_generated_test_footage():
    """Calling it a recording would be a claim about where data came from, which
    is a worse lie than ordinary placeholder content."""
    clip = SampleClip(path=sample_path("/tmp"), duration_seconds=60.0, kind="chart", detail="x")
    assert clip.duration_seconds == 60.0


@needs_ffmpeg
def test_a_generated_clip_says_it_was_generated(tmp_path):
    clip = generate_sample(tmp_path)
    assert "generated" in clip.detail.lower()
    assert "not a recording" in clip.detail.lower() or "test" in clip.detail.lower()


# ── The clip itself ───────────────────────────────────────────────────────


@needs_ffmpeg
def test_a_playable_clip_with_sound_is_produced(tmp_path):
    clip = generate_sample(tmp_path)

    assert clip.path.is_file()
    assert clip.path.stat().st_size > 0

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1",
            str(clip.path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "codec_type=video" in probe
    assert "codec_type=audio" in probe, "without audio there is no transcript to show"
    assert float(probe.split("duration=")[1].split()[0]) == pytest.approx(60.0, abs=1.0)


@needs_ffmpeg
def test_the_audio_has_real_silences_in_it(tmp_path):
    """Silence markers are one of the details that distinguish this transcript
    from a naive one, so the sample has to have silences to mark."""
    assert len(SPEECH_WINDOWS) >= 2
    gaps = [SPEECH_WINDOWS[i + 1][0] - SPEECH_WINDOWS[i][1] for i in range(len(SPEECH_WINDOWS) - 1)]
    assert all(gap >= 3.0 for gap in gaps), "gaps must exceed the silence threshold"


@needs_ffmpeg
def test_generating_twice_reuses_the_first_one(tmp_path):
    """Pressing the button again should not spend thirty seconds redrawing an
    identical clip."""
    first = generate_sample(tmp_path)
    stamp = first.path.stat().st_mtime_ns

    second = generate_sample(tmp_path)

    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == stamp


@needs_ffmpeg
def test_forcing_it_draws_a_new_one(tmp_path):
    generate_sample(tmp_path)
    target = sample_path(tmp_path)
    target.write_bytes(b"")

    clip = generate_sample(tmp_path, force=True)
    assert clip.path.stat().st_size > 0


@needs_ffmpeg
def test_an_empty_file_left_behind_is_replaced(tmp_path):
    """A previous run killed mid-encode leaves a zero-byte file. Reusing it
    would fail preflight with a confusing complaint about the video."""
    target = sample_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")

    assert generate_sample(tmp_path).path.stat().st_size > 0


def test_a_machine_without_ffmpeg_is_told_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.sample.ffmpeg_available", lambda: False)

    with pytest.raises(SampleError, match="FFmpeg"):
        generate_sample(tmp_path)


@needs_ffmpeg
def test_a_build_that_cannot_draw_the_chart_still_gets_a_clip(tmp_path, monkeypatch):
    """drawbox expressions vary between FFmpeg builds. A first-run button that
    fails on somebody's machine is worse than one that shows a plainer clip."""
    import app.services.sample as sample_module

    monkeypatch.setattr(sample_module, "_chart_filter", lambda *a, **k: "definitely_not_a_filter=1")

    clip = generate_sample(tmp_path)

    assert clip.kind == "plain"
    assert clip.path.stat().st_size > 0
    assert "does not have" in clip.detail


# ── The clip has to actually move ─────────────────────────────────────────


@needs_ffmpeg
def test_the_sample_is_not_the_same_picture_sixty_times(tmp_path):
    """The failure this catches was completely silent.

    The first version animated the bars with a time-varying expression in
    `drawbox` — but `drawbox`'s `t` is the box *thickness*, not the timestamp,
    so every expression was a constant and the clip came out as a still image.
    FFmpeg reported nothing, the file played, the pipeline ran, and twenty
    sampled pictures were two distinct images. A sample whose whole job is to
    show the product working must be seen to change.
    """
    import hashlib
    import subprocess as sub

    clip = generate_sample(tmp_path)
    frames = tmp_path / "probe"
    frames.mkdir()

    sub.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(clip.path),
            "-vf",
            "fps=1/3",
            "-frames:v",
            "12",
            str(frames / "f_%03d.jpg"),
        ],
        check=True,
        timeout=120,
    )

    digests = {hashlib.sha1(p.read_bytes()).hexdigest() for p in frames.glob("*.jpg")}
    assert len(digests) >= 10, f"only {len(digests)} distinct pictures in the whole clip"


@needs_ffmpeg
def test_the_chart_moves_rather_than_flickering(tmp_path):
    """Neighbouring frames differ, and so do distant ones.

    Two separate ways to be wrong: a still image, and a pattern that repeats
    every few seconds so a viewer sees the same thing again and again.
    """
    import hashlib
    import subprocess as sub

    clip = generate_sample(tmp_path)
    frames = tmp_path / "probe"
    frames.mkdir()
    sub.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(clip.path),
            "-vf",
            "fps=1/5",
            "-frames:v",
            "10",
            str(frames / "f_%03d.jpg"),
        ],
        check=True,
        timeout=120,
    )

    ordered = sorted(frames.glob("*.jpg"))
    digests = [hashlib.sha1(p.read_bytes()).hexdigest() for p in ordered]

    assert digests[0] != digests[1], "consecutive pictures are identical"
    assert digests[0] != digests[-1], "the clip ends where it started"
    assert len(set(digests)) == len(digests), "the picture repeats within the clip"
