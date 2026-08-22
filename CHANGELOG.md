# Changelog

Notable changes, newest first. Dates are the day the work landed.

This project follows [semantic versioning](https://semver.org). A change to the
shape of `assembled.txt`, to a manifest, or to what a command prints is a
breaking change — those files are the product, and something downstream reads
them.

## Unreleased

Nothing is on PyPI yet, so everything below is pre-release work on the way to
1.0.0.

### Fixed

- **Pausing a job now stops the work it says it stopped.** `pause_job` wrote
  `paused` and nothing in the worker read it back, so the next stage's status
  write landed over it and the interface reported *Paused* over a job that was
  still running. Worse, frames kept going to the description model afterwards —
  on a paid provider, money spent past an explicit stop request. The worker now
  re-reads the job between videos, between stages, and before every description
  batch. A stage that stopped early records itself `paused` rather than
  `completed`, so resuming re-enters it instead of skipping the rest of the
  video.
- **Frame extraction works on FFmpeg 9.** FFmpeg 9.0 removed `-vsync`, which
  every version before 5.0 needed. Extraction now reads the version once and
  asks for whichever flag that build accepts, so 4.x through 9.x all work.
  `doctor` prints the version it found and the flag it will use.

### Added

- **`run-next`, and a queue you can reorder.** The worker runs one job at a
  time and took the oldest first, so a job queued behind a long one waited for
  the whole thing — a thirteen-video job once sat untouched for hours behind a
  single video being described locally. `video-to-llm run-next <job>`, or the
  button on the job screen, moves one to the front; the job in front steps
  aside at its next safe point, keeps its place, and resumes exactly where it
  stopped. `status` now prints the queue in the order it will run.
- **`--version`**, which the bug report template asks for.
- **`server.json`**, so the tool can be published to the official MCP registry.
- Issue templates, including one for Windows and Linux reports specifically —
  the platforms this was never developed on, and where the most useful bug
  reports will come from.

### Changed

- The skill moved from `skill/` to `skills/video-to-llm/`, which is where skill
  installers actually look. `npx skills add navdeep-h-singh/video-to-llm -g`
  now works, reaching every Agent Skills host rather than only Claude Code.
- The README's comparison table said `claude-real-video` sends audio to a cloud
  API. It transcribes locally; that is true of `/watch`, and not of both.
- The test-count badge is checked in CI against a collection run, having gone
  stale once at 1,421 while the suite collected 1,498.

## 1.0.0 — unreleased

The first release. Built in nine phases over August 2026: a localhost-only
pipeline that turns local video into one timestamped, citable document, with
frame extraction, local transcription, optional visual descriptions, ordered
multi-video collections, and a resumable worker that survives a crash, a
restart, or a closed laptop.

See [`docs/DECISIONS.md`](docs/DECISIONS.md) for the choices made along the way
and why, and [`docs/FINAL_BUILD_REPORT.md`](docs/FINAL_BUILD_REPORT.md) for what
the build actually produced.
