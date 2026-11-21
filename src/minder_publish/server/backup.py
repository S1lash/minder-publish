"""Offline snapshots and explicit empty-target restore with credential revocation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

from minder_publish.errors import PublishError

from . import publication_links
from .core import Core, dumps, iso, now, parse
from .password_vault import KEY_BYTES, KEY_FILENAME, PasswordVault
from .storage import CHUNK_BYTES, fsync_directory, fsync_tree, normalize_path, safe_id

MAX_LEDGER_BYTES = 64 * 1024 * 1024


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        if self.size < 0 or self.type == tarfile.GNUTYPE_SPARSE:
            raise PublishError("unsafe_backup_member", 400)
        extensions = (
            tarfile.GNUTYPE_LONGNAME,
            tarfile.GNUTYPE_LONGLINK,
            tarfile.XHDTYPE,
            tarfile.XGLTYPE,
            tarfile.SOLARIS_XHDTYPE,
        )
        extension = self.type in extensions
        if extension:
            depth = getattr(archive, "_mp_extension_depth", 0) + 1
            total = getattr(archive, "_mp_extension_bytes", 0) + self.size
            if (
                self.size > 65536
                or depth > 8
                or total
                > getattr(archive, "_mp_extension_total_limit", MAX_LEDGER_BYTES)
            ):
                raise PublishError("backup_header_limit", 413, limit=65536)
            archive._mp_extension_depth = depth
            archive._mp_extension_bytes = total
        try:
            return super()._proc_member(archive)
        finally:
            if extension:
                archive._mp_extension_depth -= 1


@contextmanager
def instance_lock(config):
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(config.runtime_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError(
                "instance_busy", 409, "Stop the running server before this operation."
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _read_only(path, *, immutable=False):
    if not Path(path).is_file():
        raise PublishError("database_not_found", 404)
    options = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    connection = sqlite3.connect(Path(path).resolve().as_uri() + options, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def export_tombstones(config, output):
    with _read_only(config.db_path) as connection:
        connection.execute("BEGIN")
        rows = [
            {"id": r["publication_id"], "deleted_at": r["deleted_at"]}
            for r in connection.execute(
                "SELECT * FROM deletion_tombstone ORDER BY publication_id"
            )
        ]
        has_links = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_link'"
        ).fetchone()
        links = (
            [
                dict(row)
                for row in connection.execute(
                    """SELECT name,publication_id,created_at,retired_at FROM publication_link
                       WHERE name<>publication_id OR (retired_at IS NOT NULL AND is_primary=0)
                       ORDER BY name"""
                )
            ]
            if has_links else []
        )
        payload = dumps(
            {
                "format": "minder-publish-tombstones",
                "version": 2,
                "exported_at": iso(now()),
                "deletions": rows,
                "links": links,
            }
        ).encode()
    if len(payload) > MAX_LEDGER_BYTES:
        raise PublishError("ledger_size_limit", 413, limit=MAX_LEDGER_BYTES)
    output = Path(output)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    return {"output": str(output), "deletions": len(rows)}


def _ledger(path):
    if path is None:
        return [], [], None
    path = Path(path)
    if path.stat().st_size > MAX_LEDGER_BYTES:
        raise PublishError("ledger_size_limit", 413)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload["format"] != "minder-publish-tombstones"
            or type(payload["version"]) is not int
            or payload["version"] not in (1, 2)
            or not isinstance(payload["deletions"], list)
        ):
            raise ValueError()
        deletions = [
            (safe_id(item["id"]), iso(parse(item["deleted_at"])))
            for item in payload["deletions"]
        ]
        if payload["version"] == 1:
            return deletions, [], None
        exported = iso(parse(payload["exported_at"]))
        if not isinstance(payload["links"], list):
            raise ValueError()
        links = []
        seen = set()
        for item in payload["links"]:
            name = safe_id(item["name"])
            owner = safe_id(item["publication_id"])
            created = iso(parse(item["created_at"]))
            retired = iso(parse(item["retired_at"])) if item["retired_at"] is not None else None
            if name in seen or created > exported or (retired and not created <= retired <= exported):
                raise ValueError()
            seen.add(name)
            links.append((name, owner, created, retired))
        return deletions, links, exported
    except (ValueError, KeyError, TypeError, PublishError) as exc:
        raise PublishError("invalid_tombstone_ledger", 400) from exc


def _restore_link_reservations(connection, links, exported):
    for name, owner, created, retired in links:
        current = connection.execute(
            "SELECT publication_id,changed_at FROM publication_link WHERE name=?",
            (name,),
        ).fetchone()
        if current is None:
            timestamp = retired or exported
            connection.execute(
                """INSERT INTO publication_link
                   (name,publication_id,is_primary,created_at,changed_at,retired_at)
                   VALUES (?,?,0,?,?,?)""",
                (name, owner, created, timestamp, timestamp),
            )
        elif current["publication_id"] != owner:
            raise PublishError("restore_publication_link_conflict", 409)
        elif retired and parse(current["changed_at"]) < parse(retired):
            connection.execute(
                "UPDATE publication_link SET retired_at=?,changed_at=? WHERE name=?",
                (retired, retired, name),
            )
    if connection.execute(
        """SELECT 1 FROM publication p
           JOIN publication_link link ON link.publication_id=p.id AND link.is_primary=1
           LEFT JOIN deletion_tombstone t ON t.publication_id=p.id
           WHERE link.retired_at IS NOT NULL AND t.publication_id IS NULL
           LIMIT 1"""
    ).fetchone():
        raise PublishError(
            "restore_publication_link_revoked",
            409,
            "The ledger revoked a primary link in this snapshot. Restore a newer backup.",
        )


def _revision_files(root, config):
    files = entries = 0
    for path in root.rglob("*"):
        entries += 1
        if (
            entries
            > config.ceilings["site_files"] * (config.limits["path_depth"] + 2) + 3
        ):
            raise PublishError("backup_file_count_limit", 413)
        name = normalize_path(
            path.relative_to(root).as_posix(),
            {"path_utf8_bytes": 2048, "path_depth": 40},
            reserved=False,
        )
        if path.is_symlink():
            raise PublishError("unsafe_backup_path", 400)
        if path.is_file():
            files += 1
            if files > config.ceilings["site_files"] + 2:
                raise PublishError("backup_file_count_limit", 413)
            if name not in ("manifest.json", "thumbnail.jpg") and not name.startswith(
                "content/"
            ):
                raise PublishError("unsafe_backup_path", 400)
            yield path


def _revision_manifest(connection, revision_id):
    return {
        row["path"]: json.loads(row["data"])
        for row in connection.execute(
            "SELECT path,data FROM revision_file WHERE revision_id=?", (revision_id,)
        )
    }


class _HashingReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        block = self.source.read(size)
        self.digest.update(block)
        return block


def _archive_revision(archive, root, config, manifest):
    for path in _revision_files(root, config):
        name = path.relative_to(root).as_posix()
        arcname = f"revisions/{root.name}/{name}"
        if not name.startswith("content/"):
            archive.add(path, arcname=arcname, recursive=False)
            continue
        expected = manifest.pop(name.removeprefix("content/"), None)
        if expected is None:
            raise PublishError("backup_manifest_mismatch", 409)
        member = archive.gettarinfo(str(path), arcname=arcname)
        if not member.isfile() or member.size != expected["size_bytes"]:
            raise PublishError("backup_content_corrupt", 409)
        with path.open("rb") as source:
            reader = _HashingReader(source)
            archive.addfile(member, reader)
        if reader.digest.hexdigest() != expected["sha256"]:
            raise PublishError("backup_content_corrupt", 409)
    if manifest:
        raise PublishError("backup_manifest_mismatch", 409)


def _write_synced_text(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def backup(config, output):
    output = Path(output).expanduser().resolve()
    if output == config.data_dir or config.data_dir in output.parents:
        raise PublishError(
            "backup_inside_data",
            400,
            "Write the backup outside the managed data directory.",
        )
    if output.exists():
        raise PublishError("output_exists", 409)
    with (
        instance_lock(config),
        tempfile.TemporaryDirectory(
            prefix="publish-backup-", dir=output.parent
        ) as temporary,
    ):
        temporary = Path(temporary)
        snapshot = temporary / "database.sqlite3"
        with (
            _read_only(config.db_path) as source,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            source.backup(target)
        snapshot.chmod(0o600)
        password_key = None
        with _read_only(snapshot) as connection:
            revisions = [
                r["id"]
                for r in connection.execute("SELECT id FROM revision ORDER BY id")
            ]
            vault = PasswordVault(config.data_dir)
            if vault.path.exists() or vault.path.is_symlink():
                key = vault.key()
                password_key = temporary / KEY_FILENAME
                descriptor = os.open(
                    password_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as target:
                    target.write(key)
                for publication in vault.sealed_publications(connection):
                    vault.open(publication)
            elif next(vault.sealed_publications(connection), None) is not None:
                vault.key()
        sidecars = {}
        byte_count = snapshot.stat().st_size + (KEY_BYTES if password_key else 0)
        file_count = 2 if password_key else 1
        for revision_id in revisions:
            root = config.revisions_dir / safe_id(revision_id)
            if not root.is_dir():
                raise PublishError("backup_content_missing", 409, actual=revision_id)
            sidecars[revision_id] = {}
            for path in _revision_files(root, config):
                if path.is_symlink():
                    raise PublishError("unsafe_backup_path", 400)
                if path.is_file():
                    byte_count += path.stat().st_size
                    file_count += 1
                    if path.parent == root:
                        sidecars[revision_id][path.name] = _digest(path)
        metadata = {
            "format": "minder-publish-backup",
            "version": 2,
            "created_at": iso(now()),
            "database_sha256": _digest(snapshot),
            "publication_password_key_sha256": _digest(password_key)
            if password_key
            else None,
            "file_count": file_count,
            "bytes": byte_count,
            "sidecars": sidecars,
        }
        encoded = dumps(metadata).encode()
        if len(encoded) > MAX_LEDGER_BYTES:
            raise PublishError("backup_metadata_limit", 413)
        manifest = temporary / "backup.json"
        manifest.write_bytes(encoded)
        if (
            shutil.disk_usage(output.parent).free
            < byte_count + len(encoded) + file_count * 2048 + snapshot.stat().st_size
        ):
            raise PublishError("backup_disk_space_low", 507)
        partial = temporary / "snapshot.tar"
        with _read_only(snapshot) as connection, tarfile.open(partial, "w") as archive:
            archive.add(snapshot, arcname="database.sqlite3", recursive=False)
            archive.add(manifest, arcname="backup.json", recursive=False)
            if password_key:
                archive.add(password_key, arcname=KEY_FILENAME, recursive=False)
            for revision_id in revisions:
                _archive_revision(
                    archive,
                    config.revisions_dir / revision_id,
                    config,
                    _revision_manifest(connection, revision_id),
                )
        partial.chmod(0o600)
        with partial.open("rb") as completed:
            os.fsync(completed.fileno())
        # Same-filesystem hard link is atomic and refuses an existing output without a second archive copy.
        os.link(partial, output)
        descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "output": str(output),
        "files": file_count,
        "bytes": byte_count,
        "requires_latest_tombstones_for_older_snapshot": True,
    }


def _restore(config, source, tombstones=None):
    supplied, links, exported = _ledger(tombstones)
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise PublishError("backup_not_found", 404)
    with instance_lock(config):
        if any(p != config.runtime_lock_path for p in config.data_dir.iterdir()):
            raise PublishError("restore_target_not_empty", 409)
        with tempfile.TemporaryDirectory(
            prefix="publish-restore-", dir=config.data_dir
        ) as temporary:
            temporary = Path(temporary)
            total = 0
            count = 0
            max_files = (
                config.limits["publication_count"]
                * (config.operations["transient_retired_revision_cap"] + config.operations["history_max_previous_versions"] + 2)
                * (config.ceilings["site_files"] + 3)
                + 3
            )
            with tarfile.open(source, "r|", tarinfo=BoundedTarInfo) as archive:
                archive._mp_extension_total_limit = config.storage_budget_bytes
                while member := archive.next():
                    if not member.isfile() or member.issparse() or member.size < 0:
                        raise PublishError("unsafe_backup_member", 400)
                    name = normalize_path(
                        member.name,
                        {"path_utf8_bytes": 2048, "path_depth": 40},
                        reserved=False,
                    )
                    if name not in (
                        "database.sqlite3",
                        "backup.json",
                        KEY_FILENAME,
                    ) and not name.startswith("revisions/"):
                        raise PublishError("unsafe_backup_member", 400)
                    if name == KEY_FILENAME and member.size != KEY_BYTES:
                        raise PublishError("backup_integrity_failed", 400)
                    count += 1
                    total += member.size
                    if count > max_files or total > config.storage_budget_bytes:
                        raise PublishError("restore_budget_exceeded", 413)
                    if (
                        shutil.disk_usage(temporary).free - member.size
                        < config.operational_reserve_bytes
                    ):
                        raise PublishError("restore_disk_space_low", 507)
                    target = temporary.joinpath(*name.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with (
                        archive.extractfile(member) as stream,
                        target.open("xb") as out,
                    ):
                        target.chmod(0o600)
                        shutil.copyfileobj(stream, out, CHUNK_BYTES)
                        out.flush()
                        os.fsync(out.fileno())
                    archive.members.clear()
            metadata_path = temporary / "backup.json"
            if (
                not metadata_path.is_file()
                or metadata_path.stat().st_size > MAX_LEDGER_BYTES
            ):
                raise PublishError("invalid_backup", 400)
            try:
                metadata = json.loads(metadata_path.read_text())
            except ValueError as exc:
                raise PublishError("invalid_backup", 400) from exc
            database = temporary / "database.sqlite3"
            if not isinstance(metadata, dict):
                raise PublishError("invalid_backup", 400)
            if (
                metadata.get("format") != "minder-publish-backup"
                or type(metadata.get("version")) is not int
                or metadata["version"] not in (1, 2)
                or not database.is_file()
                or _digest(database) != metadata.get("database_sha256")
            ):
                raise PublishError("backup_integrity_failed", 400)
            password_key = temporary / KEY_FILENAME
            if metadata["version"] == 1:
                if password_key.exists():
                    raise PublishError("backup_integrity_failed", 400)
            elif (
                "publication_password_key_sha256" not in metadata
                or (
                    metadata["publication_password_key_sha256"] is None
                    and password_key.exists()
                )
                or (
                    metadata["publication_password_key_sha256"] is not None
                    and (
                        not password_key.is_file()
                        or _digest(password_key)
                        != metadata["publication_password_key_sha256"]
                    )
                )
            ):
                raise PublishError("backup_integrity_failed", 400)
            if not isinstance(metadata.get("sidecars"), dict):
                raise PublishError("invalid_backup", 400)
            if count - 1 != metadata.get(
                "file_count"
            ) or total - metadata_path.stat().st_size != metadata.get("bytes"):
                raise PublishError("backup_integrity_failed", 400)
            # The archive carries a standalone SQLite snapshot with no WAL sidecars.
            with _read_only(database, immutable=True) as inspection:
                if inspection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger'"
                ).fetchone():
                    raise PublishError("unexpected_database_trigger", 400)
                vault = PasswordVault(temporary)
                try:
                    for publication in vault.sealed_publications(inspection):
                        if metadata["version"] == 1 or not password_key.is_file():
                            raise PublishError("backup_integrity_failed", 400)
                        vault.open(publication)
                except PublishError as exc:
                    raise PublishError("backup_integrity_failed", 400) from exc
            database.rename(temporary / "publish.sqlite3")
            restored_config = replace(
                config, data_dir=temporary, secret=secrets.token_urlsafe(48)
            )
            core = Core(restored_config).initialize()
            with core.repository.connect(immediate=True) as connection:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger'"
                ).fetchone():
                    raise PublishError("unexpected_database_trigger", 400)
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise PublishError("backup_database_corrupt", 400)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise PublishError("backup_database_corrupt", 400)
                for publication_id, timestamp in supplied:
                    publication_links.record_deletion(connection, publication_id, timestamp)
                _restore_link_reservations(connection, links, exported)
                deleted = {
                    r["publication_id"]
                    for r in connection.execute(
                        "SELECT publication_id FROM deletion_tombstone"
                    )
                }
                revisions = connection.execute("SELECT * FROM revision").fetchall()
                known = set()
                for revision in revisions:
                    revision_id = safe_id(revision["id"])
                    known.add(revision_id)
                    root = core.storage.revision(revision_id)
                    if revision["publication_id"] in deleted:
                        shutil.rmtree(root, ignore_errors=True)
                        connection.execute(
                            "DELETE FROM revision_file WHERE revision_id=?",
                            (revision_id,),
                        )
                        connection.execute(
                            "DELETE FROM revision WHERE id=?", (revision_id,)
                        )
                        continue
                    expected = _revision_manifest(connection, revision_id)
                    actual = {
                        p.relative_to(root / "content").as_posix()
                        for p in (root / "content").rglob("*")
                        if p.is_file()
                    }
                    if set(expected) != actual:
                        raise PublishError("backup_manifest_mismatch", 400)
                    for path, info in expected.items():
                        normalize_path(
                            path, {"path_utf8_bytes": 1024, "path_depth": 32}
                        )
                        file = root / "content" / path
                        if (
                            file.stat().st_size != info["size_bytes"]
                            or _digest(file) != info["sha256"]
                        ):
                            raise PublishError("backup_content_corrupt", 400)
                    sidecars = metadata.get("sidecars", {}).get(revision_id, {})
                    if not isinstance(sidecars, dict):
                        raise PublishError("invalid_backup", 400)
                    if {
                        p.relative_to(root).as_posix()
                        for p in root.rglob("*")
                        if p.is_file() and "content" not in p.relative_to(root).parts
                    } - set(sidecars):
                        raise PublishError("backup_unknown_sidecar", 400)
                    for name, digest in sidecars.items():
                        if "/" in name or name not in (
                            "manifest.json",
                            "thumbnail.jpg",
                        ):
                            raise PublishError("unsafe_backup_member", 400)
                        if (
                            not (root / name).is_file()
                            or _digest(root / name) != digest
                        ):
                            raise PublishError("backup_content_corrupt", 400)
                if {p.name for p in core.storage.revisions.iterdir()} - known:
                    raise PublishError("backup_unknown_revision", 400)
                for publication_id in deleted:
                    connection.execute(
                        "DELETE FROM publication WHERE id=?", (publication_id,)
                    )
                connection.execute("DELETE FROM upload")
                connection.execute("DELETE FROM external_reservation")
                connection.execute("DELETE FROM read_lease")
            from .auth import AuthStore

            auth = AuthStore(restored_config)
            auth.initialize()
            auth.invalidate_all()
            _write_synced_text(temporary / "server.secret", restored_config.secret + "\n")
            metadata_path.unlink()
            fsync_tree(temporary)
            # The runtime lock prevents exposure during installation; the durable marker fails closed after a crash.
            marker = config.data_dir / ".restore-in-progress"
            installed = []
            try:
                _write_synced_text(
                    marker,
                    "Restore installation is incomplete. Repeat into a fresh empty target.\n",
                )
                fsync_directory(config.data_dir)
                for path in list(temporary.iterdir()):
                    if path == restored_config.runtime_lock_path:
                        continue
                    target = config.data_dir / path.name
                    os.rename(path, target)
                    installed.append(target)
                fsync_directory(config.data_dir)
            except BaseException:
                for path in reversed(installed):
                    os.rename(path, temporary / path.name)
                if installed:
                    fsync_directory(config.data_dir)
                marker.unlink(missing_ok=True)
                fsync_directory(config.data_dir)
                raise
            marker.unlink()
            fsync_directory(config.data_dir)
            config.secret = restored_config.secret
    return {
        "restored": True,
        "secret_rotated": True,
        "credentials_revoked": True,
        "applied_tombstones": len(deleted),
        "later_deletions_require_latest_ledger": tombstones is None,
    }


def restore(config, source, tombstones=None):
    try:
        return _restore(config, source, tombstones)
    except (tarfile.TarError, EOFError, UnicodeError) as exc:
        raise PublishError(
            "invalid_backup", 400, "The selected archive is malformed or unsupported."
        ) from exc
