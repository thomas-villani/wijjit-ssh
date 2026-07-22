Quickstart
==========

A Wijjit SSH server is three things: a **factory** that builds an app, a **host
key** that gives the server an identity, and an **auth policy** that decides who
gets in. Everything else has a default.

The factory
-----------

The factory is the SSH analogue of a Flask view: it runs once per connection and
returns the :class:`~wijjit.Wijjit` app that client will drive.

.. code-block:: python

   from wijjit import Wijjit, render_template_string
   from wijjit_ssh import SSHSession, WijjitSSH

   def make_app(session: SSHSession) -> Wijjit:
       app = Wijjit(backend=session.backend)

       @app.view("main", default=True)
       def main():
           return render_template_string(
               "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}",
               who=session.username,
           )

       return app

The one line that matters is ``Wijjit(backend=session.backend)``. That is what
routes the app's output to the SSH channel and its input to the client's
keystrokes, rather than to the server process's own console. Forget it and the
app will try to draw on the server's stdout.

Everything the factory is told about the client arrives on
:class:`~wijjit_ssh.server.SSHSession`:

.. code-block:: python

   def make_app(session: SSHSession) -> Wijjit:
       session.username     # who authenticated
       session.term_type    # their TERM, e.g. "xterm-256color"
       session.columns      # negotiated width...
       session.lines        # ...and height
       session.peer_ip      # where they connected from
       session.session_id   # correlation id, matching this session's log lines
       session.backend      # -> Wijjit(backend=...)
       session.conn         # the asyncssh connection, for advanced use

Each connection gets its own app object and its own state. Two users typing in
the same field are not sharing anything - which also means anything you *do*
want shared (a database handle, a cache) should be a module-level object the
factory closes over, not something it rebuilds per connection.

.. warning::

   The factory runs after authentication but before the app starts drawing, and
   it runs on the event loop. Keep it fast and non-blocking: an ``await``-less
   database round trip here stalls every other session in the process. If the
   app needs slow setup, do it in an ``async`` startup handler inside the app.

A host key
----------

The server's identity. Clients pin it on first connect and refuse to talk to you
if it changes.

.. tabs::

   .. tab:: Development

      .. code-block:: python

         from wijjit_ssh import ensure_host_key

         host_keys = [ensure_host_key("ssh_host_key")]

      Generated on first run, reused forever after. No ``ssh-keygen`` detour,
      and your ``known_hosts`` entry stays valid across restarts.

   .. tab:: Production

      .. code-block:: bash

         ssh-keygen -t ed25519 -f /var/lib/myapp/host_key -N ''

      .. code-block:: python

         from wijjit_ssh import load_host_keys

         host_keys = load_host_keys(["/var/lib/myapp/host_key"])

      Managed out of band, and a hard failure if it is missing - rather than
      silently inventing a new identity that breaks every client.

See :doc:`../guide/host_keys` for rotation and the permissions rules.

An auth policy
--------------

Authentication is **fail-closed**: :class:`~wijjit_ssh.server.WijjitSSH` raises
unless you either pass an ``auth`` policy or explicitly pass
``allow_anonymous=True``.

.. code-block:: python

   from wijjit_ssh import AuthorizedKeys

   auth = AuthorizedKeys("~/.ssh/authorized_keys")

Public keys are the recommended setup. Passwords, keyboard-interactive, and
chained policies are all available - see :doc:`../guide/authentication`.

Putting it together
-------------------

.. code-block:: python

   WijjitSSH(
       make_app,
       host_keys=[ensure_host_key("ssh_host_key")],
       auth=AuthorizedKeys("~/.ssh/authorized_keys"),
   ).run(port=8022)

.. code-block:: bash

   ssh -p 8022 you@localhost

A complete, runnable version of this is `examples/hello_ssh.py
<https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/hello_ssh.py>`_.

Three ways to run it
--------------------

Which entry point you use depends on who owns the process.

.. list-table::
   :header-rows: 1
   :widths: 20 34 46

   * - Method
     - Use when
     - What it does for you
   * - :meth:`~wijjit_ssh.server.WijjitSSH.run`
     - The server *is* the program
     - Blocks. Installs ``SIGINT``/``SIGTERM`` handlers and configures stderr
       logging.
   * - :meth:`~wijjit_ssh.server.WijjitSSH.run_async`
     - One coroutine in a larger asyncio app
     - Serves until :meth:`~wijjit_ssh.server.WijjitSSH.stop` or cancellation.
       Touches neither signals nor logging.
   * - :meth:`~wijjit_ssh.server.WijjitSSH.start`
     - You want the listener and control of the rest
     - Returns as soon as it is bound, handing back the
       :class:`~asyncssh.SSHAcceptor` (bind port 0 and read the assigned port
       off it - this is what the tests do).

.. code-block:: python

   # Embedded: the host application owns signals and logging.
   server = WijjitSSH(make_app, host_keys=host_keys, auth=auth)
   await server.start()
   ...
   await server.stop()

The rule is that :meth:`~wijjit_ssh.server.WijjitSSH.run` owns the process, so it
is the only entry point that touches process-global state. See
:doc:`../guide/shutdown`.

Tuning it
---------

Every knob is a :class:`~wijjit_ssh.config.ServerConfig` field, and every field
can be passed to :class:`~wijjit_ssh.server.WijjitSSH` as a keyword:

.. code-block:: python

   WijjitSSH(
       make_app,
       host_keys=host_keys,
       auth=auth,
       max_sessions=25,
       idle_timeout=120.0,
       banner="Authorized users only.\n",
   ).run()

Or build the config up front - from a file, the environment, or argparse - and
pass it as an object:

.. code-block:: python

   config = ServerConfig(port=2222, max_sessions=25, idle_timeout=120.0)
   WijjitSSH(make_app, config, host_keys=host_keys, auth=auth).run()

Unknown names raise :exc:`TypeError` rather than being ignored. That is
deliberate: a typo'd ``max_session=1`` that silently does nothing leaves an
operator believing a server is bounded when it is not.

Next steps
----------

* :doc:`../guide/authentication` - the policies, and writing your own
* :doc:`../guide/limits` - what is bounded, and what is not yet
* :doc:`../guide/shutdown` - why draining matters more than it sounds
* :doc:`../guide/logging` - session-bound logs and the metrics hook
