"""SQL-only reset hook, including installations without the optional MCP SDK."""

import json
import time

from minder_publish.errors import PublishError


def active_grant_family(
    connection, family_id, *, scope="materials:manage", resource=None
):
    row = connection.execute(
        "SELECT scopes,resource,expires_at,revoked_at FROM oauth_family WHERE id=?",
        (family_id,),
    ).fetchone()
    if (
        not row
        or row["revoked_at"] is not None
        or row["expires_at"] <= int(time.time())
    ):
        return False
    scopes = json.loads(row["scopes"])
    return (resource is None or row["resource"] == resource) and (
        scope in scopes or scope == "materials:read" and "materials:manage" in scopes
    )


def invalidate(connection, *, devices=True):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oauth_schema_version'"
    ).fetchone():
        return
    versions = connection.execute(
        "SELECT id,version FROM oauth_schema_version"
    ).fetchall()
    if len(versions) != 1 or tuple(versions[0]) != (1, 1):
        raise PublishError("unknown_oauth_schema", 409)
    connection.execute(
        "UPDATE oauth_family SET revoked_at=COALESCE(revoked_at,?)", (int(time.time()),)
    )
    connection.execute("DELETE FROM oauth_code")
    connection.execute("DELETE FROM oauth_pending")
