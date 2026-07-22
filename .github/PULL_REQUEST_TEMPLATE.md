<!--
Thanks for the patch. CONTRIBUTING.md has the setup, the checks, and the commit
conventions; this template is just the short version of "did you remember".

Delete any section that does not apply. A one-line typo fix does not need a
design note.
-->

## What this changes

<!-- And, more usefully: why. What was the alternative, and why is this better? -->

## Why it is correct

<!--
How do you know? "Added a test that fails without the fix" is the strongest
answer. For anything touching the connection lifecycle, say which path you
exercised - a polite quit, a dropped TCP connection, an idle timeout, and a
drain are four different code paths and they have all been wrong at least once.
-->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run ruff check src/ tests/ examples/` and `uv run black --check src/ tests/ examples/` pass
- [ ] `uv run mypy src/` passes
- [ ] New behaviour has a test, over a real SSH connection where that is the honest way to test it
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] Docs updated in this PR if behaviour changed
- [ ] `SPEC.md` updated if this touches something it describes
- [ ] Examples still run, if you touched `examples/` (nothing tests them)
