"""Nonsecret browser-upload intents, bound to creators and ordinary owner uploads."""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone

from minder_publish.errors import PublishError

from .auth import Principal
from .core import _PLAN_KEYS, dumps
from .oauth.invalidation import active_grant_family
from .publication_links import stable_url
from .storage import normalize_path, safe_id

MAXIMUM_JOBS = 128
JOB_TTL_SECONDS = 3600
TERMINAL_UPLOADS = frozenset({"committed", "aborted", "failed"})
OPEN_JOBS = frozenset({"pending", "uploading"})
INTENT_FIELDS = frozenset(
    {"kind", "title", "summary", "access", "expiry", "entry_path"}
)
BROWSER_FIELDS = frozenset(_PLAN_KEYS - {"publication_id", "expected_revision"})
SCHEMA = """
CREATE TABLE upload_job_schema_version (id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL);
INSERT INTO upload_job_schema_version VALUES(1,1);
CREATE TABLE upload_job (
    id TEXT PRIMARY KEY,
    creator_kind TEXT NOT NULL CHECK(creator_kind IN ('device','oauth')),
    creator_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    owner_id TEXT,
    upload_id TEXT UNIQUE,
    publication_id TEXT
);
CREATE INDEX upload_job_expiry ON upload_job(expires_at);
"""


def timestamp(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class UploadJobs:
    """The callback guards run within Core's write transaction, before commit."""

    def __init__(
        self,
        core,
        *,
        creator_active=None,
        clock=time.time,
        maximum_jobs=MAXIMUM_JOBS,
        ttl_seconds=JOB_TTL_SECONDS,
    ):
        if type(maximum_jobs) is not int or not 1 <= maximum_jobs <= MAXIMUM_JOBS:
            raise ValueError("Job capacity must be between 1 and 128.")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= JOB_TTL_SECONDS:
            raise ValueError("Job lifetime must be between 1 and 3600 seconds.")
        self.core = core
        self.repository = core.repository
        self.creator_active = creator_active or self._creator_active
        self.clock = clock
        self.maximum_jobs = maximum_jobs
        self.ttl_seconds = ttl_seconds

    def initialize(self):
        with self.repository.connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_job_schema_version'"
            ).fetchone()
            if exists:
                versions = connection.execute(
                    "SELECT id,version FROM upload_job_schema_version"
                ).fetchall()
                if len(versions) != 1 or tuple(versions[0]) != (1, 1):
                    raise PublishError("unknown_upload_job_schema", 503)
            else:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='upload_job'"
                ).fetchone():
                    raise PublishError("unversioned_upload_job_schema", 503)
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
        return self

    @staticmethod
    def _authenticated(principal):
        if not isinstance(principal, Principal):
            raise PublishError("authentication_required", 401)

    def _owner_active(self, connection, owner_id):
        instant = self.clock()
        return (
            connection.execute(
                "SELECT 1 FROM auth_sessions WHERE token_hash=? AND expires_at>? AND absolute_expires_at>?",
                (owner_id, instant, instant),
            ).fetchone()
            is not None
        )

    def _creator_active(self, connection, principal):
        if principal.kind == "device":
            return (
                connection.execute(
                    "SELECT 1 FROM auth_devices WHERE id=? AND scope='materials:manage' AND revoked_at IS NULL",
                    (principal.id,),
                ).fetchone()
                is not None
            )
        if principal.kind == "oauth":
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_schema_version'"
            ).fetchone():
                return False
            return active_grant_family(
                connection, principal.id, resource=self.core.config.base_url + "/mcp"
            )
        return False

    def _creator(self, row):
        return Principal(row["creator_kind"], row["creator_id"], "materials:manage")

    def _identifier(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[a-f0-9]{32}", identifier
        ):
            raise PublishError("upload_job_not_found", 404)
        return identifier

    def _job(self, connection, identifier, principal):
        self._authenticated(principal)
        row = connection.execute(
            "SELECT * FROM upload_job WHERE id=?", (self._identifier(identifier),)
        ).fetchone()
        if principal.owner:
            if not self._owner_active(connection, principal.id):
                raise PublishError("authentication_required", 401)
        elif (
            not row
            or principal.kind != row["creator_kind"]
            or principal.id != row["creator_id"]
            or principal.scope != "materials:manage"
            or not self.creator_active(connection, principal)
        ):
            raise PublishError("upload_job_not_found", 404)
        if not row:
            raise PublishError("upload_job_not_found", 404)
        return row

    def _intent(self, intent):
        if not isinstance(intent, dict) or set(intent) - INTENT_FIELDS:
            raise PublishError("invalid_upload_job_intent", 400)
        if intent.get("kind") not in ("file", "site"):
            raise PublishError("invalid_kind", 400, field="kind")
        settings = self.core.policy_settings()
        for name in ("title", "summary"):
            if name in intent:
                value = intent[name]
                if not isinstance(value, str):
                    raise PublishError("invalid_text", 400, field=name)
                try:
                    value.encode("utf-8")
                except UnicodeError as error:
                    raise PublishError("invalid_text", 400, field=name) from error
                if len(value) > settings["limits"][name + "_characters"]:
                    raise PublishError(
                        "text_limit",
                        400,
                        field=name,
                        limit=settings["limits"][name + "_characters"],
                    )
        access = intent.get("access", settings["defaults"]["access"])
        if access not in ("private", "link", "password"):
            raise PublishError("invalid_access", 400, field="access")
        if "expiry" in intent:
            self.core._expiry(intent["expiry"], access, settings["defaults"])
        if "entry_path" in intent:
            if intent["kind"] != "site":
                raise PublishError("invalid_entry_path", 400, field="entry_path")
            normalize_path(intent["entry_path"], settings["limits"])
        return json.loads(dumps(intent))

    def _owned_upload(self, connection, row):
        if not row["upload_id"] or not row["owner_id"]:
            return None
        upload = connection.execute(
            "SELECT * FROM upload WHERE id=?", (row["upload_id"],)
        ).fetchone()
        if not upload or upload["subject"] != "owner:" + row["owner_id"]:
            return None
        if json.loads(upload["data"]).get("upload_job_id") != row["id"]:
            return None
        return upload

    def _synchronize(self, connection, row):
        upload = self._owned_upload(connection, row)
        if upload and upload["state"] == "committed":
            publication_id = json.loads(upload["data"])["publication"]["id"]
            connection.execute(
                "UPDATE upload_job SET state='published',publication_id=? WHERE id=?",
                (publication_id, row["id"]),
            )
        elif row["state"] in OPEN_JOBS:
            state = None
            if row["expires_at"] <= self.clock():
                state = "expired"
            elif not self.creator_active(connection, self._creator(row)):
                state = "revoked"
            elif row["owner_id"] and not self._owner_active(
                connection, row["owner_id"]
            ):
                state = "cancelled"
            elif upload and upload["state"] in ("aborted", "failed", "aborting"):
                state = "failed"
            if state:
                connection.execute(
                    "UPDATE upload_job SET state=? WHERE id=?", (state, row["id"])
                )
        return connection.execute(
            "SELECT * FROM upload_job WHERE id=?", (row["id"],)
        ).fetchone()

    def _result(self, row, principal):
        result = {
            "id": row["id"],
            "state": row["state"],
            "intent": json.loads(row["intent"]),
            "created_at": timestamp(row["created_at"]),
            "expires_at": timestamp(row["expires_at"]),
        }
        if row["publication_id"]:
            with self.repository.connect() as connection:
                link = connection.execute(
                    "SELECT name FROM publication_link WHERE publication_id=? AND is_primary=1",
                    (row["publication_id"],),
                ).fetchone()
            result.update(
                publication_id=row["publication_id"],
                stable_url=stable_url(self.core.config.base_url, link["name"] if link else row["publication_id"]),
            )
        if principal.owner:
            result["claimed_by_current_session"] = row["owner_id"] == principal.id
            if row["owner_id"] == principal.id and row["upload_id"]:
                result["upload_id"] = row["upload_id"]
        return result

    def create(self, intent, creator):
        self._authenticated(creator)
        if (
            creator.kind not in ("device", "oauth")
            or creator.scope != "materials:manage"
        ):
            raise PublishError("insufficient_scope", 403)
        intent = self._intent(intent)
        self.cleanup()
        with self.repository.connect(immediate=True) as connection:
            if not self.creator_active(connection, creator):
                raise PublishError("invalid_credentials", 401)
            if (
                connection.execute("SELECT COUNT(*) FROM upload_job").fetchone()[0]
                >= self.maximum_jobs
            ):
                raise PublishError("upload_jobs_busy", 429, limit=self.maximum_jobs)
            identifier = secrets.token_hex(16)
            instant = self.clock()
            connection.execute(
                "INSERT INTO upload_job VALUES(?,?,?,?,'pending',?,?,NULL,NULL,NULL)",
                (
                    identifier,
                    creator.kind,
                    creator.id,
                    dumps(intent),
                    instant,
                    instant + self.ttl_seconds,
                ),
            )
            row = connection.execute(
                "SELECT * FROM upload_job WHERE id=?", (identifier,)
            ).fetchone()
        return self._result(row, creator)

    def get(self, identifier, principal):
        self._authenticated(principal)
        self.cleanup()
        with self.repository.connect(immediate=True) as connection:
            row = self._synchronize(
                connection, self._job(connection, identifier, principal)
            )
        return self._result(row, principal)

    def start(self, identifier, owner, plan):
        self._authenticated(owner)
        if not owner.owner:
            raise PublishError("owner_required", 403)
        if not isinstance(plan, dict) or set(plan) - BROWSER_FIELDS:
            raise PublishError("invalid_upload_job_plan", 400)
        self.cleanup()
        with self.repository.connect() as connection:
            row = self._job(connection, identifier, owner)
            intent = json.loads(row["intent"])
        if "kind" in plan and plan["kind"] != intent["kind"]:
            raise PublishError("invalid_kind", 400, field="kind")
        merged = {**intent, **plan, "kind": intent["kind"], "supports_link_names": True}

        def bind(connection, upload_id):
            current = self._job(connection, identifier, owner)
            if current["state"] != "pending" or current["upload_id"]:
                raise PublishError("upload_job_claimed", 409)
            if current["expires_at"] <= self.clock():
                raise PublishError("upload_job_expired", 410)
            if not self.creator_active(connection, self._creator(current)):
                raise PublishError("upload_job_creator_revoked", 403)
            changed = connection.execute(
                "UPDATE upload_job SET state='uploading',owner_id=?,upload_id=? WHERE id=? AND state='pending' AND upload_id IS NULL",
                (owner.id, upload_id, identifier),
            ).rowcount
            if changed != 1:
                raise PublishError("upload_job_claimed", 409)
            upload = connection.execute(
                "SELECT data FROM upload WHERE id=?", (upload_id,)
            ).fetchone()
            data = json.loads(upload["data"])
            data["upload_job_id"] = identifier
            connection.execute(
                "UPDATE upload SET data=? WHERE id=?", (dumps(data), upload_id)
            )

        upload = self.core.create_upload(merged, owner.subject, on_created=bind)
        return {**upload, "job_id": identifier}

    def guard_commit(self, connection, upload_id, principal=None):
        upload = connection.execute(
            "SELECT * FROM upload WHERE id=?", (safe_id(upload_id),)
        ).fetchone()
        if not upload:
            raise PublishError("upload_not_found", 404)
        marker = json.loads(upload["data"]).get("upload_job_id")
        if marker is None:
            return False
        row = connection.execute(
            "SELECT * FROM upload_job WHERE id=?", (marker,)
        ).fetchone()
        if not row or row["expires_at"] <= self.clock():
            raise PublishError("upload_job_expired", 410)
        if not self._owned_upload(connection, row) or row["upload_id"] != upload_id:
            raise PublishError("upload_not_found", 404)
        if principal is not None and (
            not principal.owner or principal.id != row["owner_id"]
        ):
            raise PublishError("upload_not_found", 404)
        if row["state"] not in OPEN_JOBS:
            raise PublishError("upload_job_closed", 409)
        if not self._owner_active(connection, row["owner_id"]):
            raise PublishError("authentication_required", 401)
        if not self.creator_active(connection, self._creator(row)):
            raise PublishError("upload_job_creator_revoked", 403)
        return True

    def authorize_upload(self, upload_id, principal):
        self._authenticated(principal)
        self.cleanup()
        with self.repository.connect() as connection:
            return self.guard_commit(connection, upload_id, principal)

    def cancel(self, identifier, principal):
        self._authenticated(principal)
        with self.repository.connect(immediate=True) as connection:
            row = self._synchronize(
                connection, self._job(connection, identifier, principal)
            )
            if row["state"] == "published":
                raise PublishError("publication_already_committed", 409)
            if row["state"] in OPEN_JOBS:
                connection.execute(
                    "UPDATE upload_job SET state='cancelled' WHERE id=?", (identifier,)
                )
            row = connection.execute(
                "SELECT * FROM upload_job WHERE id=?", (identifier,)
            ).fetchone()
            upload = self._owned_upload(connection, row)
        if upload and upload["state"] not in TERMINAL_UPLOADS:
            self.core.abort_upload(upload["id"], "owner:" + row["owner_id"])
        return self._result(row, principal)

    def cleanup(self):
        aborts = []
        with self.repository.connect(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM upload_job ORDER BY created_at LIMIT ?", (MAXIMUM_JOBS,)
            ).fetchall()
            for original in rows:
                row = self._synchronize(connection, original)
                upload = self._owned_upload(connection, row)
                if (
                    row["state"] not in OPEN_JOBS
                    and row["state"] != "published"
                    and upload
                    and upload["state"] not in TERMINAL_UPLOADS
                ):
                    aborts.append((upload["id"], "owner:" + row["owner_id"]))
                if row["expires_at"] <= self.clock() and (
                    not upload or upload["state"] in TERMINAL_UPLOADS
                ):
                    connection.execute(
                        "DELETE FROM upload_job WHERE id=?", (row["id"],)
                    )
        for upload_id, subject in aborts:
            self.core.abort_upload(upload_id, subject)
        if aborts:
            with self.repository.connect(immediate=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM upload_job WHERE expires_at<=?", (self.clock(),)
                ).fetchall()
                for row in rows:
                    upload = self._owned_upload(connection, row)
                    if not upload or upload["state"] in TERMINAL_UPLOADS:
                        connection.execute(
                            "DELETE FROM upload_job WHERE id=?", (row["id"],)
                        )
        return {"aborted": len(aborts)}
