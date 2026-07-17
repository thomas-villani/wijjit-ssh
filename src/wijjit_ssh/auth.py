"""Pluggable authentication for a Wijjit SSH server.

``asyncssh`` drives authentication through a handful of callbacks on its
:class:`~asyncssh.SSHServer` object. Wiring credentials straight into those
callbacks works, but it couples every deployment to the server glue. Instead the
server delegates to an :class:`AuthPolicy`, so how a deployment authenticates is
a value you pass in rather than code you edit.

Four presets ship:

:class:`AuthorizedKeys`
    Public-key auth against an OpenSSH ``authorized_keys`` file (one file for
    everyone, or one per username). The recommended default.
:class:`PasswordAuth`
    Password auth delegated to your callback (LDAP, a database, a hash check).
:class:`ChainAuth`
    Accept if any of several policies accepts.
:class:`OpenAuth`
    No authentication at all. Development only; the server logs a loud warning
    on startup and refuses to use it unless you also pass ``allow_anonymous``.

**Fail-closed.** :class:`~wijjit_ssh.server.WijjitSSH` raises if constructed with
no ``auth`` policy, unless you explicitly pass ``allow_anonymous=True``. Open
auth has to be a decision someone typed, not a default they inherited.

Notes
-----
A policy is consulted per connection attempt and may be shared across
connections, so implementations should be stateless (or internally
thread-safe/idempotent) and must not block the event loop - do slow work
(database lookups, KDF verification) in ``async`` methods or an executor.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Union

import asyncssh

from wijjit_ssh.logging import get_logger

if TYPE_CHECKING:
    from asyncssh import SSHKey

logger = get_logger(__name__)

# A password checker: (username, password) -> bool, sync or async.
PasswordChecker = Callable[[str, str], Union[bool, Awaitable[bool]]]

# The prompts of a keyboard-interactive challenge: (prompt_text, echo_input).
KbdintPrompts = Sequence[tuple[str, bool]]


def check_password(supplied: str, expected: str) -> bool:
    """Compare two passwords in constant time.

    Comparing with ``==`` leaks the length of the matching prefix through timing,
    which is enough to recover a secret given enough attempts. Use this (or a
    real password hash such as argon2/bcrypt) inside a
    :class:`PasswordAuth` callback.

    Parameters
    ----------
    supplied : str
        The password the client sent.
    expected : str
        The password on record.

    Returns
    -------
    bool
        Whether they match.
    """
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def load_authorized_keys(path: str | Path) -> list[SSHKey]:
    """Load public keys from an OpenSSH ``authorized_keys`` file.

    Parameters
    ----------
    path : str or Path
        Path to the file. ``~`` is expanded.

    Returns
    -------
    list of SSHKey
        Every key the file declares. Blank lines and ``#`` comments are skipped;
        lines carrying key options (``no-pty,from="..." ssh-ed25519 AAAA...``)
        have the options stripped. Unparseable lines are logged and skipped
        rather than failing the whole file - one bad line should not lock
        everyone out.

    Raises
    ------
    FileNotFoundError
        If the file does not exist. This is fatal on purpose: silently treating a
        missing key file as "no authorized keys" would deny everyone, and a typo
        in a config path should be loud.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"authorized_keys file not found: {resolved}")

    keys: list[SSHKey] = []
    for lineno, raw in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        key = _import_key_line(line)
        if key is None:
            logger.warning("Skipping unparseable key at %s:%d", resolved, lineno)
            continue
        keys.append(key)

    if not keys:
        logger.warning("No usable public keys in %s", resolved)
    return keys


def _import_key_line(line: str) -> SSHKey | None:
    """Import one ``authorized_keys`` line, tolerating leading key options."""
    try:
        return asyncssh.import_public_key(line)
    except Exception:
        pass

    # Retry from the first token that looks like a key type, so option-prefixed
    # lines still work.
    tokens = line.split()
    for index, token in enumerate(tokens):
        if token.startswith(("ssh-", "ecdsa-", "sk-")):
            try:
                return asyncssh.import_public_key(" ".join(tokens[index:]))
            except Exception:
                return None
    return None


class AuthPolicy:
    """How a deployment authenticates SSH clients.

    The base class **denies everything**: it requires authentication and supports
    no method, so a subclass that forgets to enable a method fails closed rather
    than open. Override only what you support.
    """

    def auth_required(self, username: str) -> bool:
        """Whether this user must authenticate at all.

        Parameters
        ----------
        username : str
            The username the client offered.

        Returns
        -------
        bool
            True to require authentication (the normal case). False lets the
            client straight in with no credential - only :class:`OpenAuth` does
            that.
        """
        return True

    # -- password ---------------------------------------------------------------

    def password_supported(self) -> bool:
        """Whether password authentication is offered."""
        return False

    async def verify_password(self, username: str, password: str) -> bool:
        """Check a password.

        Parameters
        ----------
        username : str
            The username the client offered.
        password : str
            The password the client sent.

        Returns
        -------
        bool
            Whether the credential is valid. Implementations should compare in
            constant time (see :func:`check_password`).
        """
        return False

    # -- public key -------------------------------------------------------------

    def public_key_supported(self) -> bool:
        """Whether public-key authentication is offered."""
        return False

    def authorized_keys_for(self, username: str) -> list[SSHKey] | None:
        """Return the keys this user may authenticate with.

        Parameters
        ----------
        username : str
            The username the client offered.

        Returns
        -------
        list of SSHKey or None
            The user's authorized keys, or None if the user is unknown (which
            denies them).
        """
        return None

    # -- keyboard-interactive ---------------------------------------------------

    def kbdint_supported(self) -> bool:
        """Whether keyboard-interactive authentication is offered."""
        return False

    def kbdint_prompts(self, username: str) -> KbdintPrompts:
        """The prompts to show for a keyboard-interactive challenge.

        Parameters
        ----------
        username : str
            The username the client offered.

        Returns
        -------
        sequence of (str, bool)
            ``(prompt_text, echo)`` pairs. ``echo=False`` hides typing, as for a
            password.
        """
        return [("Password: ", False)]

    async def verify_kbdint(self, username: str, responses: list[str]) -> bool:
        """Check the responses to a keyboard-interactive challenge.

        Parameters
        ----------
        username : str
            The username the client offered.
        responses : list of str
            One response per prompt from :meth:`kbdint_prompts`.

        Returns
        -------
        bool
            Whether the responses are valid.
        """
        return False


class OpenAuth(AuthPolicy):
    """No authentication: anyone may connect as any username.

    Development and demos only. There is no credential of any kind - the username
    is whatever the client typed and is not verified. Never expose this on an
    untrusted network.

    :class:`~wijjit_ssh.server.WijjitSSH` refuses to run with this policy unless
    ``allow_anonymous=True`` is also passed, and logs a warning when it does.
    """

    def auth_required(self, username: str) -> bool:
        return False


class AuthorizedKeys(AuthPolicy):
    """Public-key auth against OpenSSH ``authorized_keys`` files.

    The recommended policy for real deployments: no shared secret ever crosses
    the wire, and revoking access means deleting a line.

    Parameters
    ----------
    source : str, Path, or Mapping[str, str | Path], optional
        Either a single ``authorized_keys`` file whose keys authorize *any*
        username, or a mapping of username to that user's key file. Files are
        read once, at construction, so a missing or malformed path fails at
        startup rather than at the first login attempt.
    keys : Sequence[SSHKey], optional
        Authorized keys supplied directly rather than read from disk. Any
        username may use them. Mainly useful for tests and for deployments that
        source keys from somewhere other than a file.

    Raises
    ------
    ValueError
        If neither ``source`` nor ``keys`` is given.
    FileNotFoundError
        If a named key file does not exist.

    Examples
    --------
    >>> AuthorizedKeys("~/.ssh/authorized_keys")            # doctest: +SKIP
    >>> AuthorizedKeys({"alice": "keys/alice.pub"})         # doctest: +SKIP
    """

    def __init__(
        self,
        source: str | Path | Mapping[str, str | Path] | None = None,
        *,
        keys: Sequence[SSHKey] | None = None,
    ) -> None:
        self._global: list[SSHKey] | None = None
        self._per_user: dict[str, list[SSHKey]] | None = None

        if keys is not None:
            self._global = list(keys)
        elif isinstance(source, Mapping):
            self._per_user = {
                username: load_authorized_keys(path)
                for username, path in source.items()
            }
        elif source is not None:
            self._global = load_authorized_keys(source)
        else:
            raise ValueError(
                "AuthorizedKeys requires a path, a {username: path} mapping, or keys=."
            )

    def auth_required(self, username: str) -> bool:
        return True

    def public_key_supported(self) -> bool:
        return True

    def authorized_keys_for(self, username: str) -> list[SSHKey] | None:
        if self._global is not None:
            return self._global
        assert self._per_user is not None
        return self._per_user.get(username)


class PasswordAuth(AuthPolicy):
    """Password auth delegated to a callback.

    Parameters
    ----------
    checker : Callable[[str, str], bool | Awaitable[bool]]
        ``(username, password) -> bool``. May be sync or async; async is
        preferred for anything that talks to a database or computes a KDF, since
        a blocking checker stalls the whole server's event loop.
    keyboard_interactive : bool, optional
        Also offer the same check over keyboard-interactive (default True).
        Some clients prefer it, and it is what an interactive ``ssh`` session
        typically falls back to.

    Notes
    -----
    The callback owns credential comparison and must not leak timing: use
    :func:`check_password` for a constant-time compare of a plaintext secret, or
    a real password hash (argon2, bcrypt, scrypt) for anything stored at rest.

    Examples
    --------
    >>> from wijjit_ssh.auth import PasswordAuth, check_password
    >>> USERS = {"alice": "correct-horse"}
    >>> async def check(username, password):
    ...     expected = USERS.get(username)
    ...     return expected is not None and check_password(password, expected)
    >>> policy = PasswordAuth(check)
    """

    def __init__(
        self, checker: PasswordChecker, *, keyboard_interactive: bool = True
    ) -> None:
        self._checker = checker
        self._kbdint = keyboard_interactive

    def auth_required(self, username: str) -> bool:
        return True

    def password_supported(self) -> bool:
        return True

    async def verify_password(self, username: str, password: str) -> bool:
        result = self._checker(username, password)
        if isawaitable(result):
            return bool(await result)
        return bool(result)

    def kbdint_supported(self) -> bool:
        return self._kbdint

    def kbdint_prompts(self, username: str) -> KbdintPrompts:
        return [("Password: ", False)]

    async def verify_kbdint(self, username: str, responses: list[str]) -> bool:
        if not responses:
            return False
        return await self.verify_password(username, responses[0])


class ChainAuth(AuthPolicy):
    """Accept a client if **any** of several policies accepts.

    Lets a deployment offer, say, public keys for engineers and passwords for
    everyone else, without writing a bespoke policy.

    Parameters
    ----------
    *policies : AuthPolicy
        The policies to try. A method is offered if any policy offers it, and a
        credential is accepted if any policy that offers that method accepts it.

    Raises
    ------
    ValueError
        If no policies are given (which would deny everyone, silently).

    Notes
    -----
    If any policy does not require authentication (i.e. :class:`OpenAuth` is in
    the chain), the chain does not either - "accept if any accepts" applies to
    the no-credential case too. Chaining `OpenAuth` therefore makes every other
    policy in the chain irrelevant; it is almost certainly a mistake, and is
    logged as a warning.
    """

    def __init__(self, *policies: AuthPolicy) -> None:
        if not policies:
            raise ValueError("ChainAuth requires at least one policy.")
        self._policies = policies

        if any(isinstance(policy, OpenAuth) for policy in policies):
            logger.warning(
                "ChainAuth includes OpenAuth: every other policy in the chain is "
                "bypassed and the server accepts any client with no credential."
            )

    def auth_required(self, username: str) -> bool:
        return all(policy.auth_required(username) for policy in self._policies)

    def password_supported(self) -> bool:
        return any(policy.password_supported() for policy in self._policies)

    async def verify_password(self, username: str, password: str) -> bool:
        for policy in self._policies:
            if policy.password_supported() and await policy.verify_password(
                username, password
            ):
                return True
        return False

    def public_key_supported(self) -> bool:
        return any(policy.public_key_supported() for policy in self._policies)

    def authorized_keys_for(self, username: str) -> list[SSHKey] | None:
        collected: list[SSHKey] = []
        found = False
        for policy in self._policies:
            if not policy.public_key_supported():
                continue
            keys = policy.authorized_keys_for(username)
            if keys:
                collected.extend(keys)
                found = True
        return collected if found else None

    def kbdint_supported(self) -> bool:
        return any(policy.kbdint_supported() for policy in self._policies)

    def kbdint_prompts(self, username: str) -> KbdintPrompts:
        for policy in self._policies:
            if policy.kbdint_supported():
                return policy.kbdint_prompts(username)
        return []

    async def verify_kbdint(self, username: str, responses: list[str]) -> bool:
        for policy in self._policies:
            if policy.kbdint_supported() and await policy.verify_kbdint(
                username, responses
            ):
                return True
        return False
