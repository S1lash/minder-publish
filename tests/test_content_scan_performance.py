"""Reading a publication does not walk unrelated stored material."""
from test_http import publish

async def test_content_open_does_not_scan_thousand_unrelated_files(application, client, monkeypatch):
    import minder_publish.server.core as core_module
    material, bearer = await publish(application, client, access='link')
    root = application.state.config.data_dir / 'performance-fixture'
    root.mkdir()
    for n in range(1000):
        (root / f'{n:04}.dat').write_bytes(b'x')
    original = core_module.iter_bytes
    observed = []
    def recording(path):
        for item in original(path):
            observed.append(item)
            yield item
    monkeypatch.setattr(core_module, 'iter_bytes', recording)
    # The full settings/storage report retains its accurate filesystem scan.
    application.state.core.settings()
    assert len(observed) >= 1000
    observed.clear()
    client.cookies.clear()
    opened = await client.get(material['stable_url'])
    assert opened.content == b'<h1>Private result</h1>'
    assert not observed, f'opening scanned {len(observed)} unrelated files'
    for method, headers in [('HEAD', {}), ('GET', {'Range': 'bytes=0-3'}), ('GET', {})]:
        response = await client.request(method, str(opened.url), headers=headers, follow_redirects=False)
        assert response.status_code in (200, 206)
        assert not observed, f'{method} scanned {len(observed)} files'
    capabilities = await client.get('/api/v1/capabilities', headers=bearer)
    assert capabilities.status_code == 200
    assert not observed, 'capabilities scanned storage'

async def test_lease_is_released_if_policy_read_fails(tmp_path):
    import pytest
    from minder_publish.server.content import LeasedFile

    class Core:
        released = []
        def acquire_lease(self, revision_id):
            return 'lease-id'
        def policy_settings(self):
            raise RuntimeError('policy unavailable')
        def release_lease(self, lease):
            self.released.append(lease)

    core = Core()
    response = LeasedFile(core, 'revision-id', tmp_path / 'file')
    with pytest.raises(RuntimeError, match='policy unavailable'):
        await response({'type': 'http', 'method': 'GET', 'headers': []}, None, None)
    assert core.released == ['lease-id']

async def test_resolve_file_queries_only_the_requested_manifest_entry(application, client, monkeypatch):
    from contextlib import contextmanager
    material, _ = await publish(application, client, access='link')
    core = application.state.core
    statements = []
    connect = core.repository.connect

    @contextmanager
    def traced_connect(**kwargs):
        with connect(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    monkeypatch.setattr(core.repository, 'connect', traced_connect)
    path, info = core.resolve_file(material['current_revision'], '')
    assert path.read_bytes() == b'<h1>Private result</h1>'
    assert info['path'] == 'index.html'
    manifest_queries = [sql.lower() for sql in statements if 'from revision_file' in sql.lower()]
    assert len(manifest_queries) == 1
    assert 'and path=' in manifest_queries[0]
