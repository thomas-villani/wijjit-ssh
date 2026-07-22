Chat room
=========

`examples/chat_ssh.py
<https://github.com/thomas-villani/wijjit-ssh/blob/main/examples/chat_ssh.py>`_

A terminal chat room with a scrolling transcript, join and leave notices, and a
handful of slash commands - and no user accounts at all.

.. code-block:: bash

   uv run python examples/chat_ssh.py    # :8023

.. code-block:: bash

   ssh -p 8023 alice@localhost           # one terminal
   ssh -p 8023 bob@localhost             # another

Alice types a line and it appears in Bob's window without Bob touching a key.

No user accounts, deliberately
------------------------------

There is no registration step, no nickname collision to resolve, and no password
to store, because SSH authenticated everyone before the app was ever built. The
display name is ``session.username``, and under
:class:`~wijjit_ssh.auth.AuthorizedKeys` a user's identity *is* their SSH key.

An app served this way inherits an account system it did not have to write - and
one with better properties than most chat rooms ship with, since there is no
credential in the app to leak.

That also makes running it anonymously worse than it is for ``hello_ssh.py``.
With no auth policy, anyone can connect as anyone; in a chat room that is
impersonation, not merely unauthenticated access. The example says so, loudly,
when it falls back:

.. code-block:: text

   WARNING: no ~/.ssh/authorized_keys found - running with NO AUTHENTICATION.
            In a chat room that is worse than it sounds: usernames come
            straight from SSH, so with no auth policy anyone can connect
            as anyone. Fine on localhost; never on a real network.

One transcript, no copies
-------------------------

The room owns the conversation and every view reads it directly, so there is
exactly one transcript in the process no matter how many people are in it:

.. code-block:: python

   @dataclass
   class ChatRoom:
       transcript: deque[str] = field(
           default_factory=lambda: deque(maxlen=HISTORY_LIMIT),
       )
       subscribers: dict[str, Subscriber] = field(default_factory=dict)

       def post(self, line):
           self.transcript.append(f"{_stamp()} {line}")
           self.broadcast()

       def broadcast(self):
           for subscriber in self.subscribers.values():
               subscriber.app.refresh()

Because nothing is copied, ``broadcast`` has nothing to distribute - by the time
it runs, every window's *data* is already correct and only its *pixels* are
stale. All it does is ask each app to look again. See :doc:`index` for what
``refresh()`` costs and how fast it lands.

The ``deque`` bound matters more than it looks. A chat room that stays up for a
week must not grow a list for a week; ``maxlen`` drops the oldest line instead.

Private replies go somewhere else
---------------------------------

``/who`` answers one person. Appending that answer to the transcript would be
wrong twice over - everybody else would see one user's ``/who``, and the
transcript belongs to the room, so a private line written into it would either
leak or be lost on the next post.

Per-user replies go to ``state["notice"]``, which the view renders in the status
line. That state *is* the app's own, which makes it exactly the right place for
something only this user should see:

.. code-block:: python

   elif command == "who":
       app.state["notice"] = f"Here now: {', '.join(room.usernames) or 'nobody'}"

Two things the layout has to get right
--------------------------------------

**Focus.** Wijjit starts with nothing focused on purpose, so that focus does not
appear to "skip" the first element before its bounds are known - Tab is what
normally selects the first field. A chat room wants the message box live from the
start, and ``focus_element_by_id`` only resolves once the element has actually
rendered, so the view retries until it lands:

.. code-block:: python

   focused = False

   @app.view("main", default=True)
   def main():
       nonlocal focused
       if not focused:
           focused = app.focus_element_by_id("message")

**Size.** Every client is a different shape and any of them may resize
mid-conversation, so the view reads
:func:`~wijjit.terminal.size.get_terminal_size` at render time rather than
trusting ``session.columns`` from connect time. That function reads this task's
size override, so concurrent sessions each see their own dimensions.

One consequence is worth knowing before you debug it: a status line that *wraps*
costs a row the layout has already spent, and the frame answers by growing a
scrollbar over everything. The example truncates its status line to the terminal
width for exactly that reason.

Trying it properly
------------------

The interesting test is not two people chatting - it is the rude disconnect.
Close Bob's terminal window rather than pressing Ctrl+Q, and Alice should still
see ``* bob left``. That is the ``connection_lost`` path into
:ref:`on_event <examples-unsubscribing>`, and it is the case a hand-rolled
subscriber list gets wrong.
