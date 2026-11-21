"""Real SQLite authentication regressions; transport/cookie tests live at the HTTP boundary."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from minder_publish.errors import PublishError
from minder_publish.server import auth as module
from minder_publish.server.auth import AuthStore, Principal, digest
from minder_publish.server.config import Config


@pytest.fixture
def store(tmp_path):
    config = Config(data_dir=tmp_path, secret="test-secret-" * 8, testing=True)
    result = AuthStore(config)
    result.initialize()
    return result


def owner_session(store):
    store.bootstrap("owner", "a long test passphrase")
    return store.login("owner", "a long test passphrase", "127.0.0.1")[0]


def test_navigation_purpose_cannot_enter_normal_handoff_even_with_permissive_validator(
    store,
):
    raw = store.intent({"purpose": "navigation"})
    with pytest.raises(PublishError) as peek:
        store.peek_intent(raw)
    assert peek.value.code == "invalid_handoff"
    with pytest.raises(PublishError) as authorize:
        store.authorize_intent(raw, digest("nonce"), lambda payload: True)
    assert authorize.value.code == "handoff_denied"


def grant(store, payload, nonce="browser-nonce"):
    intent = store.intent(payload)
    code, _ = store.authorize_intent(intent, digest(nonce), lambda value: True)
    reader, _ = store.consume_handoff(
        code, nonce, payload["revision_id"], lambda value: True
    )
    return intent, code, reader


def test_bootstrap_login_credentials_are_hashed_and_csrf_bound(store):
    raw = owner_session(store)
    principal = store.session(raw)
    assert principal.owner and principal.id == digest(raw)
    assert store.validate_csrf(raw, store.csrf(raw))
    assert not store.validate_csrf(raw, store.csrf("different-session"))
    with store.connection() as connection:
        dump = "\n".join(connection.iterdump())
    assert raw not in dump and "a long test passphrase" not in dump
    with pytest.raises(PublishError) as raised:
        store.bootstrap("another", "another long passphrase")
    assert raised.value.code == "owner_exists"


def test_unicode_csrf_input_is_rejected_without_server_exception(store):
    assert store.validate_csrf("session", "é" * 64) is False


def test_unknown_bearer_with_unicode_is_rejected_without_exception(store):
    assert store.token("é" * 20) is None


def test_device_profiles_never_authorize_account_operations():
    read = Principal("device", "read", "materials:read")
    manage = Principal("device", "manage", "materials:manage")
    assert read.can("read") and not read.can("manage")
    assert manage.can("read") and manage.can("manage")
    for operation in ("settings", "tokens", "account", "unknown"):
        assert not read.can(operation)
        assert not manage.can(operation)


def test_device_token_scope_hash_redaction_and_independent_revocation(store):
    first = store.create_token("laptop", "materials:read")
    second = store.create_token("desktop", "materials:manage")
    assert not store.token(first["token"]).owner
    assert store.token(first["token"]).scope == "materials:read"
    listing = store.list_tokens()
    assert all("token" not in item and "token_hash" not in item for item in listing)
    with store.connection() as connection:
        dump = "\n".join(connection.iterdump())
    assert first["token"] not in dump and second["token"] not in dump
    store.revoke_token(first["id"])
    assert store.token(first["token"]) is None
    assert store.token(second["token"]) is not None


def test_device_token_creation_authorizes_inside_write_transaction(store):
    session = owner_session(store)

    def authorize(connection):
        assert connection.in_transaction
        return store.require_recent_auth(session, connection=connection)

    def deny(connection):
        assert connection.in_transaction
        raise PublishError("reauthentication_required", 403)

    with pytest.raises(PublishError) as rejected:
        store.create_token("denied", "materials:read", authorize=deny)
    assert rejected.value.code == "reauthentication_required"
    assert store.list_tokens() == []

    issued = store.create_token("laptop", "materials:read", authorize=authorize)
    assert store.token(issued["token"]).id == issued["id"]


def test_short_throttle_window_cannot_reset_long_login_window(store, monkeypatch):
    instant = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: instant[0])
    store.throttle("login:peer", maximum=1, seconds=900)
    instant[0] += 61
    store.throttle("short:peer", maximum=10, seconds=60)
    with pytest.raises(PublishError) as raised:
        store.throttle("login:peer", maximum=1, seconds=900)
    assert raised.value.code == "rate_limited"


def test_guest_unlock_is_publication_and_access_version_bound(store):
    raw, viewer = store.viewer(None)
    publication = {"id": "one", "access": "password", "access_version": 1}
    store.unlock(viewer, publication)
    assert store.existing_viewer(raw) == viewer
    assert store.unlocked(viewer, publication)
    assert not store.unlocked(viewer, {**publication, "access_version": 2})
    assert not store.unlocked(viewer, {**publication, "id": "two"})
    assert store.subject_active("guest", viewer, publication)
    assert not store.subject_active(
        "guest", viewer, {**publication, "access_version": 2}
    )


def test_handoff_binds_nonce_revision_and_consumes_once(store):
    payload = {
        "publication_id": "one",
        "revision_id": "rev-one",
        "access_version": 1,
        "subject_kind": "anonymous",
        "subject_id": "",
    }
    intent = store.intent(payload)
    code, _ = store.authorize_intent(
        intent, digest("correct-nonce"), lambda value: True
    )
    with pytest.raises(PublishError):
        store.authorize_intent(intent, digest("other-nonce"), lambda value: True)
    with pytest.raises(PublishError):
        store.consume_handoff(code, "wrong-nonce", "rev-one", lambda value: True)
    with pytest.raises(PublishError):
        store.consume_handoff(code, "correct-nonce", "rev-two", lambda value: True)
    reader, result = store.consume_handoff(
        code, "correct-nonce", "rev-one", lambda value: True
    )
    assert result == payload and store.reader(reader) == payload
    with pytest.raises(PublishError):
        store.consume_handoff(code, "correct-nonce", "rev-one", lambda value: True)
    with store.connection() as connection:
        dump = "\n".join(connection.iterdump())
    assert code not in dump and reader not in dump and "correct-nonce" not in dump


@pytest.mark.parametrize("table", ["auth_intents", "auth_handoffs", "auth_readers"])
def test_capability_admission_reserves_bounded_owner_headroom(store, monkeypatch, table):
    monkeypatch.setattr(module, "_OWNER_CAPABILITY_HEADROOM", 1)
    owner = {"revision_id": "rev-one", "subject_kind": "owner"}
    public = {"revision_id": "rev-one"}

    def check_capacity(payload, *, available):
        with store.connection(write=True) as connection:
            if available:
                store._admit(connection, table, 1, payload=payload)
                return
            with pytest.raises(PublishError) as rejected:
                store._admit(connection, table, 1, payload=payload)
            assert rejected.value.code == "auth_capacity"

    grant(store, public)
    check_capacity(public, available=False)
    check_capacity(owner, available=True)
    grant(store, owner)
    check_capacity(owner, available=False)


def test_concurrent_handoff_consumers_cannot_both_succeed(store):
    intent = store.intent({"publication_id": "one", "revision_id": "rev"})
    code, _ = store.authorize_intent(intent, digest("nonce"), lambda value: True)
    barrier = threading.Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        try:
            store.consume_handoff(code, "nonce", "rev", lambda value: True)
            return True
        except PublishError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(consume) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]


def test_logout_revokes_owner_readers_but_preserves_independent_guest(store):
    owner = owner_session(store)
    _, viewer = store.viewer(None)
    publication = {"id": "one", "access": "password", "access_version": 1}
    store.unlock(viewer, publication)
    owner_payload = {
        "publication_id": "one",
        "revision_id": "rev",
        "subject_kind": "owner",
        "subject_id": digest(owner),
    }
    guest_payload = {
        "publication_id": "one",
        "revision_id": "rev",
        "subject_kind": "guest",
        "subject_id": viewer,
    }
    _, _, owner_reader = grant(store, owner_payload)
    _, _, guest_reader = grant(store, guest_payload)
    store.logout(owner)
    assert store.session(owner) is None and store.reader(owner_reader) is None
    assert store.reader(guest_reader) == guest_payload
    assert store.subject_active("guest", viewer, publication)


def test_local_reset_invalidates_restored_sessions_grants_and_device_tokens(store):
    raw = owner_session(store)
    device = store.create_token("laptop", "materials:manage")
    _, _, reader = grant(
        store,
        {
            "publication_id": "one",
            "revision_id": "rev",
            "subject_kind": "owner",
            "subject_id": digest(raw),
        },
    )
    store.bootstrap("owner", "new long test passphrase", reset=True)
    assert (
        store.session(raw) is None
        and store.reader(reader) is None
        and store.token(device["token"]) is None
    )
    assert store.login("owner", "new long test passphrase", "127.0.0.2")[0]


def test_unknown_auth_schema_refused_without_reinitialization(store):
    with store.connection(write=True) as connection:
        connection.execute("UPDATE auth_schema_version SET version=999")
    with pytest.raises(PublishError) as raised:
        store.initialize()
    assert raised.value.code == "unsupported_auth_schema"


@pytest.mark.parametrize(
    "table,maximum",
    [
        ("auth_viewers", 10000),
        ("auth_intents", 10000),
        ("auth_handoffs", 10000),
        ("auth_readers", 20000),
    ],
)
def test_public_auth_state_has_bounded_live_admission(store, table, maximum):
    import time

    expires = time.time() + 3600
    payload = {"publication_id": "one", "revision_id": "rev"}
    if table == "auth_handoffs":
        intent = store.intent(payload)
    elif table == "auth_readers":
        intent = store.intent(payload)
        code, _ = store.authorize_intent(intent, digest("nonce"), lambda value: True)
    with store.connection(write=True) as connection:
        if table == "auth_viewers":
            connection.executemany(
                "INSERT INTO auth_viewers VALUES(?,?)",
                ((f"fake-{i}", expires) for i in range(maximum)),
            )
        elif table == "auth_intents":
            connection.executemany(
                "INSERT INTO auth_intents VALUES(?,?,?,0)",
                ((f"fake-{i}", "{}", expires) for i in range(maximum)),
            )
        elif table == "auth_handoffs":
            connection.executemany(
                "INSERT INTO auth_handoffs VALUES(?,?,?,?,0)",
                ((f"fake-{i}", "{}", digest("nonce"), expires) for i in range(maximum)),
            )
        else:
            connection.executemany(
                "INSERT INTO auth_readers VALUES(?,?,?)",
                ((f"fake-{i}", "{}", expires) for i in range(maximum)),
            )
    with pytest.raises(PublishError) as raised:
        if table == "auth_viewers":
            store.viewer(None)
        elif table == "auth_intents":
            store.intent(payload)
        elif table == "auth_handoffs":
            store.authorize_intent(intent, digest("nonce"), lambda value: True)
        else:
            store.consume_handoff(code, "nonce", "rev", lambda value: True)
    assert raised.value.status in (429, 503)
    with store.connection() as connection:
        assert (
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == maximum
        )


def test_unlock_admission_is_bounded_but_existing_pair_can_renew(store):
    import time

    expires = time.time() + 3600
    with store.connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO auth_unlocks VALUES(?,?,?,?)",
            ((f"viewer-{i}", "one", 1, expires) for i in range(20000)),
        )
    with pytest.raises(PublishError) as raised:
        store.unlock("new-viewer", {"id": "one", "access_version": 1})
    assert raised.value.status in (429, 503)
    store.unlock("viewer-0", {"id": "one", "access_version": 2})
    assert store.unlocked("viewer-0", {"id": "one", "access_version": 2})
