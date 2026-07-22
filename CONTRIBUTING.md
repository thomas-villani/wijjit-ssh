# Contributing

Thanks for looking. This is a small project with a written plan — [`SPEC.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/SPEC.md)
describes the architecture, the milestones, and the open questions — so the most
useful thing you can do before writing code is check whether the thing you want
is already in there, already deliberately excluded, or already scheduled.

## Setting up

`wijjit` is not on PyPI yet, so `pyproject.toml` resolves it from a sibling
checkout via `[tool.uv.sources]`. Clone both repositories side by side:

```
PycharmProjects/
  wijjit/        # github.com/thomas-villani/wijjit
  wijjit-ssh/    # this repo
```

```bash
git clone https://github.com/thomas-villani/wijjit.git
git clone https://github.com/thomas-villani/wijjit-ssh.git
cd wijjit-ssh
uv sync                            # installs wijjit editable from ../wijjit
```

The source is a **path, and editable, on purpose**: the two libraries are
developed in tandem, so a change in `../wijjit` is picked up here with no
reinstall. A git ref would test against whatever was last pushed. That section
goes away once `wijjit` publishes — see [`RELEASING.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/RELEASING.md).

Everything runs through `uv`. There is no `pip install -e .` path, because dev
dependencies here are PEP 735 `[dependency-groups]`, which pip cannot see.

## The checks

These are exactly the commands CI runs, so a clean local run means a green
build:

```bash
uv run pytest -q                                   # 334 passed, 4 skipped (Windows)
uv run ruff check src/ tests/ examples/
uv run black --check src/ tests/ examples/
uv run mypy src/
```

Four tests are POSIX-only — three `0600` host-key mode-bit assertions and the
end-to-end SIGTERM drain — so Linux and macOS report `338 passed`. CI runs
Python 3.11–3.13 across Linux, macOS, and Windows.

Docs and the dashboard example are separate dependency groups, so a test run
does not pay for Sphinx or build a C extension:

```bash
uv sync --group docs
uv run sphinx-build -b html -W --keep-going docs/source docs/build/html

uv run --group examples python examples/dashboard_ssh.py
```

`-W` turns warnings into errors. The build is warning-clean and keeping it that
way is the point of the flag: a broken cross-reference is a page nobody can
navigate, which is a bug, not a note.

## Style

- **Black, 88 columns, `py311` target.** Not negotiable and not worth
  discussing; run the formatter.
- **Ruff over `src/`, `tests/`, and `examples/`.** All three, unlike the wijjit
  repo's own workflow. They are already clean, so there is no reason to leave
  them unguarded.
- **`mypy --strict` over `src/`.** The package ships `py.typed`, so its
  annotations are a promise to every downstream type checker. New public
  functions are fully annotated or they do not merge.
- **NumPy-style docstrings**, rendered by napoleon into the API reference. The
  reference pages are near-free precisely because the module docstrings carry
  real prose — keep writing them that way rather than leaving a one-liner for
  the docs to pad out.
- **`from __future__ import annotations`** at the top of every module.
- **Comments explain why, not what.** The existing tree leans heavily on this:
  if a line looks arbitrary, wrong, or redundant and is none of those, the
  reason it survives belongs next to it. A comment that restates the code is
  worse than none.

## Tests

New behaviour needs a test, and the bar is a little unusual here: this package
is a network server, so **the interesting tests go over a real SSH connection**.
`tests/_client.py` provides the harness, including a `pyte` VT emulator — the
round-trip tests need one because Wijjit's renderer sends a diff transcript, not
a picture, and only an emulator reconstructs the screen a user would actually
see.

Prefer, in order:

1. An end-to-end test over a real connection (`test_auth.py`, `test_limits.py`,
   `test_roundtrip.py` are the models).
2. A unit test against a pure component — `KeyDecoder` is deliberately
   side-effect-free and resumable so it can be tested by feeding it bytes.
3. A test that reaches into private state. Sometimes correct, rarely first.

Async tests need no decorator; `asyncio_mode = "auto"` is set.

Nothing tests `examples/`, which is how `hello_ssh.py`'s Greet button stayed
broken from the first commit until M4. If you touch an example, run it.

## Commits

Look at `git log` and match it. The convention is:

- A **sentence-case subject** that says what changed, not which files moved.
  Milestone work is prefixed (`M3 step 4: limits.py -- registry, buckets,
  timers (spec.md §8)`); everything else just leads with the verb (`Fix the
  SIGTERM drain test: wait for "listening", not for a print`).
- A **body that explains the reasoning**, wrapped at 79 columns. This is the
  part that matters. The commits here read as short design notes — what the
  alternative was, what was surprising, what was found in passing — because six
  months later that is the only surviving record of why the code looks like it
  does.
- **One logical change per commit**, and the suite green at each. Milestones
  land as a numbered series, not one large drop.

Not Conventional Commits. Do not add `feat:` / `fix:` prefixes.

## Pull requests

- Branch off `main`.
- Update [`CHANGELOG.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/CHANGELOG.md) under `## [Unreleased]`, in the existing
  voice — a bolded lead, then what changed and why it mattered. Entries are
  written for someone deciding whether to upgrade, not for someone auditing the
  diff.
- Update the docs in the same PR. A guide page that describes the old behaviour
  is worse than no page.
- If the change touches something `SPEC.md` describes, update the spec too, and
  say so in the commit body. The spec is a living document; the milestone list
  records what actually happened, including the parts that turned out different
  from the plan.
- CI must be green: tests on nine platform/version combinations, ruff, black,
  mypy, and the docs build.

## What is deliberately out of scope

Some things are missing on purpose, and a PR adding them will be declined:

- **Shell, `exec`, SFTP, and port forwarding.** A session only ever runs a
  Wijjit app, and there is no code path to anything else. That guarantee is a
  headline feature; see [`SECURITY.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/SECURITY.md).
- **A metrics library dependency.** The `on_event` hook exists so you can wire
  up Prometheus without this package depending on it.
- **Session resume / reconnect.** Noted in `SPEC.md` §14 as out of scope, worth
  a design note if flaky mobile links ever become a target.

Known gaps that *are* wanted are in `SPEC.md` §13 under M4 and M5 — backpressure
handling, byte counters, decoder fuzzing, and a load test to replace the
guessed default limits with a measured sizing rule.

## Reporting security issues

Not here. See [`SECURITY.md`](https://github.com/thomas-villani/wijjit-ssh/blob/main/SECURITY.md) — do not open a public issue.
