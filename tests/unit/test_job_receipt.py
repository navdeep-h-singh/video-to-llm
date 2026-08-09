"""What a finished job reports about itself.

The counterpart to `test_job_plan.py`. That one guards the promise made before a
job runs; this one guards the account given afterwards. They are deliberately
the same shape, because the pair is the product's whole claim: you were told
1,488 pictures and nothing uploaded, and afterwards you can see 1,488 pictures
and nothing uploaded.

The Files screen already said what exists and what each file is for. It never
said what happened — how long the video was, what was done to it, whether
anything left, and how much smaller the document is than the alternative.

A failure here is a regression.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import new_id, open_database, utc_now
from app.services.receipt import build_receipt
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


def _finished_job(
    connection,
    root,
    *,
    provider: str = "none",
    model: str = "",
    described: int = 0,
    frames: int = 10,
    document: str = "00:00:00  Hello\n",
) -> str:
    """A job that has run, with the rows a real run leaves behind."""
    job_id, video_id = new_id(), new_id()
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, output_dirname, frame_interval_ms,"
        " visual_provider, visual_model_id, created_at, updated_at, started_at, completed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            "lecture",
            "completed",
            str(root),
            "lecture",
            2000,
            provider,
            model,
            utc_now(),
            utc_now(),
            "2026-08-09T10:00:00+00:00",
            "2026-08-09T10:12:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, duration_seconds,"
        " sequence, version, is_active_version, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            job_id,
            "/src/lecture.mp4",
            "lecture.mp4",
            125.0,
            0,
            1,
            1,
            "completed",
            utc_now(),
            utc_now(),
        ),
    )

    for stage, done in (("frames", frames), ("visual", described)):
        if not done:
            continue
        connection.execute(
            "INSERT INTO stage_runs (id, job_video_id, stage, attempt, status, items_done,"
            " started_at, finished_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                video_id,
                stage,
                1,
                "completed",
                done,
                "2026-08-09T10:00:00+00:00",
                "2026-08-09T10:05:00+00:00",
                utc_now(),
                utc_now(),
            ),
        )

    folder = root / "lecture" / f"{video_id}_v1"
    folder.mkdir(parents=True)
    (folder / "assembled.txt").write_text(document, encoding="utf-8")
    connection.execute(
        "INSERT INTO artifacts (id, job_id, job_video_id, kind, relative_path, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            new_id(),
            job_id,
            video_id,
            "assembled",
            f"lecture/{video_id}_v1/assembled.txt",
            utc_now(),
        ),
    )
    return job_id


def test_a_receipt_reports_what_was_processed(db, settings):
    job_id = _finished_job(db, settings.output_root)
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.videos == ["lecture.mp4"]
    assert receipt.length_label == "0:02:05"
    assert receipt.interval_label == "a picture every 2 seconds"


def test_a_receipt_reports_how_long_it_took(db, settings):
    """Twelve minutes, from the recorded start and finish. Measured, not
    invented — a job with no recorded finish reports nothing."""
    job_id = _finished_job(db, settings.output_root)
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.took_label == "about 12 minutes"


def test_a_local_job_says_plainly_that_nothing_left(db, settings):
    job_id = _finished_job(db, settings.output_root, provider="none")
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.anything_left is False
    assert receipt.left_machine == "Nothing left this computer. No network request was made."
    assert receipt.described_label == "not described"


def test_a_service_job_says_how_many_pictures_were_sent(db, settings):
    """The counterpart to the plan's promise. It has to be the count that was
    actually described, not the count that was planned — a job stopped by its
    budget cap sent fewer, and reporting the plan's number would overstate it."""
    job_id = _finished_job(
        db, settings.output_root, provider="anthropic", model="claude-x", described=7
    )
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.anything_left is True
    assert receipt.left_machine == (
        "7 still pictures were sent to Claude. The video and its audio were not."
    )
    assert receipt.described_label == "7 pictures described"
    assert receipt.described_by == "Claude · claude-x"


def test_a_local_model_counts_as_nothing_leaving(db, settings):
    job_id = _finished_job(
        db, settings.output_root, provider="ollama_local", model="qwen2.5vl:7b", described=10
    )
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.anything_left is False
    assert "Nothing left this computer" in receipt.left_machine


def test_the_saving_is_measured_against_the_pictures_that_were_taken(db, settings):
    """The comparison is against sending the frames, which is the realistic
    alternative because most services accept no video at all."""
    job_id = _finished_job(db, settings.output_root, frames=100, document="x" * 3600)
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.document_tokens == 1000  # 3,600 characters at 3.6 per token
    assert receipt.frames_tokens == 139_300
    assert receipt.saving_label == "99%"


def test_a_document_bigger_than_the_pictures_claims_no_saving(db, settings):
    """A short clip with a long transcript can genuinely lose. Reporting a
    negative saving as a positive one would be the kind of number nobody checks."""
    job_id = _finished_job(db, settings.output_root, frames=1, document="x" * 100_000)
    receipt = build_receipt(db, settings.output_root, job_id)

    assert receipt.saving_label is None


def test_a_job_that_never_ran_reports_nothing_rather_than_zeroes(db, settings):
    receipt = build_receipt(db, settings.output_root, "no-such-job")

    assert receipt.has_anything is False
    assert receipt.document_tokens is None
    assert receipt.took_label is None


def test_the_screen_shows_the_receipt_above_the_files(db, settings):
    job_id = _finished_job(db, settings.output_root, provider="anthropic", described=4)
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)

    markup = client.get(f"/jobs/{job_id}/outputs").text

    assert "What this job did" in markup
    assert "lecture.mp4" in markup
    assert "4 still pictures were sent to Claude" in markup
    # The token figures must never be presented as exact.
    assert "estimates, not a real tokenisation" in markup


def test_the_screen_still_renders_for_a_job_with_no_receipt(db, settings):
    """The panel is skipped rather than rendered empty. Every screen has to
    survive an empty install — that guard already caught a wrong column name
    here once."""
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    response = client.get("/jobs/does-not-exist/outputs")

    assert response.status_code in {200, 404}
    if response.status_code == 200:
        assert "What this job did" not in response.text


# ── The page behind the badge ─────────────────────────────────────────────


def test_the_badge_links_to_the_page_that_backs_it(db, settings):
    """The header has promised "nothing is uploaded" since the first screen.
    For three sessions there was nowhere to click to find out what that rested
    on, which makes it a slogan rather than a claim."""
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)
    markup = client.get("/").text

    assert 'class="localbadge" href="/privacy"' in markup


def test_a_machine_that_has_sent_nothing_says_so(db, settings):
    _finished_job(db, settings.output_root, provider="none")
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)

    text = client.get("/privacy").text
    assert "no job on this machine has sent anything to a service" in text


def test_pictures_sent_counts_work_that_ran_not_what_was_configured(db, settings):
    """A job set to a service and cancelled before its first batch sent nothing.

    Counting configuration rather than completed work would report an upload
    that never happened — on the one page where being wrong is unforgivable.
    """
    _finished_job(db, settings.output_root, provider="anthropic", described=0)
    client = TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL)

    assert "no job on this machine has sent anything" in client.get("/privacy").text

    _finished_job(db, settings.output_root, provider="anthropic", described=12)
    text = client.get("/privacy").text
    assert "12" in text
    assert "no job on this machine has sent anything" not in text
