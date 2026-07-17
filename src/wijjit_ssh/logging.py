"""Logging for wijjit-ssh: the ``wijjit_ssh`` logger tree, session-bound logs,
and the metrics hook.

Why this module exists rather than reusing :func:`wijjit.logging_config.get_logger`
------------------------------------------------------------------------------------
Wijjit's ``get_logger`` roots a name under its own namespace by prefixing
``"wijjit."`` - but only ``if not name.startswith("wijjit")``. Our modules are
named ``wijjit_ssh.*``, which *does* start with ``"wijjit"``, so the prefix is
never applied and the name passes through untouched. The result is a logger
called ``wijjit_ssh.server`` that is a **sibling** of the ``wijjit`` tree, not a
child of it.

That is not a cosmetic difference. ``wijjit.configure_logging()`` only clears,
handles, and sets ``propagate = False`` on the ``wijjit`` logger, so our loggers
inherit none of it - including ``configure_logging(None)``, the "turn logging
off" switch. A record from ``wijjit_ssh`` therefore propagates to the root
logger, and with no handler anywhere on the chain, :data:`logging.lastResort`
prints it to stderr. In a process where a Wijjit TUI has imported
``wijjit_ssh``, a warning sprays across the alternate screen buffer and corrupts
the frame.

So ``wijjit_ssh`` owns its own tree. The library posture here is deliberately
the conventional one rather than Wijjit's:

* A :class:`~logging.NullHandler` is attached to the ``wijjit_ssh`` logger **at
  import time**. :meth:`logging.Logger.callHandlers` only falls back to
  ``lastResort`` when it finds *zero* handlers anywhere on the chain, so this
  single handler is what stops the stderr spray.
* ``propagate`` is left **True**. A host application that configures the root
  logger receives our records, which is what a library embedded in someone
  else's process should do (and it keeps pytest's ``caplog`` working, which is
  why we need no equivalent of Wijjit's ``wijjit_caplog`` workaround).
* :func:`configure_logging` is opt-in. :meth:`WijjitSSH.run` calls it because
  ``run()`` owns the process; ``start()``/``run_async()`` never do, because they
  may be one coroutine inside a larger application.

Note on the module name: this file is ``wijjit_ssh/logging.py``, but ``import
logging`` below resolves to the standard library, not to itself - Python 3 uses
absolute imports. Importing this module as ``wijjit_ssh.logging`` is
unambiguous.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import IO, Any

__all__ = [
    "LOGGER_NAME",
    "EventEmitter",
    "EventHook",
    "SessionLog",
    "configure_logging",
    "get_logger",
    "new_session_id",
    "session_logger",
]

#: Root of this package's logger tree.
LOGGER_NAME = "wijjit_ssh"

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Attach at import time so a record can never reach logging.lastResort and print
# to stderr behind a host application's back. See the module docstring.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a logger rooted under the ``wijjit_ssh`` tree.

    Parameters
    ----------
    name : str
        Module name, typically ``__name__``. Names that are not already under
        ``wijjit_ssh`` are prefixed with it.

    Returns
    -------
    logging.Logger
        A logger guaranteed to be ``wijjit_ssh`` or a descendant of it.

    Examples
    --------
    >>> logger = get_logger(__name__)
    >>> logger.debug("decoded %d bytes", 12)
    """
    # The dot matters: "wijjit_sshfoo" starts with LOGGER_NAME but is not in the
    # tree. Getting this test wrong is precisely the bug this module exists to
    # fix, so be exact about it rather than reusing a bare startswith().
    if name != LOGGER_NAME and not name.startswith(f"{LOGGER_NAME}."):
        name = f"{LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def configure_logging(
    destination: str | Path | IO[str] | None = None,
    level: str | int = logging.INFO,
    *,
    format_string: str | None = None,
) -> None:
    """Configure the ``wijjit_ssh`` logger tree.

    Opt-in: this is never called on import. :meth:`WijjitSSH.run` calls it (that
    entry point owns the process); :meth:`WijjitSSH.start` and
    :meth:`WijjitSSH.run_async` do not, since they may be embedded in a host
    application that has its own logging setup.

    Unlike a Wijjit app - where stderr is the terminal the app is drawing on -
    an SSH server's stdout/stderr are ordinary process streams, so stderr is a
    sane default destination and is what systemd/Docker expect to collect.

    Parameters
    ----------
    destination : str, Path, file object, or None, optional
        Where records go. A ``str``/:class:`~pathlib.Path` opens a UTF-8 file in
        append mode; a file object (e.g. :data:`sys.stderr`) is wrapped in a
        :class:`~logging.StreamHandler`; ``None`` (the default) silences the
        tree.
    level : str or int, optional
        Level name (``"DEBUG"``) or constant (:data:`logging.DEBUG`). Default
        :data:`logging.INFO`.
    format_string : str, optional
        Custom :class:`~logging.Formatter` format. Defaults to
        :data:`DEFAULT_FORMAT`.

    Returns
    -------
    None

    Examples
    --------
    >>> import sys
    >>> configure_logging(sys.stderr, level="DEBUG")   # doctest: +SKIP
    >>> configure_logging("wijjit-ssh.log")            # doctest: +SKIP
    """
    logger = logging.getLogger(LOGGER_NAME)

    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        # Only close handlers we could plausibly have opened. Closing a
        # caller-supplied stream (sys.stderr!) would be a hostile side effect.
        if isinstance(existing, logging.FileHandler):
            existing.close()

    if destination is None:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        return

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    handler: logging.Handler
    if isinstance(destination, (str, Path)):
        handler = logging.FileHandler(destination, mode="a", encoding="utf-8")
    else:
        handler = logging.StreamHandler(destination)

    handler.setFormatter(
        logging.Formatter(format_string or DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT)
    )
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)


def _has_real_handler(logger: logging.Logger) -> bool:
    """Whether ``logger`` has a handler that would actually emit a record."""
    return any(
        not isinstance(handler, logging.NullHandler) for handler in logger.handlers
    )


def logging_is_configured() -> bool:
    """Whether records from this package would reach a handler already.

    Used by :meth:`WijjitSSH.run` to decide whether to install a default stderr
    handler: a host that has already set up either our tree or the root logger
    keeps full control, and we stay out of it.

    Returns
    -------
    bool
        True if the ``wijjit_ssh`` tree or the root logger has a non-null
        handler.
    """
    return _has_real_handler(logging.getLogger(LOGGER_NAME)) or _has_real_handler(
        logging.getLogger()
    )


def new_session_id() -> str:
    """Return a short, unique-enough id for one session.

    Eight hex characters: long enough that ids in a log file don't collide in
    practice, short enough to sit in every line and still be greppable. This is
    a correlation handle, not a security token.

    Returns
    -------
    str
        Eight lowercase hex characters, e.g. ``"3f9a1c04"``.
    """
    return secrets.token_hex(4)


class SessionLog(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A logger bound to one SSH session, prefixing ``[id user@ip]``.

    Wraps a plain logger so every record from a session carries the session id,
    username, and peer address without each call site having to pass them.

    An adapter is used rather than a :class:`~contextvars.ContextVar` for a
    structural reason: the asyncssh callbacks (``session_started``,
    ``data_received``, ``connection_lost``) run on asyncssh's connection task,
    while the app runs in a task we create in ``session_started``. A contextvar
    set inside the app task would be invisible from exactly the callbacks that
    need to log. Binding the context to the session object sidesteps the
    question of which task is running.

    Parameters
    ----------
    logger : logging.Logger
        The underlying logger to write through.
    extra : dict
        Must contain ``session_id``, ``username``, and ``peer_ip``.

    Examples
    --------
    >>> log = session_logger("3f9a1c04", "ada", "10.0.0.7")
    >>> log.info("pty requested")   # -> "[3f9a1c04 ada@10.0.0.7] pty requested"
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = self.extra or {}
        return (
            f"[{extra.get('session_id')} {extra.get('username')}"
            f"@{extra.get('peer_ip')}] {msg}",
            kwargs,
        )


def session_logger(session_id: str, username: str, peer_ip: str) -> SessionLog:
    """Build a :class:`SessionLog` for one session.

    Parameters
    ----------
    session_id : str
        Correlation id, from :func:`new_session_id`.
    username : str
        Authenticated username.
    peer_ip : str
        Client address.

    Returns
    -------
    SessionLog
        A logger that prefixes every record with the session context.
    """
    return SessionLog(
        get_logger("wijjit_ssh.session"),
        {"session_id": session_id, "username": username, "peer_ip": peer_ip},
    )


#: Signature of the ``on_event`` metrics hook: ``(event_name, fields) -> None``.
EventHook = Callable[[str, Mapping[str, object]], None]


class EventEmitter:
    """Dispatches lifecycle events to a deployment's optional metrics hook.

    Lets a deployment wire up Prometheus (or anything else) without this package
    taking a dependency on a metrics library. Events emitted:

    ==========================  ============================================
    Event                       Fields
    ==========================  ============================================
    ``connection.opened``       ``peer_ip``
    ``connection.rejected``     ``peer_ip``, ``reason``
    ``connection.closed``       ``peer_ip``
    ``auth.ok``                 ``username``, ``peer_ip``, ``method``
    ``auth.failed``             ``username``, ``peer_ip``, ``method``
    ``session.started``         ``session_id``, ``username``, ``peer_ip``
    ``session.rejected``        ``peer_ip``, ``reason``
    ``session.ended``           ``session_id``, ``reason``, ``duration``
    ==========================  ============================================

    Parameters
    ----------
    on_event : callable, optional
        ``(event: str, fields: Mapping[str, object]) -> None``. If None, every
        :meth:`emit` is a no-op.

    Examples
    --------
    >>> from collections import Counter
    >>> counts: Counter[str] = Counter()
    >>> emitter = EventEmitter(lambda event, fields: counts.update([event]))
    >>> emitter.emit("session.started", session_id="3f9a1c04")
    >>> counts["session.started"]
    1
    """

    def __init__(self, on_event: EventHook | None = None) -> None:
        self._on_event = on_event

    def emit(self, event: str, /, **fields: object) -> None:
        """Fire one event. Never raises.

        A deployment's counter must not be able to take down a session, so a
        raising hook is logged and swallowed: these callbacks run inside
        asyncssh callbacks, where an exception would propagate into the
        transport rather than anywhere useful.

        Parameters
        ----------
        event : str
            Event name, e.g. ``"session.ended"``.
        **fields : object
            Event payload.

        Returns
        -------
        None
        """
        if self._on_event is None:
            return
        try:
            self._on_event(event, fields)
        except Exception:
            get_logger(__name__).exception(
                "on_event hook raised for event %r; ignoring", event
            )
