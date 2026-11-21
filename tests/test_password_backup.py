"""Password recovery survives offline restore independently of session rotation."""

import hashlib
import io
import json
import sqlite3
import tarfile
from contextlib import closing
from dataclasses import replace

import pytest
from test_operations import instance as operations_instance
from test_operations import publish

from minder_publish.errors import PublishError
from minder_publish.server.auth import AuthStore
from minder_publish.server.backup import backup, restore
from minder_publish.server.core import Core
from minder_publish.server.password_vault import KEY_FILENAME

instance = operations_instance


def read_archive(path):
    with tarfile.open(path) as source:
        return {
            item.name: source.extractfile(item).read() for item in source.getmembers()
        }


def write_archive(path, members):
    manifest = json.loads(members["backup.json"])
    manifest["file_count"] = len(members) - 1
    manifest["bytes"] = sum(
        len(value) for name, value in members.items() if name != "backup.json"
    )
    manifest["database_sha256"] = hashlib.sha256(
        members["database.sqlite3"]
    ).hexdigest()
    members["backup.json"] = json.dumps(manifest).encode()
    with tarfile.open(path, "w") as target:
        for name, data in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o600
            target.addfile(item, io.BytesIO(data))


def test_backup_v2_preserves_publication_password_key(instance, tmp_path):
    config, core, auth = instance
    material = publish(core, access="password", password="recover this password")
    archive = tmp_path / "snapshot.tar"
    backup(config, archive)
    with tarfile.open(archive) as source:
        manifest = json.load(source.extractfile("backup.json"))
        assert manifest["version"] == 2
        assert len(source.extractfile("publication-password.key").read()) == 32
        assert manifest["publication_password_key_sha256"]
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    restore(target, archive)
    restored = Core(target).initialize()
    assert target.secret != config.secret
    assert (
        restored.password_vault.open(restored.get_publication(material["id"]))
        == "recover this password"
    )
    assert (
        target.data_dir / "publication-password.key"
    ).stat().st_mode & 0o777 == 0o600


def test_version_one_hash_only_backup_restores_without_resetting_password(
    instance, tmp_path
):
    config, core, auth = instance
    material = publish(core, access="password", password="old backup password")
    with core.repository.connect(immediate=True) as connection:
        saved = core._publication(connection, material["id"])
        saved.pop("password_sealed")
        core._save_publication(connection, saved)
        connection.execute("DELETE FROM upload")
    (config.data_dir / KEY_FILENAME).unlink()
    archive = tmp_path / "hash-only.tar"
    backup(config, archive)
    assert not (config.data_dir / KEY_FILENAME).exists(), (
        "Backup must not create a source key"
    )
    members = read_archive(archive)
    manifest = json.loads(members["backup.json"])
    manifest["version"] = 1
    manifest.pop("publication_password_key_sha256")
    members["backup.json"] = json.dumps(manifest).encode()
    old_archive = tmp_path / "version-one.tar"
    write_archive(old_archive, members)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    restore(target, old_archive)
    restored = Core(target).initialize()
    saved = restored.get_publication(material["id"])
    assert saved["password_hash"] == material["password_hash"]
    assert not saved["password_copy_available"]
    assert auth.verify_password(saved["password_hash"], "old backup password")
    with pytest.raises(PublishError) as error:
        restored.password_vault.open(saved)
    assert error.value.code == "password_copy_unavailable"


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "oversized",
        "digest",
        "wrong_matching_digest",
        "null_manifest",
        "old_version",
    ],
)
def test_restore_rejects_missing_corrupt_or_unbound_password_keys(
    instance, tmp_path, damage
):
    config, core, auth = instance
    publish(core, access="password", password="backup validation password")
    archive = tmp_path / "valid.tar"
    backup(config, archive)
    members = read_archive(archive)
    manifest = json.loads(members["backup.json"])
    if damage == "missing":
        members.pop(KEY_FILENAME)
    elif damage == "oversized":
        members[KEY_FILENAME] += b"extra"
    elif damage in ("digest", "wrong_matching_digest"):
        members[KEY_FILENAME] = b"x" * 32
        if damage == "wrong_matching_digest":
            manifest["publication_password_key_sha256"] = hashlib.sha256(
                members[KEY_FILENAME]
            ).hexdigest()
    elif damage == "null_manifest":
        manifest["publication_password_key_sha256"] = None
    else:
        manifest["version"] = 1
        manifest.pop("publication_password_key_sha256")
    members["backup.json"] = json.dumps(manifest).encode()
    damaged = tmp_path / "damaged.tar"
    write_archive(damaged, members)
    target = replace(config, data_dir=tmp_path / "rejected", secret="")
    with pytest.raises(PublishError) as error:
        restore(target, damaged)
    assert error.value.code == "backup_integrity_failed"
    assert not target.db_path.exists()
    assert not (target.data_dir / KEY_FILENAME).exists()


def test_backup_refuses_lost_key_and_staged_or_saved_ciphertext(instance, tmp_path):
    config, core, auth = instance
    publish(core, access="password", password="cannot back up without key")
    (config.data_dir / KEY_FILENAME).unlink()
    archive = tmp_path / "never-created.tar"
    with pytest.raises(PublishError) as error:
        backup(config, archive)
    assert error.value.code == "publication_password_key_unavailable"
    assert not archive.exists()


def test_restore_keeps_copy_but_revokes_owner_and_device_credentials(
    instance, tmp_path
):
    config, core, auth = instance
    material = publish(core, access="password", password="restored owner copy")
    cookie, _ = auth.login("owner", "correct-long-password", "127.0.0.1")
    device = auth.create_token("old device", "materials:manage")["token"]
    archive = tmp_path / "snapshot.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    restore(target, archive)
    restored = Core(target).initialize()
    restored_auth = AuthStore(target)
    restored_auth.initialize()
    assert restored_auth.session(cookie) is None and restored_auth.token(device) is None
    new_cookie, _ = restored_auth.login("owner", "correct-long-password", "127.0.0.1")
    result = restored.copy_publication_password(
        material["id"],
        lambda connection: restored_auth.require_recent_auth(
            new_cookie, connection=connection
        ),
    )
    assert result == {"password": "restored owner copy"}


@pytest.mark.parametrize("damaged_policy", ["{}", "[]", "{"])
def test_restore_rejects_malformed_staged_password_policy(
    instance, tmp_path, damaged_policy
):
    config, core, auth = instance
    publish(core, access="password", password="corruption check password")
    archive = tmp_path / "valid.tar"
    backup(config, archive)
    members = read_archive(archive)
    database = tmp_path / "snapshot.sqlite3"
    database.write_bytes(members["database.sqlite3"])
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute("UPDATE upload SET data=?", (damaged_policy,))
    members["database.sqlite3"] = database.read_bytes()
    damaged = tmp_path / "damaged.tar"
    write_archive(damaged, members)
    target = replace(config, data_dir=tmp_path / "rejected", secret="")
    with pytest.raises(PublishError) as error:
        restore(target, damaged)
    assert error.value.code == "backup_integrity_failed"
    assert not target.db_path.exists()


def test_backup_restore_preserves_explicit_version_sharing_epoch_and_revokes_session(instance, tmp_path):
    from minder_publish.server.history import set_sharing
    config, core, auth = instance
    first = publish(core, b'first revision', access='link')
    second = publish(core, b'second revision', publication_id=first['id'], expected_revision=first['current_revision'])
    revision = first['current_revision']
    for enabled in (True, False, True):
        set_sharing(core, first['id'], revision, enabled, authorize=lambda connection: None)
    with core.repository.connect() as connection:
        source_data = json.loads(connection.execute('SELECT data FROM revision WHERE id=?', (revision,)).fetchone()[0])
    assert source_data['shared'] is True and source_data['share_version'] == 3
    cookie, _ = auth.login('owner', 'correct-long-password', '127.0.0.1')
    archive = tmp_path / 'version-sharing.tar'
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / 'version-restored', secret='')
    result = restore(target, archive)
    assert result['credentials_revoked'] and result['secret_rotated']
    restored = Core(target).initialize()
    with restored.repository.connect() as connection:
        data = json.loads(connection.execute('SELECT data FROM revision WHERE id=?', (revision,)).fetchone()[0])
    assert (data['shared'], data['share_version']) == (True, 3)
    assert restored.resolve_file(revision, 'report.txt')[0].read_bytes() == b'first revision'
    assert restored.get_publication(first['id'])['current_revision'] == second['current_revision']
    restored_auth = AuthStore(target); restored_auth.initialize()
    assert restored_auth.session(cookie) is None
