"""The durable worker loop.

Owns jobs. The interface controls and observes them, but closing the browser
never stops work here, and a job resumes safely after a crash, a restart, or a
suspended laptop.

Runs stages 1 and 2 for every video in a job, in the order the user confirmed.
A video that cannot be read marks itself and the job as needing attention rather
than abandoning the videos that follow it.
"""

from __future__ import annotations

import signal
import sqlite3
import threading
import time
from pathlib import Path

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.core.locks import (
    HEARTBEAT_INTERVAL_SECONDS,
    WorkerAlreadyRunningError,
    heartbeat,
    worker_lock,
)
from app.core.logging import get_logger
from app.core.redaction import redacted_exception_text
from app.pipeline.stages import StageContext, run_frames_stage, run_transcription_stage
from app.worker.reconcile import reconcile

logger = get_logger(__name__)


class Worker:
    def __init__(self, settings: Settings, connection: sqlite3.Connection, worker_id: str):
        self.settings = settings
        self.connection = connection
        self.worker_id = worker_id
        self.output_root: Path = settings.output_root  # type: ignore[assignment]
        self._stop = threading.Event()
        self._last_heartbeat = 0.0

    def request_stop(self, *_: object) -> None:
        """Ask the loop to finish the current unit of work and exit.

        Deliberately cooperative. Killing mid-write is what atomic writes exist
        to survive, but exiting cleanly means the claim is released and the next
        start has nothing to reconcile.
        """
        if not self._stop.is_set():
            logger.info("Stop requested — finishing the current step first.")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def beat(self) -> bool:
        """Refresh the claim on schedule. False when ownership was lost."""
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
            return True
        self._last_heartbeat = now

        if not heartbeat(self.connection, self.output_root, self.worker_id):
            logger.error(
                "This worker no longer owns the output folder — another worker took over. "
                "Stopping so the two do not write over each other."
            )
            self._stop.set()
            return False
        return True

    def claim_next_job(self) -> sqlite3.Row | None:
        """Return the oldest job waiting for work, if any."""
        return self.connection.execute(
            "SELECT * FROM jobs WHERE status = 'ready' ORDER BY created_at LIMIT 1"
        ).fetchone()

    def process_job(self, job: sqlite3.Row) -> None:
        """Run every stage for every video in one job, in confirmed order."""
        logger.info("Picked up job %s (%s)", job["id"][:8], job["name"])
        self.connection.execute(
            "UPDATE jobs SET status = 'preparing', started_at = COALESCE(started_at, ?),"
            " updated_at = ? WHERE id = ?",
            (utc_now(), utc_now(), job["id"]),
        )

        videos = self.connection.execute(
            "SELECT * FROM job_videos WHERE job_id = ? AND is_active_version = 1"
            " AND status NOT IN ('completed', 'completed_with_gaps', 'cancelled', 'skipped')"
            " ORDER BY sequence",
            (job["id"],),
        ).fetchall()

        if not videos:
            self._settle_job(job["id"])
            return

        failed = 0
        for video in videos:
            if self.stopping:
                logger.info("Stopping before video %s as requested.", video["sequence"] + 1)
                return
            if not self.process_video(job, video):
                failed += 1

        self._settle_job(job["id"], had_failures=failed > 0)

    def process_video(self, job: sqlite3.Row, video: sqlite3.Row) -> bool:
        """Run stages 1 and 2 for one video. False when it could not finish."""
        interval_ms = job["frame_interval_ms"] or self.settings.sampling.interval_ms()
        output_dir = self.output_root / job["id"] / f"{video['id']}_v{video['version']}"

        context = StageContext(
            connection=self.connection,
            settings=self.settings,
            job_id=job["id"],
            job_video_id=video["id"],
            source_path=Path(video["source_path"]),
            output_dir=output_dir,
            interval_ms=interval_ms,
        )

        try:
            self._set_video_status(video["id"], "preparing")
            self._set_job_status(job["id"], "preparing")
            run_frames_stage(
                context,
                make_api_copies=self.settings.visual_analysis.enabled,
            )

            self._set_video_status(video["id"], "transcribing")
            self._set_job_status(job["id"], "transcribing")
            run_transcription_stage(context)
        except Exception as error:
            # One unreadable video must not abandon the others in the job.
            logger.error(
                "Video %s failed: %s", video["display_name"], redacted_exception_text(error)
            )
            self.connection.execute(
                "UPDATE job_videos SET status = 'needs_attention', error_message = ?,"
                " updated_at = ? WHERE id = ?",
                (redacted_exception_text(error), utc_now(), video["id"]),
            )
            return False

        self._set_video_status(video["id"], "completed")
        return True

    def _set_video_status(self, video_id: str, status: str) -> None:
        self.connection.execute(
            "UPDATE job_videos SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), video_id),
        )

    def _set_job_status(self, job_id: str, status: str) -> None:
        self.connection.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), job_id),
        )

    def _settle_job(self, job_id: str, *, had_failures: bool = False) -> None:
        """Give the job its final status.

        'needs_attention' when something could not be finished, so the gap is
        visible rather than buried inside an otherwise successful-looking job.
        """
        status = "needs_attention" if had_failures else "completed"
        self.connection.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), utc_now(), job_id),
        )
        logger.info("Job %s finished as %s", job_id[:8], status)

    def run(self, *, once: bool = False) -> None:
        report = reconcile(self.connection, self.output_root)
        if report.changed:
            logger.info("Recovered on start-up: %s", report.summary())

        poll = max(1, self.settings.worker.poll_interval_seconds)

        while not self.stopping:
            if not self.beat():
                break

            try:
                job = self.claim_next_job()
            except sqlite3.Error as error:
                logger.error("Could not read pending work: %s", redacted_exception_text(error))
                if once:
                    break
                time.sleep(poll)
                continue

            if job is None:
                if once:
                    logger.info("Nothing waiting.")
                    break
                self._stop.wait(poll)
                continue

            try:
                self.process_job(job)
            except Exception as error:
                # One failing job must never take the worker down; the others
                # still deserve to run.
                logger.error("Job %s failed: %s", job["id"][:8], redacted_exception_text(error))
                self.connection.execute(
                    "UPDATE jobs SET status = 'needs_attention', error_message = ?,"
                    " updated_at = ? WHERE id = ?",
                    (redacted_exception_text(error), utc_now(), job["id"]),
                )

            if once:
                break


def run_worker(settings: Settings, *, once: bool = False) -> int:
    """Take ownership of the output root and run the loop. Returns an exit code."""
    root = settings.output_root
    if root is None:
        logger.error("No output folder chosen. Set one first.")
        return 1

    connection = open_database(root)
    try:
        with worker_lock(connection, root) as worker_id:
            worker = Worker(settings, connection, worker_id)

            # Only install handlers on the main thread: `start` runs the worker
            # in a background thread, where signal() raises.
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, worker.request_stop)

            logger.info("Worker ready. Closing the browser will not stop it.")
            worker.run(once=once)
        return 0
    except WorkerAlreadyRunningError as error:
        logger.error("%s", error)
        return 1
    finally:
        connection.close()
