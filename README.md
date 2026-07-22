# wijjit-ssh

[![CI](https://github.com/thomas-villani/wijjit-ssh/actions/workflows/ci.yml/badge.svg)](https://github.com/thomas-villani/wijjit-ssh/actions/workflows/ci.yml)
[![Docs](https://github.com/thomas-villani/wijjit-ssh/actions/workflows/docs.yml/badge.svg)](https://thomas-villani.github.io/wijjit-ssh/)
[![codecov](https://codecov.io/gh/thomas-villani/wijjit-ssh/branch/main/graph/badge.svg)](https://codecov.io/gh/thomas-villani/wijjit-ssh)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/thomas-villani/wijjit-ssh/blob/main/LICENSE)

**Flask for SSH apps.** Serve [Wijjit](https://github.com/thomas-villani/wijjit) TUI
applications over SSH: Wijjit draws the UI, `asyncssh` handles the transport and
PTY, and every connection gets its own live app instance.

> Status: **early, but no longer a prototype.** The core transport seam it builds
> on (`wijjit.terminal.backend.TerminalBackend`) is in Wijjit proper; this package
> is the reference backend + server glue. The input path is production-shaped
> (async byte decoder, no threads), auth is pluggable and fail-closed, and
> resources are bounded by default. See "Not yet hardened" below for what's left.

```python
from wijjit import Wijjit, render_template_string
from wijjit_ssh import WijjitSSH, SSHSession, AuthorizedKeys, ensure_host_key

def make_app(session: SSHSession) -> Wijjit:
    app = Wijjit(backend=session.backend)          # <- routes I/O to the channel
    @app.view("main", default=True)
    def main():
        return render_template_string(
            "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}",
            who=session.username,
        )
    return app

WijjitSSH(
    make_app,
    host_keys=[ensure_host_key("ssh_host_key")],   # generated on first run
    auth=AuthorizedKeys("~/.ssh/authorized_keys"),
).run(port=8022)
```

```bash
uv run python examples/hello_ssh.py   # serve on :8022 (makes a host key if needed)
ssh -p 8022 you@localhost             # connect from anywhere
```

## How it works

Wijjit's event loop talks to "the terminal" through a `TerminalBackend` - a
small seam covering four things: frame output, key/mouse input, terminal size,
and whether the app owns the process terminal. Locally that's
`LocalTerminalBackend` (stdout / stdin / `shutil` / signals on).

`wijjit_ssh.RemoteTerminalBackend` implements the same seam against an SSH
channel:

| Concern | Local backend | Remote (SSH) backend |
|---|---|---|
| Frame output | `sys.stdout` | `chan.write(...)` |
| Input | real stdin via prompt_toolkit | raw channel bytes, decoded on the event loop (no thread, no prompt_toolkit) |
| Size | `shutil.get_terminal_size()` | negotiated PTY size, refreshed on resize, published per-task |
| Terminal ownership | `owns_terminal=True` (signals/atexit/suspend/raw mode) | `owns_terminal=False` (none of that) |

Because Wijjit's render context and the terminal-size override are
**contextvar-based**, N concurrent sessions of different sizes coexist in one
process without stepping on each other - each runs as its own asyncio task.

## Authentication

Auth is **fail-closed**: `WijjitSSH` raises unless you either pass an `auth`
policy or explicitly pass `allow_anonymous=True`. Serving an unauthenticated SSH
server should be something you typed, not something you inherited by forgetting
an argument.

```python
from wijjit_ssh.auth import AuthorizedKeys, PasswordAuth, ChainAuth, check_password

# Public keys - the recommended setup. One file for everyone...
auth = AuthorizedKeys("~/.ssh/authorized_keys")
# ...or one per user.
auth = AuthorizedKeys({"alice": "keys/alice.pub", "bob": "keys/bob.pub"})

# Passwords, checked by your callback (async is fine - hit your DB here).
async def check(username, password):
    expected = await lookup(username)
    return expected is not None and check_password(password, expected)
auth = PasswordAuth(check)

# Either credential gets you in.
auth = ChainAuth(AuthorizedKeys("~/.ssh/authorized_keys"), PasswordAuth(check))

WijjitSSH(make_app, host_keys=[ensure_host_key("ssh_host_key")], auth=auth).run()
```

The authenticated username is handed to your factory as `session.username`, so
apps can personalise and authorise per user.

Use `check_password` (constant-time) rather than `==` for plaintext secrets, or
a real password hash for anything stored at rest. Credentials are never logged.

## Host keys

A host key is your server's identity: clients pin it on first connect and refuse
to talk to you if it changes.

```python
from wijjit_ssh import ensure_host_key, load_host_keys

# Development, or a container with a mounted volume: generated on first run,
# reused forever after.
host_keys = [ensure_host_key("ssh_host_key")]

# Production: manage it out of band and fail loudly if it's missing.
#   ssh-keygen -t ed25519 -f /var/lib/myapp/host_key -N ''
host_keys = load_host_keys(["/var/lib/myapp/host_key"])

# Rotation: serve both until clients have seen the new one.
host_keys = load_host_keys(["host_key_new", "host_key_old"])
```

Keys are loaded when the server is constructed, so a bad path fails there rather
than at listen time, and each fingerprint is logged at startup. `ensure_host_key`
writes `0600` from creation (POSIX; on Windows the file inherits directory ACLs)
and logs at WARNING when it generates - if you see that on every restart, your
"persistent" volume isn't.

## Limits

Bounded by default, because a limit that's opt-in isn't a limit in any
deployment where nobody thought about it. Every value below is a
[`ServerConfig`](https://github.com/thomas-villani/wijjit-ssh/blob/main/src/wijjit_ssh/config.py) field, settable as a keyword:

```python
WijjitSSH(
    make_app,
    host_keys=host_keys,
    auth=auth,
    max_sessions=100,        # concurrent sessions, server-wide
    max_per_ip=10,           # concurrent connections from one address
    connect_rate=0.0,        # connections/sec/IP; 0 (default) disables
    connect_burst=20,        # ...and how many at once before that bites
    login_timeout=30.0,      # seconds to authenticate
    idle_timeout=600.0,      # seconds of silence before disconnect; None disables
    session_timeout=None,    # hard cap on duration regardless of activity
    keepalive_interval=30.0, # reap peers whose TCP died without a FIN
    shutdown_grace=5.0,      # seconds sessions get to exit cleanly on stop()
    banner="Authorized users only.\n",
    on_event=my_metrics_hook,
).run()
```

Two things worth knowing:

- **`max_per_ip` counts connections, `max_sessions` counts sessions.** Per-IP
  limits are enforced *before* authentication - the whole point is to not spend a
  key exchange on an abusive peer, and at that moment no session exists yet.
  Sessions per IP are bounded transitively.
- **Refusals explain themselves.** A client turned away hears "This server is at
  capacity" or "Too many connections from your address", not a bare protocol
  error.

## Shutdown

`stop()` stops accepting, asks live sessions to end, gives them
`shutdown_grace` to do it, then closes connections and the listener. It's
idempotent and safe to call concurrently.

```python
server = WijjitSSH(make_app, host_keys=host_keys, auth=auth)
await server.start()
...
await server.stop()          # drains; returns when everything is down
```

`run()` does this for you on SIGINT/SIGTERM. The grace period is not politeness:
a session that exits cleanly runs the app's teardown, which leaves the alternate
screen buffer and restores the user's terminal. One that gets cancelled doesn't,
and leaves a real person with a wedged terminal.

`run()` owns the process, so it is also the only entry point that installs signal
handlers or configures logging. `start()`/`run_async()` touch neither, so you can
embed the server in a larger asyncio application and keep control of both.

> Signal handling on Windows is best-effort: SIGTERM is never delivered there, so
> only Ctrl+C drains.

## Logging and metrics

Logs go to the `wijjit_ssh` logger tree, silent until configured, and never
propagate credentials. Each session gets a short id, bound with the username and
peer address into every line it emits:

```
2026-07-16 11:04:22 INFO    wijjit_ssh.session: [3f9a1c04 ada@10.0.0.7] Session started (term=xterm, 120x40)
```

`run()` configures stderr logging unless you already set up your own handler (on
either `wijjit_ssh` or the root logger). Otherwise call
`wijjit_ssh.configure_logging(...)` yourself.

For metrics, pass `on_event=` - called with `connection.opened|closed|rejected`,
`auth.ok|failed`, and `session.started|rejected|ended` (with `duration`), so you
can wire up Prometheus without this package depending on a metrics library. A
hook that raises is logged and swallowed; it can't take a session down.

## Deployment

Reference artifacts live in [`deploy/`](https://github.com/thomas-villani/wijjit-ssh/tree/main/deploy/)
— a sandboxed systemd unit, a non-root Dockerfile, a compose file, and a
healthcheck. All four are files you can run, not snippets to adapt.

```bash
sudo install -m 0644 deploy/wijjit-ssh.service /etc/systemd/system/
docker compose -f deploy/compose.yaml up --build
python deploy/healthcheck.py --port 8022 --verbose
```

Three things go wrong far more often than anything else:

- **The host key is not persistent.** Every returning user then gets
  `REMOTE HOST IDENTIFICATION HAS CHANGED`, which trains them to ignore the one
  warning that protects them. Mount a volume, use `StateDirectory`, and watch for
  `ensure_host_key`'s WARNING on restart — it is telling you the storage isn't.
- **The supervisor's stop timeout is shorter than `shutdown_grace`.** Then it
  sends `SIGKILL` mid-drain, no session runs its teardown, and every connected
  user keeps a wedged terminal. `TimeoutStopSec` and `stop_grace_period` must
  both exceed it.
- **The healthcheck is a TCP connect.** A wedged event loop still answers a TCP
  handshake — the kernel completes it without the application — so the probe
  reports healthy while nobody can log in. `deploy/healthcheck.py` instead
  completes the SSH key exchange and treats being refused at authentication as
  the success condition.

Full write-up, including the production security checklist:
<https://thomas-villani.github.io/wijjit-ssh/guide/deployment.html>

## Examples

Three runnable programs in [`examples/`](https://github.com/thomas-villani/wijjit-ssh/tree/main/examples/), in the order worth reading:

| Example | What it is for |
|---|---|
| [`hello_ssh.py`](https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/hello_ssh.py) | The smallest thing that works: one factory, one view, a text field and a counter. |
| [`dashboard_ssh.py`](https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/dashboard_ssh.py) | A live server dashboard - gauges, history, top processes, and a table of everyone connected to the server drawing it. One shared sampler feeding N windows. |
| [`chat_ssh.py`](https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/chat_ssh.py) | A multi-user chat room with no user accounts, because SSH already authenticated everyone. N writers feeding N windows. |

```bash
uv run python examples/hello_ssh.py                          # :8022
uv run python examples/chat_ssh.py                           # :8023
uv sync --group examples                                     # dashboard needs psutil
uv run --group examples python examples/dashboard_ssh.py     # :8022
```

The first is about the transport; the other two are about the thing the transport
makes possible - **N live apps in one process, sharing state**. Both are built the
same way: a hub at module scope that every view reads directly, and
`app.refresh()` to tell the other sessions' apps to redraw, since each is parked
in its own task waiting for a keypress that may never come. The push latency is
`REFRESH_INTERVAL / 2`, or 0.5s if you leave it unset.

The non-obvious half is *un*subscribing. There is no teardown hook on the factory,
and `app.running` races the server (the factory runs before the app's task
starts). Use `on_event=` instead: `session.ended` fires on every way out - quit,
idle timeout, dropped TCP, `stop()` - and carries the `session_id` the factory
registered under. Test it by closing a client's terminal rather than pressing
Ctrl+Q; that is the path a hand-rolled subscriber list gets wrong.

Full write-ups: <https://thomas-villani.github.io/wijjit-ssh/examples/>

## Done

- **Async byte-decoded input.** Raw channel bytes are decoded into Wijjit
  key/mouse events on the event loop - no thread and no prompt_toolkit pipe per
  session. Handles split escape sequences, split UTF-8 runes, SGR + legacy
  mouse, bracketed paste, and the lone-ESC ambiguity. (`wijjit_ssh.input`)
- **Binary channel** (`encoding=None`): the decoder sees exactly what the client
  sent.
- **Pluggable, fail-closed auth**: public key, password, keyboard-interactive,
  chained. (`wijjit_ssh.auth`)
- **Per-session isolation.** N concurrent sessions, each with its own app, state,
  and terminal size, in one process and one event loop.
- **Host keys** that generate on first run or load for production, with
  rotation. (`wijjit_ssh.keys`)
- **Resource limits** on sessions, per-IP connections, connect rate, idle time,
  and session duration - on by default. (`wijjit_ssh.limits`)
- **Graceful shutdown**: drains sessions so clients get their terminals back,
  on `stop()` or a signal.
- **Per-session logging** and a metrics hook. (`wijjit_ssh.logging`)
- **No shell, exec, sftp, or port forwarding.** A session only ever runs a
  Wijjit app; there is no code path to anything else. That's a feature.

## Not yet hardened

- **No backpressure handling.** A client that stops reading buffers frames in
  asyncssh without bound.
- Blocking sync handlers stall that session's frames - give CPU-bound apps an
  executor.
- Wide chars / emoji are treated as single-width (a Wijjit limitation, not an
  SSH one).

## Documentation

Full docs at **<https://thomas-villani.github.io/wijjit-ssh/>** — quickstart,
guides for each of the subjects above, write-ups of the examples, and an API
reference over all eight modules. See [`SPEC.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/SPEC.md) for the full plan and
remaining milestones.

## Development

Wijjit is not on PyPI yet, so `pyproject.toml` points `uv` at a sibling checkout
via `[tool.uv.sources]`. Clone the two repos side by side:

```
PycharmProjects/
  wijjit/        # github.com/thomas-villani/wijjit
  wijjit-ssh/    # this repo
```

```bash
uv sync                            # installs wijjit editable from ../wijjit
uv run pytest -q                   # 334 passed, 4 skipped
uv run ruff check src/ tests/ examples/
uv run black --check src/ tests/ examples/
uv run mypy src/
uv run pytest --cov=src/wijjit_ssh --cov-report=term-missing -q
```

Those are exactly the commands CI runs, so a clean local run means a green build.
The docs are a separate dependency group, so a test run doesn't pay for Sphinx:

```bash
uv sync --group docs
uv run sphinx-build -b html -W --keep-going docs/source docs/build/html
```

`psutil` is likewise its own `examples` group — only `dashboard_ssh.py` wants it,
and nobody running the tests should have to build a C extension for it.

`-W` is what CI uses; the build is warning-clean, and keeping it that way is the
point of the flag.

The four skips are all POSIX-only - three `0600` host-key mode-bit assertions and
the end-to-end SIGTERM drain - so on Linux and macOS the suite reports 338 passed.
CI covers Python 3.11-3.13 on Linux, macOS, and Windows.

The source is **editable and a path, not a git ref**, on purpose: the two
libraries are developed in tandem, so changes to `../wijjit` are picked up here
immediately with no reinstall. A git source would test against whatever was last
pushed instead.

Once `wijjit` is published, delete the `[tool.uv.sources]` section - the
dependency pin already says what it needs. (`uv` strips that section from
published metadata, so it never affects anyone installing the package.)

[`CONTRIBUTING.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/CONTRIBUTING.md)
has the rest: style rules, what the tests are expected to look like, the commit
conventions, and what is deliberately out of scope.
[`RELEASING.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/RELEASING.md)
covers cutting a version.

## Security

This is an SSH server: it terminates untrusted connections and authenticates
them. **Report vulnerabilities privately**, through
[GitHub Security Advisories](https://github.com/thomas-villani/wijjit-ssh/security/advisories/new)
rather than a public issue.
[`SECURITY.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/SECURITY.md)
says what is in scope, what is a known and documented gap rather than a finding,
and where the trust boundary sits.

The load-bearing guarantee is negative: **no shell, no `exec`, no SFTP, no port
forwarding**. Those asyncssh handlers are never implemented, so a session has no
code path to anything but your Wijjit app.

## License

MIT — see [`LICENSE`](https://github.com/thomas-villani/wijjit-ssh/blob/main/LICENSE).
