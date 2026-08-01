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

.. code-block:: bash

   pip install wijjit-ssh

Or with ``uv``:

.. code-block:: bash

   uv add wijjit-ssh

Wijjit and asyncssh come along as dependencies.

From source
^^^^^^^^^^^

.. code-block:: bash

   git clone https://github.com/thomas-villani/wijjit-ssh.git
   cd wijjit-ssh
   uv sync

Everything in this project runs through ``uv``. There is no ``pip install -e .``
path, because the development dependencies are PEP 735 ``[dependency-groups]``,
which pip cannot see at all.

To work against an unreleased Wijjit, install it over the top rather than adding
a ``[tool.uv.sources]`` section - the release workflow refuses to build while one
is present:

.. code-block:: bash

   uv sync && uv pip install -e ../wijjit

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
end-to-end ``SIGTERM`` drain - so Windows reports ``338 passed, 4 skipped``
where Linux and macOS report ``342 passed``.

Building these docs
-------------------

Sphinx and its theme live in their own dependency group, so a contributor
running the test suite does not pay for them:

.. code-block:: bash

   uv sync --group docs
   uv run sphinx-build -b html docs/source docs/build/html

CI builds with ``-W``, which turns warnings into errors, so run it that way
before pushing a docs change.
