Terminal input
==============

You will probably never touch this module. It is documented because it is the
part of wijjit-ssh that had to be rewritten from scratch rather than adapted, and
because it is reusable for byte transports that are not SSH at all.

Why not reuse Wijjit's input handler
------------------------------------

A local Wijjit app reads the keyboard through
``wijjit.terminal.input.InputHandler``, which polls ``prompt_toolkit`` on a
background thread. That is fine for one foreground app and wrong at server scale:
it costs an OS thread and a ``prompt_toolkit`` pipe *per connection* - when the
bytes are already being delivered to us on the event loop by ``asyncssh``.

So remote sessions get their own path. The channel is opened in **binary mode**
(``encoding=None``), so the decoder sees exactly what the client sent, byte for
byte, with no intermediate text decoding to lose information.

Two pieces
----------

:class:`~wijjit_ssh.input.KeyDecoder`
   A pure, resumable state machine turning raw terminal bytes into Wijjit
   ``Key`` and ``MouseEvent`` objects. No I/O, no threads, no clock - which is
   what makes it exhaustively unit-testable.

:class:`~wijjit_ssh.input.ChannelInputSource`
   The event loop's input handler for one session. It owns a decoder, pushes
   decoded events onto an :class:`asyncio.Queue`, and satisfies the duck-typed
   surface the loop calls: ``read_input_async``, ``mouse_enabled``,
   ``enable_mouse_tracking`` / ``disable_mouse_tracking``, ``close``, and
   ``restore_terminal``.

.. code-block:: text

   asyncssh channel ──bytes──▶ ChannelInputSource.feed
                                    │
                                    ▼
                               KeyDecoder  ──▶  Key / MouseEvent
                                    │
                                    ▼
                              asyncio.Queue  ──▶ read_input_async ──▶ Wijjit loop

What it handles
---------------

The decoder buffers incomplete trailing sequences across calls, which is the
whole reason it is a state machine rather than a lookup table. A keystroke does
not arrive as a keystroke - it arrives as whatever fits in a TCP segment.

* **Split escape sequences.** ``ESC [`` in one packet and ``A`` in the next
  still decodes as Up.
* **Split UTF-8 runes.** A multi-byte character cut mid-sequence is held until
  it is complete.
* **Mouse**, both SGR (``ESC [ < ...``) and the legacy X10 encoding.
* **Bracketed paste**, up to a size cap, so a large paste cannot be used to make
  the server allocate without bound.
* **Modifier combinations** - Ctrl, Alt, and Shift on the sequences that encode
  them.

The lone-ESC ambiguity
----------------------

There is exactly one place a timer is unavoidable. A bare ``ESC`` byte is either
the Escape key or the first byte of a sequence still in flight, and **nothing in
the byte stream distinguishes them**. Every terminal program resolves this the
same way: wait a moment, and if nothing follows, call it Escape.

:class:`~wijjit_ssh.input.ChannelInputSource` schedules
:meth:`~wijjit_ssh.input.KeyDecoder.flush` ``ESCAPE_TIMEOUT`` (50 ms) after the
last byte. In practice this rarely fires: SSH almost always delivers a full
sequence in one packet, so the ``ESC`` and the rest of the sequence arrive
together and the timer is cancelled before it runs.

Using it elsewhere
------------------

Both classes are exported from the package root, and neither imports
``asyncssh``. If you want to run a Wijjit app over telnet, a websocket, or a
local PTY, this is the piece you keep:

.. code-block:: python

   from wijjit_ssh import ChannelInputSource

   source = ChannelInputSource(writer)   # anything with .write()/.flush()
   ...
   source.feed(raw_bytes)                # whenever bytes arrive
   event = await source.read_input_async()

``writer`` is where mouse-tracking escape sequences are sent, so it must be the
stream that reaches the *client's* terminal - not the server's stdout.

For testing, :class:`~wijjit_ssh.input.KeyDecoder` alone is usually what you
want. It is synchronous and pure, so you can feed it bytes and assert on events
with no event loop at all:

.. code-block:: python

   >>> from wijjit_ssh import KeyDecoder
   >>> decoder = KeyDecoder()
   >>> decoder.feed(b"\x1b[")     # incomplete - nothing yet
   []
   >>> decoder.feed(b"A")         # ...and now the rest
   [Key(name='up', key_type=<KeyType.SPECIAL: 2>, char=None)]
