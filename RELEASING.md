# Releasing

The tag is the release. Push `vX.Y.Z` and
[`.github/workflows/release.yml`](.github/workflows/release.yml) builds, checks,
publishes to PyPI via Trusted Publishing, and opens a GitHub release with the
changelog section as its notes. Nothing is uploaded by hand.

## Before the first release

Three things blocked `0.1.0`. All three are **done** as of the 0.1.0 preparation,
and are recorded here because the `verify` job still enforces the second one and
a future contributor will want to know why.

1. **`wijjit` must be on PyPI.** ✅ `wijjit` 0.1.0 published. `wijjit-ssh` depends
   on it; until it published, a wheel here installed for nobody.
2. **`[tool.uv.sources]` must be gone from `pyproject.toml`.** ✅ Removed, and
   `uv.lock` re-resolved against the real index. It had pointed `wijjit` at
   `../wijjit`. `uv` strips the section from published metadata, so it never
   reaches an installer — which is the problem: while it is there, `wijjit>=0.1.0`
   has never once been resolved from the real index by anything, here or in CI.
   **The `verify` job still refuses to build while that line exists**, so a
   contributor who re-adds it for local development will be caught at release
   rather than by a user. Use `uv pip install -e ../wijjit` instead; see
   `CONTRIBUTING.md`.
3. **CI should collapse to a single checkout.** ✅ `ci.yml` and `docs.yml` each
   checked out both repositories to satisfy the path source. Both are ordinary
   single-checkout workflows now, and the sync is tightened to `uv sync --locked`
   — it had been unpinned because the path source made `uv.lock` move whenever
   the sibling checkout's own dependencies did.

Rehearse on TestPyPI once. A project name is claimed on first upload and a
bad `0.1.0` can be yanked but never replaced:

```bash
gh workflow run release.yml
```

That publishes the current branch's build to TestPyPI and stops. Install it in a
scratch environment and import it before tagging anything for real.

## One-time setup

**PyPI Trusted Publishing.** On PyPI → *Your projects* → *Publishing* (or the
pending-publisher form, if `wijjit-ssh` is not registered yet), add a publisher:

| Field | Value |
|---|---|
| Owner | `thomas-villani` |
| Repository | `wijjit-ssh` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Repeat on TestPyPI with environment `testpypi`. Then create both environments
under GitHub → *Settings* → *Environments*. There is no API token anywhere in
this process; GitHub mints a short-lived OIDC token per run and PyPI exchanges
it for an upload token scoped to this project alone.

**GitHub Pages** is already configured for the docs (Settings → Pages → Source =
*GitHub Actions*).

## Cutting a release

1. **Confirm the tree is green.** These are the commands CI runs:

   ```bash
   uv sync
   uv run pytest -q
   uv run ruff check src/ tests/ examples/ deploy/
   uv run black --check src/ tests/ examples/ deploy/
   uv run mypy src/ deploy/
   uv sync --group docs
   uv run sphinx-build -b html -W --keep-going docs/source docs/build/html
   ```

2. **Close the changelog section.** In `CHANGELOG.md`, rename `## [Unreleased]`
   to `## [X.Y.Z] - YYYY-MM-DD` with the real date, open a fresh empty
   `## [Unreleased]` above it, and fix the link definitions at the bottom:

   ```markdown
   [Unreleased]: https://github.com/thomas-villani/wijjit-ssh/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/thomas-villani/wijjit-ssh/releases/tag/vX.Y.Z
   ```

   Write it for someone deciding whether to upgrade. The release job publishes
   this section verbatim as the GitHub release notes.

3. **Bump the version.** One place — `__version__` in
   `src/wijjit_ssh/__init__.py`. `[tool.hatch.version]` reads it, and
   `docs/source/conf.py` reads that through `importlib.metadata`.

4. **Review the release-facing prose.** The status paragraphs in `README.md` and
   `docs/source/index.rst`, and the "not yet on PyPI" notes in
   `docs/source/getting_started/installation.rst`, all describe a pre-release
   state and will be wrong the moment this ships.

5. **Commit and tag.**

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin main --follow-tags
   ```

6. **Watch it.** `gh run watch`. The pipeline verifies the tag against
   `__version__`, verifies the changelog has a matching section, resolves
   dependencies from PyPI proper, builds, runs `twine check --strict`, asserts
   `py.typed` is in the wheel, publishes, and only then creates the release.

## If it goes wrong

A version number on PyPI is permanent. It can be **yanked** — hidden from
resolution while remaining installable by exact pin — but never replaced.

- **Failed before publishing.** Fix, delete the tag locally and on the remote,
  re-tag. Nothing was public.
- **Published something broken.** Yank it
  (`pypi.org/manage/project/wijjit-ssh/releases/`) and release the fix as the
  next patch version. Do not attempt to reuse the number.
- **Leaked a secret.** Yanking does not remove the file. Delete the release on
  PyPI, rotate the secret, then release a fixed version.

## Versioning

[Semantic Versioning](https://semver.org/spec/v2.0.0.html), with the usual 0.x
caveat: while the major version is 0, minor bumps may break the API. The public
surface is what `wijjit_ssh.__all__` exports and the fields of `ServerConfig` —
anything reachable only through a module-private name is fair game.

Post-1.0, breaking that surface requires a major bump, and a deprecation gets one
minor release of warning first.
