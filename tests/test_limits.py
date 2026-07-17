"""Unit tests for the limits machinery: buckets, timers, registry, drain.

Fast and deterministic: the clock is injected, sessions are fakes, and nothing
opens a socket. These carry the real assertions about limit behavior. The
over-SSH tests that follow in test_roundtrip.py only prove the wiring is
connected - the same split the decoder tests already use, and the reason the
timing here is exact rather than a sleep-and-hope.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from wijjit_ssh.limits import IdleTimer, Rejection, SessionRegistry, TokenBucket


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSession:
    """A ManagedSession that records what was asked of it."""

    def __init__(self, session_id: str, peer_ip: str = "10.0.0.7") -> None:
        self.session_id = session_id
        self.peer_ip = peer_ip
        self.username = "ada"
        self.started_at = 0.0
        self.closed_with: list[tuple[str, str | None]] = []
        self.aborted = False
        self.registry: SessionRegistry | None = None

    def request_close(self, reason: str, message: str | None = None) -> None:
        self.closed_with.append((reason, message))
        # A real session releases itself once its app has torn down.
        if self.registry is not None:
            self.registry.release(self)

    def abort(self) -> None:
        self.aborted = True


class StubbornSession(FakeSession):
    """A session that is asked to close and does not."""

    def request_close(self, reason: str, message: str | None = None) -> None:
        self.closed_with.append((reason, message))


# -- TokenBucket ---------------------------------------------------------------


def test_bucket_allows_a_burst_then_denies() -> None:
    bucket = TokenBucket(rate=1.0, burst=3, clock=FakeClock())
    assert [bucket.consume() for _ in range(3)] == [True, True, True]
    assert bucket.consume() is False


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, burst=2, clock=clock)
    assert bucket.consume() and bucket.consume()
    assert bucket.consume() is False

    clock.advance(0.5)  # 2/sec * 0.5s = 1 token
    assert bucket.consume() is True
    assert bucket.consume() is False


def test_bucket_refill_is_capped_at_burst() -> None:
    """An idle peer banks capacity up to burst, not indefinitely."""
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=2, clock=clock)
    clock.advance(3600)  # an hour of idleness
    assert [bucket.consume() for _ in range(3)] == [True, True, False]


def test_bucket_with_zero_rate_never_limits() -> None:
    """The default posture: rate limiting is opt-in."""
    bucket = TokenBucket(rate=0.0, burst=1, clock=FakeClock())
    assert all(bucket.consume() for _ in range(1000))
    assert bucket.enabled is False


def test_bucket_sustains_its_rate_across_a_window_boundary() -> None:
    """A fixed window would allow 2x burst here; a bucket must not."""
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=5, clock=clock)
    for _ in range(5):
        assert bucket.consume() is True
    clock.advance(1.0)  # one window later, one token
    assert bucket.consume() is True
    assert bucket.consume() is False


# -- per-IP connection limits --------------------------------------------------


def test_connections_are_allowed_up_to_the_per_ip_cap() -> None:
    registry = SessionRegistry(max_per_ip=2)
    for _ in range(2):
        assert registry.check_connection("10.0.0.7") is None
        registry.connection_opened("10.0.0.7")

    rejection = registry.check_connection("10.0.0.7")
    assert isinstance(rejection, Rejection)
    assert rejection.reason == "per_ip"
    assert "Too many connections" in rejection.message


def test_the_per_ip_cap_does_not_affect_other_peers() -> None:
    registry = SessionRegistry(max_per_ip=1)
    registry.connection_opened("10.0.0.7")
    assert registry.check_connection("10.0.0.7") is not None
    assert registry.check_connection("10.0.0.8") is None


def test_closing_a_connection_frees_a_slot() -> None:
    registry = SessionRegistry(max_per_ip=1)
    registry.connection_opened("10.0.0.7")
    assert registry.check_connection("10.0.0.7") is not None

    registry.connection_closed("10.0.0.7")
    assert registry.check_connection("10.0.0.7") is None


def test_over_releasing_a_connection_cannot_loosen_the_limit() -> None:
    """connection_lost must never raise, so extra closes are tolerated -- but
    they must not underflow the count and grant the peer extra slots."""
    registry = SessionRegistry(max_per_ip=1)
    registry.connection_opened("10.0.0.7")
    for _ in range(5):
        registry.connection_closed("10.0.0.7")

    assert registry.connections_from("10.0.0.7") == 0
    registry.connection_opened("10.0.0.7")
    assert registry.check_connection("10.0.0.7") is not None  # still capped at 1


def test_per_ip_state_is_dropped_when_a_peer_leaves() -> None:
    """Otherwise the dict grows once per distinct peer forever."""
    registry = SessionRegistry()
    registry.connection_opened("10.0.0.7")
    registry.connection_closed("10.0.0.7")
    assert registry.connections_from("10.0.0.7") == 0
    assert registry.active_connections == 0


def test_the_rate_limit_rejects_once_the_burst_is_spent() -> None:
    clock = FakeClock()
    registry = SessionRegistry(
        max_per_ip=100, connect_rate=1.0, connect_burst=2, clock=clock
    )
    for _ in range(2):
        assert registry.check_connection("10.0.0.7") is None

    rejection = registry.check_connection("10.0.0.7")
    assert isinstance(rejection, Rejection)
    assert rejection.reason == "rate_limited"

    clock.advance(1.0)
    assert registry.check_connection("10.0.0.7") is None


def test_the_rate_limit_is_per_peer() -> None:
    registry = SessionRegistry(connect_rate=1.0, connect_burst=1, clock=FakeClock())
    assert registry.check_connection("10.0.0.7") is None
    assert registry.check_connection("10.0.0.7") is not None
    assert registry.check_connection("10.0.0.8") is None  # unaffected


def test_the_per_ip_cap_is_checked_before_the_rate_limit() -> None:
    """A peer already at its connection cap should hear about that, not about
    rate -- and its bucket should not be charged for a connection we refused."""
    registry = SessionRegistry(
        max_per_ip=1, connect_rate=1.0, connect_burst=5, clock=FakeClock()
    )
    registry.connection_opened("10.0.0.7")

    rejection = registry.check_connection("10.0.0.7")
    assert rejection is not None and rejection.reason == "per_ip"

    registry.connection_closed("10.0.0.7")
    # The bucket still has its full burst: the refused attempts cost nothing.
    for _ in range(5):
        assert registry.check_connection("10.0.0.7") is None


# -- max_sessions --------------------------------------------------------------


def test_sessions_are_admitted_up_to_the_cap() -> None:
    registry = SessionRegistry(max_sessions=2)
    assert registry.try_admit(FakeSession("a")) is None
    assert registry.try_admit(FakeSession("b")) is None

    rejection = registry.try_admit(FakeSession("c"))
    assert isinstance(rejection, Rejection)
    assert rejection.reason == "server_full"
    assert "at capacity" in rejection.message
    assert registry.active_sessions == 2


def test_releasing_a_session_frees_a_slot() -> None:
    registry = SessionRegistry(max_sessions=1)
    first = FakeSession("a")
    registry.try_admit(first)
    assert registry.try_admit(FakeSession("b")) is not None

    registry.release(first)
    assert registry.try_admit(FakeSession("b")) is None


def test_releasing_twice_is_harmless() -> None:
    """Teardown is idempotent, so release will be called more than once."""
    registry = SessionRegistry()
    session = FakeSession("a")
    registry.try_admit(session)
    registry.release(session)
    registry.release(session)
    assert registry.active_sessions == 0


def test_sessions_returns_a_snapshot_not_a_live_view() -> None:
    """drain() iterates this while sessions release themselves from it."""
    registry = SessionRegistry()
    session = FakeSession("a")
    registry.try_admit(session)

    snapshot = registry.sessions()
    registry.release(session)
    assert snapshot == [session]  # unaffected by the release
    assert registry.sessions() == []


# -- drain ---------------------------------------------------------------------


async def test_drain_asks_every_session_to_close() -> None:
    registry = SessionRegistry()
    sessions = [FakeSession(str(i)) for i in range(3)]
    for session in sessions:
        session.registry = registry
        registry.try_admit(session)

    forced = await registry.drain(
        reason="server_shutdown", message="Server is shutting down.", grace=1.0
    )

    assert forced == 0
    assert registry.active_sessions == 0
    for session in sessions:
        assert session.closed_with == [("server_shutdown", "Server is shutting down.")]
        assert session.aborted is False


async def test_drain_returns_immediately_when_there_is_nothing_to_drain() -> None:
    registry = SessionRegistry()
    assert await registry.drain(reason="server_shutdown", message=None, grace=5.0) == 0


async def test_drain_aborts_sessions_that_will_not_leave() -> None:
    registry = SessionRegistry()
    stubborn = StubbornSession("stuck")
    registry.try_admit(stubborn)

    forced = await registry.drain(reason="server_shutdown", message=None, grace=0.05)

    assert forced == 1
    assert stubborn.aborted is True
    assert registry.active_sessions == 0  # not left dangling


async def test_drain_waits_for_a_slow_but_cooperative_session() -> None:
    """The grace period must actually be used, not just declared."""
    registry = SessionRegistry()
    slow = FakeSession("slow")
    registry.try_admit(slow)

    def close_soon(reason: str, message: str | None = None) -> None:
        slow.closed_with.append((reason, message))
        asyncio.get_running_loop().call_later(0.05, registry.release, slow)

    slow.request_close = close_soon  # type: ignore[method-assign]

    forced = await registry.drain(reason="server_shutdown", message=None, grace=2.0)
    assert forced == 0
    assert slow.aborted is False


async def test_drain_with_zero_grace_forces_immediately() -> None:
    registry = SessionRegistry()
    stubborn = StubbornSession("stuck")
    registry.try_admit(stubborn)

    forced = await registry.drain(reason="server_shutdown", message=None, grace=0)
    assert forced == 1
    assert stubborn.aborted is True


async def test_drain_warns_about_a_possibly_wedged_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Aborting skips the app teardown that restores the client's terminal."""
    registry = SessionRegistry()
    registry.try_admit(StubbornSession("stuck"))

    with caplog.at_level(logging.WARNING, logger="wijjit_ssh"):
        await registry.drain(reason="server_shutdown", message=None, grace=0.05)
    assert "alternate screen buffer" in caplog.text


async def test_drain_survives_a_session_that_raises() -> None:
    """A broken session must not take down the shutdown of every other one."""
    registry = SessionRegistry()

    class Exploding(FakeSession):
        def request_close(self, reason: str, message: str | None = None) -> None:
            raise RuntimeError("boom")

    exploding = Exploding("boom")
    healthy = FakeSession("ok")
    healthy.registry = registry
    registry.try_admit(exploding)
    registry.try_admit(healthy)

    forced = await registry.drain(reason="server_shutdown", message=None, grace=0.05)

    assert exploding.aborted is True  # forced, since it would not ask nicely
    assert healthy.closed_with  # and the healthy one was still asked
    assert forced == 1
    assert registry.active_sessions == 0


async def test_drain_can_run_twice() -> None:
    """stop() is idempotent, and a second drain must not wedge on stale state."""
    registry = SessionRegistry()
    first = FakeSession("a")
    first.registry = registry
    registry.try_admit(first)

    assert await registry.drain(reason="shutdown", message=None, grace=1.0) == 0

    second = FakeSession("b")
    second.registry = registry
    registry.try_admit(second)
    assert await registry.drain(reason="shutdown", message=None, grace=1.0) == 0


# -- IdleTimer -----------------------------------------------------------------


async def test_idle_timer_fires_after_silence() -> None:
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.05, session_timeout=None, on_expire=fired.append)
    timer.start()
    await asyncio.sleep(0.15)
    assert fired == ["idle_timeout"]


async def test_poke_defers_the_idle_deadline() -> None:
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.1, session_timeout=None, on_expire=fired.append)
    timer.start()

    for _ in range(4):  # keep typing for longer than the idle timeout
        await asyncio.sleep(0.04)
        timer.poke()
    assert fired == []

    await asyncio.sleep(0.2)  # then stop
    assert fired == ["idle_timeout"]
    timer.cancel()


async def test_cancel_prevents_the_idle_deadline() -> None:
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.05, session_timeout=None, on_expire=fired.append)
    timer.start()
    timer.cancel()
    await asyncio.sleep(0.15)
    assert fired == []


async def test_the_session_deadline_ignores_activity() -> None:
    """Unlike idle, this one interrupts someone who is actively working."""
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=None, session_timeout=0.1, on_expire=fired.append)
    timer.start()

    for _ in range(5):
        await asyncio.sleep(0.03)
        timer.poke()

    assert fired == ["session_timeout"]


async def test_only_the_first_deadline_to_land_fires() -> None:
    """Both can be armed; a session must not be closed twice."""
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.05, session_timeout=0.06, on_expire=fired.append)
    timer.start()
    await asyncio.sleep(0.2)
    assert fired == ["idle_timeout"]


async def test_a_timer_with_both_deadlines_disabled_never_fires() -> None:
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=None, session_timeout=None, on_expire=fired.append)
    timer.start()
    timer.poke()
    await asyncio.sleep(0.1)
    assert fired == []
    timer.cancel()


async def test_cancel_is_idempotent_and_safe_after_firing() -> None:
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.05, session_timeout=None, on_expire=fired.append)
    timer.start()
    await asyncio.sleep(0.15)
    timer.cancel()
    timer.cancel()
    assert fired == ["idle_timeout"]


async def test_poke_after_firing_does_not_rearm() -> None:
    """data_received can land after the idle timer already closed the session."""
    fired: list[str] = []
    timer = IdleTimer(idle_timeout=0.05, session_timeout=None, on_expire=fired.append)
    timer.start()
    await asyncio.sleep(0.15)
    timer.poke()
    await asyncio.sleep(0.15)
    assert fired == ["idle_timeout"]
