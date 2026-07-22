# Security policy

`wijjit-ssh` is an SSH server. It terminates untrusted connections, authenticates
them, and hands each one a live application. Bugs here have a different weight
than bugs in a library that only ever sees its caller's data, and this document
says what that means in practice.

## Reporting a vulnerability

**Do not open a public issue.**

Report privately through GitHub Security Advisories:

> <https://github.com/thomas-villani/wijjit-ssh/security/advisories/new>

Or by email to **thomas.villani@gmail.com** with `[wijjit-ssh security]` in the
subject.

Useful things to include: the version, what an attacker gets, and the smallest
reproduction you can manage — a test against the harness in `tests/_client.py`
is ideal, but a description of the connection sequence is plenty.

This is a one-maintainer project, so the honest commitment is modest: an
acknowledgement within a week, an assessment within two, and a fix released as
soon as one exists. You will be credited in the advisory and the changelog
unless you would rather not be.

## Supported versions

Pre-1.0, only the latest release gets fixes. There are no backports to earlier
0.x versions.

## In scope

Anything that breaks one of the properties this package claims:

- **Authentication bypass.** Reaching a session without satisfying the
  configured `AuthPolicy`, or an `AuthPolicy` accepting a credential it should
  reject. Construction is fail-closed — `WijjitSSH` raises without a policy
  unless `allow_anonymous=True` — and a path around that check is a
  vulnerability.
- **Escape from the application.** The package guarantees **no shell, no `exec`,
  no SFTP, no port forwarding**: a session only ever runs a Wijjit app, and
  those asyncssh handlers are never implemented. Any way to run a command, read
  a file, or open a forwarded connection through a session is in scope.
- **Credential disclosure.** Passwords and key material must never reach a log
  record, an event hook, or a client-visible error.
- **Host key exposure.** `ensure_host_key` writes `0600` from creation, via
  `O_CREAT | O_EXCL`, specifically so there is no window in which the server's
  private identity is world-readable and no race between two processes starting
  together. A path that widens that is in scope.
- **Cross-session leakage.** N sessions share one process and one event loop,
  isolated by contextvars. One session observing or affecting another's state,
  input, or screen is in scope.
- **Unauthenticated resource exhaustion** that defeats the configured limits —
  `max_sessions`, `max_per_ip`, `connect_rate`, `login_timeout`. Pre-auth limits
  exist precisely so an abusive peer cannot cost a key exchange.
- **Timing attacks on credential comparison.** `check_password` is constant-time
  for this reason.

Cryptography and the SSH protocol itself belong to
[asyncssh](https://github.com/ronf/asyncssh) — report those upstream. If an
asyncssh advisory requires a version floor here, that is in scope and worth
telling us about.

## Not vulnerabilities

These are known, documented, and behaving as designed:

- **`allow_anonymous=True` and `OpenAuth` serve an unauthenticated server.**
  That is what they are for. They are opt-in, must be typed explicitly, and are
  documented as development-only.
- **No backpressure handling.** A client that stops reading buffers frames in
  asyncssh without bound. It is in the README, the docs, and `SPEC.md` as the
  headline gap, and it is scheduled for M5. A report that it can be used to grow
  a server's memory is a duplicate of a known issue, not a finding — though a
  concrete measurement of how fast would be genuinely useful.
- **A blocking handler stalls its session's frames.** Documented; give CPU-bound
  apps an executor.
- **`max_per_ip` counts a NAT gateway as one address.** Inherent to per-IP
  limiting, noted in `SPEC.md` §14.
- **Anything your app does.** The trust boundary ends at `session.username`.
  This package authenticates the connection and tells you who it belongs to;
  what that user is then allowed to see is your factory's decision. An example
  that deliberately reopens a surface (a future `ide_ssh.py` with subprocess
  execution) is a demonstration of where that boundary sits, not a promise about
  where yours should.

## Deploying this safely

The short version, expanded in the
[deployment guide](https://thomas-villani.github.io/wijjit-ssh/guide/deployment.html):

- A real `auth` policy. Never `allow_anonymous=True` in production.
- Keep the limits and timeouts set — they are on by default, so this means *do
  not turn them off*.
- Run as an unprivileged user on a high port. Bind 22 only via a reverse proxy
  or `CAP_NET_BIND_SERVICE`, never by running as root.
- Manage the host key out of band with `load_host_keys`, so a restart cannot
  silently generate a new identity and train your users through the
  `REMOTE HOST IDENTIFICATION HAS CHANGED` warning.
- `ensure_host_key` logging at WARNING on every restart means your persistent
  volume is not.
