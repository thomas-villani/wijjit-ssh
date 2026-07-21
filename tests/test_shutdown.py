"""Tests for graceful shutdown: stop(), run_async unblocking, signal handling.

The point of a graceful shutdown is not tidiness. A session that ends cleanly
runs the app's teardown, which leaves the alternate screen buffer and restores
the client's terminal; one that is killed leaves a real person staring at a
frozen frame. So the assertions here are mostly about *who got a chance to
exit*, not about how fast the process died.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import asyncssh
import pytest
from wijjit import Wijjit, render_template_string

from wijjit_ssh import WijjitSSH


def make_app(session):
    app = Wijjit(backend=session.backend)

    @app.view("main", default=True)
    def main():
        return render_template_string(
            "{% frame %}{% text %}hi {{ who }}{% endtext %}{% endframe %}",
            who=session.username,
        )

    return app


@pytest.fixture
def host_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519")


def _server(host_key: asyncssh.SSHKey, **overrides) -> WijjitSSH:
    overrides.setdefault("allow_anonymous", True)
    return WijjitSSH(make_app, host_keys=[host_key], **overrides)


async def _connect(port: int) -> tuple[asyncssh.SSHClientConnection, object]:
    conn = await asyncssh.connect(
        "127.0.0.1",
        port,
        known_hosts=None,
        username="tester",
        client_keys=None,
        encoding=None,
    )
    chan, _ = await conn.create_session(
        asyncssh.SSHClientSession, term_type="xterm", term_size=(80, 24)
    )
    return conn, chan


# -- stop() --------------------------------------------------------------------


async def test_stop_closes_the_listener(host_key) -> None:
    server = _server(host_key)
    acceptor = await server.start(host="127.0.0.1", port=0)
    port = acceptor.get_port()

    await server.stop()

    with pytest.raises((OSError, asyncssh.Error)):
        await asyncio.wait_for(_connect(port), timeout=5)


async def test_stop_drains_live_sessions(host_key) -> None:
    server = _server(host_key)
    acceptor = await server.start(host="127.0.0.1", port=0)

    conns = []
    for _ in range(2):
        conn, chan = await _connect(acceptor.get_port())
        conns.append((conn, chan))
    await asyncio.sleep(0.4)
    assert server.active_sessions == 2

    await asyncio.wait_for(server.stop(), timeout=10)

    assert server.active_sessions == 0
    for _, chan in conns:
        assert chan.is_closing() or chan.get_exit_status() is not None
    for conn, _ in conns:
        conn.close()


async def test_stop_lets_sessions_exit_cleanly(host_key) -> None:
    """A drained client must get its terminal back, not a frozen frame.

    ESC[?1049l is the app leaving the alternate screen buffer, which only
    happens if the app's own teardown ran -- i.e. if it was asked to quit rather
    than cancelled.
    """
    server = _server(host_key)
    acceptor = await server.start(host="127.0.0.1", port=0)

    received: list[bytes] = []

    class Collector(asyncssh.SSHClientSession):
        def data_received(self, data, datatype):
            received.append(data)

    conn = await asyncssh.connect(
        "127.0.0.1",
        acceptor.get_port(),
        known_hosts=None,
        username="tester",
        client_keys=None,
        encoding=None,
    )
    chan, _ = await conn.create_session(
        Collector, term_type="xterm", term_size=(80, 24)
    )
    await asyncio.sleep(0.4)

    await asyncio.wait_for(server.stop(), timeout=10)

    stream = b"".join(received)
    assert b"\x1b[?1049l" in stream, "app never left the alternate screen buffer"
    # And the shutdown notice lands after it, on an ordinary screen.
    assert stream.rfind(b"shutting down") > stream.rfind(b"\x1b[?1049l")
    conn.close()


async def test_stop_is_idempotent(host_key) -> None:
    server = _server(host_key)
    await server.start(host="127.0.0.1", port=0)
    await server.stop()
    await server.stop()  # must not raise


async def test_concurrent_stops_do_not_race(host_key) -> None:
    server = _server(host_key)
    await server.start(host="127.0.0.1", port=0)
    await asyncio.gather(server.stop(), server.stop(), server.stop())


async def test_stop_on_a_server_that_never_started(host_key) -> None:
    await _server(host_key).stop()  # must not raise


async def test_stop_with_no_sessions_is_quick(host_key) -> None:
    """An empty drain must not sit through the whole grace period."""
    server = _server(host_key, shutdown_grace=30.0)
    await server.start(host="127.0.0.1", port=0)
    await asyncio.wait_for(server.stop(), timeout=2)


# -- run_async -----------------------------------------------------------------


async def test_run_async_serves_until_stopped(host_key) -> None:
    server = _server(host_key)
    task = asyncio.create_task(server.run_async(host="127.0.0.1", port=0))

    await asyncio.sleep(0.2)
    assert not task.done(), "run_async should serve until stopped"

    await server.stop()
    await asyncio.wait_for(task, timeout=5)


async def test_run_async_can_be_cancelled(host_key) -> None:
    """Embedded in a host application, cancellation is how it gets shut down."""
    server = _server(host_key)
    task = asyncio.create_task(server.run_async(host="127.0.0.1", port=0))
    await asyncio.sleep(0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_async_installs_no_signal_handlers(host_key) -> None:
    """It may be one coroutine in a host's process; the host owns signals."""
    before = signal.getsignal(signal.SIGINT)
    server = _server(host_key)
    task = asyncio.create_task(server.run_async(host="127.0.0.1", port=0))
    await asyncio.sleep(0.2)
    try:
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        await server.stop()
        await asyncio.wait_for(task, timeout=5)


# -- run() and signals ---------------------------------------------------------


async def test_signal_handlers_install_on_this_platform(host_key) -> None:
    """Whichever mechanism this platform offers, one of them must take.

    On POSIX that is loop.add_signal_handler; on Windows the ProactorEventLoop
    has no such method and the signal.signal fallback is the live path. Both are
    asserted here rather than only the one this developer happens to run, since
    a silently-uninstalled handler means a server that never drains.
    """
    server = _server(host_key)
    loop = asyncio.get_running_loop()
    undo: list = []
    try:
        assert server._install_signal_handler(loop, signal.SIGINT, undo) is True
        assert undo, "an installed handler must be restorable"
    finally:
        for restore in undo:
            restore()


async def test_a_signal_starts_a_graceful_shutdown(host_key) -> None:
    """The handler's job is only to begin stop(); this checks it actually does."""
    server = _server(host_key)
    task = asyncio.create_task(server.run_async(host="127.0.0.1", port=0))
    await asyncio.sleep(0.2)

    server._signal_stop(signal.SIGINT)

    await asyncio.wait_for(task, timeout=10)
    assert server.active_sessions == 0


async def test_a_second_signal_does_not_restart_the_shutdown(
    host_key, caplog: pytest.LogCaptureFixture
) -> None:
    """An impatient operator hitting Ctrl+C twice must not corrupt the drain."""
    server = _server(host_key)
    task = asyncio.create_task(server.run_async(host="127.0.0.1", port=0))
    await asyncio.sleep(0.2)

    server._signal_stop(signal.SIGINT)
    await asyncio.wait_for(task, timeout=10)

    with caplog.at_level("WARNING", logger="wijjit_ssh"):
        server._signal_stop(signal.SIGINT)
    assert "already shutting down" in caplog.text


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows delivers no SIGTERM (TerminateProcess runs no handlers), and "
    "CTRL_C_EVENT cannot be delivered to a new process group",
)
def test_run_drains_on_sigterm(host_key, tmp_path) -> None:
    """The systemd path: SIGTERM must drain rather than kill.

    Runs the server in a subprocess, since run() owns the process and installs
    real signal handlers.
    """
    import subprocess
    import textwrap
    import time

    key_path = tmp_path / "host_key"
    key_path.write_bytes(host_key.export_private_key())

    script = textwrap.dedent(f"""
        import sys
        from wijjit import Wijjit, render_template_string
        from wijjit_ssh import WijjitSSH, load_host_keys

        def make_app(session):
            app = Wijjit(backend=session.backend)
            @app.view("main", default=True)
            def main():
                return render_template_string("{{% frame %}}{{% endframe %}}")
            return app

        server = WijjitSSH(
            make_app,
            host_keys=load_host_keys([{str(key_path)!r}]),
            allow_anonymous=True,
        )
        server.run(host="127.0.0.1", port=0)
        print("EXITED CLEANLY", flush=True)
        """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stderr is not None
        # Wait until the server is actually listening before signalling it.
        #
        # This wait is load-bearing, not politeness. run() installs its signal
        # handlers inside asyncio.run(), and start() binds and logs strictly
        # after that, so this line is proof that a SIGTERM will now be *caught*.
        # Keying readiness off a print() before run() instead - which is how this
        # test was originally written - races handler installation, and losing
        # that race means the default SIGTERM disposition kills the child
        # outright: no "EXITED CLEANLY", and both pipes empty. That is exactly
        # how this test failed the first time it ever ran (it is POSIX-only, and
        # the repo was developed on Windows, so CI was its first execution).
        #
        # Coupled to the "Listening on" text in WijjitSSH.start(); if that log
        # line is reworded, reword it here too.
        deadline = time.monotonic() + 30.0
        while "Listening on" not in (line := proc.stderr.readline()):
            if not line:
                raise AssertionError("server exited before it began listening")
            if time.monotonic() > deadline:
                raise AssertionError("server never reported that it was listening")

        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=15)
    except Exception:
        proc.kill()
        raise

    assert "EXITED CLEANLY" in stdout, f"run() did not return on SIGTERM: {stderr}"
    assert proc.returncode == 0
    assert "shutting down gracefully" in stderr
