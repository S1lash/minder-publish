import os
import zipfile
from contextlib import contextmanager

import pytest

from minder_publish.client.errors import ClientError
from minder_publish.client import preflight
from minder_publish.client.preflight import prepare


@pytest.fixture
def limits():
    return {
        "file_bytes": 1024,
        "upload_payload_bytes": 4096,
        "site_expanded_bytes": 2048,
        "site_files": 5,
        "path_utf8_bytes": 128,
        "path_depth": 8,
    }


def test_file_zip_remains_original_file(tmp_path, limits):
    source = tmp_path / "data.zip"
    source.write_bytes(b"not-even-a-zip")
    with prepare(source, kind="file", limits=limits) as content:
        assert content.path != source
        assert content.kind == "file"
        assert content.size_bytes == 14
        assert b"".join(content.chunks()) == b"not-even-a-zip"


def test_directory_requires_explicit_site_mode(tmp_path, limits):
    (tmp_path / "index.html").write_text("hello")
    with pytest.raises(ClientError, match="site"):
        with prepare(tmp_path, kind="file", limits=limits):
            pass


def test_site_excludes_sensitive_files_and_builds_safe_manifest(tmp_path, limits):
    (tmp_path / "index.html").write_text("hello")
    (tmp_path / ".env").write_text("PASSWORD=not-for-upload")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private")
    with prepare(tmp_path, kind="site", limits=limits) as content:
        temporary = content.path
        assert content.files == ["index.html"]
        assert content.excluded == [".env", ".git/"]
        with zipfile.ZipFile(content.path) as archive:
            assert archive.namelist() == ["index.html"]
    assert not temporary.exists()


def test_symlink_outside_site_rejected(tmp_path, limits):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("hello")
    secret = tmp_path / "outside.txt"
    secret.write_text("private")
    try:
        (site / "asset.txt").symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ClientError, match="symlink"):
        with prepare(site, kind="site", limits=limits):
            pass


def test_entry_traversal_and_case_collision_rejected(tmp_path, limits):
    (tmp_path / "index.html").write_text("hello")
    with pytest.raises(ClientError, match="entry"):
        with prepare(tmp_path, kind="site", entry_path="../index.html", limits=limits):
            pass
    (tmp_path / "A.txt").write_text("a")
    other = tmp_path / "a.txt"
    other.write_text("b")
    if len(list(tmp_path.glob("*.txt"))) < 2:
        pytest.skip("filesystem is case insensitive")
    with pytest.raises(ClientError, match="collision"):
        with prepare(tmp_path, kind="site", limits=limits):
            pass


def test_preflight_enforces_running_file_and_site_limits(tmp_path, limits):
    (tmp_path / "index.html").write_bytes(b"x" * 1025)
    with pytest.raises(ClientError, match="limit"):
        with prepare(tmp_path, kind="site", limits=limits):
            pass


def test_explicit_zip_site_is_inspected_but_file_zip_is_never_extracted(
    tmp_path, limits
):
    source = tmp_path / "site.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("index.html", "hello")
    with prepare(source, kind="site", limits=limits) as content:
        assert content.path != source and content.files == ["index.html"]
        assert content.expanded_size_bytes == 5
    assert source.exists()


def test_site_zip_sensitive_and_traversal_entries_never_upload(tmp_path, limits):
    source = tmp_path / "unsafe.zip"
    for name in (".env", "../escape.txt"):
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("index.html", "hello")
            archive.writestr(name, "secret")
        with pytest.raises(ClientError):
            with prepare(source, kind="site", limits=limits):
                pass


def test_file_rewrite_with_preserved_stats_cannot_replace_reviewed_bytes(
    tmp_path, limits
):
    source = tmp_path / "data.txt"
    source.write_bytes(b"original")
    original = source.stat()
    with prepare(source, kind="file", limits=limits) as content:
        source.write_bytes(b"changed!")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        changed = source.stat()
        assert (
            original.st_dev,
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
        ) == (changed.st_dev, changed.st_ino, changed.st_size, changed.st_mtime_ns)
        assert b"".join(content.chunks()) == b"original"


@pytest.mark.parametrize("kind", ["file", "site"])
def test_source_rewrite_after_upload_starts_cannot_change_reviewed_snapshot(
    tmp_path, limits, kind
):
    limits = {
        **limits,
        "file_bytes": 300000,
        "upload_payload_bytes": 400000,
        "site_expanded_bytes": 300000,
    }
    source = tmp_path / ("report.bin" if kind == "file" else "site.zip")
    payload = bytes(range(256)) * 900
    if kind == "file":
        source.write_bytes(payload)
    else:
        with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("index.html", payload)
    original_bytes = source.read_bytes()
    original = source.stat()
    with prepare(source, kind=kind, limits=limits) as content:
        stream = content.chunks()
        first = next(stream)
        source.write_bytes(b"!" * len(original_bytes))
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert first + b"".join(stream) == original_bytes


def test_site_zip_cannot_swap_in_unreviewed_sensitive_entries(tmp_path, limits):
    source = tmp_path / "site.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("index.html", "reviewed")
    original_bytes = source.read_bytes()
    original = source.stat()
    with prepare(source, kind="site", limits=limits) as content:
        with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(".env", "not reviewed")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert content.files == ["index.html"]
        assert b"".join(content.chunks()) == original_bytes


def test_directory_sources_cannot_replace_snapshot_after_review(tmp_path, limits):
    site = tmp_path / "site"
    site.mkdir()
    source = site / "index.html"
    source.write_text("reviewed", encoding="utf-8")
    with prepare(site, kind="site", limits=limits) as content:
        original_bytes = content.path.read_bytes()
        source.write_text("changed!", encoding="utf-8")
        (site / ".env").write_text("not reviewed", encoding="utf-8")
        assert content.files == ["index.html"]
        assert b"".join(content.chunks()) == original_bytes
        assert content.dependency_report()["scanned_files"] == 1


def test_prepared_file_snapshot_is_cleaned_after_context_failure(tmp_path, limits):
    source = tmp_path / "data.txt"
    source.write_bytes(b"reviewed")
    with pytest.raises(RuntimeError):
        with prepare(source, kind="file", limits=limits) as content:
            snapshot = content.path
            assert snapshot != source
            raise RuntimeError("cancelled")
    assert not snapshot.exists()
    assert source.read_bytes() == b"reviewed"


@pytest.mark.parametrize("kind", ["file", "site", "directory"])
def test_source_growth_during_snapshot_is_bounded_before_review(
    tmp_path, limits, monkeypatch, kind
):
    source = tmp_path / "index.html"
    source.write_bytes(b"original")
    selected = tmp_path if kind == "directory" else source
    original_open = preflight.open_regular
    monkeypatch.setattr(preflight, "CHUNK_BYTES", 4)

    @contextmanager
    def growing_source(path):
        with original_open(path) as handle:

            class GrowingReader:
                changed = False

                def __getattr__(self, name):
                    return getattr(handle, name)

                def read(self, size):
                    chunk = handle.read(size)
                    if path == source and not self.changed:
                        self.changed = True
                        with source.open("ab") as writer:
                            writer.write(b"growth")
                    return chunk

            yield GrowingReader()

    monkeypatch.setattr(preflight, "open_regular", growing_source)
    with pytest.raises(ClientError) as failure:
        with prepare(
            selected, kind="site" if kind != "file" else "file", limits=limits
        ):
            pytest.fail("a growing source must not reach review")
    assert failure.value.code == "limit_exceeded"
    assert failure.value.field in ("source_size", "file_bytes")


@pytest.mark.parametrize("names", [("A.txt", "a.txt"), ("café.txt", "cafe\u0301.txt")])
def test_archive_collision_checks_do_not_depend_on_host_filesystem(
    tmp_path, limits, names
):
    source = tmp_path / "colliding.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("index.html", "hello")
        for name in names:
            archive.writestr(name, "x")
    with pytest.raises(ClientError) as raised:
        with prepare(source, kind="site", limits=limits):
            pass
    assert raised.value.code == "path_collision"


def test_file_preflight_reports_external_origins_missing_assets_and_honest_scope(
    tmp_path, limits
):
    source = tmp_path / "report.html"
    source.write_text(
        '<script src="https://cdn.example.test/app.js?secret=do-not-return"></script><img src="missing.png"><script>fetch(dynamicUrl)</script>'
    )
    with prepare(source, kind="file", limits=limits) as content:
        report = content.dependency_report()
    assert report["external_origins"] == ["https://cdn.example.test"]
    assert report["missing_assets"] == ["missing.png"]
    assert report["complete"] is False and report["dynamic_code_analyzed"] is False
    assert "secret" not in str(report)


def test_site_preflight_scans_the_actual_validated_zip_manifest(tmp_path, limits):
    archive = tmp_path / "site.zip"
    with zipfile.ZipFile(archive, "w") as content:
        content.writestr(
            "index.html",
            '<link rel="stylesheet" href="assets/app.css"><img src="missing.png">',
        )
        content.writestr(
            "assets/app.css",
            '@import "https://fonts.example.test/style.css?token=hidden";',
        )
    with prepare(archive, kind="site", limits=limits) as content:
        report = content.dependency_report()
    assert report["external_origins"] == ["https://fonts.example.test"]
    assert report["missing_assets"] == ["missing.png"]
    assert report["scanned_files"] == 2 and report["static_scan_complete"] is True
