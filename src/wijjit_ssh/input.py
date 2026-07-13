"""Async byte-level input decoding for SSH sessions.

A local Wijjit app reads the keyboard through
:class:`wijjit.terminal.input.InputHandler`, which polls prompt_toolkit on a
background thread. That is fine for one foreground app, but wrong at server
scale: it costs an OS thread (and a prompt_toolkit pipe) per connection, when
the bytes are already being delivered to us on the event loop by ``asyncssh``.

This module replaces that path for remote sessions with two pieces:

:class:`KeyDecoder`
    A pure, resumable state machine turning raw terminal bytes into Wijjit
    :class:`~wijjit.terminal.input.Key` and
    :class:`~wijjit.terminal.mouse.MouseEvent` objects. It buffers incomplete
    trailing sequences across calls, so a keystroke split across TCP packets
    (or a UTF-8 rune split mid-character) decodes correctly. No I/O, no
    threads, no clock - which also makes it exhaustively unit-testable.

:class:`ChannelInputSource`
    The event loop's input handler for a session. It owns a decoder, pushes
    decoded events onto an :class:`asyncio.Queue`, and satisfies the duck-typed
    surface the loop calls (``read_input_async``, ``mouse_enabled``,
    ``enable/disable_mouse_tracking``, ``close``, ``restore_terminal``).

The one place a timer is unavoidable is the lone-ESC ambiguity: a bare ``ESC``
byte is either the Escape key or the first byte of a sequence still in flight,
and nothing in the byte stream distinguishes them. :class:`ChannelInputSource`
resolves it by scheduling :meth:`KeyDecoder.flush` a few tens of milliseconds
after the last byte (see :data:`ESCAPE_TIMEOUT`). SSH almost always delivers a
full sequence in one packet, so this rarely fires.
"""

from __future__ import annotations

import asyncio
import codecs
from typing import TYPE_CHECKING, Union

from wijjit.logging_config import get_logger
from wijjit.terminal.input import (
    MAX_PASTE_SIZE,
    SINGLE_CHAR_KEYS,
    Key,
    Keys,
    KeyType,
)
from wijjit.terminal.mouse import MouseEvent, MouseEventParser, MouseTrackingMode

if TYPE_CHECKING:
    from typing import TextIO

logger = get_logger(__name__)

InputEvent = Union[Key, MouseEvent]

# How long to wait after a bare ESC before deciding it was the Escape key and
# not the start of a sequence still in flight. Long enough to absorb the gap
# between packets of a split sequence, short enough that Escape still feels
# instant. 30-50ms is the conventional range.
ESCAPE_TIMEOUT = 0.05

# A well-formed escape sequence is short (the longest realistic one is an SGR
# mouse report, ~15 bytes). If a sequence has not terminated within this many
# bytes it is malformed or hostile; drop the leading ESC and resynchronize
# rather than buffering forever.
MAX_SEQUENCE = 64

_ESC = 0x1B
_DEL = 0x7F

# Bracketed-paste delimiters. Everything between them is literal text and must
# not be interpreted as keys (that is the entire point of bracketed paste).
_PASTE_START_PARAMS = "200"
_PASTE_END = b"\x1b[201~"

# Wijjit's Keys has no Insert; the rest we reuse as-is.
_INSERT = Key("insert", KeyType.SPECIAL)

# SS3 sequences (ESC O <final>): application-cursor mode, emitted by PuTTY and
# by xterm once an app switches the cursor keys into application mode.
SS3_KEYS: dict[str, Key] = {
    "A": Keys.UP,
    "B": Keys.DOWN,
    "C": Keys.RIGHT,
    "D": Keys.LEFT,
    "H": Keys.HOME,
    "F": Keys.END,
    "P": Keys.F1,
    "Q": Keys.F2,
    "R": Keys.F3,
    "S": Keys.F4,
}

# CSI sequences ending in a letter (ESC [ <params> <letter>).
CSI_LETTER_KEYS: dict[str, Key] = {
    "A": Keys.UP,
    "B": Keys.DOWN,
    "C": Keys.RIGHT,
    "D": Keys.LEFT,
    "H": Keys.HOME,
    "F": Keys.END,
    "Z": Keys.BACKTAB,
}

# CSI sequences ending in "~" (ESC [ <number> ~), keyed by the number.
CSI_TILDE_KEYS: dict[int, Key] = {
    1: Keys.HOME,
    2: _INSERT,
    3: Keys.DELETE,
    4: Keys.END,
    5: Keys.PAGE_UP,
    6: Keys.PAGE_DOWN,
    7: Keys.HOME,
    8: Keys.END,
    11: Keys.F1,
    12: Keys.F2,
    13: Keys.F3,
    14: Keys.F4,
    15: Keys.F5,
    17: Keys.F6,
    18: Keys.F7,
    19: Keys.F8,
    20: Keys.F9,
    21: Keys.F10,
    23: Keys.F11,
    24: Keys.F12,
}


def _apply_modifier(base: Key, mask: int) -> Key:
    """Return ``base`` with modifier names applied.

    xterm encodes modifiers as a second CSI parameter whose value is
    ``1 + bitmask``, with bit 0 = shift, bit 1 = alt, bit 2 = ctrl. For example
    ``ESC [ 1 ; 5 A`` is Ctrl+Up (5 - 1 = 4 = ctrl).

    Parameters
    ----------
    base : Key
        The unmodified key (e.g. :attr:`Keys.UP`).
    mask : int
        The decoded modifier bitmask (already had 1 subtracted). Values <= 0
        mean "no modifiers".

    Returns
    -------
    Key
        ``base`` itself when no modifiers apply, otherwise a new key whose name
        is prefixed (e.g. ``"ctrl+up"``, ``"ctrl+shift+left"``). The key type is
        preserved, matching how Wijjit names modified keys locally.
    """
    if mask <= 0:
        return base

    mods: list[str] = []
    if mask & 4:
        mods.append("ctrl")
    if mask & 2:
        mods.append("alt")
    if mask & 1:
        mods.append("shift")
    if not mods:
        return base

    return Key("+".join([*mods, base.name]), base.key_type, base.char)


class KeyDecoder:
    """Resumable byte-to-event decoder for terminal input.

    Feed it whatever bytes arrive on the wire; it returns the events it can
    fully decode and retains any incomplete trailing sequence for the next call.
    It is a pure state machine: no I/O, no threads, and no clock (the one
    time-dependent decision, the lone-ESC ambiguity, is delegated to the caller
    via :meth:`pending_escape` / :meth:`flush`).

    Parameters
    ----------
    mouse_parser : MouseEventParser, optional
        Parser used for mouse sequences. Supply one to share click-synthesis
        state; by default a fresh parser is created.

    Attributes
    ----------
    mouse_parser : MouseEventParser
        The parser used for SGR and legacy mouse reports. It carries the
        press/release state that synthesizes CLICK and DOUBLE_CLICK events, so
        it must persist across calls.
    """

    def __init__(self, mouse_parser: MouseEventParser | None = None) -> None:
        self.mouse_parser = mouse_parser or MouseEventParser()
        self._buf = bytearray()
        # "replace" rather than "strict": a hostile or misconfigured client must
        # not be able to kill a session with a bad byte.
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        # Non-None while inside a bracketed paste; accumulates the payload.
        self._paste: bytearray | None = None

    # -- public API ------------------------------------------------------------

    def feed(self, data: bytes) -> list[InputEvent]:
        """Decode a chunk of raw terminal bytes.

        Parameters
        ----------
        data : bytes
            Bytes as received from the channel. May contain any number of whole
            events, and may end mid-sequence or mid-rune.

        Returns
        -------
        list of Key or MouseEvent
            Every event that could be fully decoded, in arrival order. An
            incomplete trailing sequence is retained internally for the next
            call and is not reported here.
        """
        self._buf.extend(data)

        events: list[InputEvent] = []
        while self._buf:
            if self._paste is not None:
                if not self._consume_paste(events):
                    break  # paste still open; wait for the terminator
                continue
            if self._consume_one(events) == 0:
                break  # incomplete sequence; wait for more bytes
        return events

    def pending_escape(self) -> bool:
        """Whether the buffer holds a bare ESC awaiting disambiguation.

        Returns
        -------
        bool
            True when the only thing buffered is a single ``ESC`` byte, which
            is either the Escape key or the start of a sequence still in
            flight. The caller resolves this with a timer (see
            :data:`ESCAPE_TIMEOUT`) and then calls :meth:`flush`.

        Notes
        -----
        Deliberately narrow: a *partial* sequence (``ESC [`` with no final byte)
        is not reported, because the rest is almost certainly in the next packet
        and flushing it as Escape would corrupt a real keypress.
        """
        return self._paste is None and self._buf == b"\x1b"

    def flush(self) -> list[InputEvent]:
        """Resolve a pending lone ESC as the Escape key.

        Called by the session once :data:`ESCAPE_TIMEOUT` has elapsed with no
        further bytes.

        Returns
        -------
        list of Key or MouseEvent
            ``[Keys.ESCAPE]`` if a bare ESC was buffered, else an empty list
            (bytes arrived in the meantime and already decoded).
        """
        if self.pending_escape():
            del self._buf[:1]
            return [Keys.ESCAPE]
        return []

    # -- decoding ---------------------------------------------------------------

    def _consume_one(self, events: list[InputEvent]) -> int:
        """Decode one event from the front of the buffer.

        Parameters
        ----------
        events : list
            Output list; decoded events are appended.

        Returns
        -------
        int
            Number of bytes consumed, or 0 if the buffer holds only the start of
            an incomplete sequence.
        """
        first = self._buf[0]
        if first == _ESC:
            return self._consume_escape(events)
        if first < 0x20 or first == _DEL:
            events.append(self._control_key(first))
            del self._buf[:1]
            return 1
        return self._consume_text(events)

    def _consume_text(self, events: list[InputEvent]) -> int:
        """Decode a run of printable (possibly multi-byte UTF-8) characters.

        Parameters
        ----------
        events : list
            Output list; decoded character keys are appended.

        Returns
        -------
        int
            Number of bytes consumed (always >= 1).

        Notes
        -----
        The run extends over every byte >= 0x20 other than DEL, which covers all
        UTF-8 lead and continuation bytes. Bytes are handed to a persistent
        incremental decoder, so a rune split across packets is held internally
        and completed when its tail arrives. When the run is closed by a
        following control byte, any held bytes are a genuinely truncated rune and
        are flushed as U+FFFD rather than left to corrupt the next character.
        """
        size = len(self._buf)
        end = 0
        while end < size and self._buf[end] >= 0x20 and self._buf[end] != _DEL:
            end += 1

        run = bytes(self._buf[:end])
        closed = end < size

        text = self._utf8.decode(run, final=False)
        if closed:
            text += self._utf8.decode(b"", final=True)
            self._utf8.reset()

        for char in text:
            events.append(
                Keys.SPACE if char == " " else Key(char, KeyType.CHARACTER, char)
            )

        del self._buf[:end]
        return end

    def _control_key(self, byte: int) -> Key:
        """Map a C0 control byte (or DEL) to a key.

        Parameters
        ----------
        byte : int
            The control byte.

        Returns
        -------
        Key
            The named key when one exists (Enter, Tab, Backspace, ...), else the
            corresponding Ctrl+letter.
        """
        char = chr(byte)
        named = SINGLE_CHAR_KEYS.get(char)
        if named is not None:
            return named
        if 1 <= byte <= 26:
            letter = chr(byte - 1 + ord("a"))
            return Key(f"ctrl+{letter}", KeyType.CONTROL, char)
        # 0x1c-0x1f: Ctrl+\, Ctrl+], Ctrl+^, Ctrl+_
        return Key(f"ctrl+{chr(byte + 0x40).lower()}", KeyType.CONTROL, char)

    def _consume_escape(self, events: list[InputEvent]) -> int:
        """Decode a sequence introduced by ESC.

        Parameters
        ----------
        events : list
            Output list; the decoded event is appended.

        Returns
        -------
        int
            Bytes consumed, or 0 when the sequence is incomplete.
        """
        size = len(self._buf)
        if size < 2:
            return 0  # bare ESC: only a timer can disambiguate it

        second = self._buf[1]

        if second == ord("["):
            return self._consume_csi(events)

        if second == ord("O"):
            if size < 3:
                return 0
            key = SS3_KEYS.get(chr(self._buf[2]))
            if key is not None:
                events.append(key)
            del self._buf[:3]
            return 3

        if second == _ESC:
            # ESC ESC: the first is a real Escape; re-examine the second.
            events.append(Keys.ESCAPE)
            del self._buf[:1]
            return 1

        char = chr(second)
        if second < 0x80 and char.isalpha():
            # Alt+<letter> arrives as ESC followed by the letter.
            events.append(Key(f"alt+{char.lower()}", KeyType.CONTROL))
            del self._buf[:2]
            return 2

        # ESC followed by anything else: a standalone Escape, then re-examine.
        events.append(Keys.ESCAPE)
        del self._buf[:1]
        return 1

    def _consume_csi(self, events: list[InputEvent]) -> int:
        """Decode a CSI sequence (the buffer starts with ``ESC [``).

        Parameters
        ----------
        events : list
            Output list; the decoded event is appended.

        Returns
        -------
        int
            Bytes consumed, or 0 when the sequence is incomplete.
        """
        size = len(self._buf)
        if size < 3:
            return 0

        third = self._buf[2]

        # Legacy X10/normal mouse: ESC [ M <button> <x> <y>, three raw bytes that
        # may themselves look like controls, so take them by length, not by scan.
        if third == ord("M"):
            if size < 6:
                return 0
            event = self.mouse_parser.parse_normal(bytes(self._buf[:6]))
            if event is not None:
                events.append(event)
            del self._buf[:6]
            return 6

        # SGR mouse: ESC [ < <params> (M|m), terminated by the case of the final.
        if third == ord("<"):
            end = -1
            for index in range(3, size):
                if self._buf[index] in (ord("M"), ord("m")):
                    end = index
                    break
            if end < 0:
                return self._resync_if_overlong()
            event = self.mouse_parser.parse_sgr(bytes(self._buf[: end + 1]))
            if event is not None:
                events.append(event)
            del self._buf[: end + 1]
            return end + 1

        # General CSI: parameter bytes, then intermediates, then a final byte.
        index = 2
        while index < size and 0x30 <= self._buf[index] <= 0x3F:
            index += 1
        while index < size and 0x20 <= self._buf[index] <= 0x2F:
            index += 1
        if index >= size:
            return self._resync_if_overlong()

        final_byte = self._buf[index]
        if not 0x40 <= final_byte <= 0x7E:
            # Malformed: not a valid final byte. Drop the ESC and resynchronize.
            del self._buf[:1]
            return 1

        params = bytes(self._buf[2:index]).decode("ascii", errors="replace")
        final = chr(final_byte)
        length = index + 1

        if final == "~" and params == _PASTE_START_PARAMS:
            del self._buf[:length]
            self._paste = bytearray()
            return length

        events.extend(self._csi_key(params, final))
        del self._buf[:length]
        return length

    def _resync_if_overlong(self) -> int:
        """Drop a leading ESC once an unterminated sequence grows implausible.

        Returns
        -------
        int
            1 if the buffer exceeded :data:`MAX_SEQUENCE` and the ESC was
            dropped, else 0 (keep waiting for the terminator).
        """
        if len(self._buf) > MAX_SEQUENCE:
            logger.debug(
                "Dropping unterminated escape sequence (over %d bytes)", MAX_SEQUENCE
            )
            del self._buf[:1]
            return 1
        return 0

    def _csi_key(self, params: str, final: str) -> list[Key]:
        """Map a parsed CSI sequence to a key.

        Parameters
        ----------
        params : str
            The parameter bytes between ``ESC [`` and the final byte, e.g.
            ``"1;5"`` for Ctrl+<arrow> or ``"3"`` for Delete.
        final : str
            The final byte, e.g. ``"A"`` or ``"~"``.

        Returns
        -------
        list of Key
            A single-element list, or an empty list for sequences we do not map
            (unknown finals are dropped rather than injected as junk keys).
        """
        parts = params.split(";") if params else []

        modifier = 0
        if len(parts) >= 2 and parts[1].isdigit():
            modifier = int(parts[1]) - 1

        base: Key | None
        if final == "~":
            code = int(parts[0]) if parts and parts[0].isdigit() else 0
            base = CSI_TILDE_KEYS.get(code)
        else:
            base = CSI_LETTER_KEYS.get(final)

        if base is None:
            return []
        return [_apply_modifier(base, modifier)]

    def _consume_paste(self, events: list[InputEvent]) -> bool:
        """Accumulate bracketed-paste payload until its terminator arrives.

        Parameters
        ----------
        events : list
            Output list; on completion a single character key carrying the whole
            pasted text is appended.

        Returns
        -------
        bool
            True when the paste terminated (and was emitted), False when it is
            still open and more bytes are needed.

        Notes
        -----
        The payload is emitted as one synthetic character key holding the entire
        text - the same shape Wijjit's local input handler produces for a paste -
        so a paste inserts text rather than firing each character as a hotkey.
        """
        assert self._paste is not None

        found = self._buf.find(_PASTE_END)
        if found < 0:
            # Hold back the last few bytes: the terminator itself may be split
            # across packets, and consuming a prefix of it would lose the paste.
            keep = len(_PASTE_END) - 1
            take = max(0, len(self._buf) - keep)
            if take:
                room = MAX_PASTE_SIZE - len(self._paste)
                if room > 0:
                    self._paste.extend(self._buf[: min(take, room)])
                del self._buf[:take]
            return False

        room = MAX_PASTE_SIZE - len(self._paste)
        if room > 0:
            self._paste.extend(self._buf[: min(found, room)])
        del self._buf[: found + len(_PASTE_END)]

        payload = bytes(self._paste)
        self._paste = None

        if len(payload) >= MAX_PASTE_SIZE:
            logger.warning("Paste truncated at %d bytes", MAX_PASTE_SIZE)

        text = payload.decode("utf-8", errors="replace")
        if text:
            events.append(Key(text, KeyType.CHARACTER, text))
        return True


class ChannelInputSource:
    """Event-loop input handler backed by an SSH channel's byte stream.

    Implements the duck-typed surface Wijjit's event loop expects of an input
    handler, but sourced from :meth:`feed` (called by the SSH session as bytes
    arrive) rather than from a thread polling a tty. Decoded events queue up and
    are handed to the loop one at a time by :meth:`read_input_async`.

    Parameters
    ----------
    writer : TextIO
        Stream that mouse-tracking escape sequences are written to - the SSH
        channel, so the sequences reach the *client's* terminal rather than the
        server's stdout.
    enable_mouse : bool, optional
        Whether the app wants mouse tracking (default False). Tracking is not
        turned on here; the event loop calls :meth:`enable_mouse_tracking`.
    mouse_tracking_mode : MouseTrackingMode, optional
        Tracking granularity to request (default
        :attr:`MouseTrackingMode.BUTTON_EVENT`).

    Attributes
    ----------
    mouse_enabled : bool
        Whether mouse tracking is currently active on the client terminal (i.e.
        the enable sequences have been sent).
    """

    def __init__(
        self,
        writer: TextIO,
        *,
        enable_mouse: bool = False,
        mouse_tracking_mode: MouseTrackingMode | None = None,
    ) -> None:
        self._writer = writer
        self._decoder = KeyDecoder()
        self._queue: asyncio.Queue[InputEvent] = asyncio.Queue()

        self._wants_mouse = enable_mouse
        self.mouse_enabled = False
        self._mouse_tracking_mode = (
            mouse_tracking_mode
            if mouse_tracking_mode is not None
            else MouseTrackingMode.BUTTON_EVENT
        )

        self._escape_timer: asyncio.TimerHandle | None = None
        self._closed = False

    # -- transport side (called by the SSH session) ----------------------------

    def feed(self, data: bytes) -> None:
        """Decode inbound channel bytes and queue the resulting events.

        Parameters
        ----------
        data : bytes
            Raw bytes received on the channel.
        """
        if self._closed:
            return

        for event in self._decoder.feed(data):
            self._queue.put_nowait(event)

        self._schedule_escape_flush()

    def _schedule_escape_flush(self) -> None:
        """(Re)arm the lone-ESC disambiguation timer.

        Any pending timer is cancelled first: if more bytes arrived they may
        have completed the sequence, in which case there is nothing to flush.
        """
        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None

        if not self._decoder.pending_escape():
            return

        loop = asyncio.get_event_loop()
        self._escape_timer = loop.call_later(ESCAPE_TIMEOUT, self._flush_escape)

    def _flush_escape(self) -> None:
        """Fire the Escape key for a bare ESC that no sequence ever completed."""
        self._escape_timer = None
        if self._closed:
            return
        for event in self._decoder.flush():
            self._queue.put_nowait(event)

    # -- event-loop side (the InputHandler surface) ----------------------------

    async def read_input_async(self, timeout: float | None = None) -> InputEvent | None:
        """Wait for the next decoded input event.

        Parameters
        ----------
        timeout : float or None, optional
            Maximum time to wait, in seconds. ``None`` waits indefinitely.

        Returns
        -------
        Key, MouseEvent, or None
            The next event, or ``None`` if the timeout expired first - which the
            event loop treats as a quiet frame (it uses the timeout to drive
            animations and pending re-renders).
        """
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None

    def enable_mouse_tracking(self, mode: MouseTrackingMode | None = None) -> None:
        """Turn on mouse reporting in the client's terminal.

        Parameters
        ----------
        mode : MouseTrackingMode, optional
            Tracking mode to request; defaults to the one given at construction.
        """
        if mode is not None:
            self._mouse_tracking_mode = mode
        if self._mouse_tracking_mode is None:
            return

        self._writer.write(f"\033[?{int(self._mouse_tracking_mode)}h")
        self._writer.write("\033[?1006h")  # SGR extended coordinates
        self._writer.flush()
        self.mouse_enabled = True

    def disable_mouse_tracking(self) -> None:
        """Turn off mouse reporting in the client's terminal."""
        if not self.mouse_enabled or self._mouse_tracking_mode is None:
            return

        self._writer.write(f"\033[?{int(self._mouse_tracking_mode)}l")
        self._writer.write("\033[?1006l")
        self._writer.flush()
        self.mouse_enabled = False

    def restore_terminal(self) -> None:
        """Undo terminal-affecting state on the client.

        For a remote session this is only mouse tracking: there is no local tty
        and no raw mode to leave. Safe to call more than once.
        """
        if self.mouse_enabled:
            try:
                self.disable_mouse_tracking()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Error disabling mouse tracking: %s", exc)

    def close(self) -> None:
        """Release the input source at session teardown.

        Idempotent. Cancels the escape timer and restores the client's terminal.
        The event loop calls this from its ``finally`` block, including when the
        session task is cancelled by a dropped connection.
        """
        if self._closed:
            return
        self._closed = True

        if self._escape_timer is not None:
            self._escape_timer.cancel()
            self._escape_timer = None

        self.restore_terminal()
