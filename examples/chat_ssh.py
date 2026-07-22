"""A multi-user chat room served over SSH.

Run it::

    uv run python examples/chat_ssh.py            # starts the server on :8023

Then from two different terminals::

    ssh -p 8023 alice@localhost
    ssh -p 8023 bob@localhost

Alice types a line and it appears in Bob's window without Bob touching a key.

Why this example exists
-----------------------

``hello_ssh.py`` proves the transport: one client, one app, keystrokes in and
frames out. Everything it shows, a local Wijjit app shows too. This example
shows the thing that is only true over SSH - **many live apps in one process,
sharing state, pushed to from outside their own event loop**.

The whole design is three pieces:

1. A :class:`ChatRoom` created once at module scope, holding the transcript and
   a map of live subscribers.
2. :func:`make_app`, which builds one app per connection, registers it with the
   room, and reads the room's transcript in its view.
3. :func:`on_server_event`, a single hook passed to ``WijjitSSH(on_event=...)``,
   which removes a subscriber when its session ends.

Pushing to a session from outside its own task
----------------------------------------------

Every session is its own asyncio task, parked in ``read_input_async(timeout=...)``
waiting for its user to type. A message posted by *another* session therefore has
to reach an app that is currently blocked, from a task that does not own it.

There is no copying involved: every view renders the room's transcript directly,
so the shared state is already correct the moment it changes. All the poster has
to do is get the other apps to look again, and ``app.refresh()`` does that from
any task - it sets a flag the target's loop checks when its input read times out.

That timeout is the push latency: ``REFRESH_INTERVAL / 2`` when that config value
is set, and 0.5s when it is not. This app sets ``REFRESH_INTERVAL=0.2``, so a
message lands in every other window within about 100 ms, which reads as instant.
The cost is that every session redraws five times a second whether or not
anything changed - nothing at all for a chat room, something to measure at 500
concurrent sessions. The dial is right there.

No user accounts, deliberately
------------------------------

There is no registration, no nickname collision, and no password to store,
because SSH already authenticated everyone before the app was built. The display
name is ``session.username``, and with ``AuthorizedKeys`` the user's identity is
their SSH key. An app served this way inherits an account system it did not have
to write.

That is also why running this anonymously is worse than it is for ``hello_ssh``:
with no auth policy, anyone can claim to be anyone. In a chat room that is
impersonation, not merely unauthenticated access.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from wijjit import ANSIColor, Wijjit, colorize, render_template_string
from wijjit.terminal.size import get_terminal_size

from wijjit_ssh import AuthorizedKeys, SSHSession, WijjitSSH, ensure_host_key

PORT = 8023

#: How many transcript lines the room keeps. A chat room that runs for a week
#: must not grow a list for a week; the deque drops the oldest line instead.
HISTORY_LIMIT = 500

#: Seconds between the event loop's checks for work pushed in from outside the
#: session's own task. See the module docstring - this is the push latency knob.
REFRESH_INTERVAL = 0.2

HELP = (
    "Commands: /who - who's here | /me <action> - emote | "
    "/help - this | /quit - leave (or Ctrl+Q)"
)


def _stamp() -> str:
    """Return the current wall-clock time as ``HH:MM``."""
    return datetime.now().strftime("%H:%M")


@dataclass
class Subscriber:
    """One connected client: who they are, and the app drawing their window.

    Attributes
    ----------
    username : str
        Name the client authenticated as; their display name in the room.
    app : Wijjit
        That client's live app. Held so the room can call
        :meth:`~wijjit.Wijjit.refresh` on it from another session's task.
    """

    username: str
    app: Wijjit


@dataclass
class ChatRoom:
    """The shared state every session reads and writes.

    One instance, module-scope, shared by every connection - which is the point
    of the example. Nobody holds a copy: each session's view renders
    :attr:`transcript` directly, so "broadcasting" is only a matter of telling
    the other apps to look again.

    Attributes
    ----------
    transcript : collections.deque of str
        The last :data:`HISTORY_LIMIT` lines, oldest first.
    subscribers : dict of str to Subscriber
        Live sessions, keyed by ``SSHSession.session_id``.
    """

    transcript: deque[str] = field(
        default_factory=lambda: deque(maxlen=HISTORY_LIMIT),
    )
    subscribers: dict[str, Subscriber] = field(default_factory=dict)

    @property
    def usernames(self) -> list[str]:
        """Names of everyone currently connected, in join order."""
        return [sub.username for sub in self.subscribers.values()]

    def join(self, session_id: str, username: str, app: Wijjit) -> None:
        """Register a session's app so the room can push to it.

        Parameters
        ----------
        session_id : str
            ``SSHSession.session_id``, the key :meth:`leave` will use.
        username : str
            The authenticated username.
        app : Wijjit
            The app rendering this session's window.
        """
        self.subscribers[session_id] = Subscriber(username=username, app=app)
        self.post(f"* {username} joined ({len(self.subscribers)} here)")

    def leave(self, session_id: str) -> None:
        """Drop a session and announce it. Safe to call for an unknown id.

        Parameters
        ----------
        session_id : str
            The id passed to :meth:`join`.
        """
        subscriber = self.subscribers.pop(session_id, None)
        if subscriber is None:
            return
        self.post(f"* {subscriber.username} left ({len(self.subscribers)} here)")

    def post(self, line: str) -> None:
        """Append one line to the transcript and push it to every window.

        Parameters
        ----------
        line : str
            An already-formatted transcript line, without a timestamp.
        """
        self.transcript.append(f"{_stamp()} {line}")
        self.broadcast()

    def broadcast(self) -> None:
        """Ask every subscriber's app to redraw.

        This runs on whichever session's task posted, so for all but one
        subscriber it is poking an app owned by a *different* task - one that is
        currently parked in ``read_input_async`` waiting for its own user to
        type. ``refresh()`` sets a flag that app's loop checks when that read
        times out, which is why the module docstring cares about
        ``REFRESH_INTERVAL``.

        Nothing is copied: every view reads :attr:`transcript` directly.
        """
        for subscriber in self.subscribers.values():
            subscriber.app.refresh()


room = ChatRoom()
room.transcript.append(f"{_stamp()} * Welcome. {HELP}")


SPEAKER_COLORS = {
    "*": ANSIColor.BRIGHT_BLACK,
}


def colorize_line(line: str) -> str:
    """Color a transcript line for display.

    The stored transcript stays plain text; color is applied at render time so
    the room's state is never full of escape sequences. ``colorize`` honors
    ``NO_COLOR``, so this degrades gracefully.

    Parameters
    ----------
    line : str
        A stored transcript line, e.g. ``"14:03 <alice> hi"``.

    Returns
    -------
    str
        The line with its timestamp dimmed, and system notices dimmed whole.
    """
    stamp, _, rest = line.partition(" ")
    dim_stamp = colorize(stamp, color=ANSIColor.BRIGHT_BLACK)

    if rest.startswith("*"):
        return f"{dim_stamp} {colorize(rest, color=ANSIColor.BRIGHT_BLACK)}"
    if rest.startswith("<"):
        name, sep, text = rest.partition(">")
        if sep:
            speaker = colorize(f"{name}>", color=ANSIColor.BRIGHT_CYAN, bold=True)
            return f"{dim_stamp} {speaker}{text}"
    return f"{dim_stamp} {rest}"


TEMPLATE = """
{% frame border="rounded" title=title width="fill" height="fill" %}
  {% vstack spacing=1 padding=1 %}

    {% logview id="transcript"
        lines=lines
        width=log_width
        height=log_height
        auto_scroll=True
        soft_wrap=True
        detect_log_levels=False
        bind=False
        tab_index=1
        border="single" %}
    {% endlogview %}

    {% hstack spacing=1 %}
      {% textinput id="message" placeholder="Say something..." width=input_width
                   action="send" tab_index=0 %}{% endtextinput %}
      {% button action="send" tab_index=2 %}Send{% endbutton %}
    {% endhstack %}

    {% text %}{{ status }}{% endtext %}

  {% endvstack %}
{% endframe %}
"""


def make_app(session: SSHSession) -> Wijjit:
    """Build one chat window per SSH connection.

    Parameters
    ----------
    session : SSHSession
        The connection context. ``session.backend`` routes this app's I/O to the
        SSH channel; ``session.username`` is the name the client authenticated
        as, and becomes their display name in the room.

    Returns
    -------
    Wijjit
        The app for this connection, already registered with the room.
    """
    app = Wijjit(
        backend=session.backend,
        # No transcript here: the view reads the room's, so there is exactly one
        # copy of the conversation in the process. `message` is bound to the
        # text input by id; `notice` is this user's private reply line.
        initial_state={"message": "", "notice": ""},
        REFRESH_INTERVAL=REFRESH_INTERVAL,
    )

    # Wijjit deliberately starts with nothing focused, so that focus does not
    # appear to "skip" the first element before bounds are known - Tab is what
    # normally picks the first field. A chat room wants the message box live
    # from the start, and the id only resolves once the element has actually
    # been rendered, so this retries until it lands (in practice, render two).
    focused = False

    @app.view("main", default=True)
    def main() -> str:
        nonlocal focused
        if not focused:
            focused = app.focus_element_by_id("message")

        # Read the size live rather than from `session`: every client is a
        # different shape, and any of them may resize mid-conversation.
        # get_terminal_size() reads this task's size override, so concurrent
        # sessions each see their own dimensions here.
        size = get_terminal_size()
        notice = str(app.state.get("notice", ""))
        status = notice or (
            f"{len(room.subscribers)} here | /help for commands | Ctrl+Q to leave"
        )
        return render_template_string(
            TEMPLATE,
            title=f"#general - you are {session.username}",
            # Rendered straight from the room, so a message posted by another
            # session is on screen the moment this app redraws.
            lines=[colorize_line(line) for line in room.transcript],
            # Truncated, and one line on purpose: a status line that wraps costs
            # a row the layout below has already spent, and the frame answers by
            # growing a scrollbar over everything.
            status=status[: max(10, size.columns - 4)],
            # The logview's width/height are its *content*, so its own border
            # costs two more on each axis. Horizontally that is the frame
            # border (2), the vstack padding (2), and the logview border (2);
            # vertically, those six plus two spacing rows, the input, and the
            # status line.
            log_width=max(20, size.columns - 7),
            log_height=max(3, size.lines - 10),
            input_width=max(10, size.columns - 17),
        )

    @app.on_action("send")
    def send(event: object = None) -> None:
        text = str(app.state.get("message", "")).strip()
        app.state["message"] = ""
        app.state["notice"] = ""
        if not text:
            return
        if text.startswith("/"):
            handle_command(app, session, text)
            return
        room.post(f"<{session.username}> {text}")

    # Registering here, in the factory, is safe: the room only ever pushes from
    # a task on this same event loop, and this runs on it. Note that we do NOT
    # unsubscribe here - see on_server_event below for why the teardown side
    # cannot live in the app.
    room.join(session.session_id, session.username, app)
    return app


def handle_command(app: Wijjit, session: SSHSession, text: str) -> None:
    """Run a slash command.

    Replies that concern only the person who typed the command go to
    ``state["notice"]``, which the view shows in the status line. Putting them
    in the transcript instead would be wrong twice over: everyone else would see
    one user's ``/who``, and the transcript is the room's, so a private line
    appended to it would either leak or be lost on the next post.

    Parameters
    ----------
    app : Wijjit
        The app that issued the command, for replies only that user sees.
    session : SSHSession
        The issuing session, for the username.
    text : str
        The raw input, including the leading ``/``.
    """
    command, _, argument = text[1:].partition(" ")
    command = command.lower()

    if command == "quit":
        app.quit()
    elif command == "who":
        app.state["notice"] = f"Here now: {', '.join(room.usernames) or 'nobody'}"
    elif command == "me":
        action = argument.strip()
        if action:
            room.post(f"* {session.username} {action}")
        else:
            app.state["notice"] = "Usage: /me <action>, e.g. /me waves"
    elif command == "help":
        app.state["notice"] = HELP
    else:
        app.state["notice"] = f"Unknown command /{command} - try /help"


def on_server_event(event: str, fields: Mapping[str, object]) -> None:
    """Drop a subscriber when its session ends.

    ``on_event`` is advertised as a metrics hook, and it is the right tool here
    for a structural reason: the app factory has no teardown callback, and there
    is no reliable in-app signal either. Checking ``app.running`` races - the
    server calls the factory *before* starting the app's task, so a broadcast
    landing in that window would evict a session that had not started yet.

    ``session.ended`` has no such gap. It fires on every way out - the user quit,
    the idle timeout expired, the TCP connection died, or ``stop()`` drained the
    server - and it carries the same ``session_id`` the factory registered under.

    Parameters
    ----------
    event : str
        Event name, e.g. ``"session.ended"``.
    fields : Mapping[str, object]
        Event fields. ``session.ended`` carries ``session_id``, ``username``,
        ``peer_ip``, ``reason``, and ``duration``.
    """
    if event == "session.ended":
        session_id = fields.get("session_id")
        if isinstance(session_id, str):
            room.leave(session_id)


def build_server() -> WijjitSSH:
    """Prefer public-key auth; fall back to open auth so the demo always runs."""
    host_key = ensure_host_key("ssh_host_key")
    authorized_keys = Path.home() / ".ssh" / "authorized_keys"

    if authorized_keys.is_file():
        print(f"Public-key auth against {authorized_keys}")
        return WijjitSSH(
            make_app,
            host_keys=[host_key],
            auth=AuthorizedKeys(authorized_keys),
            on_event=on_server_event,
        )

    print(
        f"WARNING: no {authorized_keys} found - running with NO AUTHENTICATION.\n"
        "         In a chat room that is worse than it sounds: usernames come\n"
        "         straight from SSH, so with no auth policy anyone can connect\n"
        "         as anyone. Fine on localhost; never on a real network."
    )
    return WijjitSSH(
        make_app,
        host_keys=[host_key],
        allow_anonymous=True,
        on_event=on_server_event,
    )


if __name__ == "__main__":
    server = build_server()
    print(f"Chat room listening on port {PORT} (ssh -p {PORT} you@localhost)")
    server.run(port=PORT)
