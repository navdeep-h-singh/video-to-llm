"""Whether the running process still matches the code on disk.

Templates are read from disk on every request. Route code is imported once and
lives in memory. So editing the application while a server is running produces a
half-updated program: new screens served by old routes.

That is not a hypothetical. It has now happened three times in one session, and
every time it surfaced as something that looked like an ordinary bug — a screen
where every service lost its name, and a button that reported "Not Found"
because the route it called had been added after the server started. Each cost
real time to diagnose, and the diagnosis was always the same sentence.

An application that can detect this should say so rather than leave the user to
work it out from the symptoms.
"""

from __future__ import annotations

import time
from pathlib import Path

#: How often the check may touch the filesystem. It walks a few dozen files, so
#: the cost is small, but not small enough to repeat for every image on a page.
RECHECK_INTERVAL_SECONDS = 2.0

_APP_ROOT = Path(__file__).resolve().parent.parent

#: None rather than 0.0: "never checked" and "checked at time zero" must not be
#: the same state, or the very first check can be throttled away and the answer
#: defaults to "nothing changed" — the wrong direction to fail in for a warning.
_last_checked: float | None = None
_last_result = 0.0


def source_fingerprint() -> float:
    """The newest modification time across the Python that defines behaviour.

    Only ``.py`` files. Templates and CSS are re-read per request and are
    *supposed* to change under a running server — flagging those would report a
    restart that changes nothing, and a warning that cries wolf is worse than no
    warning.
    """
    newest = 0.0
    for path in _APP_ROOT.rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def current_fingerprint(*, now: float | None = None) -> float:
    """The fingerprint, recomputed at most every few seconds."""
    global _last_checked, _last_result

    moment = time.monotonic() if now is None else now
    if _last_checked is None or moment - _last_checked >= RECHECK_INTERVAL_SECONDS:
        _last_checked = moment
        _last_result = source_fingerprint()
    return _last_result


def reset_cache() -> None:
    """Forget the throttle. For tests, which move faster than the interval."""
    global _last_checked, _last_result
    _last_checked = None
    _last_result = 0.0
