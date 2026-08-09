# Launch checklist

Everything here needs a decision, an account, or a machine this build could not
reach. The code side of the launch plan in
[`GITHUB_LAUNCH_PLAN.md`](GITHUB_LAUNCH_PLAN.md) is done; this is what is left.

Ordered so that each step unblocks the next.

---

## 1. Blocking — nothing can be published until these are done

### 1.1 Pick the GitHub account and resolve the placeholder

Seven places say `OWNER` where an account name belongs. Find them:

```bash
uv run python scripts/pre_publish_audit.py --release
```

Replace, then confirm the audit passes in release mode. It refuses while any
remain, so this cannot be forgotten — but it also means **the release workflow
will fail until it is done**.

Files affected: `pyproject.toml` (five URLs), `.claude-plugin/plugin.json`, and
the audit's own note.

### 1.2 Create the repository and let CI run

```bash
gh repo create video-to-llm --private --source=. --push
```

**Private first.** Nine test cells (3 operating systems × 3 Python versions),
plus an installed-wheel job and a container build, have never executed. Expect
Windows to surface something: path separators, keyring backend, the symlink and
hard-link fallbacks, and the folder-name slug are all defensively written and
never run.

Fix what it finds before going public. This is the single highest-value step in
the whole plan, and it cannot be done from here.

### 1.3 Verify the container image

The Dockerfile has **never been built** — the development machine has no Docker.
CI builds it, runs the CLI inside it, and runs the synthetic smoke test with
`--network none`. Watch that job specifically.

### 1.4 Publish to PyPI

The README's quickstart is `uvx video-to-llm process lecture.mp4`, which does
not work yet, and a note in the README says so. Remove that note in the same
commit that publishes.

1. Register the name on PyPI.
2. Configure trusted publishing for the `release` environment
   (https://pypi.org/manage/account/publishing/).
3. Tag: `git tag v1.0.0 && git push --tags`.

The workflow re-runs the whole gate, checks the tag matches the packaged
version, and installs the built wheel into a clean environment to run it from
outside the checkout before anything is uploaded.

---

## 2. Repository settings — paste these in

### About

> Turn hours of local video into timestamped, citable, LLM-ready text. Frames +
> transcript + descriptions. Fully offline. Resumable.

### Topics

```
llm  video  whisper  ffmpeg  local-first  privacy  offline  rag  ai-tools
transcription  video-analysis  ollama  claude  self-hosted  context-engineering
mcp  agent-skills  faster-whisper  python  multimodal
```

### Social preview image

Required — it is what renders when the link is posted to Hacker News, Reddit, X,
or Slack. In the existing Modernist identity: vermilion `#ec3013`, warm greys,
zero border-radius, grotesque type.

> **VIDEO → LLM**
> 15 hours of video. One document. Nothing uploaded.
> *(a strip of real `assembled.txt` beneath, timestamps visible)*

### Other settings

- Enable Issues and Discussions. Disable Projects and the Wiki.
- Default branch `main`; require CI to pass before merge.
- Enable private vulnerability reporting (Settings → Security) — `.github/SECURITY.md`
  tells people to use it.

### Badges to add once they are true

The README carries static badges today. Add the live CI badge the moment the
first run is green, and the PyPI version badge on first publish:

```markdown
[![CI](https://github.com/OWNER/video-to-llm/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/video-to-llm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/video-to-llm)](https://pypi.org/project/video-to-llm/)
```

---

## 3. Assets that need a screen recording

None of these could be produced headlessly. They are the highest-leverage
marketing work left.

- **The demo GIF.** `process` running end to end on a real video, then
  `show` resolving a citation to a frame. This one asset sells the tool better
  than any paragraph. Keep it under 30 seconds. `asciinema` plus `agg`, or a
  plain screen capture.
- **Three interface screenshots**: a job in progress with the live progress bar,
  the frame reviewer, and the collection builder.
- **The social preview card** (above).

The README references `docs/assets/demo.gif` nowhere yet — add the image to the
hero block once it exists.

---

## 4. Measurement that needs real videos and real time

### 4.1 Run the benchmark properly

```bash
uv run python scripts/benchmark.py --output-root /tmp/bench --json bench.json \
    lecture.mp4 interview.mp4 conference-talk.mp4 tutorial.mp4 screencast.mp4
```

Five or six videos of genuinely different kinds. The current published figure —
85% against sending frames — is n=1 on a chart screencast, close to the best
case. The harness prints median and range so the spread is part of the result.

This is wall-clock bound, not effort bound. Start it early; it runs unattended.

### 4.2 Exercise one cloud provider for real

No cloud adapter has ever met a live service. One small job, a low cap, one
provider. This validates the adapter, the Check button, the cost estimate, and
the budget path in one go. Until it happens the README says exactly that, and
`test_readme_claims.py` refuses to let it say otherwise.

### 4.3 Measure description accuracy

[`DESCRIPTION_QUALITY.md`](DESCRIPTION_QUALITY.md) establishes that the
confidence field now varies and that content was always largely correct. It does
**not** establish accuracy. Sample fifty frames, check them by hand, publish the
number.

---

## 5. The one product decision left

**Generalise the description schema.** Five of the eight content fields describe
forex charts. The model returns `Unknown` rather than hallucinating on other
content, so it is wasteful rather than dangerous — but a general-purpose tool
that asks a cooking video for its currency pair contradicts its own positioning
on the first video anybody tries.

The recommended shape, with reasoning, is in
[`DESCRIPTION_QUALITY.md`](DESCRIPTION_QUALITY.md): per-job profiles
(`general`, `trading`, `slides`, `code`, `custom`), with the trading profile
preserving today's behaviour exactly so the existing 15-hour corpus stays valid.

This was deliberately not done autonomously: it moves `FrameDescription`, the
parser, `SCHEMA_VERSION`, the batch artifacts already on disk, `enrich.py`,
`assemble.py`, and `review.html` together, and it deserves a decision rather
than a side effect.

**Do this before a public launch**, not after.

---

## 6. Distribution registries, once the repo is public

In the order the launch plan sequences them:

- **Phase 1 (soft):** r/LocalLLaMA, r/selfhosted, r/DataHoarder; Ollama and
  LocalLLaMA Discords. Submit to `awesome-whisper`, `awesome-selfhosted`,
  `awesome-local-ai`, `awesome-llm-apps`.
- **Phase 2 (agent surface):** the MCP registry, `awesome-mcp-servers`,
  mcpservers.org, Smithery, Glama. The Claude Code plugin marketplace and
  `npx skills add`. Then r/ClaudeAI and r/mcp.
- **Phase 3:** Show HN. Title: *"Show HN: Turn 15 hours of local video into one
  document an LLM can read"*. Tuesday–Thursday, 08:00–10:00 ET. Arrange a
  lobste.rs invite in advance.

Do not reorder these. Show HN happens once.

---

## 7. Seed the issue tracker

An empty tracker looks abandoned. These are real, small, and genuinely useful —
good `good first issue` candidates, all drawn from known limitations:

1. **Prune the event log.** It grows without bound; progress events are already
   rate-limited to one per ten minutes to avoid making it worse.
2. **Remove eleven unused decorative classes** from `static/tokens.css`.
3. **Drop three schema columns nothing reads**: `jobs.settings_json`,
   `stage_runs.output_version`, `events.detail_json`.
4. **`/settings` shells out to `ffmpeg -version` on every render** (10 s cap) —
   cache it.
5. **Add a `--quiet` flag to `process`** so it prints only the output path.
6. **`video-to-llm watch <dir>`** — process anything dropped into a folder.
7. **Speaker diarisation** — the most-requested feature in every adjacent
   project's tracker.
8. **`video-to-llm search "<phrase>"`** across processed jobs.

Items 6–8 are the P2 features deliberately not built; they are better as issues
that attract contributors than as code nobody asked for.
