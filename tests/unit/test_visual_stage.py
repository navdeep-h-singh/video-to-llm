"""Stage 3 orchestration.

The load-bearing property: **a completed batch is never re-sent**. On a cloud
provider that means never paying twice for the same work, and it is what makes
resuming a job safe rather than expensive.

Every provider here is a fake. No network call, no key, no cost.
"""

from __future__ import annotations

import json

import pytest

from app.core.db import new_id, open_database, utc_now
from app.pipeline.visual import (
    DEFAULT_PROMPT,
    build_batches,
    completed_batch_indexes,
    run_visual_analysis,
    write_visual_results,
)
from app.providers.base import (
    AnalysisRequest,
    AnalysisResult,
    FrameDescription,
    PermanentProviderError,
    SchemaValidationError,
    TransientProviderError,
)
from app.providers.costs import BudgetTracker
from app.providers.retry import RetryPolicy


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Test", "analyzing", str(tmp_path), utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("v1", "j1", "/a.mp4", "a.mp4", 0, utc_now(), utc_now()),
    )
    connection.execute(
        "INSERT INTO stage_runs (id, job_video_id, stage, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("s1", "v1", "visual", "running", utc_now(), utc_now()),
    )
    yield connection
    connection.close()


@pytest.fixture
def api_frames(tmp_path):
    directory = tmp_path / "frames_api"
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        (directory / f"{index:06d}_t000000.jpg").write_bytes(b"\xff\xd8 fake")
    return directory


def frame_records(count: int = 6) -> list[dict]:
    return [
        {
            "index": i,
            "timestamp_seconds": float(i * 2),
            "api_filename": f"{i:06d}_t000000.jpg",
            "clean_filename": f"{i:06d}_t000000.jpg",
        }
        for i in range(count)
    ]


class FakeProvider:
    """Records what it was asked to describe."""

    def __init__(self, *, cost_usd=None, fail_on=(), error=None):
        self.cost_usd = cost_usd
        self.fail_on = set(fail_on)
        self.error = error or PermanentProviderError("refused")
        self.seen: list[list[int]] = []

    def describe(self, request: AnalysisRequest) -> AnalysisResult:
        indexes = [f.index for f in request.frames]
        self.seen.append(indexes)
        if indexes[0] in self.fail_on:
            raise self.error
        return AnalysisResult(
            descriptions=[
                FrameDescription(index=f.index, visual_description="a chart")
                for f in request.frames
            ],
            provider="fake",
            model_id="fake-model",
            cost_usd=self.cost_usd,
        )


# ── Batching ──────────────────────────────────────────────────────────────


def test_frames_are_grouped_into_batches(api_frames):
    batches = build_batches(frame_records(6), api_frames, batch_size=2, model_id="m")
    assert len(batches) == 3
    assert [f.index for f in batches[0].frames] == [0, 1]


def test_a_batch_size_of_one_is_the_local_default(api_frames):
    batches = build_batches(frame_records(3), api_frames, batch_size=1, model_id="m")
    assert len(batches) == 3
    assert all(len(b.frames) == 1 for b in batches)


def test_a_trailing_partial_batch_is_kept(api_frames):
    batches = build_batches(frame_records(5), api_frames, batch_size=2, model_id="m")
    assert len(batches) == 3
    assert len(batches[-1].frames) == 1


def test_batches_point_at_the_numbered_copies_not_the_clean_frames(api_frames):
    # Only the watermarked copies are ever sent.
    batches = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    assert all("frames_api" in str(f.image_path) for f in batches[0].frames)


def test_a_zero_batch_size_is_refused(api_frames):
    with pytest.raises(ValueError, match="at least 1"):
        build_batches(frame_records(2), api_frames, batch_size=0, model_id="m")


def test_the_default_prompt_asks_for_unknown_over_guessing():
    assert "Unknown" in DEFAULT_PROMPT
    assert "Do not guess" in DEFAULT_PROMPT
    assert "IDX" in DEFAULT_PROMPT


# ── Never re-sending a completed batch ────────────────────────────────────


def test_all_batches_are_sent_on_a_fresh_run(db, api_frames, tmp_path):
    provider = FakeProvider()
    requests = build_batches(frame_records(6), api_frames, batch_size=2, model_id="m")

    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=provider,
        requests=requests,
    )

    assert result.batches_sent == 3
    assert provider.seen == [[0, 1], [2, 3], [4, 5]]


def test_a_completed_batch_is_never_sent_again(db, api_frames, tmp_path):
    """The money-critical property.

    Re-sending a batch that already succeeded means paying for the same work
    twice, which is exactly what a resumed job would do without this check.
    """
    requests = build_batches(frame_records(6), api_frames, batch_size=2, model_id="m")

    first = FakeProvider(cost_usd=0.01)
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=first,
        requests=requests,
    )
    assert first.seen == [[0, 1], [2, 3], [4, 5]]

    # Resume: nothing should go out.
    second = FakeProvider(cost_usd=0.01)
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=second,
        requests=requests,
    )

    assert second.seen == [], "a completed batch was re-sent and would be billed again"
    assert result.batches_sent == 0
    assert result.batches_skipped == 3


def test_only_the_unfinished_batches_are_retried_after_an_interruption(db, api_frames, tmp_path):
    requests = build_batches(frame_records(6), api_frames, batch_size=2, model_id="m")

    # Simulate: first batch completed, then the process died.
    db.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?, 'completed', ?, ?)",
        (new_id(), "s1", 0, 0, 1, 2, utc_now(), utc_now()),
    )

    provider = FakeProvider()
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=provider,
        requests=requests,
    )

    assert provider.seen == [[2, 3], [4, 5]]


def test_completed_batch_indexes_are_read_from_the_database(db):
    db.execute(
        "INSERT INTO batches (id, stage_run_id, batch_index, frame_start_index,"
        " frame_end_index, frame_count, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?, 'completed', ?, ?)",
        (new_id(), "s1", 4, 8, 9, 2, utc_now(), utc_now()),
    )
    assert completed_batch_indexes(db, "s1") == {4}


def test_a_skipped_batch_is_not_treated_as_completed(db, api_frames, tmp_path):
    # A skip is a gap to revisit, not finished work.
    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(fail_on=[0]),
        requests=requests,
    )
    assert completed_batch_indexes(db, "s1") == set()


# ── Persistence order ─────────────────────────────────────────────────────


def test_the_artifact_exists_before_the_batch_is_marked_completed(db, api_frames, tmp_path):
    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(),
        requests=requests,
    )

    row = db.execute("SELECT status, artifact_path, artifact_sha256 FROM batches").fetchone()
    assert row["status"] == "completed"
    assert row["artifact_sha256"]
    assert (tmp_path / row["artifact_path"]).is_file()


def test_the_batch_artifact_holds_the_descriptions(db, api_frames, tmp_path):
    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(),
        requests=requests,
    )

    path = next((tmp_path / "out" / "batches").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["frame_indexes"] == [0, 1]
    assert len(payload["descriptions"]) == 2


# ── Failures become visible gaps ──────────────────────────────────────────


def test_a_permanent_failure_becomes_skips_not_a_lost_video(db, api_frames, tmp_path):
    requests = build_batches(frame_records(4), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(fail_on=[0]),
        requests=requests,
    )

    assert len(result.skips) == 2
    assert result.batches_sent == 1, "the other batch should still have been described"
    assert result.has_gaps is True
    assert result.status == "completed_with_gaps"


def test_unreadable_output_becomes_skips(db, api_frames, tmp_path):
    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(fail_on=[0], error=SchemaValidationError("prose")),
        requests=requests,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
    )
    assert result.has_gaps is True


def test_a_clean_run_reports_no_gaps(db, api_frames, tmp_path):
    requests = build_batches(frame_records(4), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(),
        requests=requests,
    )
    assert result.has_gaps is False
    assert result.status == "completed"


# ── Budget ────────────────────────────────────────────────────────────────


def test_sending_stops_at_the_spending_limit(db, api_frames, tmp_path):
    requests = build_batches(frame_records(6), api_frames, batch_size=1, model_id="m")
    budget = BudgetTracker(limit_usd=0.0001, provider="anthropic")

    provider = FakeProvider(cost_usd=0.05)
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=provider,
        requests=requests,
        budget=budget,
    )

    assert result.stopped_on_budget is True
    assert provider.seen == [], "nothing should be sent once the limit is reached"


def test_work_finished_before_the_limit_is_kept(db, api_frames, tmp_path):
    requests = build_batches(frame_records(4), api_frames, batch_size=1, model_id="m")
    # Enough for roughly two batches at the estimated per-frame rate.
    budget = BudgetTracker(limit_usd=0.012, provider="anthropic")

    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(cost_usd=0.005),
        requests=requests,
        budget=budget,
    )

    assert result.stopped_on_budget is True
    assert result.batches_sent >= 1, "batches sent before the limit must be kept"
    assert len(result.descriptions) >= 1


def test_the_budget_stop_is_recorded_where_the_user_will_see_it(db, api_frames, tmp_path):
    requests = build_batches(frame_records(2), api_frames, batch_size=1, model_id="m")
    run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(cost_usd=1.0),
        requests=requests,
        budget=BudgetTracker(limit_usd=0.0001, provider="anthropic"),
    )

    row = db.execute("SELECT message, level FROM events WHERE kind='budget_stop'").fetchone()
    assert row is not None
    assert row["level"] == "warning"
    assert "kept" in row["message"]


def test_a_local_run_is_never_stopped_by_a_budget(db, api_frames, tmp_path):
    requests = build_batches(frame_records(4), api_frames, batch_size=1, model_id="m")
    budget = BudgetTracker(limit_usd=0.0, provider="ollama_local")

    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(cost_usd=None),
        requests=requests,
        budget=budget,
    )

    assert result.stopped_on_budget is False
    assert result.batches_sent == 4


def test_a_local_run_reports_no_provider_charge(db, api_frames, tmp_path):
    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(cost_usd=None),
        requests=requests,
    )

    assert result.total_cost_usd is None
    assert result.cost_label == "No provider API charge"


def test_cloud_costs_accumulate(db, api_frames, tmp_path):
    requests = build_batches(frame_records(4), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FakeProvider(cost_usd=0.01),
        requests=requests,
    )
    assert result.total_cost_usd == pytest.approx(0.02)


# ── Cooperative stop ──────────────────────────────────────────────────────


def test_a_stop_request_is_honoured_between_batches(db, api_frames, tmp_path):
    requests = build_batches(frame_records(6), api_frames, batch_size=1, model_id="m")
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2

    provider = FakeProvider()
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=provider,
        requests=requests,
        should_stop=should_stop,
    )

    assert result.batches_sent == 2
    assert result.stopped_at_index == 2


# ── Output files ──────────────────────────────────────────────────────────


def test_results_are_written_with_their_provenance(tmp_path):
    from app.pipeline.visual import VisualStageResult

    result = VisualStageResult(
        descriptions=[FrameDescription(index=0, visual_description="a chart")],
        batches_sent=1,
    )
    results_path, gaps_path = write_visual_results(tmp_path, result, source_filename="clip.mp4")

    payload = json.loads(results_path.read_text(encoding="utf-8"))
    assert payload["description_count"] == 1
    assert payload["source_filename"] == "clip.mp4"
    assert gaps_path is None, "no gaps file when there are no gaps"


def test_a_gaps_file_is_written_in_plain_language(tmp_path):
    from app.pipeline.visual import VisualStageResult
    from app.providers.base import SkipRecord

    result = VisualStageResult(skips=[SkipRecord(index=41, reason="the service refused")])
    _, gaps_path = write_visual_results(tmp_path, result, source_filename="clip.mp4")

    assert gaps_path is not None
    text = gaps_path.read_text(encoding="utf-8")
    assert "1 picture(s) have no description" in text
    # Numbered the way the user sees them: 1-based, so internal index 41 is
    # picture 42. Matched on the token rather than exact padding.
    assert " 42 " in text, "gaps should be numbered as the user sees them"
    assert "the service refused" in text
    assert "without redoing any other work" in text


def test_transient_errors_are_retried_before_skipping(db, api_frames, tmp_path):
    calls = {"n": 0}

    class FlakyProvider:
        def describe(self, request):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientProviderError("rate limited")
            return AnalysisResult(
                descriptions=[FrameDescription(index=f.index) for f in request.frames],
                provider="fake",
            )

    requests = build_batches(frame_records(2), api_frames, batch_size=2, model_id="m")
    result = run_visual_analysis(
        db,
        stage_run_id="s1",
        job_id="j1",
        job_video_id="v1",
        output_root=tmp_path,
        output_dir=tmp_path / "out",
        provider=FlakyProvider(),
        requests=requests,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, jitter=False),
    )

    assert result.batches_sent == 1
    assert result.has_gaps is False


# ── A stage that stopped early has not completed ──────────────────────────
#
# The stage records its own outcome, and 'completed' is what `_stage_completed`
# consults when a job resumes. A stage that stopped halfway and called itself
# completed would be skipped on the way back in, and every frame after the
# stopping point would go undescribed with nothing to say so — a pause quietly
# costing the user the rest of the video.


def _stage_context(connection, tmp_path, *, should_stop=None):
    from dataclasses import replace

    from app.core.config import Settings
    from app.pipeline.frames import MANIFEST_FILENAME
    from app.pipeline.stages import StageContext

    (tmp_path / MANIFEST_FILENAME).write_text(
        json.dumps({"frames": frame_records(6)}), encoding="utf-8"
    )

    settings = Settings().with_output_root(tmp_path)
    settings = replace(
        settings,
        visual_analysis=replace(
            settings.visual_analysis,
            enabled=True,
            provider="ollama_local",
            models={"ollama_local": "m"},
        ),
    )
    return StageContext(
        connection=connection,
        settings=settings,
        job_id="j1",
        job_video_id="v1",
        source_path=tmp_path / "a.mp4",
        output_dir=tmp_path,
        interval_ms=2000,
        should_stop=should_stop,
    )


def _visual_stage_status(connection) -> str:
    row = connection.execute(
        "SELECT status FROM stage_runs WHERE job_video_id='v1' AND stage='visual'"
        " ORDER BY attempt DESC LIMIT 1"
    ).fetchone()
    return str(row["status"])


def test_a_stage_stopped_by_the_user_records_itself_as_paused(db, api_frames, tmp_path):
    from app.pipeline.stages import run_visual_stage

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2

    context = _stage_context(db, tmp_path, should_stop=should_stop)
    result = run_visual_stage(context, provider=FakeProvider())

    assert result.stopped_at_index is not None
    assert _visual_stage_status(db) == "paused"


def test_a_paused_stage_is_not_treated_as_done_on_the_way_back_in(db, api_frames, tmp_path):
    from app.pipeline.stages import _stage_completed, run_visual_stage

    context = _stage_context(db, tmp_path, should_stop=lambda: True)
    run_visual_stage(context, provider=FakeProvider())

    assert _stage_completed(db, "v1", "visual") is False, (
        "resuming must re-enter the stage, or the rest of the video is never described"
    )


def test_a_stage_nobody_stopped_still_completes(db, api_frames, tmp_path):
    from app.pipeline.stages import _stage_completed, run_visual_stage

    context = _stage_context(db, tmp_path, should_stop=lambda: False)
    result = run_visual_stage(context, provider=FakeProvider())

    assert result.stopped_at_index is None
    assert _visual_stage_status(db) == "completed"
    assert _stage_completed(db, "v1", "visual") is True
