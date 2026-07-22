Installation
============

Requirements
------------

* Python 3.11 or newer
* `Wijjit <https://github.com/thomas-villani/wijjit>`_ (the TUI framework)
* `asyncssh <https://asyncssh.readthedocs.io/>`_ 2.14 or newer

Linux, macOS, and Windows are all supported and all tested in CI. The one
platform difference worth knowing is signal handling: Windows never delivers
``SIGTERM``, so only Ctrl+C triggers a graceful drain there. See
:doc:`../guide/shutdown`.

Installing
----------

.. note::

   Wijjit is not on PyPI yet, so ``wijjit-ssh`` cannot be installed from PyPI
   either - its dependency would not resolve. Until that changes, install from
   source with the two repositories checked out side by side.

Clone the two repositories as siblings::

   PycharmProjects/
     wijjit/        # github.com/thomas-villani/wijjit
     wijjit-ssh/    # github.com/thomas-villani/wijjit-ssh

``pyproject.toml`` points ``uv`` at that layout via ``[tool.uv.sources]``, so a
plain sync is all it takes:

.. code-block:: bash

   git clone https://github.com/thomas-villani/wijjit.git
   git clone https://github.com/thomas-villani/wijjit-ssh.git
   cd wijjit-ssh
   uv sync                       # installs wijjit editable from ../wijjit

The source is deliberately **a path and editable, not a git ref**: the two
libraries are developed in tandem, so a change in ``../wijjit`` is picked up here
with no reinstall. A git source would test against whatever was last pushed.

Once Wijjit publishes, that section goes away and ``pip install wijjit-ssh``
works like anything else. (``uv`` strips ``[tool.uv.sources]`` from published
metadata, so it never affects anyone installing the package.)

Verifying the install
---------------------

.. code-block:: bash

   uv run python -c "import wijjit_ssh; print(wijjit_ssh.__version__)"
   uv run python examples/hello_ssh.py

The second command starts a server on port 8022 and generates ``ssh_host_key``
in the working directory on first run. From another terminal::

   ssh -p 8022 yourname@localhost

Working on wijjit-ssh
---------------------

These are exactly the commands CI runs, so a clean local run means a green
build:

.. code-block:: bash

   uv run pytest -q
   uv run ruff check src/ tests/ examples/
   uv run black --check src/ tests/ examples/
   uv run mypy src/

Four tests are POSIX-only - three ``0600`` host-key mode-bit assertions and the
end-to-end ``SIGTERM`` drain - so Windows reports ``334 passed, 4 skipped``
where Linux and macOS report ``338 passed``.

Building these docs
-------------------

Sphinx and its theme live in their own dependency group, so a contributor
running the test suite does not pay for them:

.. code-block:: bash

   uv sync --group docs
   uv run sphinx-build -b html docs/source docs/build/html

CI builds with ``-W``, which turns warnings into errors, so run it that way
before pushing a docs change.
