# wijjit-ssh

**Flask for SSH apps.** Serve [Wijjit](https://github.com/tomvillani/wijjit) TUI
applications over SSH: Wijjit draws the UI, `asyncssh` handles the transport and
PTY, and every connection gets its own live app instance.

> Status: **early.** The core transport seam it builds on
> (`wijjit.terminal.backend.TerminalBackend`) is in Wijjit proper; this package
> is the reference backend + server glue. The input path is production-shaped
> (async byte decoder, no threads); auth and resource limits are not done yet.
> See "Not yet hardened" below.

```python
from wijjit import Wijjit, render_template_string
from wijjit_ssh import WijjitSSH, SSHSession

def make_app(session: SSHSession) -> Wijjit:
    app = Wijjit(backend=session.backend)          # <- routes I/O to the channel
    @app.view("main", default=True)
    def main():
        return render_template_string(
            "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}",
            who=session.username,
        )
    return app

WijjitSSH(make_app, host_keys=["ssh_host_key"]).run(port=8022)
```

```bash
ssh-keygen -f ssh_host_key -N ''      # make a host key (once)
uv run python examples/hello_ssh.py   # serve on :8022
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
| Input | real stdin via prompt_toolkit | channel bytes fed into a prompt_toolkit **pipe input** (same key/mouse parser) |
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

WijjitSSH(make_app, host_keys=["ssh_host_key"], auth=auth).run(port=8022)
```

The authenticated username is handed to your factory as `session.username`, so
apps can personalise and authorise per user.

Use `check_password` (constant-time) rather than `==` for plaintext secrets, or
a real password hash for anything stored at rest. Credentials are never logged.

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

## Not yet hardened

- **No resource limits**: no max-sessions cap, per-IP cap, or idle timeout. A
  single client can still open sessions until you run out of memory.
- No graceful shutdown / session draining.
- Blocking sync handlers stall that session's frames - give CPU-bound apps an
  executor.

See [`SPEC.md`](SPEC.md) for the full plan and remaining milestones.

## Development

```bash
uv venv && uv pip install -e . --no-deps
uv pip install pytest pytest-asyncio pyte black ruff mypy
uv run pytest                     # 175 tests
```

Wijjit is not on PyPI yet, so install it editable alongside:
`uv pip install -e ../wijjit`.
