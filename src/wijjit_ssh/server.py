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
...     host_keys=["ssh_host_key"],
...     auth=AuthorizedKeys("~/.ssh/authorized_keys"),
... ).run(port=8022)

Then ``ssh -p 8022 you@localhost`` drops the client straight into the TUI.

Authentication is **fail-closed**: constructing :class:`WijjitSSH` without an
``auth`` policy raises unless ``allow_anonymous=True`` is passed explicitly. See
:mod:`wijjit_ssh.auth`.

Not yet hardened
----------------
* No resource limits (max sessions, per-IP caps, idle timeout).
* Blocking sync handlers stall that session's frames; give each app an executor
  (``EXECUTOR``) for CPU-bound work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

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
from wijjit_ssh.keys import HostKeySource, resolve_host_keys
from wijjit_ssh.logging import get_logger

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
    """

    username: str
    term_type: str
    columns: int
    lines: int
    backend: RemoteTerminalBackend
    conn: "asyncssh.SSHServerConnection"


AppFactory = Callable[[SSHSession], Wijjit]


class _WijjitSSHSession(asyncssh.SSHServerSession[bytes]):
    """Bridges one SSH channel to one Wijjit app instance.

    Generic over ``bytes``: the channel is opened with ``encoding=None`` so this
    session sees the client's raw byte stream (see :mod:`wijjit_ssh.input`).

    Parameters
    ----------
    app_factory : AppFactory
        Builds the app for this connection.
    conn : asyncssh.SSHServerConnection
        The owning connection (source of the authenticated username).
    """

    def __init__(
        self, app_factory: AppFactory, conn: asyncssh.SSHServerConnection
    ) -> None:
        self._app_factory = app_factory
        self._conn = conn
        self._chan: asyncssh.SSHServerChannel[bytes] | None = None
        self._term_type: str = "xterm"
        self._size: tuple[int, int] = (80, 24)
        self._backend: Optional[RemoteTerminalBackend] = None
        self._app: Optional[Wijjit] = None
        self._task: Optional[asyncio.Task[None]] = None

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
        cols, lines = self._size

        self._backend = RemoteTerminalBackend(self._chan, cols, lines)

        # Seed the size override before the factory runs so Wijjit.__init__ sizes
        # its managers to this client. The factory must wire the backend into the
        # app (Wijjit(backend=session.backend)); doing so is what points the
        # app's screen/input at the channel.
        set_terminal_size(cols, lines)
        session = SSHSession(
            username=self._conn.get_extra_info("username") or "anonymous",
            term_type=self._term_type,
            columns=cols,
            lines=lines,
            backend=self._backend,
            conn=self._conn,
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
            logger.exception("App factory failed for user %r", session.username)
            self._fail(f"Failed to start application: {exc}")
            return

        self._app = app
        self._task = asyncio.ensure_future(self._run_app())

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

    async def _run_app(self) -> None:
        assert self._app is not None  # only started once the factory succeeded
        try:
            # Enter Wijjit's async loop directly (we are already on an event
            # loop; app.run() would try to start a new one via asyncio.run).
            await self._app.event_loop.run_async()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Application crashed")
            self._write(f"\r\nApplication error: {exc}\r\n")
        finally:
            if self._chan is not None:
                self._chan.close()

    def data_received(self, data: bytes, datatype: object) -> None:
        # Binary channel: `data` is raw bytes straight off the wire, which the
        # backend hands to the key/mouse decoder on this same event loop.
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
        if self._app is not None:
            # Ask the loop to stop; the task's finally closes the channel and
            # the loop's own teardown closes the input source.
            self._app.quit()
        if self._task is not None:
            self._task.cancel()


class _WijjitSSHServer(asyncssh.SSHServer):
    """asyncssh server that mints a Wijjit session per connection.

    Every authentication callback asyncssh offers is forwarded to the
    :class:`~wijjit_ssh.auth.AuthPolicy`, so credentials live in the policy and
    never in this glue.
    """

    def __init__(self, app_factory: AppFactory, auth: AuthPolicy) -> None:
        self._app_factory = app_factory
        self._auth = auth
        self._conn: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        logger.info("Connection from %s", self._peer())

    def connection_lost(self, exc: Optional[Exception]) -> None:
        logger.info("Connection closed from %s", self._peer())

    def _peer(self) -> str:
        """The client's address, for logs."""
        if self._conn is None:
            return "unknown"
        peer = self._conn.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return str(peer)

    # -- authentication (delegated to the policy) --------------------------------

    def begin_auth(self, username: str) -> bool:
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

    # -- session -----------------------------------------------------------------

    def session_requested(self) -> _WijjitSSHSession:
        assert self._conn is not None  # asyncssh calls connection_made first
        return _WijjitSSHSession(self._app_factory, self._conn)


class WijjitSSH:
    """Serve a Wijjit app over SSH, one app instance per connection.

    Parameters
    ----------
    app_factory : Callable[[SSHSession], Wijjit]
        Builds the app for each connection (the SSH analogue of a Flask view).
    host_keys : sequence of str, PathLike, or asyncssh.SSHKey
        The server's identity. Paths are loaded (and their fingerprints logged)
        when the server is constructed, so a bad path fails here rather than at
        listen time. Use :func:`~wijjit_ssh.keys.ensure_host_key` to generate one
        on first run, or :func:`~wijjit_ssh.keys.load_host_keys` for a key you
        manage out of band. Passing several supports rotation.
    auth : AuthPolicy, optional
        How to authenticate clients. See :mod:`wijjit_ssh.auth` -
        :class:`~wijjit_ssh.auth.AuthorizedKeys` is the usual choice.
    allow_anonymous : bool, optional
        Permit running with **no authentication** (default False). Required to
        omit ``auth``; see Raises.

    Raises
    ------
    ValueError
        If no ``auth`` policy is given and ``allow_anonymous`` is not True.
        Serving an unauthenticated SSH server is a decision that has to be typed
        out, not one you inherit by forgetting an argument - so the default
        fails closed rather than silently accepting every client on the internet.

    Examples
    --------
    >>> from wijjit_ssh.auth import AuthorizedKeys
    >>> WijjitSSH(                                          # doctest: +SKIP
    ...     make_app,
    ...     host_keys=["ssh_host_key"],
    ...     auth=AuthorizedKeys("~/.ssh/authorized_keys"),
    ... ).run(port=8022)
    """

    def __init__(
        self,
        app_factory: AppFactory,
        *,
        host_keys: Sequence[HostKeySource],
        auth: Optional[AuthPolicy] = None,
        allow_anonymous: bool = False,
    ) -> None:
        # Order matters: the auth check comes first so that omitting a policy
        # reports the auth error, not a host-key error, whatever else is wrong.
        if auth is None:
            if not allow_anonymous:
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
        self._host_keys = resolve_host_keys(host_keys)
        self._auth = auth

    async def start(self, host: str = "", port: int = 8022) -> "asyncssh.SSHAcceptor":
        """Bind the listener and start accepting connections.

        Returns as soon as the server is listening, so callers can drive it
        (tests bind port 0 and read the assigned port off the acceptor). Use
        :meth:`run_async` to start and then serve forever.

        Parameters
        ----------
        host : str, optional
            Bind address (default: all interfaces).
        port : int, optional
            Bind port (default: 8022). Pass 0 to let the OS choose.

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
        return await asyncssh.create_server(
            lambda: _WijjitSSHServer(self._app_factory, self._auth),
            host,
            port,
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
        )

    async def run_async(self, host: str = "", port: int = 8022) -> None:
        """Start the SSH server and serve until cancelled.

        Parameters
        ----------
        host : str, optional
            Bind address (default: all interfaces).
        port : int, optional
            Bind port (default: 8022).
        """
        await self.start(host, port)
        # Serve forever.
        await asyncio.Event().wait()

    def run(self, host: str = "", port: int = 8022) -> None:
        """Blocking convenience wrapper around :meth:`run_async`."""
        try:
            asyncio.run(self.run_async(host, port))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
