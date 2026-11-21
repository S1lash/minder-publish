"""Owner/device authentication and nonce-bound content handoffs.

This module stores opaque credential hashes. Publication policy remains owned by
Core and is revalidated by callbacks while handoff consumption holds a write lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from minder_publish.errors import PublishError

from .config import Config

_PUBLICATION_THROTTLE_PREFIX = "publication:"
_OWNER_CAPABILITY_HEADROOM = 1000


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str
    scope: str

    @property
    def subject(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def owner(self) -> bool:
        return self.kind == "owner"

    def can(self, operation: str) -> bool:
        return (
            self.owner
            or operation in ("read", "manage")
            and (
                self.scope == "materials:manage"
                or operation == "read"
                and self.scope == "materials:read"
            )
        )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthStore:
    schema_version = 2

    def __init__(self, config: Config):
        self.config = config
        self.hasher = PasswordHasher()
        self.password_slots = threading.BoundedSemaphore(2)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        self._invalidation_hooks: list[Callable] = []
        from .oauth.invalidation import invalidate

        self.register_invalidation_hook(invalidate)

    @contextmanager
    def connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.config.db_path, timeout=20, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        _, absolute_seconds, _, _ = self._session_policy()
        with self.connection(write=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_schema_version'"
            ).fetchone()
            if exists:
                row = connection.execute(
                    "SELECT version FROM auth_schema_version WHERE id=1"
                ).fetchone()
                if not row or row[0] not in (1, self.schema_version):
                    raise PublishError(
                        "unsupported_auth_schema",
                        503,
                        "The authentication schema version is not supported.",
                    )
                version = row[0]
            else:
                version = 0
            if (
                not exists
                and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='auth_owner'"
                ).fetchone()
            ):
                raise PublishError(
                    "unversioned_auth_schema",
                    503,
                    "Restore a consistent database before starting.",
                )
            for target in range(version + 1, self.schema_version + 1):
                source = (
                    Path(__file__).parent
                    / "migrations"
                    / "auth"
                    / f"{target:03}_up.sql"
                )
                for statement in source.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        connection.execute(statement)
                if target == 2:
                    connection.execute(
                        "UPDATE auth_sessions SET absolute_expires_at=MAX(expires_at,created_at+?),last_refreshed_at=created_at,authenticated_at=created_at",
                        (absolute_seconds,),
                    )

    def _session_policy(self) -> tuple[int, int, int, int]:
        names = (
            "owner_session_idle_days",
            "owner_session_absolute_days",
            "owner_session_refresh_seconds",
            "owner_recent_auth_seconds",
        )
        values = [self.config.operations.get(name) for name in names]
        for name, value in zip(names, values):
            if type(value) is not int or value <= 0:
                raise PublishError("invalid_session_policy", 503, field=name)
        idle, absolute, refresh, recent = values
        if idle > absolute or refresh >= idle * 86400 or recent > absolute * 86400:
            raise PublishError("invalid_session_policy", 503)
        return idle * 86400, absolute * 86400, refresh, recent

    @staticmethod
    def _active_session(connection, token_hash: str, instant: float):
        return connection.execute(
            "SELECT * FROM auth_sessions WHERE token_hash=? AND expires_at>? AND absolute_expires_at>?",
            (token_hash, instant, instant),
        ).fetchone()

    def _session_data(
        self, row, raw_token: str, instant: float, *, renewed=False
    ) -> dict:
        idle, absolute, refresh, recent = self._session_policy()
        return {
            "expires_in": max(
                0,
                math.ceil(min(row["expires_at"], row["absolute_expires_at"]) - instant),
            ),
            "expires_at": row["expires_at"],
            "absolute_expires_at": row["absolute_expires_at"],
            "authenticated_at": row["authenticated_at"],
            "recent_auth": 0 <= instant - row["authenticated_at"] < recent,
            "idle_timeout_seconds": idle,
            "absolute_timeout_seconds": absolute,
            "refresh_interval_seconds": refresh,
            "recent_auth_max_age_seconds": recent,
            "renewed": renewed,
            "csrf_token": self.csrf(raw_token),
        }

    def register_invalidation_hook(self, callback: Callable) -> None:
        if not callable(callback):
            raise TypeError("An invalidation hook must be callable.")
        if callback not in self._invalidation_hooks:
            self._invalidation_hooks.append(callback)

    def initialized(self) -> bool:
        with self.connection() as connection:
            return (
                connection.execute("SELECT 1 FROM auth_owner WHERE id=1").fetchone()
                is not None
            )

    def bootstrap(self, username: str, password: str, *, reset: bool = False) -> None:
        username = username.strip()
        if not 1 <= len(username) <= 128:
            raise PublishError("invalid_username", field="username", limit=128)
        if not 12 <= len(password.encode("utf-8")) <= 1024:
            raise PublishError(
                "invalid_owner_password",
                detail="Use a password between 12 and 1024 UTF-8 bytes.",
                field="password",
            )
        password_hash = self.hasher.hash(password)
        with self.connection(write=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM auth_owner WHERE id=1"
            ).fetchone()
            if exists and not reset:
                raise PublishError(
                    "owner_exists",
                    409,
                    "An owner already exists. Use the local reset command explicitly.",
                )
            connection.execute(
                "INSERT INTO auth_owner VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,password_hash=excluded.password_hash",
                (username, password_hash, time.time()),
            )
            self._invalidate(connection, devices=reset)

    def throttle(
        self,
        bucket: str,
        maximum: int = 10,
        seconds: int = 900,
        *,
        policy: Callable[[sqlite3.Connection], tuple[int, int]] | None = None,
        namespace: Literal["authentication", "publication"] = "authentication",
    ) -> None:
        if namespace not in ("authentication", "publication"):
            raise ValueError("Unsupported throttle namespace")
        key = hmac.new(
            self.config.secret.encode(), bucket.encode(), hashlib.sha256
        ).hexdigest()
        if namespace == "publication":
            key = _PUBLICATION_THROTTLE_PREFIX + key
        budget_operator = "LIKE" if namespace == "publication" else "NOT LIKE"
        now = time.time()
        with self.connection(write=True) as connection:
            connection.execute("DELETE FROM auth_attempts WHERE expires_at<=?", (now,))
            if policy is not None:
                maximum, seconds = policy(connection)
            row = connection.execute(
                "SELECT expires_at,count FROM auth_attempts WHERE bucket_hash=?", (key,)
            ).fetchone()
            if row and row["count"] >= maximum:
                raise PublishError(
                    "rate_limited",
                    429,
                    "Too many attempts. Try again later.",
                    limit=maximum,
                    retry_after=max(1, math.ceil(row["expires_at"] - now)),
                    action="retry_later",
                )
            if not row:
                pool = connection.execute(
                    f"SELECT COUNT(*) AS size, MIN(expires_at) AS first_expiry "
                    f"FROM auth_attempts WHERE bucket_hash {budget_operator} ?",
                    (_PUBLICATION_THROTTLE_PREFIX + "%",),
                ).fetchone()
                if pool["size"] >= 10000:
                    raise PublishError(
                        "rate_limited",
                        429,
                        f"The {namespace} admission budget is full.",
                        retry_after=max(1, math.ceil(pool["first_expiry"] - now)),
                        action="retry_later",
                    )
            connection.execute(
                "INSERT INTO auth_attempts(bucket_hash,started_at,count,expires_at) VALUES(?,?,1,?) ON CONFLICT(bucket_hash) DO UPDATE SET count=count+1",
                (key, now, now + seconds),
            )

    def login(self, username: str, password: str, peer: str) -> tuple[str, dict]:
        if len(username) > 128 or len(password.encode("utf-8")) > 1024:
            raise PublishError(
                "invalid_credentials", 401, "Invalid username or password."
            )
        self.throttle(f"login:{peer}")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT username,password_hash FROM auth_owner WHERE id=1"
            ).fetchone()
        expected = row["password_hash"] if row else self.dummy_hash
        valid = self.verify_password(expected, password)
        if (
            not row
            or not hmac.compare_digest(row["username"].encode(), username.encode())
            or not valid
        ):
            raise PublishError(
                "invalid_credentials", 401, "Invalid username or password."
            )
        token = secrets.token_urlsafe(32)
        idle, absolute, _, _ = self._session_policy()
        with self.connection(write=True) as connection:
            current_owner = connection.execute(
                "SELECT username,password_hash FROM auth_owner WHERE id=1"
            ).fetchone()
            if (
                not current_owner
                or current_owner["username"] != row["username"]
                or current_owner["password_hash"] != expected
            ):
                raise PublishError(
                    "invalid_credentials", 401, "Invalid username or password."
                )
            instant = time.time()
            connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at<=? OR absolute_expires_at<=?",
                (instant, instant),
            )
            self._admit(connection, "auth_sessions", 100)
            connection.execute(
                "INSERT INTO auth_sessions(token_hash,created_at,expires_at,absolute_expires_at,last_refreshed_at,authenticated_at) VALUES(?,?,?,?,?,?)",
                (
                    digest(token),
                    instant,
                    instant + idle,
                    instant + absolute,
                    instant,
                    instant,
                ),
            )
            session = self._active_session(connection, digest(token), instant)
        return token, {
            "username": row["username"],
            **self._session_data(session, token, instant),
        }

    def verify_password(self, password_hash: str, password: str) -> bool:
        if len(password.encode("utf-8")) > 1024:
            return False
        if not self.password_slots.acquire(blocking=False):
            raise PublishError(
                "auth_busy",
                429,
                "Authentication is busy. Try again shortly.",
                action="retry_later",
            )
        try:
            return self.hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
            return False
        finally:
            self.password_slots.release()

    def csrf(self, owner_token: str) -> str:
        return hmac.new(
            self.config.secret.encode(), f"csrf:{owner_token}".encode(), hashlib.sha256
        ).hexdigest()

    def validate_csrf(self, token: str, supplied: str | None) -> bool:
        return (
            isinstance(supplied, str)
            and len(supplied) == 64
            and hmac.compare_digest(self.csrf(token).encode(), supplied.encode())
        )

    def session(self, raw_token: str | None, *, connection=None) -> Principal | None:
        if not raw_token or len(raw_token) > 256:
            return None
        with (self.connection() if connection is None else nullcontext(connection)) as database:
            row = self._active_session(database, digest(raw_token), time.time())
        return Principal("owner", row["token_hash"], "owner") if row else None

    def session_info(self, raw_token: str) -> dict:
        if not raw_token or len(raw_token) > 256:
            raise PublishError("authentication_required", 401)
        instant = time.time()
        with self.connection() as connection:
            row = self._active_session(connection, digest(raw_token), instant)
        if not row:
            raise PublishError("authentication_required", 401)
        return self._session_data(row, raw_token, instant)

    def refresh_session(self, raw_token: str) -> dict:
        """Extend only an already valid session after the transport verifies trusted activity."""
        if not raw_token or len(raw_token) > 256:
            raise PublishError("authentication_required", 401)
        idle, _, refresh, _ = self._session_policy()
        with self.connection(write=True) as connection:
            instant = time.time()
            row = self._active_session(connection, digest(raw_token), instant)
            if not row:
                raise PublishError("authentication_required", 401)
            renewed = instant - row["last_refreshed_at"] >= refresh
            if renewed:
                connection.execute(
                    "UPDATE auth_sessions SET expires_at=?,last_refreshed_at=? WHERE token_hash=?",
                    (
                        min(instant + idle, row["absolute_expires_at"]),
                        instant,
                        row["token_hash"],
                    ),
                )
                row = self._active_session(connection, row["token_hash"], instant)
        return self._session_data(row, raw_token, instant, renewed=renewed)

    def recent_auth(self, raw_token: str | None) -> bool:
        if not raw_token or len(raw_token) > 256:
            return False
        try:
            return self.session_info(raw_token)["recent_auth"]
        except PublishError as error:
            if error.status != 401:
                raise
            return False

    def require_recent_auth(
        self, raw_token: str | None, *, connection: sqlite3.Connection | None = None
    ) -> Principal:
        if not raw_token or len(raw_token) > 256:
            raise PublishError("authentication_required", 401)
        if connection is None:
            with self.connection() as selected:
                return self.require_recent_auth(raw_token, connection=selected)
        instant = time.time()
        row = self._active_session(connection, digest(raw_token), instant)
        if not row:
            raise PublishError("authentication_required", 401)
        if not 0 <= instant - row["authenticated_at"] < self._session_policy()[3]:
            raise PublishError(
                "reauthentication_required", 403, action="reauthenticate"
            )
        return Principal("owner", row["token_hash"], "owner")

    def reauthenticate(self, raw_token: str, password: str, peer: str) -> dict:
        if not raw_token or len(raw_token) > 256:
            raise PublishError("authentication_required", 401)
        with self.connection() as connection:
            if not self._active_session(connection, digest(raw_token), time.time()):
                raise PublishError("authentication_required", 401)
            owner = connection.execute(
                "SELECT password_hash FROM auth_owner WHERE id=1"
            ).fetchone()
        self.throttle(f"reauth:{digest(raw_token)}:{peer}")
        if not owner or not self.verify_password(owner["password_hash"], password):
            raise PublishError(
                "invalid_credentials", 401, "Invalid username or password."
            )
        idle, _, _, _ = self._session_policy()
        with self.connection(write=True) as connection:
            instant = time.time()
            row = self._active_session(connection, digest(raw_token), instant)
            current_owner = connection.execute(
                "SELECT password_hash FROM auth_owner WHERE id=1"
            ).fetchone()
            if (
                not row
                or not current_owner
                or current_owner["password_hash"] != owner["password_hash"]
            ):
                raise PublishError("authentication_required", 401)
            connection.execute(
                "UPDATE auth_sessions SET authenticated_at=?,last_refreshed_at=?,expires_at=? WHERE token_hash=?",
                (
                    instant,
                    instant,
                    min(instant + idle, row["absolute_expires_at"]),
                    row["token_hash"],
                ),
            )
            row = self._active_session(connection, row["token_hash"], instant)
        return self._session_data(row, raw_token, instant, renewed=True)

    def username(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT username FROM auth_owner WHERE id=1"
            ).fetchone()
        return row[0] if row else ""

    def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        subject = digest(raw_token)
        with self.connection(write=True) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?", (subject,)
            )
            for table in ("auth_readers", "auth_intents", "auth_handoffs"):
                cursor = ""
                while True:
                    rows = connection.execute(
                        f"SELECT token_hash,payload FROM {table} WHERE token_hash>? ORDER BY token_hash LIMIT 128",
                        (cursor,),
                    ).fetchall()
                    if not rows:
                        break
                    cursor = rows[-1]["token_hash"]
                    for row in rows:
                        value = json.loads(row["payload"])
                        if (
                            value.get("subject_kind") == "owner"
                            and value.get("subject_id") == subject
                        ):
                            connection.execute(
                                f"DELETE FROM {table} WHERE token_hash=?",
                                (row["token_hash"],),
                            )

    def token(self, raw_token: str | None) -> Principal | None:
        if not raw_token or len(raw_token) > 512:
            return None
        key = digest(raw_token)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,scope FROM auth_devices WHERE token_hash=? AND revoked_at IS NULL",
                (key,),
            ).fetchone()
        if not row:
            return None
        return Principal("device", row["id"], row["scope"])

    def create_token(
        self,
        name: str,
        scope: str,
        *,
        authorize: Callable[[sqlite3.Connection], Principal] | None = None,
    ) -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 128 or scope not in (
            "materials:read",
            "materials:manage",
        ):
            raise PublishError(
                "invalid_device_token",
                field="name" if not name or len(name) > 128 else "scope",
            )
        identifier = secrets.token_hex(16)
        raw = "mp_" + secrets.token_urlsafe(32)
        created = time.time()
        with self.connection(write=True) as connection:
            if authorize is not None:
                authorize(connection)
            if (
                connection.execute(
                    "SELECT count(*) FROM auth_devices WHERE revoked_at IS NULL"
                ).fetchone()[0]
                >= 100
            ):
                raise PublishError(
                    "device_limit",
                    409,
                    "Revoke an unused device token first.",
                    limit=100,
                )
            connection.execute(
                "INSERT INTO auth_devices(id,token_hash,name,scope,created_at) VALUES(?,?,?,?,?)",
                (identifier, digest(raw), name, scope, created),
            )
        return {
            "id": identifier,
            "name": name,
            "scope": scope,
            "created_at": created,
            "token": raw,
        }

    def list_tokens(self) -> list[dict]:
        with self.connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,name,scope,created_at,last_used_at,revoked_at FROM auth_devices ORDER BY created_at DESC LIMIT 200"
                )
            ]

    def revoke_token(self, identifier: str) -> None:
        with self.connection(write=True) as connection:
            connection.execute(
                "UPDATE auth_devices SET revoked_at=? WHERE id=?",
                (time.time(), identifier),
            )

    def viewer(self, raw_token: str | None) -> tuple[str, str]:
        now = time.time()
        if raw_token and len(raw_token) <= 256:
            key = digest(raw_token)
            with self.connection() as connection:
                if connection.execute(
                    "SELECT 1 FROM auth_viewers WHERE token_hash=? AND expires_at>?",
                    (key, now),
                ).fetchone():
                    return raw_token, key
        raw_token = secrets.token_urlsafe(32)
        key = digest(raw_token)
        with self.connection(write=True) as connection:
            self._admit(connection, "auth_viewers", 10000)
            connection.execute(
                "INSERT INTO auth_viewers VALUES(?,?)",
                (key, now + int(self.config.operations["viewer_grant_hours"]) * 3600),
            )
        return raw_token, key

    def existing_viewer(self, raw_token: str | None) -> str | None:
        if not raw_token or len(raw_token) > 256:
            return None
        key = digest(raw_token)
        with self.connection() as connection:
            return (
                key
                if connection.execute(
                    "SELECT 1 FROM auth_viewers WHERE token_hash=? AND expires_at>?",
                    (key, time.time()),
                ).fetchone()
                else None
            )

    def unlock(self, viewer_hash: str, publication: dict) -> None:
        expires = time.time() + int(self.config.operations["viewer_grant_hours"]) * 3600
        with self.connection(write=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM auth_unlocks WHERE viewer_hash=? AND publication_id=?",
                (viewer_hash, publication["id"]),
            ).fetchone():
                self._admit(connection, "auth_unlocks", 20000)
            connection.execute(
                "INSERT INTO auth_unlocks VALUES(?,?,?,?) ON CONFLICT(viewer_hash,publication_id) DO UPDATE SET access_version=excluded.access_version,expires_at=excluded.expires_at",
                (
                    viewer_hash,
                    publication["id"],
                    publication["access_version"],
                    expires,
                ),
            )

    def unlocked(self, viewer_hash: str | None, publication: dict) -> bool:
        if not viewer_hash:
            return False
        with self.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM auth_unlocks WHERE viewer_hash=? AND publication_id=? AND access_version=? AND expires_at>?",
                    (
                        viewer_hash,
                        publication["id"],
                        publication["access_version"],
                        time.time(),
                    ),
                ).fetchone()
                is not None
            )

    @classmethod
    def _subject_active(cls, connection, kind, identifier, publication):
        if kind == "anonymous":
            return publication["access"] == "link"
        if kind == "owner":
            return cls._active_session(connection, identifier, time.time()) is not None
        if kind == "guest":
            return (
                connection.execute(
                    "SELECT 1 FROM auth_viewers v JOIN auth_unlocks u ON v.token_hash=u.viewer_hash WHERE v.token_hash=? AND u.publication_id=? AND u.access_version=? AND v.expires_at>? AND u.expires_at>?",
                    (
                        identifier,
                        publication["id"],
                        publication["access_version"],
                        time.time(),
                        time.time(),
                    ),
                ).fetchone()
                is not None
            )
        return False

    def subject_active(self, kind: str, identifier: str, publication: dict) -> bool:
        with self.connection() as connection:
            return self._subject_active(connection, kind, identifier, publication)

    def intent(self, payload: dict) -> str:
        raw = secrets.token_urlsafe(32)
        with self.connection(write=True) as connection:
            self._admit(connection, "auth_intents", 10000, payload=payload)
            connection.execute(
                "INSERT INTO auth_intents VALUES(?,?,?,0)",
                (
                    digest(raw),
                    json.dumps(payload),
                    time.time() + int(self.config.operations["handoff_seconds"]),
                ),
            )
        return raw

    def peek_intent(self, raw: str) -> dict:
        if len(raw) > 256:
            raise PublishError("invalid_handoff", 403)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=? AND expires_at>? AND consumed=0",
                (digest(raw), time.time()),
            ).fetchone()
        if not row:
            raise PublishError("invalid_handoff", 403)
        payload = json.loads(row["payload"])
        if payload.get("purpose", "handoff") != "handoff":
            raise PublishError("invalid_handoff", 403)
        return payload

    def consume_navigation(self, raw: str, cookies: dict, validator: Callable) -> dict:
        if not isinstance(raw, str) or len(raw) != 43:
            raise PublishError("invalid_navigation", 403)
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=? AND expires_at>? AND consumed=0",
                (digest(raw), time.time()),
            ).fetchone()
            payload = json.loads(row["payload"]) if row else None
            if not isinstance(payload, dict) or payload.get("purpose") != "navigation":
                raise PublishError("invalid_navigation", 403)
            realm = payload.get("realm")
            if not isinstance(realm, str) or realm not in ("owner", "normal", "probe"):
                raise PublishError("invalid_navigation", 403)
            cookie = cookies.get(realm)
            if (
                realm not in ("owner", "normal", "probe")
                or not cookie
                or len(cookie) > 256
            ):
                raise PublishError("invalid_navigation", 403)
            identifier = digest(cookie)
            if payload.get("subject_id") != identifier:
                raise PublishError("invalid_navigation", 403)
            if realm == "owner":
                active = (
                    payload.get("subject_kind") == "owner"
                    and self._active_session(connection, identifier, time.time())
                    is not None
                )
            else:
                active = (
                    payload.get("subject_kind") == "guest"
                    and connection.execute(
                        "SELECT 1 FROM auth_viewers WHERE token_hash=? AND expires_at>?",
                        (identifier, time.time()),
                    ).fetchone()
                    is not None
                )
            resolved = validator(payload, connection) if active else None
            if not isinstance(resolved, dict):
                raise PublishError("invalid_navigation", 403)
            connection.execute(
                "UPDATE auth_intents SET consumed=1 WHERE token_hash=?", (digest(raw),)
            )
            return resolved

    def authorize_intent(
        self, raw: str, nonce_hash: str, validator: Callable[[dict], bool]
    ) -> tuple[str, dict]:
        if (
            len(raw) > 256
            or len(nonce_hash) != 64
            or any(c not in "0123456789abcdef" for c in nonce_hash)
        ):
            raise PublishError("invalid_handoff", 403)
        code = secrets.token_urlsafe(32)
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=? AND consumed=0 AND expires_at>?",
                (digest(raw), time.time()),
            ).fetchone()
            if not row:
                raise PublishError("invalid_handoff", 403)
            payload = json.loads(row["payload"])
            if payload.get("purpose", "handoff") != "handoff" or not validator(payload):
                raise PublishError("handoff_denied", 403)
            self._admit(connection, "auth_handoffs", 10000, payload=payload)
            connection.execute(
                "UPDATE auth_intents SET consumed=1 WHERE token_hash=?", (digest(raw),)
            )
            connection.execute(
                "INSERT INTO auth_handoffs VALUES(?,?,?,?,0)",
                (
                    digest(code),
                    json.dumps(payload),
                    nonce_hash,
                    time.time() + int(self.config.operations["handoff_seconds"]),
                ),
            )
        return code, payload

    def consume_handoff(
        self, code: str, nonce: str, revision_id: str, validator: Callable[[dict], bool], on_consume=None
    ) -> tuple[str, dict]:
        if not nonce or len(nonce) > 256 or len(code) > 256:
            raise PublishError("invalid_handoff", 403)
        read_token = secrets.token_urlsafe(32)
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT payload,nonce_hash FROM auth_handoffs WHERE token_hash=? AND consumed=0 AND expires_at>?",
                (digest(code), time.time()),
            ).fetchone()
            if not row or not hmac.compare_digest(row["nonce_hash"], digest(nonce)):
                raise PublishError("invalid_handoff", 403)
            payload = json.loads(row["payload"])
            if payload.get("revision_id") != revision_id or not validator(payload):
                raise PublishError("handoff_denied", 403)
            self._admit(connection, "auth_readers", 20000, payload=payload)
            connection.execute(
                "UPDATE auth_handoffs SET consumed=1 WHERE token_hash=?",
                (digest(code),),
            )
            lifetime = int(self.config.operations["viewer_grant_hours"]) * 3600
            connection.execute(
                "INSERT INTO auth_readers VALUES(?,?,?)",
                (digest(read_token), json.dumps(payload), time.time() + lifetime),
            )
            if on_consume is not None:
                on_consume(connection, payload)
        return read_token, payload

    def reader(self, raw: str | None) -> dict | None:
        if not raw or len(raw) > 256:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM auth_readers WHERE token_hash=? AND expires_at>?",
                (digest(raw), time.time()),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def resume_reader(self, intent: str, reader: str | None, revision_id: str, validator: Callable, on_consume=None) -> dict | None:
        """Consume an opening only when this browser already has its exact capability."""
        if not reader or len(reader) > 256 or not intent or len(intent) > 256:
            return None
        with self.connection(write=True) as connection:
            instant = time.time()
            opening_row = connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=? AND expires_at>? AND consumed=0",
                (digest(intent), instant),
            ).fetchone()
            reader_row = connection.execute(
                "SELECT payload FROM auth_readers WHERE token_hash=? AND expires_at>?",
                (digest(reader), instant),
            ).fetchone()
            if not opening_row or not reader_row:
                return None
            opening, existing = json.loads(opening_row["payload"]), json.loads(reader_row["payload"])
            fields = (
                "publication_id", "revision_id", "subject_kind", "subject_id", "access_version",
                "owner_history", "shared_revision", "revision_share_version", "view_mode",
            )
            if (
                opening.get("purpose", "handoff") != "handoff"
                or opening.get("view_mode") != "normal"
                or opening.get("opening_method") != "GET"
                or opening.get("opening_prefetch", False)
                or opening.get("revision_id") != revision_id
                or any(opening.get(field) != existing.get(field) for field in fields)
                or not validator(opening, connection)
                or not validator(existing, connection)
            ):
                return None
            connection.execute("UPDATE auth_intents SET consumed=1 WHERE token_hash=?", (digest(intent),))
            connection.execute(
                "UPDATE auth_readers SET payload=?,expires_at=? WHERE token_hash=?",
                (json.dumps(opening), instant + int(self.config.operations["viewer_grant_hours"]) * 3600, digest(reader)),
            )
            if on_consume is not None:
                on_consume(connection, opening)
            return opening

    @staticmethod
    def _admit(
        connection: sqlite3.Connection,
        table: str,
        maximum: int,
        *,
        payload: dict | None = None,
    ) -> None:
        connection.execute(f"DELETE FROM {table} WHERE expires_at<=?", (time.time(),))
        if payload is not None and payload.get("subject_kind") == "owner":
            maximum += _OWNER_CAPABILITY_HEADROOM
        if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= maximum:
            raise PublishError(
                "auth_capacity",
                429,
                "The authentication admission budget is full.",
                limit=maximum,
                action="retry_later",
            )

    def _invalidate(self, connection: sqlite3.Connection, devices: bool = True) -> None:
        for name in (
            "auth_sessions",
            "auth_viewers",
            "auth_unlocks",
            "auth_intents",
            "auth_handoffs",
            "auth_readers",
        ):
            connection.execute(f"DELETE FROM {name}")
        if devices:
            connection.execute(
                "UPDATE auth_devices SET revoked_at=? WHERE revoked_at IS NULL",
                (time.time(),),
            )
        for callback in self._invalidation_hooks:
            callback(connection, devices=devices)

    def invalidate_all(self) -> None:
        with self.connection(write=True) as connection:
            self._invalidate(connection)

    def cleanup(self) -> None:
        now = time.time()
        with self.connection(write=True) as connection:
            for table in (
                "auth_sessions",
                "auth_viewers",
                "auth_unlocks",
                "auth_intents",
                "auth_handoffs",
                "auth_readers",
            ):
                connection.execute(f"DELETE FROM {table} WHERE expires_at<?", (now,))
            connection.execute("DELETE FROM auth_attempts WHERE expires_at<=?", (now,))
            connection.execute(
                "DELETE FROM auth_sessions WHERE absolute_expires_at<=?", (now,)
            )
