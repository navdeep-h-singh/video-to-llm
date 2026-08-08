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
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
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
from app.pipeline.stages import (
    StageContext,
    run_assembly_stage,
    run_frames_stage,
    run_transcription_stage,
    run_visual_stage,
)
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
        self._lost_ownership = False

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
        """Refresh the claim now. False when ownership was lost.

        Kept for the loop's own check and for callers that want a synchronous
        refresh; the steady rhythm comes from :meth:`_keep_alive`.
        """
        self._last_heartbeat = time.monotonic()

        if not heartbeat(self.connection, self.output_root, self.worker_id):
            self._lose_ownership()
            return False
        return True

    def _lose_ownership(self) -> None:
        logger.error(
            "This worker no longer owns the output folder — another worker took over. "
            "Stopping so the two do not write over each other."
        )
        self._lost_ownership = True
        self._stop.set()

    def _keep_alive(self) -> None:
        """Refresh the claim on a timer, including while a stage is running.

        This used to happen only at the top of the run loop, which meant it did
        not happen at all for the entire duration of a job. That is exactly
        backwards: the stages this product is built for are the long ones — a
        fifteen-hour course, or two thousand frames through a local model — and
        those are precisely the runs that went hours without a beat.

        Two things went wrong on a real machine as a result. The interface
        reported "Stopped unexpectedly" while the worker was working perfectly
        well, which is the most misleading state it can show. And the claim went
        stale, so a second worker would have judged the first one dead and taken
        over, leaving two of them writing the same output root — the one outcome
        the claim exists to prevent.

        Its own connection, deliberately. Sharing the worker's would interleave
        this write with the explicit transactions the stages run, so a heartbeat
        could land inside a stage's transaction and be rolled back with it — or
        commit one early.
        """
        connection = open_database(self.output_root, migrate_on_open=False)
        try:
            while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                try:
                    if not heartbeat(connection, self.output_root, self.worker_id):
                        self._lose_ownership()
                        return
                except sqlite3.Error as error:
                    # A failed beat is not itself a reason to abandon a running
                    # job: the claim only goes stale after several missed ones,
                    # and the next attempt is fifteen seconds away.
                    logger.warning(
                        "Could not refresh the worker claim: %s",
                        redacted_exception_text(error),
                    )
        finally:
            connection.close()

    @contextmanager
    def keeping_the_claim_fresh(self) -> Iterator[None]:
        """Run the heartbeat for as long as the body runs."""
        thread = threading.Thread(target=self._keep_alive, name="worker-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            self._stop.set()
            # Bounded: the thread waits on the same event, so it wakes at once.
            # A worker that hung here would be worse than one that leaked a
            # daemon thread on the way out.
            thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS)

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

    def settings_for(self, job: sqlite3.Row) -> Settings:
        """The description choice this job was created with, not today's setting.

        The new-job screen offers a per-job choice — on this computer, send to a
        service, or skip — and records it. The worker used to ignore it entirely
        and read the global setting instead, which made that control decorative
        in the worst direction: a job created with "skip descriptions" would
        describe everything anyway if descriptions happened to be on, and on a
        paid provider that is somebody's money spent on work they explicitly
        declined.

        A job with nothing recorded predates the choice being honoured, so it
        falls back to the global setting rather than silently losing its
        descriptions.
        """
        try:
            recorded = (job["visual_provider"] or "").strip()
        except (IndexError, KeyError):
            return self.settings
        if not recorded:
            return self.settings

        model = (job["visual_model_id"] or "").strip() or self.settings.visual_analysis.model_id
        return replace(
            self.settings,
            visual_analysis=replace(
                self.settings.visual_analysis,
                enabled=recorded != "none",
                provider=recorded,
                model_id=model,
            ),
        )

    def process_video(self, job: sqlite3.Row, video: sqlite3.Row) -> bool:
        """Run stages 1 and 2 for one video. False when it could not finish."""
        settings = self.settings_for(job)
        interval_ms = job["frame_interval_ms"] or settings.sampling.interval_ms()
        output_dir = self.output_root / job["id"] / f"{video['id']}_v{video['version']}"

        context = StageContext(
            connection=self.connection,
            settings=settings,
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
                make_api_copies=settings.visual_analysis.enabled,
            )

            self._set_video_status(video["id"], "transcribing")
            self._set_job_status(job["id"], "transcribing")
            run_transcription_stage(context)

            had_gaps = False
            if settings.visual_analysis.enabled:
                self._set_video_status(video["id"], "analyzing")
                self._set_job_status(job["id"], "analyzing")
                had_gaps = run_visual_stage(context).has_gaps

            # Assembly runs whatever happened above: a video with gaps, or with
            # no descriptions at all, still deserves its assembled document.
            run_assembly_stage(context, display_name=video["display_name"])
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

        # Gaps are visible, not fatal — the frames, transcript, and assembled
        # document are all still worth having.
        self._set_video_status(video["id"], "completed_with_gaps" if had_gaps else "completed")
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

        Three outcomes, deliberately distinct: 'needs_attention' when something
        could not be finished at all, 'completed_with_gaps' when everything ran
        but some pictures have no description, and 'completed' when neither
        applies. Collapsing the middle case into 'completed' would hide a real
        shortfall behind a green tick.
        """
        if not had_failures:
            self._finalize(job_id)

        if had_failures:
            status = "needs_attention"
        else:
            with_gaps = self.connection.execute(
                "SELECT 1 FROM job_videos WHERE job_id = ? AND status = 'completed_with_gaps'"
                " LIMIT 1",
                (job_id,),
            ).fetchone()
            status = "completed_with_gaps" if with_gaps else "completed"
        self.connection.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), utc_now(), job_id),
        )
        logger.info("Job %s finished as %s", job_id[:8], status)
        self._ring_the_bell()

    def _ring_the_bell(self) -> None:
        """Sound the terminal bell, if the user asked for one.

        The cheapest possible way to reach somebody who started this from a
        shell and switched away: no permission, no service, no outbound call,
        and nothing to install. Written to stderr because stdout may be piped
        somewhere that a control character would corrupt, and guarded because a
        detached process has no terminal to ring.
        """
        if not self.settings.notifications.terminal_bell:
            return
        try:
            if sys.stderr.isatty():
                sys.stderr.write("\a")
                sys.stderr.flush()
        except (OSError, ValueError):
            # A closed or redirected stream is not a reason to fail a job that
            # has already finished successfully.
            pass

    def _finalize(self, job_id: str) -> None:
        """Write the job-level outputs. A failure here must not lose the videos."""
        from app.pipeline.finalize import finalize_job

        job = self.connection.execute("SELECT name FROM jobs WHERE id = ?", (job_id,)).fetchone()
        try:
            result = finalize_job(
                self.connection,
                job_id=job_id,
                job_name=job["name"] if job else job_id,
                output_root=self.output_root,
            )
        except Exception as error:
            # The per-video work is already durably on disk and is the expensive
            # part. Losing the handoff folder is recoverable; losing the job is
            # not, so this is logged rather than raised.
            logger.error(
                "Could not gather up job %s: %s", job_id[:8], redacted_exception_text(error)
            )
            return

        for warning in result.warnings:
            logger.warning("%s", warning)

    def run(self, *, once: bool = False) -> None:
        report = reconcile(self.connection, self.output_root)
        if report.changed:
            logger.info("Recovered on start-up: %s", report.summary())

        poll = max(1, self.settings.worker.poll_interval_seconds)

        with self.keeping_the_claim_fresh():
            self._loop(once=once, poll=poll)

    def _loop(self, *, once: bool, poll: int) -> None:
        while not self.stopping:
            if self._lost_ownership:
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


#: How long to wait before trying the claim again when another worker holds it.
CLAIM_RETRY_SECONDS = 20.0


def run_worker(settings: Settings, *, once: bool = False) -> int:
    """Take ownership of the output root and run the loop. Returns an exit code.

    A claim conflict is treated as temporary, not fatal, whenever the worker is
    meant to keep running. The holder may exit cleanly, crash, or go stale at any
    moment, and a worker that gave up permanently on its first refusal would
    leave the output root unattended for as long as the process stayed alive —
    which is exactly what happened here: a worker was refused at start-up while
    the previous one was genuinely alive, the previous one died five hours later,
    and nothing ever tried again. A thirteen-video job sat untouched for nine
    hours behind a claim held by a dead process.

    `--once` still returns non-zero immediately, because a one-shot run has
    nothing to wait for.
    """
    root = settings.output_root
    if root is None:
        logger.error("No output folder chosen. Set one first.")
        return 1

    connection = open_database(root)
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    on_main_thread = threading.current_thread() is threading.main_thread()

    try:
        while not stop.is_set():
            try:
                with worker_lock(connection, root) as worker_id:
                    worker = Worker(settings, connection, worker_id)

                    # Only install handlers on the main thread: `start` runs the
                    # worker in a background thread, where signal() raises.
                    if on_main_thread:
                        for sig in (signal.SIGINT, signal.SIGTERM):
                            signal.signal(sig, worker.request_stop)

                    logger.info("Worker ready. Closing the browser will not stop it.")
                    worker.run(once=once)
                return 0
            except WorkerAlreadyRunningError as error:
                if once:
                    logger.error("%s", error)
                    return 1
                logger.warning(
                    "%s Waiting %.0fs and trying again — the other worker may stop "
                    "or its claim may go stale.",
                    error,
                    CLAIM_RETRY_SECONDS,
                )
                if stop.wait(CLAIM_RETRY_SECONDS):
                    break
        return 0
    finally:
        connection.close()
