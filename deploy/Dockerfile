# A wijjit-ssh server in a container.
#
#   docker build -f deploy/Dockerfile -t wijjit-ssh-demo .
#   docker run --rm -p 8022:8022 -v wijjit-hostkey:/var/lib/wijjit-ssh wijjit-ssh-demo
#
# Build context is the repository root, not deploy/, because the image copies
# both the healthcheck and the example it serves.
#
# It runs examples/hello_ssh.py so the image is something you can actually start.
# Swap the COPY and CMD for your own application; everything else transfers.

FROM python:3.13-slim

# uv, for the same reason the rest of the project uses it: dev and docs
# dependencies here are PEP 735 groups, which pip cannot see.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Unbuffered stdout, or logs sit in a pipe buffer and `docker logs` shows nothing
# until the process exits - which for a server is "never".
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN uv venv /opt/venv

# Dependencies in their own layer, ahead of the application source, so editing
# the app does not re-resolve the world.
#
# BEFORE wijjit IS ON PyPI this line cannot work - `wijjit>=0.1.0` will not
# resolve. Until then, build with both repositories as siblings and the parent
# directory as the build context:
#     COPY wijjit /src/wijjit
#     COPY wijjit-ssh /src/wijjit-ssh
#     RUN uv pip install /src/wijjit /src/wijjit-ssh
# See RELEASING.md.
RUN uv pip install --no-cache wijjit-ssh

# A non-root user. This process terminates untrusted connections; there is no
# argument for giving it uid 0.
RUN useradd --system --create-home --home-dir /home/wijjit --shell /usr/sbin/nologin wijjit \
    && mkdir -p /var/lib/wijjit-ssh \
    && chown wijjit:wijjit /var/lib/wijjit-ssh \
    && chmod 0700 /var/lib/wijjit-ssh

WORKDIR /app
COPY --chown=wijjit:wijjit examples/hello_ssh.py /app/app.py
COPY --chown=wijjit:wijjit deploy/healthcheck.py /app/healthcheck.py

USER wijjit

# The host key lives here, and this MUST be a mounted volume. Without one it is
# regenerated on every `docker run`, and every returning user gets REMOTE HOST
# IDENTIFICATION HAS CHANGED - the warning you least want people trained to
# click through. wijjit-ssh logs at WARNING each time it generates a key, so if
# you see that line on every start, the volume is not attached.
VOLUME ["/var/lib/wijjit-ssh"]
WORKDIR /var/lib/wijjit-ssh

EXPOSE 8022

# Completes the SSH key exchange and gets refused at authentication, which
# proves the event loop is running and the host key is usable. A TCP probe would
# pass against a wedged process, because the kernel completes the handshake
# without the application. See the module docstring in healthcheck.py.
#
# start-period covers first-boot host key generation.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "/app/healthcheck.py", "--port", "8022", "--timeout", "8"]

# exec form, so the process is PID 1 and receives SIGTERM directly - `docker
# stop` then triggers the graceful drain instead of being swallowed by a shell.
#
# `docker stop` allows 10s by default, which must exceed shutdown_grace
# (default 5.0). Raise both together if your app's teardown is slow:
#     docker stop --timeout 30 <container>
CMD ["python", "/app/app.py"]
