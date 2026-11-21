"""Real offline backup/restore/import exercises with immutable source fixtures."""

import asyncio
import io
import json
import os
import sqlite3
import tarfile
import threading
from dataclasses import replace

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.app import create_app
from minder_publish.server.auth import AuthStore
from minder_publish.server.backup import (
    backup,
    export_tombstones,
    instance_lock,
    restore,
)
from minder_publish.server.commands import doctor
from minder_publish.server.config import Config
from minder_publish.server.core import Core
from minder_publish.server.migration import migrate


@pytest.fixture
def instance(tmp_path):
    config = Config(
        tmp_path / "source",
        base_url="http://localhost:8080",
        secret="original-secret-" * 4,
        testing=True,
    )
    core = Core(config).initialize()
    auth = AuthStore(config)
    auth.initialize()
    auth.bootstrap("owner", "correct-long-password")
    return config, core, auth


def publish(core, payload=b"unchanged original bytes", **options):
    upload = core.create_upload(
        dict(kind="file", filename="report.txt", size_bytes=len(payload), **options),
        "operator",
    )

    async def stream():
        yield payload

    asyncio.run(core.receive_upload(upload["id"], stream(), "operator"))
    return core.commit_upload(upload["id"], "operator")


def test_backup_restore_revokes_credentials_and_preserves_files(instance, tmp_path):
    config, core, auth = instance
    item = publish(
        core, access="password", password="material-password", summary="Résumé"
    )
    owner_cookie, _ = auth.login("owner", "correct-long-password", "127.0.0.1")
    device = auth.create_token("laptop", "materials:manage")
    archive = tmp_path / "backup.tar"
    result = backup(config, archive)
    assert result["files"] >= 3 and archive.stat().st_mode & 0o777 == 0o600
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    result = restore(target, archive)
    assert result["secret_rotated"] and target.secret != config.secret
    restored = Core(target).initialize()
    info = restored.get_publication(item["id"])
    assert (
        info["summary"] == "Résumé" and info["password_hash"] == item["password_hash"]
    )
    assert (
        restored.resolve_file(info["current_revision"], "report.txt")[0].read_bytes()
        == b"unchanged original bytes"
    )
    restored_auth = AuthStore(target)
    restored_auth.initialize()
    assert restored_auth.session(owner_cookie) is None
    assert restored_auth.token(device["token"]) is None
    assert restored_auth.login("owner", "correct-long-password", "127.0.0.1")[0]
    assert (target.data_dir / "server.secret").stat().st_mode & 0o777 == 0o600


def test_restore_merges_latest_tombstone_ledger(instance, tmp_path):
    config, core, _ = instance
    item = publish(core)
    archive = tmp_path / "old.tar"
    backup(config, archive)
    core.delete_publication(item["id"])
    core.sweep()
    ledger = tmp_path / "deletions.json"
    export_tombstones(config, ledger)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    result = restore(target, archive, ledger)
    assert result["applied_tombstones"] == 1
    restored = Core(target).initialize()
    with pytest.raises(PublishError):
        restored.get_publication(item["id"])
    assert not list(target.revisions_dir.iterdir())


def test_runtime_lock_blocks_backup_and_restore(instance, tmp_path):
    config, _, _ = instance
    archive = tmp_path / "source.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "target", secret="")
    with instance_lock(config):
        with pytest.raises(PublishError) as error:
            backup(config, tmp_path / "blocked.tar")
        assert error.value.code == "instance_busy"
    with instance_lock(target):
        with pytest.raises(PublishError) as error:
            restore(target, archive)
        assert error.value.code == "instance_busy"
    assert not (tmp_path / "blocked.tar").exists()
    assert not target.db_path.exists()


async def test_shutdown_holds_runtime_lock_until_active_cleanup_finishes(
    tmp_path, monkeypatch
):
    config = Config(
        tmp_path / "data",
        base_url="https://publish.test",
        secret="test-only-secret-" * 4,
        testing=True,
    )
    config.operations["sweeper_interval_seconds"] = 0.01
    app = create_app(config)
    cleanup_started = asyncio.Event()
    cleanup_finished = threading.Event()
    finish_cleanup = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_cleanup():
        loop.call_soon_threadsafe(cleanup_started.set)
        if finish_cleanup.wait(timeout=5):
            cleanup_finished.set()

    monkeypatch.setattr(app.state.core, "sweep", blocked_cleanup)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=3)
    except BaseException:
        finish_cleanup.set()
        await lifespan.__aexit__(None, None, None)
        raise

    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        done, _ = await asyncio.wait({shutdown}, timeout=0.2)
        with pytest.raises(PublishError) as error, instance_lock(config):
            pass
        assert error.value.code == "instance_busy"
        assert not done
    finally:
        finish_cleanup.set()
        await asyncio.wait_for(shutdown, timeout=3)
        assert await asyncio.to_thread(cleanup_finished.wait, 3)

    with instance_lock(config):
        assert cleanup_finished.is_set()


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        ("missing", "backup_manifest_mismatch"),
        ("added", "backup_manifest_mismatch"),
        ("rewritten", "backup_content_corrupt"),
    ],
)
def test_backup_refuses_content_that_restore_would_reject(
    instance, tmp_path, damage, code
):
    config, core, _ = instance
    item = publish(core, payload=b"original")
    path, _ = core.resolve_file(item["current_revision"], "report.txt")
    if damage == "missing":
        path.unlink()
    elif damage == "added":
        path.with_name("unexpected.txt").write_bytes(b"unexpected")
    else:
        path.write_bytes(b"modified")  # Keep the original length to require a hash check.

    archive = tmp_path / "unusable.tar"
    with pytest.raises(PublishError) as error:
        backup(config, archive)
    assert error.value.code == code
    assert not archive.exists()


def test_backup_no_overwrite_inside_data_and_restore_empty_target(instance, tmp_path):
    config, _, _ = instance
    with pytest.raises(PublishError):
        backup(config, config.data_dir / "bad.tar")
    archive = tmp_path / "good.tar"
    backup(config, archive)
    with pytest.raises(PublishError):
        backup(config, archive)
    with pytest.raises(PublishError):
        restore(config, archive)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "revisions/../escape"])
def test_restore_rejects_paths_before_install(instance, tmp_path, name):
    config, _, _ = instance
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo(name)
        info.size = 1
        output.addfile(info, io.BytesIO(b"x"))
    target = replace(config, data_dir=tmp_path / "empty")
    with pytest.raises(PublishError):
        restore(target, archive)
    assert not (target.data_dir / "publish.sqlite3").exists()
    assert not (tmp_path / "escape").exists()


def test_restore_rejects_link_and_corrupt_database(instance, tmp_path):
    config, _, _ = instance
    archive = tmp_path / "evil-link.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("revisions/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        output.addfile(info)
    with pytest.raises(PublishError):
        restore(replace(config, data_dir=tmp_path / "empty"), archive)
    archive = tmp_path / "good.tar"
    backup(config, archive)
    malformed = tmp_path / "corrupt.tar"
    with tarfile.open(archive) as source, tarfile.open(malformed, "w") as output:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == "database.sqlite3":
                data = b"X" + data[1:]
            output.addfile(member, io.BytesIO(data))
    with pytest.raises(PublishError) as error:
        restore(replace(config, data_dir=tmp_path / "another"), malformed)
    assert error.value.code == "backup_integrity_failed"


def test_doctor_does_not_initialize_or_mutate_sources(instance, tmp_path):
    config, _, _ = instance
    missing = replace(config, data_dir=tmp_path / "does-not-exist")
    assert doctor(missing)["database_exists"] is False
    assert not missing.data_dir.exists()
    before = {p.name: p.read_bytes() for p in config.data_dir.iterdir() if p.is_file()}
    assert doctor(config)["healthy"]
    after = {p.name: p.read_bytes() for p in config.data_dir.iterdir() if p.is_file()}
    assert before == after


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    blobs = root / "blobs"
    blobs.mkdir()
    database = root / "metadata.sqlite3"
    schema = """CREATE TABLE artifact(id TEXT PRIMARY KEY,title TEXT,kind TEXT,entry_path TEXT,content_type TEXT,size_bytes INTEGER,
        created_at TEXT,expires_at TEXT,password_hash TEXT,password_hint TEXT,status TEXT,view_count INTEGER,last_viewed TEXT,owner TEXT)"""
    from argon2 import PasswordHasher

    password_hash = PasswordHasher().hash("legacy-password")
    rows = [
        (
            "Legacy7",
            "Old report",
            "single_file",
            "report.html",
            "text/html",
            8,
            "2026-01-01T00:00:00+00:00",
            None,
            password_hash,
            "hint",
            "active",
            17,
            "2026-02-01T00:00:00+00:00",
            "ilya",
        ),
        (
            "old-site",
            "Old site",
            "site_dir",
            "index.html",
            "text/html",
            15,
            "2025-01-01T00:00:00+00:00",
            "2025-01-08T00:00:00+00:00",
            None,
            None,
            "expired",
            4,
            None,
            "ilya",
        ),
        (
            "missing",
            "Missing",
            "single_file",
            "lost.txt",
            "text/plain",
            10,
            "2025-01-01T00:00:00+00:00",
            "2025-01-08T00:00:00+00:00",
            None,
            None,
            "expired",
            0,
            None,
            "ilya",
        ),
    ]
    with sqlite3.connect(database) as conn:
        conn.execute(schema)
        conn.executemany(
            "INSERT INTO artifact VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    for artifact, files in [
        ("Legacy7", {"report.html": b"<h1>hi</h1>"}),
        ("old-site", {"index.html": b"home", "assets/app.js": b"javascript"}),
    ]:
        content = blobs / artifact / "content"
        content.mkdir(parents=True)
        for name, data in files.items():
            target = content / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    (blobs / "orphan" / "content").mkdir(parents=True)
    return database, blobs, password_hash


def test_legacy_dry_run_and_apply_preserve_policies_and_source(tmp_path, legacy):
    database, blobs, password_hash = legacy
    config = Config(tmp_path / "new", secret="new-secret-" * 4, testing=True)
    before = {
        str(p.relative_to(database.parent)): p.read_bytes()
        for p in database.parent.rglob("*")
        if p.is_file()
    }
    report = migrate(config, database, blobs)
    assert not config.data_dir.exists()
    assert (
        report["publications"] == 3
        and report["files"] == 3
        and report["missing_content"] == ["missing"]
    )
    assert report["orphans"] == ["orphan"] and not report["errors"]
    result = migrate(config, database, blobs, apply=True)
    assert result["imported_ids"] == ["Legacy7", "missing", "old-site"]
    core = Core(config).initialize()
    item = core.get_publication("Legacy7")
    assert item["access"] == "password" and item["password_hash"] == password_hash
    assert (
        item["view_count"] == 17
        and item["last_viewed"] == "2026-02-01T00:00:00.000000Z"
    )
    site = core.get_publication("old-site", manifest=True)
    assert (
        site["access"] == "link" and site["purge_mode"] == "never" and site["expired"]
    )
    assert len(site["files"]) == 2
    core.sweep()
    assert core.get_publication("old-site")["content_state"] == "present"
    assert core.get_publication("missing")["content_state"] == "purged"
    after = {
        str(p.relative_to(database.parent)): p.read_bytes()
        for p in database.parent.rglob("*")
        if p.is_file()
    }
    assert before == after
    assert migrate(config, database, blobs)["conflicts"] == [
        "Legacy7",
        "missing",
        "old-site",
    ]


def test_legacy_source_overlap_and_symlink_rejected(tmp_path, legacy):
    database, blobs, _ = legacy
    config = Config(database.parent / "new", secret="secret-" * 8, testing=True)
    with pytest.raises(PublishError):
        migrate(config, database, blobs)
    (blobs / "old-site" / "content" / "link").symlink_to(database)
    config = replace(config, data_dir=tmp_path / "safe")
    result = migrate(config, database, blobs)
    assert result["errors"] == [{"id": "old-site", "code": "unsafe_legacy_path"}]
    with pytest.raises(PublishError):
        migrate(config, database, blobs, apply=True)


def test_malformed_ledger_never_installs_target(instance, tmp_path):
    config, _, _ = instance
    archive = tmp_path / "backup.tar"
    backup(config, archive)
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"format":"wrong"}')
    target = replace(config, data_dir=tmp_path / "target")
    with pytest.raises(PublishError):
        restore(target, archive, ledger)
    assert not target.data_dir.exists()


def test_import_rolls_back_all_created_publications_after_late_failure(
    tmp_path, legacy, monkeypatch
):
    database, blobs, _ = legacy
    config = Config(tmp_path / "new", secret="new-secret-" * 4, testing=True)
    original = Core._admit
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublishError("disk_space_low", 507)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Core, "_admit", fail_second)
    with pytest.raises(PublishError):
        migrate(config, database, blobs, apply=True)
    core = Core(config).initialize()
    assert core.list_publications()["total"] == 0
    assert not list(config.revisions_dir.iterdir())
    monkeypatch.setattr(Core, "_admit", original)
    assert len(migrate(config, database, blobs, apply=True)["imported_ids"]) == 3


def test_import_rejects_symlink_artifact_directory(tmp_path, legacy):
    database, blobs, _ = legacy
    import shutil

    outside = tmp_path / "outside"
    shutil.move(blobs / "Legacy7", outside)
    (blobs / "Legacy7").symlink_to(outside, target_is_directory=True)
    config = Config(tmp_path / "target", secret="new-secret-" * 4, testing=True)
    result = migrate(config, database, blobs)
    assert {"id": "Legacy7", "code": "unsafe_legacy_path"} in result["errors"]


def test_restore_rejects_oversized_pax_before_parsing(instance, tmp_path):
    config, _, _ = instance
    archive = tmp_path / "header.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("header")
        member.type = tarfile.XHDTYPE
        member.size = 70000
        output.addfile(member, io.BytesIO(b"x" * 70000))
    with pytest.raises(PublishError) as error:
        restore(replace(config, data_dir=tmp_path / "target"), archive)
    assert error.value.code == "backup_header_limit"


def test_restore_malformed_archive_and_incomplete_marker_fail_closed(
    instance, tmp_path
):
    config, _, _ = instance
    archive = tmp_path / "invalid.tar"
    archive.write_bytes(b"not an archive")
    with pytest.raises(PublishError):
        restore(replace(config, data_dir=tmp_path / "target"), archive)
    marker = config.data_dir / ".restore-in-progress"
    marker.write_text("interrupted")
    with pytest.raises(PublishError) as error:
        Core(config).initialize()
    assert error.value.code == "incomplete_restore"


def test_doctor_unhealthy_exit_is_nonzero(tmp_path, monkeypatch, capsys):
    from minder_publish.server.commands import main

    monkeypatch.setenv("PUBLISH_DATA_DIR", str(tmp_path / "missing"))
    monkeypatch.delenv("PUBLISH_SECRET", raising=False)
    assert main(["doctor"]) == 1
    assert json.loads(capsys.readouterr().out)["healthy"] is False
    assert not (tmp_path / "missing").exists()


def test_bootstrap_cli_uses_stdin_and_never_prints_password(
    tmp_path, monkeypatch, capsys
):
    import sys

    from minder_publish.server.commands import main

    monkeypatch.setenv("PUBLISH_DATA_DIR", str(tmp_path / "instance"))
    monkeypatch.delenv("PUBLISH_SECRET", raising=False)
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b"long-owner-password\n"))
    )
    assert main(["bootstrap", "--username", "owner", "--password-stdin"]) == 0
    captured = capsys.readouterr()
    assert "long-owner-password" not in captured.out + captured.err
    assert (tmp_path / "instance" / "server.secret").stat().st_mode & 0o777 == 0o600


def test_restore_install_failure_rolls_back_created_files(
    instance, tmp_path, monkeypatch
):
    config, _, _ = instance
    archive = tmp_path / "source.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "target")

    original = os.rename
    install_calls = 0

    def fail_install(source, destination, *args, **kwargs):
        nonlocal install_calls
        if destination.parent == target.data_dir:
            install_calls += 1
            if install_calls == 2:
                raise OSError("injected installation failure")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", fail_install)
    with pytest.raises(OSError, match="injected installation failure"):
        restore(target, archive)
    assert {p.name for p in target.data_dir.iterdir()} == {".runtime.lock"}


def test_restore_flushes_marker_and_staged_tree_before_install(
    instance, tmp_path, monkeypatch
):
    config, core, _ = instance
    publish(core, access="password", password="material-password")
    archive = tmp_path / "source.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "target")
    marker = target.data_dir / ".restore-in-progress"
    synced = set()
    checked_install = False
    fsync, rename = os.fsync, os.rename

    def identity(info):
        return info.st_dev, info.st_ino

    def record_sync(fd):
        fsync(fd)
        synced.add(identity(os.fstat(fd)))

    def check_install(source, destination, *args, **kwargs):
        nonlocal checked_install
        if destination.parent == target.data_dir and not checked_install:
            assert identity(marker.stat()) in synced, "restore marker was not flushed"
            assert identity(target.data_dir.stat()) in synced, (
                "restore marker entry was not flushed"
            )
            staging = source.parent
            assert identity(staging.stat()) in synced
            for path in staging.rglob("*"):
                assert identity(path.stat()) in synced, (
                    f"staged restore entry was not flushed: {path.name}"
                )
            checked_install = True
        return rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_sync)
    monkeypatch.setattr(os, "rename", check_install)
    assert restore(target, archive)["restored"]
    assert checked_install
    assert not marker.exists()
    assert not list(target.data_dir.glob("database.sqlite3*"))
