"""Tests for :mod:`wijjit_ssh.auth`.

Two layers:

1. **Unit** - each policy in isolation, no sockets. Fast, and the place to prove
   the fail-closed behaviour of every branch.
2. **Over real SSH** - a genuine asyncssh client authenticating (or failing to)
   against a genuine server. Unit tests prove the policy says "no"; these prove
   the server actually *refuses the connection*, which is the property that
   matters and which a policy test alone cannot establish.
"""

from __future__ import annotations

import asyncssh
import pytest
from wijjit import Wijjit, render_template_string

from wijjit_ssh import SSHSession, WijjitSSH
from wijjit_ssh.auth import (
    AuthorizedKeys,
    AuthPolicy,
    ChainAuth,
    OpenAuth,
    PasswordAuth,
    check_password,
    load_authorized_keys,
)

TEMPLATE = "{% frame %}{% text %}Hi {{ who }}!{% endtext %}{% endframe %}"


def make_app(session: SSHSession) -> Wijjit:
    app = Wijjit(backend=session.backend)

    @app.view("main", default=True)
    def main():
        return render_template_string(TEMPLATE, who=session.username)

    return app


@pytest.fixture
def keypair() -> asyncssh.SSHKey:
    """A throwaway client keypair."""
    return asyncssh.generate_private_key("ssh-ed25519")


def public_of(key: asyncssh.SSHKey) -> asyncssh.SSHKey:
    """The public half of a private key."""
    return key.convert_to_public()


# ---------------------------------------------------------------------------
# check_password
# ---------------------------------------------------------------------------


def test_check_password_matches() -> None:
    assert check_password("hunter2", "hunter2") is True


def test_check_password_rejects() -> None:
    assert check_password("hunter2", "hunter3") is False
    assert check_password("", "hunter2") is False
    assert check_password("hunter2", "") is False


def test_check_password_handles_non_ascii() -> None:
    assert check_password("pässwörd", "pässwörd") is True
    assert check_password("pässwörd", "password") is False


# ---------------------------------------------------------------------------
# The base policy denies everything
# ---------------------------------------------------------------------------


async def test_base_policy_fails_closed() -> None:
    """A subclass that forgets to enable a method must deny, not allow."""
    policy = AuthPolicy()

    assert policy.auth_required("anyone") is True
    assert policy.password_supported() is False
    assert policy.public_key_supported() is False
    assert policy.kbdint_supported() is False
    assert await policy.verify_password("anyone", "secret") is False
    assert policy.authorized_keys_for("anyone") is None
    assert await policy.verify_kbdint("anyone", ["secret"]) is False


# ---------------------------------------------------------------------------
# OpenAuth
# ---------------------------------------------------------------------------


def test_open_auth_requires_nothing() -> None:
    assert OpenAuth().auth_required("anyone") is False


# ---------------------------------------------------------------------------
# AuthorizedKeys
# ---------------------------------------------------------------------------


def test_authorized_keys_needs_a_source() -> None:
    with pytest.raises(ValueError, match="requires a path"):
        AuthorizedKeys()


def test_authorized_keys_from_keys(keypair: asyncssh.SSHKey) -> None:
    policy = AuthorizedKeys(keys=[public_of(keypair)])

    assert policy.public_key_supported() is True
    assert policy.auth_required("anyone") is True
    assert policy.authorized_keys_for("anyone") == [public_of(keypair)]


def test_authorized_keys_from_file(tmp_path, keypair: asyncssh.SSHKey) -> None:
    path = tmp_path / "authorized_keys"
    path.write_bytes(public_of(keypair).export_public_key())

    policy = AuthorizedKeys(path)
    keys = policy.authorized_keys_for("anyone")

    assert keys is not None
    assert keys == [public_of(keypair)]


def test_authorized_keys_per_user_mapping(tmp_path) -> None:
    alice_key = asyncssh.generate_private_key("ssh-ed25519")
    bob_key = asyncssh.generate_private_key("ssh-ed25519")

    alice_path = tmp_path / "alice.pub"
    bob_path = tmp_path / "bob.pub"
    alice_path.write_bytes(public_of(alice_key).export_public_key())
    bob_path.write_bytes(public_of(bob_key).export_public_key())

    policy = AuthorizedKeys({"alice": alice_path, "bob": bob_path})

    assert policy.authorized_keys_for("alice") == [public_of(alice_key)]
    assert policy.authorized_keys_for("bob") == [public_of(bob_key)]
    # An unknown user gets None, which the server treats as "deny" - never as
    # "no keys required".
    assert policy.authorized_keys_for("mallory") is None


def test_authorized_keys_missing_file_is_fatal(tmp_path) -> None:
    """A typo'd path must fail loudly at startup, not deny everyone at login."""
    with pytest.raises(FileNotFoundError):
        AuthorizedKeys(tmp_path / "nope")


def test_authorized_keys_skips_comments_and_blanks(
    tmp_path, keypair: asyncssh.SSHKey
) -> None:
    entry = public_of(keypair).export_public_key().decode()
    path = tmp_path / "authorized_keys"
    path.write_text(f"# a comment\n\n{entry}\n\n")

    assert load_authorized_keys(path) == [public_of(keypair)]


def test_authorized_keys_handles_key_options(
    tmp_path, keypair: asyncssh.SSHKey
) -> None:
    """Real authorized_keys files carry options; they must not break parsing."""
    entry = public_of(keypair).export_public_key().decode().strip()
    path = tmp_path / "authorized_keys"
    path.write_text(f'no-pty,from="10.0.0.0/8" {entry}\n')

    assert load_authorized_keys(path) == [public_of(keypair)]


def test_authorized_keys_skips_bad_lines_without_locking_everyone_out(
    tmp_path, keypair: asyncssh.SSHKey
) -> None:
    entry = public_of(keypair).export_public_key().decode().strip()
    path = tmp_path / "authorized_keys"
    path.write_text(f"this-is-not-a-key\n{entry}\n")

    assert load_authorized_keys(path) == [public_of(keypair)]


# ---------------------------------------------------------------------------
# PasswordAuth
# ---------------------------------------------------------------------------


async def test_password_auth_with_sync_checker() -> None:
    policy = PasswordAuth(lambda user, pw: user == "alice" and pw == "s3cret")

    assert policy.password_supported() is True
    assert await policy.verify_password("alice", "s3cret") is True
    assert await policy.verify_password("alice", "wrong") is False
    assert await policy.verify_password("mallory", "s3cret") is False


async def test_password_auth_with_async_checker() -> None:
    async def check(username: str, password: str) -> bool:
        return username == "alice" and check_password(password, "s3cret")

    policy = PasswordAuth(check)

    assert await policy.verify_password("alice", "s3cret") is True
    assert await policy.verify_password("alice", "wrong") is False


async def test_password_auth_offers_keyboard_interactive() -> None:
    policy = PasswordAuth(lambda user, pw: pw == "s3cret")

    assert policy.kbdint_supported() is True
    assert await policy.verify_kbdint("alice", ["s3cret"]) is True
    assert await policy.verify_kbdint("alice", ["wrong"]) is False
    # No response at all must not be treated as an empty-password match.
    assert await policy.verify_kbdint("alice", []) is False


async def test_password_auth_can_disable_keyboard_interactive() -> None:
    policy = PasswordAuth(lambda user, pw: True, keyboard_interactive=False)
    assert policy.kbdint_supported() is False


# ---------------------------------------------------------------------------
# ChainAuth
# ---------------------------------------------------------------------------


def test_chain_auth_requires_a_policy() -> None:
    with pytest.raises(ValueError, match="at least one policy"):
        ChainAuth()


async def test_chain_auth_accepts_if_any_accepts(keypair: asyncssh.SSHKey) -> None:
    chain = ChainAuth(
        AuthorizedKeys(keys=[public_of(keypair)]),
        PasswordAuth(lambda user, pw: pw == "s3cret"),
    )

    assert chain.public_key_supported() is True
    assert chain.password_supported() is True
    assert chain.authorized_keys_for("alice") == [public_of(keypair)]
    assert await chain.verify_password("alice", "s3cret") is True
    assert await chain.verify_password("alice", "wrong") is False


async def test_chain_auth_still_requires_auth() -> None:
    chain = ChainAuth(PasswordAuth(lambda user, pw: False))
    assert chain.auth_required("alice") is True


async def test_chain_auth_with_open_auth_requires_nothing() -> None:
    """Documented (and warned about): OpenAuth in a chain bypasses the rest."""
    chain = ChainAuth(PasswordAuth(lambda user, pw: False), OpenAuth())
    assert chain.auth_required("alice") is False


# ---------------------------------------------------------------------------
# Fail-closed construction
# ---------------------------------------------------------------------------


def test_server_refuses_to_run_without_an_auth_policy() -> None:
    """Forgetting `auth=` must not silently serve an open SSH server."""
    with pytest.raises(ValueError, match="requires an auth policy"):
        WijjitSSH(make_app, host_keys=[])


def test_server_allows_anonymous_when_asked_explicitly() -> None:
    WijjitSSH(make_app, host_keys=[], allow_anonymous=True)


# ---------------------------------------------------------------------------
# Over real SSH
# ---------------------------------------------------------------------------


async def _serve(policy: AuthPolicy | None, **kwargs) -> asyncssh.SSHAcceptor:
    """Start a server on an ephemeral port with the given policy."""
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = WijjitSSH(make_app, host_keys=[host_key], auth=policy, **kwargs)
    return await server.start(host="127.0.0.1", port=0)


async def test_public_key_auth_accepts_an_authorized_key(
    keypair: asyncssh.SSHKey,
) -> None:
    acceptor = await _serve(AuthorizedKeys(keys=[public_of(keypair)]))
    try:
        conn = await asyncssh.connect(
            "127.0.0.1",
            port=acceptor.get_port(),
            username="alice",
            client_keys=[keypair],
            known_hosts=None,
        )
        async with conn:
            assert conn.get_extra_info("username") == "alice"
    finally:
        acceptor.close()


async def test_public_key_auth_rejects_an_unauthorized_key(
    keypair: asyncssh.SSHKey,
) -> None:
    """A key the server has never heard of must not get in."""
    stranger = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await _serve(AuthorizedKeys(keys=[public_of(keypair)]))
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                port=acceptor.get_port(),
                username="alice",
                client_keys=[stranger],
                known_hosts=None,
            )
    finally:
        acceptor.close()


async def test_public_key_auth_rejects_an_unknown_user(
    tmp_path, keypair: asyncssh.SSHKey
) -> None:
    """Per-user keys: the right key under the wrong username is still refused."""
    path = tmp_path / "alice.pub"
    path.write_bytes(public_of(keypair).export_public_key())

    acceptor = await _serve(AuthorizedKeys({"alice": path}))
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                port=acceptor.get_port(),
                username="mallory",
                client_keys=[keypair],
                known_hosts=None,
            )
    finally:
        acceptor.close()


async def test_password_auth_accepts_the_right_password() -> None:
    policy = PasswordAuth(
        lambda user, pw: user == "alice" and check_password(pw, "s3cret")
    )
    acceptor = await _serve(policy)
    try:
        conn = await asyncssh.connect(
            "127.0.0.1",
            port=acceptor.get_port(),
            username="alice",
            password="s3cret",
            client_keys=[],
            known_hosts=None,
        )
        async with conn:
            assert conn.get_extra_info("username") == "alice"
    finally:
        acceptor.close()


async def test_password_auth_rejects_the_wrong_password() -> None:
    policy = PasswordAuth(
        lambda user, pw: user == "alice" and check_password(pw, "s3cret")
    )
    acceptor = await _serve(policy)
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                port=acceptor.get_port(),
                username="alice",
                password="wrong",
                client_keys=[],
                known_hosts=None,
            )
    finally:
        acceptor.close()


async def test_chain_auth_over_real_ssh(keypair: asyncssh.SSHKey) -> None:
    """Either credential gets in; a bad one does not."""
    chain = ChainAuth(
        AuthorizedKeys(keys=[public_of(keypair)]),
        PasswordAuth(lambda user, pw: check_password(pw, "s3cret")),
    )
    acceptor = await _serve(chain)
    try:
        port = acceptor.get_port()

        by_key = await asyncssh.connect(
            "127.0.0.1",
            port=port,
            username="alice",
            client_keys=[keypair],
            known_hosts=None,
        )
        by_key.close()

        by_password = await asyncssh.connect(
            "127.0.0.1",
            port=port,
            username="bob",
            password="s3cret",
            client_keys=[],
            known_hosts=None,
        )
        by_password.close()

        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1",
                port=port,
                username="mallory",
                password="wrong",
                client_keys=[],
                known_hosts=None,
            )
    finally:
        acceptor.close()


async def test_anonymous_server_lets_anyone_in() -> None:
    """The escape hatch still works when explicitly requested."""
    acceptor = await _serve(None, allow_anonymous=True)
    try:
        conn = await asyncssh.connect(
            "127.0.0.1",
            port=acceptor.get_port(),
            username="whoever",
            client_keys=[],
            known_hosts=None,
        )
        async with conn:
            assert conn.get_extra_info("username") == "whoever"
    finally:
        acceptor.close()
