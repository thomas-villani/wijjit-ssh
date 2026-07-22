# wijjit-ssh — Implementation Spec

Status: **in progress — M1, M2 and M3 done.** This document specifies the work to
take `wijjit-ssh` from the original prototype to something you could actually
deploy: a real async byte-parser input path, pluggable authentication,
terminal-capability negotiation, resource limits, graceful shutdown, logging,
tests, and packaging.

- **M1 — async byte-parser input path (§4).** Done. The reader thread and the
  prompt_toolkit pipe are gone; the channel is binary.
- **M2 — authentication (§5).** Done. Fail-closed, pluggable, with accept *and*
  reject proven over real SSH.
- **M3 — robust lifecycle (§7–§10).** Done. Host keys, resource limits,
  per-session logging + metrics hook, and graceful shutdown. `ServerConfig` was
  pulled forward from M4, since M3 introduced the twelve knobs it exists to hold.
- **M4 — packaging, CI, docs.** In progress. Packaging metadata, CI, the Sphinx
  docs site, and the first two examples are landed; the remaining two examples
  and the §12 deployment artifacts are not.

334 tests (338 on POSIX, where four Windows-skipped tests run). §4, §5 and §7–§10
below now describe the code rather than a plan. The remaining work is the rest of
**M4** and all of **M5 (hardening)** — the big remaining gap is **backpressure
(§8)**: a client that stops reading still buffers frames in asyncssh without bound.

It is deliberately concrete — interface signatures, file layout, and phased
milestones — so it can be read top-to-bottom to understand the whole design,
then executed phase by phase.

> **Corrections folded in during M3.** Several §7–§10 items turned out to be
> wrong when checked against asyncssh 2.24 and CPython, and have been rewritten
> in place rather than left as aspiration. The substantive ones: a session cannot
> be refused *with a message* by returning falsy from `session_requested` (§8);
> per-IP limits and `max_sessions` cannot share a chokepoint, because one is
> pre-auth and the other cannot be (§8); the idle-timeout notice has to be
> written *after* the app's teardown, not before, or it lands inside the
> alternate screen buffer (§8); and reusing Wijjit's `get_logger` was not merely
> untidy but an active bug (§9).

---

## 1. Goals & non-goals

**Goals**
- Serve any Wijjit app over SSH with per-connection isolation, one app instance
  per session, N sessions per process.
- Zero changes required to an existing Wijjit app beyond `Wijjit(backend=...)`.
- Real auth (public key, password, keyboard-interactive), configurable per
  deployment.
- Correct terminal handling: raw byte I/O, full key/mouse parsing, resize,
  UTF-8, sane defaults for real-world SSH clients (OpenSSH, PuTTY, mobile).
- Bounded resources: max sessions, idle/absolute timeouts, per-IP limits.
- Clean shutdown that restores every client and cancels every app task.

**Non-goals (for v0.1)**
- Not a general SSH server: no exec/subsystem/sftp/port-forwarding. Sessions
  only ever run a Wijjit app; there is no shell, no command execution, no
  filesystem access. This is a feature (attack surface), not a limitation.
- No multi-process/worker model. One event loop, one process. Horizontal
  scale is "run more instances behind a TCP load balancer" (see §12).
- No built-in TLS/cert stuff — SSH provides the transport crypto.

---

## 2. Architecture recap

Wijjit's event loop talks to "the terminal" through a `TerminalBackend`
(`wijjit.terminal.backend`). The seam is already in core:

```
asyncssh channel  ──bytes──▶  RemoteTerminalBackend  ──Key/MouseEvent──▶  Wijjit event loop
       ▲                            (this package)                              │
       └────────────────────────  frame bytes  ◀───────────────────────────────┘
```

Per connection we build: a backend, an input source, and a `Wijjit` app, then
run `app.event_loop.run_async()` as its own asyncio task. Wijjit's render
context and terminal-size override are **contextvar-based and task-local**, so
concurrent sessions never collide. `owns_terminal=False` on the backend keeps
the app from installing any process-global signal/atexit/suspend handlers.

The prototype took two shortcuts, **both now removed (M1)**:
1. ~~Input goes through a prompt_toolkit pipe input + reader thread (one thread
   per session).~~ Replaced by an async byte parser on the event loop (§4). No
   threads, no prompt_toolkit, in the remote path.
2. ~~The channel is opened in UTF-8 text mode.~~ The channel is now binary
   (`encoding=None`), so the decoder sees exactly what the client sent (§6).

One core change fell out of M1: `TerminalBackend.create_input_handler` used to
be typed as returning the concrete `InputHandler`, which contradicted the seam's
whole purpose. Wijjit core now defines an `InputSource` **Protocol**
(`wijjit.terminal.backend`) describing the six members the event loop actually
calls; `InputHandler` and `ChannelInputSource` both satisfy it structurally.
This is what §4.2 below always assumed, now enforced by the type system.

---

## 3. Target package layout

```
wijjit-ssh/
  pyproject.toml
  README.md
  CHANGELOG.md                                                              [done]
  LICENSE                     MIT, matching wijjit                          [done]
  SPEC.md                     (this file)
  .github/workflows/
    ci.yml                    test matrix / lint / coverage   (§13 M4)      [done]
    docs.yml                  Sphinx build + Pages deploy     (§13 M4)      [done]
  docs/                       Sphinx site                     (§13 M4)      [done]
  deploy/                     systemd unit, Dockerfile, healthcheck (TODO, §12)
  src/wijjit_ssh/
    __init__.py               public API
    py.typed                  PEP 561 marker                                [done]
    server.py                 WijjitSSH, session glue (asyncssh callbacks)  [done]
    backend.py                RemoteTerminalBackend  (bytes I/O + size)     [done]
    input.py                  ChannelInputSource + KeyDecoder  (§4)         [done]
    auth.py                   AuthPolicy + presets            (§5)          [done]
    keys.py                   host-key loading/generation     (§7)          [done]
    limits.py                 SessionRegistry, timeouts, rate limit (§8)    [done]
    config.py                 ServerConfig dataclass          (§10)         [done]
    logging.py                per-session logging + events    (§9)          [done]
  examples/
    hello_ssh.py              minimal demo                                  [done]
    dashboard_ssh.py          shared sampler -> N windows       (§13 M4)    [done]
    chat_ssh.py               N writers -> N windows            (§13 M4)    [done]
    ide_ssh.py                editor + sandboxed exec (TODO, M4)
    store_ssh.py              cart + out-of-band checkout (TODO, M4)
  tests/
    __init__.py               (a package, so helpers import as tests._client)
    _client.py                pyte-backed client harness                    [done]
    conftest.py               `serve` factory fixture                       [done]
    test_input_decoder.py     table-driven byte->Key/Mouse (165 cases)      [done]
    test_roundtrip.py         in-process client<->server + limits wiring    [done]
    test_auth.py              policies + real-SSH accept/reject (30 tests)  [done]
    test_keys.py              host keys (22 tests)                          [done]
    test_config.py            ServerConfig + resolution (31 tests)          [done]
    test_limits.py            buckets/timers/registry, fake clock (33)      [done]
    test_logging.py           tree containment, session logs, events (18)   [done]
    test_shutdown.py          stop(), drain, signals (13 tests)             [done]
```

---

## 4. Async byte-parser input path (replaces the reader thread)

### 4.1 Why

The prototype reuses `wijjit.terminal.input.InputHandler`, which spawns a
background thread polling prompt_toolkit. That is fine for a local app but wrong
at server scale: one OS thread per connection, plus a prompt_toolkit pipe per
session. We already receive bytes on the event loop in `data_received`; parse
them there, on the loop, with no thread and no prompt_toolkit.

### 4.2 The contract the event loop requires

An input handler is duck-typed. The complete surface Wijjit calls (verified
against `core/event_loop.py`, `core/suspend.py`, `testing/harness.py`):

```python
class InputSource(Protocol):
    mouse_enabled: bool
    async def read_input_async(self, timeout: float) -> Key | MouseEvent | None: ...
    def enable_mouse_tracking(self) -> None: ...
    def disable_mouse_tracking(self) -> None: ...
    def close(self) -> None: ...
    def restore_terminal(self) -> None: ...
```

`ChannelInputSource` implements exactly this. `read_input_async` awaits an
`asyncio.Queue` (populated by the decoder) with the given timeout, returning
`None` on timeout — matching how the loop already treats a quiet frame.
`enable/disable_mouse_tracking` write the DEC private-mode sequences to the
channel (through the backend's byte writer). `restore_terminal`/`close` are
no-ops beyond draining the queue (there is no local tty/termios to restore).

### 4.3 `KeyDecoder` — a resumable byte→event parser

A pure, side-effect-free state machine. `feed(data: bytes)` returns a list of
`Key`/`MouseEvent`; it buffers incomplete trailing sequences across calls.

```python
class KeyDecoder:
    def __init__(self, *, utf8: bool = True) -> None: ...
    def feed(self, data: bytes) -> list[Key | MouseEvent]: ...
    def flush(self) -> list[Key | MouseEvent]:  # resolve a pending lone ESC
```

Parsing rules (reuse existing tables in `wijjit.terminal.input` and
`wijjit.terminal.mouse` wherever possible):

- **Printable / UTF-8**: decode incrementally (`codecs.getincrementaldecoder`)
  so multi-byte runes split across packets are handled. Each rune → a
  `KeyType.CHARACTER` `Key`.
- **C0 control** (`0x00–0x1f`, `0x7f`): map via `SINGLE_CHAR_KEYS` (Enter, Tab,
  Backspace, Ctrl+letter). Ctrl+A..Ctrl+Z = `0x01..0x1a`.
- **CSI** (`ESC [ … final`): match `ESCAPE_SEQUENCES` (arrows, Home/End,
  PgUp/Dn, Delete, F-keys with `~`), including modifier params
  (`ESC [ 1 ; 5 A` = Ctrl+Up) → set modifiers on the `Key`.
- **SS3** (`ESC O …`): application-cursor-mode arrows/Home/End (PuTTY, some
  terminals) → same keys.
- **SGR mouse** (`ESC [ < b ; x ; y M|m`): delegate to
  `wijjit.terminal.mouse.MouseEventParser` (already handles this) → `MouseEvent`.
  Also accept legacy X10/normal mouse (`ESC [ M cb cx cy`) for old clients.
- **Bracketed paste** (`ESC [ 200~ … ESC [ 201~`): collect the payload and emit
  it as character keys (respect `MAX_PASTE_SIZE`), so paste doesn't trigger
  hotkeys.
- **Lone ESC ambiguity**: if the buffer is exactly `ESC` (or `ESC` + an
  incomplete sequence) and no more bytes have arrived, we cannot tell "user hit
  Escape" from "start of a sequence". Resolve with a short timer: the session
  schedules `decoder.flush()` ~30–50 ms after the last byte if the buffer still
  holds a bare ESC, emitting `Keys.ESCAPE`. (SSH batches a full sequence in one
  packet almost always, so this rarely fires.)

The decoder is the crown-jewel unit-test target (§11): a table of
`bytes → [events]` covering every branch, including split-packet cases fed one
byte at a time.

### 4.4 Wiring

`RemoteTerminalBackend.create_input_handler(...)` returns a
`ChannelInputSource`. `_WijjitSSHSession.data_received(data)` calls
`source.feed(data)` (decoder → queue). The prompt_toolkit pipe and the
`input=`/thread path are dropped from the remote backend entirely. (Core's
`InputHandler` keeps its `input=`/`output=` params for the local case — they're
harmless and still used by the local backend.)

---

## 5. Authentication (`auth.py`)

asyncssh drives auth through `SSHServer` callbacks. We wrap them behind an
`AuthPolicy` so deployments choose a strategy without touching server glue.

```python
class AuthPolicy:
    def auth_required(self, username: str) -> bool: ...          # begin_auth
    def password_supported(self) -> bool: ...
    async def verify_password(self, username: str, password: str) -> bool: ...
    def public_key_supported(self) -> bool: ...
    def authorized_keys_for(self, username: str) -> "list[SSHKey] | None": ...
    def kbdint_supported(self) -> bool: ...
    async def verify_kbdint(self, username: str, responses: list[str]) -> bool: ...
```

`_WijjitSSHServer` forwards each asyncssh callback to the policy:
`begin_auth → auth_required`, `validate_password → verify_password`,
`public_key_auth_supported/validate_public_key` via `authorized_keys_for` (or
asyncssh's `authorized_client_keys` on `create_server`), etc.

**Presets shipped:**
- `OpenAuth()` — no auth (any username). Dev only; server logs a loud warning
  at startup when used. This is today's default; it becomes opt-in.
- `AuthorizedKeys(path="~/.ssh/authorized_keys" | mapping)` — public-key auth
  against an OpenSSH `authorized_keys` file (global or per-username mapping).
  Recommended default for real deployments.
- `PasswordAuth(callback)` — `async (username, password) -> bool`. Callback is
  responsible for constant-time comparison / hashing (document `secrets.compare_digest`).
- `ChainAuth(*policies)` — accept if any policy accepts.

The `username` is surfaced to the app via `SSHSession.username` (already wired),
so apps can personalize/authorize per user.

**Default posture:** constructing `WijjitSSH(...)` **without** an `auth=`
argument raises unless `allow_anonymous=True` is passed — fail-closed, so no one
accidentally ships `OpenAuth`.

---

## 6. Terminal capability negotiation (§ in `server.py`/`backend.py`)

- Open the server channel with **`encoding=None`** → `data_received` gets
  `bytes`, backend writes `bytes`. The decoder and frame writer both work in
  bytes; ANSI frames from Wijjit are `str`, encoded once with
  `str.encode("utf-8")` at the boundary.
- `pty_requested(term_type, term_size, term_modes)`: capture `term_type`
  (exposed on `SSHSession`), initial `(cols, rows)`, and the pty modes. Honor
  `term_modes` only insofar as needed; the app draws everything.
- **`line_editor=False`** on `create_server` (already discovered in the
  prototype — asyncssh otherwise echoes and line-buffers input, breaking a TUI).
- Reject non-PTY sessions (`pty_requested` never called → `shell_requested`
  returns an error string) with a friendly message: this server only serves
  interactive TUIs.
- Terminal size on connect and on `terminal_size_changed` → `backend.resize()`;
  the loop republishes it to the task-local size override (already implemented).
- Mouse: `ChannelInputSource.enable_mouse_tracking()` emits the DEC modes;
  Wijjit already decides *whether* to enable based on `ENABLE_MOUSE`.
- Encoding note: assume UTF-8 clients. Optionally read `LANG` from
  `env_requested` later; out of scope for v0.1.

---

## 7. Host keys (`keys.py`) — **done**

- `load_host_keys(paths) -> list[SSHKey]` — clear error if missing/unreadable,
  naming the path (asyncssh's own `KeyImportError` says only "Invalid private
  key", which is useless when several were passed). A missing path raises rather
  than being skipped: skipping would serve a *different identity* than intended,
  which every client reports as a possible attack.
- `ensure_host_key(path) -> SSHKey` — load, or generate + persist an ed25519 key
  on first run. Logs at **WARNING** when generating, not INFO: on a healthy
  server it happens exactly once, and if a container's volume isn't actually
  persistent this is the line that explains why every client suddenly reports a
  host-key mismatch.
- `resolve_host_keys(sources)` — normalizes paths / `PathLike` / live `SSHKey`
  into loaded keys. This is what lets `WijjitSSH` tighten `host_keys` to
  `Sequence[HostKeySource]` without breaking the three call styles already in
  the tree. Resolution is eager, at construction, so a bad path fails where the
  server is configured rather than inside `create_server`.
- **Correction — 0600.** `SSHKey.write_private_key()` is a plain file write with
  no mode, so it would create the key world-readable and only narrow it
  afterwards, leaving a window where the server's identity is locally readable.
  We write via `os.open(..., O_CREAT | O_EXCL, 0o600)` instead: private from
  creation, and `O_EXCL` also settles the two-processes-starting-together race
  (the loser adopts the winner's key). On Windows the mode argument is silently
  **ignored** — the file inherits directory ACLs and `st_mode` always reads
  `0o666` — so the permission warning is POSIX-only rather than noise.
- Rotation works by passing multiple keys (asyncssh serves all).
- README documents both `ssh-keygen -t ed25519 -f ssh_host_key -N ''` and
  `ensure_host_key`.

---

## 8. Concurrency, limits, lifecycle (`limits.py`) — **done except backpressure**

A `SessionRegistry` tracks live sessions and enforces bounds; the server
consults it in `connection_made`/`session_requested`. The registry is **pure**:
no sockets, no asyncssh import, injectable clock, sessions reached only through
a `ManagedSession` Protocol. That is what lets the real assertions be fast unit
tests with a fake clock, leaving the over-SSH tests to prove wiring only — the
same split §11 already uses for the decoder.

- **Correction — two chokepoints, not one.** The original "per-IP concurrency +
  connect rate limit" bullet lumped these with `max_sessions`, but they cannot
  share a hook. Per-IP limits and the rate limit are only worth having
  **pre-auth** — the whole point is to not spend a key exchange on an abusive
  peer — while `max_sessions` is inherently **post-auth**, since a session only
  exists once a channel opens. So **per-IP counts connections** and **the global
  cap counts sessions**; per-IP sessions are bounded transitively.
- **Max concurrent sessions** (`max_sessions`): checked in `session_requested`,
  which is also where a session registers (not `session_started`: the username is
  already known, and it closes the window where a client opens channels and never
  asks for a shell).
- **Correction — refusing "with a message"** is impossible via a falsy
  `session_requested`, which raises `ChannelOpenError(OPEN_CONNECT_FAILED,
  'Session refused')` with no way to attach text. A refusal is therefore a real
  `_RejectedSession` object that writes its reason and exits(1).
- **Correction — pre-auth rejection must be deferred** with `loop.call_soon`.
  asyncssh calls `connection_made` one line before `_send_version()`, and
  `MSG_DISCONNECT` is packet type 1 — below the gate that defers packets pre-kex.
  Disconnecting inline puts a binary packet ahead of the `SSH-2.0-` banner and
  nulls the transport out from under the banner write. Deferred by a tick, the
  client parses it properly: verified to surface as `DisconnectError` carrying
  our text and `DISC_TOO_MANY_CONNECTIONS`.
- **Login grace timeout**: `login_timeout` on `create_server`. Note this is a
  *tightening* (30s), not a new capability — asyncssh already defaults to 120s.
- **Idle timeout** (`idle_timeout`): per-session timer reset on each
  `data_received`.
- **Correction — "notify + close" is the wrong order.** The app is in the
  alternate screen buffer with a diff renderer, so a message written before the
  app tears down lands inside the TUI frame and is painted over by the next
  repaint. The order must be: `quit()` → await the app's teardown (which emits
  `ESC[?1049l`) → notify → close. Pinned by a test asserting the message byte
  offset exceeds the alt-buffer-exit offset.
- **Absolute session timeout** (`session_timeout`, optional): a separate
  deadline, not a bound on the idle one, since it interrupts someone actively
  working. Off by default.
- **Keepalive** (`keepalive_interval`/`keepalive_count_max`) reaps peers whose
  TCP died without a FIN.
- **Backpressure — still TODO (M5).** If a client stops reading, `chan.write`
  buffers. Watch `chan` drain, throttle that session's render cadence (or drop
  frames — the diff renderer self-heals on the next full repaint). The byte
  in/out counters from §9 land here too, since they share the `_ChannelWriter`
  seam.

**Lifecycle (per session):**
```
connection_made → check_connection (per-IP, rate) → begin_auth (banner)
  → (auth) → session_requested: try_admit(max_sessions) → register
  → pty_requested → shell_requested
  → session_started:  reject if no pty → seed size override → build
                      backend+input+app → task = ensure_future(run_async())
                      → task.add_done_callback → timer.start()
  → data_received*    → timer.poke() → decoder.feed → input queue
  → terminal_size_changed* → backend.resize
  → (app exits  OR  idle/absolute timeout  OR  connection_lost  OR  drain)
  → request_close(reason, message)  [idempotent, one path down]
  → _close: app.quit() → await task (grace) → cancel if it won't go
            → write message → close channel → registry.release → emit
```
Teardown is idempotent and never raises into asyncssh callbacks.

**Correction — the old teardown was wrong**, not just untidy. `connection_lost`
called `app.quit()` and `task.cancel()` in the same tick; since `quit()` only
sets a flag the loop checks on its next pass, the cancel always landed first and
every session ended by cancellation. Survivable when the peer has already gone,
wrong for idle timeout and shutdown, where the channel is alive and the app's
`finally` is what restores the client's terminal. Relatedly, `_run_app`'s
`finally` must **not** close the channel: that fires `connection_lost` →
`request_close` → `_close` → `await self._task` from inside that very task
("Task cannot await on itself"). A done callback replaces it.

---

## 9. Logging & observability (`logging.py`) — **done**

- **Correction — "reuse Wijjit's `get_logger`" was an active bug**, not a style
  choice. Wijjit's `get_logger` prefixes `"wijjit."` only `if not
  name.startswith("wijjit")` — and `wijjit_ssh.server` *does*, so the prefix was
  never applied and every logger here landed as a **sibling** of the `wijjit`
  tree. `wijjit.configure_logging()` touches only the `wijjit` logger, so our
  records inherited none of its handlers and none of its `propagate = False`,
  including from `configure_logging(None)` — the "logging off" switch. They
  propagated to root, found no handler, and printed to stderr via
  `logging.lastResort`. In a process where a Wijjit TUI imports `wijjit_ssh`,
  that sprays across the alternate screen buffer and corrupts the frame.
  Verified before fixing; there is a regression test.
- `wijjit_ssh` therefore owns its own tree, with the conventional library
  posture: a `NullHandler` attached **at import** (`callHandlers` only falls back
  to `lastResort` with zero handlers on the chain), `propagate` left **True** so
  a host that configures root still receives us — which also keeps `caplog`
  working, so unlike Wijjit we need no `wijjit_caplog` workaround.
- `configure_logging()` is opt-in and defaults to silence. Only `run()` calls it
  (to stderr, and only if neither our tree nor root has a handler), because only
  `run()` owns the process. Same rule as signal handling.
- `SessionLog`, a `LoggerAdapter` binding session id + username + peer IP into
  every line. An adapter rather than a contextvar for a structural reason: the
  asyncssh callbacks run on the *connection* task, not the app task, so a
  contextvar set in the app task would be invisible from exactly the callbacks
  that need to log.
- Lifecycle at INFO (connect, auth ok/fail, session start/end + reason +
  duration), errors at ERROR with the session id. Credentials are never logged.
- `EventEmitter` / `on_event`: `connection.opened|closed|rejected`,
  `auth.ok|failed`, `session.started|rejected|ended`. A raising hook is logged
  and swallowed — a deployment's Prometheus counter must not be able to take a
  session down from inside an asyncssh callback.
- **Byte in/out counters deferred to M5**, where they share the
  `_ChannelWriter` seam with backpressure and that file gets touched once.

---

## 10. Server API surface (`config.py` + `server.py`) — **done**

```python
@dataclass
class ServerConfig:
    host: str = ""
    port: int = 8022
    host_keys: Sequence[HostKeySource] = ()     # paths, PathLike, or SSHKey
    auth: AuthPolicy | None = None
    allow_anonymous: bool = False               # must be True to run OpenAuth
    max_sessions: int = 100
    max_per_ip: int = 10                        # connections, not sessions (§8)
    connect_rate: float = 0.0                   # 0 disables; opt-in
    connect_burst: int = 20
    login_timeout: float = 30.0
    idle_timeout: float | None = 600.0
    session_timeout: float | None = None
    keepalive_interval: float = 30.0
    keepalive_count_max: int = 3
    shutdown_grace: float = 5.0
    banner: str | None = None                   # pre-auth SSH banner text
    on_event: EventHook | None = None           # metrics (§9)

class WijjitSSH:
    def __init__(self, app_factory: Callable[[SSHSession], Wijjit],
                 config: ServerConfig | None = None, **overrides): ...
    async def start(self, host=None, port=None) -> asyncssh.SSHAcceptor: ...
    async def run_async(self, host=None, port=None) -> None: ...  # until stop()
    def run(self, host=None, port=None) -> None: ...              # owns process
    async def stop(self, *, grace: float | None = None) -> None: ...
    @property
    def active_sessions(self) -> int: ...
```

Every limit ships with a real default: a limit that is opt-in is not a limit in
any deployment where nobody thought about it. The exception is `connect_rate`,
since rate-limiting an unmeasured service mostly throttles your own health check.

**Correction — `host_keys: list[str] | None`** would have broken every existing
call site; it is `Sequence[HostKeySource]` (§7).

**Correction — `**overrides` needs explicit field validation.** Under mypy strict
it is `Any`, so a typo'd `max_session=1` would be silently dropped — leaving an
operator believing a server is bounded when it is not, which is the worst
available failure mode for a limits API. Unknown names raise `TypeError` listing
the valid ones.

`app_factory` stays the SSH analogue of a Flask view. `SSHSession` gained
`session_id` and `peer_ip` (`term_type` was already there; `env` is still TODO).

**Graceful shutdown:** `stop()` closes the listener, drains sessions with a real
grace period, then closes the connections underneath them, then waits for the
listener. Idempotent under a lock; safe on a server that never started.

- **Correction — draining sessions is not enough to shut down.** A session's
  teardown closes its *channel*, but the SSH *connection* survives it and only
  its owner can close that. Since Python 3.12 changed
  `asyncio.Server.wait_closed()` to wait for every connection rather than just
  the listener, `stop()` hung until each client happened to give up. A real `ssh`
  client would have exited on channel close and masked it — which is exactly why
  a shutdown must not depend on client goodwill. `WijjitSSH` tracks live
  connections and disconnects them after the drain.
- **Signals only in `run()`.** §10 was right that the server owns the process —
  but only in `run()`. `run_async()` may be one coroutine inside a host
  application, and installing process-global handlers from a library coroutine is
  the same sin `owns_terminal = False` avoids in the backend. `run()` is likewise
  the only entry point that configures logging.
- **Correction — on Windows the "fallback" is the only path.**
  `ProactorEventLoop` has no `add_signal_handler` at all, so `signal.signal` +
  `call_soon_threadsafe` carries every Windows deployment. SIGTERM is never
  delivered there (`TerminateProcess` runs no handlers) and `CTRL_C_EVENT` cannot
  be delivered to a new process group, so the end-to-end SIGTERM test is
  POSIX-only and the Windows path is covered by testing handler installation and
  `_signal_stop` directly. Graceful shutdown on Windows is explicitly
  best-effort; §12's targets are systemd and Docker.

---

## 11. Testing strategy

- **`test_input_decoder.py`** — the priority. **[done: 165 cases]** Table-driven
  `bytes → [events]` for: ASCII, Ctrl+letters, all arrows (CSI + SS3),
  Home/End/PgUp/Dn/Del, F1–F12, modified keys (`ESC[1;5A`), SGR + X10 mouse,
  bracketed paste, split UTF-8, malformed/hostile input, and *every* case
  re-run one byte at a time to prove resumability. Pure, fast, no I/O.
- **`test_roundtrip.py`** — **[done: 21 tests]** in-process asyncssh client ↔
  `WijjitSSH`, generated host key, `known_hosts=None`. Covers initial frame,
  keystrokes, UTF-8, split escape sequences, the lone-ESC timer, resize,
  Ctrl+Q disconnect, a failing app factory, and two concurrent differently-sized
  sessions — plus, since M3, the limits *wiring*: max_sessions refusal text,
  pre-auth per-IP and rate rejection, idle/session timeouts, non-PTY refusal,
  banner, and metrics events. Client machinery lives in `tests/_client.py`, with
  a `serve` factory fixture in `conftest.py`.

  > **Trap worth knowing.** The client side must run a real VT emulator
  > (`pyte`), not `strip_ansi()` over the accumulated bytes. Wijjit uses a
  > **diff** renderer: after the first frame it re-sends only the cells that
  > changed, addressed by cursor position. So the wire carries a transcript, not
  > a picture — after a counter goes `N 0` → `N 1`, the bytes contain a bare
  > `1`, and asserting `"N 1" in text` never matches even though the client's
  > screen plainly reads `N 1`. Feeding the stream through `pyte` reconstructs
  > what the user actually sees, and has the bonus of validating that our escape
  > sequences are well-formed, since a real emulator has to accept them.
- **`test_auth.py`** — **[done: 30]** each preset in isolation with fake asyncssh
  callbacks, plus accept *and* reject over real SSH.
- **`test_limits.py`** — **[done: 33]** buckets, timers, registry, and drain,
  against an injected clock with no sockets. These carry the real assertions
  about limit behavior; the over-SSH tests above only prove the wiring. Same
  split as the decoder, and the reason the timing assertions here are exact
  rather than sleep-and-hope.
- **`test_keys.py`** (22), **`test_config.py`** (31), **`test_logging.py`** (18),
  **`test_shutdown.py`** (13) — **[done]**. Four tests are POSIX-only (three 0600
  mode-bit assertions in `test_keys.py`, the end-to-end SIGTERM drain in
  `test_shutdown.py`) and skip on Windows, the only machine this repo was
  developed on. **They first ran under CI in M4** — which is the main reason CI
  mattered here more than it usually does.
- **Concurrency smoke** — open K simultaneous clients with different sizes;
  assert each renders at its own size (proves task-local size isolation).
- **CI — [done, M4].** `.github/workflows/ci.yml`: pytest across 3.11–3.13 ×
  Linux/macOS/Windows, plus ruff/black/mypy-strict and coverage. asyncssh is a
  hard dep of this package so tests always have it. `pyte` is declared in the dev
  group — it was imported by the round-trip tests and undeclared, which would
  have broken CI the day it was switched on.

---

## 12. Deployment notes — **done**

Shipped as `deploy/` (systemd unit, Dockerfile, compose file, healthcheck)
plus `docs/source/guide/deployment.rst`. The outline below is what was
planned; the guide is what was built, and the M4 entry in §13 records what
the work turned up that this list did not anticipate.

- **Run:** `ensure_host_key("ssh_host_key")`, `WijjitSSH(make_app, auth=...).run()`.
- **systemd:** simple unit, `Restart=on-failure`, a dedicated unprotected user,
  host key under `StateDirectory`.
- **Docker:** copy app + host key volume; expose `8022`; healthcheck via a
  scripted asyncssh client.
- **Scaling:** stateless per-connection apps ⇒ run N instances behind a TCP
  load balancer; no shared state needed unless the app itself has a backend.
- **Security checklist:** real `auth` (never `allow_anonymous` in prod), keep
  `max_sessions`/`max_per_ip`/timeouts set, run as an unprivileged user on a
  high port (or `setcap`/reverse-proxy for 22), rotate host keys, no exec/sftp
  surface (guaranteed by design — we never implement those handlers).

---

## 13. Milestones

- **M1 — Byte parser. [DONE]** `KeyDecoder` + `ChannelInputSource`; backend
  switched to `encoding=None`; reader thread and prompt_toolkit pipe dropped from
  the remote path. Core gained the `InputSource` protocol (§2). Also hardened: a
  raising app factory now reports to the client and logs, instead of dropping the
  connection silently.
- **M2 — Auth. [DONE]** `AuthPolicy` + `AuthorizedKeys` / `PasswordAuth` /
  `ChainAuth` / `OpenAuth`; every asyncssh auth callback forwarded to the policy;
  fail-closed construction (`allow_anonymous=True` required to run open);
  constant-time `check_password`; auth attempts logged (never the credential).
  30 tests in `test_auth.py`, including accept **and reject** over real SSH.
- **M3 — Robust lifecycle. [DONE]** `keys.py` (§7), `limits.py` (§8),
  `logging.py` (§9), `config.py` (§10, pulled forward from M4 — M3 introduced the
  twelve knobs it exists to hold, and threading them as kwargs first would have
  broken the public signature twice). Graceful shutdown with a real drain,
  per-session logging + metrics hook, idle/absolute timeouts, keepalive, non-PTY
  refusal (§6). Also rebuilt the session teardown, which had been ending every
  session by cancellation (§8). Landed as seven commits, suite green at each.
- **M4 — Packaging, docs & polish.** Landed in slices.
  - **Packaging + CI. [DONE]** Metadata a package needs and did not have:
    `LICENSE` (MIT), classifiers, `py.typed` (the tree is mypy-strict clean but
    shipped no PEP 561 marker, so every downstream checker silently ignored its
    annotations), a version single-sourced from `__init__.py` via
    `[tool.hatch.version]`, an explicit sdist include list, and the removal of
    `prompt-toolkit` from `dependencies` — dead since M1, which is exactly when
    the pin should have gone. `.gitignore` had a real hole: the rules were
    `*.key` and `host_key*`, but the examples write **`ssh_host_key`**, whose
    basename matched neither, so running the documented demo from the repo root
    left an untracked private host key that `git add .` would commit.
    `.github/workflows/ci.yml` runs test (3.11–3.13 × Linux/macOS/Windows), lint
    (ruff + black + mypy over `src/`, `tests/`, and `examples/`), and coverage.
    It is uv-based rather than a copy of wijjit's pip-based workflow, because dev
    deps here are PEP 735 `[dependency-groups]`, which `pip install -e ".[dev]"`
    cannot see at all. Since wijjit is not on PyPI, each job checks out **both**
    repos as siblings so the existing `../wijjit` path source resolves unchanged
    — CI is then byte-for-byte the layout the README documents for local
    development, and collapses to a single checkout the day wijjit publishes.
    **This is the first time the four POSIX-only tests have ever run.**
  - **Docs site. [DONE]** A Sphinx site under `docs/`, same stack as wijjit
    (`sphinx-rtd-theme` / `copybutton` / `tabs`, `docs/Makefile` + `make.bat`),
    for consistency across the two projects. Structure is deliberately lean:
    index, install + quickstart, six guide pages (auth, host keys, limits,
    shutdown, logging, terminal input), and an autodoc reference over the eight
    modules. The reference is nearly free — the module docstrings were already
    thorough NumPy-style prose, so `napoleon` + `autodoc` render what is there
    rather than needing new text written. Intersphinx points at CPython,
    asyncssh, and wijjit's own Pages site, so the `:class:` references those
    docstrings are dense with become links. Sphinx and its theme sit in their own
    PEP 735 `docs` group rather than in `dev`, so a contributor running the tests
    does not install two dozen packages they have no use for.
    `.github/workflows/docs.yml` builds with `-W` (warnings are errors; the build
    is clean) on both pushes to main and pull requests, and deploys to Pages only
    from main — so a docs change that breaks the build reddens the PR rather than
    the deploy. It reuses ci.yml's two-checkout arrangement, since autodoc has to
    import `wijjit_ssh`, which imports `wijjit`.
  - **Examples. [DONE for the first two.]** The original plan said "second
    example: auth + multiple views", which would have demonstrated nothing a
    local Wijjit app does not. The gap worth closing is the one that only exists
    over SSH: **N live apps in one process, sharing state, pushed to from outside
    their own task.** So there are two, from opposite ends of it —
    `dashboard_ssh.py` (one sampler fanning out to N viewers) and `chat_ssh.py`
    (N writers fanning out to N viewers) — plus a `docs/source/examples/` section
    carrying the shared explanation once rather than twice.

    Two things came out of building them that the spec had not anticipated:

    - **`on_event` is the unsubscribe hook**, not just a metrics hook. A shared
      hub holding apps must drop them, the factory has no teardown callback, and
      the obvious in-app signal races: `session_started` calls the factory and
      *then* `ensure_future(_run_app())`, so a subscriber exists while
      `app.running` is still `False`, and a broadcast landing in that window
      would evict a live session. `session.ended` has no such gap and covers
      every exit — quit, idle timeout, `connection_lost`, drain. Both examples
      use it; both were verified against an aborted connection, not just a
      polite Ctrl+Q.
    - **Cross-session push has a latency floor** set by the event loop's input
      poll: `REFRESH_INTERVAL / 2`, or the 0.5s fallback when it is unset. That
      makes `REFRESH_INTERVAL` the dial for any multi-session app, at the cost of
      redrawing on that cadence whether or not anything changed. Documented on
      the examples index; worth re-measuring in M5's load test alongside the
      `max_sessions` sizing rule.

    Also fixed in passing: `hello_ssh.py`'s Greet button had **never worked**.
    Action handlers are always called with the `ActionEvent`, and the handler
    took no parameters, so every press raised `TypeError` into
    `_dispatch_action`'s catch and the counter stayed at 0. It was the repo's
    only example and the README's headline demo. Nothing tests the examples —
    which is the argument for the tests that should ship with `ide_ssh.py`.
  - **Examples, remaining. [TODO]** `ide_ssh.py`: `{% splitpanel %}` +
    `{% tree %}` + `{% codeeditor %}` over `wijjit.helpers.load_filesystem_tree`,
    with execution as a no-shell subprocess — fixed interpreter argv, every path
    `resolve()`d and checked with `is_relative_to` against a whitelist of roots,
    `cwd` pinned inside the root, wall-clock timeout, output capped. The framing
    is the point: this package guarantees *no* exec surface (§1), and the example
    is what deliberately reopening one looks like when the trust boundary is
    somewhere you can see it. **Ships with tests** for the path sandbox — it is
    the first example with a security boundary in it. `store_ssh.py`: catalog and
    per-user cart, with checkout deliberately out of band (a payment-link URL
    rendered as text), because card data must never touch the SSH session.
  - **Deployment. [DONE]** The §12 material, as real artifacts rather than
    snippets: `deploy/` holds a sandboxed systemd unit, a non-root Dockerfile, a
    compose file, and `healthcheck.py`, with `docs/source/guide/deployment.rst`
    describing the choices in them and carrying the security checklist.

    The healthcheck is the piece that turned out to have a real argument in it.
    The obvious probe — a TCP connect — is a false-healthy: a process that
    accepted the socket and then wedged still completes the handshake, because
    the kernel does it without the application ever being scheduled. So the probe
    completes the SSH version and key exchange (which needs a live event loop, a
    loadable host key, and a working transport) and then offers no credentials at
    all, treating the resulting `PermissionDenied` as the success condition:
    being told "no" proves the server reached its auth policy. All three exit
    paths were verified against a live server. It never authenticates, so it
    never starts a session and never counts against `max_sessions` — but it *is*
    an ordinary connection, so it counts against `max_per_ip` and `connect_rate`,
    which is a real way to configure a healthy server into reporting itself dead.

    The other thing worth recording is that the supervisor can undo §8's drain
    entirely. If `TimeoutStopSec` (systemd) or `stop_grace_period` (compose) is
    shorter than `shutdown_grace`, the supervisor sends SIGKILL mid-drain, no
    session runs its teardown, and every connected user keeps a wedged terminal —
    the exact outcome the grace period exists to prevent. Both files set it high
    and say why.
  - **Release pipeline. [DONE]** Not in the original M4 list, but a package that
    cannot be published is not packaged. `release.yml` publishes on a `v*` tag
    through PyPI Trusted Publishing, and its `verify` job encodes the
    preconditions that are easy to forget: tag matches `__version__`, the
    changelog has a matching section, `py.typed` is in the wheel, and
    `[tool.uv.sources]` is gone. That last one is the load-bearing check —
    while the path source exists, `wijjit>=0.1.0` has never been resolved from
    the real index by anything in this repo, and PyPI does not allow a version
    number to be reused once that is discovered. `RELEASING.md`, `CONTRIBUTING.md`,
    and `SECURITY.md` cover the procedure, the conventions, and the trust
    boundary respectively.
- **M5 — Hardening pass.** Backpressure handling + the §9 byte counters (shared
  `_ChannelWriter` seam), bracketed-paste + mouse edge cases, fuzz the decoder,
  load test (hundreds of concurrent sessions).

Each milestone is independently shippable; M1 alone made the prototype
production-shaped on the hot path, and M3 makes it deployable.

---

## 14. Open questions

- **ESC timeout value** — 30–50 ms is typical; confirm against real latency
  over WAN SSH. Could be adaptive.
- **Wide chars / emoji** — Wijjit's screen buffer treats wide chars as
  single-width (documented limitation). SSH doesn't change that; noted in the
  README.
- **Windows clients** — PuTTY/Windows Terminal key encodings differ (esp. Alt,
  function keys); the decoder table must be tested against them.
- **Per-session executor** — should the server hand each app a `ThreadPoolExecutor`
  for blocking sync handlers, or document "keep handlers async"? Leaning toward
  a shared, bounded executor with a config knob.
- **Reconnect / session resume** — out of scope, but worth a design note if
  mobile clients (flaky links) become a target.

Raised by M3:

- **Default limits are guesses.** 100 sessions / 10 per IP / 600s idle are
  plausible but unmeasured. A session is a live Wijjit app, so `max_sessions` is
  really a memory bound and the honest default depends on the app. Worth
  measuring in M5's load test and documenting a sizing rule rather than a number.
- **`session.ended` fires after `connection.closed`** when the peer vanishes,
  because `connection_lost` is synchronous while the session teardown is a task.
  Harmless for counters, mildly surprising for anyone reconstructing a timeline.
- **Per-IP limits and NAT.** `max_per_ip=10` counts a shared corporate egress IP
  as one peer. Fine for abuse resistance, wrong for a team behind one NAT. If
  that bites, the answer is probably a per-*user* cap post-auth, alongside rather
  than instead of the per-IP one.
- **`ensure_host_key` on a non-persistent volume** silently mints a new identity
  every restart; all we can do is log at WARNING (§7). A startup check that
  refuses to generate unless `WIJJIT_SSH_ALLOW_HOST_KEY_GEN=1` might be worth it
  for containers, but it trades DX for a footgun that logging already covers.
