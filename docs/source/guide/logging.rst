Logging and metrics
===================

Records go to the ``wijjit_ssh`` logger tree, which is **silent until
configured** and never carries credentials.

Turning it on
-------------

:meth:`~wijjit_ssh.server.WijjitSSH.run` configures stderr logging for you -
unless you have already set up a handler on either ``wijjit_ssh`` or the root
logger, in which case it stays out of your way entirely.

Everywhere else, call :func:`~wijjit_ssh.logging.configure_logging` yourself:

.. code-block:: python

   import sys
   from wijjit_ssh import configure_logging

   configure_logging(sys.stderr)                    # a stream
   configure_logging(sys.stderr, level="DEBUG")     # ...more of it
   configure_logging("wijjit-ssh.log")              # a UTF-8 file, appended
   configure_logging(None)                          # silence

stderr is the default because, unlike a Wijjit app - where stderr *is* the
terminal the app is drawing on - an SSH server's stdout and stderr are ordinary
process streams, and stderr is what systemd and Docker collect.

Or ignore this function entirely and configure the ``wijjit_ssh`` logger with
``logging.config.dictConfig`` like any other library. It is a normal logger tree
with ``propagate=True``.

Session-bound records
---------------------

Every session gets a short correlation id, bound with the username and peer
address into every line it emits:

.. code-block:: text

   2026-07-16 11:04:22 INFO  wijjit_ssh.session: [3f9a1c04 ada@10.0.0.7] Session started (term=xterm, 120x40)
   2026-07-16 11:09:47 INFO  wijjit_ssh.session: [3f9a1c04 ada@10.0.0.7] Session ended (idle timeout, 5m25s)

The id is eight hex characters - long enough not to collide in a log file, short
enough to sit in every line and still be greppable. It is a correlation handle,
not a security token.

That same id is on ``session.session_id``, so surfacing it in your app's own logs
or in an error dialog is what ties a user's report back to the server-side
record.

.. note::

   The context is bound to the session object rather than to a
   :class:`~contextvars.ContextVar` for a structural reason: the ``asyncssh``
   callbacks (``session_started``, ``data_received``, ``connection_lost``) run on
   asyncssh's connection task, while the app runs in a task created in
   ``session_started``. A contextvar set inside the app task would be invisible
   from exactly the callbacks that need to log.

The metrics hook
----------------

Pass ``on_event=`` to get lifecycle events without this package depending on a
metrics library:

.. code-block:: python

   from collections import Counter

   counts: Counter[str] = Counter()

   def on_event(event: str, fields: Mapping[str, object]) -> None:
       counts[event] += 1

   WijjitSSH(make_app, host_keys=host_keys, auth=auth, on_event=on_event).run()

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Event
     - Fields
   * - ``connection.opened``
     - ``peer_ip``
   * - ``connection.rejected``
     - ``peer_ip``, ``reason``
   * - ``connection.closed``
     - ``peer_ip``
   * - ``auth.ok``
     - ``username``, ``peer_ip``, ``method``
   * - ``auth.failed``
     - ``username``, ``peer_ip``, ``method``
   * - ``session.started``
     - ``session_id``, ``username``, ``peer_ip``
   * - ``session.rejected``
     - ``peer_ip``, ``reason``
   * - ``session.ended``
     - ``session_id``, ``reason``, ``duration``

Wiring that to Prometheus is a dozen lines and no dependency on our side:

.. code-block:: python

   from prometheus_client import Counter, Histogram

   EVENTS = Counter("wijjit_ssh_events", "Lifecycle events", ["event", "reason"])
   DURATION = Histogram("wijjit_ssh_session_seconds", "Session duration")

   def on_event(event: str, fields: Mapping[str, object]) -> None:
       EVENTS.labels(event, str(fields.get("reason", ""))).inc()
       if event == "session.ended":
           DURATION.observe(float(fields["duration"]))

.. important::

   A hook that raises is logged and swallowed. A deployment's counter must not be
   able to take a session down - these callbacks run inside asyncssh callbacks,
   where an exception would propagate into the transport rather than anywhere
   useful.

   The hook is called **on the event loop**, so it must not block. Increment a
   counter; do not write to a database.

Why this package owns its own logger tree
-----------------------------------------

Wijjit's ``get_logger`` roots a name under its own namespace by prefixing
``"wijjit."`` - but only ``if not name.startswith("wijjit")``. Our modules are
named ``wijjit_ssh.*``, which *does* start with ``"wijjit"``, so the prefix is
never applied. The result is a logger called ``wijjit_ssh.server`` that is a
**sibling** of the ``wijjit`` tree rather than a child of it.

That is not cosmetic. ``wijjit.configure_logging()`` only handles the ``wijjit``
logger, so our loggers inherit none of it - including ``configure_logging(None)``,
the "turn logging off" switch. A record from ``wijjit_ssh`` would therefore
propagate to the root logger, find no handler anywhere, and be printed to stderr
by :data:`logging.lastResort`. In a process where a Wijjit TUI has imported
``wijjit_ssh``, that means a warning sprayed across the alternate screen buffer,
corrupting the frame.

So ``wijjit_ssh`` owns its tree, with the conventional library posture:

* A :class:`~logging.NullHandler` is attached **at import time**.
  :meth:`logging.Logger.callHandlers` only falls back to ``lastResort`` when it
  finds *zero* handlers on the chain, so this one handler is what stops the
  stderr spray.
* ``propagate`` is left **True**, so a host application that configures the root
  logger receives our records - which is what a library embedded in someone
  else's process should do. It also keeps pytest's ``caplog`` working.
* :func:`~wijjit_ssh.logging.configure_logging` is opt-in.

.. note::

   This module is ``wijjit_ssh/logging.py``, but ``import logging`` inside it
   resolves to the standard library rather than to itself - Python 3 uses
   absolute imports. Importing it as ``wijjit_ssh.logging`` is unambiguous.
