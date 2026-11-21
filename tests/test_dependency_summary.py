"""Static findings and their limits remain separately legible."""
import io
import pytest
from minder_publish.dependencies import scan_dependencies


@pytest.mark.parametrize(('body','expected'), [
    (b'<h1>Simple</h1>', 'No missing local assets'),
    (b'<img src="missing.png">', 'Missing local assets: 1'),
    (b'<img src="https://cdn.example/a.png">', 'External origins: 1'),
    (b'<script>fetch("https://api.example/")</script>', 'No missing local assets'),
])
def test_summary_preserves_qualified_scan_limit(body, expected):
    result = scan_dependencies([{'path':'index.html', 'size_bytes':len(body)}], lambda _: io.BytesIO(body))
    assert result['static_scan_complete'] and not result['complete']
    assert expected in result['summary']
    assert 'runtime behavior are not analyzed' in result['limitations']
    assert 'does not prove' in result['limitations']


def test_truncated_scan_reports_incomplete():
    body = b'x' * 100
    result = scan_dependencies([{'path':'index.html', 'size_bytes':len(body)}], lambda _: io.BytesIO(body), limits={'file_bytes':16})
    assert result['truncated'] and not result['static_scan_complete']
    assert 'incomplete' in result['summary'].lower()
