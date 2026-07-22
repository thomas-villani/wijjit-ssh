Authentication
==============

Authentication is **fail-closed**. :class:`~wijjit_ssh.server.WijjitSSH` raises
at construction unless you either pass an ``auth`` policy or explicitly pass
``allow_anonymous=True``:

.. code-block:: python

   >>> WijjitSSH(make_app, host_keys=host_keys)
   ValueError: WijjitSSH requires an auth policy. Pass auth=... ...

Serving an unauthenticated SSH server should be something you typed, not
something you inherited by forgetting an argument.

``asyncssh`` drives authentication through a handful of callbacks on its
:class:`~asyncssh.SSHServer` object. Wiring credentials straight into those
callbacks works, but couples every deployment to the server glue. Instead the
server delegates to an :class:`~wijjit_ssh.auth.AuthPolicy`, so *how* a
deployment authenticates is a value you pass in rather than code you edit.

The presets
-----------

Public keys
^^^^^^^^^^^

:class:`~wijjit_ssh.auth.AuthorizedKeys` is the recommended policy: no shared
secret ever crosses the wire.

.. code-block:: python

   from wijjit_ssh import AuthorizedKeys

   # One file for everyone, OpenSSH format.
   auth = AuthorizedKeys("~/.ssh/authorized_keys")

   # Or one file per user - the username selects the file.
   auth = AuthorizedKeys({"alice": "keys/alice.pub", "bob": "keys/bob.pub"})

A username with no entry is denied. In the per-user form that is the whole
access-control list: adding a user means adding a file.

Passwords
^^^^^^^^^

:class:`~wijjit_ssh.auth.PasswordAuth` delegates the check to your callback, so
the credential store is yours - LDAP, a database, a hash table.

.. code-block:: python

   from wijjit_ssh import PasswordAuth
   from wijjit_ssh.auth import check_password

   async def check(username: str, password: str) -> bool:
       expected = await lookup(username)          # your database; async is fine
       return expected is not None and check_password(password, expected)

   auth = PasswordAuth(check)

The callback may be sync or async. Prefer async for anything that talks to a
network or runs a KDF: a policy is consulted on the event loop, so a blocking
lookup stalls every other session in the process.

.. important::

   Use :func:`~wijjit_ssh.auth.check_password` rather than ``==`` when comparing
   plaintext secrets - it compares in constant time, so an attacker cannot
   recover the secret one byte at a time from response timing. For anything
   stored at rest, use a real password hash (``argon2``, ``bcrypt``) and verify
   with that library instead.

Chaining
^^^^^^^^

:class:`~wijjit_ssh.auth.ChainAuth` accepts if any of its policies accepts:

.. code-block:: python

   from wijjit_ssh import ChainAuth

   auth = ChainAuth(
       AuthorizedKeys("~/.ssh/authorized_keys"),
       PasswordAuth(check),
   )

The client picks a method and the chain answers for it, so this is "keys *or* a
password", not "keys *then* a password". There is no multi-factor mode.

No authentication
^^^^^^^^^^^^^^^^^

:class:`~wijjit_ssh.auth.OpenAuth` lets anyone connect as any username. The
username is whatever the client typed and is not verified in any way.

.. danger::

   Development and demos only. ``WijjitSSH`` refuses to use this policy unless
   ``allow_anonymous=True`` is also passed, and logs a loud warning at startup
   when it does. Never expose it on an untrusted network.

Writing your own
----------------

Subclass :class:`~wijjit_ssh.auth.AuthPolicy` and override only the methods for
the mechanisms you support. The base class **denies everything** - it requires
authentication and supports no method - so a subclass that forgets to enable a
method fails closed rather than open.

The surface is three pairs, one per SSH mechanism:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Mechanism
     - "Do you offer it?"
     - "Is this credential good?"
   * - Password
     - ``password_supported()``
     - ``async verify_password(username, password)``
   * - Public key
     - ``public_key_supported()``
     - ``authorized_keys_for(username)``
   * - Keyboard-interactive
     - ``kbdint_supported()``
     - ``async verify_kbdint(username, responses)``, prompts from
       ``kbdint_prompts(username)``

Public keys work differently from the other two: rather than verifying a
credential, you return the list of keys a user is *allowed* to use, and
``asyncssh`` performs the signature check itself. Return ``None`` for an unknown
user.

.. code-block:: python

   from wijjit_ssh.auth import AuthPolicy, check_password

   class TokenAuth(AuthPolicy):
       """Accept a bearer token typed at a keyboard-interactive prompt."""

       def kbdint_supported(self) -> bool:
           return True

       def kbdint_prompts(self, username: str):
           return [("Token: ", False)]        # echo=False hides typing

       async def verify_kbdint(self, username: str, responses: list[str]) -> bool:
           expected = await self.store.token_for(username)
           return expected is not None and check_password(responses[0], expected)

.. note::

   A policy is consulted per connection attempt and may be shared across
   connections, so implementations should be stateless (or internally
   idempotent) and must not block the event loop. Do slow work - database
   lookups, KDF verification - in the ``async`` methods or an executor.

What the app sees
-----------------

The authenticated username is handed to your factory as ``session.username``, so
apps can personalise and authorise per user:

.. code-block:: python

   def make_app(session: SSHSession) -> Wijjit:
       if session.username not in ADMINS:
           return build_readonly_app(session)
       return build_admin_app(session)

Note that authorisation *inside* the app is your job. wijjit-ssh answers "is this
person who they say they are"; it has no notion of what they are then allowed to
do.

Timeouts and logging
--------------------

``login_timeout`` (default 30 seconds, tightening asyncssh's own 120) bounds how
long an unauthenticated peer can hold resources. Per-IP connection limits are
applied *before* authentication - see :doc:`limits`.

Credentials are never logged. Outcomes are: ``auth.ok`` and ``auth.failed``
records carry the username, peer address, and method, and the same pair is
emitted to the ``on_event`` metrics hook. See :doc:`logging`.
