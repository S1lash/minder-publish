"""Real SQLite/disk tests for publication invariants, not mocked HTTP wiring."""

import asyncio
import io
import sqlite3
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.config import Config
from minder_publish.server.core import Core, iso, now


@pytest.fixture
def core(tmp_path):
    config = Config(tmp_path / "data", secret="x" * 40, testing=True)
    config.limits.update(
        file_bytes=1 << 20,
        upload_payload_bytes=1 << 20,
        site_expanded_bytes=2 << 20,
        content_quota_bytes=16 << 20,
        disk_reserve_bytes=1 << 20,
        disk_reserve_percent=1,
    )
    config.operations.update(export_metadata_bytes=1024, export_buffer_bytes=1024)
    config.storage_budget_bytes = 32 << 20
    return Core(config).initialize()


async def chunks(payload):
    for start in range(0, len(payload), 65536):
        yield payload[start : start + 65536]


def publish(core, payload=b"hello", filename="note.txt", **plan):
    upload = core.create_upload(
        dict(kind="file", filename=filename, size_bytes=len(payload), **plan), "owner"
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(payload), "owner"))
    return core.commit_upload(upload["id"], "owner")


def bundle(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files:
            archive.writestr(name, content)
    return output.getvalue()


def publish_site(core, payload, **plan):
    upload = core.create_upload(
        dict(kind="site", filename="site.zip", size_bytes=len(payload), **plan), "owner"
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(payload), "owner"))
    return core.commit_upload(upload["id"], "owner")


def test_publish_restart_hash_manifest_and_source(core):
    publication = publish(
        core, b"\x00\xfforiginal", "unknown.bin", summary="Сводка Café"
    )
    restarted = Core(core.config).initialize()
    result = restarted.get_publication(publication["id"], manifest=True)
    assert result["access"] == "private" and result["expires_at"] is None
    assert result["summary"] == "Сводка Café"
    assert result["files"][0]["size_bytes"] == 10
    path, info = restarted.resolve_file(result["current_revision"], "unknown.bin")
    assert path.read_bytes() == b"\x00\xfforiginal"
    assert len(info["sha256"]) == 64
    assert (
        result["files"] == restarted.get_revision(result["current_revision"])["files"]
    )


def test_sparse_summary_and_unicode_search(core):
    result = publish(core, title="ДоКуМеНт", summary="Cafe\u0301 — Résumé")
    assert core.list_publications("CAFÉ")["total"] == 1
    assert core.list_publications("документ")["total"] == 1
    core.patch_publication(result["id"], {"title": "New"})
    assert core.get_publication(result["id"])["summary"] == "Cafe\u0301 — Résumé"
    core.patch_publication(result["id"], {"summary": ""})
    assert core.get_publication(result["id"])["summary"] == ""
    for value in (None, 42, "x" * 2001):
        with pytest.raises(PublishError):
            core.patch_publication(result["id"], {"summary": value})


def test_policy_snapshot_expiry_transition_and_pause(core):
    first = publish(core)
    core.update_settings(
        {"defaults": {"external_ttl_seconds": 3600, "purge_after_seconds": 120}}
    )
    assert core.get_publication(first["id"])["purge_after_seconds"] == 2592000
    with pytest.raises(PublishError, match="external expiry required"):
        core.patch_publication(first["id"], {"access": "link"})
    result = core.patch_publication(
        first["id"], {"access": "link", "expiry": {"mode": "default"}}
    )
    assert result["expires_at"] is not None
    result = core.pause(first["id"], True)
    result = core.extend(first["id"], 600)
    assert result["paused"] is True
    assert result["purge_after_seconds"] == 2592000


def test_password_content_update_preserves_metadata(core):
    first = publish(
        core,
        access="password",
        password="correct",
        expiry={"mode": "never"},
        summary="Keep",
    )
    second = publish(
        core,
        b"new",
        publication_id=first["id"],
        expected_revision=first["current_revision"],
    )
    assert second["password_hash"] == first["password_hash"]
    assert second["expires_at"] is None and second["summary"] == "Keep"
    assert second["access_version"] == first["access_version"]
    assert core.get_revision(first["current_revision"])["state"] == "retired"
    assert (
        core.resolve_file(first["current_revision"], "note.txt")[0].read_bytes()
        == b"hello"
    )


def test_invalid_site_update_preserves_current_bytes_and_policy(core):
    first = publish(core, access="password", password="correct")
    with pytest.raises(PublishError):
        publish_site(
            core,
            b"invalid",
            publication_id=first["id"],
            expected_revision=first["current_revision"],
        )
    result = core.get_publication(first["id"])
    assert result["current_revision"] == first["current_revision"]
    assert result["password_hash"] == first["password_hash"]
    assert (
        core.resolve_file(first["current_revision"], "note.txt")[0].read_bytes()
        == b"hello"
    )
    assert core.storage_snapshot()["reserved_bytes"] == 0


def test_stale_concurrent_update_cannot_replace_current(core):
    first = publish(core)
    uploads = [
        core.create_upload(
            {
                "kind": "file",
                "filename": "next.txt",
                "size_bytes": 1,
                "publication_id": first["id"],
                "expected_revision": first["current_revision"],
            },
            "owner",
        )
        for _ in range(2)
    ]
    for item in uploads:
        asyncio.run(core.receive_upload(item["id"], chunks(b"x"), "owner"))
    second = core.commit_upload(uploads[0]["id"], "owner")
    with pytest.raises(PublishError) as error:
        core.commit_upload(uploads[1]["id"], "owner")
    assert error.value.code == "revision_conflict"
    assert (
        core.get_publication(first["id"])["current_revision"]
        == second["current_revision"]
    )


def test_concurrent_quota_admission_is_serialized(core):
    core.config.limits["content_quota_bytes"] = 5 << 20
    core.update_settings({"limits": {"content_quota_bytes": 5 << 20}})

    def create(_):
        try:
            return core.create_upload(
                {"kind": "site", "filename": "site.zip", "size_bytes": 1 << 20}, "owner"
            )
        except PublishError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert "quota_exceeded" in results


def test_subject_size_disconnect_and_abort_release_reservation(core):
    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 2}, "owner"
    )
    with pytest.raises(PublishError) as error:
        asyncio.run(core.receive_upload(upload["id"], chunks(b"xx"), "stranger"))
    assert error.value.status == 404
    with pytest.raises(PublishError):
        asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    assert core.storage_snapshot()["reserved_bytes"] == 0
    assert not core.storage.stage(upload["id"]).exists()
    another = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 1}, "owner"
    )

    async def disconnected():
        yield b"x"
        raise ConnectionError("disconnected")

    with pytest.raises(ConnectionError):
        asyncio.run(core.receive_upload(another["id"], disconnected(), "owner"))
    assert core.storage_snapshot()["reserved_bytes"] == 0
    assert not core.storage.stage(another["id"]).exists()


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "C:/drive", "a\\b", "__mp/code", "a/../b", "a//b"],
)
def test_archive_paths_rejected(core, name):
    with pytest.raises(PublishError):
        publish_site(core, bundle([("index.html", b"index"), (name, b"bad")]))
    assert core.list_publications()["total"] == 0


def test_site_manifest_nested_entry_and_plain_zip(core):
    payload = bundle(
        [
            ("index.html", b"home"),
            ("guide/index.html", b"guide"),
            ("assets/app.js", b"code"),
        ]
    )
    site = publish_site(core, payload, entry_path="guide/index.html")
    assert site["entry_path"] == "guide/index.html" and len(site["files"]) == 3
    assert (
        core.resolve_file(site["current_revision"], "assets/app.js")[0].read_bytes()
        == b"code"
    )
    ordinary = publish(core, payload, "archive.zip")
    assert ordinary["kind"] == "file" and len(ordinary["files"]) == 1
    assert (
        core.resolve_file(ordinary["current_revision"], "archive.zip")[0].read_bytes()
        == payload
    )


def test_zip_collision_link_and_expansion_limits(core):
    for files in [
        [("index.html", b"x"), ("INDEX.HTML", b"y")],
        [("index.html", b"x"), ("a", b"f"), ("a/b", b"g")],
    ]:
        with pytest.raises(PublishError):
            publish_site(core, bundle(files))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("index.html", b"x")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(PublishError):
        publish_site(core, output.getvalue())
    with pytest.raises(PublishError):
        publish_site(core, bundle([("index.html", b"A" * 100)]), expanded_size_bytes=10)


def test_retention_and_delete_wait_for_active_lease(core):
    item = publish(core, access="link", purge_mode="immediate")
    lease = core.acquire_lease(item["current_revision"])
    core.expire(item["id"])
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "purge_pending"
    core.release_lease(lease)
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "purged"
    with pytest.raises(PublishError):
        core.extend(item["id"], 60)
    another = publish(core)
    core.delete_publication(another["id"])
    core.sweep()
    with pytest.raises(PublishError):
        core.get_publication(another["id"])
    assert core.delete_publication(another["id"])["deletion_state"] == "complete"


def test_extend_cancels_pending_purge_preserves_manual_pause(core):
    item = publish(core, purge_mode="immediate")
    lease = core.acquire_lease(item["current_revision"])
    core.expire(item["id"])
    core.pause(item["id"], True)
    core.sweep()
    result = core.extend(item["id"], 120)
    assert result["paused"] and result["content_state"] == "present"
    core.release_lease(lease)
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "present"


def test_phase_crash_recovery_and_unknown_schema_refusal(core):
    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 1}, "owner"
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    with core.repository.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE upload SET state='preparing',deadline=? WHERE id=?",
            (iso(now() - timedelta(seconds=1)), upload["id"]),
        )
    Core(core.config).initialize().sweep()
    assert not core.storage.stage(upload["id"]).exists()
    assert core.storage_snapshot()["reserved_bytes"] == 0
    with core.repository.connect(immediate=True) as conn:
        conn.execute("UPDATE core_schema_version SET version=99")
    with pytest.raises(PublishError) as error:
        Core(core.config).initialize()
    assert error.value.code == "unknown_core_schema"


def test_legacy_database_is_never_mutated(core):
    target = core.config.data_dir / "legacy.sqlite3"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE artifact(id TEXT)")
    from minder_publish.server.repository import Repository

    with pytest.raises(PublishError):
        Repository(target).initialize()
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='core_schema_version'"
            ).fetchone()
            is None
        )


def test_core_migration_up_down_round_trip(tmp_path):
    from importlib.resources import files

    migration = files("minder_publish.server").joinpath("migrations/core")
    with sqlite3.connect(tmp_path / "migration.db") as conn:
        conn.executescript(migration.joinpath("001_up.sql").read_text())
        assert conn.execute("SELECT version FROM core_schema_version").fetchone() == (
            1,
        )
        conn.executescript(migration.joinpath("001_down.sql").read_text())
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()


def test_external_reservations_persist_ownership_accounting_and_expire(core):
    reservation = core.reserve_external(1000, "proxy")
    assert Core(core.config).initialize().storage_snapshot()["reserved_bytes"] == 1000
    parser = core.config.data_dir / "parser-temp"
    parser.mkdir()
    (parser / "spool").write_bytes(b"x" * 500)
    snapshot = core.storage_snapshot()
    assert snapshot["reserved_bytes"] == 1000
    # Parser bytes are represented by their held reservation, not charged twice.
    assert (
        snapshot["parser_reserved_bytes"] == 500
        and snapshot["parser_unreserved_bytes"] == 0
    )
    with pytest.raises(PublishError):
        core.release_external(reservation, "stranger")
    core.release_external(reservation, "proxy")
    core.release_external(reservation, "proxy")
    assert core.storage_snapshot()["reserved_bytes"] == 0
    assert core.storage_snapshot()["parser_unreserved_bytes"] == 500
    for invalid in (
        -1,
        True,
        core.config.limits["upload_payload_bytes"]
        + core.config.limits["multipart_overhead_bytes"]
        + 1,
    ):
        with pytest.raises(PublishError):
            core.reserve_external(invalid, "proxy")
    reservation = core.reserve_external(1000, "proxy")
    with core.repository.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE external_reservation SET deadline=? WHERE id=?",
            (iso(now() - timedelta(seconds=1)), reservation),
        )
    core.sweep()
    assert core.storage_snapshot()["reserved_bytes"] == 0


def test_retired_revision_gc_and_lease_barrier(core):
    first = publish(core)
    second = publish(
        core,
        b"next",
        publication_id=first["id"],
        expected_revision=first["current_revision"],
    )
    lease = core.acquire_lease(first["current_revision"])
    past = iso(now() - timedelta(seconds=1))
    with core.repository.connect(immediate=True) as conn:
        conn.execute(
            "UPDATE revision SET drain_until=?,recovery_until=? WHERE id=?",
            (past, past, first["current_revision"]),
        )
    core.sweep()
    assert core.get_revision(first["current_revision"])["state"] == "retired"
    core.release_lease(lease)
    core.sweep()
    with pytest.raises(PublishError):
        core.get_revision(first["current_revision"])
    assert (
        core.resolve_file(second["current_revision"], "note.txt")[0].read_bytes()
        == b"next"
    )


def test_prepare_and_commit_crash_keep_old_revision(core, monkeypatch):
    first = publish(core)
    upload = core.create_upload(
        {
            "kind": "file",
            "filename": "next",
            "size_bytes": 1,
            "publication_id": first["id"],
            "expected_revision": first["current_revision"],
        },
        "owner",
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    original = core.storage.promote

    def fail_after_rename(*args):
        original(*args)
        raise OSError("injected crash after rename before database commit")

    monkeypatch.setattr(core.storage, "promote", fail_after_rename)
    with pytest.raises(OSError):
        core.commit_upload(upload["id"], "owner")
    assert (
        core.get_publication(first["id"])["current_revision"]
        == first["current_revision"]
    )
    assert (
        core.resolve_file(first["current_revision"], "note.txt")[0].read_bytes()
        == b"hello"
    )
    # Unreferenced renamed bytes are deliberately left for orphan GC after the grace window.
    assert core.storage_snapshot()["reserved_bytes"] == 0


def test_migration_v1_to_current_preserves_publications(core, tmp_path):
    from importlib.resources import files

    target = tmp_path / "v1.sqlite3"
    with sqlite3.connect(target) as conn:
        conn.executescript(
            files("minder_publish.server")
            .joinpath("migrations/core/001_up.sql")
            .read_text()
        )
        conn.execute(
            "INSERT INTO publication VALUES (?,?)", ("example", '{"id":"example"}')
        )
    from minder_publish.server.repository import Repository

    repository = Repository(target)
    repository.initialize()
    with repository.connect() as conn:
        assert (
            conn.execute("SELECT version FROM core_schema_version").fetchone()[0] == 6
        )
        assert conn.execute("SELECT id FROM publication").fetchone()[0] == "example"
        assert (
            conn.execute("SELECT COUNT(*) FROM external_reservation").fetchone()[0] == 0
        )


def test_material_budget_reduction_blocks_writes_without_purge(core):
    item = publish(core, b"x" * 10000)
    # A deployment envelope smaller than actual use remains read-only; no collection is implied.
    core.config.storage_budget_bytes = 100
    assert core.storage_snapshot()["write_blocked"]
    with pytest.raises(PublishError):
        core.create_upload(
            {"kind": "file", "filename": "next", "size_bytes": 1}, "owner"
        )
    assert (
        core.resolve_file(item["current_revision"], "note.txt")[0].stat().st_size
        == 10000
    )


def test_zip_nul_path_and_missing_entry_rejected(core):
    payload = bundle([("index.html", b"x"), ("badXname", b"y")]).replace(
        b"badXname", b"bad\x00name"
    )
    with pytest.raises(PublishError):
        publish_site(core, payload)
    with pytest.raises(PublishError):
        publish_site(core, bundle([("other.html", b"x")]))


def test_export_concurrency_and_collection_lease(core):
    item = publish(core)
    first = core.acquire_lease(item["current_revision"], "export")
    with pytest.raises(PublishError) as error:
        core.acquire_lease(item["current_revision"], "export")
    assert error.value.code == "exports_busy"
    core.renew_lease(first)
    core.release_lease(first)
    other = core.acquire_lease(item["current_revision"], "export")
    core.release_lease(other)


def test_active_abort_waits_for_receiver_then_releases(core):
    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 2}, "owner"
    )

    async def cancelling():
        yield b"x"
        result = core.abort_upload(upload["id"], "owner")
        assert result["state"] == "aborting"
        assert core.storage_snapshot()["reserved_bytes"] > 0
        yield b"x"

    with pytest.raises(PublishError):
        asyncio.run(core.receive_upload(upload["id"], cancelling(), "owner"))
    assert core.storage_snapshot()["reserved_bytes"] == 0
    assert not core.storage.stage(upload["id"]).exists()
    assert core.abort_upload(upload["id"], "owner")["state"] == "aborted"


def test_metadata_retention_never_and_after_purge(core):
    private = publish(
        core, expiry={"mode": "at", "value": "2000-01-01T00:00:00Z"}, purge_mode="never"
    )
    core.sweep()
    assert core.get_publication(private["id"])["content_state"] == "present"
    item = publish(
        core,
        purge_mode="immediate",
        metadata_policy="after_purge",
        metadata_after_purge_seconds=0,
    )
    core.expire(item["id"])
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "purged"
    core.sweep()
    with pytest.raises(PublishError):
        core.get_publication(item["id"])


def test_capacity_report_counts_retained_catalog_records(core):
    assert core.settings()["storage"]["publication_count"] == 0
    item = publish(core, purge_mode="immediate", metadata_policy="keep")
    settings = core.settings()
    assert settings["storage"]["publication_count"] == 1
    assert settings["storage"]["used_bytes"] >= len(b"hello")

    core.expire(item["id"])
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "purged"
    storage = core.settings()["storage"]
    assert storage["publication_count"] == 1
    assert storage["used_bytes"] == 0


def test_invalid_timezone_and_non_rfc3339_expiry_rejected(core):
    item = publish(core)
    for value in [
        "2026-01-01",
        "2026-01-01 00:00:00+00:00",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00+0000",
    ]:
        with pytest.raises(PublishError):
            core.patch_publication(
                item["id"], {"expiry": {"mode": "at", "value": value}}
            )
    with pytest.raises(PublishError):
        core.update_settings({"defaults": {"timezone": "Wrong/Zone"}})
    assert (
        core.update_settings({"defaults": {"timezone": "Asia/Tbilisi"}})["defaults"][
            "timezone"
        ]
        == "Asia/Tbilisi"
    )


def test_archive_metadata_preflight_bounded_before_zipfile(core, monkeypatch):
    payload = bytearray(bundle([("index.html", b"file")]))
    import struct

    position = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", payload, position + 8, 50000, 50000)
    with pytest.raises(PublishError) as error:
        publish_site(core, bytes(payload))
    assert error.value.code == "archive_metadata_limit"


def test_export_lease_release_idempotent_and_unknown_kind(core):
    item = publish(core)
    lease = core.acquire_lease(item["current_revision"])
    core.release_lease(lease)
    core.release_lease(lease)
    with pytest.raises(PublishError):
        core.renew_lease(lease)
    with pytest.raises(PublishError):
        core.acquire_lease(item["current_revision"], "unknown")


def test_legacy_refusal_does_not_change_journal_or_file_permissions(tmp_path):
    target = tmp_path / "legacy.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE artifact(id TEXT)")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    target.chmod(0o644)
    from minder_publish.server.repository import Repository

    with pytest.raises(PublishError):
        Repository(target).initialize()
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_private_storage_permissions(core):
    publication = publish(core)
    target, _ = core.resolve_file(publication["current_revision"], "note.txt")
    assert stat.S_IMODE(core.config.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(core.config.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restart_with_reduced_operator_budget_preserves_reads(core):
    items = [publish(core, b"x" * (1 << 20)) for _ in range(4)]
    core.config.storage_budget_bytes = 4 << 20
    restarted = Core(core.config).initialize()
    assert restarted.storage_snapshot()["write_blocked"]
    assert (
        restarted.resolve_file(items[0]["current_revision"], "note.txt")[0]
        .stat()
        .st_size
        == 1 << 20
    )
    with pytest.raises(PublishError):
        publish(restarted, b"next")


def test_guest_counter_does_not_conflict_or_get_lost_on_content_update(core):
    first = publish(core, access="link")
    upload = core.create_upload(
        {
            "kind": "file",
            "filename": "note.txt",
            "size_bytes": 1,
            "publication_id": first["id"],
            "expected_revision": first["current_revision"],
        },
        "owner",
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    core.record_view(first["id"])
    assert core.get_publication(first["id"])["updated_at"] == first["updated_at"]
    updated = core.commit_upload(upload["id"], "owner")
    assert updated["view_count"] == 1 and updated["last_viewed"]
    core.patch_publication(first["id"], {"guest_counts": False})
    core.record_view(first["id"])
    assert core.get_publication(first["id"])["view_count"] == 1


def test_internal_requested_id_is_not_public_plan_field(core):
    with pytest.raises(PublishError):
        core.create_upload(
            {
                "kind": "file",
                "filename": "x",
                "size_bytes": 0,
                "requested_id": "custom",
            },
            "owner",
        )
    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 0},
        "owner",
        requested_id="custom-slug",
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(b""), "owner"))
    assert core.commit_upload(upload["id"], "owner")["id"] == "custom-slug"
    with pytest.raises(PublishError):
        core.create_upload(
            {"kind": "file", "filename": "x", "size_bytes": 0},
            "owner",
            requested_id="custom-slug",
        )


def test_password_utf8_byte_cap_and_invalid_unicode(core):
    with pytest.raises(PublishError):
        publish(core, access="password", password="😀" * 300)
    item = publish(core, access="password", password="😀" * 200)
    assert item["has_password"]
    for patch in ({"title": "\ud800"}, {"summary": "\ud800"}, {"password": "\ud800"}):
        with pytest.raises(PublishError):
            core.patch_publication(item["id"], patch)


def test_background_thumbnail_lease_and_shutdown_barrier(core, monkeypatch):
    import threading

    from minder_publish.server import thumbnails

    started = threading.Event()
    finish = threading.Event()

    def controlled_decoder(source, target, limits):
        started.set()
        assert finish.wait(3)
        target.write_bytes(b"generated")
        return True

    monkeypatch.setattr(thumbnails, "generate_thumbnail", controlled_decoder)
    item = publish(core, b"not-decoded-by-this-test", "image.png")
    assert started.wait(1)
    core.delete_publication(item["id"])
    core.sweep()
    assert core.get_publication(item["id"])["content_state"] == "purge_pending"
    finish.set()
    core.close()
    core.sweep()
    with pytest.raises(PublishError):
        core.get_publication(item["id"])


def test_extending_never_would_shorten_and_is_rejected(core):
    item = publish(core)
    with pytest.raises(PublishError) as error:
        core.extend(item["id"], 60)
    assert error.value.code == "no_expiry"
    assert core.get_publication(item["id"])["expires_at"] is None


def test_policy_cas_survives_frozen_updated_timestamp(core, monkeypatch):
    import minder_publish.server.core as module

    fixed = module.now()
    monkeypatch.setattr(module, "now", lambda: fixed)
    item = publish(core)
    upload = core.create_upload(
        {
            "kind": "file",
            "filename": "note.txt",
            "size_bytes": 1,
            "publication_id": item["id"],
            "expected_revision": item["current_revision"],
        },
        "owner",
    )
    core.patch_publication(item["id"], {"title": "Do not overwrite"})
    assert core.get_publication(item["id"])["updated_at"] == item["updated_at"]
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    with pytest.raises(PublishError) as error:
        core.commit_upload(upload["id"], "owner")
    assert error.value.code == "publication_changed"
    assert core.get_publication(item["id"])["title"] == "Do not overwrite"


def test_purged_restore_requires_explicit_null_cas_and_expiry(core):
    item = publish(core, purge_mode="immediate")
    core.expire(item["id"])
    core.sweep()
    assert core.get_publication(item["id"])["current_revision"] is None
    base = {
        "kind": "file",
        "filename": "restored.txt",
        "size_bytes": 1,
        "publication_id": item["id"],
        "expiry": {"mode": "never"},
    }
    with pytest.raises(PublishError):
        core.create_upload(base, "owner")
    with pytest.raises(PublishError):
        core.create_upload({**base, "expected_revision": None, "expiry": None}, "owner")
    upload = core.create_upload({**base, "expected_revision": None}, "owner")
    asyncio.run(core.receive_upload(upload["id"], chunks(b"r"), "owner"))
    restored = core.commit_upload(upload["id"], "owner")
    assert restored["current_revision"] and restored["content_state"] == "present"
    assert restored["access_version"] > item["access_version"]


@pytest.mark.parametrize(
    "field",
    ["path_depth", "metadata_body_bytes", "disk_reserve_bytes", "title_characters"],
)
def test_operator_guards_not_writable_via_owner_settings(core, field):
    previous = core.settings()["limits"][field]
    with pytest.raises(PublishError) as error:
        core.update_settings({"limits": {field: previous + 1}})
    assert error.value.code == "operator_setting_required"
    assert core.settings()["limits"][field] == previous


def test_upload_size_defaults_apply_only_to_new_instances(tmp_path):
    data = tmp_path / "fresh"
    fresh = Core(Config(data)).initialize()
    limits = fresh.policy_settings()["limits"]
    assert limits["file_bytes"] == limits["upload_payload_bytes"] == 109 * 2**20

    existing_data = tmp_path / "existing"
    existing_config = Config(existing_data)
    existing_config.limits.update(file_bytes=2**30, upload_payload_bytes=2**30)
    Core(existing_config).initialize()
    reopened = Core(Config(existing_data)).initialize()
    limits = reopened.policy_settings()["limits"]
    assert limits["file_bytes"] == limits["upload_payload_bytes"] == 2**30


def test_materials_limit_blocks_new_records_without_blocking_replacement(core):
    first = publish(core)
    second = publish(core)
    limit = core.update_settings({"limits": {"publication_count": 1}})
    assert limit["storage"]["publication_count"] == 2
    assert limit["limits"]["publication_count"] == 1
    with pytest.raises(PublishError) as error:
        publish(core)
    assert error.value.code == "publication_count_limit"
    updated = publish(
        core, b"new version", publication_id=first["id"], expected_revision=first["current_revision"]
    )
    assert updated["id"] == first["id"]
    assert core.get_publication(second["id"])["content_state"] == "present"
    assert core.storage_snapshot()["used_bytes"] >= len(b"new version")


def test_usage_scan_keeps_memory_bounded_and_accounts_one_observation(
    core, monkeypatch
):
    import tracemalloc

    import minder_publish.server.core as module

    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 32}, "owner"
    )
    external = core.reserve_external(5, "parser")
    with core.repository.connect() as connection:
        reservation = connection.execute(
            "SELECT reservation_bytes FROM upload WHERE id=?", (upload["id"],)
        ).fetchone()[0]

    class OnePass:
        def __init__(self):
            self.position = 0
            self.iterations = 0

        def __len__(self):
            raise AssertionError("The filesystem observation was materialized")

        def __iter__(self):
            self.iterations += 1
            assert self.iterations == 1
            return self

        def __next__(self):
            self.position += 1
            if self.position <= 100000:
                return f"revisions/old/content/file-{self.position}", 1
            if self.position == 100001:
                return f"staging/{upload['id']}/payload", 11
            if self.position == 100002:
                return "parser-temp/spool", 5
            if self.position == 100003:
                return "publish.sqlite3", 100
            raise StopIteration

    observed = OnePass()
    monkeypatch.setattr(module, "iter_bytes", lambda root: observed)
    tracemalloc.start()
    try:
        result = core.storage_snapshot()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["material_used_bytes"] == 100011
    assert result["total_used_bytes"] == 100111
    assert result["reserved_bytes"] == reservation - 11 + 5
    assert result["parser_reserved_bytes"] == 5
    assert peak < 2 * 1024**2
    core.release_external(external, "parser")


def test_storage_iterator_yields_before_discovering_remaining_files(
    tmp_path, monkeypatch
):
    from minder_publish.server import storage

    for number in range(1000):
        (tmp_path / f"file-{number}").write_bytes(b"x")
    original = storage.os.scandir
    discovered = []

    class Observe:
        def __init__(self, root):
            self.inner = original(root)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self.inner)
            discovered.append(value.name)
            return value

    monkeypatch.setattr(storage.os, "scandir", Observe)
    iterator = storage.iter_bytes(tmp_path)
    assert discovered == []
    assert next(iterator)[1] == 1
    assert len(discovered) == 1
    iterator.close()


def test_generated_manifest_metadata_guard_precedes_publication(core):
    first = publish(core)
    core.config.operations["export_metadata_bytes"] = 100
    with pytest.raises(PublishError) as error:
        publish(
            core,
            b"next",
            publication_id=first["id"],
            expected_revision=first["current_revision"],
        )
    assert error.value.code == "manifest_metadata_limit"
    assert (
        core.get_publication(first["id"])["current_revision"]
        == first["current_revision"]
    )
    assert (
        core.resolve_file(first["current_revision"], "note.txt")[0].read_bytes()
        == b"hello"
    )
    assert core.storage_snapshot()["reserved_bytes"] == 0


def test_directory_flush_is_postorder_without_materializing_tree(tmp_path, monkeypatch):
    from minder_publish.server import storage

    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    flushed = []
    monkeypatch.setattr(
        storage,
        "fsync_directory",
        lambda path: flushed.append(path.relative_to(tmp_path).as_posix()),
    )
    storage.fsync_tree(tmp_path)
    assert flushed == ["a/b", "a", "."]
