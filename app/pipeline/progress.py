"""Live progress for a stage that is still running.

``stage_runs`` has always carried ``items_total`` and ``items_done``, but both
were written at the same instant — when the stage *finished*. So a stage in
flight had ``items_total`` NULL, the interface divided by nothing, and the bar
sat at exactly 0% for the whole run before jumping to 100%. On a fifty-minute
video that is an hour of a screen that cannot distinguish "working" from "hung";
on a local description run over 1,488 frames it is most of a day of it.

Two decisions worth keeping:

**Progress is measured in the unit the user experiences, not the unit the loop
iterates.** Transcription walks speech segments, which vary from a second to
several minutes, so "12 of 40 segments" lurches and implies a finishing time it
cannot deliver. Seconds of audio covered advance evenly, and an estimate derived
from them holds up.

**A missed tick is never allowed to end the stage.** Writes are throttled,
wrapped, and best-effort: SQLite is being written concurrently by the worker's
heartbeat on another connection, and losing a progress update costs a moment of
staleness, while raising out of a transcription costs the transcription.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

from app.core.db import utc_now

logger = logging.getLogger("video_to_llm.pipeline.progress")

#: How often progress reaches the database. The interface polls every two
#: seconds, so writing faster than this buys nothing and only adds contention.
WRITE_INTERVAL_SECONDS = 2.0

#: How often a still-working line is added to the event log. The strip on screen
#: is what proves liveness *now*; this exists so the history can answer "what was
#: it doing at 3am", which the strip cannot. Ten minutes keeps an eleven-hour
#: description run to a few dozen rows — the event log already grows without
#: bound, and this must not be what makes that matter.
EVENT_INTERVAL_SECONDS = 600.0


def format_clock(seconds: float | None) -> str:
    """Seconds as a position on a video: ``4:07``, ``1:12:30``.

    The canonical one. ``app.web.status.format_duration`` delegates here so a
    stage's own event log and the screen describing that stage cannot drift into
    formatting the same number two different ways.
    """
    if not seconds or seconds <= 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class StageProgress:
    """Throttled progress for one ``stage_runs`` row.

    Constructed even when a stage cannot measure itself; in that case nothing
    calls :meth:`advance_to` and the row simply keeps a total of ``None``, which
    the interface renders as working-without-a-figure rather than as 0%.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        stage_run_id: str,
        *,
        on_event: Callable[[int, int], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        write_interval_seconds: float = WRITE_INTERVAL_SECONDS,
        event_interval_seconds: float = EVENT_INTERVAL_SECONDS,
    ) -> None:
        self._connection = connection
        self._stage_run_id = stage_run_id
        self._on_event = on_event
        self._clock = clock
        self._write_interval = write_interval_seconds
        self._event_interval = event_interval_seconds

        self._total: int | None = None
        self._done = 0
        self._last_write = 0.0
        self._last_event = clock()

    @property
    def total(self) -> int | None:
        return self._total

    @property
    def done(self) -> int:
        return self._done

    def set_total(self, total: int) -> None:
        """Publish the size of the work before any of it is done.

        Written immediately and unthrottled: until a denominator exists the
        interface has nothing to draw, and this is the moment a long stage stops
        looking like a stalled one.
        """
        self._total = max(0, int(total))
        self._write(force=True)

    def advance_to(self, done: float) -> None:
        """Record absolute progress. Throttled; safe to call in a tight loop."""
        self._done = max(0, int(done))
        if self._total is not None:
            self._done = min(self._done, self._total)
        self._write()
        self._maybe_announce()

    def finish(self) -> None:
        """Flush the last value, whatever the throttle would have said."""
        self._write(force=True)

    # ── internals ─────────────────────────────────────────────────────────

    def _write(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_write < self._write_interval:
            return
        self._last_write = now

        try:
            self._connection.execute(
                "UPDATE stage_runs SET items_total = ?, items_done = ?, updated_at = ?"
                " WHERE id = ?",
                (self._total, self._done, utc_now(), self._stage_run_id),
            )
        except sqlite3.Error as error:
            # Best-effort by design. The stage is the valuable thing; a progress
            # tick is not worth losing it over, and the next tick is seconds away.
            logger.debug("Could not record progress: %s", error)

    def _maybe_announce(self) -> None:
        if self._on_event is None or not self._total:
            return
        now = self._clock()
        if now - self._last_event < self._event_interval:
            return
        self._last_event = now
        try:
            self._on_event(self._done, self._total)
        except Exception as error:  # pragma: no cover - defensive
            logger.debug("Could not record a progress event: %s", error)
