"""Explicit local operator commands. No deployment, DNS, or cloud side effects."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import sys
from contextlib import closing, contextmanager

from minder_publish.errors import PublishError

from .auth import AuthStore
from .backup import backup, export_tombstones, instance_lock, restore
from .config import Config
from .core import Core
from .migration import migrate
from .password_vault import PasswordVault
from .repository import SCHEMA_VERSION, Repository


@contextmanager
def _doctor_connection(config):
    database = config.db_path
    lock_path = config.runtime_lock_path
    try:
        lock = lock_path.open("rb")
    except FileNotFoundError:
        lock = None
    try:
        writer_active = False
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                writer_active = True
        journal_paths = [
            database.with_name(database.name + suffix) for suffix in ("-wal", "-journal")
        ]
        # An idle checkpointed database needs no WAL sidecars. An active or
        # uncheckpointed database needs SQLite's normal snapshot and locking.
        checkpointed = not writer_active and not any(
            path.exists() for path in journal_paths
        )
        before = database.stat() if checkpointed and lock is None else None
        flags = "mode=ro&immutable=1" if checkpointed else "mode=ro"
        with closing(
            sqlite3.connect(database.as_uri() + "?" + flags, uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            yield connection
        if before is not None:
            after = database.stat()
            if (
                (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)
                or lock_path.exists()
                or any(path.exists() for path in journal_paths)
            ):
                raise PublishError(
                    "instance_busy", 409, "The database changed during inspection. Run doctor again."
                )
    finally:
        if lock is not None:
            lock.close()


def doctor(config):
    result = {
        "data_dir": str(config.data_dir),
        "database_exists": config.db_path.is_file(),
        "secret_configured": bool(config.secret),
        "publication_password_key_available": False,
        "configured_budget_bytes": config.storage_budget_bytes,
        "healthy": False,
        "read_only": True,
    }
    if not config.db_path.is_file():
        return result
    with _doctor_connection(config) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if (
            not {
                "core_schema_version",
                "core_settings",
                "publication",
                "external_reservation",
            }
            <= tables
        ):
            result["error"] = "unsupported_schema"
            return result
        result["core_schema_version"] = connection.execute(
            "SELECT version FROM core_schema_version"
        ).fetchone()[0]
        if result["core_schema_version"] != SCHEMA_VERSION:
            result["error"] = "unsupported_schema"
            return result
        result["database_integrity"] = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]
        settings = json.loads(
            connection.execute("SELECT data FROM core_settings WHERE id=1").fetchone()[
                0
            ]
        )
        result["storage"] = Core(config)._snapshot(connection, settings["limits"])
        vault = PasswordVault(config.data_dir)
        try:
            if vault.path.exists() or vault.path.is_symlink():
                vault.key()
                result["publication_password_key_available"] = True
            for publication in vault.sealed_publications(connection):
                vault.open(publication)
        except PublishError as error:
            result["error"] = error.code
            return result
        result["healthy"] = result["database_integrity"] == "ok" and config.secret != ""
    return result


def _password(args):
    if args.password_stdin:
        value = sys.stdin.buffer.readline(2048)
        if len(value) > 1025:
            raise PublishError("invalid_owner_password", 400)
        return value.decode("utf-8").rstrip("\r\n")
    first = getpass.getpass("Owner password: ")
    if first != getpass.getpass("Repeat password: "):
        raise PublishError("password_mismatch", 400)
    return first


def build_parser():
    parser = argparse.ArgumentParser(prog="minder-publish-server")
    parser.add_argument("--data-dir")
    parser.add_argument("--base-url")
    parser.add_argument("--content-domain")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--username", required=True)
    bootstrap.add_argument("--password-stdin", action="store_true")
    reset = commands.add_parser("reset-password")
    reset.add_argument("--username")
    reset.add_argument("--password-stdin", action="store_true")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    commands.add_parser("doctor")
    commands.add_parser("sweep")
    downgrade = commands.add_parser(
        "schema-downgrade",
        help="Offline rollback to schema 3, retaining owner aggregates. Back up first; an older release can leave owner collection gaps.",
    )
    downgrade.add_argument("--target", type=int, choices=(3,), required=True)
    save = commands.add_parser("backup")
    save.add_argument("--output", required=True)
    load = commands.add_parser("restore")
    load.add_argument("--input", required=True)
    load.add_argument("--tombstones")
    ledger = commands.add_parser("tombstones")
    ledger.add_argument("--output", required=True)
    migration = commands.add_parser("migrate")
    migration.add_argument("--legacy-db", required=True)
    migration.add_argument("--legacy-blobs", required=True)
    mode = migration.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        for name in ("data_dir", "base_url", "content_domain"):
            value = getattr(args, name)
            if value is not None:
                os.environ["PUBLISH_" + name.upper()] = value
        config = Config.from_env()
        if args.command in ("bootstrap", "reset-password"):
            password = _password(args)
            with instance_lock(config):
                if not (config.data_dir / "server.secret").exists():
                    secret = config.secret or secrets.token_urlsafe(48)
                    path = config.data_dir / "server.secret"
                    descriptor = os.open(
                        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "w") as target:
                        target.write(secret + "\n")
                        target.flush()
                        os.fsync(target.fileno())
                    config.secret = secret
                Core(config).initialize()
                auth = AuthStore(config)
                auth.initialize()
                username = args.username if args.username else auth.username()
                auth.bootstrap(
                    username, password, reset=args.command == "reset-password"
                )
            result = {
                "initialized": True,
                "username": username,
                "secret_file": str(config.data_dir / "server.secret"),
            }
        elif args.command == "serve":
            config.require_secret()
            logger = logging.getLogger("minder_publish")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            if not logger.handlers:
                handler = logging.StreamHandler(sys.stdout)
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger.addHandler(handler)
            import uvicorn

            from .app import create_app

            trusted = os.getenv("PUBLISH_TRUSTED_PROXIES", "").strip()
            if trusted and trusted != "*":
                try:
                    for value in trusted.split(","):
                        ipaddress.ip_network(value.strip(), strict=False)
                except ValueError as exc:
                    raise PublishError(
                        "invalid_trusted_proxies", 400, field="PUBLISH_TRUSTED_PROXIES"
                    ) from exc
            uvicorn.run(
                create_app(config),
                host=args.host,
                port=args.port,
                workers=1,
                proxy_headers=bool(trusted),
                forwarded_allow_ips=trusted or None,
                access_log=False,
            )
            return 0
        elif args.command == "doctor":
            result = doctor(config)
        elif args.command == "sweep":
            config.require_secret()
            with instance_lock(config):
                result = Core(config).initialize().sweep()
        elif args.command == "schema-downgrade":
            with instance_lock(config):
                result = Repository(config.db_path).downgrade_owner_statistics(args.target)
        elif args.command == "backup":
            result = backup(config, args.output)
        elif args.command == "restore":
            result = restore(config, args.input, args.tombstones)
        elif args.command == "tombstones":
            result = export_tombstones(config, args.output)
        elif args.command == "migrate":
            result = migrate(
                config, args.legacy_db, args.legacy_blobs, apply=args.apply
            )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if args.command != "doctor" or result.get("healthy") else 1
    except PublishError as error:
        print(
            json.dumps(
                {
                    "code": error.code,
                    "detail": error.detail,
                    "field": error.field,
                    "action": error.action,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        # Operator failures remain structured without dumping credentials/stack frames.
        print(
            json.dumps(
                {"code": "operation_failed", "detail": str(error)[:300]},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
