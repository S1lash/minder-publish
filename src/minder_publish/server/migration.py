"""Read-only legacy inspection and verified copy into a separate publication root."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from minder_publish.errors import PublishError

from . import publication_links
from .backup import instance_lock
from .core import Core, dumps, iso, now
from .storage import CHUNK_BYTES, content_type, fsync_directory, normalize_path, safe_id


@contextmanager
def legacy_snapshot(database):
    """Never let SQLite create/checkpoint sidecars beside the source database."""
    database = Path(database).resolve()
    if not database.is_file():
        raise PublishError("legacy_database_not_found", 404)
    with tempfile.TemporaryDirectory(prefix="publish-legacy-read-") as temporary:
        target = Path(temporary) / "legacy.db"
        before_set = {
            suffix: (
                Path(str(database) + suffix).stat().st_ino,
                Path(str(database) + suffix).stat().st_size,
                Path(str(database) + suffix).stat().st_mtime_ns,
            )
            for suffix in ("", "-wal")
            if Path(str(database) + suffix).exists()
        }
        for suffix in ("", "-wal"):
            source = Path(str(database) + suffix)
            if source.exists():
                before = source.stat()
                shutil.copyfile(source, Path(str(target) + suffix))
                after = source.stat()
                if (before.st_mtime_ns, before.st_size) != (
                    after.st_mtime_ns,
                    after.st_size,
                ):
                    raise PublishError(
                        "legacy_source_changed",
                        409,
                        "Stop legacy writers before inspection/import.",
                    )
        after_set = {
            suffix: (
                Path(str(database) + suffix).stat().st_ino,
                Path(str(database) + suffix).stat().st_size,
                Path(str(database) + suffix).stat().st_mtime_ns,
            )
            for suffix in ("", "-wal")
            if Path(str(database) + suffix).exists()
        }
        if before_set != after_set:
            raise PublishError("legacy_source_changed", 409)
        connection = sqlite3.connect(target)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def _legacy_time(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return iso(result)
    except (ValueError, AttributeError) as exc:
        raise PublishError("invalid_legacy_timestamp", 400) from exc


def _inspect(row, blobs, config):
    publication_id = safe_id(row["id"])
    root = blobs / publication_id / "content"
    limits = dict(config.limits)
    limits.update(
        site_expanded_bytes=config.ceilings["site_expanded_bytes"],
        site_files=config.ceilings["site_files"],
    )
    if row["kind"] not in ("single_file", "site_dir"):
        raise PublishError("invalid_legacy_kind", 400)
    entry = normalize_path(row["entry_path"], limits)
    files = []
    total = 0
    if root.is_symlink() or (blobs / publication_id).is_symlink():
        raise PublishError("unsafe_legacy_path", 400)
    if root.is_dir():
        candidates = [root / entry] if row["kind"] == "single_file" else root.rglob("*")
        discovered = 0
        for path in candidates:
            discovered += 1
            if discovered > limits["site_files"] * (limits["path_depth"] + 1):
                raise PublishError("legacy_content_limit", 413)
            normalize_path(path.relative_to(root).as_posix(), limits)
            if path.is_symlink() or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != blobs and blobs in parent.parents
            ):
                raise PublishError("unsafe_legacy_path", 400)
            if path.is_dir():
                continue
            if not path.is_file():
                continue
            name = normalize_path(path.relative_to(root).as_posix(), limits)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise PublishError("unsafe_legacy_path", 400)
            total += info.st_size
            maximum = (
                config.ceilings["file_bytes"]
                if row["kind"] == "single_file"
                else config.ceilings["site_expanded_bytes"]
            )
            if total > maximum or len(files) >= config.ceilings["site_files"]:
                raise PublishError("legacy_content_limit", 413)
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(CHUNK_BYTES):
                    digest.update(chunk)
            after = path.stat()
            if (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
                raise PublishError("legacy_source_changed", 409)
            files.append(
                {
                    "path": name,
                    "size_bytes": info.st_size,
                    "content_type": content_type(name),
                    "sha256": digest.hexdigest(),
                }
            )
    files.sort(key=lambda item: item["path"])
    names = [f["path"] for f in files]
    if len(set(n.casefold() for n in names)) != len(names):
        raise PublishError("legacy_path_collision", 400)
    if files and entry not in names:
        raise PublishError("legacy_entry_missing", 400)
    return {
        "id": publication_id,
        "files": files,
        "bytes": total,
        "entry": entry,
        "present": bool(files),
        "created_at": _legacy_time(row["created_at"]),
        "expires_at": _legacy_time(row["expires_at"]),
    }


def migrate(config, legacy_db, legacy_blobs, *, apply=False):
    database = Path(legacy_db).expanduser().resolve()
    blobs = Path(legacy_blobs).expanduser().resolve()
    if (
        config.data_dir == database.parent
        or database.parent in config.data_dir.parents
        or config.data_dir == blobs
        or blobs in config.data_dir.parents
        or config.data_dir in blobs.parents
    ):
        raise PublishError("migration_target_overlaps_source", 400)
    target_ids = set()
    if config.db_path.exists():
        with sqlite3.connect(config.db_path.as_uri() + "?mode=ro", uri=True) as target:
            if target.execute(
                "SELECT 1 FROM sqlite_master WHERE name='publication'"
            ).fetchone():
                target_ids = {
                    r[0]
                    for r in target.execute(
                        "SELECT id FROM publication UNION SELECT publication_id FROM deletion_tombstone"
                    )
                }
                if target.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='publication_link'"
                ).fetchone():
                    target_ids.update(r[0] for r in target.execute("SELECT name FROM publication_link"))
    report = {
        "dry_run": not apply,
        "source_unchanged": True,
        "publications": 0,
        "files": 0,
        "bytes": 0,
        "missing_content": [],
        "conflicts": [],
        "errors": [],
        "orphans": [],
        "retention": "preserve_legacy_bytes_with_purge_never",
        "items": [],
    }
    records = []
    with legacy_snapshot(database) as source:
        if not source.execute(
            "SELECT 1 FROM sqlite_master WHERE name='artifact'"
        ).fetchone():
            raise PublishError("unsupported_legacy_schema", 400)
        rows = source.execute("SELECT * FROM artifact ORDER BY id").fetchall()
        for row in rows:
            try:
                item = _inspect(row, blobs, config)
                if item["id"] in target_ids:
                    report["conflicts"].append(item["id"])
                if not item["present"]:
                    report["missing_content"].append(item["id"])
                report["publications"] += 1
                report["files"] += len(item["files"])
                report["bytes"] += item["bytes"]
                report["items"].append(
                    {
                        "id": item["id"],
                        "files": len(item["files"]),
                        "bytes": item["bytes"],
                        "expires_at": item["expires_at"],
                        "purge_mode": "never",
                        "purge_due_at": None,
                    }
                )
                records.append((dict(row), item))
            except PublishError as error:
                report["errors"].append({"id": row["id"], "code": error.code})
        known = {r["id"] for r in rows}
    if blobs.is_dir():
        report["orphans"] = sorted(
            p.name for p in blobs.iterdir() if p.is_dir() and p.name not in known
        )
    if not apply:
        return report
    if report["errors"] or report["conflicts"]:
        raise PublishError(
            "migration_preflight_failed",
            409,
            "Resolve reported errors/conflicts before applying the import.",
        )
    with instance_lock(config):
        core = Core(config).initialize()
        created_revisions = []
        try:
            for row, item in records:
                revision_id = secrets.token_hex(16) if item["present"] else None
                upload_id = secrets.token_hex(16)
                stage = core.storage.stage(upload_id)
                try:
                    if item["present"]:
                        # Import runs with the offline instance lock, so no runtime can race admission.
                        with core.repository.connect(immediate=True) as connection:
                            core._admit(
                                connection,
                                core._settings(connection)["limits"],
                                item["bytes"]
                                + config.operations["export_metadata_bytes"],
                            )
                        content = stage / "prepared" / "content"
                        content.mkdir(parents=True, mode=0o700)
                        for info in item["files"]:
                            source = blobs / item["id"] / "content" / info["path"]
                            target = content / info["path"]
                            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            digest = hashlib.sha256()
                            with (
                                source.open("rb") as input,
                                target.open("xb") as output,
                            ):
                                target.chmod(0o600)
                                while chunk := input.read(CHUNK_BYTES):
                                    output.write(chunk)
                                    digest.update(chunk)
                                output.flush()
                                os.fsync(output.fileno())
                            if (
                                target.stat().st_size != info["size_bytes"]
                                or digest.hexdigest() != info["sha256"]
                            ):
                                raise PublishError("legacy_source_changed", 409)
                        manifest = stage / "prepared" / "manifest.json"
                        manifest.write_text(dumps(item["files"]), encoding="utf-8")
                        manifest.chmod(0o600)
                        with manifest.open("rb") as file:
                            os.fsync(file.fileno())
                        fsync_directory(content)
                        fsync_directory(stage / "prepared")
                        core.storage.promote(upload_id, revision_id)
                        created_revisions.append(revision_id)
                    with core.repository.connect(immediate=True) as connection:
                        if publication_links.occupied(connection, item["id"]):
                            raise PublishError("migration_conflict", 409)
                        defaults = core._settings(connection)["defaults"]
                        publication = {
                            "id": item["id"],
                            "title": row["title"] or "",
                            "summary": "",
                            "kind": "file" if row["kind"] == "single_file" else "site",
                            "filename": Path(item["entry"]).name
                            if row["kind"] == "single_file"
                            else item["id"],
                            "entry_path": item["entry"],
                            "content_type": content_type(item["entry"]),
                            "size_bytes": item["bytes"],
                            "created_at": item["created_at"],
                            "updated_at": item["created_at"],
                            "expires_at": item["expires_at"],
                            "access": "password" if row["password_hash"] else "link",
                            "password_hash": row["password_hash"],
                            "password_hint": row["password_hint"],
                            "paused": row["status"] == "revoked"
                            or (row["status"] == "expired" and not item["expires_at"]),
                            "content_state": "present" if item["present"] else "purged",
                            "purge_mode": "never",
                            "purge_after_seconds": defaults["purge_after_seconds"],
                            "metadata_policy": "keep",
                            "metadata_after_purge_seconds": defaults[
                                "metadata_after_purge_seconds"
                            ],
                            "site_routing": "strict",
                            "external_resources": "allow",
                            "preview": "auto",
                            "indexing": False,
                            "guest_counts": True,
                            "owner_counts": defaults["owner_counts"],
                            "current_revision": revision_id,
                            "access_version": 1,
                            "metadata_version": 1,
                            "view_count": row["view_count"],
                            "last_viewed": _legacy_time(row["last_viewed"]),
                            "purge_due_at": None,
                            "purged_at": None,
                            "purge_reason": None if item["present"] else "unknown",
                            "missing_on_import_at": None if item["present"] else iso(now()),
                            "metadata_due_at": None,
                            "defaults_snapshot": defaults,
                            "legacy_import": True,
                        }
                        connection.execute(
                            "INSERT INTO publication VALUES (?,?)",
                            (publication["id"], dumps(publication)),
                        )
                        publication_links.add_initial(connection, publication)
                        if revision_id:
                            revision = {
                                "id": revision_id,
                                "publication_id": publication["id"],
                                "size_bytes": item["bytes"],
                                "created_at": item["created_at"],
                                "entry_path": item["entry"],
                                "kind": publication["kind"],
                            }
                            connection.execute(
                                "INSERT INTO revision VALUES (?,?,?,?,NULL,NULL,NULL)",
                                (
                                    revision_id,
                                    publication["id"],
                                    dumps(revision),
                                    "current",
                                ),
                            )
                            connection.executemany(
                                "INSERT INTO revision_file VALUES (?,?,?)",
                                [
                                    (revision_id, f["path"], dumps(f))
                                    for f in item["files"]
                                ],
                            )
                    report.setdefault("imported_ids", []).append(item["id"])
                finally:
                    core.storage.remove_stage(upload_id)
        except BaseException:
            with core.repository.connect(immediate=True) as connection:
                for publication_id in report.get("imported_ids", []):
                    connection.execute(
                        "DELETE FROM publication_link WHERE publication_id=?", (publication_id,)
                    )
                    for revision in connection.execute(
                        "SELECT id FROM revision WHERE publication_id=?",
                        (publication_id,),
                    ).fetchall():
                        connection.execute(
                            "DELETE FROM revision_file WHERE revision_id=?",
                            (revision["id"],),
                        )
                    connection.execute(
                        "DELETE FROM revision WHERE publication_id=?", (publication_id,)
                    )
                    connection.execute(
                        "DELETE FROM publication WHERE id=?", (publication_id,)
                    )
            for revision_id in created_revisions:
                shutil.rmtree(core.storage.revision(revision_id), ignore_errors=True)
            raise
    report["dry_run"] = False
    return report
