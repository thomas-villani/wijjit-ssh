"""End-to-end tests: a real asyncssh client against a real WijjitSSH server.

Everything here runs in-process over the loopback interface with a generated
host key. These are the tests that prove the whole stack actually works: PTY
negotiation, the byte decoder, frame delivery, resize, per-session isolation,
and that the limits from ``limits.py`` are really wired to asyncssh's callbacks.

The split is deliberate. The decoder's semantics are covered exhaustively (and
much faster) in ``test_input_decoder.py``, and the limits' semantics in
``test_limits.py`` with an injected clock. These tests exist to prove the wiring,
not the logic - which is why the timeouts here are generous and the assertions
are coarse.

The client machinery lives in ``_client.py``; read its docstring before writing
an assertion about output, because Wijjit's diff renderer means the byte stream
is a transcript rather than a picture.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest
from wijjit import Wijjit

from tests._client import _Server, _open
from wijjit_ssh import SSHSession, WijjitSSH


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


async def test_bad_app_factory_reports_instead_of_dropping() -> None:
    """A factory that raises tells the client why, rather than vanishing.

    Without this, a template typo or bad key binding shows up as an unexplained
    disconnect - which is exactly how this bug was found.
    """

    def broken_factory(session: SSHSession) -> Wijjit:
        raise RuntimeError("kaboom")

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    wijjit_ssh = WijjitSSH(broken_factory, host_keys=[host_key], allow_anonymous=True)
    acceptor = await wijjit_ssh.start("127.0.0.1", 0)
    broken = _Server(acceptor, wijjit_ssh)
    try:
        client = await _open(broken)
        async with client.conn:
            await client.expect("Failed to start application")
            assert "kaboom" in client.screen()
        # The slot taken at session_requested must be given back, or a broken
        # factory would leak capacity one failed connection at a time.
        await asyncio.sleep(0.1)
        assert wijjit_ssh.active_sessions == 0
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


# -- limits, over a real socket ------------------------------------------------
#
# These prove the wiring only: that limits.py is actually connected to asyncssh's
# callbacks. The limits' own semantics are pinned in test_limits.py against an
# injected clock, which is why nothing here asserts on precise timing.


async def test_max_sessions_refuses_with_an_explanation(serve) -> None:
    """A refused session must say why, not just fail to open.

    Returning falsy from session_requested would give the user a bare
    "Session refused" protocol error; this is why _RejectedSession exists.
    """
    server = await serve(max_sessions=1)
    first = await _open(server)
    async with first.conn:
        await first.expect("USER tester")
        assert server.server.active_sessions == 1

        second = await _open(server)
        async with second.conn:
            screen = await second.expect("at capacity")
            assert "1 sessions" in screen


async def test_a_freed_slot_admits_the_next_session(serve) -> None:
    server = await serve(max_sessions=1)
    first = await _open(server)
    await first.expect("USER tester")

    first.conn.close()
    await first.wait_closed()
    await asyncio.sleep(0.2)
    assert server.server.active_sessions == 0

    second = await _open(server)
    async with second.conn:
        await second.expect("USER tester")  # admitted, not refused


async def test_the_per_ip_cap_rejects_before_authentication(serve) -> None:
    """The peer is turned away with a reason, without a key exchange on it.

    This pins the deferred-disconnect trick in _WijjitSSHServer.connection_made:
    because the MSG_DISCONNECT waits one tick for asyncssh's version banner to go
    out first, the client parses it properly and reports our text and
    DISC_TOO_MANY_CONNECTIONS (12), rather than seeing a bare connection reset.
    """
    server = await serve(max_per_ip=1)
    first = await _open(server)
    async with first.conn:
        await first.expect("USER tester")

        with pytest.raises(asyncssh.DisconnectError) as excinfo:
            await asyncio.wait_for(_open(server), timeout=10)

    assert excinfo.value.code == asyncssh.DISC_TOO_MANY_CONNECTIONS
    assert "Too many connections from your address" in str(excinfo.value)


async def test_the_connect_rate_limit_rejects_once_the_burst_is_spent(serve) -> None:
    server = await serve(connect_rate=0.01, connect_burst=1, max_per_ip=50)
    first = await _open(server)
    async with first.conn:
        await first.expect("USER tester")

        with pytest.raises(asyncssh.DisconnectError) as excinfo:
            await asyncio.wait_for(_open(server), timeout=10)

    assert "Too many connection attempts" in str(excinfo.value)


async def test_idle_timeout_closes_a_quiet_session(serve) -> None:
    server = await serve(idle_timeout=0.4)
    client = await _open(server)
    async with client.conn:
        await client.expect("USER tester")
        await client.wait_closed(timeout=5)

    assert b"inactivity" in client.raw()


async def test_the_idle_message_lands_outside_the_alternate_buffer(serve) -> None:
    """The subtle one: written after the app teardown, not before.

    A message written while the app still owns the screen goes inside the TUI
    frame and is painted over by the next repaint. It is only safe to write once
    ESC[?1049l has taken the client back to its normal screen.
    """
    server = await serve(idle_timeout=0.4)
    client = await _open(server)
    async with client.conn:
        await client.expect("USER tester")
        await client.wait_closed(timeout=5)

    raw = client.raw()
    assert raw.rfind(b"inactivity") > raw.rfind(b"\x1b[?1049l") > -1


async def test_input_resets_the_idle_timer(serve) -> None:
    """Someone who is typing is not idle."""
    server = await serve(idle_timeout=0.5)
    client = await _open(server)
    async with client.conn:
        await client.expect("USER tester")

        for _ in range(5):  # keep typing for 1s, twice the idle timeout
            await asyncio.sleep(0.2)
            client.send(b"x")

        assert not client.collector.closed.is_set(), "an active session was reaped"
        await client.expect("N 5")


async def test_session_timeout_closes_an_active_session(serve) -> None:
    """Unlike idle, the absolute deadline interrupts someone mid-work."""
    server = await serve(idle_timeout=None, session_timeout=0.5)
    client = await _open(server)
    async with client.conn:
        await client.expect("USER tester")

        async def keep_typing() -> None:
            while True:
                await asyncio.sleep(0.1)
                client.send(b"x")

        typing = asyncio.create_task(keep_typing())
        try:
            await client.wait_closed(timeout=5)
        finally:
            typing.cancel()

    assert b"session limit" in client.raw()


async def test_a_session_with_no_pty_is_turned_away(serve) -> None:
    """`ssh host command` has no terminal for a TUI to draw on."""
    server = await serve()
    conn = await asyncssh.connect(
        "127.0.0.1", port=server.port, username="tester", known_hosts=None
    )
    async with conn:
        received: list[bytes] = []

        class Collector(asyncssh.SSHClientSession):
            def data_received(self, data, datatype):
                received.append(data)

        chan, _ = await conn.create_session(Collector, encoding=None)  # no term_type
        await chan.wait_closed()

    assert b"only serves interactive terminal applications" in b"".join(received)


async def test_the_banner_reaches_the_client_before_auth(serve) -> None:
    server = await serve(banner="Authorized users only.\n")
    seen: list[str] = []

    class BannerClient(asyncssh.SSHClient):
        def auth_banner_received(self, msg: str, lang: str) -> None:
            seen.append(msg)

    conn, _ = await asyncssh.create_connection(
        BannerClient,
        "127.0.0.1",
        port=server.port,
        username="tester",
        known_hosts=None,
    )
    async with conn:
        assert "Authorized users only." in "".join(seen)


async def test_lifecycle_events_reach_the_metrics_hook(serve) -> None:
    events: list[str] = []
    server = await serve(on_event=lambda event, fields: events.append(event))

    client = await _open(server)
    await client.expect("USER tester")
    client.conn.close()
    await client.wait_closed()
    await asyncio.sleep(0.2)

    assert "connection.opened" in events
    assert "session.started" in events
    assert "session.ended" in events
    assert "connection.closed" in events


async def test_the_disconnect_log_keeps_the_peer_address(
    serve, caplog: pytest.LogCaptureFixture
) -> None:
    """asyncssh drops peername on teardown, so it must be cached.

    Read lazily, connection_lost logs "Connection closed from None" -- losing the
    address on the one line you would most want to grep for.
    """
    server = await serve()
    client = await _open(server)
    await client.expect("USER tester")

    with caplog.at_level("INFO", logger="wijjit_ssh"):
        client.conn.close()
        await client.wait_closed()
        await asyncio.sleep(0.2)

    closed = [r.message for r in caplog.records if "Connection closed" in r.message]
    assert closed, "no disconnect was logged"
    assert "from None" not in closed[0]
    assert "127.0.0.1:" in closed[0]
