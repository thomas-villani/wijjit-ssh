"""Sphinx configuration for the wijjit-ssh documentation.

Build it with ``uv run --group docs sphinx-build -b html docs/source docs/build/html``
(or ``make html`` from ``docs/``). CI adds ``-W``, so a warning here is a failed
build there; keep it clean.

There is deliberately no ``sys.path`` insertion. ``uv sync`` installs the project
itself into the environment, so autodoc imports the same ``wijjit_ssh`` the tests
do rather than a second copy reached by path - and :func:`importlib.metadata.version`
below then reports the real installed version instead of a string duplicated here.
"""

from importlib.metadata import version as _version

# -- Project information -------------------------------------------------------

project = "wijjit-ssh"
copyright = "2025-2026, Tom Villani, Ph.D."
author = "Tom Villani, Ph.D."
release = _version("wijjit-ssh")
version = release

# -- General configuration -----------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",  # API reference from the docstrings
    "sphinx.ext.napoleon",  # ...which are NumPy-style
    "sphinx.ext.intersphinx",  # resolve wijjit/asyncssh/stdlib cross-references
    "sphinx.ext.viewcode",  # "[source]" links
    "sphinx_copybutton",  # copy button on code blocks
    "sphinx_tabs.tabs",  # tabbed alternatives (dev vs production, etc.)
    "myst_parser",  # so CHANGELOG.md and friends can be included
]

exclude_patterns: list[str] = []

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}
master_doc = "index"

# -- Autodoc / napoleon --------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "show-inheritance": True,
}
# Signatures stay readable; the types show up in the parameter descriptions.
autodoc_typehints = "description"
# `from __future__ import annotations` is on everywhere, so every annotation is
# already a string. Without this, autodoc renders them verbatim - readers get
# `Union[str, PathLike[str]]` as raw text instead of a resolved cross-reference.
autodoc_typehints_format = "short"
autodoc_preserve_defaults = True

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = False
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_ivar = True
napoleon_use_param = True
napoleon_use_rtype = True

# -- Intersphinx ---------------------------------------------------------------

# The module docstrings are dense with :class:`~wijjit.terminal.backend.TerminalBackend`
# and :class:`~asyncssh.SSHServer` references, because those two seams are the
# whole subject of this package. Both projects publish an inventory, so those
# references become links rather than bare text.
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "wijjit": ("https://thomas-villani.github.io/wijjit/", None),
    "asyncssh": ("https://asyncssh.readthedocs.io/en/latest/", None),
}
intersphinx_timeout = 30

# -- MyST ----------------------------------------------------------------------

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

# -- HTML output ---------------------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_title = "wijjit-ssh"
html_static_path = ["_static"]
html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": False,
    "sticky_navigation": True,
}
htmlhelp_basename = "wijjitsshdoc"
