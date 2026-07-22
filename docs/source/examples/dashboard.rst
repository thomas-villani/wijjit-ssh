Server dashboard
================

`examples/dashboard_ssh.py
<https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/dashboard_ssh.py>`_

CPU and memory gauges, a history chart, disk and uptime, the heaviest processes -
and a table of everyone currently connected to the server drawing it, which is
the panel a local ``top`` cannot give you.

.. code-block:: bash

   uv sync --group examples
   uv run --group examples python examples/dashboard_ssh.py    # :8022
   ssh -p 8022 you@localhost

``psutil`` lives in its own ``examples`` dependency group, so a plain ``uv sync``
for the test suite does not build a C extension.

One sampler, not one per session
--------------------------------

Port a local monitor to SSH naively and every connection brings its own timer and
its own ``psutil`` calls: ten viewers, ten independent interrogations of the same
machine, ten sets of slightly different numbers.

A single :class:`Sampler` task samples once per second and every window renders
the same :class:`Reading`. It is started lazily by the first viewer and cancelled
after the last one leaves, so a dashboard nobody is watching costs nothing:

.. code-block:: python

   def join(self, session_id, session, app):
       self.viewers[session_id] = Viewer(...)
       if self._task is None:
           # We are on the event loop here - the factory runs inside
           # session_started - so this is the natural place to start it.
           self._task = asyncio.ensure_future(self._run())

   def leave(self, session_id):
       self.viewers.pop(session_id, None)
       if not self.viewers and self._task is not None:
           self._task.cancel()
           self._task = None

Starting it from ``run()`` instead would mean sampling an empty room, and would
have to happen outside the server's own entry point.

This is also the example where getting :ref:`unsubscribing
<examples-unsubscribing>` wrong costs more than a stale table row: miss a
``session.ended`` and the sampler never sees the room empty, so it samples
forever.

Blocking work goes to a thread
------------------------------

Every session in the process shares one event loop. ``psutil.process_iter()``
walks every process on the box - comfortably hundreds of milliseconds on a busy
desktop, and closer to a second on the machine this was written on - so calling
it inline would freeze **every** connected client's frames for that long, not
just the caller's.

.. code-block:: python

   self.reading = await asyncio.to_thread(self.sample)

This is the README's "give CPU-bound apps an executor" caveat in the one place a
reader is likely to actually meet it. It also means a cycle costs the sleep
*plus* however long the sample took, which is why the history chart is labelled
by point count rather than by a duration it cannot promise.

Two details worth stealing
--------------------------

**Prime the percentage APIs.** Both ``psutil.cpu_percent()`` and
``Process.cpu_percent()`` report usage *since the previous call*, so the first
reading of each is a meaningless ``0.0``. The sampler burns one reading up front
and puts its ``sleep`` at the top of the loop, so the first number a viewer sees
spans a real interval.

**Drop the idle process.** ``System Idle Process`` (or ``kernel_task``) is
"using" every core nothing else is, so it pins itself to the top of a CPU sort
forever while telling you nothing. Per-process CPU is also summed across cores
and runs past 100%, so the table divides by ``psutil.cpu_count()`` to make its
column mean the same thing as the gauge above it.

Authentication is not optional here
-----------------------------------

``hello_ssh.py`` falls back to ``allow_anonymous=True`` when it finds no
``~/.ssh/authorized_keys``, so the demo always runs. This app shows the machine's
process table and the address of every connected user, so it refuses to start
instead:

.. code-block:: python

   if not authorized_keys.is_file():
       raise SystemExit(
           f"No {authorized_keys}.\n"
           "This dashboard exposes the machine's process table and every\n"
           "connected user's address, so it will not run unauthenticated.\n"
           ...
       )

The contrast with ``hello_ssh.py`` is the point: ``allow_anonymous`` is a
decision about what a particular app exposes, not a default to inherit. See
:doc:`../guide/authentication`.

It also passes ``idle_timeout=None``. A dashboard is watched, not typed at, so
the default ten-minute idle timeout would disconnect exactly the people using it
as intended. See :doc:`../guide/limits` for what that turns off.

Trying it properly
------------------

Open it from two terminals of **different sizes**. Both render at their own
dimensions - Wijjit's terminal-size override is task-local, so concurrent
sessions never collide - both show the same numbers from the one sampler, and
each appears in the other's *Connected now* table. Resize one and the other is
untouched.

Then disconnect every client and watch the sampler stop; reconnect and watch it
start again.
