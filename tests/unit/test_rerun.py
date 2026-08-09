"""Targeted reruns.

The rule the whole feature exists to keep is: **a rerun never overwrites what
came before.** On a paid provider that is the difference between improving a
result and paying twice for one you already had; for collections it is the
difference between a citation and a moving target.

Everything else here is in service of that: frames and the transcript are
carried over rather than recomputed, descriptions outside the chosen scope are
kept verbatim, and switching the active version rewrites nothing.
"""

from __future__ import annotations

import json

import pytest

from app.core.db import open_database, utc_now
from app.pipeline.rerun import (
    RerunError,
    RerunScope,
    load_rerun_plan,
    make_active,
    plan_rerun,
    seed_new_version,
    start_rerun,
    version_summaries,
)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "out"


@pytest.fixture
def db(root):
    connection = open_database(root)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, frame_interval_ms,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("j1", "Course capture", "completed", str(root), 2000, utc_now(), utc_now()),
    )
    yield connection
    connection.close()


def add_processed_video(
    connection,
    root,
    video_id="v1",
    *,
    frame_count=6,
    described=None,
    confidences=None,
    version=1,
):
    """A processed video with frames, a transcript, and descriptions on disk.

    `described` limits which frame indices got a description at all — the ones
    left out are what "came back unusable" means.
    """
    described = list(range(frame_count)) if described is None else described
    confidences = confidences or {}

    directory = root / "j1" / f"{video_id}_v{version}"
    (directory / "frames").mkdir(parents=True, exist_ok=True)
    (directory / "frames_api").mkdir(parents=True, exist_ok=True)

    for index in range(frame_count):
        (directory / "frames" / f"{index:06d}_t{index:06d}.jpg").write_bytes(b"\xff\xd8")
        (directory / "frames_api" / f"{index:06d}_t{index:06d}.jpg").write_bytes(b"\xff\xd8")

    (directory / "frames_manifest.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "index": i,
                        "clean_filename": f"{i:06d}_t{i:06d}.jpg",
                        "api_filename": f"{i:06d}_t{i:06d}.jpg",
                        "timestamp_seconds": float(i),
                    }
                    for i in range(frame_count)
                ]
            }
        ),
        "utf-8",
    )
    (directory / "transcript.json").write_text(
        json.dumps({"segments": [{"start_seconds": 0, "text": "hello"}]}), "utf-8"
    )
    (directory / "transcript.txt").write_text("hello\n", "utf-8")
    (directory / "visual_results.json").write_text(
        json.dumps(
            {
                "descriptions": [
                    {
                        "index": i,
                        "confidence": confidences.get(i, "High"),
                        "visual_description": f"frame {i} as first described",
                    }
                    for i in described
                ]
            }
        ),
        "utf-8",
    )
    (directory / "assembled.txt").write_text("the first version's document\n", "utf-8")

    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " version, is_active_version, status, frame_count, duration_seconds,"
        " output_dir, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            "j1",
            "/src/clip.mp4",
            "capture.mp4",
            0,
            version,
            1,
            "completed",
            frame_count,
            600.0,
            f"j1/{video_id}_v{version}",
            utc_now(),
            utc_now(),
        ),
    )
    connection.commit()
    return directory


# ── Choosing a scope ──────────────────────────────────────────────────────


def test_every_picture_is_the_widest_scope(db, root):
    add_processed_video(db, root, frame_count=6)
    plan = plan_rerun(db, "v1", root, scope=RerunScope.ALL)

    assert plan.frame_count == 6
    assert plan.carried_over == 0


def test_low_confidence_selects_only_the_ones_marked_low(db, root):
    """The scope that makes the feature worth having: a better model asked to
    redo the handful that were doubtful, not the two thousand that were fine."""
    add_processed_video(db, root, frame_count=6, confidences={1: "Low", 4: "Low"})

    plan = plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE)

    assert plan.indices == [1, 4]
    assert plan.carried_over == 4, "the confident four are kept, not re-sent"


def test_unusable_results_are_the_frames_that_were_never_described(db, root):
    add_processed_video(db, root, frame_count=6, described=[0, 1, 2, 5])

    plan = plan_rerun(db, "v1", root, scope=RerunScope.FALLBACK)

    assert plan.indices == [3, 4]


def test_a_range_selects_between_the_two_ends_inclusive(db, root):
    add_processed_video(db, root, frame_count=10)
    plan = plan_rerun(db, "v1", root, scope=RerunScope.RANGE, start=3, end=5)

    assert plan.indices == [3, 4, 5]


def test_a_backwards_range_is_read_the_way_it_was_meant(db, root):
    add_processed_video(db, root, frame_count=10)
    assert plan_rerun(db, "v1", root, scope=RerunScope.RANGE, start=5, end=3).indices == [3, 4, 5]


def test_a_scope_that_matches_nothing_reports_nothing(db, root):
    """Reported as empty rather than silently widened. Quietly turning "redo the
    doubtful ones" into "redo all two thousand" would be an expensive surprise."""
    add_processed_video(db, root, frame_count=6)  # everything is High
    plan = plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE)

    assert plan.is_empty
    with pytest.raises(RerunError, match="nothing to do again"):
        start_rerun(db, plan, output_root=root)


def test_planning_changes_nothing(db, root):
    add_processed_video(db, root, frame_count=6)
    plan_rerun(db, "v1", root, scope=RerunScope.ALL)

    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 1


# ── The previous version survives ─────────────────────────────────────────


def test_a_rerun_creates_a_new_version_rather_than_replacing_one(db, root):
    directory = add_processed_video(db, root, frame_count=6, confidences={2: "Low"})
    plan = plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE)

    new_id = start_rerun(db, plan, output_root=root)

    rows = db.execute(
        "SELECT id, version, is_active_version FROM job_videos ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    assert [r["is_active_version"] for r in rows] == [0, 1]
    assert new_id != "v1"
    # And the first version's output is exactly as it was.
    assert (directory / "assembled.txt").read_text("utf-8") == "the first version's document\n"
    assert json.loads((directory / "visual_results.json").read_text("utf-8"))["descriptions"]


def test_a_collection_pinned_to_the_old_version_is_untouched(db, root):
    """The reason any of this matters. A citation that silently changes when the
    source is revised is worse than no citation."""
    from app.collections.model import assess_source, create_collection, load_collection, set_sources

    add_processed_video(db, root, frame_count=4, confidences={0: "Low"})
    collection_id = create_collection(db, name="Week 6")
    set_sources(db, collection_id, [assess_source(db, "v1", root)])
    before = load_collection(db, collection_id)

    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root)

    after = load_collection(db, collection_id)
    assert after.sources[0].job_video_id == before.sources[0].job_video_id == "v1"
    assert after.sources[0].source_version == 1


def test_the_new_version_gets_its_own_folder(db, root):
    previous = add_processed_video(db, root, frame_count=4)
    new_id = start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    assert row["output_dir"] != "j1/v1_v1"
    assert (root / row["output_dir"]).is_dir()
    assert previous.is_dir()


# ── The expensive parts are carried, not recomputed ───────────────────────


def test_frames_and_the_transcript_are_carried_over(db, root):
    """Re-transcribing an hour of audio on a processor to redo a few
    descriptions is an afternoon nobody gets back."""
    add_processed_video(db, root, frame_count=6)
    new_id = start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    target = root / row["output_dir"]

    assert len(list((target / "frames").glob("*.jpg"))) == 6
    assert (target / "transcript.json").is_file()
    assert (target / "frames_manifest.json").is_file()


def test_the_carried_stages_are_recorded_as_carried(db, root):
    """The stage functions skip work whose run is already complete — that is
    what stops the rerun re-extracting. The provenance says where the output
    came from, because a run claiming work it inherited makes the record useless
    for the one question it exists to answer."""
    add_processed_video(db, root, frame_count=4)
    new_id = start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    rows = db.execute(
        "SELECT stage, status, provenance_json FROM stage_runs WHERE job_video_id = ?",
        (new_id,),
    ).fetchall()

    stages = {r["stage"]: r for r in rows}
    assert stages["frames"]["status"] == "completed"
    assert stages["transcribe"]["status"] == "completed"
    assert json.loads(stages["frames"]["provenance_json"])["carried_over_from_version"] == 1
    assert "visual" not in stages, "the visual stage is the one that must actually run"


def test_frames_are_shared_rather_than_duplicated_where_the_filesystem_allows(db, root):
    """Two thousand JPEGs per version adds up. A link where possible, a copy
    where not — correctness first, space second."""
    add_processed_video(db, root, frame_count=4)
    new_id = start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    original = root / "j1" / "v1_v1" / "frames" / "000000_t000000.jpg"
    copy = root / row["output_dir"] / "frames" / "000000_t000000.jpg"

    assert copy.read_bytes() == original.read_bytes()


def test_the_plan_travels_with_the_new_folder(db, root):
    add_processed_video(db, root, frame_count=6, confidences={1: "Low", 3: "Low"})
    new_id = start_rerun(
        db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root
    )

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    loaded = load_rerun_plan(root / row["output_dir"])

    assert loaded is not None
    assert loaded.indices == frozenset({1, 3})
    assert loaded.from_version == 1


def test_an_ordinary_job_has_no_rerun_plan(db, root):
    """A first run must not be mistaken for a rerun, or the visual stage would
    describe a subset of the frames and call the video done."""
    directory = add_processed_video(db, root, frame_count=4)
    assert load_rerun_plan(directory) is None


def test_seeding_is_safe_when_the_previous_version_has_no_transcript(db, root, tmp_path):
    previous = tmp_path / "previous"
    (previous / "frames").mkdir(parents=True)
    (previous / "frames" / "a.jpg").write_bytes(b"\xff\xd8")
    target = tmp_path / "target"

    seed_new_version(previous, target)

    assert (target / "frames" / "a.jpg").is_file()


def test_a_rerun_needs_the_previous_folder_to_still_exist(db, root):
    """Reported, with nothing created. A half-made version pointing at an empty
    folder is worse than a refusal."""
    import shutil

    add_processed_video(db, root, frame_count=4)
    plan = plan_rerun(db, "v1", root, scope=RerunScope.ALL)
    shutil.rmtree(root / "j1" / "v1_v1")

    with pytest.raises(RerunError, match="could not be found"):
        start_rerun(db, plan, output_root=root)
    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 1


# ── Queueing ──────────────────────────────────────────────────────────────


def test_the_job_is_put_back_in_the_queue(db, root):
    add_processed_video(db, root, frame_count=4)
    db.execute("UPDATE jobs SET status = 'completed' WHERE id = 'j1'")

    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    assert db.execute("SELECT status FROM jobs WHERE id = 'j1'").fetchone()["status"] == "ready"


def test_the_rerun_is_recorded_in_the_log_with_what_it_will_do(db, root):
    add_processed_video(db, root, frame_count=6, confidences={2: "Low"})
    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root)

    message = db.execute("SELECT message FROM events WHERE kind = 'rerun_requested'").fetchone()[
        "message"
    ]

    assert "Version 2" in message
    assert "untouched" in message


# ── Switching versions ────────────────────────────────────────────────────


def test_versions_are_listed_newest_first_with_what_they_produced(db, root):
    add_processed_video(db, root, frame_count=6, confidences={1: "Low"})
    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root)

    summaries = version_summaries(db, "v1", root)

    assert [s.version for s in summaries] == [2, 1]
    assert summaries[0].is_active is True
    assert summaries[1].low_confidence == 1
    assert summaries[1].scope_label == "First run"


def test_the_active_version_can_be_switched_back(db, root):
    add_processed_video(db, root, frame_count=4)
    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    make_active(db, "v1")

    rows = db.execute(
        "SELECT version, is_active_version FROM job_videos ORDER BY version"
    ).fetchall()
    assert [r["is_active_version"] for r in rows] == [1, 0]


def test_switching_versions_deletes_nothing(db, root):
    directory = add_processed_video(db, root, frame_count=4)
    new_id = start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)
    new_row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()

    make_active(db, "v1")

    assert directory.is_dir()
    assert (root / new_row["output_dir"]).is_dir()
    assert db.execute("SELECT COUNT(*) FROM job_videos").fetchone()[0] == 2


def test_only_one_version_is_ever_active(db, root):
    add_processed_video(db, root, frame_count=4)
    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)
    second = db.execute("SELECT id FROM job_videos WHERE version = 2").fetchone()["id"]

    make_active(db, "v1")
    make_active(db, second)

    active = db.execute("SELECT COUNT(*) FROM job_videos WHERE is_active_version = 1").fetchone()[0]
    assert active == 1


def test_activating_something_that_is_not_there_is_refused(db, root):
    with pytest.raises(RerunError):
        make_active(db, "no-such-version")


# ── What the visual stage actually does with a plan ───────────────────────
#
# The scope has to reach the provider. A rerun that recomputed the plan
# correctly and then sent every frame anyway would be the most expensive kind of
# bug this product can have: silent, and billed.


class RecordingProvider:
    """Stands in for a description model and records what it was asked for."""

    def __init__(self):
        self.seen: list[int] = []

    def describe(self, request):
        from app.providers.base import AnalysisResult, FrameDescription

        self.seen.extend(frame.index for frame in request.frames)
        return AnalysisResult(
            descriptions=[
                FrameDescription(
                    index=frame.index,
                    visual_description=f"frame {frame.index} as described again",
                    confidence="High",
                )
                for frame in request.frames
            ],
            provider="fake",
            model_id="better-model",
            cost_usd=None,
        )


def run_the_visual_stage(connection, root, job_video_id, provider):
    from dataclasses import replace

    from app.core.config import Settings
    from app.pipeline.stages import StageContext, run_visual_stage

    settings = Settings().with_output_root(root)
    settings = replace(
        settings,
        visual_analysis=replace(
            settings.visual_analysis,
            enabled=True,
            provider="ollama_local",
            models={"ollama_local": "better-model"},
        ),
    )

    row = connection.execute("SELECT * FROM job_videos WHERE id = ?", (job_video_id,)).fetchone()

    return run_visual_stage(
        StageContext(
            connection=connection,
            settings=settings,
            job_id="j1",
            job_video_id=job_video_id,
            source_path=root / "clip.mp4",
            output_dir=root / row["output_dir"],
            interval_ms=2000,
        ),
        provider=provider,
    )


def test_a_rerun_only_sends_the_frames_it_was_asked_to(db, root):
    """The whole feature in one assertion. Sending the rest would be charged
    again to arrive at answers we already have."""
    add_processed_video(db, root, frame_count=6, confidences={1: "Low", 4: "Low"})
    new_id = start_rerun(
        db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root
    )

    provider = RecordingProvider()
    run_the_visual_stage(db, root, new_id, provider)

    assert sorted(provider.seen) == [1, 4]


def test_the_new_version_still_describes_the_whole_video(db, root):
    """Only two frames were re-sent, but the result has to stand on its own —
    a version describing two frames out of six would be useless to assemble."""
    add_processed_video(db, root, frame_count=6, confidences={1: "Low", 4: "Low"})
    new_id = start_rerun(
        db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root
    )

    run_the_visual_stage(db, root, new_id, RecordingProvider())

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    written = json.loads((root / row["output_dir"] / "visual_results.json").read_text("utf-8"))[
        "descriptions"
    ]

    assert [d["index"] for d in written] == [0, 1, 2, 3, 4, 5]
    # The redone ones carry the new text; the rest are exactly as they were.
    by_index = {d["index"]: d["visual_description"] for d in written}
    assert by_index[1] == "frame 1 as described again"
    assert by_index[0] == "frame 0 as first described"


def test_a_first_run_describes_everything(db, root):
    """No plan file means no scope. If an ordinary job were treated as a rerun
    it would describe a subset and call the video finished."""
    add_processed_video(db, root, frame_count=4, described=[])
    db.execute("DELETE FROM stage_runs WHERE job_video_id = 'v1'")

    provider = RecordingProvider()
    run_the_visual_stage(db, root, "v1", provider)

    assert sorted(provider.seen) == [0, 1, 2, 3]


def test_the_run_records_what_it_did_and_what_it_inherited(db, root):
    add_processed_video(db, root, frame_count=6, confidences={2: "Low"})
    new_id = start_rerun(
        db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root
    )

    run_the_visual_stage(db, root, new_id, RecordingProvider())

    provenance = json.loads(
        db.execute(
            "SELECT provenance_json FROM stage_runs WHERE job_video_id = ? AND stage = 'visual'",
            (new_id,),
        ).fetchone()["provenance_json"]
    )

    assert provenance["described_this_run"] == 1
    assert provenance["carried_over"] == 5
    assert provenance["rerun_scope"] == "low_confidence"


def test_carrying_forward_survives_a_description_from_an_older_schema(db, root):
    """Older output must stay buildable. A rerun that fell over on the previous
    version's file would make the oldest work the hardest to improve."""
    directory = add_processed_video(db, root, frame_count=4, confidences={0: "Low"})
    payload = json.loads((directory / "visual_results.json").read_text("utf-8"))
    for entry in payload["descriptions"]:
        entry["a_field_this_version_dropped"] = "something"
    (directory / "visual_results.json").write_text(json.dumps(payload), "utf-8")

    new_id = start_rerun(
        db, plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE), output_root=root
    )
    run_the_visual_stage(db, root, new_id, RecordingProvider())

    row = db.execute("SELECT output_dir FROM job_videos WHERE id = ?", (new_id,)).fetchone()
    written = json.loads((root / row["output_dir"] / "visual_results.json").read_text("utf-8"))[
        "descriptions"
    ]
    assert len(written) == 4


def test_a_rerun_turns_descriptions_on_for_the_job(db, root):
    """The worker honours the job's own description choice. A job created with
    descriptions off would otherwise queue the new version and then skip the one
    stage the rerun exists to run."""
    add_processed_video(db, root, frame_count=4, confidences={0: "Low"})
    db.execute("UPDATE jobs SET visual_provider = 'none', visual_model_id = '' WHERE id = 'j1'")

    start_rerun(
        db,
        plan_rerun(db, "v1", root, scope=RerunScope.LOW_CONFIDENCE),
        output_root=root,
        provider="ollama_local",
        model_id="a-better-model",
    )

    job = db.execute("SELECT visual_provider, visual_model_id FROM jobs WHERE id='j1'").fetchone()
    assert job["visual_provider"] == "ollama_local"
    assert job["visual_model_id"] == "a-better-model"


def test_a_rerun_without_a_named_provider_leaves_the_job_alone(db, root):
    add_processed_video(db, root, frame_count=4)
    db.execute("UPDATE jobs SET visual_provider = 'ollama_local' WHERE id = 'j1'")

    start_rerun(db, plan_rerun(db, "v1", root, scope=RerunScope.ALL), output_root=root)

    job = db.execute("SELECT visual_provider, status FROM jobs WHERE id='j1'").fetchone()
    assert job["visual_provider"] == "ollama_local"
    assert job["status"] == "ready"
