"""SSH host keys: loading, first-run generation, and fingerprints.

A host key is the server's identity. Clients pin it on first connect and refuse
to talk to you if it changes, so the operational rules are: generate one, keep
it private, keep it stable across restarts, and rotate deliberately (serve the
old and new key together for a transition window - see :func:`load_host_keys`,
which takes several).

The DX goal is that a first run should just work without a README detour through
``ssh-keygen``, while a production run should never silently invent a new
identity that breaks every client's ``known_hosts``. :func:`ensure_host_key`
does the former and logs loudly about the latter.

Examples
--------
Development, or a container with a mounted volume - generate on first run,
reuse it forever after::

    >>> from wijjit_ssh.keys import ensure_host_key
    >>> WijjitSSH(make_app, host_keys=[ensure_host_key("ssh_host_key")], ...)

Production - manage the key out of band and fail if it is missing::

    $ ssh-keygen -t ed25519 -f ssh_host_key -N ''

    >>> from wijjit_ssh.keys import load_host_keys
    >>> WijjitSSH(make_app, host_keys=load_host_keys(["ssh_host_key"]), ...)
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Union

import asyncssh
from asyncssh import SSHKey

from wijjit_ssh.logging import get_logger

__all__ = [
    "DEFAULT_HOST_KEY_ALGORITHM",
    "HostKeySource",
    "ensure_host_key",
    "fingerprint",
    "load_host_keys",
    "resolve_host_keys",
]

logger = get_logger(__name__)

#: Algorithm used by :func:`ensure_host_key`. ed25519: small, fast, no parameter
#: choices to get wrong, and supported by every client we target.
DEFAULT_HOST_KEY_ALGORITHM = "ssh-ed25519"

#: Anything accepted as a host key: a path to a private key file, or an
#: already-loaded key. Paths cover the config-file case, live keys cover tests
#: and callers who mint keys themselves.
HostKeySource = Union[str, "os.PathLike[str]", SSHKey]


def fingerprint(key: SSHKey) -> str:
    """Return a human-readable ``"<algorithm> <SHA256:...>"`` fingerprint.

    The string clients compare against ``known_hosts``, so it is what you want
    in a startup log line and in a "did my key change?" investigation.

    Parameters
    ----------
    key : asyncssh.SSHKey
        The key to describe.

    Returns
    -------
    str
        e.g. ``"ssh-ed25519 SHA256:8xKNBvF6TPMK/LxQ+zKOmM0GzGIQSfEfp4pF/ZqWlE4"``.
    """
    algorithm = key.algorithm
    # SSHKey.algorithm is bytes.
    name = algorithm.decode("ascii", errors="replace")
    return f"{name} {key.get_fingerprint()}"


def _warn_on_loose_permissions(path: Path) -> None:
    """Warn if a private key is readable by anyone but its owner.

    Advisory only: refusing to start would be worse than serving with a warning,
    and unlike OpenSSH we are not in a position to know the deployment's threat
    model. OpenSSH itself hard-fails on this for *client* keys, which is why the
    warning is worth printing even though we proceed.

    Parameters
    ----------
    path : Path
        The private key file to check.

    Returns
    -------
    None
    """
    if os.name == "nt":
        # POSIX mode bits are not meaningful on Windows - the file is governed
        # by ACLs inherited from its directory, which st_mode does not reflect
        # (it reports 0o666 regardless). Checking it would only produce noise.
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - raced away between load and stat
        return
    if mode & 0o077:
        logger.warning(
            "Host key %s is accessible to group/other (mode %o). Anyone who can "
            "read it can impersonate this server; run: chmod 600 %s",
            path,
            mode,
            path,
        )


def _write_private_key_secure(path: Path, key: SSHKey) -> None:
    """Write ``key`` to ``path``, private from the moment it exists.

    Deliberately not :meth:`asyncssh.SSHKey.write_private_key`, which is a plain
    file write with no mode: that would create the key world-readable and only
    narrow it afterwards, leaving a window in which the server's identity is
    readable by any local user.

    ``O_CREAT | O_EXCL`` with mode ``0o600`` closes both windows at once - the
    file is private from creation, and an exclusive create means two processes
    starting together cannot both think they generated the key (the loser gets
    :exc:`FileExistsError` and reads the winner's).

    On Windows the mode argument is ignored (the file inherits its directory's
    ACLs), which is why there is no ``sys.platform`` branch here: the call is
    portable, only its guarantee is weaker. See the module docstring.

    Parameters
    ----------
    path : Path
        Destination file. Must not exist.
    key : asyncssh.SSHKey
        Key to serialize in OpenSSH private key format.

    Returns
    -------
    None

    Raises
    ------
    FileExistsError
        If ``path`` already exists (a concurrent generator won the race).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key.export_private_key())
    finally:
        os.close(fd)


def load_host_keys(paths: Iterable[str | os.PathLike[str]]) -> list[SSHKey]:
    """Load host keys from private key files.

    Pass more than one to rotate: asyncssh offers every key it is given, so a
    server can serve a new key alongside the old one until clients have seen it.

    Parameters
    ----------
    paths : iterable of str or PathLike
        Private key files, in OpenSSH or PEM format.

    Returns
    -------
    list[asyncssh.SSHKey]
        The loaded keys, in the order given.

    Raises
    ------
    FileNotFoundError
        If a path does not exist. Never silently skipped: a typo in a key path
        would otherwise start a server with a *different* identity than
        intended, which every client would report as a possible attack.
    ValueError
        If a file exists but is not a readable private key.

    Examples
    --------
    >>> keys = load_host_keys(["ssh_host_key"])            # doctest: +SKIP
    >>> keys = load_host_keys(["host_key_new", "host_key_old"])  # rotation
    """
    keys: list[SSHKey] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            key = asyncssh.read_private_key(path)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Host key file not found: {path}. Generate one with: "
                f"ssh-keygen -t ed25519 -f {path} -N '' - or use "
                f"wijjit_ssh.keys.ensure_host_key() to create it on first run."
            ) from exc
        except asyncssh.KeyImportError as exc:
            # KeyImportError's message is just "Invalid private key" with no
            # path, which is unhelpful when several keys were passed.
            raise ValueError(f"Could not read host key {path}: {exc}") from exc

        _warn_on_loose_permissions(path)
        logger.info("Loaded host key %s from %s", fingerprint(key), path)
        keys.append(key)
    return keys


def ensure_host_key(
    path: str | os.PathLike[str], *, algorithm: str = DEFAULT_HOST_KEY_ALGORITHM
) -> SSHKey:
    """Load the host key at ``path``, generating and persisting it if absent.

    The convenient default for development and for containers with a mounted
    volume: the first run creates a key, every later run reuses it, so clients'
    ``known_hosts`` entries stay valid.

    In production, prefer :func:`load_host_keys` and manage the key out of band.
    The difference matters: if a deployment's volume is not actually persistent,
    ``ensure_host_key`` will cheerfully mint a new identity on every restart and
    every client will report a host key mismatch. That is why generation logs at
    WARNING rather than INFO - on a healthy server it should happen exactly once.

    Parameters
    ----------
    path : str or PathLike
        Private key file. Parent directories are created if needed.
    algorithm : str, optional
        Key algorithm to generate, in asyncssh's naming (default
        :data:`DEFAULT_HOST_KEY_ALGORITHM`, ``"ssh-ed25519"``). Ignored when the
        file already exists.

    Returns
    -------
    asyncssh.SSHKey
        The loaded or newly generated key.

    Raises
    ------
    ValueError
        If the file exists but is not a readable private key, or if
        ``algorithm`` is not a valid key algorithm.
    OSError
        If the key cannot be written.

    Examples
    --------
    >>> key = ensure_host_key("ssh_host_key")              # doctest: +SKIP
    >>> key = ensure_host_key("/var/lib/myapp/host_key")   # doctest: +SKIP
    """
    resolved = Path(path).expanduser()
    if resolved.exists():
        return load_host_keys([resolved])[0]

    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        key = asyncssh.generate_private_key(algorithm)
    except asyncssh.KeyGenerationError as exc:
        raise ValueError(f"Cannot generate a {algorithm!r} host key: {exc}") from exc

    try:
        _write_private_key_secure(resolved, key)
    except FileExistsError:
        # Another process generated it between our exists() check and now. Its
        # key is as good as ours and is the one on disk, so adopt it - both
        # processes must serve the same identity.
        logger.info("Host key %s was created concurrently; loading it", resolved)
        return load_host_keys([resolved])[0]

    logger.warning(
        "Generated a new %s host key at %s (%s). This is the server's identity: "
        "keep this file, and back it up. Clients that trusted a previous key "
        "will refuse to connect until their known_hosts entry is updated.",
        algorithm,
        resolved,
        fingerprint(key),
    )
    return key


def resolve_host_keys(sources: Iterable[HostKeySource]) -> list[SSHKey]:
    """Normalize mixed host key sources into loaded keys.

    Accepts paths and already-loaded :class:`~asyncssh.SSHKey` objects in one
    list, so callers can mix ``ensure_host_key(...)`` with a path from config
    without thinking about it.

    Resolving eagerly (rather than handing paths to asyncssh at listen time) is
    the point: a bad key path becomes a clear error where the server was
    *configured*, with a fingerprint logged for the key that was actually
    loaded, instead of a late failure inside ``create_server``.

    Parameters
    ----------
    sources : iterable of str, PathLike, or asyncssh.SSHKey
        Mixed host key sources.

    Returns
    -------
    list[asyncssh.SSHKey]
        Loaded keys, in the order given.

    Raises
    ------
    FileNotFoundError
        If a path does not exist.
    ValueError
        If a path is not a readable private key.

    Examples
    --------
    >>> resolve_host_keys([ensure_host_key("host_key"), "backup_key"])  # doctest: +SKIP
    """
    keys: list[SSHKey] = []
    for source in sources:
        if isinstance(source, SSHKey):
            keys.append(source)
        else:
            keys.extend(load_host_keys([source]))
    return keys
