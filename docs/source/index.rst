wijjit-ssh
==========

**Flask for SSH apps.** Serve `Wijjit <https://github.com/thomas-villani/wijjit>`_
TUI applications over SSH: Wijjit draws the UI, ``asyncssh`` handles the transport
and PTY, and every connection gets its own live app instance.

.. code-block:: python

   from wijjit import Wijjit, render_template_string
   from wijjit_ssh import WijjitSSH, SSHSession, AuthorizedKeys, ensure_host_key

   def make_app(session: SSHSession) -> Wijjit:
       app = Wijjit(backend=session.backend)          # <- routes I/O to the channel

       @app.view("main", default=True)
       def main():
           return render_template_string(
               "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}",
               who=session.username,
           )

       return app

   WijjitSSH(
       make_app,
       host_keys=[ensure_host_key("ssh_host_key")],   # generated on first run
       auth=AuthorizedKeys("~/.ssh/authorized_keys"),
   ).run(port=8022)

.. code-block:: bash

   ssh -p 8022 you@localhost

That is the whole idea: a function that builds an app, and a client that lands
straight in it. No shell, no ``exec``, no SFTP, no port forwarding - a session
only ever runs your Wijjit app.

The backend seam
----------------

Wijjit's event loop talks to "the terminal" through a
:class:`~wijjit.terminal.backend.TerminalBackend` - a small seam covering four
things: frame output, key/mouse input, terminal size, and whether the app owns
the process terminal. Locally that is ``LocalTerminalBackend``.
:class:`~wijjit_ssh.backend.RemoteTerminalBackend` implements the same seam
against an SSH channel:

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Concern
     - Local backend
     - Remote (SSH) backend
   * - Frame output
     - ``sys.stdout``
     - ``chan.write(...)``
   * - Input
     - real stdin, via prompt_toolkit on a thread
     - raw channel bytes decoded on the event loop - no thread, no
       prompt_toolkit
   * - Size
     - ``shutil.get_terminal_size()``
     - negotiated PTY size, refreshed on resize, published per task
   * - Terminal ownership
     - ``owns_terminal=True`` (signals, atexit, suspend, raw mode)
     - ``owns_terminal=False`` (none of that)

Because Wijjit's render context and terminal-size override are
**contextvar-based**, N concurrent sessions of different sizes coexist in one
process without stepping on each other - each runs as its own asyncio task.

Status
------

Early, but no longer a prototype. The input path is production-shaped,
authentication is pluggable and fail-closed, resources are bounded by default,
and shutdown drains rather than kills. What is *not* done yet is backpressure:
a client that stops reading buffers frames in ``asyncssh`` without bound. See
:doc:`guide/limits` for what is enforced today.

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   getting_started/installation
   getting_started/quickstart

.. toctree::
   :maxdepth: 2
   :caption: Guide

   guide/authentication
   guide/host_keys
   guide/limits
   guide/shutdown
   guide/logging
   guide/terminal_input
   guide/deployment

.. toctree::
   :maxdepth: 2
   :caption: Examples

   examples/index

.. toctree::
   :maxdepth: 1
   :caption: Project

   changelog
   contributing

.. toctree::
   :maxdepth: 2
   :caption: API reference

   api/server
   api/config
   api/auth
   api/keys
   api/limits
   api/logging
   api/backend
   api/input

Links
-----

* **GitHub**: https://github.com/thomas-villani/wijjit-ssh
* **Wijjit**: https://github.com/thomas-villani/wijjit
* **Changelog**: `CHANGELOG.md <https://github.com/thomas-villani/wijjit-ssh/blob/main/CHANGELOG.md>`_
* **Issues**: https://github.com/thomas-villani/wijjit-ssh/issues

Indices
-------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
