Resource limits
===============

Bounded by default, because a limit that is opt-in is not a limit in any
deployment where nobody thought about it. Without limits, any peer can open
sessions until the process runs out of memory, and a forgotten ``ssh`` window
holds a session slot forever.

Every value below is a :class:`~wijjit_ssh.config.ServerConfig` field and can be
passed to :class:`~wijjit_ssh.server.WijjitSSH` as a keyword:

.. code-block:: python

   WijjitSSH(
       make_app,
       host_keys=host_keys,
       auth=auth,
       max_sessions=100,        # concurrent sessions, server-wide
       max_per_ip=10,           # concurrent connections from one address
       connect_rate=0.0,        # connections/sec/IP; 0 (the default) disables
       connect_burst=20,        # ...and how many at once before that bites
       login_timeout=30.0,      # seconds to authenticate
       idle_timeout=600.0,      # seconds of silence before disconnect
       session_timeout=None,    # hard cap on duration regardless of activity
       keepalive_interval=30.0, # reap peers whose TCP died without a FIN
       shutdown_grace=5.0,      # seconds sessions get to exit cleanly on stop()
   ).run()

What each one is for
--------------------

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Setting
     - Default
     - What it actually bounds
   * - ``max_sessions``
     - 100
     - Live Wijjit apps in the process. Really a memory bound - size it against
       what your app costs, not against a round number.
   * - ``max_per_ip``
     - 10
     - Concurrent connections from one address, so a single peer cannot consume
       the whole session pool.
   * - ``connect_rate`` / ``connect_burst``
     - 0.0 / 20
     - Sustained connections per second per IP, as a token bucket. Off by
       default.
   * - ``login_timeout``
     - 30.0
     - How long an *unauthenticated* peer can hold resources. Tightens
       asyncssh's own 120 seconds.
   * - ``idle_timeout``
     - 600.0
     - Silence before a session is closed. This is what reaps the forgotten
       ``ssh`` window. ``None`` disables.
   * - ``session_timeout``
     - ``None``
     - Hard cap on duration regardless of activity. Off by default because
       unlike ``idle_timeout`` it interrupts someone who is actively working.
   * - ``keepalive_interval`` / ``keepalive_count_max``
     - 30.0 / 3
     - Reclaims peers whose TCP connection died without a FIN - a closed laptop
       lid, a NAT timeout. A dead peer is gone in about 90 seconds.

The one deliberately-off default is ``connect_rate``. Rate limiting a service you
have not measured is how you throttle your own health check, so it is opt-in.

Two chokepoints, not one
------------------------

**``max_per_ip`` counts connections; ``max_sessions`` counts sessions.** That is
not an inconsistency, it is a consequence of where each one can be enforced:

* Per-IP limits and the rate limit are **pre-authentication**, checked when the
  TCP connection arrives. The entire point is to not spend a key exchange on an
  abusive peer, and at that moment no session exists yet.
* ``max_sessions`` is inherently **post-authentication**. A session only exists
  once a channel is opened, which requires a successful userauth.

Sessions per IP are bounded transitively, since every session lives inside a
connection.

Refusals explain themselves
---------------------------

A client turned away hears why, on its terminal, rather than getting a bare
protocol error:

.. code-block:: text

   This server is at capacity (100 sessions). Please try again shortly.

   Too many connections from your address (limit 10). Close one and try again.

   Too many connection attempts. Please wait and try again.

Refusals are also logged and emitted to the ``on_event`` hook as
``connection.rejected`` and ``session.rejected``, each carrying a ``reason``, so
"we are turning people away" is something your metrics can show you rather than
something users have to report. See :doc:`logging`.

Tuning for an exposed deployment
--------------------------------

The defaults are meant to be invisible to a legitimate user and finite to an
abusive one. A server on a public address wants something tighter:

.. code-block:: python

   WijjitSSH(
       make_app,
       host_keys=load_host_keys(["/var/lib/myapp/host_key"]),
       auth=AuthorizedKeys("/etc/myapp/authorized_keys"),
       port=22,
       max_sessions=25,
       max_per_ip=2,
       connect_rate=1.0,        # 1/sec sustained, 20 at once
       login_timeout=15.0,
       idle_timeout=120.0,
   ).run()

Out-of-range values raise :exc:`ValueError` at construction, and an unknown name
raises :exc:`TypeError` rather than being ignored - a typo'd ``max_session=1``
that silently does nothing leaves an operator believing a server is bounded when
it is not.

What is *not* bounded yet
-------------------------

.. warning::

   **There is no backpressure handling.** A client that stops reading - suspends
   its terminal, or has its network stall - causes frames to buffer inside
   ``asyncssh`` without bound. ``max_sessions`` limits how many such clients can
   exist, but not how much memory each one can accumulate. This is the largest
   open item before this package is production-ready on a hostile network.

Two smaller ones:

* A blocking synchronous handler stalls that session's frames, and with them the
  event loop. Give CPU-bound work an executor.
* Wide characters and emoji are treated as single-width. That is a Wijjit
  limitation rather than an SSH one.

How it is enforced
------------------

:mod:`wijjit_ssh.limits` is **pure bookkeeping and policy**: no sockets, no
asyncssh imports, and an injectable clock. Sessions are reached only through the
:class:`~wijjit_ssh.limits.ManagedSession` protocol, which is what lets the real
assertions run as fast unit tests with a fake clock and leaves the over-SSH tests
to prove only that the wiring is connected.

There is no locking anywhere in that module. It is correct because asyncio is
single-threaded and none of its methods await: each runs to completion before
another callback can observe the state.
:meth:`~wijjit_ssh.limits.SessionRegistry.try_admit` is one call rather than a
check followed by a register for exactly this reason - it makes the atomicity
structural rather than a comment a later refactor can invalidate.
