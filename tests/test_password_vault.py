"""Recoverable publication secrets stay outside ordinary material responses."""

import copy
import json
import os

import pytest
from test_operations import instance as operations_instance
from test_operations import publish

from minder_publish.errors import PublishError
from minder_publish.server.commands import doctor
from minder_publish.server.core import Core
from minder_publish.server.password_vault import KEY_FILENAME

instance = operations_instance


def owner_copy(core, auth, identifier, cookie=None):
    if cookie is None:
        cookie, _ = auth.login("owner", "correct-long-password", "127.0.0.1")
    return core.copy_publication_password(
        identifier,
        lambda connection: auth.require_recent_auth(cookie, connection=connection),
    )["password"]


def test_password_policy_saves_an_encrypted_copy(instance):
    config, core, auth = instance
    password = "synthetic-publication-secret-秘密"
    material = publish(core, access="password", password=password)
    assert material.get("password_sealed"), "Password Copy needs an encrypted copy"
    with core.repository.connect() as connection:
        stored = connection.execute(
            "SELECT data FROM publication WHERE id=?", (material["id"],)
        ).fetchone()["data"]
    assert password not in stored
    assert json.loads(stored)["password_hash"] == material["password_hash"]
    assert owner_copy(core, auth, material["id"]) == password
    key = config.data_dir / KEY_FILENAME
    assert key.stat().st_size == 32 and key.stat().st_mode & 0o777 == 0o600


def test_sparse_updates_and_password_changes_keep_hash_and_copy_consistent(instance):
    config, core, auth = instance
    material = publish(core, access="password", password="first password")
    title_edit = core.patch_publication(material["id"], {"title": "An edited title"})
    assert title_edit["password_sealed"] == material["password_sealed"]
    assert title_edit["password_hash"] == material["password_hash"]
    changed = core.patch_publication(material["id"], {"password": "second password"})
    assert changed["password_sealed"] != material["password_sealed"]
    assert changed["password_hash"] != material["password_hash"]
    assert changed["access_version"] == material["access_version"] + 1
    assert auth.verify_password(
        changed["password_hash"], owner_copy(core, auth, material["id"])
    )
    cleared = core.patch_publication(material["id"], {"access": "link"})
    assert cleared["password_hash"] is None and cleared["password_sealed"] is None
    assert not cleared["password_copy_available"]
    with pytest.raises(PublishError, match="password copy unavailable"):
        owner_copy(core, auth, material["id"])


@pytest.mark.parametrize("password", ["", "x" * 1025, "\ud800"])
def test_invalid_password_and_later_validation_rollback_leave_previous_copy(
    instance, password
):
    config, core, auth = instance
    material = publish(core, access="password", password="untouched password")
    with pytest.raises(PublishError):
        core.patch_publication(material["id"], {"password": password})
    with pytest.raises(PublishError):
        core.patch_publication(
            material["id"],
            {"password": "valid new password", "expiry": {"mode": "bogus"}},
        )
    saved = core.get_publication(material["id"])
    assert saved["password_hash"] == material["password_hash"]
    assert saved["password_sealed"] == material["password_sealed"]
    assert owner_copy(core, auth, material["id"]) == "untouched password"


def test_maximum_utf8_password_roundtrips_and_nonce_changes(instance):
    config, core, auth = instance
    password = "秘密" * 170 + "abcd"
    assert len(password.encode()) == 1024
    material = publish(core, access="password", password=password)
    assert owner_copy(core, auth, material["id"]) == password
    first = core.password_vault.seal(material, password)
    second = core.password_vault.seal(material, password)
    assert first != second


@pytest.mark.parametrize("mutation", ["id", "hash", "ciphertext", "version", "shape"])
def test_envelopes_reject_substitution_and_tampering(instance, mutation):
    config, core, auth = instance
    material = publish(core, access="password", password="sealed password")
    damaged = copy.deepcopy(material)
    if mutation == "id":
        damaged["id"] = "another-publication"
    elif mutation == "hash":
        damaged["password_hash"] = "another-password-hash"
    elif mutation == "ciphertext":
        value = damaged["password_sealed"]["sealed"]
        damaged["password_sealed"]["sealed"] = (
            "B" if value[0] == "A" else "A"
        ) + value[1:]
    elif mutation == "version":
        damaged["password_sealed"]["version"] = True
    else:
        damaged["password_sealed"]["unexpected"] = "field"
    with pytest.raises(PublishError) as error:
        core.password_vault.open(damaged)
    assert error.value.code == "publication_password_unavailable"


@pytest.mark.parametrize(
    "key_state", ["missing", "wrong", "truncated", "public", "symlink"]
)
def test_live_key_loss_or_replacement_fails_closed(instance, key_state):
    config, core, auth = instance
    material = publish(core, access="password", password="protected password")
    key = config.data_dir / KEY_FILENAME
    if key_state == "missing":
        key.unlink()
    elif key_state == "wrong":
        key.write_bytes(os.urandom(32))
    elif key_state == "truncated":
        key.write_bytes(b"short")
    elif key_state == "public":
        key.chmod(0o644)
    else:
        original = key.with_name("original-test-key")
        key.rename(original)
        key.symlink_to(original)
    with pytest.raises(PublishError):
        owner_copy(core, auth, material["id"])
    with pytest.raises(PublishError):
        Core(config).initialize()
    if key_state == "missing":
        assert not key.exists()


def test_live_key_replacement_cannot_mix_password_encryption_keys(instance):
    config, core, auth = instance
    material = publish(core, access="password", password="original key password")
    key = config.data_dir / KEY_FILENAME
    key.write_bytes(os.urandom(32))
    with pytest.raises(PublishError):
        core.patch_publication(material["id"], {"password": "must not save"})
    with pytest.raises(PublishError):
        core.create_upload(
            {
                "kind": "file",
                "filename": "new.txt",
                "size_bytes": 3,
                "access": "password",
                "password": "must not stage",
            },
            "operator",
        )
    saved = core.get_publication(material["id"])
    assert saved["password_hash"] == material["password_hash"]
    assert saved["password_sealed"] == material["password_sealed"]


def test_missing_key_is_not_regenerated_for_staged_password(instance):
    config, core, auth = instance
    upload = core.create_upload(
        {
            "kind": "file",
            "filename": "pending.txt",
            "size_bytes": 3,
            "access": "password",
            "password": "pending password",
        },
        "operator",
    )
    with core.repository.connect() as connection:
        staged = connection.execute(
            "SELECT data FROM upload WHERE id=?", (upload["id"],)
        ).fetchone()["data"]
    assert "pending password" not in staged
    key = config.data_dir / KEY_FILENAME
    key.unlink()
    with pytest.raises(PublishError) as error:
        Core(config).initialize()
    assert error.value.code == "publication_password_key_unavailable"
    assert not key.exists()


def test_wrong_key_is_rejected_on_restart_for_staged_password_only(instance):
    config, core, auth = instance
    core.create_upload(
        {
            "kind": "file",
            "filename": "pending.txt",
            "size_bytes": 3,
            "access": "password",
            "password": "staged key password",
        },
        "operator",
    )
    (config.data_dir / KEY_FILENAME).write_bytes(os.urandom(32))
    with pytest.raises(PublishError) as error:
        Core(config).initialize()
    assert error.value.code == "publication_password_unavailable"


def test_old_hash_only_password_stays_valid_until_explicit_reset(instance):
    config, core, auth = instance
    material = publish(core, access="password", password="old password")
    with core.repository.connect(immediate=True) as connection:
        previous = core._publication(connection, material["id"])
        previous.pop("password_sealed")
        core._save_publication(connection, previous)
        connection.execute("DELETE FROM upload")
    (config.data_dir / KEY_FILENAME).unlink()
    restarted = Core(config).initialize()
    saved = restarted.get_publication(material["id"])
    assert saved["password_hash"] == material["password_hash"]
    assert not saved["password_copy_available"]
    assert auth.verify_password(saved["password_hash"], "old password")
    with pytest.raises(PublishError) as error:
        owner_copy(restarted, auth, material["id"])
    assert error.value.code == "password_copy_unavailable"
    assert error.value.action == "reset_publication_password"
    restarted.patch_publication(material["id"], {"password": "new explicit password"})
    assert owner_copy(restarted, auth, material["id"]) == "new explicit password"


def test_expiry_pause_and_retained_purge_preserve_copy_but_delete_denies(instance):
    config, core, auth = instance
    material = publish(
        core, access="password", password="retained password", purge_mode="immediate"
    )
    core.pause(material["id"], True)
    core.expire(material["id"])
    assert owner_copy(core, auth, material["id"]) == "retained password"
    core.sweep()
    assert core.get_publication(material["id"])["content_state"] == "purged"
    assert owner_copy(core, auth, material["id"]) == "retained password"
    core.delete_publication(material["id"])
    with pytest.raises(PublishError) as error:
        owner_copy(core, auth, material["id"])
    assert error.value.code == "publication_deleted"


def test_tombstone_denies_copy_even_if_metadata_was_not_marked_deleted(instance):
    config, core, auth = instance
    material = publish(core, access="password", password="tombstoned password")
    with core.repository.connect(immediate=True) as connection:
        connection.execute(
            "INSERT INTO deletion_tombstone VALUES (?,?)",
            (material["id"], "2026-11-18T00:00:00Z"),
        )
    with pytest.raises(PublishError) as error:
        owner_copy(core, auth, material["id"])
    assert error.value.code == "publication_deleted"


def test_owner_reset_preserves_copy_but_revokes_old_session(instance):
    config, core, auth = instance
    material = publish(core, access="password", password="publication stays same")
    old_cookie, _ = auth.login("owner", "correct-long-password", "127.0.0.1")
    auth.bootstrap("owner", "a changed owner password", reset=True)
    with pytest.raises(PublishError) as error:
        owner_copy(core, auth, material["id"], old_cookie)
    assert error.value.code == "authentication_required"
    new_cookie, _ = auth.login("owner", "a changed owner password", "127.0.0.1")
    assert (
        owner_copy(core, auth, material["id"], new_cookie) == "publication stays same"
    )


def test_doctor_reports_missing_key_without_replacing_it(instance):
    config, core, auth = instance
    publish(core, access="password", password="doctor password")
    key = config.data_dir / KEY_FILENAME
    key.unlink()
    report = doctor(config)
    assert not report["healthy"]
    assert report["error"] == "publication_password_key_unavailable"
    assert not key.exists()


def test_doctor_accepts_hash_only_database_without_creating_a_key(instance):
    config, core, auth = instance
    key = config.data_dir / KEY_FILENAME
    key.unlink()
    report = doctor(config)
    assert report["healthy"]
    assert not report["publication_password_key_available"]
    assert not key.exists()
