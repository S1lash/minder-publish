"""History uses immutable files, bounded retention and normal commit guards."""

from datetime import timedelta

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.core import iso, now
from test_core_domain import bundle, core, publish, publish_site


def replace(core, previous, payload=b"next", **options):
    return publish(core, payload, publication_id=previous["id"], expected_revision=previous["current_revision"], **options)


def authorize(connection, upload_id):
    assert connection.execute("SELECT 1 FROM upload WHERE id=?", (upload_id,)).fetchone()


def test_history_numbers_survive_collection_and_catalog_limit(core):
    core.config.operations["history_catalog_entries"] = 3
    value = publish(core, change_summary="First draft")
    for number in range(5):
        value = replace(core, value, str(number).encode(), change_summary=f"Change {number}")
    history = core.history(value["id"])
    assert history["total"] == 6
    assert [item["sequence"] for item in history["items"]] == [6, 5, 4]
    assert history["items"][0]["change_summary"] == "Change 4"
    assert history["available"] == 3


def test_history_restore_new_origin_preserves_current_policy(core):
    first = publish(core, b"original", access="password", password="initial-long-password")
    second = replace(core, first, b"newer")
    core.patch_publication(first["id"], {"summary": "Current summary", "password": "current-long-password"})
    current = core.get_publication(first["id"])
    restored = core.restore_revision(first["id"], first["current_revision"], second["current_revision"], "owner", authorize=authorize)
    assert restored["current_revision"] not in (first["current_revision"], second["current_revision"])
    assert restored["summary"] == "Current summary"
    assert restored["password_hash"] == current["password_hash"]
    assert restored["expires_at"] == current["expires_at"]
    assert core.resolve_file(restored["current_revision"], "")[0].read_bytes() == b"original"
    assert core.history(first["id"])["total"] == 3


def test_restore_site_keeps_manifest_and_nested_paths(core):
    first = publish_site(core, bundle([("index.html", b"old"), ("assets/a.txt", b"asset")]))
    second = publish_site(core, bundle([("index.html", b"new")]), publication_id=first["id"], expected_revision=first["current_revision"])
    restored = core.restore_revision(first["id"], first["current_revision"], second["current_revision"], "owner", authorize=authorize)
    assert core.resolve_file(restored["current_revision"], "assets/a.txt")[0].read_bytes() == b"asset"


def test_history_collection_respects_active_lease_and_keeps_record(core):
    first = publish(core)
    second = replace(core, first)
    lease = core.acquire_lease(first["current_revision"])
    core.update_settings({"defaults": {"history_previous_versions": 0}})
    with core.repository.connect(immediate=True) as conn:
        conn.execute("UPDATE revision SET drain_until=? WHERE id=?", (iso(now()-timedelta(seconds=1)), first["current_revision"]))
    core.sweep()
    assert core.storage.revision(first["current_revision"]).is_dir()
    core.release_lease(lease)
    core.sweep()
    assert not core.storage.revision(first["current_revision"]).exists()
    assert core.history(second["id"])["items"][1]["available"] is False
    with pytest.raises(PublishError) as failed:
        core.history_revision(first["id"], first["current_revision"])
    assert failed.value.status in (404, 410)


def test_restore_conflicts_and_authorization_fail_closed(core):
    first = publish(core)
    second = replace(core, first)
    with pytest.raises(PublishError):
        core.restore_revision(first["id"], first["current_revision"], second["current_revision"], "owner")
    with pytest.raises(PublishError) as failure:
        core.restore_revision(first["id"], first["current_revision"], first["current_revision"], "owner", authorize=authorize)
    assert failure.value.code == "revision_conflict"
    def revoked(connection, upload_id):
        raise PublishError("unauthorized", 401)
    with pytest.raises(PublishError):
        core.restore_revision(first["id"], first["current_revision"], second["current_revision"], "owner", authorize=revoked)
    assert core.get_publication(first["id"])["current_revision"] == second["current_revision"]


def test_history_deleted_publication_cannot_open(core):
    first = publish(core)
    second = replace(core, first)
    core.delete_publication(first["id"])
    with pytest.raises(PublishError):
        core.history_revision(first["id"], first["current_revision"])
    core.sweep()
    with core.repository.connect() as connection:
        assert not connection.execute("SELECT 1 FROM revision_history WHERE publication_id=?", (first["id"],)).fetchone()


def test_history_reports_each_revision_kind_and_mime_across_type_replacements(core):
    first = publish(core, b'<h1>old html</h1>', filename='old.html')
    second = publish(core, b'PK\x03\x04binary', filename='archive.zip', publication_id=first['id'], expected_revision=first['current_revision'])
    third = publish_site(core, bundle([('index.html', b'<h1>site</h1>')]), publication_id=first['id'], expected_revision=second['current_revision'])
    entries = {item['revision_id']: item for item in core.history(first['id'])['items']}
    assert (entries[first['current_revision']]['kind'], entries[first['current_revision']]['filename'], entries[first['current_revision']]['content_type']) == ('file', 'old.html', 'text/html')
    assert (entries[second['current_revision']]['kind'], entries[second['current_revision']]['filename'], entries[second['current_revision']]['content_type']) == ('file', 'archive.zip', 'application/zip')
    assert entries[third['current_revision']]['kind'] == 'site'
