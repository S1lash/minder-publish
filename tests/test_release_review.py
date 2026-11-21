"""Regression cases found during the independent release-boundary review."""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import replace

import httpx
import pytest

from minder_publish.server.app import create_app
from minder_publish.server.auth import AuthStore
from minder_publish.server.backup import backup, restore
from minder_publish.server.config import Config
from minder_publish.server.core import Core


def review_config(path):
    config = Config(
        path,
        base_url="https://publish.test",
        secret="review-test-secret-" * 4,
        testing=True,
    )
    config.storage_budget_bytes = 256 * 1024**2
    config.limits.update(
        file_bytes=8 * 1024**2,
        upload_payload_bytes=8 * 1024**2,
        site_expanded_bytes=16 * 1024**2,
        content_quota_bytes=128 * 1024**2,
        disk_reserve_bytes=1024**2,
        disk_reserve_percent=1,
    )
    config.operations.update(export_metadata_bytes=1024, export_buffer_bytes=1024)
    return config


@pytest.fixture
async def review_client(tmp_path):
    app = create_app(review_config(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        app.state.auth.bootstrap("owner", "review-only-owner-password")
        token = app.state.auth.create_token("review", "materials:manage")["token"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://publish.test"
        ) as client:
            yield app, client, {"Authorization": "Bearer " + token}


async def test_legacy_clear_password_preserves_bytes_and_expiry(review_client):
    app, client, headers = review_client
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"password": "publication-password", "ttl": "7d", "title": "Preserve me"},
        files={"file": ("report.txt", b"original bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    previous = response.json()
    response = await client.patch(
        f"/api/artifact/{previous['id']}",
        headers=headers,
        data={"clear_password": "true"},
    )
    assert response.status_code == 200, response.text
    publication = app.state.core.get_publication(previous["id"])
    assert publication["access"] == "link" and not publication["has_password"]
    assert publication["expires_at"] == previous["expires_at"]
    assert publication["title"] == "Preserve me"
    assert (
        app.state.core.resolve_file(
            publication["current_revision"], publication["entry_path"]
        )[0].read_bytes()
        == b"original bytes"
    )


def site_zip(body):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("app.html", body)
    return output.getvalue()


async def test_legacy_site_replace_preserves_omitted_entry_path(review_client):
    app, client, headers = review_client
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"kind": "site_dir", "entry": "app.html", "ttl": "7d"},
        files={"file": ("site.zip", site_zip("first revision"), "application/zip")},
    )
    assert response.status_code == 201, response.text
    previous = response.json()
    response = await client.put(
        f"/api/artifact/{previous['id']}/content",
        headers=headers,
        data={"kind": "site_dir"},
        files={"file": ("site.zip", site_zip("second revision"), "application/zip")},
    )
    assert response.status_code == 200, response.text
    publication = app.state.core.get_publication(previous["id"])
    assert publication["entry_path"] == "app.html"
    assert publication["expires_at"] == previous["expires_at"]
    assert (
        app.state.core.resolve_file(publication["current_revision"], "app.html")[
            0
        ].read_text()
        == "second revision"
    )


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Permission-boundary test requires the non-root server identity",
)
def test_restore_uses_writable_data_volume_when_parent_is_read_only(tmp_path):
    config = review_config(tmp_path / "source")
    Core(config).initialize()
    auth = AuthStore(config)
    auth.initialize()
    auth.bootstrap("owner", "review-only-owner-password")
    archive = tmp_path / "snapshot.tar"
    backup(config, archive)
    parent = tmp_path / "read-only-root"
    target = parent / "data"
    target.mkdir(parents=True, mode=0o700)
    parent.chmod(0o555)
    try:
        result = restore(replace(config, data_dir=target), archive)
        assert result["restored"]
        assert (target / "publish.sqlite3").is_file()
    finally:
        parent.chmod(0o755)


def test_restore_rejects_recursive_extension_header_chain_without_stack_exhaustion(
    tmp_path,
):
    import tarfile

    from minder_publish.errors import PublishError

    archive_path = tmp_path / "nested-headers.tar"
    with tarfile.open(archive_path, "w", format=tarfile.GNU_FORMAT) as archive:
        for _ in range(1200):
            header = tarfile.TarInfo("extended-name")
            header.type = tarfile.GNUTYPE_LONGNAME
            header.size = 2
            archive.addfile(header, io.BytesIO(b"x\0"))
        member = tarfile.TarInfo("backup.json")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"{}"))
    target = review_config(tmp_path / "target")
    with pytest.raises(PublishError):
        restore(target, archive_path)
    assert not (target.data_dir / "publish.sqlite3").exists()


def test_legacy_inspection_stops_discovery_at_the_file_limit(tmp_path, monkeypatch):
    from pathlib import Path

    from minder_publish.errors import PublishError
    from minder_publish.server.migration import _inspect

    config = review_config(tmp_path / "target")
    config.ceilings["site_files"] = 2
    blobs = tmp_path / "legacy-blobs"
    root = blobs / "site" / "content"
    root.mkdir(parents=True)
    for name in ("app.html", "a.txt", "b.txt", "c.txt", "d.txt"):
        (root / name).write_text("x")
    discovered = []
    original = Path.rglob

    def tracked(path, pattern, *args, **kwargs):
        for entry in original(path, pattern, *args, **kwargs):
            if path == root:
                discovered.append(entry)
            yield entry

    monkeypatch.setattr(Path, "rglob", tracked)
    with pytest.raises(PublishError) as error:
        _inspect(
            {
                "id": "site",
                "kind": "site_dir",
                "entry_path": "app.html",
                "created_at": "2026-11-18T00:00:00Z",
                "expires_at": None,
            },
            blobs,
            config,
        )
    assert error.value.code == "legacy_content_limit"
    assert len(discovered) <= 3, (
        "Do not materialize an arbitrarily large source tree before enforcing its file limit."
    )


async def test_v1_site_replace_preserves_omitted_entry_path(review_client):
    app, client, headers = review_client
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"kind": "site_dir", "entry": "app.html", "ttl": "7d"},
        files={"file": ("site.zip", site_zip("first revision"), "application/zip")},
    )
    assert response.status_code == 201, response.text
    previous = app.state.core.get_publication(response.json()["id"])
    payload = site_zip("second revision")
    response = await client.post(
        "/api/v1/uploads",
        headers=headers,
        json={
            "kind": "site",
            "filename": "replacement.zip",
            "size_bytes": len(payload),
            "publication_id": previous["id"],
            "expected_revision": previous["current_revision"],
        },
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["data"]["id"]
    response = await client.put(
        f"/api/v1/uploads/{upload_id}/content", headers=headers, content=payload
    )
    assert response.status_code == 200, response.text
    response = await client.post(f"/api/v1/uploads/{upload_id}/commit", headers=headers)
    assert response.status_code == 200, response.text
    current = response.json()["data"]
    assert current["entry_path"] == "app.html"
    assert current["expires_at"] == previous["expires_at"]
    assert (
        app.state.core.resolve_file(current["current_revision"], "app.html")[
            0
        ].read_text()
        == "second revision"
    )


async def test_purged_record_accepts_explicit_null_revision_cas_and_expiry(
    review_client,
):
    app, client, headers = review_client
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"ttl": "2001-01-01T00:00:00+00:00", "title": "Kept history"},
        files={"file": ("report.txt", b"old bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    app.state.core.sweep()
    purged = app.state.core.get_publication(identifier)
    assert purged["content_state"] == "purged" and purged["current_revision"] is None
    response = await client.post(
        "/api/v1/uploads",
        headers=headers,
        json={
            "kind": "file",
            "filename": "report-v2.txt",
            "size_bytes": 9,
            "publication_id": identifier,
            "expected_revision": None,
            "expiry": {"mode": "never"},
        },
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["data"]["id"]
    assert (
        await client.put(
            f"/api/v1/uploads/{upload_id}/content",
            headers=headers,
            content=b"new bytes",
        )
    ).status_code == 200
    response = await client.post(f"/api/v1/uploads/{upload_id}/commit", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["title"] == "Kept history"
    assert response.json()["data"]["content_state"] == "present"


async def test_head_stable_link_follows_the_same_authorized_revision_opening(
    review_client,
):
    app, client, headers = review_client
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"ttl": "7d"},
        files={"file": ("report.txt", b"original bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    response = await client.head(f"/p/{identifier}", follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("https://r-"), response.headers[
        "location"
    ]
    response = await client.head(f"/p/{identifier}", follow_redirects=True)
    assert response.status_code == 200 and response.content == b""
    assert response.url.host.startswith("r-")
