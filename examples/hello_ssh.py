"""A minimal Wijjit-over-SSH app.

Run it::

    uv run python examples/hello_ssh.py           # starts the server on :8022

Then from another terminal::

    ssh -p 8022 yourname@localhost

You land straight in a live Wijjit TUI - a text field and a counter - served
over SSH. Every connection gets its own independent app instance and state.

Host key: :func:`~wijjit_ssh.keys.ensure_host_key` generates ``ssh_host_key`` on
first run and reuses it afterwards, so no ``ssh-keygen`` step is needed and your
client's ``known_hosts`` entry stays valid across restarts. In production you
would generate the key out of band and load it with ``load_host_keys`` instead.

Authentication: if you have a ``~/.ssh/authorized_keys``, this serves public-key
auth against it (the recommended setup). If you do not, it falls back to *no
authentication* so the demo still runs - and says so loudly. wijjit-ssh will not
run unauthenticated unless you ask for it in as many words, which is why the
fallback has to pass ``allow_anonymous=True``.

Bind address: the unauthenticated fallback listens on **loopback only**, because
"anyone who can reach the port gets a session" is a very different sentence when
the port is on every interface of a laptop on someone else's wifi. Set
``WIJJIT_SSH_HOST`` to widen it - ``deploy/Dockerfile`` sets ``0.0.0.0``, since a
container's published port cannot reach a loopback bind.
"""

from __future__ import annotations

import os
from pathlib import Path

from wijjit import Wijjit, render_template_string

from wijjit_ssh import AuthorizedKeys, SSHSession, WijjitSSH, ensure_host_key

TEMPLATE = """
{% frame title="Wijjit over SSH" %}
  {% vstack spacing=1 %}
    {% text %}Hello, {{ username }}! Your terminal is {{ cols }}x{{ rows }}.{% endtext %}
    {% text %}Type your name, then press the button:{% endtext %}
    {% textinput id="name" placeholder="name..." %}{% endtextinput %}
    {% text %}Greeted {{ count }} time(s).{% endtext %}
    {% button action="greet" %}Greet{% endbutton %}
    {% text %}Ctrl+Q to disconnect.{% endtext %}
  {% endvstack %}
{% endframe %}
"""


def make_app(session: SSHSession) -> Wijjit:
    """Build one app per SSH connection (the SSH analogue of a Flask view)."""
    # Pass the session's backend into the app - this is what routes I/O to the
    # SSH channel instead of the server's own console.
    app = Wijjit(backend=session.backend, initial_state={"count": 0})

    @app.view("main", default=True)
    def main():
        return render_template_string(
            TEMPLATE,
            username=session.username,
            cols=session.columns,
            rows=session.lines,
            count=app.state["count"],
        )

    # Action handlers are always called with the ActionEvent, so the parameter
    # is not optional even when the handler ignores it.
    @app.on_action("greet")
    def greet(event=None):
        app.state["count"] += 1

    return app


def bind_host(*, authenticated: bool) -> str:
    """Where to listen: loopback unless authenticated, or unless told otherwise.

    ``""`` means every interface, which is the right default for a server that
    checks credentials and the wrong one for the fallback below.
    """
    override = os.environ.get("WIJJIT_SSH_HOST")
    if override is not None:
        return override
    return "" if authenticated else "127.0.0.1"


def build_server() -> WijjitSSH:
    """Prefer public-key auth; fall back to open auth so the demo always runs."""
    # Generated on first run, reused after. Production would manage this out of
    # band and use load_host_keys().
    host_key = ensure_host_key("ssh_host_key")
    authorized_keys = Path.home() / ".ssh" / "authorized_keys"

    if authorized_keys.is_file():
        print(f"Public-key auth against {authorized_keys}")
        return WijjitSSH(
            make_app,
            host_keys=[host_key],
            auth=AuthorizedKeys(authorized_keys),
            host=bind_host(authenticated=True),
        )

    host = bind_host(authenticated=False)
    print(
        f"WARNING: no {authorized_keys} found - running with NO AUTHENTICATION.\n"
        f"         Anyone who can reach this port can connect as any username.\n"
        f"         Listening on {host or 'ALL interfaces'}; set WIJJIT_SSH_HOST to\n"
        f"         change that, and never widen it on an untrusted network."
    )
    return WijjitSSH(make_app, host_keys=[host_key], allow_anonymous=True, host=host)


if __name__ == "__main__":
    server = build_server()
    where = server.config.host or "0.0.0.0"
    print(f"Wijjit SSH server listening on {where}:8022 (ssh -p 8022 you@localhost)")
    server.run(port=8022)
