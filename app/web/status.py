"""The status vocabulary the interface shows.

Every state is presented as **text plus an icon shape plus a colour**, never
colour alone. Colour-only status is unreadable to a large minority of users and
invisible in a screenshot pasted into a monochrome document, so the word is
always the primary signal and the shape distinguishes states that share a hue.

The wording is deliberately plain. A first-time user should be able to read
every one of these without knowing what an API is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusPresentation:
    key: str
    label: str
    css_class: str
    #: Described for screen readers, so the shape is not the only cue.
    shape: str

    @property
    def aria_label(self) -> str:
        return f"{self.label} ({self.shape})"


#: Job and video states, in the order the specification lists them.
STATUSES: dict[str, StatusPresentation] = {
    "draft": StatusPresentation("draft", "Draft", "status-draft", "hollow square"),
    "ready": StatusPresentation("ready", "Ready to start", "status-ready", "hollow square"),
    "preparing": StatusPresentation("preparing", "Preparing", "status-running", "filled square"),
    "transcribing": StatusPresentation(
        "transcribing", "Writing the transcript", "status-running", "filled square"
    ),
    "analyzing": StatusPresentation(
        "analyzing", "Describing pictures", "status-running", "filled square"
    ),
    "waiting_retry": StatusPresentation(
        "waiting_retry", "Waiting to try again", "status-waiting", "turned square"
    ),
    "paused": StatusPresentation("paused", "Paused", "status-paused", "grey square"),
    "needs_attention": StatusPresentation(
        "needs_attention", "Needs you", "status-attention", "turned square"
    ),
    "completed": StatusPresentation("completed", "Finished", "status-done", "filled square"),
    "completed_with_gaps": StatusPresentation(
        "completed_with_gaps", "Finished, with gaps", "status-gaps", "outlined turned square"
    ),
    "cancelled": StatusPresentation("cancelled", "Cancelled", "status-cancelled", "faded square"),
    # Video-only states.
    "pending": StatusPresentation("pending", "Waiting", "status-ready", "hollow square"),
    "skipped": StatusPresentation("skipped", "Skipped", "status-cancelled", "faded square"),
}

#: A state we do not recognise is shown as itself rather than hidden. Silently
#: rendering an unknown status as "Finished" would be a lie.
UNKNOWN = StatusPresentation("unknown", "Unknown state", "status-draft", "hollow square")


def present(status: str | None) -> StatusPresentation:
    if not status:
        return UNKNOWN
    return STATUSES.get(status, StatusPresentation(status, status, "status-draft", "square"))


def is_finished(status: str | None) -> bool:
    return status in {"completed", "completed_with_gaps", "cancelled"}


def is_running(status: str | None) -> bool:
    return status in {"preparing", "transcribing", "analyzing"}


def format_duration(seconds: float | None) -> str:
    # One implementation, in the pipeline, so a stage's own progress events and
    # the screen reporting that stage cannot format the same number differently.
    from app.pipeline.progress import format_clock

    return format_clock(seconds)


def format_relative(timestamp: str | None) -> str:
    """A human-scale 'when', not a machine timestamp.

    "9 seconds ago" tells the reader whether the job is alive; an ISO string
    makes them do the arithmetic themselves.
    """
    if not timestamp:
        return "—"

    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    delta = (datetime.now(UTC) - parsed).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 45:
        return f"{int(delta)} seconds ago"
    if delta < 90:
        return "a minute ago"
    if delta < 3600:
        return f"{int(delta // 60)} minutes ago"
    if delta < 7200:
        return "an hour ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hours ago"
    if delta < 172800:
        return "yesterday"
    return f"{int(delta // 86400)} days ago"


def format_bytes(size: float | None) -> str:
    if not size:
        return "—"
    for unit, threshold in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{int(size)} bytes"


def format_elapsed(started_at: str | None, completed_at: str | None = None) -> str:
    """How long a job took, or has been going.

    Previously derivable only by subtracting two timestamps in the event log.
    "2,307 pictures in 19 seconds" is one of the most persuasive facts this
    application produces and it was hiding it from its own user.
    """
    if not started_at:
        return "—"

    from datetime import UTC, datetime

    def parse(value: str) -> datetime | None:
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment

    start = parse(started_at)
    if start is None:
        return "—"
    end = parse(completed_at) if completed_at else datetime.now(UTC)
    if end is None:
        end = datetime.now(UTC)

    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def elapsed_seconds(started_at: str | None) -> float | None:
    """Seconds since *started_at*, or None if it cannot be read.

    None rather than 0 for an unparseable timestamp: a caller dividing by this
    must be able to tell "no time has passed" from "I do not know", and an
    estimate built on a silent zero would be confidently wrong.
    """
    if not started_at:
        return None

    from datetime import UTC, datetime

    try:
        moment = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    start = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    return max(0.0, (datetime.now(UTC) - start).total_seconds())


def format_span(seconds: float | None) -> str:
    """A rough duration in words: "5 minutes", "about 2 hours".

    Deliberately coarser than :func:`format_elapsed`. This renders a *prediction*
    and the precision should admit that — "about 3 hours 47 minutes left" claims
    an accuracy no estimate from a running average has.
    """
    if not seconds or seconds <= 0:
        return "a moment"
    total = int(seconds)
    if total < 90:
        return "under a minute"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes / 60
    if hours < 2:
        return "about an hour"
    if hours < 10:
        rounded = round(hours * 2) / 2
        whole = int(rounded)
        return f"about {whole} hours" if rounded == whole else f"about {whole}½ hours"
    return f"about {round(hours)} hours"


def format_moment(timestamp: str | None) -> str:
    """A log timestamp with its date, so 'yesterday' is expressible.

    The event log showed times alone, which is self-defeating in a tool designed
    to run for hours and survive overnight suspensions.
    """
    if not timestamp:
        return "—"

    from datetime import UTC, datetime

    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return timestamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    local = moment.astimezone()
    today = datetime.now().astimezone().date()
    delta = (today - local.date()).days

    if delta == 0:
        return local.strftime("%H:%M:%S")
    if delta == 1:
        return f"yesterday {local.strftime('%H:%M')}"
    return local.strftime("%-d %b %H:%M")
