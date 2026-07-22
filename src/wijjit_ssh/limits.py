"""Resource limits: how many sessions, from whom, for how long.

Without this module a Wijjit SSH server is unbounded in every direction that
matters. Any peer can open sessions until the process runs out of memory, a
forgotten ``ssh`` window holds a session slot forever, and a shutdown has no way
to find the sessions it needs to drain.

Design
------
Everything here is **pure bookkeeping and policy**: no sockets, no asyncssh
imports, and the clock is injectable. Sessions are reached only through the
:class:`ManagedSession` protocol. That is what lets the real assertions in
``test_limits.py`` run as fast unit tests with a fake clock, leaving the
over-SSH tests to prove only that the wiring is connected - the same split that
:mod:`wijjit_ssh.input`'s decoder tests already use.

Two chokepoints, not one
------------------------
``SPEC.md`` §8 lists "per-IP concurrency + connect rate limit" as one bullet, but
they cannot share a hook, and the difference is load-bearing:

* **Per-IP limits and the rate limit are pre-authentication**, checked when the
  TCP connection arrives. The entire point is to not spend a key exchange on an
  abusive peer, so waiting for auth would defeat them.
* **``max_sessions`` is inherently post-authentication.** A session only exists
  once a channel is opened, which requires a successful userauth.

So per-IP counts **connections** while the global cap counts **sessions**. The
per-IP session count is bounded transitively, since every session lives inside a
connection.

There is no locking anywhere in this module. It is correct only because asyncio
is single-threaded and none of these methods await: each runs to completion
before another callback can observe the state. :meth:`SessionRegistry.try_admit`
is one call rather than a check followed by a register for exactly this reason -
it makes the atomicity structural rather than a comment that a later refactor
can invalidate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from wijjit_ssh.logging import get_logger

__all__ = [
    "IdleTimer",
    "ManagedSession",
    "Rejection",
    "SessionRegistry",
    "TokenBucket",
]

logger = get_logger(__name__)

#: Why a session ended or was refused. Slugs rather than prose: these reach logs
#: and the on_event metrics hook, where a stable label is worth more than a
#: readable sentence (the readable sentence is Rejection.message).
REASON_SERVER_FULL = "server_full"
REASON_PER_IP = "per_ip"
REASON_RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class Rejection:
    """Why a connection or session was refused, in both registers.

    Attributes
    ----------
    reason : str
        Stable slug for logs and metrics, e.g. ``"server_full"``.
    message : str
        Human-readable text for the client. Worth writing carefully: it is the
        only thing a locked-out user sees, and "try again later" versus "you
        have too many sessions open" is the difference between a support ticket
        and a self-service fix.
    """

    reason: str
    message: str


class ManagedSession(Protocol):
    """What :class:`SessionRegistry` needs of a session.

    Structural, so the registry never imports the server and the tests never
    need a socket.

    Attributes
    ----------
    session_id : str
        Correlation id; see :func:`~wijjit_ssh.logging.new_session_id`.
    peer_ip : str
        Client address, for per-IP accounting.
    username : str
        Authenticated username.
    started_at : float
        Monotonic timestamp of admission.
    """

    session_id: str
    peer_ip: str
    username: str
    started_at: float

    def request_close(self, reason: str, message: str | None = None) -> None:
        """Ask the session to shut down cleanly. Must be idempotent."""
        ...

    def abort(self) -> None:
        """Force the session down now, having declined to exit cleanly."""
        ...


class TokenBucket:
    """Classic token bucket: sustained ``rate`` per second, up to ``burst`` at once.

    Chosen over a fixed window because a window lets a peer make ``burst``
    connections at the end of one window and ``burst`` more at the start of the
    next - twice the intended rate, at the worst possible moment. A bucket
    refills continuously, so the sustained rate holds across any interval.

    Refill is computed lazily from the clock on each :meth:`consume` rather than
    on a timer, so an idle bucket costs nothing and there is no task to cancel.

    Parameters
    ----------
    rate : float
        Tokens added per second. **0 disables the bucket entirely** -
        :meth:`consume` always allows. This is the default posture: see
        :class:`~wijjit_ssh.config.ServerConfig.connect_rate`.
    burst : float
        Maximum tokens held; the bucket starts full, so a fresh peer may make
        ``burst`` connections immediately.
    clock : callable, optional
        Returns monotonic seconds. Injectable so tests need no sleeping.

    Examples
    --------
    >>> bucket = TokenBucket(rate=1.0, burst=2)
    >>> bucket.consume(), bucket.consume()
    (True, True)
    >>> bucket.consume()          # burst exhausted, refill is 1/second
    False
    """

    def __init__(
        self,
        rate: float,
        burst: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._updated = clock()

    @property
    def enabled(self) -> bool:
        """Whether this bucket limits anything at all."""
        return self._rate > 0

    def consume(self, amount: float = 1.0) -> bool:
        """Take ``amount`` tokens if available.

        Parameters
        ----------
        amount : float, optional
            Tokens to take. Default 1.

        Returns
        -------
        bool
            True if taken (the caller may proceed); False if the bucket is dry.
            Always True when ``rate`` is 0.
        """
        if not self.enabled:
            return True

        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now

        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False


class IdleTimer:
    """Closes a session that has gone quiet, or that has simply run too long.

    Two independent deadlines, because they answer different questions:

    * ``idle_timeout`` reclaims the forgotten ``ssh`` window - reset by every
      byte the client sends (:meth:`poke`).
    * ``session_timeout`` caps total duration regardless of activity. It will
      interrupt someone who is actively working, which is why it is off by
      default and why it is a separate deadline rather than a bound on the idle
      one.

    Owns real timers, which is why it lives outside :class:`SessionRegistry` -
    keeping the registry free of them is what makes the registry testable
    without a loop.

    Parameters
    ----------
    idle_timeout : float or None
        Seconds of silence before expiry, or None to disable.
    session_timeout : float or None
        Seconds since :meth:`start` before expiry, or None to disable.
    on_expire : callable
        ``(reason: str) -> None``, called with ``"idle_timeout"`` or
        ``"session_timeout"``. Called at most once.

    Examples
    --------
    >>> timer = IdleTimer(                       # doctest: +SKIP
    ...     idle_timeout=600.0,
    ...     session_timeout=None,
    ...     on_expire=lambda reason: session.request_close(reason),
    ... )
    >>> timer.start()                            # doctest: +SKIP
    >>> timer.poke()      # on each byte from the client   # doctest: +SKIP
    >>> timer.cancel()    # on teardown                    # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        idle_timeout: float | None,
        session_timeout: float | None,
        on_expire: Callable[[str], None],
    ) -> None:
        self._idle_timeout = idle_timeout
        self._session_timeout = session_timeout
        self._on_expire = on_expire
        self._idle_handle: asyncio.TimerHandle | None = None
        self._absolute_handle: asyncio.TimerHandle | None = None
        self._fired = False

    def start(self) -> None:
        """Arm both deadlines. Call once, when the session begins."""
        loop = asyncio.get_running_loop()
        if self._session_timeout is not None:
            self._absolute_handle = loop.call_later(
                self._session_timeout, self._expire, "session_timeout"
            )
        self._arm_idle()

    def poke(self) -> None:
        """Reset the idle deadline. Call on every byte received from the client.

        Cheap by construction: this runs on every keystroke, so it does no work
        beyond cancelling and rescheduling one timer handle. The absolute
        deadline is deliberately untouched.
        """
        if self._fired or self._idle_timeout is None:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._arm_idle()

    def cancel(self) -> None:
        """Disarm both deadlines. Idempotent; safe after expiry."""
        for handle in (self._idle_handle, self._absolute_handle):
            if handle is not None:
                handle.cancel()
        self._idle_handle = None
        self._absolute_handle = None

    def _arm_idle(self) -> None:
        if self._idle_timeout is None:
            return
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(
            self._idle_timeout, self._expire, "idle_timeout"
        )

    def _expire(self, reason: str) -> None:
        # Both deadlines can be in flight at once; whichever lands first wins and
        # the other must not fire a second close into a session already tearing
        # down.
        if self._fired:
            return
        self._fired = True
        self.cancel()
        self._on_expire(reason)


class SessionRegistry:
    """Tracks live sessions and enforces the bounds around them.

    See the module docstring for why connections and sessions are counted at
    different chokepoints, and why nothing here locks.

    Parameters
    ----------
    max_sessions : int, optional
        Concurrent sessions server-wide. Default 100.
    max_per_ip : int, optional
        Concurrent connections from one IP. Default 10.
    connect_rate : float, optional
        Sustained connections/second/IP; 0 disables. Default 0.
    connect_burst : int, optional
        Bucket capacity for ``connect_rate``. Default 20.
    clock : callable, optional
        Monotonic clock, injectable for tests.

    Examples
    --------
    >>> registry = SessionRegistry(max_sessions=2)
    >>> registry.check_connection("10.0.0.7") is None    # allowed
    True
    >>> registry.connection_opened("10.0.0.7")
    >>> registry.active_connections
    1
    """

    def __init__(
        self,
        *,
        max_sessions: int = 100,
        max_per_ip: int = 10,
        connect_rate: float = 0.0,
        connect_burst: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_sessions = max_sessions
        self._max_per_ip = max_per_ip
        self._connect_rate = connect_rate
        self._connect_burst = connect_burst
        self._clock = clock

        self._sessions: dict[str, ManagedSession] = {}
        self._connections_per_ip: dict[str, int] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._drained: asyncio.Event | None = None

    # -- connection admission (pre-auth) ---------------------------------------

    def check_connection(self, peer_ip: str) -> Rejection | None:
        """Decide whether to accept a new TCP connection from ``peer_ip``.

        Called before authentication, so this is cheap on purpose: it must cost
        far less than the key exchange it is declining to perform.

        Does **not** record the connection - call :meth:`connection_opened` for
        that, and only if this returned None.

        Parameters
        ----------
        peer_ip : str
            Client address.

        Returns
        -------
        Rejection or None
            None to accept.
        """
        if self._connections_per_ip.get(peer_ip, 0) >= self._max_per_ip:
            return Rejection(
                REASON_PER_IP,
                f"Too many connections from your address "
                f"(limit {self._max_per_ip}). Close one and try again.",
            )

        if self._connect_rate > 0 and not self._bucket_for(peer_ip).consume():
            return Rejection(
                REASON_RATE_LIMITED,
                "Too many connection attempts. Please wait and try again.",
            )
        return None

    def connection_opened(self, peer_ip: str) -> None:
        """Record an accepted connection.

        Parameters
        ----------
        peer_ip : str
            Client address.
        """
        self._connections_per_ip[peer_ip] = self._connections_per_ip.get(peer_ip, 0) + 1

    def connection_closed(self, peer_ip: str) -> None:
        """Release a connection previously passed to :meth:`connection_opened`.

        Tolerates an unknown IP: this is called from a ``connection_lost``
        callback, which must never raise, and an over-release would otherwise
        underflow the count and permanently loosen the limit for that peer.

        Parameters
        ----------
        peer_ip : str
            Client address.
        """
        remaining = self._connections_per_ip.get(peer_ip, 0) - 1
        if remaining > 0:
            self._connections_per_ip[peer_ip] = remaining
        else:
            # Drop the key rather than store a 0: otherwise the dict grows once
            # per distinct peer and never shrinks, which is a slow leak on a
            # public server.
            self._connections_per_ip.pop(peer_ip, None)
            self._buckets.pop(peer_ip, None)

    def _bucket_for(self, peer_ip: str) -> TokenBucket:
        bucket = self._buckets.get(peer_ip)
        if bucket is None:
            bucket = TokenBucket(
                self._connect_rate, self._connect_burst, clock=self._clock
            )
            self._buckets[peer_ip] = bucket
        return bucket

    # -- session admission (post-auth) -----------------------------------------

    def try_admit(self, session: ManagedSession) -> Rejection | None:
        """Register ``session`` if there is room, atomically.

        Deliberately one call rather than a check followed by a register: on a
        single-threaded loop a non-awaiting method is atomic by construction, and
        collapsing the two makes that structural instead of a comment a later
        refactor could invalidate.

        Parameters
        ----------
        session : ManagedSession
            The session asking to start.

        Returns
        -------
        Rejection or None
            None if admitted.
        """
        if len(self._sessions) >= self._max_sessions:
            return Rejection(
                REASON_SERVER_FULL,
                f"This server is at capacity ({self._max_sessions} sessions). "
                f"Please try again shortly.",
            )
        self._sessions[session.session_id] = session
        return None

    def release(self, session: ManagedSession) -> None:
        """Deregister a session. Idempotent.

        Parameters
        ----------
        session : ManagedSession
            The session that has ended.
        """
        self._sessions.pop(session.session_id, None)
        if not self._sessions and self._drained is not None:
            self._drained.set()

    @property
    def active_sessions(self) -> int:
        """How many sessions are live right now."""
        return len(self._sessions)

    @property
    def active_connections(self) -> int:
        """How many connections are live right now, across all peers."""
        return sum(self._connections_per_ip.values())

    def connections_from(self, peer_ip: str) -> int:
        """How many connections are live from one peer.

        Parameters
        ----------
        peer_ip : str
            Client address.

        Returns
        -------
        int
        """
        return self._connections_per_ip.get(peer_ip, 0)

    def sessions(self) -> list[ManagedSession]:
        """A snapshot of the live sessions.

        A copy, because callers iterate it while sessions close themselves and
        mutate the underlying dict.

        Returns
        -------
        list[ManagedSession]
        """
        return list(self._sessions.values())

    # -- shutdown ---------------------------------------------------------------

    async def drain(self, *, reason: str, message: str | None, grace: float) -> int:
        """Ask every session to end, and wait up to ``grace`` for them to.

        Clean exit matters here beyond tidiness: a session that ends properly
        runs the app's teardown, which leaves the alternate screen buffer and
        restores the client's terminal. A session that is aborted skips that and
        leaves a real person with a wedged terminal. So sessions are asked
        first, and only killed if they will not go.

        Parameters
        ----------
        reason : str
            Slug recorded for each session, e.g. ``"server_shutdown"``.
        message : str or None
            Text shown to each client.
        grace : float
            Seconds to wait before forcing. 0 forces immediately.

        Returns
        -------
        int
            How many sessions had to be aborted. 0 means everyone left cleanly.
        """
        if not self._sessions:
            return 0

        self._drained = asyncio.Event()
        count = len(self._sessions)
        logger.info("Draining %d session(s), grace %.1fs", count, grace)

        for session in self.sessions():
            try:
                session.request_close(reason, message)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "Session %s raised on request_close; will abort it",
                    session.session_id,
                )

        if grace > 0:
            try:
                await asyncio.wait_for(self._drained.wait(), grace)
            except (TimeoutError, asyncio.TimeoutError):
                pass

        stragglers = self.sessions()
        for session in stragglers:
            logger.warning(
                "Session %s did not exit within %.1fs; aborting it. The client's "
                "terminal may be left in the alternate screen buffer.",
                session.session_id,
                grace,
            )
            try:
                session.abort()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Session %s raised on abort", session.session_id)
            self.release(session)

        self._drained = None
        if not stragglers:
            logger.info("All %d session(s) exited cleanly", count)
        return len(stragglers)
