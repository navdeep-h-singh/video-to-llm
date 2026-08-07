"""FastAPI application.

The screens are implemented in Phase 7 against the supplied design. This module
establishes the application object and the localhost boundary so the CLI's
`start-ui` path is real from the beginning.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import Settings, assert_loopback


def create_app(settings: Settings) -> FastAPI:
    # Asserted at construction rather than trusted: the boundary is the one
    # property of this application that must never quietly change.
    assert_loopback(settings.host, context="server bind host")

    app = FastAPI(
        title="Video to LLM",
        description="Runs only on this computer.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "bound_to": settings.host,
                "output_root_set": settings.output_root is not None,
            }
        )

    return app
