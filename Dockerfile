# A container for the pipeline, not for the interface.
#
# The interface binds 127.0.0.1 and that is asserted at application construction
# — it is the guarantee the whole product rests on, not a default. Inside a
# container that means the container's own loopback, which is unreachable from
# the host no matter what is published with -p. Rather than weaken the binding
# to make a demo work, this image ships the command line: `process`, `show`,
# `export`, `doctor`, and the MCP server. Anyone who wants the interface runs it
# on the host, where loopback means their machine.
#
#   docker build -t video-to-llm .
#   docker run --rm -v "$PWD:/media" -v vtl-out:/out \
#     video-to-llm process /media/lecture.mp4
#
# The named volume matters: without it the transcription model is downloaded
# again on every run, and so is every frame you extracted last time.

FROM python:3.14-slim AS base

# ffmpeg and ffprobe are the only system dependencies. `--no-install-recommends`
# keeps this from dragging in an X stack for a headless image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, from the lockfile, so a source change does not re-resolve
# and re-download several hundred megabytes of wheels.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY app ./app
RUN uv sync --locked --no-dev

# Nothing here needs root. The output root is owned by this user so a bind mount
# without explicit ownership still works for the common case.
RUN useradd --create-home --uid 1000 vtl \
    && mkdir -p /out /media \
    && chown -R vtl:vtl /out /app
USER vtl

ENV PATH="/app/.venv/bin:${PATH}" \
    VIDEO_TO_LLM_OUTPUT_ROOT="/out" \
    XDG_CACHE_HOME="/out/.cache" \
    HOME="/out"

VOLUME ["/out"]

# `doctor` is a real readiness check — ffmpeg, the transcription model, the
# output root, disk, and the worker. If the image is broken this says how.
HEALTHCHECK --interval=1m --timeout=30s --start-period=10s --retries=2 \
    CMD ["video-to-llm", "doctor"]

ENTRYPOINT ["video-to-llm"]
CMD ["--help"]
