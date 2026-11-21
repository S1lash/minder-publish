"""Bounded durable OAuth grants. No protocol SDK or web framework dependency."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files

from minder_publish.errors import PublishError

from ..repository import Repository
from .invalidation import invalidate

SCOPES = frozenset({"materials:read", "materials:manage"})


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthFailure(Exception):
    def __init__(self, code="invalid_grant", status=400):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class OAuthOptions:
    access_seconds: int = 900
    code_seconds: int = 120
    pending_seconds: int = 600
    refresh_seconds: int = 30 * 86400
    registration_seconds: int = 86400
    maximum_clients: int = 128
    maximum_pending: int = 256
    maximum_connections: int = 128
    maximum_tokens: int = 32768
    maximum_body_bytes: int = 16384

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("OAuth limits must be positive integers")


class OAuthStore:
    def __init__(self, config, options=None, clock=time.time):
        self.config = config
        self.options = options or OAuthOptions()
        self.clock = clock
        self.repository = Repository(config.db_path)
        self.resource = config.base_url + "/mcp"

    def initialize(self):
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.config.db_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with self.repository.connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_schema_version'"
            ).fetchone()
            if exists:
                rows = connection.execute(
                    "SELECT id,version FROM oauth_schema_version"
                ).fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (1, 1):
                    raise PublishError("unknown_oauth_schema", 409)
            else:
                source = (
                    files("minder_publish.server")
                    .joinpath("migrations/oauth/001_up.sql")
                    .read_text()
                )
                for statement in source.split(";"):
                    if statement.strip():
                        connection.execute(statement)
        return self

    invalidate = staticmethod(invalidate)

    def _cleanup(self, connection):
        current = int(self.clock())
        connection.execute("DELETE FROM oauth_pending WHERE expires_at<=?", (current,))
        connection.execute("DELETE FROM oauth_code WHERE expires_at<=?", (current,))
        connection.execute("DELETE FROM oauth_token WHERE expires_at<=?", (current,))
        connection.execute(
            "DELETE FROM oauth_token WHERE family_id IN (SELECT id FROM oauth_family WHERE revoked_at IS NOT NULL)"
        )
        connection.execute("DELETE FROM oauth_family WHERE revoked_at IS NOT NULL")
        connection.execute("DELETE FROM oauth_family WHERE expires_at<=?", (current,))
        connection.execute(
            "DELETE FROM oauth_client WHERE expires_at<=? AND id NOT IN (SELECT client_id FROM oauth_family WHERE revoked_at IS NULL)",
            (current,),
        )

    def register(self, client_id, metadata, secret=None):
        with self.repository.connect(immediate=True) as connection:
            self._cleanup(connection)
            if (
                connection.execute("SELECT count(*) FROM oauth_client").fetchone()[0]
                >= self.options.maximum_clients
            ):
                raise OAuthFailure("registration_limit", 429)
            current = int(self.clock())
            connection.execute(
                "INSERT INTO oauth_client VALUES (?,?,?,?,?)",
                (
                    client_id,
                    json.dumps(metadata),
                    digest(secret) if secret else None,
                    current,
                    current + self.options.registration_seconds,
                ),
            )

    def client(self, client_id):
        if not isinstance(client_id, str) or len(client_id) > 256:
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_client WHERE id=?", (client_id,)
            ).fetchone()
            if row and (
                row["expires_at"] > int(self.clock())
                or connection.execute(
                    "SELECT 1 FROM oauth_family WHERE client_id=? AND revoked_at IS NULL AND expires_at>?",
                    (client_id, int(self.clock())),
                ).fetchone()
            ):
                return {**dict(row), "metadata": json.loads(row["metadata"])}
        return None

    def _scopes(self, scopes):
        if not scopes or len(scopes) > 2 or not set(scopes) <= SCOPES:
            raise OAuthFailure("invalid_scope")
        return sorted(set(scopes))

    def pending(self, client_id, payload):
        raw = secrets.token_urlsafe(32)
        with self.repository.connect(immediate=True) as connection:
            self._cleanup(connection)
            if (
                connection.execute("SELECT count(*) FROM oauth_pending").fetchone()[0]
                >= self.options.maximum_pending
            ):
                raise OAuthFailure("temporarily_unavailable", 429)
            connection.execute(
                "INSERT INTO oauth_pending VALUES (?,?,?,?,NULL)",
                (
                    digest(raw),
                    client_id,
                    json.dumps(payload),
                    int(self.clock()) + self.options.pending_seconds,
                ),
            )
        return raw

    def pending_details(self, raw, session_hash):
        with self.repository.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM oauth_pending WHERE id=? AND expires_at>?",
                (digest(raw), int(self.clock())),
            ).fetchone()
            if not row or (row["session_hash"] and row["session_hash"] != session_hash):
                raise OAuthFailure()
            connection.execute(
                "UPDATE oauth_pending SET session_hash=? WHERE id=?",
                (session_hash, row["id"]),
            )
            payload = json.loads(row["payload"])
            if payload.get("resource") != self.resource:
                raise OAuthFailure("invalid_target")
            return {"client_id": row["client_id"], **payload}

    def consent(self, raw, session_hash, approve, authorize_owner):
        with self.repository.connect(immediate=True) as connection:
            authorize_owner(connection)
            row = connection.execute(
                "SELECT * FROM oauth_pending WHERE id=? AND expires_at>?",
                (digest(raw), int(self.clock())),
            ).fetchone()
            if not row or row["session_hash"] != session_hash:
                raise OAuthFailure()
            connection.execute("DELETE FROM oauth_pending WHERE id=?", (row["id"],))
            payload = {"client_id": row["client_id"], **json.loads(row["payload"])}
            if payload.get("resource") != self.resource:
                raise OAuthFailure("invalid_target")
            if not approve:
                return None, payload
            self._cleanup(connection)
            if (
                connection.execute("SELECT count(*) FROM oauth_code").fetchone()[0]
                >= self.options.maximum_pending
            ):
                raise OAuthFailure("temporarily_unavailable", 429)
            code = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO oauth_code VALUES (?,?,?,?,NULL,NULL)",
                (
                    digest(code),
                    row["client_id"],
                    json.dumps(payload),
                    int(self.clock()) + self.options.code_seconds,
                ),
            )
            return code, payload

    def code(self, raw, client_id):
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_code WHERE id=? AND client_id=? AND expires_at>?",
                (digest(raw), client_id, int(self.clock())),
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            return (
                {**dict(row), "payload": payload}
                if payload.get("resource") == self.resource
                else None
            )

    def _tokens(self, connection, family_id, scopes):
        self._cleanup(connection)
        if (
            connection.execute("SELECT count(*) FROM oauth_token").fetchone()[0] + 2
            > self.options.maximum_tokens
        ):
            raise OAuthFailure("temporarily_unavailable", 429)
        current = int(self.clock())
        family = connection.execute(
            "SELECT * FROM oauth_family WHERE id=?", (family_id,)
        ).fetchone()
        if (
            not family
            or family["expires_at"] <= current
            or family["revoked_at"] is not None
            or family["resource"] != self.resource
        ):
            raise OAuthFailure()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        access_expiry = min(current + self.options.access_seconds, family["expires_at"])
        for raw, kind, expires in (
            (access, "access", access_expiry),
            (refresh, "refresh", family["expires_at"]),
        ):
            connection.execute(
                "INSERT INTO oauth_token VALUES (?,?,?,?,?,?,NULL)",
                (digest(raw), kind, family_id, json.dumps(scopes), current, expires),
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": access_expiry - current,
            "refresh_token": refresh,
            "scope": " ".join(scopes),
        }

    def exchange_code(self, raw, client_id):
        invalid = False
        result = None
        with self.repository.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM oauth_code WHERE id=? AND client_id=? AND expires_at>?",
                (digest(raw), client_id, int(self.clock())),
            ).fetchone()
            if not row or json.loads(row["payload"]).get("resource") != self.resource:
                raise OAuthFailure()
            if row["used_at"] is not None:
                connection.execute(
                    "UPDATE oauth_family SET revoked_at=? WHERE id=?",
                    (int(self.clock()), row["family_id"]),
                )
                invalid = True
            else:
                self._cleanup(connection)
                active = connection.execute(
                    "SELECT count(*) FROM oauth_family WHERE revoked_at IS NULL AND client_id!=?",
                    (client_id,),
                ).fetchone()[0]
                if active >= self.options.maximum_connections:
                    raise OAuthFailure("temporarily_unavailable", 429)
                payload = json.loads(row["payload"])
                scopes = self._scopes(payload["scopes"])
                family = secrets.token_hex(16)
                current = int(self.clock())
                connection.execute(
                    "UPDATE oauth_family SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
                    (current, client_id),
                )
                connection.execute(
                    "INSERT INTO oauth_family VALUES (?,?,?,?,?,?,?,NULL)",
                    (
                        family,
                        client_id,
                        json.dumps(scopes),
                        "owner",
                        self.resource,
                        current,
                        current + self.options.refresh_seconds,
                    ),
                )
                connection.execute(
                    "UPDATE oauth_code SET used_at=?,family_id=? WHERE id=?",
                    (current, family, row["id"]),
                )
                result = self._tokens(connection, family, scopes)
        if invalid:
            raise OAuthFailure()
        return result

    def token(self, raw, *, kind=None, client_id=None, include_used=False):
        if not isinstance(raw, str) or not 32 <= len(raw) <= 512:
            return None
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT t.*,f.client_id,f.subject,f.resource,f.revoked_at,f.expires_at AS family_expiry FROM oauth_token t JOIN oauth_family f ON f.id=t.family_id WHERE t.id=?",
                (digest(raw),),
            ).fetchone()
            if (
                not row
                or row["expires_at"] <= int(self.clock())
                or row["family_expiry"] <= int(self.clock())
                or row["revoked_at"] is not None
                or row["resource"] != self.resource
            ):
                return None
            if (
                kind
                and row["kind"] != kind
                or client_id
                and row["client_id"] != client_id
            ):
                return None
            if not include_used and row["used_at"] is not None:
                return None
            return {**dict(row), "scopes": json.loads(row["scopes"])}

    def refresh(self, raw, client_id, scopes):
        scopes = self._scopes(scopes)
        invalid = False
        result = None
        with self.repository.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT t.*,f.client_id,f.resource,f.revoked_at,f.expires_at AS family_expiry FROM oauth_token t JOIN oauth_family f ON f.id=t.family_id WHERE t.id=? AND t.kind='refresh' AND f.client_id=?",
                (digest(raw), client_id),
            ).fetchone()
            if (
                not row
                or row["expires_at"] <= int(self.clock())
                or row["family_expiry"] <= int(self.clock())
                or row["revoked_at"] is not None
                or row["resource"] != self.resource
            ):
                raise OAuthFailure()
            if row["used_at"] is not None:
                connection.execute(
                    "UPDATE oauth_family SET revoked_at=? WHERE id=?",
                    (int(self.clock()), row["family_id"]),
                )
                invalid = True
            else:
                if not set(scopes) <= set(json.loads(row["scopes"])):
                    raise OAuthFailure("invalid_scope")
                connection.execute(
                    "UPDATE oauth_token SET used_at=? WHERE family_id=? AND kind='access' AND used_at IS NULL",
                    (int(self.clock()), row["family_id"]),
                )
                connection.execute(
                    "UPDATE oauth_token SET used_at=? WHERE id=?",
                    (int(self.clock()), row["id"]),
                )
                result = self._tokens(connection, row["family_id"], scopes)
                connection.execute(
                    "UPDATE oauth_family SET scopes=? WHERE id=?",
                    (json.dumps(scopes), row["family_id"]),
                )
        if invalid:
            raise OAuthFailure()
        return result

    def principal(self, raw, resource):
        if resource != self.resource:
            return None
        row = self.token(raw, kind="access")
        if not row or row["resource"] != resource:
            return None
        return {
            "id": row["family_id"],
            "client_id": row["client_id"],
            "scope": "materials:manage"
            if "materials:manage" in row["scopes"]
            else "materials:read",
            "scopes": row["scopes"],
            "subject": row["subject"],
            "resource": row["resource"],
        }

    def revoke_token(self, raw, client_id):
        with self.repository.connect(immediate=True) as connection:
            connection.execute(
                "UPDATE oauth_family SET revoked_at=? WHERE client_id=? AND id IN (SELECT family_id FROM oauth_token WHERE id=?)",
                (int(self.clock()), client_id, digest(raw)),
            )

    def connections(self):
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT f.*,c.metadata FROM oauth_family f JOIN oauth_client c ON c.id=f.client_id WHERE f.revoked_at IS NULL AND f.expires_at>? ORDER BY f.created_at DESC",
                (int(self.clock()),),
            ).fetchall()

            def stamp(value):
                return (
                    datetime.fromtimestamp(value, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            return [
                {
                    "id": row["id"],
                    "client_id": row["client_id"],
                    "name": json.loads(row["metadata"]).get("client_name")
                    or "OAuth application",
                    "scopes": json.loads(row["scopes"]),
                    "created_at": stamp(row["created_at"]),
                    "expires_at": stamp(row["expires_at"]),
                }
                for row in rows
            ]

    def revoke_connection(self, identifier):
        with self.repository.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT client_id FROM oauth_family WHERE id=? AND revoked_at IS NULL",
                (identifier,),
            ).fetchone()
            if not row:
                return
            connection.execute(
                "UPDATE oauth_family SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (int(self.clock()), identifier),
            )
            # A code approved before revocation must not recreate the app's grant.
            connection.execute(
                "DELETE FROM oauth_code WHERE client_id=?", (row["client_id"],)
            )
            connection.execute(
                "DELETE FROM oauth_pending WHERE client_id=?", (row["client_id"],)
            )

    def cleanup(self):
        with self.repository.connect(immediate=True) as connection:
            before = connection.total_changes
            self._cleanup(connection)
            return {"deleted": connection.total_changes - before}
