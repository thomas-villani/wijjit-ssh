"""Tests for host key loading, generation, and fingerprints.

Mostly filesystem-level and fast. The one round-trip test at the bottom proves
that a key produced by ``ensure_host_key`` is actually servable, which is the
claim the rest of the module rests on.
"""

from __future__ import annotations

import logging
import os

import asyncssh
import pytest

from wijjit_ssh.keys import (
    DEFAULT_HOST_KEY_ALGORITHM,
    ensure_host_key,
    fingerprint,
    load_host_keys,
    resolve_host_keys,
)

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits; on Windows the file inherits directory ACLs",
)


def _write_key(path) -> asyncssh.SSHKey:
    """Write a fresh ed25519 private key to ``path`` and return it."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    path.write_bytes(key.export_private_key())
    return key


# -- fingerprint ---------------------------------------------------------------


def test_fingerprint_includes_algorithm_and_sha256() -> None:
    key = asyncssh.generate_private_key("ssh-ed25519")
    printed = fingerprint(key)
    assert printed.startswith("ssh-ed25519 SHA256:")
    assert key.get_fingerprint() in printed


# -- load_host_keys ------------------------------------------------------------


def test_load_host_keys_reads_a_key(tmp_path) -> None:
    written = _write_key(tmp_path / "host_key")
    (loaded,) = load_host_keys([tmp_path / "host_key"])
    assert loaded.get_fingerprint() == written.get_fingerprint()


def test_load_host_keys_supports_rotation_with_several_keys(tmp_path) -> None:
    """asyncssh serves every key it is given; that is how a rotation window works."""
    new = _write_key(tmp_path / "new")
    old = _write_key(tmp_path / "old")
    loaded = load_host_keys([tmp_path / "new", tmp_path / "old"])
    assert [k.get_fingerprint() for k in loaded] == [
        new.get_fingerprint(),
        old.get_fingerprint(),
    ]


def test_load_host_keys_names_the_missing_path(tmp_path) -> None:
    """A typo'd path must not be skipped: it would change the server's identity."""
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError) as excinfo:
        load_host_keys([missing])
    assert str(missing) in str(excinfo.value)
    assert "ssh-keygen" in str(excinfo.value)


def test_load_host_keys_names_the_path_of_an_unreadable_key(tmp_path) -> None:
    """asyncssh's own error is just "Invalid private key" with no path."""
    garbage = tmp_path / "garbage"
    garbage.write_bytes(b"not a key at all")
    with pytest.raises(ValueError) as excinfo:
        load_host_keys([garbage])
    assert str(garbage) in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, asyncssh.KeyImportError)


def test_load_host_keys_expands_user(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    written = _write_key(tmp_path / "host_key")
    (loaded,) = load_host_keys(["~/host_key"])
    assert loaded.get_fingerprint() == written.get_fingerprint()


def test_load_host_keys_accepts_an_empty_list() -> None:
    assert load_host_keys([]) == []


@posix_only
def test_load_host_keys_warns_about_a_world_readable_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "host_key"
    _write_key(path)
    path.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="wijjit_ssh"):
        load_host_keys([path])
    assert "accessible to group/other" in caplog.text


@posix_only
def test_load_host_keys_is_quiet_about_a_private_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "host_key"
    _write_key(path)
    path.chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="wijjit_ssh"):
        load_host_keys([path])
    assert "accessible to group/other" not in caplog.text


# -- ensure_host_key -----------------------------------------------------------


def test_ensure_host_key_generates_on_first_run(tmp_path) -> None:
    path = tmp_path / "host_key"
    key = ensure_host_key(path)
    assert path.exists()
    assert key.algorithm == DEFAULT_HOST_KEY_ALGORITHM.encode()


def test_ensure_host_key_is_stable_across_runs(tmp_path) -> None:
    """The whole point: a restart must not change the server's identity."""
    path = tmp_path / "host_key"
    first = ensure_host_key(path)
    second = ensure_host_key(path)
    assert first.get_fingerprint() == second.get_fingerprint()


def test_ensure_host_key_creates_parent_directories(tmp_path) -> None:
    path = tmp_path / "state" / "keys" / "host_key"
    ensure_host_key(path)
    assert path.exists()


def test_ensure_host_key_warns_loudly_when_generating(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Generation is WARNING, not INFO: on a healthy server it happens once."""
    with caplog.at_level(logging.INFO, logger="wijjit_ssh"):
        ensure_host_key(tmp_path / "host_key")
    generated = [r for r in caplog.records if "Generated a new" in r.message]
    assert len(generated) == 1
    assert generated[0].levelno == logging.WARNING


def test_ensure_host_key_does_not_warn_when_loading(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "host_key"
    ensure_host_key(path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="wijjit_ssh"):
        ensure_host_key(path)
    assert "Generated a new" not in caplog.text


@posix_only
def test_ensure_host_key_writes_a_private_key_file(tmp_path) -> None:
    """0600 from creation, not narrowed afterwards."""
    path = tmp_path / "host_key"
    ensure_host_key(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_host_key_rejects_an_unknown_algorithm(tmp_path) -> None:
    with pytest.raises(ValueError) as excinfo:
        ensure_host_key(tmp_path / "host_key", algorithm="bogus-alg")
    assert "bogus-alg" in str(excinfo.value)


def test_ensure_host_key_surfaces_a_corrupt_existing_key(tmp_path) -> None:
    """Must not silently replace it: that would change the server's identity."""
    path = tmp_path / "host_key"
    path.write_bytes(b"not a key at all")
    with pytest.raises(ValueError) as excinfo:
        ensure_host_key(path)
    assert str(path) in str(excinfo.value)


def test_ensure_host_key_adopts_a_concurrently_created_key(
    tmp_path, monkeypatch
) -> None:
    """Two processes starting together must agree on one identity.

    Simulates losing the O_EXCL race: the file appears between the exists()
    check and the write.
    """
    path = tmp_path / "host_key"
    winner = asyncssh.generate_private_key("ssh-ed25519")

    real_generate = asyncssh.generate_private_key

    def generate_then_lose_the_race(algorithm):
        path.write_bytes(winner.export_private_key())  # the other process wins
        return real_generate(algorithm)

    monkeypatch.setattr(asyncssh, "generate_private_key", generate_then_lose_the_race)

    adopted = ensure_host_key(path)
    assert adopted.get_fingerprint() == winner.get_fingerprint()


# -- resolve_host_keys ---------------------------------------------------------


def test_resolve_host_keys_accepts_every_supported_source_style(tmp_path) -> None:
    """Pins the three call styles in the tree: live key, path str, PathLike."""
    live = asyncssh.generate_private_key("ssh-ed25519")
    on_disk = _write_key(tmp_path / "from_path")

    resolved = resolve_host_keys([live, str(tmp_path / "from_path")])
    assert [k.get_fingerprint() for k in resolved] == [
        live.get_fingerprint(),
        on_disk.get_fingerprint(),
    ]

    (from_pathlike,) = resolve_host_keys([tmp_path / "from_path"])
    assert from_pathlike.get_fingerprint() == on_disk.get_fingerprint()


def test_resolve_host_keys_accepts_an_empty_list() -> None:
    assert resolve_host_keys([]) == []


def test_resolve_host_keys_reports_a_bad_path_among_good_keys(tmp_path) -> None:
    live = asyncssh.generate_private_key("ssh-ed25519")
    with pytest.raises(FileNotFoundError):
        resolve_host_keys([live, tmp_path / "nope"])


# -- integration with the server ----------------------------------------------


def test_server_resolves_host_key_paths_at_construction(tmp_path) -> None:
    """A bad path must fail where the server is configured, not at listen time."""
    from wijjit_ssh import WijjitSSH

    with pytest.raises(FileNotFoundError):
        WijjitSSH(
            lambda session: None,
            host_keys=[tmp_path / "nope"],
            allow_anonymous=True,
        )


def test_server_reports_a_missing_auth_policy_before_a_bad_host_key(tmp_path) -> None:
    """Omitting auth is the more important error; it must not be masked."""
    from wijjit_ssh import WijjitSSH

    with pytest.raises(ValueError, match="requires an auth policy"):
        WijjitSSH(lambda session: None, host_keys=[tmp_path / "nope"])


async def test_server_without_host_keys_explains_itself(tmp_path) -> None:
    """Empty host_keys is fine to construct but cannot serve."""
    from wijjit_ssh import WijjitSSH

    server = WijjitSSH(lambda session: None, host_keys=[], allow_anonymous=True)
    with pytest.raises(ValueError, match="no host keys"):
        await server.start(host="127.0.0.1", port=0)


# -- end to end ----------------------------------------------------------------


async def test_a_generated_host_key_actually_serves(tmp_path) -> None:
    """A key from ensure_host_key must be usable as a real server identity."""
    key = ensure_host_key(tmp_path / "host_key")

    class _OpenServer(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return False  # this test is about the host key, not auth

    async def handler(process):
        process.exit(0)

    server = await asyncssh.create_server(
        _OpenServer,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=handler,
    )
    try:
        port = server.get_port()
        async with asyncssh.connect(
            "127.0.0.1",
            port,
            known_hosts=None,
            username="tester",
            client_keys=None,
        ) as conn:
            # The server key the client actually saw is the one we generated.
            assert conn.get_server_host_key().get_fingerprint() == key.get_fingerprint()
    finally:
        server.close()
        await server.wait_closed()
