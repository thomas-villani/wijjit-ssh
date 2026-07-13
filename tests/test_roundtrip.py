"""End-to-end tests: a real asyncssh client against a real WijjitSSH server.

Everything here runs in-process over the loopback interface with a generated
host key, so there is no fixture setup and nothing to clean up. These are the
tests that prove the whole stack actually works: PTY negotiation, the byte
decoder, frame delivery, resize, and per-session isolation.

The decoder's own semantics are covered exhaustively (and much faster) in
``test_input_decoder.py``; these tests exist to prove the wiring, not the
parsing.
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

    Wijjit ships a *diff* renderer: after the first frame it re-sends only the
    cells that changed, addressed by cursor position. So the byte stream is a
    transcript, not a picture - concatenating it and stripping ANSI would show
    the initial frame plus a scattering of single characters, and an assertion
    like ``"N 1" in text`` would never match even though the client's screen
    plainly reads ``N 1``.

    Feeding the stream through a VT emulator reconstructs the screen the user
    actually sees, which is the only thing worth asserting on - and it validates
    that our escape sequences are well-formed, since a real emulator has to
    accept them.
    """

    def __init__(self, columns: int = 80, lines: int = 24) -> None:
        self.screen_buffer = pyte.Screen(columns, lines)
        self.stream = pyte.Stream(self.screen_buffer)
        self.closed = asyncio.Event()

    def data_received(self, data: bytes, datatype: object) -> None:
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

    def __init__(self, acceptor: asyncssh.SSHAcceptor) -> None:
        self._acceptor = acceptor
        self.port: int = acceptor.get_port()

    def close(self) -> None:
        self._acceptor.close()


@pytest.fixture
async def server() -> _Server:
    """Start a WijjitSSH on 127.0.0.1 with a throwaway host key."""
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await WijjitSSH(make_app, host_keys=[host_key]).start(
        host="127.0.0.1", port=0
    )
    running = _Server(acceptor)
    yield running
    running.close()


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

    async def expect(self, needle: str, timeout: float = 5.0) -> str:
        return await _await_text(self.collector, needle, timeout)


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


async def test_initial_frame_reaches_the_client(server: _Server) -> None:
    """The app renders and its first frame lands on the client's screen."""
    client = await _open(server)
    async with client.conn:
        screen = await client.expect("USER tester")

        assert "SIZE 80x24" in screen
        assert "N 0" in screen


async def test_keystroke_routes_through_the_decoder(server: _Server) -> None:
    """A raw byte on the wire becomes a Wijjit key event and mutates state."""
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        client.send(b"x")
        await client.expect("N 1")

        client.send(b"xx")
        await client.expect("N 3")


async def test_utf8_character_routes_as_one_key(server: _Server) -> None:
    """A multi-byte rune is one key, not one key per byte."""
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        # 'x' encodes as one byte; send a 2-byte rune first to prove the decoder
        # does not emit spurious keys for continuation bytes.
        client.send("é".encode())
        client.send(b"x")
        await client.expect("N 1")


async def test_escape_sequence_routes_as_a_special_key(server: _Server) -> None:
    """An arrow key (a multi-byte CSI sequence) arrives as `up`, not as junk."""
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        client.send(b"\x1b[A")
        await client.expect("UP")


async def test_split_escape_sequence_still_decodes(server: _Server) -> None:
    """An arrow key split across two packets still decodes as one key.

    This is the case the old thread/prompt_toolkit path handled by luck and the
    decoder handles by construction.
    """
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        client.send(b"\x1b")
        await asyncio.sleep(0.01)  # deliberately under the ESC timeout
        client.send(b"[A")

        await client.expect("UP")


async def test_lone_escape_fires_the_escape_key(server: _Server) -> None:
    """A bare ESC with nothing behind it resolves to Escape via the flush timer.

    This is the one timing-dependent path in the decoder, so it is worth proving
    over a real channel and not only in the unit tests.
    """
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        client.send(b"\x1b")
        await client.expect("ESC")


async def test_resize_reflows_the_app(server: _Server) -> None:
    """A window resize republishes the size to the app's task-local override."""
    client = await _open(server)
    async with client.conn:
        await client.expect("SIZE 80x24")

        client.resize(100, 30)
        await client.expect("SIZE 100x30")


async def test_app_quit_closes_the_session(server: _Server) -> None:
    """Ctrl+Q reaches the app, which quits, which drops the SSH session."""
    client = await _open(server)
    async with client.conn:
        await client.expect("N 0")

        client.send(b"\x11")  # Ctrl+Q
        await asyncio.wait_for(client.collector.closed.wait(), timeout=5.0)


async def test_bad_app_factory_reports_instead_of_dropping(server: _Server) -> None:
    """A factory that raises tells the client why, rather than vanishing.

    Without this, a template typo or bad key binding shows up as an unexplained
    disconnect - which is exactly how this bug was found.
    """

    def broken_factory(session: SSHSession) -> Wijjit:
        raise RuntimeError("kaboom")

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await WijjitSSH(broken_factory, host_keys=[host_key]).start(
        "127.0.0.1", 0
    )
    broken = _Server(acceptor)
    try:
        client = await _open(broken)
        async with client.conn:
            await client.expect("Failed to start application")
            assert "kaboom" in client.screen()
    finally:
        broken.close()


async def test_concurrent_sessions_are_isolated(server: _Server) -> None:
    """Two clients of different sizes each render at their own size.

    This is the payoff of the contextvar-based size override: one process, one
    event loop, two apps, no interference.
    """
    alice = await _open(server, username="alice", size=(80, 24))
    bob = await _open(server, username="bob", size=(120, 40))

    async with alice.conn, bob.conn:
        screen_a = await alice.expect("USER alice")
        screen_b = await bob.expect("USER bob")

        assert "SIZE 80x24" in screen_a
        assert "SIZE 120x40" in screen_b

        # State is per-session too: keying one must not move the other.
        alice.send(b"x")
        await alice.expect("N 1")
        assert "N 1" not in bob.screen()
