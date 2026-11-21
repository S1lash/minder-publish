import io
import zipfile

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.storage import preflight_zip


@pytest.mark.parametrize("zip64", [False, True])
@pytest.mark.parametrize("prefix", [b"", b"application prefix\n" * 10])
def test_preflight_accepts_standard_zip_and_zip64(tmp_path, monkeypatch, zip64, prefix):
    if zip64:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 32)
    archive_path = tmp_path / "site.zip"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("index.html", b"hello")
        archive.writestr("assets/site.css", b"body {}")
        archive.comment = b"Site bundle"
    if zip64:
        assert b"PK\x06\x07" in payload.getvalue()
    archive_path.write_bytes(prefix + payload.getvalue())

    preflight_zip(archive_path, {"site_files": 2}, 1024)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("index.html") == b"hello"
        assert archive.read("assets/site.css") == b"body {}"


@pytest.mark.parametrize("zip64", [False, True])
@pytest.mark.parametrize("site_files,metadata_limit", [(1, 1024), (3, 64)])
def test_preflight_applies_small_budgets_to_standard_archives(
    tmp_path, monkeypatch, zip64, site_files, metadata_limit
):
    if zip64:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 32)
    archive_path = tmp_path / "site.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in ("index.html", "site.css", "site.js"):
            archive.writestr(path, b"plain content")

    with pytest.raises(PublishError) as raised:
        preflight_zip(archive_path, {"site_files": site_files}, metadata_limit)
    assert raised.value.code == "archive_metadata_limit"
