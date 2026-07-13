"""A :class:`~wijjit.terminal.backend.TerminalBackend` bridged to an SSH channel.

This is the whole reason the backend seam exists: :class:`RemoteTerminalBackend`
runs an ordinary Wijjit app against an ``asyncssh`` session instead of the local
console. It:

* writes rendered frames and screen-control sequences to the SSH channel,
* decodes inbound channel bytes into Wijjit key/mouse events on the event loop
  (see :mod:`wijjit_ssh.input`), so every element behaves exactly as it does
  locally, and
* reports the client's negotiated PTY size (refreshed on ``SIGWINCH``-style
  resize events) as a task-local override, so many differently-sized sessions
  coexist in one server process.

It sets ``owns_terminal = False`` so the event loop installs none of the
process-global terminal machinery (SIGTERM/SIGHUP/atexit restore, SIGTSTP
suspend) - there is no local tty to restore, and those handlers are
process-global and would collide across concurrent sessions.

The channel is opened in **binary mode** (``encoding=None`` on the server), so
inbound data arrives as raw bytes for the decoder and outbound frames are
encoded here, at the one boundary where text becomes wire bytes.
"""

from __future__ import annotations

from typing import TextIO

from wijjit.terminal.backend import TerminalBackend
from wijjit.terminal.mouse import MouseTrackingMode

from wijjit_ssh.input import ChannelInputSource


class _ChannelWriter:
    """Text stream that encodes onto a binary ``asyncssh`` channel.

    Presents the ``write``/``flush`` surface that
    :class:`~wijjit.terminal.screen.ScreenManager` and
    :class:`~wijjit_ssh.input.ChannelInputSource` expect, while the channel
    underneath is binary. ``flush`` is a no-op: ``asyncssh`` channels buffer and
    drain on the event loop themselves.

    Parameters
    ----------
    chan : asyncssh.SSHServerChannel
        The channel to write to. Must be written from the event-loop thread.
    """

    def __init__(self, chan: object) -> None:
        self._chan = chan

    def write(self, data: str) -> None:
        """Encode and write terminal output to the channel.

        Parameters
        ----------
        data : str
            Frame or control-sequence text. ANSI escapes survive the UTF-8
            encoding unchanged (they are ASCII).
        """
        # Silently drop writes once the peer has gone, so a final frame racing
        # connection loss cannot crash the session's task.
        try:
            self._chan.write(data.encode("utf-8", errors="replace"))  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def flush(self) -> None:
        return None


class RemoteTerminalBackend(TerminalBackend):
    """Terminal backend that drives a Wijjit app over an SSH channel.

    Parameters
    ----------
    chan : asyncssh.SSHServerChannel
        The client's session channel; frames are written here.
    columns : int
        Initial terminal width negotiated by the PTY request.
    lines : int
        Initial terminal height negotiated by the PTY request.
    """

    # Never touch process-global terminal state - there is no local tty here and
    # the process is shared by every concurrent session.
    owns_terminal = False
    # get_size() is authoritative and should be published to the task-local
    # size override so render/layout track this client's dimensions.
    provides_size = True

    def __init__(self, chan: object, columns: int, lines: int) -> None:
        self._writer = _ChannelWriter(chan)
        self._columns = columns
        self._lines = lines
        self._input: ChannelInputSource | None = None

    @property
    def screen_output(self) -> TextIO | None:
        # ScreenManager writes alt-buffer/cursor/title sequences to the channel.
        return self._writer  # type: ignore[return-value]

    def write_frame(self, data: str) -> None:
        self._writer.write(data)

    def get_size(self) -> tuple[int, int]:
        return (self._columns, self._lines)

    def create_input_handler(
        self,
        *,
        enable_mouse: bool,
        mouse_tracking_mode: MouseTrackingMode | None,
    ) -> ChannelInputSource:
        """Build this session's input source.

        Called once by :class:`~wijjit.core.app.Wijjit` during construction. The
        instance is retained so :meth:`feed` can route inbound channel bytes to
        it.

        Parameters
        ----------
        enable_mouse : bool
            Whether the app wants mouse tracking.
        mouse_tracking_mode : MouseTrackingMode or None
            Requested tracking granularity.

        Returns
        -------
        ChannelInputSource
            An input source fed by this channel's byte stream.
        """
        self._input = ChannelInputSource(
            self._writer,  # type: ignore[arg-type]
            enable_mouse=enable_mouse,
            mouse_tracking_mode=mouse_tracking_mode,
        )
        return self._input

    # -- transport-side hooks (called by the SSH session, not by Wijjit) --------

    def feed(self, data: bytes) -> None:
        """Push inbound channel bytes into the input decoder.

        Parameters
        ----------
        data : bytes
            Raw bytes received on the channel.

        Notes
        -----
        A no-op until the app has been constructed (which is what creates the
        input source), so data racing session startup is dropped rather than
        crashing the session.
        """
        if self._input is not None:
            self._input.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        """Record a new client terminal size.

        The running event loop reads this via :meth:`get_size` on its next frame
        and republishes it to the task-local size override, so overlays,
        notifications, and layout reflow to the new dimensions.

        Parameters
        ----------
        columns : int
            New terminal width.
        lines : int
            New terminal height.
        """
        self._columns = columns
        self._lines = lines
