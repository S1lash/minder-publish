"""Idempotent retention, revision collection and bounded-phase crash recovery."""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import timedelta

from minder_publish.errors import PublishError

from . import publication_links
from .core import iso, metadata_deadline, now, parse


def sweep(core):
    timestamp = now()
    counts = {
        "uploads_reclaimed": 0,
        "publications_purged": 0,
        "publications_deleted": 0,
        "revisions_deleted": 0,
        "orphans_deleted": 0,
        "aborted": False,
    }
    expired_uploads = []
    with core.repository.connect(immediate=True) as conn:
        conn.execute(
            "DELETE FROM external_reservation WHERE deadline<=?", (iso(timestamp),)
        )
        conn.execute(
            "DELETE FROM read_lease WHERE deadline<=? OR hard_deadline<=?",
            (iso(timestamp), iso(timestamp)),
        )
        for row in conn.execute(
            "SELECT * FROM upload WHERE state NOT IN ('committed','aborted','failed')"
        ):
            if (
                parse(row["deadline"]) <= timestamp
                or parse(row["hard_deadline"]) <= timestamp
            ):
                expired_uploads.append(
                    (row["id"], json.loads(row["data"])["revision_id"])
                )
                conn.execute(
                    "UPDATE upload SET state='failed',reservation_bytes=0 WHERE id=?",
                    (row["id"],),
                )
    for upload_id, revision_id in expired_uploads:
        core.storage.remove_stage(upload_id)
        with core.repository.connect() as conn:
            referenced = conn.execute(
                "SELECT 1 FROM revision WHERE id=?", (revision_id,)
            ).fetchone()
        if not referenced:
            shutil.rmtree(core.storage.revision(revision_id), ignore_errors=True)
        counts["uploads_reclaimed"] += 1

    with core.repository.connect() as conn:
        rows = conn.execute("SELECT data FROM publication").fetchall()
        revision_count = conn.execute("SELECT COUNT(*) FROM revision").fetchone()[0]
        upload_count = conn.execute("SELECT COUNT(*) FROM upload").fetchone()[0]
        tombstones = conn.execute("SELECT COUNT(*) FROM deletion_tombstone").fetchone()[
            0
        ]
    # Empty reconstructed metadata must not erase an unknown store.
    if (
        not rows
        and not revision_count
        and not tombstones
        and not upload_count
        and (
            any(core.storage.revisions.iterdir()) or any(core.storage.staging.iterdir())
        )
    ):
        counts["aborted"] = True
        return counts

    for row in rows:
        snapshot = json.loads(row["data"])
        publication_id = snapshot["id"]
        purge = False
        with core.repository.connect(immediate=True) as conn:
            try:
                publication = core._publication(conn, publication_id)
            except PublishError as error:
                if error.status == 404:
                    continue
                raise
            due = publication.get("purge_due_at")
            if publication["content_state"] == "purged":
                metadata_due = publication.get("metadata_due_at")
                if metadata_due and parse(metadata_due) <= timestamp:
                    publication_links.record_deletion(conn, publication_id, iso(timestamp))
                    conn.execute(
                        "DELETE FROM publication WHERE id=?", (publication_id,)
                    )
                    counts["publications_deleted"] += 1
                continue
            if (
                publication.get("deletion_state")
                or publication["content_state"] == "purging"
                or (due and parse(due) <= timestamp)
            ):
                if (
                    publication["content_state"] == "purging"
                    and publication.get("purge_lease_until")
                    and parse(publication["purge_lease_until"]) > timestamp
                ):
                    continue
                if not publication.get("purge_reason"):
                    # Only this fresh scheduled claim establishes a known cause.
                    # Existing pending/purging records may predate reason tracking.
                    publication["purge_reason"] = (
                        "retention"
                        if publication["content_state"] == "present"
                        and not publication.get("deletion_state")
                        and due
                        else "unknown"
                    )
                busy = conn.execute(
                    "SELECT 1 FROM read_lease l JOIN revision r ON r.id=l.revision_id WHERE r.publication_id=? AND l.deadline>? LIMIT 1",
                    (publication_id, iso(timestamp)),
                ).fetchone()
                uploads = conn.execute(
                    "SELECT data FROM upload WHERE state NOT IN ('committed','aborted','failed')"
                ).fetchall()
                upload_busy = any(
                    json.loads(u["data"])["publication"]["id"] == publication_id
                    for u in uploads
                )
                if busy or upload_busy:
                    if publication["content_state"] != "purging":
                        publication["content_state"] = "purge_pending"
                        core._save_publication(conn, publication)
                    continue
                publication["content_state"] = "purging"
                purge_token = secrets.token_hex(16)
                publication["purge_token"] = purge_token
                publication["purge_lease_until"] = iso(
                    timestamp + timedelta(seconds=core.config.limits["prepare_seconds"])
                )
                core._save_publication(conn, publication)
                revisions = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM revision WHERE publication_id=?",
                        (publication_id,),
                    )
                ]
                historical = [
                    (u["id"], json.loads(u["data"]))
                    for u in conn.execute("SELECT id,data FROM upload")
                    if json.loads(u["data"])["publication"]["id"] == publication_id
                ]
                revisions = list(
                    set(revisions) | {u["revision_id"] for _, u in historical}
                )
                purge = True
        if purge:
            # The durable purging phase blocks new leases/updates until cleanup completes.
            for upload_id, _ in historical:
                core.storage.remove_stage(upload_id)
            for revision_id in revisions:
                path = core.storage.revision(revision_id)
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    pass
            with core.repository.connect(immediate=True) as conn:
                try:
                    publication = core._publication(conn, publication_id)
                except PublishError as error:
                    if error.status == 404:
                        continue
                    raise
                if publication.get("purge_token") != purge_token:
                    continue
                publication.pop("purge_token", None)
                publication.pop("purge_lease_until", None)
                for revision_id in revisions:
                    conn.execute(
                        "DELETE FROM revision_file WHERE revision_id=?", (revision_id,)
                    )
                conn.execute(
                    "DELETE FROM revision WHERE publication_id=?", (publication_id,)
                )
                completed_at = now()
                publication.update(
                    content_state="purged",
                    purged_at=iso(completed_at),
                    size_bytes=0,
                    current_revision=None,
                    purge_due_at=None,
                )
                publication["metadata_due_at"] = metadata_deadline(publication, completed_at)
                if (
                    publication.get("deletion_state")
                    or publication["metadata_policy"] == "with_content"
                ):
                    publication_links.record_deletion(conn, publication_id, iso(timestamp))
                    conn.execute(
                        "DELETE FROM publication WHERE id=?", (publication_id,)
                    )
                    counts["publications_deleted"] += 1
                else:
                    core._save_publication(conn, publication)
                counts["publications_purged"] += 1

    from .history import apply_retention
    from .statistics import Statistics

    with core.repository.connect(immediate=True) as conn:
        apply_retention(core, conn, timestamp)
        Statistics(core).cleanup(conn, timestamp)

    # Claim each retired revision while holding the same transaction that creates leases.
    with core.repository.connect(immediate=True) as conn:
        rows = conn.execute(
            "SELECT * FROM revision WHERE state IN ('retired','collecting')"
        ).fetchall()
        for row in rows:
            if row["state"] != "collecting" and any(
                row[k] and parse(row[k]) > timestamp
                for k in ("drain_until", "recovery_until")
            ):
                continue
            if conn.execute(
                "SELECT 1 FROM read_lease WHERE revision_id=? AND deadline>?",
                (row["id"], iso(timestamp)),
            ).fetchone():
                continue
            conn.execute(
                "UPDATE revision SET state='collecting' WHERE id=?", (row["id"],)
            )
    with core.repository.connect() as conn:
        collecting = [
            r["id"]
            for r in conn.execute("SELECT id FROM revision WHERE state='collecting'")
        ]
    for revision_id in collecting:
        path = core.storage.revision(revision_id)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        with core.repository.connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM revision_file WHERE revision_id=?", (revision_id,)
            )
            conn.execute(
                "DELETE FROM revision WHERE id=? AND state='collecting'", (revision_id,)
            )
        counts["revisions_deleted"] += 1

    with core.repository.connect() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM revision")}
        uploads = {
            r["id"]: json.loads(r["data"])
            for r in conn.execute(
                "SELECT id,data FROM upload WHERE state NOT IN ('committed','aborted','failed')"
            )
        }
    grace = core.config.operations["orphan_grace_seconds"]
    staged_revisions = {value["revision_id"] for value in uploads.values()}
    for root, protected in (
        (core.storage.revisions, known | staged_revisions),
        (core.storage.staging, set(uploads)),
    ):
        for path in root.iterdir():
            if (
                path.is_dir()
                and not path.is_symlink()
                and path.name not in protected
                and timestamp.timestamp() - path.stat().st_mtime > grace
            ):
                shutil.rmtree(path)
                counts["orphans_deleted"] += 1
    parser_root = core.config.data_dir / "parser-temp"
    if parser_root.is_dir():
        for path in parser_root.iterdir():
            if timestamp.timestamp() - path.lstat().st_mtime > grace:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                counts["orphans_deleted"] += 1
    with core.repository.connect(immediate=True) as conn:
        threshold = iso(timestamp - timedelta(seconds=grace))
        conn.execute(
            "DELETE FROM upload WHERE state IN ('committed','aborted','failed') AND deadline<?",
            (threshold,),
        )
    return counts
