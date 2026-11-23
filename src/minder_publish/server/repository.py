"""Versioned core transactions. Authentication owns a separate version table."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from minder_publish.errors import PublishError

SCHEMA_VERSION = 6


def _apply_migration(connection, version, *, direction="up"):
    source = (
        files("minder_publish.server")
        .joinpath(f"migrations/core/{version:03d}_{direction}.sql")
        .read_text(encoding="utf-8")
    )
    # These packaged migrations contain ordinary DDL/DML, with an explicit outer transaction.
    statements = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )
    for statement in statements.split(";"):
        statement = statement.strip()
        if statement and statement.upper() not in ("BEGIN IMMEDIATE", "COMMIT"):
            connection.execute(statement)


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self, *, immediate=False):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(str(self.path), timeout=15)
        try:
            tables = {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "artifact" in tables:
                raise PublishError(
                    "legacy_database",
                    409,
                    "Import the legacy database into a separate data directory.",
                )
            self.path.chmod(0o600)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock if another initializer raced this process.
            tables = {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = 0
            if "core_schema_version" in tables:
                versions = connection.execute(
                    "SELECT version FROM core_schema_version"
                ).fetchall()
                if len(versions) != 1 or versions[0][0] not in range(
                    1, SCHEMA_VERSION + 1
                ):
                    raise PublishError(
                        "unknown_core_schema",
                        409,
                        "The core schema requires an explicitly supported migration.",
                    )
                version = versions[0][0]
            for target in range(version + 1, SCHEMA_VERSION + 1):
                _apply_migration(connection, target)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def downgrade_owner_statistics(self, target):
        """Retain the additive owner extension for an explicit offline schema-3 rollback."""
        if target != 3:
            raise PublishError("unsupported_schema_downgrade", 400, field="target")
        if not self.path.is_file():
            raise PublishError("database_not_found", 404)
        with self.connect(immediate=True) as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM core_schema_version")]
            if versions not in ([4], [5], [6]):
                raise PublishError("unsupported_schema_downgrade", 409, actual=versions, limit=SCHEMA_VERSION)
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
                raise PublishError("database_integrity_failed", 409)
            if versions == [6]:
                if connection.execute(
                    """SELECT 1 FROM publication_link WHERE preview_mode<>'inherit'
                       UNION ALL SELECT 1 FROM revision WHERE json_extract(data,'$.preview_mode') IN ('on','off')
                       UNION ALL SELECT 1 FROM core_settings
                        WHERE json_extract(data,'$.defaults.link_previews')=0 LIMIT 1"""
                ).fetchone():
                    raise PublishError("link_preview_policy_prevents_downgrade", 409)
                _apply_migration(connection, 6, direction="down")
            if versions[0] >= 5:
                if connection.execute(
                    "SELECT 1 FROM publication_link WHERE name<>publication_id LIMIT 1"
                ).fetchone():
                    raise PublishError("publication_links_prevent_downgrade", 409)
                _apply_migration(connection, 5, direction="down")
            _apply_migration(connection, 4, direction="down")
        return {"schema_version": 3, "owner_statistics_retained": True, "owner_collection_gap_possible": True}
