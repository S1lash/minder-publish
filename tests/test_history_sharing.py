"""Version links never silently fall forward or outlive sharing authority."""
from urllib.parse import urlsplit

from test_http import sign_in
from test_http_security import upload_site


async def test_selected_history_is_explicit_and_revocation_fails_closed(application, client):
    old, bearer = await upload_site(application, client, entry='index.html', html='old html')
    current, _ = await upload_site(application, client, entry='index.html', html='new html', publication=old)
    revision = old['current_revision']
    share = f"/api/v1/publications/{old['id']}/revisions/{revision}/sharing"
    version = old['stable_url'] + '~1'
    client.cookies.clear()
    denied = await client.get(version, follow_redirects=False)
    assert denied.status_code in (403, 404, 410) and 'old html' not in denied.text
    raw = await client.get(application.state.config.content_origin(revision), follow_redirects=False)
    assert raw.status_code in (303, 403, 404, 410)
    assert raw.headers.get('location', '') != old['stable_url']
    assert (await client.post(share, json={'enabled': True}, headers=bearer)).status_code in (401, 403)
    owner = await sign_in(client)
    assert (await client.post(share, json={'enabled': True})).status_code == 403
    enabled = await client.post(share, json={'enabled': True}, headers=owner)
    assert enabled.status_code == 200, enabled.text
    data = enabled.json()['data']
    assert data['shared'] is True and data['sequence'] == 1
    assert data['version_url'] == version
    client.cookies.clear()
    opened = await client.get(version)
    assert opened.status_code == 200 and opened.text == 'old html'
    assert 'new html' not in opened.text
    owner = await sign_in(client)
    disabled = await client.post(share, json={'enabled': False}, headers=owner)
    assert disabled.status_code == 200 and disabled.json()['data']['shared'] is False
    client.cookies.clear()
    after = await client.get(version, follow_redirects=False)
    assert after.status_code in (403, 404, 410) and 'old html' not in after.text
    raw = await client.get(application.state.config.content_origin(revision), follow_redirects=False)
    assert raw.headers.get('location', '') != current['stable_url']

async def test_pending_version_intent_cannot_revive_after_unshare_and_reshare(application, client):
    from browser_navigation import opening_target
    old, _ = await upload_site(application, client, entry='index.html', html='old html')
    await upload_site(application, client, entry='index.html', html='new html', publication=old)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    owner = await sign_in(client)
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    landing = await client.get(old['stable_url'] + '~1', follow_redirects=False)
    intent = opening_target(landing)
    assert intent
    owner = await sign_in(client)
    assert (await client.post(share, json={'enabled': False}, headers=owner)).status_code == 200
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    stale = await client.get(intent)
    assert stale.status_code in (401, 403, 404, 410)
    assert b'old html' not in stale.content
    fresh = await client.get(old['stable_url'] + '~1')
    assert fresh.status_code == 200 and fresh.text == 'old html'

async def test_pinned_current_requires_explicit_share_and_obeys_pause(application, client):
    material, bearer = await upload_site(application, client, entry='index.html', html='current html')
    version = material['stable_url'] + '~1'
    client.cookies.clear()
    assert (await client.get(material['stable_url'])).text == 'current html'
    pinned = await client.get(version, follow_redirects=False)
    assert pinned.status_code in (403, 404, 410) and 'current html' not in pinned.text
    owner = await sign_in(client)
    share = f"/api/v1/publications/{material['id']}/revisions/{material['current_revision']}/sharing"
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    assert (await client.get(version)).text == 'current html'
    pause = await client.post(f"/api/v1/publications/{material['id']}/pause", json={'paused': True}, headers=bearer)
    assert pause.status_code == 200
    client.cookies.clear()
    blocked = await client.get(version, follow_redirects=False)
    assert 'current html' not in blocked.text
    assert blocked.status_code in (403, 404, 410)


async def test_shared_version_obeys_private_and_expired_current_policy(application, client):
    old, bearer = await upload_site(application, client, entry='index.html', html='old html')
    await upload_site(application, client, entry='index.html', html='new html', publication=old)
    owner = await sign_in(client)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    assert (await client.patch(f"/api/v1/publications/{old['id']}", json={'access': 'private'}, headers=bearer)).status_code == 200
    client.cookies.clear()
    private = await client.get(old['stable_url'] + '~1', follow_redirects=False)
    assert 'old html' not in private.text and 'new html' not in private.text
    assert 'Owner sign-in' in private.text
    assert (await client.patch(f"/api/v1/publications/{old['id']}", json={'access': 'link', 'expiry': {'mode': 'default'}}, headers=bearer)).status_code == 200
    assert (await client.post(f"/api/v1/publications/{old['id']}/expire", json={}, headers=bearer)).status_code == 200
    expired = await client.get(old['stable_url'] + '~1', follow_redirects=False)
    assert expired.status_code in (403, 404, 410) and 'old html' not in expired.text


async def test_shared_version_password_change_revokes_guest_grant(application, client):
    from test_http import publish
    from test_guest_view import csrf_from
    from browser_navigation import follow_navigation
    old, bearer = await publish(application, client, access='password', password='synthetic original password', body=b'old secret')
    core = application.state.core
    from test_revision_history import replace
    import asyncio
    current = await asyncio.to_thread(replace, core, old, b'new secret')
    owner = await sign_in(client)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    version = old['stable_url'] + '~1'
    gate = await client.get(version)
    assert 'type="password"' in gate.text and 'old secret' not in gate.text
    unlock = await client.post(version, data={'csrf': csrf_from(gate), 'password': 'synthetic original password'}, headers={'Origin': 'https://publish.test'}, follow_redirects=False)
    opened = await follow_navigation(client, unlock)
    assert opened.content == b'old secret'
    assert (await client.patch(f"/api/v1/publications/{old['id']}", json={'password': 'synthetic changed password'}, headers=bearer)).status_code == 200
    old_origin = application.state.config.content_origin(old['current_revision'])
    revoked = await client.get(old_origin + '/index.html', follow_redirects=False)
    assert revoked.status_code == 403 and b'old secret' not in revoked.content
    gate = await client.get(version)
    assert 'type="password"' in gate.text and 'old secret' not in gate.text
    assert core.get_publication(old['id'])['current_revision'] == current['current_revision']


async def test_shared_history_expired_retention_returns_unavailable(application, client):
    from datetime import timedelta
    from minder_publish.server.core import iso, now
    old, _ = await upload_site(application, client, entry='index.html', html='old html')
    await upload_site(application, client, entry='index.html', html='new html', publication=old)
    owner = await sign_in(client)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    core = application.state.core
    with core.repository.connect(immediate=True) as connection:
        connection.execute('UPDATE revision SET recovery_until=? WHERE id=?', (iso(now()-timedelta(seconds=1)), old['current_revision']))
    client.cookies.clear()
    response = await client.get(old['stable_url'] + '~1', follow_redirects=False)
    assert response.status_code == 410 and 'old html' not in response.text


async def test_shared_historical_guest_opening_counts_but_probe_does_not(application, client):
    old, _ = await upload_site(application, client, entry='index.html', html='old html')
    await upload_site(application, client, entry='index.html', html='new html', publication=old)
    owner = await sign_in(client)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    assert (await client.get(old['stable_url'] + '~1?guest=1')).text == 'old html'
    client.cookies.clear()
    assert (await client.get(old['stable_url'] + '~1')).text == 'old html'
    owner = await sign_in(client)
    stats = await client.get(f"/api/v1/publications/{old['id']}/statistics", headers=owner)
    assert stats.status_code == 200
    assert stats.json()['data']['total'] == 1
