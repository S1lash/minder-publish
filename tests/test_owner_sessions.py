"""Owner-session timing and migration guarantees against actual SQLite."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from minder_publish.errors import PublishError
from minder_publish.server import auth as module
from minder_publish.server.auth import AuthStore, digest
from minder_publish.server.config import Config

DAY = 86400
PASSWORD = "a long owner passphrase"


@pytest.fixture
def clock(monkeypatch):
    value = [1700000000.0]
    monkeypatch.setattr(module.time, "time", lambda: value[0])
    return value


@pytest.fixture
def store(tmp_path, clock):
    result = AuthStore(
        Config(data_dir=tmp_path, secret="test-secret-" * 8, testing=True)
    )
    result.initialize()
    result.bootstrap("owner", PASSWORD)
    return result


def login(store):
    return store.login("owner", PASSWORD, "127.0.0.1")


def row(store, raw):
    with store.connection() as connection:
        result = connection.execute(
            "SELECT * FROM auth_sessions WHERE token_hash=?", (digest(raw),)
        ).fetchone()
    return dict(result) if result else None


def test_new_session_has_idle_and_absolute_limits_without_read_side_renewal(
    store, clock
):
    raw, info = login(store)
    original = row(store, raw)
    assert info["expires_in"] == 90 * DAY
    assert original["expires_at"] == clock[0] + 90 * DAY
    assert original["absolute_expires_at"] == clock[0] + 365 * DAY
    clock[0] += 2 * DAY
    assert store.session(raw).id == digest(raw)
    assert store.subject_active("owner", digest(raw), {})
    assert row(store, raw) == original


def test_trusted_refresh_slides_idle_but_preserves_token_csrf_and_absolute_limit(
    store, clock
):
    raw, _ = login(store)
    original = row(store, raw)
    csrf = store.csrf(raw)
    clock[0] += 89 * DAY
    info = store.refresh_session(raw)
    current = row(store, raw)
    assert info["renewed"] is True
    assert current["expires_at"] == clock[0] + 90 * DAY
    assert current["absolute_expires_at"] == original["absolute_expires_at"]
    assert current["authenticated_at"] == original["authenticated_at"]
    assert store.csrf(raw) == csrf and store.session(raw).id == digest(raw)
    for _ in range(3):
        clock[0] += 89 * DAY
        store.refresh_session(raw)
    assert row(store, raw)["expires_at"] == original["absolute_expires_at"]
    clock[0] = original["absolute_expires_at"]
    assert store.session(raw) is None
    assert not store.subject_active("owner", digest(raw), {})
    with pytest.raises(PublishError) as failure:
        store.refresh_session(raw)
    assert failure.value.status == 401


def test_refresh_cooldown_and_expired_session_cannot_be_revived(store, clock):
    raw, _ = login(store)
    original = row(store, raw)
    clock[0] += 899
    assert store.refresh_session(raw)["renewed"] is False
    assert row(store, raw) == original
    clock[0] += 1
    assert store.refresh_session(raw)["renewed"] is True
    deadline = row(store, raw)["expires_at"]
    clock[0] = deadline
    assert store.session(raw) is None
    with pytest.raises(PublishError):
        store.refresh_session(raw)
    assert row(store, raw)["expires_at"] == deadline


def test_recent_auth_requires_real_reauthentication_not_activity(store, clock):
    raw, _ = login(store)
    assert store.recent_auth(raw)
    clock[0] += 600
    assert not store.recent_auth(raw)
    with pytest.raises(PublishError) as failure:
        store.require_recent_auth(raw)
    assert failure.value.code == "reauthentication_required"
    clock[0] += 301
    store.refresh_session(raw)
    assert not store.recent_auth(raw)
    before = row(store, raw)
    with pytest.raises(PublishError):
        store.reauthenticate(raw, "wrong password", "127.0.0.1")
    assert row(store, raw) == before
    csrf = store.csrf(raw)
    info = store.reauthenticate(raw, PASSWORD, "127.0.0.1")
    assert info["authenticated_at"] == clock[0]
    assert store.recent_auth(raw) and store.csrf(raw) == csrf
    assert row(store, raw)["absolute_expires_at"] == before["absolute_expires_at"]


def test_migration_preserves_fixed_expiry_until_explicit_trusted_refresh(
    tmp_path, clock
):
    config = Config(data_dir=tmp_path, secret="test-secret-" * 8, testing=True)
    database = sqlite3.connect(config.db_path)
    migration = Path(module.__file__).parent / "migrations/auth/001_up.sql"
    database.executescript(migration.read_text())
    raw = "legacy-owner-session"
    created = clock[0] - 10 * DAY
    fixed_expiry = clock[0] + 20 * DAY
    database.execute(
        "INSERT INTO auth_sessions VALUES(?,?,?)", (digest(raw), created, fixed_expiry)
    )
    database.execute(
        "INSERT INTO auth_sessions VALUES(?,?,?)",
        (digest("expired"), created, clock[0]),
    )
    database.commit()
    database.close()
    store = AuthStore(config)
    store.initialize()
    original = row(store, raw)
    assert original["expires_at"] == fixed_expiry
    assert original["absolute_expires_at"] == created + 365 * DAY
    assert store.session(raw) and row(store, raw) == original
    assert store.session("expired") is None
    with pytest.raises(PublishError):
        store.refresh_session("expired")
    assert store.refresh_session(raw)["renewed"] is True
    assert row(store, raw)["expires_at"] == clock[0] + 90 * DAY
    store.initialize()
    assert row(store, raw)["expires_at"] == clock[0] + 90 * DAY
    with store.connection() as connection:
        assert (
            connection.execute("SELECT version FROM auth_schema_version").fetchone()[0]
            == 2
        )


def test_concurrent_refreshes_renew_once_and_logout_cannot_be_undone(store, clock):
    raw, _ = login(store)
    clock[0] += DAY
    barrier = threading.Barrier(2)

    def refresh():
        barrier.wait(timeout=5)
        return store.refresh_session(raw)["renewed"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(refresh) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    barrier = threading.Barrier(2)

    def race_refresh():
        barrier.wait(timeout=5)
        try:
            store.refresh_session(raw)
        except PublishError:
            pass

    def race_logout():
        barrier.wait(timeout=5)
        store.logout(raw)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(race_refresh), executor.submit(race_logout)]
        for future in futures:
            future.result(timeout=10)
    assert store.session(raw) is None and row(store, raw) is None


def test_reauthentication_cannot_resurrect_a_session_reset_during_password_verification(
    store, clock, monkeypatch
):
    raw, _ = login(store)
    original_verify = store.verify_password

    def reset_while_verifying(password_hash, password):
        valid = original_verify(password_hash, password)
        store.bootstrap("owner", "a different long password", reset=True)
        return valid

    monkeypatch.setattr(store, "verify_password", reset_while_verifying)
    with pytest.raises(PublishError) as failure:
        store.reauthenticate(raw, PASSWORD, "127.0.0.1")
    assert failure.value.status == 401
    assert store.session(raw) is None and row(store, raw) is None


def test_reset_hooks_share_transaction_and_ordinary_logout_does_not_invoke_them(store):
    calls = []
    with store.connection(write=True) as connection:
        connection.execute("CREATE TABLE own_grant (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO own_grant VALUES(1)")

    def invalidate(connection, *, devices):
        calls.append(devices)
        connection.execute("DELETE FROM own_grant")

    store.register_invalidation_hook(invalidate)
    raw, _ = login(store)
    store.logout(raw)
    assert calls == []
    store.bootstrap("owner", "a new long passphrase", reset=True)
    assert calls == [True]
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM own_grant").fetchone()[0] == 0


def test_failed_reset_hook_rolls_back_password_sessions_and_related_grants(store):
    raw, _ = login(store)
    device = store.create_token("laptop", "materials:manage")

    def failing_hook(connection, *, devices):
        raise RuntimeError("grant revocation failed")

    store.register_invalidation_hook(failing_hook)
    with pytest.raises(RuntimeError):
        store.bootstrap("changed-owner", "a replacement passphrase", reset=True)
    assert store.username() == "owner"
    assert store.session(raw) and store.token(device["token"])
    assert login(store)


def test_refresh_preserves_owner_reader_and_independent_guest_lifetimes(store, clock):
    raw, _ = login(store)
    viewer_raw, viewer = store.viewer(None)
    publication = {"id": "one", "access": "password", "access_version": 1}
    store.unlock(viewer, publication)
    payload = {
        "publication_id": "one",
        "revision_id": "revision",
        "subject_kind": "owner",
        "subject_id": digest(raw),
    }
    intent = store.intent(payload)
    code, _ = store.authorize_intent(intent, digest("nonce"), lambda value: True)
    reader, _ = store.consume_handoff(code, "nonce", "revision", lambda value: True)
    with store.connection() as connection:
        guest_before = tuple(
            connection.execute(
                "SELECT expires_at FROM auth_viewers WHERE token_hash=?", (viewer,)
            ).fetchone()
        )
        reader_before = tuple(
            connection.execute(
                "SELECT expires_at FROM auth_readers WHERE token_hash=?",
                (digest(reader),),
            ).fetchone()
        )
    clock[0] += 901
    store.refresh_session(raw)
    assert store.reader(reader) == payload
    assert store.subject_active("owner", payload["subject_id"], publication)
    assert store.existing_viewer(viewer_raw) == viewer
    assert store.unlocked(viewer, publication)
    with store.connection() as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT expires_at FROM auth_viewers WHERE token_hash=?", (viewer,)
                ).fetchone()
            )
            == guest_before
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT expires_at FROM auth_readers WHERE token_hash=?",
                    (digest(reader),),
                ).fetchone()
            )
            == reader_before
        )
    clock[0] += 12 * 3600
    assert store.reader(reader) is None and store.existing_viewer(viewer_raw) is None
    assert store.session(raw)


def test_login_cannot_create_session_from_password_reset_during_verification(
    store, monkeypatch
):
    original_verify = store.verify_password

    def reset_while_verifying(password_hash, password):
        valid = original_verify(password_hash, password)
        store.bootstrap("owner", "a replacement owner password", reset=True)
        return valid

    monkeypatch.setattr(store, "verify_password", reset_while_verifying)
    with pytest.raises(PublishError) as failure:
        login(store)
    assert failure.value.status == 401
    with store.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        )


def test_recent_auth_can_guard_related_operation_in_its_existing_transaction(
    store, clock
):
    raw, _ = login(store)
    with store.connection(write=True) as connection:
        assert store.require_recent_auth(raw, connection=connection).id == digest(raw)
    clock[0] += 600
    with store.connection(write=True) as connection:
        with pytest.raises(PublishError) as failure:
            store.require_recent_auth(raw, connection=connection)
    assert failure.value.code == "reauthentication_required"


def test_expired_session_cannot_reauthenticate_or_restore_recent_auth(
    store, clock, monkeypatch
):
    raw, _ = login(store)
    clock[0] = row(store, raw)["expires_at"]
    monkeypatch.setattr(
        store,
        "verify_password",
        lambda *args: pytest.fail("expired session must fail before Argon2"),
    )
    with pytest.raises(PublishError) as failure:
        store.reauthenticate(raw, PASSWORD, "127.0.0.1")
    assert failure.value.status == 401
    assert not store.recent_auth(raw)


def test_fresh_operator_auth_store_automatically_revokes_persisted_oauth_state(
    store, clock
):
    from minder_publish.server.oauth.store import OAuthStore

    oauth = OAuthStore(store.config, clock=lambda: clock[0]).initialize()
    raw = "a" * 40
    expiry = int(clock[0] + DAY)
    with store.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO oauth_family VALUES(?,?,?,?,?,?,?,NULL)",
            (
                "family",
                "client",
                '["materials:read"]',
                "owner",
                oauth.resource,
                int(clock[0]),
                expiry,
            ),
        )
        connection.execute(
            "INSERT INTO oauth_token VALUES(?,?,?,?,?,?,NULL)",
            (
                digest(raw),
                "access",
                "family",
                '["materials:read"]',
                int(clock[0]),
                expiry,
            ),
        )
        connection.execute(
            "INSERT INTO oauth_pending VALUES(?,?,?,?,NULL)",
            ("pending", "client", "{}", expiry),
        )
        connection.execute(
            "INSERT INTO oauth_code VALUES(?,?,?,?,NULL,NULL)",
            ("code", "client", "{}", expiry),
        )
    assert oauth.token(raw)
    # A CLI/restore-created AuthStore must not depend on application hook wiring.
    operator = AuthStore(store.config)
    operator.initialize()
    operator.bootstrap("owner", "a replacement owner password", reset=True)
    assert oauth.token(raw) is None
    with store.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM oauth_pending").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM oauth_code").fetchone()[0] == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("owner_session_idle_days", 0),
        ("owner_session_absolute_days", 89),
        ("owner_session_refresh_seconds", True),
        ("owner_recent_auth_seconds", -1),
    ],
)
def test_invalid_session_policy_fails_closed(store, key, value):
    store.config.operations[key] = value
    with pytest.raises(PublishError) as failure:
        store.initialize()
    assert failure.value.code == "invalid_session_policy"


def test_cleanup_removes_absolute_expiry_even_if_idle_field_is_later(store, clock):
    raw, _ = login(store)
    with store.connection(write=True) as connection:
        connection.execute(
            "UPDATE auth_sessions SET expires_at=?,absolute_expires_at=? WHERE token_hash=?",
            (clock[0] + DAY, clock[0], digest(raw)),
        )
    assert store.session(raw) is None
    store.cleanup()
    assert row(store, raw) is None
