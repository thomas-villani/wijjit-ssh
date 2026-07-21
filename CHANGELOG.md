# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing released yet. `wijjit-ssh` is at `0.0.1` and cannot publish until
[`wijjit`](https://github.com/thomas-villani/wijjit) 0.1.0 is on PyPI, since
`pyproject.toml` still resolves it from a sibling checkout. The work below is the
history from the original prototype to a deployable server, by milestone
(see [`spec.md`](spec.md)).

### Added

- **Async byte-parser input path (M1).** `KeyDecoder`, a resumable, side-effect-free
  `bytes -> Key | MouseEvent` state machine, and `ChannelInputSource`, which feeds it
  from the SSH channel on the event loop. Handles split escape sequences, UTF-8 runes
  split across packets, CSI and SS3 keys with modifiers, SGR and legacy X10 mouse,
  bracketed paste, and the lone-ESC ambiguity. Replaces the prototype's per-session
  reader thread and prompt_toolkit pipe.
- **Binary channel (M1).** The server channel is opened with `encoding=None`, so the
  decoder sees exactly the bytes the client sent.
- **Pluggable authentication (M2).** `AuthPolicy` with `AuthorizedKeys`,
  `PasswordAuth`, `ChainAuth`, and the development-only `OpenAuth`; every asyncssh
  auth callback is forwarded to the policy. Construction is fail-closed:
  `WijjitSSH` raises without a policy unless `allow_anonymous=True` is passed.
  `check_password` provides a constant-time comparison. Credentials are never logged.
- **Host keys (M3).** `ensure_host_key` generates and persists an ed25519 key on first
  run (written `0600` from creation via `O_CREAT | O_EXCL`, so there is no window where
  the server's identity is world-readable, and so two processes starting together
  cannot race); `load_host_keys` loads keys managed out of band; `resolve_host_keys`
  normalises paths, `PathLike`, and live `SSHKey` objects. Resolution is eager at
  construction, so a bad path fails where the server is configured.
- **Resource limits (M3).** `SessionRegistry` enforcing `max_sessions` (post-auth, at
  `session_requested`), `max_per_ip` connections and a `connect_rate` token bucket
  (both pre-auth), plus `login_timeout`, `idle_timeout`, `session_timeout`, and
  keepalives. On by default, because a limit that is opt-in is not a limit in any
  deployment where nobody thought about it. Refused clients get an explanatory
  message rather than a bare protocol error.
- **`ServerConfig` (M3).** One validated dataclass holding every knob, with unknown
  keyword overrides raising `TypeError` rather than being silently dropped.
- **Graceful shutdown (M3).** `stop()` closes the listener, drains live sessions with a
  real grace period so each app's teardown restores the client's terminal, then closes
  the connections underneath them. Idempotent, lock-guarded, and safe on a server that
  never started. `run()` wires it to SIGINT/SIGTERM; `start()`/`run_async()`
  deliberately install no process-global handlers so the server can be embedded.
- **Per-session logging and metrics (M3).** A `wijjit_ssh` logger tree with a
  `NullHandler` at import, `SessionLog` binding session id / username / peer IP into
  every line, and an `on_event` hook for `connection.*`, `auth.*`, and `session.*`.
  A hook that raises is logged and swallowed rather than taking a session down.
- **Non-PTY refusal.** A session that never requests a PTY is declined with a friendly
  message; this server only serves interactive TUIs.

### Fixed

- **Session teardown ended every session by cancellation.** `connection_lost` called
  `app.quit()` and `task.cancel()` in the same tick, but `quit()` only sets a flag the
  event loop reads on its next pass, so the cancel always won. Harmless when the peer
  had already gone, wrong for idle timeout and shutdown, where the channel is still
  alive and the app's `finally` is what restores the user's terminal.
- **The idle-timeout notice landed inside the alternate screen buffer.** The message
  has to be written *after* the app's teardown emits `ESC[?1049l`, not before, or the
  diff renderer paints over it.
- **`wijjit_ssh` loggers escaped to stderr.** Reusing Wijjit's `get_logger` applied its
  `"wijjit."` prefix only when the name did not already start with `wijjit` — which
  `wijjit_ssh.server` does. Every logger here landed as a sibling of the `wijjit` tree,
  inheriting none of its handlers and none of its `propagate = False`, so records fell
  through to `logging.lastResort` and sprayed across any local TUI's screen.
- **Pre-auth rejections corrupted the SSH banner.** Disconnecting inline from
  `connection_made` puts `MSG_DISCONNECT` ahead of the `SSH-2.0-` version string;
  the rejection is now deferred a tick with `loop.call_soon`, and reaches the client
  as a proper `DisconnectError` carrying our text.
- **`stop()` hung until clients gave up.** Draining sessions closes channels, but the
  SSH connection outlives them and only its owner can close it — and Python 3.12
  changed `asyncio.Server.wait_closed()` to wait for every connection. The server now
  tracks live connections and disconnects them after the drain.
- **A raising `app_factory` dropped the connection silently.** It now reports to the
  client and logs.

[Unreleased]: https://github.com/thomas-villani/wijjit-ssh/commits/main
