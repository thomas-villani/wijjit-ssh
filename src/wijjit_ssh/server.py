"""A "Flask for SSH apps" server: expose a Wijjit app over SSH.

:class:`WijjitSSH` wraps ``asyncssh`` so that each incoming SSH connection gets
its own Wijjit application, driven through a
:class:`~wijjit_ssh.backend.RemoteTerminalBackend`. You supply a factory - the
SSH analogue of a Flask view - that builds an app per connection:

>>> from wijjit import Wijjit, render_template_string
>>> from wijjit_ssh import WijjitSSH
>>> from wijjit_ssh.auth import AuthorizedKeys
>>>
>>> def make_app(session):
...     app = Wijjit(backend=session.backend)
...     @app.view("main", default=True)
...     def main():
...         return render_template_string(
...             "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}",
...             who=session.username,
...         )
...     return app
>>>
>>> WijjitSSH(
...     make_app,
...     host_keys=[ensure_host_key("ssh_host_key")],
...     auth=AuthorizedKeys("~/.ssh/authorized_keys"),
... ).run(port=8022)

Then ``ssh -p 8022 you@localhost`` drops the client straight into the TUI.

Authentication is **fail-closed**: constructing :class:`WijjitSSH` without an
``auth`` policy raises unless ``allow_anonymous=True`` is passed explicitly. See
:mod:`wijjit_ssh.auth`.

Resources are **bounded by default**: concurrent sessions, connections per IP,
idle time, and login time all have limits without being asked for. See
:class:`~wijjit_ssh.config.ServerConfig` to tune them and :mod:`wijjit_ssh.limits`
for how they are enforced.

Not yet hardened
----------------
* No backpressure handling: a client that stops reading buffers frames in
  asyncssh without bound (M5).
* Blocking sync handlers stall that session's frames; give each app an executor
  (``EXECUTOR``) for CPU-bound work.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Optional

try:
    import asyncssh
except ImportError as exc:  # pragma: no cover - asyncssh is an optional dep
    raise ImportError(
        "wijjit-ssh requires asyncssh. Install it with: pip install asyncssh"
    ) from exc

from wijjit import Wijjit
from wijjit.terminal.size import set_terminal_size

from wijjit_ssh.auth import AuthPolicy, OpenAuth
from wijjit_ssh.backend import RemoteTerminalBackend
from wijjit_ssh.config import ServerConfig
from wijjit_ssh.keys import fingerprint, resolve_host_keys
from wijjit_ssh.limits import IdleTimer, Rejection, SessionRegistry
from wijjit_ssh.logging import (
    EventEmitter,
    configure_logging,
    get_logger,
    logging_is_configured,
    new_session_id,
    session_logger,
)

logger = get_logger(__name__)


@dataclass
class SSHSession:
    """Context passed to the per-connection app factory.

    Attributes
    ----------
    username : str
        Username the client authenticated as.
    term_type : str
        Client's ``TERM`` (e.g. ``"xterm-256color"``).
    columns : int
        Negotiated terminal width.
    lines : int
        Negotiated terminal height.
    backend : RemoteTerminalBackend
        The transport for this connection. **Pass it to the app**:
        ``Wijjit(backend=session.backend)`` - this is what routes the app's I/O
        to the SSH channel instead of the server's console.
    conn : asyncssh.SSHServerConnection
        The underlying connection, for advanced use.
    session_id : str
        Short correlation id, matching the one in this session's log lines.
        Worth surfacing in an app's own logs or error messages: it is what ties
        a user's report back to the server-side record.
    peer_ip : str
        Client address, e.g. for per-user rate limiting inside the app.
    """

    username: str
    term_type: str
    columns: int
    lines: int
    backend: RemoteTerminalBackend
    conn: "asyncssh.SSHServerConnection"
    session_id: str = ""
    peer_ip: str = ""


AppFactory = Callable[[SSHSession], Wijjit]


def _format_seconds(seconds: float | None) -> str:
    """Render a timeout for a person: "10 minutes", "90s", "0.4s".

    Timeouts here span three orders of magnitude - a 0.4s test value and a
    600s production one - so a single format is wrong for one of them. ("%.0f"
    turns 0.4s into a baffling "Disconnected after 0s of inactivity.")

    Parameters
    ----------
    seconds : float or None
        The duration. None renders as ``"the configured time"``, since a
        disabled timeout should never reach a message anyway.

    Returns
    -------
    str
    """
    if seconds is None:  # pragma: no cover - a disabled timeout cannot expire
        return "the configured time"
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds / 60:g} minutes"
    return f"{seconds:g}s"


class _RejectedSession(asyncssh.SSHServerSession[bytes]):
    """A session that exists only to tell the client why it was refused.

    asyncssh lets ``session_requested`` return a falsy value to refuse, but that
    path raises ``ChannelOpenError(OPEN_CONNECT_FAILED, 'Session refused')`` with
    no way to attach text - the client sees a bare protocol error and has no idea
    whether the server is full, whether it is broken, or whether they should try
    again. So a refusal is a real session object that writes its reason and
    exits, which costs one channel and produces an error a person can act on.

    Parameters
    ----------
    rejection : Rejection
        Why this session was refused.
    """

    def __init__(self, rejection: Rejection) -> None:
        self._rejection = rejection
        self._chan: asyncssh.SSHServerChannel[bytes] | None = None

    def connection_made(self, chan: asyncssh.SSHServerChannel[bytes]) -> None:
        self._chan = chan

    def pty_requested(
        self,
        term_type: str,
        term_size: tuple[int, int, int, int],
        term_modes: Mapping[int, int],
    ) -> bool:
        # Accept the pty so the client proceeds to shell_requested and gets as
        # far as session_started, where it can actually be told what happened.
        return True

    def shell_requested(self) -> bool:
        return True

    def session_started(self) -> None:
        if self._chan is None:  # pragma: no cover - defensive
            return
        try:
            self._chan.write(f"\r\n{self._rejection.message}\r\n".encode())
            self._chan.exit(1)
        except Exception:  # pragma: no cover - the peer may already be gone
            self._chan.close()


class _WijjitSSHSession(asyncssh.SSHServerSession[bytes]):
    """Bridges one SSH channel to one Wijjit app instance.

    Generic over ``bytes``: the channel is opened with ``encoding=None`` so this
    session sees the client's raw byte stream (see :mod:`wijjit_ssh.input`).

    Satisfies :class:`~wijjit_ssh.limits.ManagedSession`, which is how the
    registry closes it on shutdown without importing anything from this module.

    Parameters
    ----------
    app_factory : AppFactory
        Builds the app for this connection.
    conn : asyncssh.SSHServerConnection
        The owning connection (source of the authenticated username).
    config : ServerConfig
        Timeouts and limits for this session.
    registry : SessionRegistry
        Where this session deregisters itself when it ends.
    emitter : EventEmitter
        Metrics hook.
    session_id : str
        Correlation id, already allocated by the caller (which registered us).
    peer_ip : str
        Client address.
    """

    def __init__(
        self,
        app_factory: AppFactory,
        conn: asyncssh.SSHServerConnection,
        *,
        config: ServerConfig,
        registry: SessionRegistry,
        emitter: EventEmitter,
        session_id: str,
        peer_ip: str,
    ) -> None:
        self._app_factory = app_factory
        self._conn = conn
        self._config = config
        self._registry = registry
        self._emitter = emitter

        # -- ManagedSession protocol --
        self.session_id = session_id
        self.peer_ip = peer_ip
        self.username: str = conn.get_extra_info("username") or "anonymous"
        self.started_at = time.monotonic()

        self._log = session_logger(session_id, self.username, peer_ip)
        self._chan: asyncssh.SSHServerChannel[bytes] | None = None
        self._term_type: str = "xterm"
        self._size: tuple[int, int] = (80, 24)
        self._pty_requested = False
        self._backend: Optional[RemoteTerminalBackend] = None
        self._app: Optional[Wijjit] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._closing: Optional[asyncio.Task[None]] = None
        self._timer = IdleTimer(
            idle_timeout=config.idle_timeout,
            session_timeout=config.session_timeout,
            on_expire=self._expired,
        )

    # -- asyncssh session callbacks --------------------------------------------

    def connection_made(self, chan: asyncssh.SSHServerChannel[bytes]) -> None:
        self._chan = chan

    def pty_requested(
        self,
        term_type: str,
        term_size: tuple[int, int, int, int],
        term_modes: Mapping[int, int],
    ) -> bool:
        # term_size is (width, height, pixwidth, pixheight).
        self._pty_requested = True
        self._term_type = term_type or "xterm"
        width, height = term_size[0] or 80, term_size[1] or 24
        self._size = (width, height)
        return True

    def shell_requested(self) -> bool:
        return True

    def session_started(self) -> None:
        # Runs on the event loop. Seed the size override BEFORE building the app
        # so Wijjit.__init__ (which reads the terminal size) sees this client's
        # dimensions, then start the app in its own task, which inherits this
        # context (including the size override) at creation time.
        if not self._pty_requested:
            # No pty means `ssh host command` or a piped stdin. This server only
            # ever runs a TUI, which needs a terminal to draw on; without one the
            # app would render escape sequences into a pipe. Say so rather than
            # failing obscurely somewhere in the renderer.
            self._log.info("Rejected a session with no pty")
            self._emitter.emit(
                "session.rejected", peer_ip=self.peer_ip, reason="no_pty"
            )
            self._fail(
                "This server only serves interactive terminal applications, and "
                "your client did not request a terminal. Connect without a "
                "command (ssh -t if your client needs persuading)."
            )
            return

        cols, lines = self._size

        self._backend = RemoteTerminalBackend(self._chan, cols, lines)

        # Seed the size override before the factory runs so Wijjit.__init__ sizes
        # its managers to this client. The factory must wire the backend into the
        # app (Wijjit(backend=session.backend)); doing so is what points the
        # app's screen/input at the channel.
        set_terminal_size(cols, lines)
        session = SSHSession(
            username=self.username,
            term_type=self._term_type,
            columns=cols,
            lines=lines,
            backend=self._backend,
            conn=self._conn,
            session_id=self.session_id,
            peer_ip=self.peer_ip,
        )

        # A raising factory (a template typo, a bad key binding, a failed DB
        # connection) must not become a silent disconnect: asyncssh would swallow
        # the exception and the client would just see the connection drop with no
        # clue why. Log it server-side and tell the client something actionable.
        try:
            app = self._app_factory(session)
            if app._backend is not self._backend:
                raise RuntimeError(
                    "The app factory must pass the session backend to the app: "
                    "Wijjit(backend=session.backend)."
                )
        except Exception as exc:
            self._log.exception("App factory failed")
            self._fail(f"Failed to start application: {exc}")
            return

        self._app = app
        self._task = asyncio.ensure_future(self._run_app())
        # When the app exits on its own (the user quit, or it crashed), tear the
        # session down through the same path everything else uses. Deliberately
        # a done callback rather than a chan.close() inside _run_app's finally:
        # closing there fires connection_lost, which routes back into _close,
        # which awaits the app task -- from inside that very task. This callback
        # only runs once the task is already complete, so the await returns
        # immediately and there is no way to await ourselves.
        self._task.add_done_callback(self._app_finished)

        self._timer.start()
        self._log.info("Session started (term=%s, %dx%d)", self._term_type, cols, lines)
        self._emitter.emit(
            "session.started",
            session_id=self.session_id,
            username=self.username,
            peer_ip=self.peer_ip,
        )

    def _write(self, message: str) -> None:
        """Write a plain message to the (binary) channel, ignoring a dead peer.

        Parameters
        ----------
        message : str
            Text to send; encoded to UTF-8 at this boundary.
        """
        if self._chan is None:
            return
        try:
            self._chan.write(message.encode("utf-8", errors="replace"))
        except Exception:  # pragma: no cover - the peer may already be gone
            pass

    def _fail(self, message: str) -> None:
        """Report a startup failure to the client and close the session.

        For failures *before* the app exists (no pty, a raising factory). Writes
        first and closes, unlike :meth:`_close`: with no app there is no
        alternate screen buffer to escape, so the message lands on an ordinary
        terminal and there is nothing to wait for.

        Parameters
        ----------
        message : str
            Human-readable reason, shown on the client's terminal.
        """
        self._write(f"\r\n{message}\r\n")
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:  # pragma: no cover - defensive
                pass
        # We were registered at session_requested, so a session that never
        # started still holds a slot until it is released.
        self._registry.release(self)

    async def _run_app(self) -> None:
        assert self._app is not None  # only started once the factory succeeded
        try:
            # Enter Wijjit's async loop directly (we are already on an event
            # loop; app.run() would try to start a new one via asyncio.run).
            await self._app.event_loop.run_async()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("Application crashed")
            self._write(f"\r\nApplication error: {exc}\r\n")

    def _app_finished(self, task: asyncio.Task[None]) -> None:
        """The app's task ended on its own; take the session down with it."""
        self.request_close("app_exited")

    def _expired(self, reason: str) -> None:
        """An idle or absolute deadline landed."""
        self._log.info("Closing session: %s", reason)
        if reason == "idle_timeout":
            limit = _format_seconds(self._config.idle_timeout)
            message = f"Disconnected after {limit} of inactivity."
        else:
            limit = _format_seconds(self._config.session_timeout)
            message = f"Disconnected after reaching the {limit} session limit."
        self.request_close(reason, message)

    def data_received(self, data: bytes, datatype: object) -> None:
        # Binary channel: `data` is raw bytes straight off the wire, which the
        # backend hands to the key/mouse decoder on this same event loop.
        self._timer.poke()
        if self._backend is not None:
            self._backend.feed(data)

    def terminal_size_changed(
        self, width: int, height: int, pixwidth: int, pixheight: int
    ) -> None:
        if self._backend is not None:
            self._backend.resize(width, height)

    def eof_received(self) -> bool:
        return False

    def connection_lost(self, exc: Optional[Exception]) -> None:
        # The peer is already gone, so there is nobody to say goodbye to and no
        # point waiting for a graceful exit: no grace, no message.
        self.request_close("connection_lost")

    # -- ManagedSession -------------------------------------------------------

    def request_close(self, reason: str, message: str | None = None) -> None:
        """Begin an orderly shutdown of this session. Idempotent.

        The single way a session ends, whoever decided it: the client vanished,
        a deadline fired, the app quit, or the server is draining.

        Parameters
        ----------
        reason : str
            Slug for logs and metrics, e.g. ``"idle_timeout"``.
        message : str, optional
            Text to show the client, once the app has released the terminal.
        """
        if self._closing is not None:
            return  # already on the way down
        self._timer.cancel()
        self._closing = asyncio.ensure_future(self._close(reason, message))

    def abort(self) -> None:
        """Force this session down now, having declined to leave on request."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._chan is not None:
            with suppress(Exception):
                self._chan.close()

    async def _close(self, reason: str, message: str | None) -> None:
        """Stop the app, tell the client, close the channel, deregister.

        Ordering here is the whole point, and it is not the obvious one.

        ``app.quit()`` only sets a flag; the app is parked in
        ``read_input_async``, and its loop notices on the next pass (within
        ~0.5s, which is the read timeout Wijjit uses). Waiting for it - rather
        than cancelling immediately, which is what this code used to do - is
        what lets the app's own ``finally`` run: leave the alternate screen
        buffer, show the cursor, reset SGR. Skip that and the client is left
        looking at a frozen frame with no cursor.

        That is also why ``message`` is written *after* the task finishes rather
        than before. spec.md §8 says idle timeout should "notify + close", which
        reads as write-then-close, but a write while the app still owns the
        screen lands inside the TUI frame and gets painted over by the next
        repaint. Only once the app has exited the alternate buffer is there an
        ordinary screen to write a message on.
        """
        # The peer is gone in the connection_lost case, so waiting for a clean
        # exit would just delay reclaiming the slot for no one's benefit.
        grace = 0.0 if reason == "connection_lost" else self._config.shutdown_grace

        if self._app is not None:
            self._app.quit()

        task = self._task
        if task is not None and task is not asyncio.current_task():
            if grace > 0:
                # wait() does not cancel on timeout, unlike wait_for(): we want
                # to know whether it finished, then decide.
                await asyncio.wait([task], timeout=grace)
            if not task.done():
                self._log.warning("App did not exit within %.1fs; cancelling it", grace)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        if message:
            self._write(f"\r\n{message}\r\n")

        if self._chan is not None:
            with suppress(Exception):
                self._chan.close()

        self._registry.release(self)
        duration = time.monotonic() - self.started_at
        self._log.info("Session ended after %.1fs: %s", duration, reason)
        self._emitter.emit(
            "session.ended",
            session_id=self.session_id,
            username=self.username,
            peer_ip=self.peer_ip,
            reason=reason,
            duration=duration,
        )


class _WijjitSSHServer(asyncssh.SSHServer):
    """asyncssh server that mints a Wijjit session per connection.

    Every authentication callback asyncssh offers is forwarded to the
    :class:`~wijjit_ssh.auth.AuthPolicy`, so credentials live in the policy and
    never in this glue. Admission control lives here too: this is the only place
    that sees a connection before its key exchange.

    Parameters
    ----------
    app_factory : AppFactory
        Builds the app for each session.
    auth : AuthPolicy
        How to authenticate this connection.
    config : ServerConfig
        Limits and timeouts.
    registry : SessionRegistry
        Shared across connections; the source of truth for what is live.
    emitter : EventEmitter
        Metrics hook.
    """

    def __init__(
        self,
        app_factory: AppFactory,
        auth: AuthPolicy,
        *,
        config: ServerConfig,
        registry: SessionRegistry,
        emitter: EventEmitter,
        live: set["_WijjitSSHServer"] | None = None,
    ) -> None:
        self._app_factory = app_factory
        self._auth = auth
        self._config = config
        self._registry = registry
        self._emitter = emitter
        # The owning server's set of live connections, so shutdown can find us.
        # A session's teardown closes its channel, which leaves the SSH
        # connection underneath it open; only the connection's owner can close
        # that, and stop() has to, or it would be waiting on a client to
        # volunteer.
        self._live = live
        self._conn: asyncssh.SSHServerConnection | None = None
        self._peer_ip: str = "unknown"
        # Cached at connection_made: asyncssh drops peername once the transport
        # is gone, so reading it lazily would log "Connection closed from None"
        # - losing the address on the one line most likely to be grepped for.
        self._peer_name: str = "unknown"
        self._counted = False

    def disconnect(self, message: str) -> None:
        """Drop this connection, telling the client why. Idempotent."""
        if self._conn is None:
            return
        with suppress(Exception):
            self._conn.disconnect(asyncssh.DISC_BY_APPLICATION, message)

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        self._peer_ip = self._peer_address()
        self._peer_name = self._read_peer_name()

        rejection = self._registry.check_connection(self._peer_ip)
        if rejection is not None:
            logger.warning(
                "Refused connection from %s: %s", self._peer(), rejection.reason
            )
            self._emitter.emit(
                "connection.rejected",
                peer_ip=self._peer_ip,
                reason=rejection.reason,
            )
            # Deferred, not immediate, and the delay is load-bearing. asyncssh
            # calls this callback from its own connection_made, one line before
            # it sends the "SSH-2.0-..." version banner. Disconnecting now would
            # put a binary MSG_DISCONNECT on the wire ahead of the banner (the
            # pre-kex deferral gate does not catch it, being packet type 1) and
            # would null the transport out from under the banner write that
            # follows. Waiting a tick lets the banner go first, after which
            # MSG_DISCONNECT is legal and OpenSSH renders it as a readable
            # "Received disconnect ...: Too many connections".
            asyncio.get_running_loop().call_soon(
                self._disconnect_now, rejection.message
            )
            return

        self._registry.connection_opened(self._peer_ip)
        self._counted = True
        if self._live is not None:
            self._live.add(self)
        logger.info("Connection from %s", self._peer())
        self._emitter.emit("connection.opened", peer_ip=self._peer_ip)

    def _disconnect_now(self, message: str) -> None:
        """Send a disconnect with a reason the client will print."""
        if self._conn is None:  # pragma: no cover - defensive
            return
        try:
            self._conn.disconnect(asyncssh.DISC_TOO_MANY_CONNECTIONS, message)
        except Exception:  # pragma: no cover - peer may have gone already
            with suppress(Exception):
                self._conn.abort()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if self._live is not None:
            self._live.discard(self)
        # Only release what we counted: a refused connection never took a slot,
        # and releasing one it never held would loosen the limit for that peer.
        if self._counted:
            self._registry.connection_closed(self._peer_ip)
            self._counted = False
            logger.info("Connection closed from %s", self._peer())
            self._emitter.emit("connection.closed", peer_ip=self._peer_ip)

    def _peer_address(self) -> str:
        """The client's IP, for limits and logs."""
        if self._conn is None:  # pragma: no cover - defensive
            return "unknown"
        peer = self._conn.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 1:
            return str(peer[0])
        return str(peer)

    def _read_peer_name(self) -> str:
        """Read the client's ``host:port`` off the live transport."""
        if self._conn is None:  # pragma: no cover - defensive
            return "unknown"
        peer = self._conn.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return str(peer) if peer else "unknown"

    def _peer(self) -> str:
        """The client's address, for logs. Valid after the transport is gone."""
        return self._peer_name

    # -- authentication (delegated to the policy) --------------------------------

    def begin_auth(self, username: str) -> bool:
        if self._config.banner and self._conn is not None:
            # Pre-auth, so every stranger who reaches the port reads this,
            # whether or not they get in. Legal notices, not secrets.
            with suppress(Exception):
                self._conn.send_auth_banner(self._config.banner)

        required = self._auth.auth_required(username)
        if not required:
            logger.warning(
                "Accepting %r from %s with NO authentication (open auth)",
                username,
                self._peer(),
            )
        return required

    def password_auth_supported(self) -> bool:
        return self._auth.password_supported()

    async def validate_password(self, username: str, password: str) -> bool:
        ok = await self._auth.verify_password(username, password)
        self._log_auth("password", username, ok)
        return ok

    def public_key_auth_supported(self) -> bool:
        return self._auth.public_key_supported()

    def validate_public_key(self, username: str, key: "asyncssh.SSHKey") -> bool:
        authorized = self._auth.authorized_keys_for(username)
        # An unknown user yields None and an empty list means "no keys on
        # record". Both deny: never let "nothing to check against" mean
        # "nothing to check".
        if not authorized:
            self._log_auth("public-key", username, False)
            return False

        ok = any(key == candidate for candidate in authorized)
        self._log_auth("public-key", username, ok)
        return ok

    def kbdint_auth_supported(self) -> bool:
        return self._auth.kbdint_supported()

    def get_kbdint_challenge(
        self, username: str, lang: str, submethods: str
    ) -> tuple[str, str, str, Sequence[tuple[str, bool]]]:
        return ("", "", "", self._auth.kbdint_prompts(username))

    async def validate_kbdint_response(
        self, username: str, responses: Sequence[str]
    ) -> bool:
        ok = await self._auth.verify_kbdint(username, list(responses))
        self._log_auth("keyboard-interactive", username, ok)
        return ok

    def _log_auth(self, method: str, username: str, ok: bool) -> None:
        """Record an auth attempt. Never logs the credential itself."""
        if ok:
            logger.info("Auth OK (%s) for %r from %s", method, username, self._peer())
        else:
            logger.warning(
                "Auth FAILED (%s) for %r from %s", method, username, self._peer()
            )
        self._emitter.emit(
            "auth.ok" if ok else "auth.failed",
            username=username,
            peer_ip=self._peer_ip,
            method=method,
        )

    # -- session -----------------------------------------------------------------

    def session_requested(self) -> asyncssh.SSHServerSession[bytes]:
        """Admit a session, or return one that explains the refusal.

        The admission chokepoint for ``max_sessions``. Registering here rather
        than in ``session_started`` matters: this is the first moment a session
        exists, the username is already known (auth has succeeded by now), and it
        closes the window where a client opens channels and never asks for a
        shell - which would otherwise consume slots invisibly.
        """
        assert self._conn is not None  # asyncssh calls connection_made first

        session = _WijjitSSHSession(
            self._app_factory,
            self._conn,
            config=self._config,
            registry=self._registry,
            emitter=self._emitter,
            session_id=new_session_id(),
            peer_ip=self._peer_ip,
        )

        rejection = self._registry.try_admit(session)
        if rejection is not None:
            logger.warning(
                "Refused session for %r from %s: %s",
                session.username,
                self._peer(),
                rejection.reason,
            )
            self._emitter.emit(
                "session.rejected",
                peer_ip=self._peer_ip,
                username=session.username,
                reason=rejection.reason,
            )
            return _RejectedSession(rejection)

        return session


class WijjitSSH:
    """Serve a Wijjit app over SSH, one app instance per connection.

    Parameters
    ----------
    app_factory : Callable[[SSHSession], Wijjit]
        Builds the app for each connection (the SSH analogue of a Flask view).
    config : ServerConfig, optional
        Every knob the server takes; see :class:`~wijjit_ssh.config.ServerConfig`.
        Defaults are used when omitted.
    **overrides
        Any :class:`~wijjit_ssh.config.ServerConfig` field, as a keyword. Applied
        on top of ``config``, so the common case needs no config object at all::

            WijjitSSH(make_app, host_keys=[key], auth=policy, max_sessions=10)

        Unknown names raise :exc:`TypeError` rather than being ignored - a
        typo'd ``max_session=1`` that silently does nothing would leave a server
        the operator believes is bounded and which is not.

    Attributes
    ----------
    config : ServerConfig
        The resolved configuration, after overrides and validation.

    Raises
    ------
    ValueError
        If no ``auth`` policy is given and ``allow_anonymous`` is not True.
        Serving an unauthenticated SSH server is a decision that has to be typed
        out, not one you inherit by forgetting an argument - so the default
        fails closed rather than silently accepting every client on the internet.
        Also raised for an out-of-range config value, or an unreadable host key.
    TypeError
        If an override is not a config field.

    Examples
    --------
    >>> from wijjit_ssh import AuthorizedKeys, ensure_host_key, WijjitSSH
    >>> WijjitSSH(                                          # doctest: +SKIP
    ...     make_app,
    ...     host_keys=[ensure_host_key("ssh_host_key")],
    ...     auth=AuthorizedKeys("~/.ssh/authorized_keys"),
    ... ).run()

    Or build the config up front, e.g. from a file or argparse:

    >>> config = ServerConfig(port=2222, max_sessions=10)   # doctest: +SKIP
    >>> WijjitSSH(make_app, config, host_keys=[key], auth=policy).run()
    """

    def __init__(
        self,
        app_factory: AppFactory,
        config: Optional[ServerConfig] = None,
        **overrides: Any,
    ) -> None:
        base = config if config is not None else ServerConfig()
        self.config = base.replace(**overrides) if overrides else base

        auth = self.config.auth
        # Order matters: the auth check comes first so that omitting a policy
        # reports the auth error, not a host-key error, whatever else is wrong.
        if auth is None:
            if not self.config.allow_anonymous:
                raise ValueError(
                    "WijjitSSH requires an auth policy. Pass auth=... (see "
                    "wijjit_ssh.auth: AuthorizedKeys, PasswordAuth, ChainAuth), "
                    "or pass allow_anonymous=True to run with NO authentication "
                    "- which lets anyone connect as any username, and must never "
                    "be used on an untrusted network."
                )
            auth = OpenAuth()

        if not auth.auth_required(""):
            logger.warning(
                "SERVER IS UNAUTHENTICATED: any client may connect as any "
                "username. This is for development only - do not expose it."
            )

        self._app_factory = app_factory
        # Resolve eagerly: a bad key path should fail here, where the server is
        # configured and the traceback points at the caller, rather than later
        # inside create_server. An empty list is allowed through so that
        # construction stays cheap to test; start() is where it has to be real.
        self._host_keys = resolve_host_keys(self.config.host_keys)
        self._auth = auth
        self._emitter = EventEmitter(self.config.on_event)
        # One registry for the whole server: it is what makes "how many sessions
        # are there" and "close all of them" answerable at all.
        self._registry = SessionRegistry(
            max_sessions=self.config.max_sessions,
            max_per_ip=self.config.max_per_ip,
            connect_rate=self.config.connect_rate,
            connect_burst=self.config.connect_burst,
        )

        self._acceptor: asyncssh.SSHAcceptor | None = None
        self._live: set[_WijjitSSHServer] = set()
        # Created in start(), not here: an Event binds no loop at construction on
        # 3.11, but a WijjitSSH reused across two asyncio.run() calls would carry
        # waiters registered against the first, dead loop.
        self._stopping: asyncio.Event | None = None
        self._stop_lock: asyncio.Lock | None = None
        self._stopped = False

    @property
    def active_sessions(self) -> int:
        """How many sessions are live right now."""
        return self._registry.active_sessions

    async def start(
        self, host: str | None = None, port: int | None = None
    ) -> "asyncssh.SSHAcceptor":
        """Bind the listener and start accepting connections.

        Returns as soon as the server is listening, so callers can drive it
        (tests bind port 0 and read the assigned port off the acceptor). Use
        :meth:`run_async` to start and then serve forever.

        Does not configure logging or install signal handlers: this entry point
        may be one coroutine inside a larger application, which owns both. Use
        :meth:`run` when the server owns the process.

        Parameters
        ----------
        host : str, optional
            Bind address, overriding ``config.host``.
        port : int, optional
            Bind port, overriding ``config.port``. Pass 0 to let the OS choose.

        Returns
        -------
        asyncssh.SSHAcceptor
            The listening server; call ``close()`` on it to stop accepting.

        Raises
        ------
        ValueError
            If no host keys were configured. asyncssh would refuse every
            connection with an opaque handshake failure, so say it plainly here.
        """
        if not self._host_keys:
            raise ValueError(
                "WijjitSSH has no host keys, so no client could verify this "
                "server. Pass host_keys=[...] - e.g. "
                "host_keys=[ensure_host_key('ssh_host_key')] to generate and "
                "reuse one, or host_keys=load_host_keys(['ssh_host_key']) to "
                "load a key you manage yourself (see wijjit_ssh.keys)."
            )

        self._stopping = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._stopped = False

        acceptor = await asyncssh.create_server(
            lambda: _WijjitSSHServer(
                self._app_factory,
                self._auth,
                config=self.config,
                registry=self._registry,
                emitter=self._emitter,
                live=self._live,
            ),
            self.config.host if host is None else host,
            self.config.port if port is None else port,
            server_host_keys=self._host_keys,
            # A TUI needs raw, char-at-a-time input and does its own drawing.
            # asyncssh's default PTY line editor would echo keystrokes and
            # buffer them until Enter - disable it so keys reach Wijjit
            # immediately and nothing is echoed over our frames.
            line_editor=False,
            # Binary channel: deliver input to data_received() as raw bytes for
            # the key/mouse decoder, and take frames as bytes. Without this,
            # asyncssh would decode/encode as text on our behalf and we would
            # lose the exact byte stream the client sent.
            encoding=None,
            # Bound what an unauthenticated peer can hold: asyncssh's own
            # default is 120s.
            login_timeout=self.config.login_timeout,
            # Reap peers whose TCP connection died without a FIN (a closed
            # laptop, a NAT timeout); they would otherwise hold a session slot
            # until the OS gave up, which can be hours.
            keepalive_interval=self.config.keepalive_interval,
            keepalive_count_max=self.config.keepalive_count_max,
        )

        self._acceptor = acceptor
        for key in self._host_keys:
            logger.info("Serving host key %s", fingerprint(key))
        logger.info(
            "Listening on %s:%d (max %d sessions, %d per IP)",
            acceptor.get_addresses()[0][0] if acceptor.get_addresses() else "?",
            acceptor.get_port(),
            self.config.max_sessions,
            self.config.max_per_ip,
        )
        return acceptor

    async def stop(self, *, grace: float | None = None) -> None:
        """Stop accepting, drain live sessions, and close the listener.

        Idempotent and safe to call concurrently: a second caller awaits the
        first rather than racing it. Safe to call on a server that never
        started.

        The order is deliberate. Accepting stops first, so the drain is not
        chasing a moving target. Then sessions are *asked* to end and given
        ``grace`` to do it, because a session that ends cleanly runs the app's
        teardown and restores the client's terminal, while one that is cancelled
        leaves a real person in the alternate screen buffer. Only then does the
        listener close.

        Parameters
        ----------
        grace : float, optional
            Seconds to allow for a clean exit, overriding
            ``config.shutdown_grace``.

        Returns
        -------
        None

        Examples
        --------
        >>> server = WijjitSSH(make_app, host_keys=[key], auth=policy)  # doctest: +SKIP
        >>> await server.start()                                        # doctest: +SKIP
        >>> await server.stop()                                         # doctest: +SKIP
        """
        if self._acceptor is None:
            return  # never started, or already fully stopped

        assert self._stop_lock is not None  # set by start(), with the acceptor
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True

            acceptor = self._acceptor
            logger.info("Shutting down: no longer accepting connections")
            acceptor.close()

            if self._stopping is not None:
                self._stopping.set()  # release run_async

            forced = await self._registry.drain(
                reason="server_shutdown",
                message="This server is shutting down. Please reconnect shortly.",
                grace=self.config.shutdown_grace if grace is None else grace,
            )

            # Draining ends sessions, which closes their channels - but the SSH
            # connection underneath each one survives that, and only its owner
            # can close it. Do so now: a shutdown that left clients connected to
            # a server with no sessions would be a lie, and (since Python 3.12
            # made Server.wait_closed() wait for every connection) the wait below
            # would hang until each client happened to give up.
            for connection in list(self._live):
                connection.disconnect("Server is shutting down.")
            self._live.clear()

            await acceptor.wait_closed()
            self._acceptor = None
            logger.info("Shutdown complete (%d session(s) had to be forced)", forced)

    async def run_async(self, host: str | None = None, port: int | None = None) -> None:
        """Start the SSH server and serve until :meth:`stop` is called.

        Like :meth:`start`, this configures no logging and installs no signal
        handlers - it may be embedded in a host application that owns both. A
        host that wants signal handling should install its own and call
        :meth:`stop`, or use :meth:`run`.

        Parameters
        ----------
        host : str, optional
            Bind address, overriding ``config.host``.
        port : int, optional
            Bind port, overriding ``config.port``.
        """
        await self.start(host, port)
        assert self._stopping is not None  # start() creates it
        await self._stopping.wait()

    def run(self, host: str | None = None, port: int | None = None) -> None:
        """Serve until interrupted, draining cleanly. Blocking; owns the process.

        The entry point for "this process is the server", as opposed to
        :meth:`run_async`, which may be one coroutine inside a larger
        application. That ownership is the whole distinction: this is the only
        method that configures logging or installs signal handlers, because a
        library coroutine has no business doing either to somebody else's
        process. (It is the same reasoning that makes the backend set
        ``owns_terminal = False``.)

        On SIGINT/SIGTERM the server stops accepting, gives live sessions
        ``config.shutdown_grace`` to exit cleanly - which is what restores each
        client's terminal - and then exits.

        Parameters
        ----------
        host : str, optional
            Bind address, overriding ``config.host``.
        port : int, optional
            Bind port, overriding ``config.port``.

        Notes
        -----
        Signal handling on Windows is best-effort: SIGTERM is never delivered
        there (``TerminateProcess`` does not run handlers), so only Ctrl+C
        drains. The deployment targets in the README are systemd and Docker,
        both POSIX.
        """
        # Only here, and only if nobody else has: a host that configured its own
        # logging keeps full control. See wijjit_ssh.logging.
        if not logging_is_configured():
            configure_logging(sys.stderr)
        try:
            asyncio.run(self._run_owning_process(host, port))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def _run_owning_process(self, host: str | None, port: int | None) -> None:
        """:meth:`run_async`, plus the signal handlers only :meth:`run` may install."""
        loop = asyncio.get_running_loop()
        undo: list[Callable[[], None]] = []

        for sig in (signal.SIGINT, signal.SIGTERM):
            if not self._install_signal_handler(loop, sig, undo):
                logger.debug("No handler installed for %s", sig.name)

        try:
            await self.run_async(host, port)
        finally:
            for restore in undo:
                with suppress(Exception):
                    restore()
            # A signal only asks stop() to start; without this, run() could
            # return while sessions are still draining.
            await self.stop()

    def _install_signal_handler(
        self,
        loop: asyncio.AbstractEventLoop,
        sig: signal.Signals,
        undo: list[Callable[[], None]],
    ) -> bool:
        """Install one signal handler, by whichever mechanism this platform has.

        Returns
        -------
        bool
            True if a handler was installed.
        """
        try:
            loop.add_signal_handler(sig, self._signal_stop, sig)
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            # Windows' ProactorEventLoop has no add_signal_handler. Fall back to
            # the C-level handler and bounce onto the loop thread, which works
            # because call_soon_threadsafe writes to the loop's self-pipe and so
            # wakes the proactor.
            try:
                previous = signal.signal(
                    sig,
                    lambda s, frame: loop.call_soon_threadsafe(self._signal_stop, s),
                )
            except (ValueError, OSError, AttributeError):
                # signal.signal only works on the main thread, and not every
                # signal exists everywhere. Not fatal: run() still has its
                # KeyboardInterrupt net.
                return False

            def restore_c_handler() -> None:
                signal.signal(sig, previous)

            undo.append(restore_c_handler)
        else:

            def restore_loop_handler() -> None:
                loop.remove_signal_handler(sig)

            undo.append(restore_loop_handler)
        return True

    def _signal_stop(self, sig: int) -> None:
        """Begin a graceful shutdown in response to a signal."""
        name = signal.Signals(sig).name if isinstance(sig, int) else str(sig)
        if self._stopped:
            # A second signal from an impatient operator. stop() is idempotent
            # and already draining; say so rather than appearing to ignore them.
            logger.warning("%s received again; already shutting down", name)
            return
        logger.info("%s received; shutting down gracefully", name)
        asyncio.ensure_future(self.stop())
