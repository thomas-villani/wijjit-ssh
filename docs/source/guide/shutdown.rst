Graceful shutdown
=================

The point of a graceful shutdown is not tidiness.

A Wijjit app runs inside the **alternate screen buffer** - it takes over the
client's terminal, draws, and on exit puts everything back. A session that ends
cleanly runs the app's teardown, leaves that buffer, and restores the user's
terminal. One that is cancelled does not, and leaves a real person staring at a
frozen frame in a wedged shell.

So the grace period is not politeness. It is the difference between a user
noticing the server restarted and a user having to type ``reset``.

What ``stop()`` does
--------------------

:meth:`~wijjit_ssh.server.WijjitSSH.stop` runs four steps in order:

1. **Stop accepting.** The listener closes, so no new connection can arrive
   mid-drain.
2. **Ask live sessions to end.** Each app is asked to quit, which runs its own
   teardown.
3. **Wait up to** ``shutdown_grace`` **seconds** (default 5.0) for them to
   finish.
4. **Close what is left**, then the connections and the listener.

.. code-block:: python

   server = WijjitSSH(make_app, host_keys=host_keys, auth=auth)
   await server.start()
   ...
   await server.stop()          # drains; returns when everything is down

It is **idempotent and safe to call concurrently** - a second call, or three at
once, joins the shutdown already in progress rather than starting a new one.
Calling it on a server that never started is also fine.

An empty drain does not sit through the grace period: with no live sessions,
``stop()`` returns immediately regardless of how large ``shutdown_grace`` is.

Clients that are drained get told why, on an ordinary screen after the app has
left the alternate buffer:

.. code-block:: text

   This server is shutting down. Please reconnect shortly.

Choosing ``shutdown_grace``
---------------------------

It trades shutdown latency against leaving a client's terminal wedged. Five
seconds is generous for a Wijjit teardown, which is a handful of escape
sequences. Raise it if your app has slow ``on_quit`` work (flushing a buffer,
committing a transaction); lower it only if something upstream is going to kill
the process anyway - and note that if systemd's ``TimeoutStopSec`` is shorter
than your grace period, systemd wins and nobody drains.

Signals
-------

:meth:`~wijjit_ssh.server.WijjitSSH.run` installs ``SIGINT`` and ``SIGTERM``
handlers, so Ctrl+C and ``systemctl stop`` both drain:

.. code-block:: text

   INFO wijjit_ssh.server: SIGTERM received; shutting down gracefully

A second signal does not restart the drain - it logs and is ignored, so an
impatient operator hitting Ctrl+C twice cannot corrupt a shutdown in progress:

.. code-block:: text

   WARNING wijjit_ssh.server: SIGTERM received again; already shutting down

.. note::

   **Windows delivers no** ``SIGTERM``. ``TerminateProcess`` runs no handlers at
   all, so only Ctrl+C drains there. Signal handling is installed through
   ``loop.add_signal_handler`` on POSIX and falls back to ``signal.signal`` on
   the Windows Proactor loop, which has no such method.

Who owns the process
--------------------

This is the rule that decides which entry point you want:

.. list-table::
   :header-rows: 1
   :widths: 26 20 26 28

   * - Entry point
     - Blocks?
     - Installs signal handlers
     - Configures logging
   * - :meth:`~wijjit_ssh.server.WijjitSSH.run`
     - yes
     - **yes** (SIGINT, SIGTERM)
     - **yes** (stderr, unless already configured)
   * - :meth:`~wijjit_ssh.server.WijjitSSH.run_async`
     - until stopped
     - no
     - no
   * - :meth:`~wijjit_ssh.server.WijjitSSH.start`
     - no
     - no
     - no

``run()`` owns the process, so it is the only entry point that touches
process-global state. ``start()`` and ``run_async()`` may be one coroutine inside
a much larger application, and a library that quietly stole that host's
``SIGINT`` handler or reconfigured its logging would be a bad guest.

Embedding
---------

When embedded, cancellation is the idiomatic way to shut the server down, and
:meth:`~wijjit_ssh.server.WijjitSSH.run_async` supports it directly:

.. code-block:: python

   async def main() -> None:
       server = WijjitSSH(make_app, host_keys=host_keys, auth=auth)
       task = asyncio.create_task(server.run_async(port=8022))
       try:
           await my_application()
       finally:
           task.cancel()
           with contextlib.suppress(asyncio.CancelledError):
               await task

Or drive it explicitly, which is what you want if your host application has its
own shutdown sequence to order this against:

.. code-block:: python

   acceptor = await server.start(host="127.0.0.1", port=0)
   port = acceptor.get_port()        # port 0: ask the OS, then find out
   ...
   await server.stop()

Either way, install your own signal handling and call
:func:`~wijjit_ssh.logging.configure_logging` yourself if you want this package's
records to go anywhere. See :doc:`logging`.
