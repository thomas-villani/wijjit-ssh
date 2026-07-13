"""Table-driven tests for :class:`wijjit_ssh.input.KeyDecoder`.

The decoder is the hot path for every keystroke of every SSH session and is a
pure function of its byte stream, so it gets exhaustive, fast, I/O-free tests.

Two properties matter most and are tested explicitly:

1. **Correctness** - a given byte sequence produces exactly the events Wijjit's
   local input handler would produce for the same keypress.
2. **Resumability** - feeding the same bytes one at a time, in arbitrary splits,
   produces identical results. TCP does not respect escape-sequence boundaries.
"""

from __future__ import annotations

import pytest
from wijjit.terminal.input import Key, KeyType
from wijjit.terminal.mouse import MouseButton, MouseEvent, MouseEventType

from wijjit_ssh.input import KeyDecoder


def describe(events: list[object]) -> list[object]:
    """Reduce events to comparable descriptors.

    Parameters
    ----------
    events : list
        Events returned by the decoder.

    Returns
    -------
    list
        Key names for keys, ``(type, button, x, y)`` tuples for mouse events.
    """
    out: list[object] = []
    for event in events:
        if isinstance(event, Key):
            out.append(event.name)
        elif isinstance(event, MouseEvent):
            out.append((event.type, event.button, event.x, event.y))
        else:  # pragma: no cover - guard
            raise AssertionError(f"unexpected event: {event!r}")
    return out


def decode(data: bytes) -> list[object]:
    """Decode ``data`` in one shot and return descriptors."""
    return describe(KeyDecoder().feed(data))


def decode_split(data: bytes) -> list[object]:
    """Decode ``data`` one byte at a time and return descriptors.

    This is the resumability check: no matter how the stream is chopped up, the
    decoder must produce the same events.
    """
    decoder = KeyDecoder()
    events: list[object] = []
    for index in range(len(data)):
        events.extend(decoder.feed(data[index : index + 1]))
    return describe(events)


# ---------------------------------------------------------------------------
# Characters and control keys
# ---------------------------------------------------------------------------

CHARACTER_CASES = [
    pytest.param(b"a", ["a"], id="ascii-letter"),
    pytest.param(b"abc", ["a", "b", "c"], id="ascii-run"),
    pytest.param(b"A", ["A"], id="ascii-upper"),
    pytest.param(b"1", ["1"], id="ascii-digit"),
    pytest.param(b" ", ["space"], id="space"),
    pytest.param(b"hi there", ["h", "i", "space", "t", "h", "e", "r", "e"], id="words"),
    # Multi-byte UTF-8 must decode to one character key, not N byte keys.
    pytest.param("é".encode(), ["é"], id="utf8-2-byte"),
    pytest.param("€".encode(), ["€"], id="utf8-3-byte"),
    pytest.param("\U0001f600".encode(), ["\U0001f600"], id="utf8-4-byte"),
]

CONTROL_CASES = [
    pytest.param(b"\r", ["enter"], id="cr"),
    pytest.param(b"\n", ["enter"], id="lf"),
    pytest.param(b"\t", ["tab"], id="tab"),
    pytest.param(b"\x7f", ["backspace"], id="del-as-backspace"),
    pytest.param(b"\x08", ["backspace"], id="bs"),
    pytest.param(b"\x00", ["ctrl+space"], id="nul"),
    pytest.param(b"\x03", ["ctrl+c"], id="ctrl-c"),
    pytest.param(b"\x04", ["ctrl+d"], id="ctrl-d"),
    pytest.param(b"\x1a", ["ctrl+z"], id="ctrl-z"),
    pytest.param(b"\x01", ["ctrl+a"], id="ctrl-a"),
    pytest.param(b"\x11", ["ctrl+q"], id="ctrl-q"),
    pytest.param(b"\x17", ["ctrl+w"], id="ctrl-w"),
]


@pytest.mark.parametrize("data,expected", CHARACTER_CASES + CONTROL_CASES)
def test_characters_and_controls(data: bytes, expected: list[object]) -> None:
    assert decode(data) == expected


# ---------------------------------------------------------------------------
# Escape sequences: arrows, navigation, function keys
# ---------------------------------------------------------------------------

CSI_CASES = [
    pytest.param(b"\x1b[A", ["up"], id="csi-up"),
    pytest.param(b"\x1b[B", ["down"], id="csi-down"),
    pytest.param(b"\x1b[C", ["right"], id="csi-right"),
    pytest.param(b"\x1b[D", ["left"], id="csi-left"),
    pytest.param(b"\x1b[H", ["home"], id="csi-home"),
    pytest.param(b"\x1b[F", ["end"], id="csi-end"),
    pytest.param(b"\x1b[Z", ["shift+tab"], id="csi-backtab"),
    pytest.param(b"\x1b[1~", ["home"], id="tilde-home"),
    pytest.param(b"\x1b[2~", ["insert"], id="tilde-insert"),
    pytest.param(b"\x1b[3~", ["delete"], id="tilde-delete"),
    pytest.param(b"\x1b[4~", ["end"], id="tilde-end"),
    pytest.param(b"\x1b[5~", ["pageup"], id="tilde-pageup"),
    pytest.param(b"\x1b[6~", ["pagedown"], id="tilde-pagedown"),
]

SS3_CASES = [
    pytest.param(b"\x1bOA", ["up"], id="ss3-up"),
    pytest.param(b"\x1bOB", ["down"], id="ss3-down"),
    pytest.param(b"\x1bOC", ["right"], id="ss3-right"),
    pytest.param(b"\x1bOD", ["left"], id="ss3-left"),
    pytest.param(b"\x1bOH", ["home"], id="ss3-home"),
    pytest.param(b"\x1bOF", ["end"], id="ss3-end"),
    pytest.param(b"\x1bOP", ["f1"], id="ss3-f1"),
    pytest.param(b"\x1bOQ", ["f2"], id="ss3-f2"),
    pytest.param(b"\x1bOR", ["f3"], id="ss3-f3"),
    pytest.param(b"\x1bOS", ["f4"], id="ss3-f4"),
]

FUNCTION_KEY_CASES = [
    pytest.param(b"\x1b[11~", ["f1"], id="f1"),
    pytest.param(b"\x1b[12~", ["f2"], id="f2"),
    pytest.param(b"\x1b[13~", ["f3"], id="f3"),
    pytest.param(b"\x1b[14~", ["f4"], id="f4"),
    pytest.param(b"\x1b[15~", ["f5"], id="f5"),
    pytest.param(b"\x1b[17~", ["f6"], id="f6"),
    pytest.param(b"\x1b[18~", ["f7"], id="f7"),
    pytest.param(b"\x1b[19~", ["f8"], id="f8"),
    pytest.param(b"\x1b[20~", ["f9"], id="f9"),
    pytest.param(b"\x1b[21~", ["f10"], id="f10"),
    pytest.param(b"\x1b[23~", ["f11"], id="f11"),
    pytest.param(b"\x1b[24~", ["f12"], id="f12"),
]

MODIFIER_CASES = [
    pytest.param(b"\x1b[1;5A", ["ctrl+up"], id="ctrl-up"),
    pytest.param(b"\x1b[1;5B", ["ctrl+down"], id="ctrl-down"),
    pytest.param(b"\x1b[1;5C", ["ctrl+right"], id="ctrl-right"),
    pytest.param(b"\x1b[1;5D", ["ctrl+left"], id="ctrl-left"),
    pytest.param(b"\x1b[1;2A", ["shift+up"], id="shift-up"),
    pytest.param(b"\x1b[1;2C", ["shift+right"], id="shift-right"),
    pytest.param(b"\x1b[1;3B", ["alt+down"], id="alt-down"),
    pytest.param(b"\x1b[1;6D", ["ctrl+shift+left"], id="ctrl-shift-left"),
    pytest.param(b"\x1b[1;7A", ["ctrl+alt+up"], id="ctrl-alt-up"),
    pytest.param(b"\x1b[3;5~", ["ctrl+delete"], id="ctrl-delete"),
    pytest.param(b"\x1b[1;5H", ["ctrl+home"], id="ctrl-home"),
    pytest.param(b"\x1b[5;5~", ["ctrl+pageup"], id="ctrl-pageup"),
]

ALT_CASES = [
    pytest.param(b"\x1bb", ["alt+b"], id="alt-b"),
    pytest.param(b"\x1bx", ["alt+x"], id="alt-x"),
    # Alt+Shift+letter still reports the lowercase letter, as Wijjit does locally.
    pytest.param(b"\x1bB", ["alt+b"], id="alt-upper-b"),
]


@pytest.mark.parametrize(
    "data,expected",
    CSI_CASES + SS3_CASES + FUNCTION_KEY_CASES + MODIFIER_CASES + ALT_CASES,
)
def test_escape_sequences(data: bytes, expected: list[object]) -> None:
    assert decode(data) == expected


# ---------------------------------------------------------------------------
# Resumability: every case must survive being fed one byte at a time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    CHARACTER_CASES
    + CONTROL_CASES
    + CSI_CASES
    + SS3_CASES
    + FUNCTION_KEY_CASES
    + MODIFIER_CASES,
)
def test_split_one_byte_at_a_time(data: bytes, expected: list[object]) -> None:
    """A sequence chopped across packets decodes identically.

    Alt cases are excluded: a lone ESC followed later by a letter is genuinely
    ambiguous and is resolved by the ESC timer, which is tested separately.
    """
    assert decode_split(data) == expected


def test_split_utf8_across_feeds() -> None:
    """A multi-byte rune split across packets emits one key, not garbage."""
    decoder = KeyDecoder()
    encoded = "é".encode()  # 2 bytes

    assert decoder.feed(encoded[:1]) == []  # incomplete: nothing yet
    assert describe(decoder.feed(encoded[1:])) == ["é"]


def test_split_emoji_across_three_feeds() -> None:
    decoder = KeyDecoder()
    encoded = "\U0001f600".encode()  # 4 bytes

    assert decoder.feed(encoded[:1]) == []
    assert decoder.feed(encoded[1:3]) == []
    assert describe(decoder.feed(encoded[3:])) == ["\U0001f600"]


def test_burst_of_mixed_input() -> None:
    """A single packet holding several distinct events decodes in order."""
    data = b"a\x1b[Ab\r\x1b[3~"
    assert decode(data) == ["a", "up", "b", "enter", "delete"]


# ---------------------------------------------------------------------------
# The lone-ESC ambiguity
# ---------------------------------------------------------------------------


def test_lone_escape_is_pending_not_emitted() -> None:
    """A bare ESC is withheld: it may be the head of a sequence still in flight."""
    decoder = KeyDecoder()

    assert decoder.feed(b"\x1b") == []
    assert decoder.pending_escape() is True


def test_lone_escape_resolves_on_flush() -> None:
    """With no follow-up bytes, the timer's flush turns it into the Escape key."""
    decoder = KeyDecoder()
    decoder.feed(b"\x1b")

    assert describe(decoder.flush()) == ["escape"]
    assert decoder.pending_escape() is False


def test_escape_completed_by_later_bytes_is_not_an_escape_key() -> None:
    """ESC arriving alone, then '[A', is Up - never Escape followed by junk."""
    decoder = KeyDecoder()

    assert decoder.feed(b"\x1b") == []
    assert decoder.pending_escape() is True

    assert describe(decoder.feed(b"[A")) == ["up"]
    assert decoder.pending_escape() is False
    assert decoder.flush() == []  # nothing left to resolve


def test_partial_csi_is_not_pending_escape() -> None:
    """'ESC [' with no final byte waits for more; flushing it must not fire Escape.

    Flushing here would corrupt a real arrow key whose tail is one packet behind.
    """
    decoder = KeyDecoder()

    assert decoder.feed(b"\x1b[") == []
    assert decoder.pending_escape() is False
    assert decoder.flush() == []

    assert describe(decoder.feed(b"A")) == ["up"]


def test_double_escape_emits_one_escape_and_holds_the_second() -> None:
    decoder = KeyDecoder()

    assert describe(decoder.feed(b"\x1b\x1b")) == ["escape"]
    assert decoder.pending_escape() is True
    assert describe(decoder.flush()) == ["escape"]


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------


def test_sgr_mouse_press() -> None:
    # ESC [ < 0 ; 10 ; 5 M -> left press; coordinates are 1-based on the wire.
    assert decode(b"\x1b[<0;10;5M") == [(MouseEventType.PRESS, MouseButton.LEFT, 9, 4)]


def test_sgr_mouse_press_release_synthesizes_click() -> None:
    decoder = KeyDecoder()

    press = describe(decoder.feed(b"\x1b[<0;10;5M"))
    release = describe(decoder.feed(b"\x1b[<0;10;5m"))

    assert press == [(MouseEventType.PRESS, MouseButton.LEFT, 9, 4)]
    # The parser turns a press/release pair at the same spot into a CLICK - and
    # the decoder must share one parser across feeds for that state to survive.
    assert release == [(MouseEventType.CLICK, MouseButton.LEFT, 9, 4)]


def test_sgr_mouse_scroll() -> None:
    assert decode(b"\x1b[<64;3;7M") == [
        (MouseEventType.SCROLL, MouseButton.SCROLL_UP, 2, 6)
    ]


def test_sgr_mouse_right_button() -> None:
    assert decode(b"\x1b[<2;1;1M") == [(MouseEventType.PRESS, MouseButton.RIGHT, 0, 0)]


def test_sgr_mouse_split_across_feeds() -> None:
    assert decode_split(b"\x1b[<0;10;5M") == [
        (MouseEventType.PRESS, MouseButton.LEFT, 9, 4)
    ]


def test_legacy_x10_mouse() -> None:
    # ESC [ M <button+32> <x+33> <y+33>
    data = b"\x1b[M" + bytes([32, 33 + 9, 33 + 4])
    assert decode(data) == [(MouseEventType.PRESS, MouseButton.LEFT, 9, 4)]


def test_legacy_x10_mouse_split_across_feeds() -> None:
    data = b"\x1b[M" + bytes([32, 33 + 9, 33 + 4])
    assert decode_split(data) == [(MouseEventType.PRESS, MouseButton.LEFT, 9, 4)]


def test_mouse_followed_by_key_in_one_packet() -> None:
    events = decode(b"\x1b[<0;10;5Mq")
    assert events == [(MouseEventType.PRESS, MouseButton.LEFT, 9, 4), "q"]


# ---------------------------------------------------------------------------
# Bracketed paste
# ---------------------------------------------------------------------------


def test_bracketed_paste_emits_one_character_key() -> None:
    """Paste is one synthetic key holding all the text, matching local Wijjit."""
    events = KeyDecoder().feed(b"\x1b[200~hello\x1b[201~")

    assert len(events) == 1
    key = events[0]
    assert isinstance(key, Key)
    assert key.key_type == KeyType.CHARACTER
    assert key.char == "hello"


def test_bracketed_paste_does_not_fire_hotkeys() -> None:
    """Control bytes inside a paste stay literal text - that is the whole point.

    A pasted newline must not submit the form, and a pasted ESC must not close
    the dialog.
    """
    events = KeyDecoder().feed(b"\x1b[200~a\rb\x1b[201~")

    assert len(events) == 1
    key = events[0]
    assert isinstance(key, Key)
    assert key.char == "a\rb"


def test_bracketed_paste_split_across_feeds() -> None:
    decoder = KeyDecoder()

    assert decoder.feed(b"\x1b[200~hel") == []
    assert decoder.feed(b"lo wor") == []
    events = decoder.feed(b"ld\x1b[201~")

    assert len(events) == 1
    assert isinstance(events[0], Key)
    assert events[0].char == "hello world"


def test_bracketed_paste_terminator_split_across_feeds() -> None:
    """The terminator itself may be chopped; a prefix of it must not be lost."""
    decoder = KeyDecoder()

    assert decoder.feed(b"\x1b[200~hi\x1b[2") == []
    events = decoder.feed(b"01~")

    assert len(events) == 1
    assert isinstance(events[0], Key)
    assert events[0].char == "hi"


def test_keys_after_paste_decode_normally() -> None:
    events = KeyDecoder().feed(b"\x1b[200~hi\x1b[201~\r")
    assert describe(events) == ["hi", "enter"]


# ---------------------------------------------------------------------------
# Malformed and hostile input
# ---------------------------------------------------------------------------


def test_unknown_csi_is_dropped_not_injected_as_junk() -> None:
    """An unmapped sequence (here a device-attributes reply) yields no keys."""
    assert decode(b"\x1b[?1;2c") == []


def test_unknown_csi_does_not_swallow_following_keys() -> None:
    assert decode(b"\x1b[?1;2ca") == ["a"]


def test_invalid_utf8_becomes_replacement_not_an_exception() -> None:
    """A hostile client must not be able to kill a session with a bad byte."""
    events = decode(b"\xff\xfe")
    assert all(isinstance(name, str) for name in events)


def test_unterminated_sequence_resyncs_instead_of_buffering_forever() -> None:
    """A never-terminated CSI is eventually abandoned so the stream recovers."""
    decoder = KeyDecoder()
    decoder.feed(b"\x1b[" + b"1" * 100)

    # The wedged sequence is dropped; subsequent real keys still decode.
    events = describe(decoder.feed(b"\ra"))
    assert "enter" in events


def test_empty_feed_is_a_no_op() -> None:
    decoder = KeyDecoder()
    assert decoder.feed(b"") == []
    assert decoder.pending_escape() is False
