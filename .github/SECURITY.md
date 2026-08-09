# Security

## Reporting a vulnerability

Please report privately, through GitHub's **Report a vulnerability** button on
the Security tab, rather than opening a public issue.

Include what you did, what happened, and what you expected. A proof of concept
helps enormously. You will get an acknowledgement, and credit in the release
notes unless you would rather not have it.

## What this software's security actually rests on

This is a localhost application with no accounts and no authentication. That is
safe only because of a specific set of properties, so a bug in any of them is a
security bug rather than a defect:

- **The interface binds `127.0.0.1`**, asserted at application construction
  rather than defaulted. Anything that lets it bind another address is a
  vulnerability.
- **One middleware refuses a foreign `Host` header (421) and a foreign origin on
  every write and every `/api/` read (403).** Loopback binding keeps other
  *machines* out; it does nothing about other *origins*. A urlencoded form post
  is a CORS "simple request", so without this boundary any page a user had open
  could delete a job with its files, remove a stored key, or start a rerun that
  spends money — and a hostname rebound to 127.0.0.1 could read the replies.
- **API keys live in the operating system's secure store** — macOS Keychain,
  Windows Credential Manager, Linux Secret Service. **No plaintext fallback is
  ever created.** Storage refuses rather than degrading to a file.
- **A stored key is never rendered back**, not the value and not a prefix. Key
  fields are write-only, and redaction is applied at format time in one place.
- **No page loads an off-origin resource.** No CDN, no web font, no analytics.
  A change that adds one breaks the guarantee the header badge makes.
- **The budget is checked before a request is sent**, never after.

Things that would be reportable: any way to reach the interface from another
machine; any cross-origin request that succeeds; a key appearing in a log, a
manifest, an artifact, an export, a database row, or on a screen; a path
traversal out of the output root; a provider called after a budget cap is
reached.

## Scope

The application never installs, starts, or updates Ollama, and only loopback
endpoints are accepted for it. Cloud providers are contacted only when a user
opts in per job. Source videos are never copied, moved, or uploaded.

## Not in scope

- An attacker who already has local access to the account running the tool. The
  threat model is a hostile web page, not a hostile user.
- Content of the videos processed, or the behaviour of third-party models.
- Denial of service by handing the tool an enormous file.

## Supported versions

The latest release. This project is young; there are no backports yet.
