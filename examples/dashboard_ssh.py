"""A live server dashboard served over SSH - including the server itself.

Run it::

    uv sync --group examples
    uv run --group examples python examples/dashboard_ssh.py    # serves on :8022

Then, from anywhere you have a key on the box::

    ssh -p 8022 you@localhost

You land in a dashboard that refreshes itself: CPU and memory gauges, a history
chart, disk and uptime, the heaviest processes - and a table of everyone who is
currently connected to *this* server, which is the part a local ``top`` cannot
show you.

Why this example exists
-----------------------

``hello_ssh.py`` proves the transport. This one is about what changes when the
same app runs N times in one process:

**One sampler, not N.** The obvious port of a local monitor gives every session
its own timer and its own ``psutil`` calls, so ten viewers sample the same
machine ten times. Here a single :class:`Sampler` task samples once per second
and every window renders the same reading. It starts when the first viewer
connects and stops when the last one leaves, so an idle server is idle.

**Blocking work goes to a thread.** All sessions share one event loop, so a
sampler that called ``psutil.process_iter()`` inline would freeze *every*
connected client for as long as it took - and enumerating processes takes tens to
hundreds of milliseconds. :func:`Sampler.sample` runs in ``asyncio.to_thread``
instead. This is the README's "give CPU-bound apps an executor" warning in the
one place a reader will actually meet it.

**Pushing to a session from outside its own task.** Each session's app is parked
in ``read_input_async`` waiting for a keypress that may never come. The sampler
calls ``app.refresh()`` on each of them from its own task; that sets a flag the
target's loop checks when its input read times out - ``REFRESH_INTERVAL / 2`` if
set, 0.5s if not. See ``chat_ssh.py`` for the same mechanism used the other way
round (many writers, many readers).

Authentication is not optional here
-----------------------------------

``hello_ssh.py`` falls back to ``allow_anonymous=True`` when it finds no
``authorized_keys``, so the demo always runs. A dashboard that prints the
machine's process table and the address of every connected user must not do that,
so this one refuses to start instead and says how to fix it. The contrast is the
point: ``allow_anonymous`` is a decision about what the app exposes, not a
default to inherit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import psutil
from wijjit import Wijjit, render_template_string
from wijjit.terminal.size import get_terminal_size

from wijjit_ssh import AuthorizedKeys, SSHSession, WijjitSSH, ensure_host_key

PORT = 8022

#: Seconds between samples. One second is what a person reads as "live"; the
#: history chart below holds HISTORY_POINTS of these.
SAMPLE_INTERVAL = 1.0

#: How many samples the history chart keeps. Not a duration: a cycle costs
#: SAMPLE_INTERVAL *plus* however long the sample itself took, and enumerating
#: processes is not free, so the window this covers depends on the machine.
HISTORY_POINTS = 240

#: How many processes the "heaviest" table shows.
TOP_PROCESSES = 8

#: The sampler updates once a second, so there is nothing to gain from checking
#: for pushed work more often than twice that. See the module docstring.
REFRESH_INTERVAL = 0.5


@dataclass
class Reading:
    """One sample of the machine, shared by every connected window.

    Attributes
    ----------
    cpu : float
        System-wide CPU utilisation, percent.
    memory : float
        Virtual memory used, percent.
    disk : float
        Root/system volume used, percent.
    memory_used_gb, memory_total_gb : float
        Absolute memory figures, for the label under the gauge.
    processes : list of dict
        The heaviest processes, newest sample only, ready for ``{% table %}``.
    taken_at : str
        Wall-clock time of the sample, ``HH:MM:SS``.
    """

    cpu: float = 0.0
    memory: float = 0.0
    disk: float = 0.0
    memory_used_gb: float = 0.0
    memory_total_gb: float = 0.0
    processes: list[dict[str, Any]] = field(default_factory=list)
    taken_at: str = "-"


@dataclass
class Viewer:
    """One connected client, as both a dashboard row and a push target.

    Attributes
    ----------
    username : str
        Name the client authenticated as.
    peer_ip : str
        Address it connected from.
    app : Wijjit
        That client's live app, so the sampler can
        :meth:`~wijjit.Wijjit.refresh` it from the sampler's own task.
    joined_at : float
        ``time.monotonic()`` at connect, for the "for" column.
    """

    username: str
    peer_ip: str
    app: Wijjit
    joined_at: float = field(default_factory=time.monotonic)


def _format_duration(seconds: float) -> str:
    """Render a duration the way a person reads one: ``2h 05m``, ``40s``."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class Sampler:
    """The one place this process asks the OS how it is doing.

    Owns the reading, the history, and the set of windows to notify. Runs at most
    one task, started on the first viewer and stopped after the last one, so a
    dashboard nobody is watching costs nothing.
    """

    def __init__(self) -> None:
        self.reading = Reading()
        self.cpu_history: list[float] = []
        self.memory_history: list[float] = []
        self.viewers: dict[str, Viewer] = {}
        self.started_at = datetime.now()
        self._task: asyncio.Task[None] | None = None

    def join(self, session_id: str, session: SSHSession, app: Wijjit) -> None:
        """Register a window and make sure the sampler is running.

        Parameters
        ----------
        session_id : str
            ``SSHSession.session_id``; the key :meth:`leave` will use.
        session : SSHSession
            The connection, for the username and peer address shown in the
            session table.
        app : Wijjit
            The app rendering this window.
        """
        self.viewers[session_id] = Viewer(
            username=session.username, peer_ip=session.peer_ip, app=app
        )
        if self._task is None:
            # We are on the event loop here (the factory runs inside
            # session_started), so this is the natural place to start it. Doing
            # it in run() instead would mean sampling an empty room.
            self._task = asyncio.ensure_future(self._run())

    def leave(self, session_id: str) -> None:
        """Drop a window, and stop sampling if it was the last one.

        Parameters
        ----------
        session_id : str
            The id passed to :meth:`join`.
        """
        self.viewers.pop(session_id, None)
        if not self.viewers and self._task is not None:
            self._task.cancel()
            self._task = None

    def sessions_table(self) -> list[dict[str, str]]:
        """The connected-clients table, newest connection last."""
        now = time.monotonic()
        return [
            {
                "user": viewer.username,
                "from": viewer.peer_ip or "-",
                "for": _format_duration(now - viewer.joined_at),
            }
            for viewer in self.viewers.values()
        ]

    def uptime(self) -> str:
        """How long this server process has been up."""
        return _format_duration((datetime.now() - self.started_at).total_seconds())

    @staticmethod
    def _prime() -> None:
        """Take the throwaway first reading. **Blocking**, like :meth:`sample`.

        Both of psutil's percentage APIs report usage *since the previous call*,
        and so both return a meaningless 0.0 the first time. Burning one reading
        up front is what makes the first number a viewer sees a real one.
        """
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter(["cpu_percent"]):
            del proc

    @staticmethod
    def sample() -> Reading:
        """Read the machine. **Blocking** - always call this off the loop.

        ``process_iter`` walks every process on the box; on a busy desktop that
        is comfortably hundreds of milliseconds, and it was closer to a second on
        the machine this was written on. Every session in this process shares one
        event loop, so calling it inline would freeze *every* connected client's
        frames for that long - not just the caller's.

        Returns
        -------
        Reading
            A complete sample.
        """
        memory = psutil.virtual_memory()
        cpu = float(psutil.cpu_percent(interval=None))
        cores = psutil.cpu_count() or 1

        processes = []
        for proc in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            info = proc.info
            name = info.get("name") or "?"
            # The idle process is "using" every core that nothing else is, so it
            # sits at the top of a CPU sort forever while telling you nothing.
            if name in ("System Idle Process", "kernel_task"):
                continue
            processes.append(
                {
                    "process": name[:24],
                    # Per-process CPU is summed across cores and so runs past
                    # 100%; divide it down to match the gauge above.
                    "cpu %": f"{(info.get('cpu_percent') or 0.0) / cores:.1f}",
                    "mem %": f"{info.get('memory_percent') or 0.0:.1f}",
                }
            )
        processes.sort(key=lambda row: float(row["cpu %"]), reverse=True)

        return Reading(
            cpu=cpu,
            memory=float(memory.percent),
            disk=float(psutil.disk_usage(str(Path.home().anchor or "/")).percent),
            memory_used_gb=(memory.total - memory.available) / 1024**3,
            memory_total_gb=memory.total / 1024**3,
            processes=processes[:TOP_PROCESSES],
            taken_at=datetime.now().strftime("%H:%M:%S"),
        )

    async def _run(self) -> None:
        """Sample on a timer and poke every window. Cancelled when idle."""
        await asyncio.to_thread(self._prime)
        while True:
            # The sleep leads, so the first reading spans a real interval rather
            # than the instant since _prime. It costs the first viewer one
            # interval of empty gauges and buys everyone honest numbers.
            await asyncio.sleep(SAMPLE_INTERVAL)
            self.reading = await asyncio.to_thread(self.sample)
            self.cpu_history = (self.cpu_history + [self.reading.cpu])[-HISTORY_POINTS:]
            self.memory_history = (self.memory_history + [self.reading.memory])[
                -HISTORY_POINTS:
            ]
            # Each of these apps belongs to a different task, parked waiting for
            # its own user to type. refresh() is what gets it to redraw anyway.
            # See the module docstring for the latency this implies.
            for viewer in self.viewers.values():
                viewer.app.refresh()


sampler = Sampler()


TEMPLATE = """
{% frame title=title border="double" width="fill" height="fill" %}
  {% vstack spacing=0 padding=1 %}
    {% text %}{{ headline }}{% endtext %}

    {% hstack spacing=2 %}
      {% frame title="CPU" border="single" width=gauge_frame height=6 %}
        {% vstack spacing=0 %}
          {% gauge id="cpu" value=reading.cpu max_value=100 width=gauge_width
                   label="CPU" unit="%" color="threshold" %}{% endgauge %}
          {% text %}{{ "%.1f"|format(reading.cpu) }}% of all cores{% endtext %}
        {% endvstack %}
      {% endframe %}

      {% frame title="Memory" border="single" width=gauge_frame height=6 %}
        {% vstack spacing=0 %}
          {% gauge id="mem" value=reading.memory max_value=100 width=gauge_width
                   label="Memory" unit="%" color="gradient"
                   color_scale="heat" %}{% endgauge %}
          {% text %}{{ memory_label }}{% endtext %}
        {% endvstack %}
      {% endframe %}
    {% endhstack %}

    {% frame title="History" border="single" width=panel height=chart_frame %}
      {% linechart id="history" data=history width=chart_width height=chart_height
                   style="line" show_axis=true show_labels=false
                   show_legend=true %}
      {% endlinechart %}
    {% endframe %}

    {% hstack spacing=2 %}
      {% frame title="Heaviest processes" border="single"
               width=half height=table_frame %}
        {% table id="processes" data=reading.processes
                 columns=["process", "cpu %", "mem %"]
                 width=table_width height=table_height
                 show_header=true show_scrollbar=false bind=False
                 border_style="none" tab_index=-1 %}
        {% endtable %}
      {% endframe %}

      {% frame title="Connected now" border="single"
               width=half height=table_frame %}
        {% table id="sessions" data=sessions columns=["user", "from", "for"]
                 width=table_width height=table_height
                 show_header=true show_scrollbar=false bind=False
                 border_style="none" tab_index=-1 %}
        {% endtable %}
      {% endframe %}
    {% endhstack %}
  {% endvstack %}
{% endframe %}
"""


def make_app(session: SSHSession) -> Wijjit:
    """Build one dashboard window per SSH connection.

    Parameters
    ----------
    session : SSHSession
        The connection context. ``session.backend`` routes this app's I/O to the
        SSH channel.

    Returns
    -------
    Wijjit
        The app for this connection, already registered with the sampler.
    """
    app = Wijjit(
        backend=session.backend,
        # Nothing to seed: every number on screen is read from the sampler at
        # render time, so there is one copy of the machine's state per process
        # rather than one per viewer.
        REFRESH_INTERVAL=REFRESH_INTERVAL,
    )

    @app.view("main", default=True)
    def main() -> str:
        # Live, not from `session`: each client is its own shape and may resize.
        size = get_terminal_size()
        reading = sampler.reading

        # Frame border (2) + vstack padding (2).
        panel = max(30, size.columns - 4)
        half = (panel - 2) // 2
        # Rows: frame border 2, padding 2, headline 1, gauge frame 6.
        remaining = max(8, size.lines - 11)
        # Split what is left between the chart and the two tables.
        chart_frame = max(5, remaining // 2)
        table_frame = max(3, remaining - chart_frame)

        return render_template_string(
            TEMPLATE,
            title=f"{session.username}@this server - Ctrl+Q to disconnect",
            headline=(
                f"up {sampler.uptime()} | disk {reading.disk:.0f}% used | "
                f"{len(sampler.viewers)} watching | sampled {reading.taken_at}"
            )[:panel],
            reading=reading,
            memory_label=(
                f"{reading.memory_used_gb:.1f} of "
                f"{reading.memory_total_gb:.1f} GiB in use"
            ),
            history={"CPU": sampler.cpu_history, "Memory": sampler.memory_history},
            sessions=sampler.sessions_table(),
            panel=panel,
            half=half,
            gauge_frame=half,
            gauge_width=max(10, half - 4),
            chart_frame=chart_frame,
            chart_width=max(20, panel - 4),
            chart_height=max(3, chart_frame - 2),
            table_frame=table_frame,
            table_width=max(20, half - 4),
            table_height=max(2, table_frame - 2),
        )

    sampler.join(session.session_id, session, app)
    return app


def on_server_event(event: str, fields: Mapping[str, object]) -> None:
    """Drop a viewer when its session ends.

    ``on_event`` is advertised as a metrics hook, and it is the right tool here
    for a structural reason: the app factory has no teardown callback, and there
    is no reliable in-app signal either. Checking ``app.running`` races - the
    server calls the factory *before* starting the app's task, so a sampler tick
    landing in that window would evict a session that had not started yet.

    ``session.ended`` has no such gap. It fires on every way out - the user quit,
    the idle timeout expired, the TCP connection died, or ``stop()`` drained the
    server - and it carries the same ``session_id`` the factory registered under.
    Getting this wrong here costs more than a stale row: the sampler would never
    see the room empty, and would keep sampling forever.

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
            sampler.leave(session_id)


def build_server() -> WijjitSSH:
    """Build the server, or refuse to.

    Unlike ``hello_ssh.py`` there is no anonymous fallback. This app shows the
    machine's process table and the address of everyone connected to it; serving
    that to whoever can reach the port is not a demo convenience, it is a leak.

    Returns
    -------
    WijjitSSH
        A configured, unstarted server.

    Raises
    ------
    SystemExit
        If there is no ``~/.ssh/authorized_keys`` to authenticate against.
    """
    authorized_keys = Path.home() / ".ssh" / "authorized_keys"
    if not authorized_keys.is_file():
        raise SystemExit(
            f"No {authorized_keys}.\n"
            "This dashboard exposes the machine's process table and every\n"
            "connected user's address, so it will not run unauthenticated.\n"
            "Create a key and authorize it:\n"
            "    ssh-keygen -t ed25519\n"
            "    cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys"
        )

    print(f"Public-key auth against {authorized_keys}")
    return WijjitSSH(
        make_app,
        host_keys=[ensure_host_key("ssh_host_key")],
        auth=AuthorizedKeys(authorized_keys),
        on_event=on_server_event,
        # A dashboard is watched, not typed at, so the default 10-minute idle
        # timeout would disconnect exactly the people using it as intended.
        idle_timeout=None,
    )


if __name__ == "__main__":
    server = build_server()
    print(f"Dashboard listening on port {PORT} (ssh -p {PORT} you@localhost)")
    server.run(port=PORT)
