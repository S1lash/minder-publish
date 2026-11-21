"""Purge causes reflect recorded transitions, never guesses about legacy data."""
import json
from test_core_domain import core, publish


def test_retention_pending_and_completed_then_extend_clears_reason(core):
    item = publish(core, access='link', purge_mode='immediate')
    lease = core.acquire_lease(item['current_revision'])
    core.expire(item['id']); core.sweep()
    pending = core.get_publication(item['id'])
    assert pending['content_state'] == 'purge_pending' and pending['purge_reason'] == 'retention'
    extended = core.extend(item['id'], 120)
    assert extended['content_state'] == 'present' and extended['purge_reason'] is None
    core.release_lease(lease)
    core.expire(item['id']); core.sweep()
    purged = core.get_publication(item['id'])
    assert purged['content_state'] == 'purged' and purged['purge_reason'] == 'retention'


def test_owner_delete_records_reason_before_lease_releases(core):
    item = publish(core)
    lease = core.acquire_lease(item['current_revision'])
    core.delete_publication(item['id']); core.sweep()
    pending = core.get_publication(item['id'])
    assert pending['content_state'] == 'purge_pending' and pending['purge_reason'] == 'owner_delete'
    core.release_lease(lease)


def test_legacy_purged_without_reason_is_unknown(core):
    item = publish(core, access='link', purge_mode='immediate')
    core.expire(item['id']); core.sweep()
    with core.repository.connect(immediate=True) as conn:
        row = conn.execute('SELECT data FROM publication WHERE id=?', (item['id'],)).fetchone()
        data = json.loads(row['data']); data.pop('purge_reason', None)
        conn.execute('UPDATE publication SET data=? WHERE id=?', (json.dumps(data), item['id']))
    assert core.get_publication(item['id'])['purge_reason'] == 'unknown'


def test_import_missing_records_observation_not_fabricated_purge_time(tmp_path, legacy):
    from minder_publish.server.config import Config
    from minder_publish.server.core import Core
    from minder_publish.server.migration import migrate
    database, blobs, _ = legacy
    settings = Config(tmp_path / 'target', secret='x' * 40, testing=True)
    migrate(settings, database, blobs, apply=True)
    imported = Core(settings).initialize().get_publication('missing')
    assert imported['content_state'] == 'purged'
    assert imported['purge_reason'] == 'unknown'
    assert imported['purged_at'] is None
    assert imported['missing_on_import_at'] is not None


from test_operations import legacy


def test_resume_legacy_purging_does_not_invent_retention_reason(core):
    item = publish(core, access='link', purge_mode='immediate')
    core.expire(item['id']); core.sweep()
    with core.repository.connect(immediate=True) as conn:
        row = conn.execute('SELECT data FROM publication WHERE id=?', (item['id'],)).fetchone()
        data = json.loads(row['data']); data.pop('purge_reason', None); data['content_state'] = 'purging'
        conn.execute('UPDATE publication SET data=? WHERE id=?', (json.dumps(data), item['id']))
    core.sweep()
    assert core.get_publication(item['id'])['purge_reason'] == 'unknown'


def test_paused_expired_material_still_records_retention(core):
    item = publish(core, access='link', purge_mode='immediate')
    core.pause(item['id'], True); core.expire(item['id']); core.sweep()
    result = core.get_publication(item['id'])
    assert result['content_state'] == 'purged' and result['purge_reason'] == 'retention'


def purged_material(core, **options):
    item = publish(core, access='link', purge_mode='immediate', **options)
    core.expire(item['id']); core.sweep()
    return core.get_publication(item['id'])


def test_purged_metadata_policy_changes_recalculate_only_when_requested(core):
    from datetime import timedelta
    from minder_publish.server.core import parse
    item = purged_material(core, metadata_policy='after_purge', metadata_after_purge_seconds=100)
    assert item['metadata_due_at'] is not None
    unchanged = core.patch_publication(item['id'], {'summary':'new summary'})
    assert unchanged['metadata_due_at'] == item['metadata_due_at']
    kept = core.patch_publication(item['id'], {'metadata_policy':'keep'})
    assert kept['metadata_due_at'] is None
    revised = core.patch_publication(item['id'], {'metadata_policy':'after_purge','metadata_after_purge_seconds':200})
    assert parse(revised['metadata_due_at']) == parse(item['purged_at']) + timedelta(seconds=200)
    immediate = core.patch_publication(item['id'], {'metadata_policy':'with_content'})
    assert immediate['metadata_due_at'] is not None
    core.sweep()
    from minder_publish.errors import PublishError
    import pytest
    with pytest.raises(PublishError): core.get_publication(item['id'])


def test_import_missing_after_purge_uses_observation_timestamp(tmp_path, legacy):
    from datetime import timedelta
    from minder_publish.server.config import Config
    from minder_publish.server.core import Core, parse
    from minder_publish.server.migration import migrate
    database, blobs, _ = legacy
    settings = Config(tmp_path / 'target', secret='x' * 40, testing=True)
    migrate(settings, database, blobs, apply=True)
    core = Core(settings).initialize()
    before = core.get_publication('missing')
    updated = core.patch_publication('missing', {'metadata_policy':'after_purge','metadata_after_purge_seconds':300})
    assert updated['purged_at'] is None
    assert parse(updated['metadata_due_at']) == parse(before['missing_on_import_at']) + timedelta(seconds=300)


def test_reupload_clears_old_cause_only_on_success(core):
    import asyncio
    from test_core_domain import chunks
    item = purged_material(core)
    assert item['purge_reason'] == 'retention'
    upload = core.create_upload({'kind':'file','filename':'replacement.txt','size_bytes':3,'publication_id':item['id'],'expected_revision':None, 'expiry':{'mode':'never'}}, 'owner')
    core.abort_upload(upload['id'], 'owner')
    assert core.get_publication(item['id'])['purge_reason'] == 'retention'
    upload = core.create_upload({'kind':'file','filename':'replacement.txt','size_bytes':3,'publication_id':item['id'],'expected_revision':None, 'expiry':{'mode':'never'}}, 'owner')
    asyncio.run(core.receive_upload(upload['id'], chunks(b'new'), 'owner'))
    restored = core.commit_upload(upload['id'], 'owner')
    assert restored['content_state'] == 'present'
    assert restored['purge_reason'] is None and restored['missing_on_import_at'] is None


def test_legacy_unknown_metadata_due_is_anchored_to_selection_not_fabricated_purge(core):
    from datetime import timedelta
    from minder_publish.server.core import now, parse
    item = purged_material(core)
    with core.repository.connect(immediate=True) as conn:
        row = conn.execute('SELECT data FROM publication WHERE id=?', (item['id'],)).fetchone()
        data = json.loads(row['data']); data['purged_at'] = None; data['missing_on_import_at'] = None
        conn.execute('UPDATE publication SET data=? WHERE id=?', (json.dumps(data), item['id']))
    before = now()
    result = core.patch_publication(item['id'], {'metadata_policy':'after_purge', 'metadata_after_purge_seconds':300})
    after = now()
    assert result['purged_at'] is None and result['missing_on_import_at'] is None
    assert before + timedelta(seconds=300) <= parse(result['metadata_due_at']) <= after + timedelta(seconds=300)
    unchanged = core.patch_publication(item['id'], {'metadata_policy':'after_purge', 'metadata_after_purge_seconds':300})
    assert unchanged['metadata_due_at'] == result['metadata_due_at']
