# deploy/

Reference artifacts for running a `wijjit-ssh` server in production. They are
files rather than snippets in a document on purpose: a unit file that has been
started is worth more than one that has been typed.

| File | What it is |
|---|---|
| [`wijjit-ssh.service`](wijjit-ssh.service) | systemd unit — sandboxed, drains on `systemctl stop`, host key under `StateDirectory` |
| [`Dockerfile`](Dockerfile) | Container image, non-root, with a real healthcheck |
| [`compose.yaml`](compose.yaml) | The same image plus the named volume the host key needs |
| [`healthcheck.py`](healthcheck.py) | Liveness probe — completes key exchange and expects to be refused at auth |

The prose that explains the choices, and the production security checklist, is in
the deployment guide:
<https://thomas-villani.github.io/wijjit-ssh/guide/deployment.html>

## The image is a demo, and it is unauthenticated

The `Dockerfile` serves `examples/hello_ssh.py`, which uses public-key auth when
it finds a `~/.ssh/authorized_keys` and falls back to `allow_anonymous=True` when
it does not. There is no `authorized_keys` in the image, so **as built it accepts
any username with no credential.** `compose.yaml` therefore publishes to
`127.0.0.1` only.

What is production-shaped here is the *structure* around the app — non-root user,
read-only root filesystem, dropped capabilities, a persistent host key volume, a
stop timeout above `shutdown_grace`, and a healthcheck that proves more than a
TCP handshake. Point the `COPY` and `CMD` at an app of yours that passes a real
`auth` policy, then widen the port binding.

## The three things that go wrong

1. **The host key is not persistent.** Regenerating it gives every returning user
   `REMOTE HOST IDENTIFICATION HAS CHANGED`, which trains them to ignore the one
   warning that matters. `ensure_host_key` logs at WARNING every time it
   generates — if you see that line on every restart, your volume is not
   attached.

2. **The stop timeout is shorter than `shutdown_grace`.** Then the supervisor
   sends `SIGKILL` mid-drain, sessions never run their teardown, and every
   connected user is left in the alternate screen buffer with a terminal that
   needs `reset`. `TimeoutStopSec` (systemd) and `stop_grace_period` (compose)
   must both exceed `shutdown_grace`, which defaults to 5 seconds.

3. **The healthcheck is a TCP connect.** A wedged event loop still has a
   listening socket — the kernel completes the handshake without the
   application — so a TCP probe reports healthy while nobody can log in. Use
   `healthcheck.py`.

## Trying it

```bash
python deploy/healthcheck.py --port 8022 --verbose
docker compose -f deploy/compose.yaml up --build
```
