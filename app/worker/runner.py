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


#: Statuses that mean the user asked this job to stop. The worker re-reads the
#: job row to find them, because they are written by the interface on a
#: different connection while a stage is already running.
HALTING_JOB_STATES = ("paused", "cancelled")

#: The three status writes the worker makes while a job runs, each refusing to
#: move a job or video the user has stopped. Written out rather than built from
#: the tuple above so the queries stay plain literals; `test_worker_pause.py`
#: asserts the two agree, which is what keeps them from drifting apart.
_HALTED_SQL = "('paused', 'cancelled')"
_SET_VIDEO_STATUS = (
    "UPDATE job_videos SET status = ?, updated_at = ? WHERE id = ?"
    " AND status NOT IN ('paused', 'cancelled')"
)
_SET_JOB_STATUS = (
    "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?"
    " AND status NOT IN ('paused', 'cancelled')"
)
_START_JOB = (
    "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?"
    " WHERE id = ? AND status NOT IN ('paused', 'cancelled')"
)


class JobHalted(Exception):
    """Raised inside a job when the user paused or cancelled it.

    Not a failure: the work already done is kept and the job keeps the status
    the user asked for, rather than being settled as completed or marked as
    needing attention.
    """

    def __init__(self, status: str):
        super().__init__(f"job {status}")
        self.status = status


class JobYielded(Exception):
    """Raised inside a job that should go back in the queue rather than finish.

    Also not a failure, and distinct from a halt: this job still wants to run.
    It goes back to 'ready' and keeps its place in the queue, and resumes from
    the same point whenever the worker reaches it again — which the stage
    records and the completed batches on disk already make safe.

    ``taken_over_by`` names the job the user moved ahead. None means the stage
    put itself down and by the time it did, whatever asked for that had been
    withdrawn — the video is still unfinished either way, so it goes round
    again rather than being assembled from a partial description set.
    """

    def __init__(self, taken_over_by: str | None = None):
        super().__init__(
            f"stepped aside for {taken_over_by}" if taken_over_by else "returned to the queue"
        )
        self.taken_over_by = taken_over_by


def _step_aside_message(taken_over_by: str | None) -> str:
    """What the job's own event log says about going back in the queue."""
    if taken_over_by:
        return (
            f"Waiting while '{taken_over_by}' runs — you moved it ahead of this one. "
            "Everything finished so far is kept, and this job picks up where it stopped."
        )
    return (
        "Stopped partway and went back in the queue. Everything finished so far is kept, "
        "and this job picks up where it stopped."
    )


def _job_folder(job: sqlite3.Row) -> str:
    """The folder a job's output belongs in, by name where one was recorded."""
    try:
        recorded = job["output_dirname"]
    except (IndexError, KeyError):
        return str(job["id"])
    return str(recorded) if recorded else str(job["id"])


class Worker:
    def __init__(
        self,
        settings: Settings,
        connection: sqlite3.Connection,
        worker_id: str,
        *,
        only_job_id: str | None = None,
    ):
        self.settings = settings
        self.connection = connection
        self.worker_id = worker_id
        self.only_job_id = only_job_id
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
        """Return the job that should run now, if any.

        Highest priority first, oldest first within a priority. Every job starts
        at priority 0, so a queue nobody has reordered runs in exactly the order
        it was created, as it always did.

        When the worker is scoped to one job it considers only that job. A
        headless `process` run creates a job and then turns the loop once; if
        that turn took the front of the queue instead, a video queued earlier in
        the interface would run in its place — and where that job names a cloud
        service, the command would spend money on work the user did not just ask
        for. A one-shot run does the thing it was asked to do, or nothing.
        """
        if self.only_job_id is not None:
            return self.connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND status = 'ready'",
                (self.only_job_id,),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM jobs WHERE status = 'ready'"
            " ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()

    def outranked_by(self, job: sqlite3.Row) -> sqlite3.Row | None:
        """A waiting job the user has put ahead of this one, if there is one.

        Strictly higher priority, never equal: two jobs at the same priority
        must not be able to take turns pushing each other aside, and priority
        only ever changes because somebody asked for it.

        A one-shot run never steps aside. `video-to-llm process` was told to do
        one particular thing, and abandoning it halfway to run something queued
        in the interface is not a service to anybody standing at a terminal.
        """
        if self.only_job_id is not None:
            return None
        try:
            return self.connection.execute(
                "SELECT id, name FROM jobs WHERE status = 'ready' AND priority > ?"
                " AND id <> ? ORDER BY priority DESC, created_at ASC LIMIT 1",
                (int(job["priority"]), job["id"]),
            ).fetchone()
        except (sqlite3.Error, IndexError, KeyError) as error:
            # Same reading as a failed halt check: a read that did not happen is
            # not evidence that anything is waiting.
            logger.warning(
                "Could not check the queue: %s",
                redacted_exception_text(error),
            )
            return None

    def halt_requested(self, job_id: str) -> str | None:
        """The status this job was asked to stop at, or None to keep going.

        Re-read from the database every time, deliberately. The interface writes
        'paused' from its own connection while a stage is mid-flight, and the
        row the worker claimed minutes or hours ago cannot know about it. This
        is the read that used to be missing: without it a pause wrote a status
        nobody consulted, the interface reported Paused over work that was still
        running, and on a paid provider frames kept being sent — and charged for
        — after the user had asked it to stop.
        """
        if self.stopping:
            return "stopping"
        try:
            row = self.connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        except sqlite3.Error as error:
            # A read that fails is not evidence of a stop request. Carrying on
            # is the safe reading: the next check is seconds away.
            logger.warning(
                "Could not check whether job was stopped: %s",
                redacted_exception_text(error),
            )
            return None
        if row is None:
            # Deleted mid-flight. Stopping is how the worker finds out politely,
            # rather than by crashing into a foreign-key failure on its next write.
            return "cancelled"
        status = str(row["status"])
        return status if status in HALTING_JOB_STATES else None

    def _checkpoint(self, job: sqlite3.Row) -> None:
        """The one place the worker asks whether it should still be doing this.

        Halt before yield, deliberately. If the user paused this job in the same
        moment somebody moved another one ahead of it, the pause is the answer:
        one is an instruction about this job and the other is a preference about
        the queue.
        """
        halt = self.halt_requested(job["id"])
        if halt is not None:
            raise JobHalted(halt)

        ahead = self.outranked_by(job)
        if ahead is not None:
            raise JobYielded(str(ahead["name"]))

    def _should_stop_for(self, job: sqlite3.Row) -> bool:
        """Whether a long stage should put itself down at the next safe point."""
        if self.halt_requested(job["id"]) is not None:
            return True
        return self.outranked_by(job) is not None

    def process_job(self, job: sqlite3.Row) -> None:
        """Run every stage for every video in one job, in confirmed order."""
        logger.info("Picked up job %s (%s)", job["id"][:8], job["name"])
        self._set_job_status(job["id"], "preparing", starting=True)

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
        try:
            for video in videos:
                self._checkpoint(job)
                if not self.process_video(job, video):
                    failed += 1
        except JobHalted as halt:
            # Left exactly as the user asked. No settling: a paused job is not a
            # finished one, and overwriting its status here is what made the
            # pause evaporate before.
            logger.info(
                "Job %s stopped as requested (%s). Finished work is kept.",
                job["id"][:8],
                halt.status,
            )
            return
        except JobYielded as yielded:
            self._step_aside(job, yielded)
            return

        self._settle_job(job["id"], had_failures=failed > 0)

    def _step_aside(self, job: sqlite3.Row, yielded: JobYielded) -> None:
        """Put a job back in the queue rather than finishing it now.

        Back to 'ready', not 'paused': nobody stopped this job, and a status the
        user did not ask for is exactly the kind of lie the pause fix existed to
        remove. The video in flight goes back to 'pending' so the next pass
        picks it up — every stage it already finished is recorded, and every
        description batch already paid for is on disk, so resuming costs nothing
        beyond the part that was interrupted.
        """
        logger.info(
            "Job %s returned to the queue (%s). It resumes where it stopped.",
            job["id"][:8],
            yielded,
        )
        self.connection.execute(
            "UPDATE job_videos SET status = 'pending', updated_at = ? WHERE job_id = ?"
            " AND status IN ('preparing', 'transcribing', 'analyzing')",
            (utc_now(), job["id"]),
        )
        self._set_job_status(job["id"], "ready")
        self.connection.execute(
            "INSERT INTO events (job_id, level, kind, message, created_at) VALUES (?,?,?,?,?)",
            (
                job["id"],
                "info",
                "queue",
                _step_aside_message(yielded.taken_over_by),
                utc_now(),
            ),
        )

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

        # The job's own model wins, filed under the job's own provider. Writing
        # it into the map rather than over a single shared field is what keeps a
        # job that chose Claude from being run against whatever model the global
        # settings happen to name today.
        visual = self.settings.visual_analysis
        model = (job["visual_model_id"] or "").strip() or visual.model_for(recorded)
        return replace(
            self.settings,
            visual_analysis=replace(
                visual,
                enabled=recorded != "none",
                provider=recorded,
                models={**visual.models, recorded: model},
            ),
        )

    def process_video(self, job: sqlite3.Row, video: sqlite3.Row) -> bool:
        """Run stages 1 and 2 for one video. False when it could not finish."""
        settings = self.settings_for(job)
        interval_ms = job["frame_interval_ms"] or settings.sampling.interval_ms()
        # NULL for every job created before folders were named, which is why
        # the fallback is the identifier rather than a slug computed now:
        # recomputing would point at a folder that does not hold the job's
        # existing output.
        folder = _job_folder(job)
        output_dir = self.output_root / folder / f"{video['id']}_v{video['version']}"

        context = StageContext(
            connection=self.connection,
            settings=settings,
            job_id=job["id"],
            job_video_id=video["id"],
            source_path=Path(video["source_path"]),
            output_dir=output_dir,
            interval_ms=interval_ms,
            # Consulted between description batches, which is the only place a
            # stage runs long enough — and spends enough — for the answer to
            # matter within a single video. Covers both reasons to put the work
            # down: the user stopped this job, or moved another one ahead of it.
            should_stop=lambda: self._should_stop_for(job),
        )

        try:
            self._checkpoint(job)
            self._set_video_status(video["id"], "preparing")
            self._set_job_status(job["id"], "preparing")
            run_frames_stage(
                context,
                make_api_copies=settings.visual_analysis.enabled,
            )

            self._checkpoint(job)
            self._set_video_status(video["id"], "transcribing")
            self._set_job_status(job["id"], "transcribing")
            run_transcription_stage(context)

            had_gaps = False
            if settings.visual_analysis.enabled:
                self._checkpoint(job)
                self._set_video_status(video["id"], "analyzing")
                self._set_job_status(job["id"], "analyzing")
                result = run_visual_stage(context)
                # The stage stops itself between batches when asked. Coming back
                # here without finishing means the video is not finished either,
                # so it must not fall through to assembly and a completed status.
                # Which of the two reasons it was is decided here, by asking
                # again — the stage only ever knew that it should stop.
                if result.stopped_at_index is not None and not result.stopped_on_budget:
                    self._checkpoint(job)
                    # Neither still applies: whatever asked for the stop was
                    # withdrawn while the batch in flight finished.
                    raise JobYielded()
                had_gaps = result.has_gaps

            self._checkpoint(job)
            # Assembly runs whatever happened above: a video with gaps, or with
            # no descriptions at all, still deserves its assembled document.
            run_assembly_stage(context, display_name=video["display_name"])
        except (JobHalted, JobYielded):
            raise
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
        # Guarded for the same reason as the job status below: a video the user
        # paused must not be dragged back into a running state by the stage that
        # was already in flight when they asked.
        self.connection.execute(
            _SET_VIDEO_STATUS,
            (status, utc_now(), video_id),
        )

    def _set_job_status(self, job_id: str, status: str, *, starting: bool = False) -> None:
        """Move a job to a running status — unless the user asked it to stop.

        The guard is the whole point. Without it the next stage's status write
        landed straight over 'paused' (this was `UPDATE jobs SET status = ?`
        with no condition), so a pause survived only until the job reached its
        next stage, and the interface then showed a running status for a job the
        user believed they had stopped.
        """
        if starting:
            self.connection.execute(
                _START_JOB,
                (status, utc_now(), utc_now(), job_id),
            )
            return
        self.connection.execute(
            _SET_JOB_STATUS,
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


def run_worker(settings: Settings, *, once: bool = False, only_job_id: str | None = None) -> int:
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
                    worker = Worker(settings, connection, worker_id, only_job_id=only_job_id)

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
