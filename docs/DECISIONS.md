# Build Decisions

Recorded at build start. Answers came from a single consolidated questionnaire
(spec §3). Items marked **(default)** were not explicitly answered and use the
conservative default stated in the questionnaire.

## Answered directly

| # | Decision | Choice |
|---|----------|--------|
| 1 | Python toolchain | **Python 3.11 + `uv`.** `brew install uv`; uv-managed CPython 3.11.15 pinned in `.python-version`. System Python untouched. |
| 2 | Local transcription engine | **faster-whisper, `medium` model default.** CTranslate2 backend, int8 on CPU, ~1.5 GB weights fetched on first use. Size selectable `tiny`→`large-v3` in Settings. |
| 3 | Ollama | **Installed on this machine** (Homebrew cask `ollama-app`, v0.32.6) and `qwen2.5vl:7b` pulled, so the Local Ollama adapter is verified against a real runtime as well as mocks. The *application* still never installs, starts, updates, or bundles Ollama — it remains user-managed (spec §6). |
| 4 | Build autonomy | **Self-paced loop with push updates.** Wake-ups scheduled by the agent so the build resumes across usage-limit windows. Notification at each phase boundary and on completion. |

## Defaults applied

| # | Decision | Default taken |
|---|----------|---------------|
| 5 | Project root | Built in place in the directory the specification was supplied from, with a local git repo initialised there. The absolute path is deliberately not recorded in any tracked file (spec §10 forbids user paths in source control). |
| 6 | Git commit identity | Repo-local `video-to-llm <local@localhost>`. Keeps the maintainer's real name and email out of commit history so the repository stays safe for a future public export (spec §10). |
| 7 | Output root | `~/Documents/VideoToLLM`, changeable on the first-run readiness screen. Source videos are referenced by absolute path, never copied or moved (spec §6 Stage 1). |
| 8 | UI bind address | `127.0.0.1:8712`. Loopback only. `0.0.0.0` and LAN mode are not implemented and not offered (spec §4). |
| 9 | Frontend | FastAPI + Jinja2 templates + vanilla JS, no build step. Modernist design tokens ported verbatim to `app/web/static/tokens.css`. No SPA framework (spec §4). |
| 10 | Stage 3 default | **Off.** All five adapters built (Anthropic Claude, Google Gemini, OpenAI, OpenAI-compatible advanced, Local Ollama). No API key is required at any point during the build; no paid provider call is made. |
| 11 | Sampling | 2 s Balanced default. Presets 1 s / 2 s / 3 s / custom 0.5–10 s in 0.5 s steps. Fixed interval only. |
| 12 | External hard budget | $25 when an external provider is enabled. `No provider API charge` (not `$0.00`) shown for Local Ollama. |
| 13 | Worker start | Manual. `video-to-llm start` launches controller + worker; closing the browser never stops the worker. |
| 14 | Silence threshold | > 3 s. |
| 15 | Test media | Synthetic generated video + synthetic frames/transcripts + mocked providers only. No personal media enters the repo or the test suite (spec §10, §11). |
| 16 | CI | GitHub Actions workflow committed for the macOS/Windows/Ubuntu × Python 3.11 matrix, verified locally. Not pushed — no git remote is configured. |

## Standing constraints

- Localhost-only. Binding to anything other than the loopback interface is a
  build failure, covered by test.
- Secrets live only in the OS secure store or the process environment. Never in
  SQLite, logs, manifests, artifacts, exports, browser storage, docs, tests, or
  error messages. No plaintext fallback is ever created.
- No telemetry, no automatic uploads, no remote diagnostics.
- No personal, company, founder, or marketing branding anywhere in the UI or
  the repository.
