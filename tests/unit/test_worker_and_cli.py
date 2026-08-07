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


def test_worker_picks_up_a_ready_job(settings, db):
    db.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Ready job", "ready", str(settings.output_root), utc_now(), utc_now()),
    )
    run_worker(settings, once=True)
    assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"] == "preparing"


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
    # analyzing -> ready by reconciliation, then claimed in the same pass.
    status = db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()["status"]
    assert status in {"ready", "preparing"}


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
