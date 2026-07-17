"""Shared fixtures for the wijjit-ssh test suite.

Kept deliberately thin: the machinery lives in ``_client.py`` and is imported by
name, so a test reads as what it does rather than as fixture indirection. What
belongs here is only the part that needs pytest's teardown - making sure every
server a test starts is closed, whatever the test did or how it failed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest

from tests._client import _Server, start_server


@pytest.fixture
async def serve() -> AsyncIterator[Callable[..., object]]:
    """Factory fixture: start WijjitSSH servers, and close them afterwards.

    Yields
    ------
    callable
        ``await serve(**config_overrides) -> _Server``. Call it as many times as
        a test needs; every server is closed on teardown, including if the test
        raised.

    Examples
    --------
    >>> async def test_something(serve):          # doctest: +SKIP
    ...     server = await serve(max_sessions=1)
    ...     client = await _open(server)
    """
    started: list[_Server] = []

    async def _serve(**overrides: object) -> _Server:
        server = await start_server(**overrides)
        started.append(server)
        return server

    yield _serve

    for server in started:
        server.close()


@pytest.fixture
async def server(serve) -> _Server:
    """One WijjitSSH with default config, on an ephemeral port."""
    return await serve()
