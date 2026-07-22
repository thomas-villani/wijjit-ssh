Examples
========

Three runnable programs in `examples/
<https://github.com/thomas-villani/wijjit-ssh/tree/main/examples>`_, in the order
they are worth reading.

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Example
     - What it is for
   * - ``hello_ssh.py``
     - The smallest thing that works: one factory, one view, a text field and a
       counter. Read it to see the shape; :doc:`../getting_started/quickstart`
       walks through the same code.
   * - :doc:`dashboard <dashboard>` (``dashboard_ssh.py``)
     - A live server dashboard, including a table of everyone connected to the
       server drawing it. One shared sampler feeding N windows.
   * - :doc:`chat <chat>` (``chat_ssh.py``)
     - A multi-user chat room with no user accounts, because SSH already
       authenticated everyone. N writers feeding N windows.

The first is about the transport. The other two are about the thing the
transport makes possible.

.. toctree::
   :hidden:

   dashboard
   chat

Many apps, one process
----------------------

Both of the larger examples exist to demonstrate one idea that a local Wijjit
app never has to confront: **N live apps in a single process, sharing state.**

A local app is alone. Its state is its own, and the only thing that changes it is
the person at the keyboard. Over SSH, the interesting state usually belongs to
the *server* - the machine's load, the room's transcript, the queue's depth - and
every connected app is a view onto it, changed by things none of those users did.

So the shape is the same in both examples, and it is small:

.. code-block:: python

   hub = Hub()                       # module scope: the shared state

   def make_app(session):
       app = Wijjit(backend=session.backend)

       @app.view("main", default=True)
       def main():
           return render(...)        # reads hub.<something> directly

       hub.join(session.session_id, app)
       return app

Nothing is copied into the app. Each view reads the shared object at render time,
so the moment the hub's state changes, every window is already out of date by
exactly one redraw.

Getting the redraw
------------------

That last redraw is the part with a wrinkle in it.

Each session is its own asyncio task, and while its user is not typing it is
parked inside ``read_input_async(timeout=...)``. Whoever changed the shared state
is on a *different* task - another session, or a background sampler - and cannot
simply call ``render``.

:meth:`~wijjit.Wijjit.refresh` is what bridges that. It sets a flag, and the
target's own loop acts on it when its input read next times out:

.. code-block:: python

   def broadcast(self):
       for subscriber in self.subscribers.values():
           subscriber.app.refresh()      # safe from any task on this loop

**The timeout is the push latency**, and it is worth knowing which number you are
getting:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - ``REFRESH_INTERVAL``
     - Worst-case delay before a pushed change is on screen
   * - unset (the default)
     - 0.5s - the loop's fallback poll
   * - set to ``x``
     - ``x / 2``

Wijjit takes ``REFRESH_INTERVAL`` as a config override on the constructor:

.. code-block:: python

   app = Wijjit(backend=session.backend, REFRESH_INTERVAL=0.2)   # ~100 ms

The tradeoff is that a session with an interval set also *redraws* on that
cadence whether or not anything changed. ``chat_ssh.py`` sets ``0.2`` because a
chat room that lags feels broken; ``dashboard_ssh.py`` sets ``0.5`` because its
sampler only produces a new number once a second and anything tighter would burn
frames for nothing. At a few hundred concurrent sessions this is a dial worth
measuring rather than guessing.

.. _examples-unsubscribing:

Unsubscribing, and why it is not obvious
----------------------------------------

A hub holding apps must let go of them, or it leaks a whole Wijjit app per
disconnect and keeps pushing to windows nobody is looking at.

There is no teardown callback on the app factory, and the tempting in-app signals
are all wrong:

- **Checking ``app.running``** races. The server calls your factory and *then*
  starts the app's task, so a subscriber exists for a moment while ``running`` is
  still ``False``. A broadcast landing in that window would evict a session that
  had not started yet.
- **A Ctrl+Q handler** only covers the polite exit. It misses idle timeouts,
  dropped TCP connections, and :meth:`~wijjit_ssh.WijjitSSH.stop`.

The signal that has neither problem is
:attr:`ServerConfig.on_event <wijjit_ssh.config.ServerConfig.on_event>`.
It is documented as a metrics hook, but ``session.ended`` fires on *every* way out
and carries the same ``session_id`` your factory registered under:

.. code-block:: python

   def on_server_event(event, fields):
       if event == "session.ended":
           session_id = fields.get("session_id")
           if isinstance(session_id, str):
               hub.leave(session_id)

   WijjitSSH(make_app, host_keys=..., auth=..., on_event=on_server_event)

Both examples use exactly this, and both are worth testing by killing a client
rudely - closing the terminal rather than pressing Ctrl+Q - because that is the
path a naive implementation gets wrong.

See :doc:`../guide/logging` for the full event table.
