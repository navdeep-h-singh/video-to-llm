"""The address the test client speaks to.

The application refuses a ``Host`` header that does not name this machine, so
that a hostname rebound to 127.0.0.1 cannot read a response that the browser
would then treat as same-origin. Starlette's ``TestClient`` defaults to
``http://testserver``, which is exactly the kind of name that check exists to
turn away.

Every client in the suite therefore speaks to a loopback address, the same way a
browser does. Kept in one place so a future test cannot quietly opt out of the
boundary by forgetting the argument.
"""

from __future__ import annotations

LOOPBACK_BASE_URL = "http://127.0.0.1:8712"
