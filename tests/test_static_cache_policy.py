"""Only immutable application resources can be privately revalidated."""
from test_http import publish, sign_in

async def test_static_revalidation_does_not_cache_sensitive_responses(application, client):
    asset = await client.get('/static/viewer.js')
    assert asset.status_code == 200
    assert 'no-cache' in asset.headers['cache-control'] and 'private' in asset.headers['cache-control']
    assert asset.headers.get('etag')
    unchanged = await client.get('/static/viewer.js', headers={'If-None-Match': asset.headers['etag']})
    assert unchanged.status_code == 304 and unchanged.content == b''
    assert 'no-cache' in unchanged.headers['cache-control']
    missing = await client.get('/static/no-such-resource.js')
    assert missing.status_code == 404 and 'no-store' in missing.headers['cache-control']
    material, _ = await publish(application, client, access='link')
    owner = await sign_in(client)
    for url in ('/api/v1/session','/api/v1/settings',f"/api/v1/publications/{material['id']}", material['stable_url']):
        response = await client.get(url, headers=owner, follow_redirects=False)
        assert 'no-store' in response.headers['cache-control'], url
    client.cookies.clear()
    content = await client.get(material['stable_url'])
    assert content.content == b'<h1>Private result</h1>'
    assert 'no-store' in content.headers['cache-control']

async def test_packaged_assets_negotiate_compression_head_and_range(application, client):
    identity = await client.get('/static/viewer.js', headers={'Accept-Encoding': 'identity'})
    encoded = await client.get('/static/viewer.js', headers={'Accept-Encoding': 'gzip'})
    assert identity.status_code == encoded.status_code == 200
    assert 'content-encoding' not in identity.headers
    assert encoded.headers['content-encoding'] == 'gzip'
    assert encoded.content == identity.content
    assert encoded.headers['etag'] != identity.headers['etag']
    assert 'accept-encoding' in encoded.headers['vary'].lower()
    refused = await client.get('/static/viewer.js', headers={'Accept-Encoding': 'gzip;q=0, identity;q=1'})
    assert 'content-encoding' not in refused.headers and refused.content == identity.content
    head = await client.head('/static/viewer.js', headers={'Accept-Encoding': 'gzip'})
    assert head.status_code == 200 and head.content == b''
    assert head.headers['etag'] == encoded.headers['etag']
    ranged = await client.get('/static/viewer.js', headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-9'})
    assert ranged.status_code == 206 and ranged.content == identity.content[:10]
    assert 'content-encoding' not in ranged.headers

async def test_asset_etag_tracks_content_even_when_size_and_mtime_match(tmp_path):
    import os
    import httpx
    from starlette.applications import Starlette
    from minder_publish.server.static import PackagedStaticFiles

    asset = tmp_path / 'asset.js'
    asset.write_bytes(b'first version')
    original = asset.stat()

    def application():
        app = Starlette()
        app.mount('/static', PackagedStaticFiles(tmp_path))
        return app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url='http://test') as client:
        first = await client.get('/static/asset.js', headers={'Accept-Encoding': 'identity'})
    asset.write_bytes(b'other version')
    os.utime(asset, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert asset.stat().st_size == original.st_size
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url='http://test') as client:
        second = await client.get('/static/asset.js', headers={'Accept-Encoding': 'identity', 'If-None-Match': first.headers['etag']})
    assert second.status_code == 200 and second.content == b'other version'
    assert second.headers['etag'] != first.headers['etag']

async def test_static_assets_skip_auth_resolution_but_api_and_publications_do_not(application, client, monkeypatch):
    material, token_headers = await publish(application, client, access='link')
    await sign_in(client)
    auth = application.state.auth
    calls = []
    original_session, original_token = auth.session, auth.token

    def session(raw):
        calls.append('session')
        return original_session(raw)

    def token(raw):
        calls.append('token')
        return original_token(raw)

    monkeypatch.setattr(auth, 'session', session)
    monkeypatch.setattr(auth, 'token', token)
    for method in (client.get, client.head):
        assert (await method('/static/viewer.js', headers=token_headers)).status_code == 200
    assert calls == []
    assert (await client.get('/api/v1/session')).status_code == 200
    assert 'session' in calls
    calls.clear()
    assert (await client.get('/api/v1/publications', headers=token_headers)).status_code == 200
    assert 'token' in calls
    calls.clear()
    await client.get(material['stable_url'], follow_redirects=False)
    assert 'session' in calls
