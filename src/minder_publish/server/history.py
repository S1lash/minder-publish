"""Bounded owner history, immutable restoration and retained-byte policy."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

from minder_publish.errors import PublishError

from .core import dumps, iso, now, parse
from .storage import CHUNK_BYTES, fsync_directory, fsync_tree, normalize_path, safe_id, tree_bytes


def record_revision(core, connection, publication, revision, summary):
    sequence = publication.get("history_sequence", 0) + 1
    publication["history_sequence"] = sequence
    core._save_publication(connection, publication)
    connection.execute(
        "INSERT INTO revision_history VALUES (?,?,?,?,?,?,0)",
        (revision["id"], publication["id"], sequence, revision["created_at"], revision["size_bytes"], summary),
    )
    # Preserve monotonic numbering in publication even when the bounded ledger ages out.
    connection.execute(
        "DELETE FROM revision_history WHERE publication_id=? AND revision_id NOT IN "
        "(SELECT revision_id FROM revision_history WHERE publication_id=? ORDER BY sequence DESC LIMIT ?)",
        (publication["id"], publication["id"], core.config.operations["history_catalog_entries"]),
    )


def apply_retention(core, connection, timestamp):
    defaults = core._settings(connection)["defaults"]
    count_limit = defaults["history_previous_versions"]
    days = defaults["history_retention_days"]
    budget = core.config.storage_budget_bytes * defaults["history_budget_percent"] // 100
    candidates = []
    counts = {}
    rows = connection.execute("SELECT * FROM revision WHERE state='retired' ORDER BY retired_at DESC,id DESC").fetchall()
    for row in rows:
        identifier = row["publication_id"]
        counts[identifier] = counts.get(identifier, 0) + 1
        deadline = parse(row["retired_at"]) + timedelta(days=days)
        retained = count_limit > 0 and days > 0 and counts[identifier] <= count_limit and deadline > timestamp
        # A reduced policy must not revive files already released by an earlier decision.
        if not row["recovery_until"]:
            retained = False
        elif parse(row["recovery_until"]) < deadline:
            deadline = parse(row["recovery_until"])
        if retained and deadline > timestamp:
            candidates.append((row, deadline))
        else:
            connection.execute("UPDATE revision SET recovery_until=NULL WHERE id=?", (row["id"],))
    used = 0
    for row, deadline in candidates:
        size = tree_bytes(core.storage.revision(row["id"]))
        used += size
        connection.execute(
            "UPDATE revision SET recovery_until=? WHERE id=?",
            (iso(deadline) if used <= budget else None, row["id"]),
        )


def list_history(core, publication_id):
    with core.repository.connect() as connection:
        publication = core._publication(connection, publication_id)
        rows = connection.execute(
            "SELECT h.*,r.state,r.recovery_until,r.data,f.data AS entry_data FROM revision_history h "
            "LEFT JOIN revision r ON r.id=h.revision_id "
            "LEFT JOIN revision_file f ON f.revision_id=r.id AND f.path=json_extract(r.data,'$.entry_path') "
            "WHERE h.publication_id=? ORDER BY sequence DESC",
            (publication_id,),
        ).fetchall()
    entries = []
    for row in rows:
        current = row["revision_id"] == publication["current_revision"]
        available = publication["content_state"] == "present" and not publication.get("deletion_state") and (
            row["state"] == "current" or row["state"] == "retired" and row["recovery_until"] and parse(row["recovery_until"]) > now()
        )
        revision = json.loads(row["data"]) if row["data"] else {}
        entry = json.loads(row["entry_data"]) if row["entry_data"] else {}
        entries.append({
            "revision_id": row["revision_id"], "sequence": row["sequence"],
            "created_at": row["created_at"], "size_bytes": row["size_bytes"],
            "change_summary": row["change_summary"], "baseline": bool(row["baseline"]),
            "available": bool(available), "current": current, "retain_until": row["recovery_until"],
            "kind": revision["kind"] if available else None,
            "filename": (revision.get("filename") or PurePosixPath(revision["entry_path"]).name) if available else None,
            "content_type": entry.get("content_type", "application/octet-stream") if available else None,
            "shared": bool(revision.get("shared", False)),
            "version_url": version_url(core.config, publication_id, row["sequence"]),
        })
    return {"items": entries, "total": publication.get("history_sequence", len(entries)), "available": sum(item["available"] for item in entries), "catalog_limit": core.config.operations["history_catalog_entries"]}


def version_url(config, publication_id, sequence):
    return f"{config.base_url}/p/{quote(publication_id, safe='')}~{sequence}"


def version_sequence(core, publication_id, revision_id):
    with core.repository.connect() as connection:
        row = connection.execute(
            "SELECT sequence FROM revision_history WHERE publication_id=? AND revision_id=?",
            (publication_id, revision_id),
        ).fetchone()
    if not row:
        raise PublishError("revision_not_found", 404)
    return row["sequence"]


def pinned_revision(core, publication_id, sequence):
    with core.repository.connect() as connection:
        row = connection.execute(
            "SELECT revision_id FROM revision_history WHERE publication_id=? AND sequence=?",
            (publication_id, sequence),
        ).fetchone()
    if not row:
        raise PublishError("revision_not_found", 404)
    return retained_revision(core, publication_id, row["revision_id"])


def retained_available(publication, revision):
    return bool(
        publication["content_state"] == "present" and not publication.get("deletion_state")
        and revision["publication_id"] == publication["id"]
        and (revision["state"] == "current" or (
            revision["state"] == "retired" and revision["recovery_until"]
            and parse(revision["recovery_until"]) > now()
        ))
    )


def revision_grant_available(publication, revision, payload, *, new):
    """Keep explicit shared/owner history grants separate from ordinary drain."""
    if revision["publication_id"] != publication["id"]:
        return False
    if payload.get("owner_history") is True:
        return (
            payload["subject_kind"] == "owner"
            and payload.get("view_mode", "normal") == "normal"
            and retained_available(publication, revision)
        )
    if payload.get("shared_revision") is True:
        return (
            revision.get("shared") is True
            and revision.get("share_version", 0) == payload.get("revision_share_version")
            and retained_available(publication, revision)
        )
    if new:
        return revision["state"] == "current" and publication["current_revision"] == revision["id"]
    return revision["state"] == "current" or bool(
        revision["state"] == "retired" and revision["drain_until"]
        and parse(revision["drain_until"]) > now()
    )


def set_sharing(core, publication_id, revision_id, enabled, *, authorize):
    with core.repository.connect(immediate=True) as connection:
        authorize(connection)
        publication = core._publication(connection, publication_id)
        row = connection.execute(
            "SELECT r.*,h.sequence FROM revision r JOIN revision_history h ON h.revision_id=r.id "
            "WHERE r.id=? AND r.publication_id=?", (safe_id(revision_id), publication_id),
        ).fetchone()
        if not row:
            raise PublishError("revision_not_found", 404)
        data = json.loads(row["data"])
        revision = dict(data, state=row["state"], recovery_until=row["recovery_until"])
        if not retained_available(publication, revision):
            raise PublishError("revision_unavailable", 410)
        if enabled != data.get("shared", False):
            data["shared"] = enabled
            data["share_version"] = data.get("share_version", 0) + 1
            connection.execute("UPDATE revision SET data=? WHERE id=?", (dumps(data), revision_id))
        return {"revision_id": revision_id, "sequence": row["sequence"], "shared": enabled,
                "version_url": version_url(core.config, publication_id, row["sequence"])}


def retained_revision(core, publication_id, revision_id):
    publication = core.get_publication(publication_id)
    revision = core.get_revision(safe_id(revision_id))
    if revision["publication_id"] != publication_id:
        raise PublishError("revision_not_found", 404)
    if publication["content_state"] != "present" or publication.get("deletion_state"):
        raise PublishError("content_unavailable", 410)
    if revision["state"] != "current" and not (
        revision["state"] == "retired" and revision["recovery_until"] and parse(revision["recovery_until"]) > now()
    ):
        raise PublishError("revision_unavailable", 410)
    return revision


def restore_revision(core, publication_id, revision_id, expected_revision, subject, *, authorize=None):
    """Copy validated retained bytes into the ordinary reserved/CAS commit path.

    The caller authenticates an owner before entry and supplies the normal commit
    authorization callback, which rechecks that authority inside the transaction.
    """
    if authorize is None:
        raise PublishError("owner_authorization_required", 403)
    publication = core.get_publication(publication_id)
    source = retained_revision(core, publication_id, revision_id)
    if publication["current_revision"] != expected_revision:
        raise PublishError("revision_conflict", 409, field="expected_revision")
    lease = core.acquire_lease(revision_id, "export")
    upload_id = None
    try:
        source = retained_revision(core, publication_id, revision_id)
        plan = {"publication_id": publication_id, "expected_revision": expected_revision,
                "kind": source["kind"], "filename": source.get("filename", publication["filename"]),
                "entry_path": source["entry_path"], "size_bytes": source["size_bytes"] if source["kind"] == "file" else 0,
                "expanded_size_bytes": source["size_bytes"], "change_summary": "Restored a saved version",
                "supports_link_names": True}
        if source["kind"] == "file":
            plan["filename"] = source["entry_path"]
        upload_id = core.create_upload(plan, subject)["id"]
        with core.repository.connect(immediate=True) as connection:
            row, data = core._upload(connection, upload_id, subject, ("planned",))
            if connection.execute("SELECT COUNT(*) FROM upload WHERE state IN ('preparing','committing')").fetchone()[0] >= data["limits"]["concurrent_preparations"]:
                raise PublishError("preparations_busy", 429)
            deadline = min(parse(row["hard_deadline"]), now() + timedelta(seconds=data["limits"]["prepare_seconds"]))
            connection.execute("UPDATE upload SET state='preparing',deadline=? WHERE id=?", (iso(deadline), upload_id))
        prepared = core.storage.stage(upload_id) / "prepared"
        content = prepared / "content"
        content.mkdir(parents=True, mode=0o700)
        total = 0
        for info in source["files"]:
            if now() >= deadline:
                raise PublishError("prepare_timeout", 408)
            core.renew_lease(lease)
            path = normalize_path(info["path"], data["limits"])
            original, _ = core.resolve_file(revision_id, path)
            target = content / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            size = 0
            with original.open("rb") as reader, target.open("xb") as output:
                os.chmod(target, 0o600)
                while block := reader.read(CHUNK_BYTES):
                    if now() >= deadline:
                        raise PublishError("prepare_timeout", 408)
                    size += len(block)
                    total += len(block)
                    if size > info["size_bytes"] or total > source["size_bytes"]:
                        raise PublishError("revision_integrity_failed", 409)
                    digest.update(block)
                    output.write(block)
                    core.renew_lease(lease)
                output.flush()
                os.fsync(output.fileno())
            if size != info["size_bytes"] or digest.hexdigest() != info["sha256"]:
                raise PublishError("revision_integrity_failed", 409)
            fsync_directory(target.parent)
        if total != source["size_bytes"]:
            raise PublishError("revision_integrity_failed", 409)
        with (prepared / "manifest.json").open("x") as output:
            output.write(dumps(source["files"]))
            output.flush()
            os.fsync(output.fileno())
        fsync_tree(content)
        fsync_directory(prepared)
        from minder_publish.dependencies import scan_dependencies
        data["dependency_report"] = scan_dependencies(source["files"], lambda name: (content / name).open("rb"), base_origin=core.config.content_origin(data["revision_id"]), entry_path=source["entry_path"], limits={"seconds": min(5, max(0.01, (deadline-now()).total_seconds()))})
        data["prepared_size"] = total
        with core.repository.connect(immediate=True) as connection:
            core._upload(connection, upload_id, subject, ("preparing",))
            connection.execute("UPDATE upload SET state='prepared',data=? WHERE id=?", (dumps(data), upload_id))
        return core.commit_upload(upload_id, subject, authorize=authorize)
    except BaseException:
        if upload_id is not None:
            core._fail_upload(upload_id)
        raise
    finally:
        core.release_lease(lease)
