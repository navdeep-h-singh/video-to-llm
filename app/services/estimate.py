"""How long a stage is likely to take, learned from what it has already done.

Every figure here comes from finished ``stage_runs`` on *this* machine. Nothing
is hardcoded, because the number that matters depends on the model, the file,
the processor, and whatever else the computer is busy with — a constant baked in
at build time would be wrong for everyone except the machine it was measured on.

When there is no history there is no estimate. Saying "we do not know yet" is
the honest answer and matches invariant 6: ``Unknown`` is preserved, never
guessed. An invented duration is worse than none, because someone plans an
afternoon around it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

#: Runs needed before a rate is offered. One sample can be a cold model load, a
#: laptop waking up, or a file that happened to be cached — none of which
#: predicts the next run.
MIN_SAMPLES = 2


@dataclass(frozen=True, slots=True)
class Estimate:
    """A predicted duration, or an honest absence of one."""

    seconds: float | None
    samples: int

    @property
    def known(self) -> bool:
        return self.seconds is not None


def measured_rate(
    connection: sqlite3.Connection, stage: str, *, model_id: str | None = None
) -> tuple[float | None, int]:
    """Median seconds per item for a stage, or None if it has never finished.

    The median rather than the mean: one run interrupted by a laptop going to
    sleep, or one that hit a cold model load, would drag an average far enough
    to make every later estimate wrong in the same direction.
    """
    if model_id:
        rows = connection.execute(
            "SELECT started_at, finished_at, items_done FROM stage_runs"
            " WHERE stage = ? AND model_id = ? AND status IN ('completed', 'completed_with_gaps')"
            " AND finished_at IS NOT NULL AND items_done > 0"
            " ORDER BY id DESC LIMIT 20",
            (stage, model_id),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT started_at, finished_at, items_done FROM stage_runs"
            " WHERE stage = ? AND status IN ('completed', 'completed_with_gaps')"
            " AND finished_at IS NOT NULL AND items_done > 0"
            " ORDER BY id DESC LIMIT 20",
            (stage,),
        ).fetchall()

    rates: list[float] = []
    for row in rows:
        try:
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["finished_at"])
        except (TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        seconds = (end - start).total_seconds()
        items = row["items_done"] or 0
        if seconds <= 0 or items <= 0:
            continue
        rates.append(seconds / items)

    if len(rates) < MIN_SAMPLES:
        return None, len(rates)

    rates.sort()
    middle = len(rates) // 2
    if len(rates) % 2:
        return rates[middle], len(rates)
    return (rates[middle - 1] + rates[middle]) / 2, len(rates)


def measured_rate_per_video_second(
    connection: sqlite3.Connection, stage: str
) -> tuple[float | None, int]:
    """Median wall-clock seconds spent per second of video, for a stage.

    Needed because `measured_rate` divides by `items_done`, and for transcription
    that unit is *speech segments* — a count nobody can know before transcribing.
    Estimating a new video from a segment rate would be the same mistake the
    progress bar already avoids: segments run from a second to several minutes,
    so any prediction built on them is wrong.

    Video duration is knowable in advance, recorded on `job_videos`, and stable
    across files. That makes it the only honest basis for a transcription
    estimate.
    """
    rows = connection.execute(
        "SELECT s.started_at, s.finished_at, v.duration_seconds"
        " FROM stage_runs s JOIN job_videos v ON v.id = s.job_video_id"
        " WHERE s.stage = ? AND s.status IN ('completed', 'completed_with_gaps')"
        " AND s.finished_at IS NOT NULL AND v.duration_seconds > 0"
        " ORDER BY s.id DESC LIMIT 20",
        (stage,),
    ).fetchall()

    rates: list[float] = []
    for row in rows:
        try:
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["finished_at"])
        except (TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)

        seconds = (end - start).total_seconds()
        duration = float(row["duration_seconds"] or 0)
        if seconds <= 0 or duration <= 0:
            continue
        rates.append(seconds / duration)

    if len(rates) < MIN_SAMPLES:
        return None, len(rates)

    rates.sort()
    middle = len(rates) // 2
    if len(rates) % 2:
        return rates[middle], len(rates)
    return (rates[middle - 1] + rates[middle]) / 2, len(rates)


def estimate_by_video_seconds(
    connection: sqlite3.Connection, stage: str, video_seconds: float
) -> Estimate:
    """How long *stage* should take on this much video, if we can tell."""
    if video_seconds <= 0:
        return Estimate(seconds=None, samples=0)

    rate, samples = measured_rate_per_video_second(connection, stage)
    if rate is None:
        return Estimate(seconds=None, samples=samples)
    return Estimate(seconds=rate * video_seconds, samples=samples)


def estimate_stage(
    connection: sqlite3.Connection, stage: str, items: int, *, model_id: str | None = None
) -> Estimate:
    """How long *items* of *stage* should take here, if we can tell."""
    if items <= 0:
        return Estimate(seconds=None, samples=0)

    rate, samples = measured_rate(connection, stage, model_id=model_id)
    if rate is None:
        return Estimate(seconds=None, samples=samples)
    return Estimate(seconds=rate * items, samples=samples)
