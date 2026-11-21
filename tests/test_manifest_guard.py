"""Approving a snapshot must prevent upload after source changes."""
import io
import zipfile

import httpx
import pytest
from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError
from test_client_api import LIMITS


def fixture_client(requests):
    def transport(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith('/capabilities'):
            return httpx.Response(200, json={'data': {'limits': LIMITS}})
        return httpx.Response(500, json={'error': {'code': 'unexpected_mutation'}})
    return PublishClient('http://localhost:8080', 'synthetic-token', transport=httpx.MockTransport(transport))


@pytest.mark.parametrize('change', ['bytes', 'membership', 'entry'])
@pytest.mark.parametrize('updating', [False, True])
def test_changed_site_snapshot_rejected_before_upload(tmp_path, change, updating):
    site = tmp_path / 'site'; site.mkdir()
    (site / 'index.html').write_text('initial')
    (site / 'other.html').write_text('other')
    requests = []
    with fixture_client(requests) as client:
        baseline = client.publish(site, kind='site', dry_run=True)
        if change == 'bytes': (site / 'index.html').write_text('changed')
        if change == 'membership': (site / 'new.txt').write_text('added')
        entry = 'other.html' if change == 'entry' else 'index.html'
        kwargs = {'publication_id': 'fixture', 'expected_revision': 'old'} if updating else {}
        with pytest.raises(ClientError) as error:
            client.publish(site, kind='site', entry_path=entry, expected_manifest=baseline['manifest_sha256'], **kwargs)
    assert error.value.code == 'manifest_mismatch'
    assert all(method == 'GET' for method, path in requests)


def test_site_zip_metadata_does_not_change_logical_manifest(tmp_path):
    requests = []
    archives = []
    for i, stamp in enumerate(((2020, 1, 1, 0, 0, 0), (2024, 1, 1, 0, 0, 0))):
        path = tmp_path / f'archive{i}.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('index.html', stamp)
            archive.writestr(info, b'<h1>same</h1>')
        archives.append(path)
    with fixture_client(requests) as client:
        first = client.publish(archives[0], kind='site', dry_run=True)
        second = client.publish(archives[1], kind='site', dry_run=True)
    # Same published filename is part of identity; compare same path after replacement.
    archives[0].write_bytes(archives[1].read_bytes())
    with fixture_client(requests) as client:
        third = client.publish(archives[0], kind='site', dry_run=True)
    assert first['manifest_sha256'] == third['manifest_sha256']
