"""Shared machinery for tests that drive a real WijjitSSH over a real socket.

Not a conftest: these are imported by name, so it is obvious where they come
from. The fixtures that wrap them live in ``conftest.py``.

The important piece here is :class:`_Collector`, and the reason is worth reading
before writing a test against it. Wijjit ships a **diff** renderer: after the
first frame it re-sends only the cells that changed, addressed by cursor
position. So the byte stream is a transcript, not a picture - concatenating it
and stripping ANSI would show the initial frame plus a scattering of single
characters, and an assertion like ``"N 1" in text`` would never match even
though the client's screen plainly reads ``N 1``.

Feeding the stream through a real VT emulator (``pyte``) reconstructs the screen
the user actually sees, which is the only thing worth asserting on - and it
validates that our escape sequences are well-formed, since a real emulator has
to accept them.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pyte
import pytest
from wijjit import Wijjit, render_template_string
from wijjit.terminal.size import get_terminal_size

from wijjit_ssh import SSHSession, WijjitSSH

TEMPLATE = """
{% vstack %}
  {% text %}USER {{ who }} SIZE {{ cols }}x{{ rows }} N {{ n }} {{ tag }}{% endtext %}
{% endvstack %}
"""


def make_app(session: SSHSession) -> Wijjit:
    """Build the app under test for one connection."""
    app = Wijjit(backend=session.backend, initial_state={"n": 0, "tag": "-"})

    @app.view("main", default=True)
    def main():
        # Read the size live rather than from `session`, so a resize is visible.
        # This also exercises the task-local size override: each concurrent
        # session must observe its own dimensions here.
        size = get_terminal_size()
        return render_template_string(
            TEMPLATE,
            who=session.username,
            cols=size.columns,
            rows=size.lines,
            n=app.state["n"],
            tag=app.state["tag"],
        )

    @app.on_key("x")
    def bump(event=None):
        app.state["n"] += 1

    @app.on_key("escape")
    def escaped(event=None):
        app.state["tag"] = "ESC"

    @app.on_key("up")
    def arrow(event=None):
        app.state["tag"] = "UP"

    # No Ctrl+Q handler: Wijjit reserves that key to quit the app, and binding
    # it raises. test_app_quit_closes_the_session relies on the built-in.
    return app


class _Collector(asyncssh.SSHClientSession):
    """Client session that renders what the server sends into a real screen.

    See the module docstring for why this is a VT emulator and not a string
    buffer.
    """

    def __init__(self, columns: int = 80, lines: int = 24) -> None:
        self.screen_buffer = pyte.Screen(columns, lines)
        self.stream = pyte.Stream(self.screen_buffer)
        self.closed = asyncio.Event()
        self.raw = bytearray()

    def data_received(self, data: bytes, datatype: object) -> None:
        # Keep the raw stream too: a few assertions are about escape-sequence
        # *ordering* (did the app leave the alternate buffer before we wrote a
        # message?), which a reconstructed screen cannot show.
        self.raw += data
        self.stream.feed(data.decode("utf-8", errors="replace"))

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed.set()

    def resize(self, columns: int, lines: int) -> None:
        """Match the emulator to a new terminal size (rows first, as pyte wants)."""
        self.screen_buffer.resize(lines, columns)

    def screen(self) -> str:
        """The client's visible screen, as text."""
        return "\n".join(self.screen_buffer.display)


async def _await_text(collector: _Collector, needle: str, timeout: float = 5.0) -> str:
    """Wait until ``needle`` shows up in the delivered output.

    Parameters
    ----------
    collector : _Collector
        The client session collecting frames.
    needle : str
        Text to wait for.
    timeout : float, optional
        Seconds to wait before failing.

    Returns
    -------
    str
        The full screen text once the needle appeared.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        screen = collector.screen()
        if needle in screen:
            return screen
        await asyncio.sleep(0.02)
    pytest.fail(f"timed out waiting for {needle!r}\n--- got ---\n{collector.screen()}")


class _Server:
    """A running WijjitSSH bound to an ephemeral port."""

    def __init__(self, acceptor: asyncssh.SSHAcceptor, server: WijjitSSH) -> None:
        self._acceptor = acceptor
        self.server = server
        self.port: int = acceptor.get_port()

    def close(self) -> None:
        self._acceptor.close()


async def start_server(**overrides: object) -> _Server:
    """Start a WijjitSSH on 127.0.0.1 with a throwaway host key.

    Tests that use this are about the transport, not the gate, so the server
    runs open - which auth deliberately makes you say out loud. Auth itself is
    covered in ``test_auth.py``.

    Parameters
    ----------
    **overrides : object
        Any :class:`~wijjit_ssh.config.ServerConfig` field.

    Returns
    -------
    _Server
    """
    overrides.setdefault("allow_anonymous", True)
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    wijjit_ssh = WijjitSSH(make_app, host_keys=[host_key], **overrides)
    acceptor = await wijjit_ssh.start(host="127.0.0.1", port=0)
    return _Server(acceptor, wijjit_ssh)


class _Client:
    """An open SSH session plus the emulator showing what it renders."""

    def __init__(self, conn, chan, collector: _Collector) -> None:
        self.conn = conn
        self.chan = chan
        self.collector = collector

    def send(self, data: bytes) -> None:
        """Write raw bytes to the channel, exactly as a real terminal would."""
        self.chan.write(data)

    def resize(self, columns: int, lines: int) -> None:
        self.chan.change_terminal_size(columns, lines)
        self.collector.resize(columns, lines)

    def screen(self) -> str:
        return self.collector.screen()

    def raw(self) -> bytes:
        """Everything the server sent, unrendered."""
        return bytes(self.collector.raw)

    async def expect(self, needle: str, timeout: float = 5.0) -> str:
        return await _await_text(self.collector, needle, timeout)

    async def wait_closed(self, timeout: float = 5.0) -> None:
        """Wait for the server to hang up on us."""
        await asyncio.wait_for(self.collector.closed.wait(), timeout)


async def _open(
    server: _Server,
    username: str = "tester",
    size: tuple[int, int] = (80, 24),
) -> _Client:
    """Connect, request a PTY of ``size``, and return the driving handle."""
    columns, lines = size
    conn = await asyncssh.connect(
        "127.0.0.1", port=server.port, username=username, known_hosts=None
    )
    chan, collector = await conn.create_session(
        lambda: _Collector(columns, lines),
        term_type="xterm",
        term_size=size,
        encoding=None,
    )
    return _Client(conn, chan, collector)
