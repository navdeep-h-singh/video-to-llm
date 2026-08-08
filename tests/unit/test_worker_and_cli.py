"""Worker ownership, doctor checks, the CLI surface, and the smoke test."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.cli.main import build_parser, main
from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.core.locks import read_claim, worker_lock
from app.core.logging import configure_logging
from app.services.doctor import CheckState, check_localhost_binding, run_doctor
from app.services.smoke import run_smoke_test
from app.worker.runner import Worker, run_worker


@pytest.fixture(autouse=True)
def _quiet_logging():
    configure_logging(level="CRITICAL", force=True)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings().with_output_root(tmp_path / "out")


@pytest.fixture
def db(settings):
    connection = open_database(settings.output_root)
    yield connection
    connection.close()


# ── Worker ────────────────────────────────────────────────────────────────


def test_worker_exits_cleanly_when_there_is_nothing_to_do(settings):
    assert run_worker(settings, once=True) == 0


def test_worker_releases_ownership_on_exit(settings, db):
    run_worker(settings, once=True)
    assert read_claim(db, settings.output_root) is None


def test_a_second_worker_is_refused_while_the_first_holds_the_root(settings, db):
    with worker_lock(db, settings.output_root):
        assert run_worker(settings, once=True) == 1


def test_worker_refuses_to_start_without_an_output_root():
    assert run_worker(Settings(), once=True) == 1


def test_a_ready_job_with_no_videos_settles_as_completed(settings, db):
    # Nothing to do is a finished job, not a stuck one.
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Ready job", "ready", str(settings.output_root), utc_now(), utc_now()),
    )
    run_worker(settings, once=True)
    row = db.execute("SELECT status, completed_at FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


def test_a_video_that_cannot_be_read_marks_the_job_as_needing_attention(settings, db):
    # The gap must be visible rather than buried in an otherwise green job.
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Bad source", "ready", str(settings.output_root), utc_now(), utc_now()),
    )
    db.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v1", "j1", "/does/not/exist.mp4", "exist.mp4", 0, "pending", utc_now(), utc_now()),
    )
    run_worker(settings, once=True)

    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == (
        "needs_attention"
    )
    video = db.execute("SELECT status, error_message FROM job_videos WHERE id='v1'").fetchone()
    assert video["status"] == "needs_attention"
    assert video["error_message"]


def test_worker_ignores_jobs_that_are_not_ready(settings, db):
    for job_id, status in (("d", "draft"), ("p", "paused"), ("c", "completed")):
        db.execute(
            "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (job_id, status, status, str(settings.output_root), utc_now(), utc_now()),
        )
    run_worker(settings, once=True)
    states = {row["id"]: row["status"] for row in db.execute("SELECT id, status FROM jobs")}
    assert states == {"d": "draft", "p": "paused", "c": "completed"}


def test_a_failing_job_does_not_take_the_worker_down(settings, db, monkeypatch):
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Doomed", "ready", str(settings.output_root), utc_now(), utc_now()),
    )

    def explode(self, job):
        raise RuntimeError("stage blew up")

    monkeypatch.setattr(Worker, "process_job", explode)
    assert run_worker(settings, once=True) == 0

    row = db.execute("SELECT status, error_message FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "needs_attention"
    assert "stage blew up" in row["error_message"]


def test_worker_stops_when_it_loses_ownership(settings, db):
    with worker_lock(db, settings.output_root) as owner_id:
        worker = Worker(settings, db, worker_id="a-different-worker")
        assert worker.beat() is False
        assert worker.stopping is True
        assert owner_id != "a-different-worker"


def test_stop_request_is_cooperative(settings, db):
    worker = Worker(settings, db, worker_id="w1")
    assert worker.stopping is False
    worker.request_stop()
    assert worker.stopping is True


def test_worker_reconciles_before_taking_new_work(settings, db):
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Interrupted", "analyzing", str(settings.output_root), utc_now(), utc_now()),
    )
    run_worker(settings, once=True)
    # 'analyzing' -> 'ready' by reconciliation, then claimed and run in the same
    # pass. With no videos attached it settles as completed.
    status = db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"]
    assert status == "completed"

    recovered = db.execute(
        "SELECT message FROM events WHERE job_id='j1' AND kind='recovered'"
    ).fetchone()
    assert recovered is not None, "the interruption should be recorded for the user"


# ── Doctor ────────────────────────────────────────────────────────────────


def test_doctor_reports_the_localhost_boundary_as_ok():
    assert check_localhost_binding(Settings()).state is CheckState.OK


def test_doctor_runs_every_check(settings):
    report = run_doctor(settings)
    keys = {check.key for check in report.checks}
    assert keys == {
        "binding",
        "ffmpeg",
        "transcription",
        "output_root",
        "visual_analysis",
        "worker",
    }


def test_unconfigured_visual_analysis_is_optional_not_a_failure(settings):
    # A first-time user must be able to process a video without meeting any of
    # the provider machinery.
    check = run_doctor(settings).get("visual_analysis")
    assert check.state is CheckState.OPTIONAL
    assert not check.blocking


def test_a_machine_with_no_output_root_is_not_ready():
    report = run_doctor(Settings())
    assert report.ready is False
    assert report.get("output_root").state is CheckState.FAIL


def test_local_provider_reports_no_charge_language(tmp_path):
    from app.core.config import VisualAnalysisSettings

    settings = Settings(
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="ollama_local", model_id="qwen2.5vl:7b"
        )
    ).with_output_root(tmp_path)
    detail = run_doctor(settings).get("visual_analysis").detail
    assert "no provider charge" in detail.lower()
    assert "$0.00" not in detail


def test_external_provider_states_what_leaves_the_machine(tmp_path):
    from app.core.config import VisualAnalysisSettings

    settings = Settings(
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="anthropic", model_id="claude-sonnet-4-5"
        )
    ).with_output_root(tmp_path)
    check = run_doctor(settings).get("visual_analysis")
    assert "never your video" in check.remediation.lower()


def test_doctor_report_is_printable(settings):
    from app.services.doctor import format_report

    text = format_report(run_doctor(settings))
    assert "Runs only on this computer" in text
    assert text.strip()


# ── Smoke test ────────────────────────────────────────────────────────────


def test_smoke_test_passes(settings, capsys):
    assert run_smoke_test(settings) == 0
    assert "no network used" in capsys.readouterr().out


def test_smoke_test_does_not_touch_the_real_output_root(settings):
    # It uses a throwaway directory, so it can never disturb real work.
    marker = settings.output_root / "precious.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("do not delete", encoding="utf-8")

    run_smoke_test(settings)
    assert marker.read_text(encoding="utf-8") == "do not delete"


# ── CLI ───────────────────────────────────────────────────────────────────


def test_every_required_command_is_registered():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)
    assert {
        "start",
        "start-ui",
        "run-worker",
        "doctor",
        "smoke-test",
        "status",
        "import",
    } <= commands


def test_doctor_command_exits_zero_on_a_ready_machine(tmp_path, capsys):
    code = main(["--output-root", str(tmp_path), "doctor"])
    capsys.readouterr()
    assert code == 0


def test_status_command_is_friendly_before_anything_exists(tmp_path, capsys):
    assert main(["--output-root", str(tmp_path / "fresh"), "status"]) == 0
    assert "No jobs yet" in capsys.readouterr().out


def test_smoke_test_command_exits_zero(tmp_path, capsys):
    assert main(["--output-root", str(tmp_path), "smoke-test"]) == 0
    capsys.readouterr()


def test_run_worker_once_exits_zero(tmp_path, capsys):
    assert main(["--output-root", str(tmp_path), "run-worker", "--once"]) == 0
    capsys.readouterr()


def test_an_unknown_command_is_rejected():
    with pytest.raises(SystemExit):
        main(["not-a-command"])


def test_import_reports_that_it_is_not_available_yet(tmp_path):
    # Honest failure beats a command that silently does nothing.
    assert main(["--output-root", str(tmp_path), "import", str(tmp_path)]) == 1


# ── Web application ───────────────────────────────────────────────────────


def test_the_app_asserts_the_loopback_boundary_at_construction(settings):
    from app.web.app import create_app

    app = create_app(settings)
    assert app is not None


def test_health_endpoint_reports_the_bound_address(settings):
    from fastapi.testclient import TestClient

    from app.web.app import create_app

    with TestClient(create_app(settings)) as client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["bound_to"] == "127.0.0.1"


def test_the_api_documentation_endpoints_are_disabled(settings):
    from fastapi.testclient import TestClient

    from app.web.app import create_app

    with TestClient(create_app(settings)) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


def test_settings_with_a_different_port_still_binds_loopback(settings):
    assert replace(settings, port=9999).host == "127.0.0.1"


# ── The terminal bell ─────────────────────────────────────────────────────
#
# The cheapest way to reach someone who started this from a shell and switched
# away: no permission, no service, no outbound call, nothing to install.


def _finish_a_job(settings, connection):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Ready job", "ready", str(settings.output_root), utc_now(), utc_now()),
    )
    connection.commit()
    run_worker(settings, once=True)


class _FakeTerminal:
    def __init__(self, *, tty=True):
        self.tty = tty
        self.written = ""

    def isatty(self):
        return self.tty

    def write(self, text):
        self.written += text

    def flush(self):
        pass


def test_a_finished_job_rings_the_terminal_bell(settings, db, monkeypatch):
    terminal = _FakeTerminal()
    monkeypatch.setattr("sys.stderr", terminal)

    _finish_a_job(settings, db)

    assert "\a" in terminal.written


def test_the_bell_can_be_switched_off(settings, db, monkeypatch):
    from app.core.config import NotificationSettings

    quiet = replace(settings, notifications=NotificationSettings(terminal_bell=False))
    terminal = _FakeTerminal()
    monkeypatch.setattr("sys.stderr", terminal)

    _finish_a_job(quiet, db)

    assert "\a" not in terminal.written


def test_nothing_is_written_when_there_is_no_terminal(settings, db, monkeypatch):
    """A worker started detached has no terminal to ring, and a stray control
    character in a redirected log is noise at best."""
    terminal = _FakeTerminal(tty=False)
    monkeypatch.setattr("sys.stderr", terminal)

    _finish_a_job(settings, db)

    assert terminal.written == ""


def test_a_closed_stream_does_not_fail_a_finished_job(settings, db, monkeypatch):
    """The job succeeded. Losing the bell is not a reason to report otherwise."""

    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr("sys.stderr", Closed())

    _finish_a_job(settings, db)

    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "completed"


# ── A job's own description choice ────────────────────────────────────────
#
# The new-job screen offers a per-job choice and records it. The worker used to
# ignore it and read the global setting, which made the control decorative in
# the worst direction: "skip descriptions" described everything anyway if the
# global setting happened to be on. On a paid provider that is money spent on
# work the user explicitly declined.


def _job_row(connection, *, provider, model=""):
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, visual_provider,"
        " visual_model_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("j1", "A job", "ready", "/out", provider, model, utc_now(), utc_now()),
    )
    connection.commit()
    return connection.execute("SELECT * FROM jobs WHERE id='j1'").fetchone()


def _worker(settings, connection):
    return Worker(settings, connection, worker_id="test")


def test_a_job_that_declined_descriptions_does_not_get_them(settings, db):
    """The expensive direction of the bug, and the reason this is tested."""
    from app.core.config import VisualAnalysisSettings

    globally_on = replace(
        settings,
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="anthropic", model_id="a-model"
        ),
    )
    job = _job_row(db, provider="none")

    resolved = _worker(globally_on, db).settings_for(job)

    assert resolved.visual_analysis.enabled is False
    assert resolved.visual_analysis.provider == "none"


def test_a_job_that_asked_for_descriptions_gets_them(settings, db):
    job = _job_row(db, provider="ollama_local", model="qwen2.5vl:7b")

    resolved = _worker(settings, db).settings_for(job)

    assert resolved.visual_analysis.enabled is True
    assert resolved.visual_analysis.provider == "ollama_local"
    assert resolved.visual_analysis.model_id == "qwen2.5vl:7b"


def test_the_job_keeps_the_provider_it_was_created_with(settings, db):
    """Changing the global setting later must not retarget work already queued
    against a different service."""
    from app.core.config import VisualAnalysisSettings

    changed_since = replace(
        settings,
        visual_analysis=VisualAnalysisSettings(
            enabled=True, provider="openai", model_id="something-else"
        ),
    )
    job = _job_row(db, provider="ollama_local", model="qwen2.5vl:7b")

    assert _worker(changed_since, db).settings_for(job).visual_analysis.provider == "ollama_local"


def test_everything_else_about_the_settings_is_untouched(settings, db):
    """Only the description choice comes from the job. The output root, the
    budget, and the worker's own tuning are properties of this machine."""
    job = _job_row(db, provider="ollama_local")

    resolved = _worker(settings, db).settings_for(job)

    assert resolved.output_root == settings.output_root
    assert resolved.worker == settings.worker
    assert resolved.visual_analysis.budget == settings.visual_analysis.budget
