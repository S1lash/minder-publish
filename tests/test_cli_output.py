"""Operation expiry is not material expiry in human-readable output."""
import json
from minder_publish.client.cli import _print_data


def test_committed_upload_status_keeps_state_and_operation_expiry(capsys):
    status = {'id':'upload1', 'state':'committed', 'publication_id':'material1', 'stable_url':'https://publish.test/p/material1', 'expires_at':'2026-12-01T00:00:00Z'}
    _print_data(status, False)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == status
    assert captured.err == ''
    _print_data(status, True)
    assert json.loads(capsys.readouterr().out) == {'data':status}


def test_real_material_keeps_compact_url_and_access(capsys):
    material = {'id':'material1', 'stable_url':'https://publish.test/p/material1', 'access':'link', 'expires_at':'2026-12-01T00:00:00Z'}
    _print_data(material, False)
    captured = capsys.readouterr()
    assert captured.out.strip() == material['stable_url']
    assert 'access=link' in captured.err and 'expires=2026-12-01T00:00:00Z' in captured.err
