# Security

Two properties define this application's security posture, and both are enforced
in code and covered by tests rather than left to convention:

1. **It is reachable only from this computer.**
2. **A credential never reaches disk in readable form.**

---

## The localhost boundary

The HTTP server binds to the loopback interface and nothing else. There is no
setting, flag, environment variable, or hidden mode that changes this. `0.0.0.0`
does not appear in the codebase, LAN access is not implemented, and a test
asserts the bind address on every run.

This is a deliberate design limit, not an oversight. The application has no
authentication, no authorisation, and no session model, because it has no remote
surface that would need them. Exposing it to a network — by port-forwarding it,
tunnelling it, or putting a reverse proxy in front of it — removes the only
control protecting your data and your provider credentials. Do not do it.

The Ollama endpoint is held to the same rule from the other direction: only
`127.0.0.1`, `localhost`, and `::1` are accepted. A configured endpoint pointing
anywhere else is rejected before any request is made, so frames cannot be sent to
a "local" model that is quietly on another machine.

## Where credentials live

In order of preference:

1. **The operating system's secure store** — macOS Keychain, Windows Credential
   Manager, or a Linux Secret Service–compatible keyring. This is the normal
   path.
2. **A process-scoped environment variable** — only where no secure store is
   available, which in practice means headless Linux without a keyring daemon.

There is no third option. **A plaintext on-disk fallback is never created**, not
even as a convenience, not even temporarily. If neither store is available, the
external providers simply stay unavailable and local-only processing continues to
work — which is the whole point of local-only processing being the default.

Local Ollama has no credential at all. There is no key field for it in the
interface, no environment variable for it, and no entry for it in any store.

## Where credentials never go

A key is never written to, or displayed in:

- the SQLite database
- any log file, at any level, including debug
- any manifest, provenance record, or artifact
- any export, collection, or analysis handoff
- browser storage of any kind — no cookie, no `localStorage`, no `sessionStorage`
- documentation, source, test fixtures, or example files
- any error message, exception, or stack trace shown to you or written down

A stored key is never displayed back, not even partially masked with a few
characters revealed. The interface shows only whether a key is present.

## How that is enforced

`app/core/redaction.py` is the single place redaction rules live, so they are
tested once and trusted everywhere. It works in two layers:

**Registered values.** When a credential is read out of the secure store or the
environment, its literal value is registered. Any text containing it afterwards
is masked by exact match — which catches keys whose format we have never seen.

**Shape patterns.** Well-known credential formats and credential-bearing headers
are masked whether or not they were ever registered, so a key pasted into the
wrong field, or one belonging to a provider we do not adapt, still never reaches
a log line.

Both layers are applied at **format time**, not before it. This matters more than
it sounds: `logger.info("key=sk-ant-%s", tail)` holds no secret in the format
string and none in the argument — the secret exists only once the two are
interpolated. Redacting the pieces would leak the whole. `RedactingFormatter`
redacts the finished line, which also covers exception messages and full
tracebacks, the most common way a provider SDK spills a request header into a log.

`tests/unit/test_redaction.py` asserts all of this, including the interpolation
case specifically.

## What leaves this computer

With visual descriptions **off** — the default — nothing. No network call is made
at any point in a job. You can verify this by running with the network down.

With visual descriptions on and an **external provider** selected, exactly one
thing is sent: the small numbered still pictures, as image payloads, to the
endpoint you chose. Never the video file. Never the audio. Never the transcript.
Never a file path, a filename from your disk, or any identifier tying the
pictures to you. You are shown what will be sent and an estimated cost before
anything leaves, and sending stops at the spending cap you set.

With **Local Ollama** selected, the pictures go to the loopback interface and no
further.

There is no telemetry, no crash reporting, no usage analytics, no update check,
and no remote diagnostics. The application makes no outbound connection you did
not explicitly configure.

## Reporting a problem

This is a local single-user tool with no server component and no user data
outside your own machine. If you find a way to make it bind beyond loopback, or a
path where a credential reaches disk in readable form, treat that as a serious
defect: it contradicts a documented guarantee and there is a test that should
have caught it.
