"""Tests for ServerConfig and how WijjitSSH resolves it.

The validation tests matter more than they look: this is a limits API, so a
value that is silently ignored (a typo, an out-of-range number coerced to
something harmless) leaves an operator believing a server is bounded when it is
not. Everything invalid should be loud and immediate.
"""

from __future__ import annotations

import asyncssh
import pytest

from wijjit_ssh import ServerConfig, WijjitSSH
from wijjit_ssh.auth import OpenAuth


def _app_factory(session):  # never called; these tests do not start a server
    raise AssertionError("factory should not run")


@pytest.fixture
def host_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519")


# -- defaults ------------------------------------------------------------------


def test_defaults_are_bounded() -> None:
    """Every limit must have a real default; an opt-in limit is no limit."""
    config = ServerConfig()
    assert config.max_sessions == 100
    assert config.max_per_ip == 10
    assert config.idle_timeout == 600.0
    assert config.login_timeout == 30.0
    assert config.keepalive_interval == 30.0


def test_rate_limiting_is_off_by_default() -> None:
    """The one limit that is opt-in: throttling an unmeasured service."""
    assert ServerConfig().connect_rate == 0.0


def test_optional_timeouts_may_be_disabled() -> None:
    config = ServerConfig(idle_timeout=None, session_timeout=None)
    assert config.idle_timeout is None
    assert config.session_timeout is None


# -- validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": -1}, "port must be between 0 and 65535"),
        ({"port": 70000}, "port must be between 0 and 65535"),
        ({"max_sessions": 0}, "max_sessions must be >= 1"),
        ({"max_per_ip": 0}, "max_per_ip must be >= 1"),
        ({"connect_rate": -1.0}, "connect_rate must be >= 0"),
        ({"connect_burst": 0}, "connect_burst must be >= 1"),
        ({"login_timeout": 0}, "login_timeout must be > 0"),
        ({"keepalive_interval": -1}, "keepalive_interval must be >= 0"),
        ({"keepalive_count_max": 0}, "keepalive_count_max must be >= 1"),
        ({"shutdown_grace": -1}, "shutdown_grace must be >= 0"),
        ({"idle_timeout": 0}, "idle_timeout must be > 0 or None"),
        ({"idle_timeout": -5}, "idle_timeout must be > 0 or None"),
        ({"session_timeout": 0}, "session_timeout must be > 0 or None"),
    ],
)
def test_out_of_range_values_raise(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ServerConfig(**kwargs)


def test_port_zero_is_allowed() -> None:
    """Tests bind port 0 to let the OS choose."""
    assert ServerConfig(port=0).port == 0


def test_keepalive_may_be_disabled_with_zero() -> None:
    assert ServerConfig(keepalive_interval=0).keepalive_interval == 0


# -- replace -------------------------------------------------------------------


def test_replace_returns_a_new_validated_config() -> None:
    base = ServerConfig(max_sessions=100)
    assert base.replace(max_sessions=5).max_sessions == 5
    assert base.max_sessions == 100  # original untouched


def test_replace_revalidates() -> None:
    with pytest.raises(ValueError, match="max_sessions must be >= 1"):
        ServerConfig().replace(max_sessions=0)


def test_replace_rejects_an_unknown_option() -> None:
    """A typo'd limit that silently does nothing is the worst failure mode here."""
    with pytest.raises(TypeError) as excinfo:
        ServerConfig().replace(max_session=1)  # note: missing "s"
    assert "max_session" in str(excinfo.value)
    assert "max_sessions" in str(excinfo.value)  # the valid names are listed


def test_replace_preserves_unmentioned_fields() -> None:
    base = ServerConfig(max_sessions=7, banner="hello", idle_timeout=None)
    updated = base.replace(port=2222)
    assert (updated.max_sessions, updated.banner, updated.idle_timeout) == (
        7,
        "hello",
        None,
    )


# -- WijjitSSH resolution ------------------------------------------------------


def test_server_accepts_config_fields_as_keywords(host_key) -> None:
    """The common case needs no config object."""
    server = WijjitSSH(
        _app_factory, host_keys=[host_key], allow_anonymous=True, max_sessions=5
    )
    assert server.config.max_sessions == 5


def test_server_accepts_a_config_object(host_key) -> None:
    config = ServerConfig(host_keys=[host_key], allow_anonymous=True, max_sessions=5)
    assert WijjitSSH(_app_factory, config).config.max_sessions == 5


def test_keywords_override_the_config_object(host_key) -> None:
    config = ServerConfig(host_keys=[host_key], allow_anonymous=True, max_sessions=5)
    server = WijjitSSH(_app_factory, config, max_sessions=9)
    assert server.config.max_sessions == 9
    assert config.max_sessions == 5  # the caller's config is not mutated


def test_server_rejects_an_unknown_keyword(host_key) -> None:
    with pytest.raises(TypeError, match="max_session"):
        WijjitSSH(
            _app_factory, host_keys=[host_key], allow_anonymous=True, max_session=1
        )


def test_server_still_fails_closed_without_auth(host_key) -> None:
    """The config rework must not have loosened the M2 posture."""
    with pytest.raises(ValueError, match="requires an auth policy"):
        WijjitSSH(_app_factory, host_keys=[host_key])
    with pytest.raises(ValueError, match="requires an auth policy"):
        WijjitSSH(_app_factory, ServerConfig(host_keys=[host_key]))


def test_server_accepts_auth_through_a_config_object(host_key) -> None:
    config = ServerConfig(host_keys=[host_key], auth=OpenAuth())
    assert WijjitSSH(_app_factory, config).config.auth is not None


def test_server_defaults_are_the_config_defaults(host_key) -> None:
    server = WijjitSSH(_app_factory, host_keys=[host_key], allow_anonymous=True)
    assert server.config.max_sessions == ServerConfig().max_sessions


async def test_start_uses_config_host_and_port(host_key) -> None:
    server = WijjitSSH(
        _app_factory,
        host_keys=[host_key],
        allow_anonymous=True,
        host="127.0.0.1",
        port=0,
    )
    acceptor = await server.start()
    try:
        assert acceptor.get_port() != 0  # the OS assigned one
    finally:
        acceptor.close()


async def test_start_arguments_override_config(host_key) -> None:
    server = WijjitSSH(_app_factory, host_keys=[host_key], allow_anonymous=True, port=1)
    acceptor = await server.start(host="127.0.0.1", port=0)
    try:
        assert acceptor.get_port() not in (0, 1)
    finally:
        acceptor.close()
