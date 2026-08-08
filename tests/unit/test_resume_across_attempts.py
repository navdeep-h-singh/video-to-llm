"""Restarting a worker must not re-describe what is already described.

The defect this pins cost three hours of real work. ``completed_batch_indexes``
was scoped to one ``stage_run_id``, but ``_begin_stage`` mints a fresh row for
every attempt — so a restarted worker saw none of the previous attempt's 562
completed batches and began again at zero. The docstring said these are "never
re-sent"; the query said otherwise, and the query is what runs.

On a local model that is silent hours. On a paid provider it is being billed
twice for the same frames, which is the invariant this code exists to hold.

A failure here is a regression.
"""

from __future__ import annotations

import json

import pytest

from app.core.db import new_id, open_database, utc_now
from app.pipeline.visual import (
    completed_batch_descriptions,
    completed_batch_indexes,
)
from app.providers.base import FrameDescription


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "out")
    # Real parent rows: stage_runs.job_video_id is a foreign key, and a fixture
    # that dodged it would be testing a schema this application never has.
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " visual_provider, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("j1", "Course", "analyzing", "/out", 2000, "ollama_local", utc_now(), utc_now()),
    )
    for sequence, video in enumerate(("v1", "v2")):
        connection.execute(
            "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
            " status, output_dir, is_active_version, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                video,
                "j1",
                f"/src/{video}.mp4",
                f"{video}.mp4",
                sequence,
                "analyzing",
                f"j1/{video}",
                1,
                utc_now(),
                utc_now(),
            ),
        )
    connection.commit()
    yield connection
    connection.close()


def _stage_run(connection, *, video="v1", stage="visual", attempt=1):
    run_id = new_id()
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, attempt, status,"
        " started_at, created_at, updated_at) VALUES (?,?,?,?,'running',?,?,?)",
        (run_id, video, stage, attempt, utc_now(), utc_now(), utc_now()),
    )
    connection.commit()
    return run_id


def _completed_batch(connection, run_id, index, *, artifact_path=None):
    # The frame columns are NOT NULL in the real schema; a batch here stands for
    # one picture, which is how the local provider is configured.
    connection.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, artifact_path, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,'completed',?,?,?)",
        (new_id(), run_id, index, index, index, 1, artifact_path, utc_now(), utc_now()),
    )
    connection.commit()


def _write_batch_file(root, relative, indexes):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "descriptions": [
                    FrameDescription(index=i, visual_description=f"picture {i}").as_dict()
                    for i in indexes
                ],
            }
        ),
        encoding="utf-8",
    )
    return relative


# ── The bug that cost three hours ─────────────────────────────────────────


def test_a_new_attempt_sees_the_previous_attempts_completed_batches(db):
    first = _stage_run(db, attempt=1)
    for index in range(5):
        _completed_batch(db, first, index)

    second = _stage_run(db, attempt=2)

    assert completed_batch_indexes(db, second) == {0, 1, 2, 3, 4}, (
        "a restarted worker did not see the previous attempt's completed work "
        "and would describe all of it again"
    )


def test_another_videos_batches_are_never_reused(db):
    """Attempts of the same stage on the same video are the same work. A
    different video is not, and reusing across them would attach one video's
    descriptions to another's frames."""
    mine = _stage_run(db, video="v1", attempt=1)
    theirs = _stage_run(db, video="v2", attempt=1)
    _completed_batch(db, theirs, 7)

    assert completed_batch_indexes(db, mine) == set()


def test_a_different_stage_is_not_reused(db):
    mine = _stage_run(db, video="v1", stage="visual", attempt=1)
    other = _stage_run(db, video="v1", stage="frames", attempt=1)
    _completed_batch(db, other, 3)

    assert completed_batch_indexes(db, mine) == set()


# ── Carrying the descriptions, not just the count ─────────────────────────


def test_completed_batches_bring_their_descriptions_back(db, tmp_path):
    """Skipping a batch used to add to a counter and nothing else, so a resumed
    run wrote a results file describing only the frames it happened to redo."""
    root = tmp_path / "out"
    first = _stage_run(db, attempt=1)
    relative = _write_batch_file(root, "v1/batches/000000_batch.json", [0, 1])
    _completed_batch(db, first, 0, artifact_path=str(relative))

    second = _stage_run(db, attempt=2)
    recovered = completed_batch_descriptions(db, second, root)

    assert set(recovered) == {0}
    assert [d.index for d in recovered[0]] == [0, 1]
    assert recovered[0][0].visual_description == "picture 0"


def test_a_batch_whose_artifact_is_gone_is_described_again(db, tmp_path):
    """Redoing work is wasteful. Omitting it from the evidence while reporting
    it as done is the failure this product exists to avoid, so a missing
    artifact must fall back to re-describing rather than to silence."""
    root = tmp_path / "out"
    first = _stage_run(db, attempt=1)
    _completed_batch(db, first, 0, artifact_path="v1/batches/gone.json")

    second = _stage_run(db, attempt=2)

    assert completed_batch_descriptions(db, second, root) == {}


def test_an_unreadable_artifact_is_described_again(db, tmp_path):
    root = tmp_path / "out"
    broken = root / "v1" / "batches" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")

    first = _stage_run(db, attempt=1)
    _completed_batch(db, first, 0, artifact_path="v1/batches/broken.json")
    second = _stage_run(db, attempt=2)

    assert completed_batch_descriptions(db, second, root) == {}


def test_an_unrecoverable_batch_becomes_a_gap_not_a_second_charge(db, tmp_path):
    """The two wrong answers, and why this is the third.

    Re-sending would violate the invariant that a completed batch is never sent
    twice — on a paid provider, being billed again for work already done.
    Skipping silently would drop those frames from the document with nothing to
    say so. A visible gap is recoverable and honest.
    """
    from app.pipeline.visual import run_visual_analysis
    from app.providers.base import AnalysisRequest, FrameRequest

    class NeverCalled:
        def describe(self, request):  # pragma: no cover - must not run
            raise AssertionError("a completed batch was sent to the provider again")

    first = _stage_run(db, attempt=1)
    _completed_batch(db, first, 0, artifact_path="v1/batches/vanished.json")
    second = _stage_run(db, attempt=2)

    frame = FrameRequest(index=0, timestamp_seconds=0.0, image_path=tmp_path / "000000.jpg")
    request = AnalysisRequest(frames=(frame,), model_id="m", prompt="describe")

    result = run_visual_analysis(
        db,
        stage_run_id=second,
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=NeverCalled(),
        requests=[request],
    )

    assert result.batches_sent == 0, "the batch was described again"
    assert result.has_gaps, "the missing descriptions vanished without a trace"
    assert result.status == "completed_with_gaps"


def test_a_resume_shows_what_it_skipped_before_the_first_slow_call(db, tmp_path):
    """The skip burst must reach the screen, not be eaten by the throttle.

    Recognising hundreds of finished batches takes well under a second, so every
    one of those updates falls inside the throttle window. The run then blocks on
    the first real piece of work for half a minute — and without a flush the
    screen still reads zero throughout, which looks exactly like a resume that
    achieved nothing. It happened on the real job: 562 batches skipped, "0 of
    1,488" on screen.
    """
    from app.pipeline.progress import StageProgress
    from app.pipeline.visual import run_visual_analysis
    from app.providers.base import AnalysisRequest, AnalysisResult, FrameRequest

    root = tmp_path / "out"
    first = _stage_run(db, attempt=1)
    requests = []
    for index in range(4):
        relative = _write_batch_file(root, f"v1/batches/{index:06d}_batch.json", [index])
        if index < 3:  # the first three are already done
            _completed_batch(db, first, index, artifact_path=str(relative))
        requests.append(
            AnalysisRequest(
                frames=(
                    FrameRequest(
                        index=index,
                        timestamp_seconds=float(index),
                        image_path=tmp_path / f"{index}.jpg",
                    ),
                ),
                model_id="m",
                prompt="describe",
            )
        )

    second = _stage_run(db, attempt=2)
    progress = StageProgress(db, second, clock=lambda: 1000.0)  # throttle always closed
    progress.set_total(4)

    seen_before_the_call = []

    class RecordingProvider:
        """Reads the published figure at the moment a real run would be waiting."""

        def describe(self, request):
            row = db.execute("SELECT items_done FROM stage_runs WHERE id = ?", (second,)).fetchone()
            seen_before_the_call.append(row["items_done"])
            return AnalysisResult(
                descriptions=[FrameDescription(index=f.index) for f in request.frames],
                provider="fake",
                model_id="m",
            )

    run_visual_analysis(
        db,
        stage_run_id=second,
        job_id="j1",
        job_video_id="v1",
        output_root=root,
        output_dir=root / "out",
        provider=RecordingProvider(),
        requests=requests,
        on_progress=progress.advance_to,
        on_flush=progress.flush,
    )

    assert seen_before_the_call, "the provider was never reached"
    assert seen_before_the_call[0] == 3, (
        "the screen still showed "
        f"{seen_before_the_call[0]} of 4 while the run blocked on the model, "
        "after skipping three finished batches"
    )
