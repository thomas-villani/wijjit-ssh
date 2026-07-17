"""Tests for the wijjit_ssh logger tree, session-bound logging, and events.

The interesting assertions here are the ones about *containment*: a library that
sprays to stderr behind a host application's back is a real bug for this package
in particular, because the host may be a Wijjit TUI whose screen we would
corrupt. See :mod:`wijjit_ssh.logging`.
"""

from __future__ import annotations

import io
import logging

import pytest

from wijjit_ssh.logging import (
    LOGGER_NAME,
    EventEmitter,
    configure_logging,
    get_logger,
    logging_is_configured,
    new_session_id,
    session_logger,
)


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Snapshot and restore the wijjit_ssh logger, since configure_logging mutates it."""
    logger = logging.getLogger(LOGGER_NAME)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = propagate


# -- tree shape ----------------------------------------------------------------


def test_get_logger_roots_names_under_the_package_tree() -> None:
    assert get_logger("wijjit_ssh.server").name == "wijjit_ssh.server"
    assert get_logger(LOGGER_NAME).name == LOGGER_NAME
    # A bare module name gets rooted.
    assert get_logger("limits").name == "wijjit_ssh.limits"


def test_get_logger_does_not_confuse_a_prefix_with_the_tree() -> None:
    """ "wijjit_sshfoo" starts with LOGGER_NAME but is not in the tree.

    A bare ``startswith(LOGGER_NAME)`` would leave this name outside the tree
    with no handler - the exact class of bug this module exists to fix.
    """
    assert get_logger("wijjit_sshfoo").name == "wijjit_ssh.wijjit_sshfoo"


def test_package_logger_has_a_null_handler_at_import_time() -> None:
    """Without this, records reach logging.lastResort and print to stderr."""
    handlers = logging.getLogger(LOGGER_NAME).handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_records_do_not_reach_last_resort(capsys: pytest.CaptureFixture[str]) -> None:
    """An unconfigured library must stay silent, not write to stderr.

    This is the regression test for the bug that motivated the module: under
    wijjit's get_logger, this warning printed to stderr even after wijjit's own
    configure_logging(None).
    """
    get_logger("wijjit_ssh.server").warning("SERVER IS UNAUTHENTICATED")
    assert capsys.readouterr().err == ""


def test_propagate_stays_true_so_a_host_can_capture_us(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A host configuring the root logger must receive our records.

    This is also what keeps pytest's caplog working, so unlike wijjit (which
    sets propagate=False and needs a wijjit_caplog fixture) we need no
    workaround.
    """
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        get_logger("wijjit_ssh.server").info("connection from 10.0.0.7")
    assert "connection from 10.0.0.7" in caplog.text


# -- configure_logging ---------------------------------------------------------


def test_configure_logging_to_a_stream() -> None:
    stream = io.StringIO()
    configure_logging(stream, level="INFO")
    get_logger("wijjit_ssh.server").info("listening on 8022")
    assert "listening on 8022" in stream.getvalue()


def test_configure_logging_to_a_file(tmp_path) -> None:
    path = tmp_path / "server.log"
    configure_logging(path, level="INFO")
    get_logger("wijjit_ssh.server").info("listening on 8022")
    logging.getLogger(LOGGER_NAME).handlers[0].flush()
    assert "listening on 8022" in path.read_text(encoding="utf-8")


def test_configure_logging_none_silences_the_tree() -> None:
    stream = io.StringIO()
    configure_logging(stream)
    configure_logging(None)
    get_logger("wijjit_ssh.server").error("should not appear")
    assert stream.getvalue() == ""


def test_configure_logging_respects_the_level() -> None:
    stream = io.StringIO()
    configure_logging(stream, level="WARNING")
    logger = get_logger("wijjit_ssh.server")
    logger.info("filtered out")
    logger.warning("kept")
    output = stream.getvalue()
    assert "filtered out" not in output
    assert "kept" in output


def test_configure_logging_does_not_close_a_caller_supplied_stream() -> None:
    """Reconfiguring must not close sys.stderr out from under the process."""
    stream = io.StringIO()
    configure_logging(stream)
    configure_logging(io.StringIO())  # replaces the handler
    assert not stream.closed


def test_logging_is_configured_reflects_real_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest installs its own root handler, so pin root to empty: this test is
    # about our tree, and the root case is covered separately below.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    configure_logging(None)
    assert not logging_is_configured()
    configure_logging(io.StringIO())
    assert logging_is_configured()


def test_logging_is_configured_defers_to_a_host_that_configured_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host owning the root logger must stop run() installing its own handler."""
    configure_logging(None)
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.StreamHandler()])
    assert logging_is_configured()


# -- session logging -----------------------------------------------------------


def test_new_session_id_is_short_and_unique() -> None:
    ids = {new_session_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 8 and int(i, 16) >= 0 for i in ids)


def test_session_logger_binds_id_username_and_peer() -> None:
    stream = io.StringIO()
    configure_logging(stream, level="INFO")
    session_logger("3f9a1c04", "ada", "10.0.0.7").info("pty requested")
    assert "[3f9a1c04 ada@10.0.0.7] pty requested" in stream.getvalue()


def test_session_logger_supports_lazy_formatting_args() -> None:
    stream = io.StringIO()
    configure_logging(stream, level="INFO")
    session_logger("3f9a1c04", "ada", "10.0.0.7").info("resize to %dx%d", 120, 40)
    assert "[3f9a1c04 ada@10.0.0.7] resize to 120x40" in stream.getvalue()


# -- events --------------------------------------------------------------------


def test_event_emitter_forwards_name_and_fields() -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    emitter = EventEmitter(lambda event, fields: seen.append((event, dict(fields))))
    emitter.emit("session.started", session_id="3f9a1c04", username="ada")
    assert seen == [("session.started", {"session_id": "3f9a1c04", "username": "ada"})]


def test_event_emitter_without_a_hook_is_a_no_op() -> None:
    EventEmitter().emit("session.started", session_id="3f9a1c04")


def test_a_raising_hook_cannot_break_a_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """These fire inside asyncssh callbacks; a metrics bug must not kill the session."""

    def boom(event: str, fields) -> None:
        raise RuntimeError("prometheus is down")

    emitter = EventEmitter(boom)
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        emitter.emit("session.ended", reason="idle_timeout")  # must not raise
    assert "on_event hook raised" in caplog.text
    assert "prometheus is down" in caplog.text
