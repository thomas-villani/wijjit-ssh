Host keys
=========

A host key is the server's **identity**. Clients pin it on first connect, write
it into ``known_hosts``, and refuse to talk to you if it ever changes. The
operational rules follow from that: generate one, keep it private, keep it
stable across restarts, and rotate deliberately.

Two ways to get one
-------------------

.. tabs::

   .. tab:: ensure_host_key

      .. code-block:: python

         from wijjit_ssh import ensure_host_key

         host_keys = [ensure_host_key("ssh_host_key")]

      Generates an ed25519 key on first run and reuses it afterwards. The right
      choice for development, and for a container with a genuinely persistent
      mounted volume.

   .. tab:: load_host_keys

      .. code-block:: bash

         ssh-keygen -t ed25519 -f /var/lib/myapp/host_key -N ''

      .. code-block:: python

         from wijjit_ssh import load_host_keys

         host_keys = load_host_keys(["/var/lib/myapp/host_key"])

      Loads keys you manage out of band and raises if one is missing. The right
      choice for production.

The difference is what happens when the file is *not* where you think it is.
:func:`~wijjit_ssh.keys.ensure_host_key` will cheerfully mint a new identity, and
every client will then report a host key mismatch - which looks exactly like an
attack. :func:`~wijjit_ssh.keys.load_host_keys` fails loudly instead.

That is also why generation logs at ``WARNING`` rather than ``INFO``:

.. code-block:: text

   WARNING wijjit_ssh.keys: Generated a new ssh-ed25519 host key at ssh_host_key
   (SHA256:...). This is the server's identity: keep this file, and back it up.

On a healthy server that line appears exactly once, ever. **If you see it on
every restart, your "persistent" volume is not.**

Rotation
--------

``asyncssh`` offers every key it is given, and a client accepts a connection if
*any* offered key matches what it has pinned. So rotating is a matter of serving
both for a transition window:

.. code-block:: python

   # 1. Serve old and new together. Clients pinned to either one connect fine;
   #    those that negotiate the new one quietly update.
   host_keys = load_host_keys(["host_key_new", "host_key_old"])

   # 2. Once every client has seen the new key, drop the old one.
   host_keys = load_host_keys(["host_key_new"])

Keys are resolved when the server is **constructed**, not when it starts
listening, so a bad path raises where the traceback points at your configuration
rather than somewhere inside ``create_server``. Every fingerprint is logged at
startup.

Permissions
-----------

:func:`~wijjit_ssh.keys.ensure_host_key` writes with ``O_CREAT | O_EXCL`` and
mode ``0600``, so the file is private **from the moment it exists** rather than
being created world-readable and narrowed afterwards. The exclusive create also
means two processes starting together cannot both believe they generated the
key - the loser reads the winner's, and both serve the same identity.

On Windows the mode argument is ignored and the file inherits its directory's
ACLs. The call is portable; only its guarantee is weaker.

A key that is group- or world-readable gets a warning, not a refusal:

.. code-block:: text

   WARNING wijjit_ssh.keys: Host key host_key is accessible to group/other
   (mode 644). Anyone who can read it can impersonate this server;
   run: chmod 600 host_key

Advisory rather than fatal because refusing to start would be worse than serving
with a warning, and unlike OpenSSH we are not in a position to know your threat
model. (This check is skipped on Windows, where POSIX mode bits are not
meaningful - ``st_mode`` reports ``0o666`` regardless, so checking it would only
produce noise.)

.. warning::

   Never commit a host key. Anyone who has it can impersonate your server. This
   repository's ``.gitignore`` covers ``*.key``, ``*host_key*``, and
   ``known_hosts``; if you adopt a different naming scheme, extend it - note that
   ``.gitignore`` matches on the **basename**, so a pattern anchored to a prefix
   will miss ``ssh_host_key``.

Fingerprints
------------

:func:`~wijjit_ssh.keys.fingerprint` returns the algorithm and the same
``SHA256:...`` digest a client compares against ``known_hosts``, so you can hand
users something to check on first connect:

.. code-block:: python

   >>> from wijjit_ssh import ensure_host_key, fingerprint
   >>> fingerprint(ensure_host_key("ssh_host_key"))
   'ssh-ed25519 SHA256:8xKNBvF6TPMK/LxQ+zKOmM0GzGIQSfEfp4pF/ZqWlE4'

Publishing that out of band - in a runbook, an onboarding email, your
configuration management - is what turns trust-on-first-use into actual
verification.
