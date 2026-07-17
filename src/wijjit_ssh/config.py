"""Server configuration: every knob :class:`~wijjit_ssh.server.WijjitSSH` takes.

One dataclass rather than a long keyword list, so a deployment can build its
config once (from a file, from the environment, from argparse), inspect it, pass
it around, and diff it against the defaults.

Every limit ships with a real default. The reasoning is that a server whose
limits are opt-in is a server that is unbounded in every deployment where nobody
thought about it, which is most of them. The defaults here (100 sessions, 10 per
IP, a 10-minute idle timeout) are meant to be invisible to a legitimate user and
finite to an abusive one.

The one deliberately-off default is ``connect_rate``: rate limiting a service you
have not measured is how you throttle your own health check, so it is opt-in.

Examples
--------
Defaults, with just the two things that have no sensible default::

    >>> config = ServerConfig(
    ...     host_keys=[ensure_host_key("ssh_host_key")],
    ...     auth=AuthorizedKeys("~/.ssh/authorized_keys"),
    ... )

A tighter posture for an exposed deployment::

    >>> config = ServerConfig(
    ...     port=22,
    ...     host_keys=load_host_keys(["/var/lib/myapp/host_key"]),
    ...     auth=AuthorizedKeys("/etc/myapp/authorized_keys"),
    ...     max_sessions=25,
    ...     max_per_ip=2,
    ...     connect_rate=1.0,
    ...     idle_timeout=120.0,
    ... )
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from wijjit_ssh.auth import AuthPolicy
from wijjit_ssh.keys import HostKeySource
from wijjit_ssh.logging import EventHook

__all__ = ["ServerConfig"]


@dataclass
class ServerConfig:
    """Everything :class:`~wijjit_ssh.server.WijjitSSH` needs to serve.

    Parameters
    ----------
    host : str
        Bind address. Default ``""`` (all interfaces). Use ``"127.0.0.1"`` to
        serve only local clients.
    port : int
        Bind port. Default 8022 - a high port, so the server needs no
        privileges. Pass 0 to let the OS choose (tests do this).
    host_keys : sequence of str, PathLike, or asyncssh.SSHKey
        The server's identity; see :mod:`wijjit_ssh.keys`. Paths are resolved
        when the server is constructed. Passing several supports rotation.
        Required to serve, though not to construct.
    auth : AuthPolicy, optional
        How clients authenticate; see :mod:`wijjit_ssh.auth`. Required unless
        ``allow_anonymous=True``.
    allow_anonymous : bool
        Permit running with **no authentication**. Default False. This exists so
        that serving an unauthenticated SSH server is something you type, not
        something you inherit by forgetting an argument.
    max_sessions : int
        Concurrent sessions across the whole server; further sessions are
        refused with a message. Default 100. Each session is a live Wijjit app,
        so this is really a memory bound - size it against what your app costs.
    max_per_ip : int
        Concurrent *connections* from one IP, refused pre-authentication.
        Default 10. Note this counts connections rather than sessions: the point
        is to reject an abusive peer before spending a key exchange on it, and
        at that moment no session exists yet. Sessions per IP are transitively
        bounded by this, since every session lives inside a connection.
    connect_rate : float
        Sustained connections per second per IP, as a token bucket refill rate.
        Default 0.0, which disables rate limiting: throttling a service you have
        not measured mostly succeeds at throttling your own health check.
    connect_burst : int
        Bucket capacity for ``connect_rate`` - how many connections an IP may
        make at once before the sustained rate binds. Default 20. Ignored when
        ``connect_rate`` is 0.
    login_timeout : float
        Seconds a client may take to authenticate before being dropped. Default
        30.0, tightening asyncssh's own 120s. Bounds how long an unauthenticated
        peer can hold resources.
    idle_timeout : float or None
        Seconds without client input before a session is closed, or None to
        disable. Default 600.0. This is what reaps the forgotten ssh window that
        would otherwise hold a session slot forever.
    session_timeout : float or None
        Hard cap on session duration regardless of activity, or None to disable.
        Default None. Unlike ``idle_timeout`` this will interrupt someone who is
        actively using the app, so it is off unless a deployment wants it.
    keepalive_interval : float
        Seconds between keepalives on an idle connection, or 0 to disable.
        Default 30.0. Reaps peers whose TCP connection died without a FIN
        (a laptop lid, a NAT timeout) and would otherwise linger.
    keepalive_count_max : int
        Unanswered keepalives before the connection is dropped. Default 3, so a
        dead peer is reclaimed in ~90s at the default interval.
    shutdown_grace : float
        Seconds :meth:`~wijjit_ssh.server.WijjitSSH.stop` gives sessions to exit
        cleanly before cancelling them. Default 5.0. Cleanly means the app's
        teardown runs and the client's terminal is restored; cancelling skips
        that, so this trades shutdown latency against leaving a client's
        terminal in the alternate screen buffer.
    banner : str or None
        Text sent to clients before authentication, or None. Default None.
        Shown by the client even if auth then fails, so it is the place for a
        legal notice, not for anything you would rather an unauthenticated
        stranger did not read.
    on_event : callable, optional
        ``(event: str, fields: Mapping[str, object]) -> None``, called for
        lifecycle events; see :class:`~wijjit_ssh.logging.EventEmitter`. Lets a
        deployment wire up metrics without this package depending on a metrics
        library. Exceptions from the hook are logged and swallowed.

    Raises
    ------
    ValueError
        If any value is out of range.

    Examples
    --------
    >>> config = ServerConfig(max_sessions=10, idle_timeout=60.0)
    >>> config.max_sessions
    10
    >>> ServerConfig(max_sessions=0)
    Traceback (most recent call last):
        ...
    ValueError: max_sessions must be >= 1, got 0
    """

    host: str = ""
    port: int = 8022
    host_keys: Sequence[HostKeySource] = ()
    auth: AuthPolicy | None = None
    allow_anonymous: bool = False
    max_sessions: int = 100
    max_per_ip: int = 10
    connect_rate: float = 0.0
    connect_burst: int = 20
    login_timeout: float = 30.0
    idle_timeout: float | None = 600.0
    session_timeout: float | None = None
    keepalive_interval: float = 30.0
    keepalive_count_max: int = 3
    shutdown_grace: float = 5.0
    banner: str | None = None
    on_event: EventHook | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if not 0 <= self.port <= 65535:
            raise ValueError(f"port must be between 0 and 65535, got {self.port}")
        if self.max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {self.max_sessions}")
        if self.max_per_ip < 1:
            raise ValueError(f"max_per_ip must be >= 1, got {self.max_per_ip}")
        if self.connect_rate < 0:
            raise ValueError(f"connect_rate must be >= 0, got {self.connect_rate}")
        if self.connect_burst < 1:
            raise ValueError(f"connect_burst must be >= 1, got {self.connect_burst}")
        if self.login_timeout <= 0:
            raise ValueError(f"login_timeout must be > 0, got {self.login_timeout}")
        if self.keepalive_interval < 0:
            raise ValueError(
                f"keepalive_interval must be >= 0, got {self.keepalive_interval}"
            )
        if self.keepalive_count_max < 1:
            raise ValueError(
                f"keepalive_count_max must be >= 1, got {self.keepalive_count_max}"
            )
        if self.shutdown_grace < 0:
            raise ValueError(f"shutdown_grace must be >= 0, got {self.shutdown_grace}")

        for name in ("idle_timeout", "session_timeout"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be > 0 or None, got {value}")

    @classmethod
    def _field_names(cls) -> frozenset[str]:
        """Names accepted as keyword overrides by :class:`WijjitSSH`."""
        return frozenset(f.name for f in fields(cls))

    def replace(self, **overrides: Any) -> "ServerConfig":
        """Return a copy with ``overrides`` applied, re-validated.

        Unlike :func:`dataclasses.replace`, unknown names raise rather than
        being silently accepted. That matters for a limits API: a typo'd
        ``max_session=1`` that quietly does nothing leaves a server the operator
        believes is bounded and which is not.

        Parameters
        ----------
        **overrides : Any
            Field names and values to change.

        Returns
        -------
        ServerConfig
            A new, validated config. The original is unchanged.

        Raises
        ------
        TypeError
            If a name is not a config field.
        ValueError
            If a value is out of range.

        Examples
        --------
        >>> base = ServerConfig(max_sessions=100)
        >>> base.replace(max_sessions=5).max_sessions
        5
        >>> base.max_sessions   # unchanged
        100
        """
        unknown = set(overrides) - self._field_names()
        if unknown:
            known = ", ".join(sorted(self._field_names()))
            raise TypeError(
                f"Unknown ServerConfig option(s): {', '.join(sorted(unknown))}. "
                f"Valid options are: {known}"
            )
        current = {f.name: getattr(self, f.name) for f in fields(self)}
        return ServerConfig(**{**current, **overrides})
