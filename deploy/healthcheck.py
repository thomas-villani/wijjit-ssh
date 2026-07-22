#!/usr/bin/env python3
"""Liveness probe for a wijjit-ssh server.

Exits ``0`` if the server is up and speaking SSH, ``1`` otherwise. Written for
``HEALTHCHECK`` in a Dockerfile, a Kubernetes ``exec`` probe, or a systemd timer.

Why not a TCP connect
---------------------
A TCP handshake succeeds against a process that has accepted the socket and then
wedged - a deadlocked event loop still has a listening socket, because the kernel
completes the handshake without the application. It is the classic false-healthy.

This probe instead completes the SSH **version exchange and key exchange**, which
requires the event loop to be running, the host key to be loaded and usable, and
the transport to be functioning. Then it tries to authenticate with nothing at
all, and treats the resulting rejection as the healthy answer: being told "no" is
proof the server reached its auth policy.

So a pass means the whole stack up to and including authentication works. It does
not exercise the app factory - nothing can, without a credential.

Notes
-----
The probe is an ordinary connection from the server's point of view, so it counts
against ``max_per_ip`` for whatever address it comes from, and against
``connect_rate`` if that is enabled. Probing from loopback every 30s is
comfortably inside the defaults (10 concurrent per IP, rate limiting off); a
1-second interval against ``connect_rate=0.5`` would eventually rate-limit the
probe itself and report a healthy server as dead.

It never authenticates, so it never starts a session and never counts against
``max_sessions``.

Examples
--------
::

    python healthcheck.py                          # localhost:8022
    python healthcheck.py --port 2222 --timeout 3
    python healthcheck.py --host ssh.example.com --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncssh


async def probe(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """Connect and attempt authentication with no credentials.

    Parameters
    ----------
    host, port
        Where the server is listening.
    timeout
        Seconds for the whole exchange. A server too slow to answer within this
        is not usefully alive.

    Returns
    -------
    tuple of (bool, str)
        Whether the server is healthy, and a one-line explanation.
    """
    try:
        async with asyncio.timeout(timeout):
            conn = await asyncssh.connect(
                host,
                port,
                username="healthcheck",
                # Present nothing. The point is to be rejected, and a probe that
                # picked up an agent key or ~/.ssh/id_* from the environment
                # might not be - which would start a real session, and on a
                # server at max_sessions would report unhealthy for the wrong
                # reason entirely.
                client_keys=None,
                password=None,
                agent_path=None,
                # No known_hosts file: this is a liveness check, not an identity
                # check. Pinning would make a legitimate host-key rotation look
                # like an outage.
                known_hosts=None,
            )
    except asyncssh.PermissionDenied:
        # The healthy path. The server completed key exchange, read our auth
        # attempt, consulted its policy, and refused. Everything under test
        # worked.
        return True, "up (authentication refused, as expected)"
    except asyncssh.DisconnectError as exc:
        # A limit turning us away is also a live server - and one worth seeing
        # in the probe output, since "at capacity" is a real condition rather
        # than a failure of the process.
        return True, f"up (refused: {exc.reason.strip()})"
    except TimeoutError:
        return False, f"no answer within {timeout}s"
    except ConnectionRefusedError:
        return False, "connection refused (nothing listening)"
    except OSError as exc:
        return False, f"network error: {exc}"
    except asyncssh.Error as exc:
        return False, f"ssh error: {exc}"

    # Reached only if authentication with no credentials *succeeded*, which
    # means the server is running allow_anonymous=True. It is up - that is what
    # the probe asked - but say so, because in production it is a finding.
    conn.close()
    await conn.wait_closed()
    return True, "up (WARNING: accepted an anonymous login)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8022)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print the reason on success too; failures always print.",
    )
    args = parser.parse_args()

    healthy, message = asyncio.run(probe(args.host, args.port, args.timeout))

    if healthy:
        if args.verbose or "WARNING" in message:
            print(f"{args.host}:{args.port} {message}")
        return 0

    # Always on stderr, always printed: this is the line that ends up in
    # `docker inspect` output or a systemd journal when something is wrong.
    print(f"{args.host}:{args.port} {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
