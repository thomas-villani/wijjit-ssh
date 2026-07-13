"""wijjit-ssh: serve Wijjit TUI apps over SSH ("Flask for SSH apps").

Public API
----------
WijjitSSH
    The server. Give it a per-connection app factory and host keys, call
    ``run()``.
SSHSession
    Context handed to the factory (username, term type, size, backend).
RemoteTerminalBackend
    The :class:`~wijjit.terminal.backend.TerminalBackend` implementation that
    bridges a Wijjit app to an SSH channel. Usually you never touch it directly;
    the factory just forwards ``session.backend`` into ``Wijjit(backend=...)``.
AuthPolicy and presets
    How clients authenticate: ``AuthorizedKeys`` (recommended), ``PasswordAuth``,
    ``ChainAuth``, and the development-only ``OpenAuth``. Auth is fail-closed -
    ``WijjitSSH`` raises without a policy unless ``allow_anonymous=True``.
KeyDecoder, ChannelInputSource
    The byte-level input path (raw terminal bytes to Wijjit key/mouse events).
    Exported mainly for testing and for embedding Wijjit on other byte
    transports (telnet, a websocket, a local PTY).
"""

from wijjit_ssh.auth import (
    AuthorizedKeys,
    AuthPolicy,
    ChainAuth,
    OpenAuth,
    PasswordAuth,
    check_password,
    load_authorized_keys,
)
from wijjit_ssh.backend import RemoteTerminalBackend
from wijjit_ssh.input import ChannelInputSource, KeyDecoder
from wijjit_ssh.server import SSHSession, WijjitSSH

__all__ = [
    "WijjitSSH",
    "SSHSession",
    "RemoteTerminalBackend",
    "AuthPolicy",
    "AuthorizedKeys",
    "PasswordAuth",
    "ChainAuth",
    "OpenAuth",
    "check_password",
    "load_authorized_keys",
    "KeyDecoder",
    "ChannelInputSource",
]

__version__ = "0.0.1"
