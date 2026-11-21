"""Warm handoffs preserve one-use capability and traffic classification."""
from browser_navigation import opening_target
from minder_publish.server.statistics import Statistics
from test_http import publish


async def start(client, url, headers=None):
    landing = await client.get(url, headers=headers, follow_redirects=False)
    target = opening_target(landing)
    assert target
    return target


async def test_warm_open_consumes_intent_once_and_preserves_bridge(application, client):
    material, _ = await publish(application, client, access='link')
    assert (await client.get(material['stable_url'])).status_code == 200
    config = application.state.config
    target = await start(client, material['stable_url'])
    response = await client.get(target, follow_redirects=False)
    assert response.status_code == 303
    assert '/__mp/authorize' not in response.headers['location']
    assert not any(cookie.startswith(config.bridge_cookie + '=') for cookie in response.headers.get_list('set-cookie'))
    assert (await client.get(response.headers['location'])).content == b'<h1>Private result</h1>'
    assert Statistics(application.state.core).read(material['id'])['total'] == 2
    replay = await client.get(target, follow_redirects=False)
    assert replay.status_code in (403, 404, 410)
    assert Statistics(application.state.core).read(material['id'])['total'] == 2


async def test_prefetch_intent_does_not_resume_when_redirect_drops_prefetch_header(application, client):
    material, _ = await publish(application, client, access='link')
    assert (await client.get(material['stable_url'])).status_code == 200
    # A prefetch can receive a direct redirect instead of the document opening page.
    response = await client.get(material['stable_url'], headers={'Purpose': 'prefetch'}, follow_redirects=False)
    target = opening_target(response) or response.headers.get('location')
    assert target and '/__mp/start' in target
    response = await client.get(target, follow_redirects=False)
    assert response.status_code == 303 and '/__mp/authorize' in response.headers['location']
    assert Statistics(application.state.core).read(material['id'])['total'] == 1

async def test_stale_reader_after_access_change_cannot_resume(application, client):
    material, bearer = await publish(application, client, access='link')
    assert (await client.get(material['stable_url'])).content == b'<h1>Private result</h1>'
    changed = await client.patch('/api/v1/publications/' + material['id'], headers=bearer,
                                 json={'access': 'password', 'password': 'new synthetic password'})
    assert changed.status_code == 200
    gate = await client.get(material['stable_url'], follow_redirects=False)
    assert b'Private result' not in gate.content and 'type="password"' in gate.text
    origin = application.state.config.content_origin(material['current_revision'])
    asset = await client.get(origin + '/index.html', follow_redirects=False)
    assert asset.status_code == 403


async def test_head_intent_cannot_resume_via_get(application, client):
    material, _ = await publish(application, client, access='link')
    assert (await client.get(material['stable_url'])).status_code == 200
    head = await client.head(material['stable_url'], follow_redirects=False)
    target = head.headers.get('location')
    assert target and '/__mp/start' in target
    response = await client.get(target, follow_redirects=False)
    assert response.status_code == 303 and '/__mp/authorize' in response.headers['location']
    assert Statistics(application.state.core).read(material['id'])['total'] == 1

async def test_unshare_and_reshare_revokes_existing_reader_and_pending_intent(application, client):
    from test_http import sign_in
    from test_http_security import upload_site
    old, _ = await upload_site(application, client, entry='index.html', html='old html')
    await upload_site(application, client, entry='index.html', html='new html', publication=old)
    share = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    owner = await sign_in(client)
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    client.cookies.clear()
    version = old['stable_url'] + '~1'
    assert (await client.get(version)).text == 'old html'
    pending = await start(client, version)
    owner = await sign_in(client)
    assert (await client.post(share, json={'enabled': False}, headers=owner)).status_code == 200
    assert (await client.post(share, json={'enabled': True}, headers=owner)).status_code == 200
    response = await client.get(pending, follow_redirects=False)
    assert b'old html' not in response.content
    assert '/__mp/authorize' in response.headers.get('location', '') or response.status_code in (401, 403, 404, 410)
    raw = await client.get(application.state.config.content_origin(old['current_revision']) + '/index.html', follow_redirects=False)
    assert raw.status_code == 403

async def test_expired_reader_falls_back_and_expired_intent_fails(application, client):
    from urllib.parse import parse_qs, urlsplit
    from minder_publish.server.auth import digest
    material, _ = await publish(application, client, access='link')
    await client.get(material['stable_url'])
    target = await start(client, material['stable_url'])
    reader = next(c.value for c in client.cookies.jar if c.name == application.state.config.reader_cookie)
    with application.state.auth.connection(write=True) as connection:
        connection.execute('UPDATE auth_readers SET expires_at=0 WHERE token_hash=?', (digest(reader),))
    fallback = await client.get(target, follow_redirects=False)
    assert fallback.status_code == 303 and '/__mp/authorize' in fallback.headers['location']
    target2 = await start(client, material['stable_url'])
    intent = parse_qs(urlsplit(target2).query)['intent'][0]
    with application.state.auth.connection(write=True) as connection:
        connection.execute('UPDATE auth_intents SET expires_at=0 WHERE token_hash=?', (digest(intent),))
    expired = await client.get(target2, follow_redirects=False)
    assert expired.status_code in (401, 403, 404, 410)


async def test_guest_probe_never_uses_normal_reader_or_counts(application, client):
    material, _ = await publish(application, client, access='link')
    await client.get(material['stable_url'])
    probe = await start(client, material['stable_url'] + '?guest=1')
    response = await client.get(probe, follow_redirects=False)
    assert response.status_code == 303 and '/__mp/authorize' in response.headers['location']
    assert Statistics(application.state.core).read(material['id'])['total'] == 1

async def test_different_reader_subject_does_not_consume_intent(application, client):
    from urllib.parse import parse_qs, urlsplit
    from minder_publish.server.auth import digest
    material, _ = await publish(application, client, access='link')
    await client.get(material['stable_url'])
    target = await start(client, material['stable_url'])
    intent = parse_qs(urlsplit(target).query)['intent'][0]
    reader = next(c.value for c in client.cookies.jar if c.name == application.state.config.reader_cookie)
    auth = application.state.auth
    with auth.connection(write=True) as connection:
        import json
        row = connection.execute('SELECT payload FROM auth_readers WHERE token_hash=?', (digest(reader),)).fetchone()
        payload = json.loads(row['payload'])
        payload['subject_id'] = 'different-subject'
        connection.execute('UPDATE auth_readers SET payload=? WHERE token_hash=?', (json.dumps(payload), digest(reader)))
    fallback = await client.get(target, follow_redirects=False)
    assert fallback.status_code == 303 and '/__mp/authorize' in fallback.headers['location']
    with auth.connection() as connection:
        row = connection.execute('SELECT consumed FROM auth_intents WHERE token_hash=?', (digest(intent),)).fetchone()
        assert row['consumed'] == 0

async def test_concurrent_resume_consumes_same_intent_once(application, client):
    import asyncio
    from urllib.parse import parse_qs, urlsplit
    material, _ = await publish(application, client, access='link')
    await client.get(material['stable_url'])
    target = await start(client, material['stable_url'])
    intent = parse_qs(urlsplit(target).query)['intent'][0]
    reader = next(c.value for c in client.cookies.jar if c.name == application.state.config.reader_cookie)
    auth = application.state.auth
    results = await asyncio.gather(*[
        asyncio.to_thread(auth.resume_reader, intent, reader, material['current_revision'], lambda payload, connection: True)
        for _ in range(2)
    ])
    assert sum(result is not None for result in results) == 1
