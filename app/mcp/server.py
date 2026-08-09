"""Letting an agent use this tool without a browser.

Every other video tool in this category re-downloads and re-processes the video
for each question. This one does not, and that is the whole reason to expose it
over MCP: an agent that processes a forty-hour course once can ask a thousand
questions of it afterwards for nothing. `process_video` is the expensive call
and it is idempotent — asked for a video it has already done, it returns the
existing job rather than doing the work again.

Four tools, deliberately few:

* `process_video`  — do the work, or report that it is already done
* `list_videos`    — what has been processed already
* `get_transcript` — the assembled document, whole or in a time range
* `get_segment`    — what happened at a timestamp, and the frame from it

Nothing here reaches the network on its own. Descriptions stay off unless the
caller asks for them, exactly as they do everywhere else, and asking for a cloud
service over MCP is refused: an agent should not be able to spend the user's
money through a tool call the user never saw. Local descriptions are allowed
because they cost nothing but time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import Settings, load_settings
from app.core.db import database_path, open_database
from app.core.logging import get_logger

logger = get_logger(__name__)

INSTALL_HINT = (
    "The MCP server needs the `mcp` package.\n"
    "  uv pip install 'video-to-llm[mcp]'\n"
    "or\n"
    "  pip install 'video-to-llm[mcp]'"
)

#: Descriptions an agent may ask for. A paid service is not on this list on
#: purpose — see the module docstring. The user can still choose one in the
#: interface, where they can see the estimate and the cap before agreeing.
AGENT_DESCRIBE = {"none": "none", "local": "ollama_local"}


def _settings(output_root: str | None = None) -> Settings:
    from app.core.config import default_output_root

    settings = load_settings()
    if output_root:
        return settings.with_output_root(Path(output_root).expanduser())
    if settings.output_root is None:
        return settings.with_output_root(default_output_root())
    return settings


def _existing_job(settings: Settings, paths: list[Path]) -> dict[str, Any] | None:
    """A finished job over exactly these source files, if there is one.

    Matched on the recorded source paths rather than on the job name, because
    the name is the caller's label and the paths are the identity. This is what
    makes `process_video` cheap to call twice.
    """
    root = settings.output_root
    if root is None or not database_path(Path(root)).exists():
        return None

    wanted = sorted(str(p) for p in paths)
    connection = open_database(Path(root), migrate_on_open=False)
    try:
        rows = connection.execute(
            "SELECT id, name, status FROM jobs WHERE status IN"
            " ('completed', 'completed_with_gaps') ORDER BY created_at DESC"
        ).fetchall()
        for row in rows:
            sources = sorted(
                str(r["source_path"])
                for r in connection.execute(
                    "SELECT source_path FROM job_videos WHERE job_id = ? AND is_active_version = 1",
                    (row["id"],),
                ).fetchall()
            )
            if sources == wanted:
                from app.services.headless import documents_for

                documents = documents_for(connection, Path(root), str(row["id"]))
                if documents:
                    return {
                        "job": row["name"],
                        "job_id": row["id"],
                        "status": row["status"],
                        "documents": [str(d) for d in documents],
                        "reused": True,
                    }
        return None
    finally:
        connection.close()


# ── Tool implementations, kept free of any MCP types ──────────────────────
#
# Plain functions taking and returning plain data, so the tests exercise the
# behaviour without the transport and without the optional dependency.


def tool_process_video(
    paths: list[str],
    *,
    name: str | None = None,
    interval_seconds: float | None = None,
    describe: str = "none",
    output_root: str | None = None,
) -> dict[str, Any]:
    from app.services.headless import process_videos

    if describe not in AGENT_DESCRIBE:
        return {
            "error": (
                f"describe must be one of {sorted(AGENT_DESCRIBE)}. Sending pictures to a "
                "paid service is chosen by the person in the interface, where the estimate "
                "and the spending cap are visible, not through a tool call."
            )
        }

    settings = _settings(output_root)
    resolved = [Path(p).expanduser() for p in paths]

    missing = [str(p) for p in resolved if not p.exists()]
    if missing:
        return {"error": f"No such file: {', '.join(missing)}"}

    already = _existing_job(settings, resolved)
    if already is not None:
        return already

    provider = AGENT_DESCRIBE[describe]
    result = process_videos(
        settings,
        paths=resolved,
        name=name,
        interval_ms=round(interval_seconds * 1000) if interval_seconds else None,
        provider=provider,
        model_id=settings.visual_analysis.model_for(provider) if provider != "none" else "",
    )

    if result.job_id is None:
        return {"error": "; ".join(result.problems) or "The job could not be created."}

    return {
        "job": name or (result.documents[0].parent.name if result.documents else ""),
        "job_id": result.job_id,
        "status": result.status,
        "documents": [str(d) for d in result.documents],
        "problems": result.problems,
        "reused": False,
    }


def tool_list_videos(output_root: str | None = None) -> dict[str, Any]:
    settings = _settings(output_root)
    root = settings.output_root
    if root is None or not database_path(Path(root)).exists():
        return {"videos": []}

    connection = open_database(Path(root), migrate_on_open=False)
    try:
        rows = connection.execute(
            "SELECT j.id, j.name, j.status, v.display_name, v.duration_seconds"
            " FROM jobs j JOIN job_videos v ON v.job_id = j.id"
            " WHERE v.is_active_version = 1 ORDER BY j.created_at DESC LIMIT 200"
        ).fetchall()
        return {
            "videos": [
                {
                    "job": row["name"],
                    "job_id": row["id"],
                    "status": row["status"],
                    "file": row["display_name"],
                    "duration_seconds": row["duration_seconds"],
                }
                for row in rows
            ]
        }
    finally:
        connection.close()


def tool_get_transcript(
    job: str,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    output_root: str | None = None,
) -> dict[str, Any]:
    from app.services.citation import CitationError, find_job, video_dirs
    from app.services.export import ExportError, read_timeline

    settings = _settings(output_root)
    root = settings.output_root
    if root is None or not database_path(Path(root)).exists():
        return {"error": "Nothing has been processed yet."}

    connection = open_database(Path(root), migrate_on_open=False)
    try:
        row = find_job(connection, job)
        folders = video_dirs(connection, Path(root), row)
    except CitationError as error:
        return {"error": str(error)}
    finally:
        connection.close()

    if not folders:
        return {"error": f"'{job}' has no output on disk yet."}

    out: list[dict[str, Any]] = []
    for folder in folders:
        try:
            entries = read_timeline(folder)
        except ExportError as error:
            return {"error": str(error)}
        for entry in entries:
            if start_seconds is not None and entry.seconds < start_seconds:
                continue
            if end_seconds is not None and entry.seconds > end_seconds:
                continue
            record: dict[str, Any] = {
                "t": round(entry.seconds, 3),
                "kind": entry.kind,
                "text": entry.text,
            }
            if entry.confidence:
                record["confidence"] = entry.confidence
            out.append(record)

    return {"job": str(row["name"]), "entry_count": len(out), "entries": out}


def tool_get_segment(
    job: str, timestamp: str, *, window: float = 15.0, output_root: str | None = None
) -> dict[str, Any]:
    from app.services.citation import CitationError, format_citation, resolve_citation

    try:
        citation = resolve_citation(_settings(output_root), job, timestamp, window=window)
    except CitationError as error:
        return {"error": str(error)}

    return {
        "job": citation.job_name,
        "video": citation.video_name,
        "seconds": citation.seconds,
        # The frame path is the point of this tool: an agent that can name the
        # picture behind a claim can hand it back to the user to check.
        "frame_path": str(citation.frame_path) if citation.frame_path else None,
        "rendered": format_citation(citation),
    }


TOOLS = {
    "process_video": tool_process_video,
    "list_videos": tool_list_videos,
    "get_transcript": tool_get_transcript,
    "get_segment": tool_get_segment,
}


# ── Transport ─────────────────────────────────────────────────────────────


def build_server() -> Any:
    """Wire the plain functions above onto an MCP server.

    Imported lazily so that `video-to-llm` with no `[mcp]` extra installed still
    starts, and says what to install rather than raising an ImportError.
    """
    # The SDK renamed its high-level server between major versions: `FastMCP`
    # in 1.x, `MCPServer` in 2.x. Both are supported rather than pinning one,
    # because an agent host usually already has the SDK installed at whatever
    # version it wants, and refusing to load next to it would make this server
    # uninstallable for exactly the people it is for.
    server_class: Any = None
    try:
        from mcp.server.mcpserver import MCPServer

        server_class = MCPServer
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP

            server_class = FastMCP
        except ImportError as error:  # pragma: no cover - depends on the extra
            raise RuntimeError(INSTALL_HINT) from error

    server = server_class("video-to-llm")

    @server.tool()
    def process_video(
        paths: list[str],
        name: str | None = None,
        interval_seconds: float | None = None,
        describe: str = "none",
    ) -> str:
        """Process local video files into one timestamped, citable document.

        Runs entirely on this computer. Returns the existing result if these
        files have already been processed, so calling it twice is cheap. Set
        describe to "local" to also describe the pictures with a local vision
        model — slow, roughly 30 seconds per picture.
        """
        return json.dumps(
            tool_process_video(
                paths, name=name, interval_seconds=interval_seconds, describe=describe
            ),
            indent=2,
        )

    @server.tool()
    def list_videos() -> str:
        """List videos that have already been processed and can be asked about."""
        return json.dumps(tool_list_videos(), indent=2)

    @server.tool()
    def get_transcript(
        job: str, start_seconds: float | None = None, end_seconds: float | None = None
    ) -> str:
        """Read an already-processed video's timeline, whole or between two times."""
        return json.dumps(
            tool_get_transcript(job, start_seconds=start_seconds, end_seconds=end_seconds),
            indent=2,
        )

    @server.tool()
    def get_segment(job: str, timestamp: str, window: float = 15.0) -> str:
        """What happened at a timestamp, and the path to the picture from it.

        Use this to check a claim: every line in the document carries a time,
        and this resolves one back to the frame it came from.
        """
        return json.dumps(tool_get_segment(job, timestamp, window=window), indent=2)

    return server


def run() -> int:
    try:
        server = build_server()
    except RuntimeError as error:
        print(str(error))
        return 1
    server.run()
    return 0
