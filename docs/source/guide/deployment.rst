Deployment
==========

Everything here exists as a real file in
`deploy/ <https://github.com/thomas-villani/wijjit-ssh/tree/main/deploy>`_ — a
systemd unit, a Dockerfile, a compose file, and a healthcheck. This page explains
the choices in them.

Read it as three questions in order: how does the process get supervised, how
does it prove it is alive, and what does the security checklist say before you
open the port.

Running it
----------

A production entry point is one line, and the interesting part is which one:

.. code-block:: python

   from wijjit_ssh import WijjitSSH, AuthorizedKeys, load_host_keys

   WijjitSSH(
       make_app,
       host_keys=load_host_keys(["/var/lib/wijjit-ssh/host_key"]),
       auth=AuthorizedKeys("/etc/wijjit-ssh/authorized_keys"),
       max_sessions=100,
       idle_timeout=600.0,
       banner="Authorized users only.\n",
   ).run(host="0.0.0.0", port=8022)

:meth:`~wijjit_ssh.server.WijjitSSH.run` owns the process. It is the only entry
point that installs signal handlers and configures logging, which is exactly what
you want under a supervisor and exactly what you do not want when embedding — see
:doc:`shutdown`.

Note :func:`~wijjit_ssh.keys.load_host_keys` rather than
:func:`~wijjit_ssh.keys.ensure_host_key`. In production the key is managed out of
band, and a missing one should be a loud startup failure rather than a silently
generated new identity. Both are covered in :doc:`host_keys`.

systemd
-------

`wijjit-ssh.service <https://github.com/thomas-villani/wijjit-ssh/blob/main/deploy/wijjit-ssh.service>`_
is ready to install:

.. code-block:: bash

   sudo useradd --system --home-dir /opt/wijjit-app --shell /usr/sbin/nologin wijjit
   sudo install -m 0644 deploy/wijjit-ssh.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now wijjit-ssh
   systemd-analyze security wijjit-ssh.service     # check the sandbox

Three parts of it matter more than the rest.

**StateDirectory.** ``StateDirectory=wijjit-ssh`` gives the service
``/var/lib/wijjit-ssh``, owned by its user, mode ``0700``, persistent across
restarts and package upgrades. That is where the host key goes. Generate it
before the first start:

.. code-block:: bash

   sudo -u wijjit ssh-keygen -t ed25519 -f /var/lib/wijjit-ssh/host_key -N ''

**TimeoutStopSec must exceed shutdown_grace.**

.. warning::

   If systemd's stop timeout is shorter than ``shutdown_grace`` (default 5.0),
   systemd wins: it sends ``SIGKILL`` in the middle of the drain, no session runs
   its teardown, and every connected user is left inside the alternate screen
   buffer with a terminal that needs ``reset``. The unit sets
   ``TimeoutStopSec=30`` against the default grace of 5 seconds. Raise both
   together, never one alone.

**The sandbox.** This process terminates untrusted connections, so the unit drops
every capability, mounts the filesystem read-only except its state directory,
restricts syscalls to ``@system-service``, and permits only ``AF_INET`` and
``AF_INET6``. It needs a socket and a directory; nothing else is a regression to
take away.

.. note::

   Binding port 22 needs ``CAP_NET_BIND_SERVICE`` — the unit has the two lines
   commented out. Prefer not to. Port 22 is also where the system ``sshd`` you
   use for administration lives, and a high port with a firewall redirect avoids
   the collision entirely.

Docker
------

.. code-block:: bash

   docker compose -f deploy/compose.yaml up --build
   ssh -p 8022 you@localhost

The image runs as a non-root user, is read-only apart from its state volume, and
drops all capabilities.

.. danger::

   **Mount a volume at** ``/var/lib/wijjit-ssh``. Without one the host key is
   regenerated on every ``docker run``, and every returning user is greeted with
   ``REMOTE HOST IDENTIFICATION HAS CHANGED`` — the warning you least want people
   trained to click through. ``compose.yaml`` makes the named volume structural
   so it cannot be forgotten.

   The tell is in your logs: ``ensure_host_key`` logs at WARNING every time it
   generates a key. On a correctly mounted volume you see that line exactly once,
   ever.

``stop_grace_period: 30s`` is the compose equivalent of ``TimeoutStopSec``, and
carries the same warning. Docker's default is 10 seconds; on the command line
that is ``docker stop --timeout 30``.

The ``CMD`` is in exec form so the server is PID 1 and receives ``SIGTERM``
directly. A shell-form ``CMD`` would put ``/bin/sh`` at PID 1, which does not
forward signals, and every deploy would kill sessions instead of draining them.

Health checks
-------------

.. code-block:: bash

   python deploy/healthcheck.py --port 8022 --verbose
   # 127.0.0.1:8022 up (authentication refused, as expected)

`healthcheck.py <https://github.com/thomas-villani/wijjit-ssh/blob/main/deploy/healthcheck.py>`_
exits ``0`` if the server is up and ``1`` otherwise.

**Do not use a TCP connect for this.** A process that accepted the socket and
then wedged still answers a TCP handshake, because the kernel completes it
without the application ever being scheduled. The probe reports healthy while
nobody can log in — the classic false-healthy.

Instead the probe completes the SSH version exchange and key exchange, which
requires a running event loop, a loadable host key, and a working transport, and
then offers no credentials at all. Being refused is the success condition:

.. list-table::
   :header-rows: 1
   :widths: 40 12 48

   * - Outcome
     - Exit
     - Meaning
   * - Authentication refused
     - ``0``
     - Healthy. Everything up to and including the auth policy works.
   * - Refused by a limit
     - ``0``
     - Alive and at capacity — a real condition, not a failure.
   * - Anonymous login accepted
     - ``0``
     - Healthy, but prints a warning: this is ``allow_anonymous=True`` in production.
   * - Connection refused / timeout
     - ``1``
     - Nothing listening, or the loop is not running.

It never authenticates, so it never starts a session and never counts against
``max_sessions``. It *is* an ordinary connection, so it counts against
``max_per_ip`` and ``connect_rate``: probing loopback every 30 seconds sits
comfortably inside the defaults, but a one-second interval against
``connect_rate=0.5`` would eventually rate-limit the probe and report a healthy
server as dead.

Scaling out
-----------

Sessions are independent — one app instance per connection, no shared state
unless your application introduces it — so horizontal scaling is a TCP load
balancer in front of N identical instances. Two consequences:

- **Every instance must serve the same host key**, or a client reconnecting
  through the balancer lands on a different identity and refuses to talk. Mount
  the same key everywhere; this is the one piece of shared state the deployment
  requires.
- **SSH is a long-lived connection**, not a request. Balance on connections, not
  requests, and give the pool a generous idle timeout — anything shorter than
  ``idle_timeout`` (default 600s) cuts sessions the server considers healthy.

Anything the app shares between sessions — a chat room, a live dashboard feed —
is per-process. Two instances mean two rooms. Splitting that across instances
needs a real backend, which is the app's problem rather than this package's.

The vertical bound is memory: a session is a live Wijjit app, so ``max_sessions``
is a memory limit wearing a different name. Its default of 100 is a plausible but
unmeasured guess, and the honest answer depends on your app — measure one session
and divide. See :doc:`limits`.

Security checklist
------------------

Before the port is open:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Check
     - Why
   * - A real ``auth`` policy
     - Never ``allow_anonymous=True`` or ``OpenAuth`` in production. Construction
       is fail-closed so this cannot happen by omission — only by typing it.
   * - Host key managed out of band
     - :func:`~wijjit_ssh.keys.load_host_keys`, from persistent storage, mode
       ``0600``. A key that regenerates trains users to ignore the warning that
       protects them.
   * - Limits left on
     - ``max_sessions``, ``max_per_ip``, ``login_timeout``, ``idle_timeout`` are
       on by default. The failure mode here is turning them off, not forgetting
       to turn them on.
   * - Unprivileged user, high port
     - Never root. Reach port 22 with a redirect or a proxy, not with privileges.
   * - Stop timeout > ``shutdown_grace``
     - Otherwise every deploy wedges the terminal of everyone connected.
   * - Password hashing, if you use passwords
     - :func:`~wijjit_ssh.auth.check_password` is constant-time for comparison;
       anything stored at rest wants a real password hash. Prefer public keys.
   * - Backpressure understood
     - A client that stops reading buffers frames without bound. Known, scheduled
       for M5, and the reason to set ``MemoryMax`` in the unit file.
   * - Your app's own authorisation
     - This package authenticates the connection and hands you
       ``session.username``. What that user may then see is your factory's
       decision.

What you do **not** have to check is the exec surface. There is no shell, no
``exec``, no SFTP, and no port forwarding: those asyncssh handlers are never
implemented, so a session has no code path to anything but your Wijjit app. See
the `security policy <https://github.com/thomas-villani/wijjit-ssh/blob/main/SECURITY.md>`_.

Logging in production
---------------------

``run()`` configures stderr logging unless you already set up a handler, which is
the right default under systemd (the journal captures stderr) and under Docker
(``docker logs``). Each line carries the session id, username, and peer address:

.. code-block:: text

   2026-07-16 11:04:22 INFO wijjit_ssh.session: [3f9a1c04 ada@10.0.0.7] Session started (term=xterm, 120x40)

For metrics, pass ``on_event=``. It fires for ``connection.*``, ``auth.*``, and
``session.*``, so a Prometheus exporter is a few counters in your own process and
no dependency here. See :doc:`logging`.

Two lines are worth alerting on directly: ``ensure_host_key`` at WARNING after
the first boot means your persistent storage is not, and a sustained rate of
``connection.rejected`` means a limit is biting — either an attack, or a limit
set too low for real traffic.
