"""Smoke tests for ``examples/``.

The examples are the first code most people run, and nothing else in the tree
imports them - so nothing notices when one breaks. Two bugs have already shipped
here for exactly that reason: a Greet button that did nothing, and an anonymous
fallback that published an unauthenticated server on every interface while
printing that it was fine on localhost.

These are deliberately coarse. They are not a second copy of the round-trip
suite, and they assert nothing about ``psutil`` readings, chart layout, or how
many spaces a frame border uses. What they pin is the two things a broken
example has in common: **it does not build the server it claims to**, and **the
screen a user lands on is not the one the docstring describes**.

Each example is loaded by path into its own module object, so a test gets a
fresh ``ChatRoom``/``Sampler`` rather than one carrying another test's state.
``Path.home()`` and the working directory are both redirected into ``tmp_path``,
because ``build_server()`` reads ``~/.ssh/authorized_keys`` to decide which
branch to take and writes ``ssh_host_key`` next to itself - and a test whose
result depends on whether the developer running it happens to have SSH keys is
worse than no test.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType

import asyncssh
import pytest

from tests._client import _open, _Server

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

#: Every example that decides its own bind address. ``dashboard_ssh`` is absent
#: on purpose: it has no anonymous path to bind carefully, it exits instead.
BINDING_EXAMPLES = ["hello_ssh", "chat_ssh"]


@pytest.fixture
def example(tmp_path, monkeypatch) -> AsyncIterator[Callable[..., ModuleType]]:
    """Load an example module with its filesystem inputs pointed at ``tmp_path``.

    Yields
    ------
    callable
        ``example(name, *, authorized_keys=False) -> module``. Pass
        ``authorized_keys=True`` to put a real public key in the fake home first,
        which is what sends ``build_server()`` down its authenticated branch.
    """
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # ensure_host_key() writes a relative path, so this is where it lands.
    monkeypatch.chdir(tmp_path)
    # A developer with this exported in their shell must not change what the
    # tests below assert. The override itself is covered by its own test.
    monkeypatch.delenv("WIJJIT_SSH_HOST", raising=False)

    loaded: list[str] = []

    def _load(name: str, *, authorized_keys: bool = False) -> ModuleType:
        if authorized_keys:
            key = asyncssh.generate_private_key("ssh-ed25519")
            (home / ".ssh" / "authorized_keys").write_bytes(key.export_public_key())

        path = EXAMPLES / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_example_{name}", path)
        assert spec is not None and spec.loader is not None, path
        module = importlib.util.module_from_spec(spec)
        # Registered before exec so the example is importable by name while it
        # runs, and so a traceback out of one points at the real file.
        sys.modules[spec.name] = module
        loaded.append(spec.name)
        spec.loader.exec_module(module)
        return module

    yield _load

    for name in loaded:
        sys.modules.pop(name, None)


@pytest.fixture
async def serve_example(example) -> AsyncIterator[Callable[..., object]]:
    """Start an example's own ``build_server()`` on an ephemeral loopback port.

    The server under test is the one the example returns - its app factory, its
    event hook, its config - rather than a rebuild of it here, which could drift
    from the file a user actually runs and take the test with it.
    """
    started: list[_Server] = []

    async def _start(name: str, **kwargs: object) -> _Server:
        server = example(name, **kwargs).build_server()
        acceptor = await server.start(host="127.0.0.1", port=0)
        started.append(_Server(acceptor, server))
        return started[-1]

    yield _start

    for server in started:
        server.close()


def _dashboard(example, **kwargs: object) -> ModuleType:
    """Load ``dashboard_ssh``, or skip if its optional dependency is absent.

    ``dev`` includes the ``examples`` group precisely so these two run by
    default, so this guard should never fire under ``uv sync``. It is here for
    the environment that is not that one - an sdist unpacked and tested against
    an install, say - where skipping ``psutil`` beats a collection error that
    takes the other 353 tests down with it.
    """
    pytest.importorskip("psutil")
    return example("dashboard_ssh", **kwargs)


# -- what the examples decide to expose ---------------------------------------


@pytest.mark.parametrize("name", BINDING_EXAMPLES)
def test_the_anonymous_fallback_binds_loopback(example, name: str) -> None:
    """No authentication must not also mean no network boundary.

    Both examples fall back to ``allow_anonymous=True`` when they find no
    ``authorized_keys``, so the demo runs on a machine with no SSH keys set up.
    ``ServerConfig.host`` defaults to ``""`` - every interface - so without an
    explicit bind, that fallback offered a session to anyone who could reach the
    port, which on a laptop is everyone else on the wifi.
    """
    server = example(name).build_server()

    assert server.config.allow_anonymous is True
    assert server.config.auth is None
    assert server.config.host == "127.0.0.1"


@pytest.mark.parametrize("name", BINDING_EXAMPLES)
def test_an_authenticated_example_serves_every_interface(example, name: str) -> None:
    """The narrowed bind is about the fallback, not a blanket loopback default.

    With a key to check against, ``""`` is the right answer again - otherwise
    the fix would quietly break the case these examples are actually written for.
    """
    server = example(name, authorized_keys=True).build_server()

    assert server.config.allow_anonymous is False
    assert server.config.auth is not None
    assert server.config.host == ""


@pytest.mark.parametrize("name", BINDING_EXAMPLES)
def test_wijjit_ssh_host_overrides_the_bind(example, monkeypatch, name: str) -> None:
    """The escape hatch ``deploy/Dockerfile`` depends on.

    Docker forwards a published port to the container's own address, so a
    loopback bind inside one is reachable by nobody and the image sets this to
    ``0.0.0.0``. What keeps that safe is the host side: ``compose.yaml``
    publishes to ``127.0.0.1``.
    """
    monkeypatch.setenv("WIJJIT_SSH_HOST", "0.0.0.0")

    assert example(name).build_server().config.host == "0.0.0.0"


def test_the_dashboard_refuses_to_run_unauthenticated(example) -> None:
    """The contrast the dashboard is written to draw.

    It serves the machine's process table and the address of everyone connected,
    so it has no anonymous fallback to bind carefully - it exits and says how to
    fix it. ``allow_anonymous`` is a decision about what an app exposes, not a
    default to inherit from the example next door.
    """
    dashboard = _dashboard(example)

    with pytest.raises(SystemExit) as excinfo:
        dashboard.build_server()

    message = str(excinfo.value)
    assert "authorized_keys" in message
    assert "ssh-keygen" in message


def test_the_dashboard_watches_without_being_reaped(example) -> None:
    """A dashboard is watched, not typed at.

    The default 10-minute idle timeout would disconnect exactly the people using
    this as intended, and ``on_event`` is what stops the sampler when the last
    window closes - without it the process samples an empty room forever.
    """
    server = _dashboard(example, authorized_keys=True).build_server()

    assert server.config.idle_timeout is None
    assert server.config.on_event is not None


# -- what the examples actually put on screen ---------------------------------
#
# Over a real socket, through the example's own build_server(). These are the
# ones that catch a template that no longer parses or an action that no longer
# fires - the class of bug that is invisible until someone runs the file.


async def test_hello_renders_and_its_button_works(serve_example) -> None:
    """The example's headline claim, end to end.

    The Greet button shipped broken once. Rendering the frame proves the template
    still parses; pressing the button proves the handler is still reached, which
    is the half a screenshot would not have caught.
    """
    server = await serve_example("hello_ssh")

    client = await _open(server, username="ada")
    async with client.conn:
        screen = await client.expect("Hello, ada!")
        assert "80x24" in screen
        assert "Greeted 0 time(s)" in screen

        client.send(b"\t")  # focus the name field...
        client.send(b"\t")  # ...then the button
        client.send(b"\r")  # press it
        await client.expect("Greeted 1 time(s)")


async def test_chat_pushes_a_join_into_an_already_open_window(serve_example) -> None:
    """The thing that is only true over SSH: one process, two live apps.

    Alice is parked in ``read_input_async`` when Bob connects, and nothing she
    does makes his arrival appear. The room posts from Bob's task and calls
    ``refresh()`` on her app from outside it, which is the whole point of the
    example - and it is wired through ``on_event``, so it breaks silently.
    """
    server = await serve_example("chat_ssh")

    alice = await _open(server, username="alice")
    async with alice.conn:
        await alice.expect("you are alice")

        bob = await _open(server, username="bob")
        async with bob.conn:
            await alice.expect("bob joined")
