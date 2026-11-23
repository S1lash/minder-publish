"""Stable names reserve an independent namespace around immutable material IDs."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib.resources import files

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.backup import backup, export_tombstones, restore
from minder_publish.server.core import Core
from minder_publish.server.repository import Repository
from test_core_domain import chunks, core, publish
from test_operations import instance


def authorized(connection):
    pass


def rename(core, publication, name, keep_previous=True, expected=None):
    return core.rename_publication_link(
        publication["id"], name, keep_previous, expected or publication["link_name"],
        authorize=authorized,
    )


def test_rename_alias_revocation_and_reclaim_preserve_identity(core):
    original = publish(core, key="original")
    current = rename(core, original, "published-report")
    assert current["id"] == original["id"]
    assert current["stable_url"].endswith("/p/published-report")
    assert current["previous_links"][0]["name"] == original["id"]
    assert core.get_linked_publication(original["id"])["link_name"] == "published-report"
    assert core.resolve_link_name("published-report") == original["id"]
    assert core.get_publication(original["id"])["current_revision"] == original["current_revision"]
    assert core.list_publications()["items"][0]["link_name"] == "published-report"

    with pytest.raises(PublishError) as error:
        rename(core, original, "unexpected")
    assert error.value.code == "publication_link_changed"
    with pytest.raises(PublishError) as error:
        rename(core, current, "viewer")
    assert (error.value.code, error.value.field) == ("reserved_key", "name")

    revoked = core.disable_publication_alias(original["id"], original["id"], authorize=authorized)
    assert revoked["previous_links"] == []
    with pytest.raises(PublishError, match="publication not found"):
        core.resolve_link_name(original["id"])
    other = publish(core, key="other-material")
    with pytest.raises(PublishError, match="publication exists"):
        rename(core, other, original["id"])

    reclaimed = rename(core, current, original["id"], keep_previous=False)
    assert reclaimed["previous_links"] == []
    assert core.resolve_link_name(original["id"]) == original["id"]
    with pytest.raises(PublishError, match="publication not found"):
        core.resolve_link_name("published-report")


def test_active_alias_promotes_and_namespace_reserves_uploads_and_deleted_names(core):
    original = publish(core, key="original")
    current = rename(core, original, "published-report")
    promoted = rename(core, current, original["id"])
    assert [entry["name"] for entry in promoted["previous_links"]] == ["published-report"]
    reservation = core.create_upload(
        {"kind": "file", "filename": "x", "key": "pending-name", "size_bytes": 1}, "owner"
    )
    with pytest.raises(PublishError, match="publication exists"):
        rename(core, original, "pending-name")
    assert core.abort_upload(reservation["id"], "owner")["state"] == "aborted"
    renamed = rename(core, original, "pending-name")
    core.delete_publication(original["id"])
    with pytest.raises(PublishError, match="publication not found"):
        core.resolve_link_name("published-report")
    other = publish(core, key="other")
    with pytest.raises(PublishError, match="publication exists"):
        rename(core, other, renamed["link_name"])


def test_two_writers_cannot_claim_same_name(core):
    originals = [publish(core), publish(core)]

    def claim(original):
        try:
            return rename(core, original, "one-slot")["id"]
        except PublishError as exc:
            assert exc.code == "publication_exists"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, originals))
    winners = [owner for owner in results if owner]
    assert len(winners) == 1
    assert core.resolve_link_name("one-slot") == winners[0]


def test_replacement_checks_stale_name_at_receive_and_commit(core):
    publication = publish(core, key="original")
    replace_plan = {
        "publication_id": publication["id"],
        "expected_revision": publication["current_revision"],
        "kind": "file",
        "filename": "note.txt",
        "size_bytes": 1,
    }
    legacy = core.create_upload(replace_plan, "owner")
    aware = core.create_upload({**replace_plan, "supports_link_names": True}, "owner")
    asyncio.run(core.receive_upload(aware["id"], chunks(b"x"), "owner"))
    rename(core, publication, "new-name")
    with pytest.raises(PublishError, match="publication link upgrade required"):
        asyncio.run(core.receive_upload(legacy["id"], chunks(b"x"), "owner"))
    with pytest.raises(PublishError, match="publication link upgrade required"):
        core.create_upload(replace_plan, "owner")
    with pytest.raises(PublishError, match="publication link changed"):
        core.commit_upload(aware["id"], "owner")
    assert core.get_publication(publication["id"])["current_revision"] == publication["current_revision"]
    replaced = publish(core, payload=b"x", supports_link_names=True,
                       publication_id=publication["id"], expected_revision=publication["current_revision"])
    assert replaced["link_name"] == "new-name"


def test_successful_commit_preserves_receipt_if_renamed_after_commit(core, monkeypatch):
    upload = core.create_upload({"kind": "file", "filename": "note.txt", "size_bytes": 1}, "owner")
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    core.inspect_upload(upload["id"], "owner")
    remove_stage = core.storage.remove_stage

    def rename_before_stage_cleanup(upload_id):
        detail = core.list_publications()["items"][0]
        rename(core, detail, "new-name", keep_previous=False)
        return remove_stage(upload_id)

    monkeypatch.setattr(core.storage, "remove_stage", rename_before_stage_cleanup)
    original_receipt = core.commit_upload(upload["id"], "owner")
    assert original_receipt["link_name"] == original_receipt["id"]
    assert core.commit_upload(upload["id"], "owner")["link_name"] == original_receipt["id"]
    status = core.upload_status(upload["id"], "owner")
    assert status["link_name"] == "new-name"
    assert status["stable_url"].endswith("/p/new-name")


def test_schema_five_backfills_legacy_and_deleted_ids(tmp_path):
    path = tmp_path / "schema-four.sqlite3"
    migrations = files("minder_publish.server").joinpath("migrations/core")
    with sqlite3.connect(path) as connection:
        for version in (1, 2, 3, 4):
            connection.executescript(migrations.joinpath(f"{version:03d}_up.sql").read_text())
        connection.execute("INSERT INTO publication VALUES (?,?)", ("Old_Name", '{"id":"Old_Name"}'))
        connection.execute("INSERT INTO deletion_tombstone VALUES (?,?)", ("deleted", "2026-01-01T00:00:00Z"))
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT name,publication_id,is_primary,retired_at IS NULL FROM publication_link ORDER BY name"
        )] == [("Old_Name", "Old_Name", 1, 1), ("deleted", "deleted", 1, 0)]


def test_restore_reserves_later_names_without_activating_them(instance, tmp_path):
    config, core, _ = instance
    original = publish(core, key="original")
    archive, ledger = tmp_path / "backup.tar", tmp_path / "ledger.json"
    backup(config, archive)
    rename(core, original, "later-primary")
    export_tombstones(config, ledger)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    restore(target, archive, ledger)
    restored = Core(target).initialize()
    assert restored.resolve_link_name("original") == original["id"]
    with pytest.raises(PublishError, match="publication not found"):
        restored.resolve_link_name("later-primary")
    with pytest.raises(PublishError, match="publication exists"):
        publish(restored, key="later-primary")
    assert rename(restored, original, "later-primary")["link_name"] == "later-primary"


def test_restore_rejects_snapshot_whose_primary_the_ledger_revokes(instance, tmp_path):
    config, core, _ = instance
    original = publish(core, key="original")
    archive, ledger = tmp_path / "backup.tar", tmp_path / "ledger.json"
    backup(config, archive)
    rename(core, original, "later-primary", keep_previous=False)
    export_tombstones(config, ledger)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    with pytest.raises(PublishError) as error:
        restore(target, archive, ledger)
    assert error.value.code == "restore_publication_link_revoked"
    assert not target.db_path.exists()


async def test_link_management_requires_current_owner_and_csrf(application, client, monkeypatch):
    from test_http import publish as publish_http, sign_in

    publication, manage = await publish_http(application, client)
    path = f"/api/v1/publications/{publication['id']}/link"
    payload = {"name": "renamed", "keep_previous": True, "expected_name": publication["id"]}
    assert (await client.get(path, headers=manage)).status_code == 403
    assert (await client.patch(path, content=b"malformed", headers=manage)).status_code == 403
    owner = await sign_in(client)
    assert (await client.patch(path, json=payload)).status_code == 403
    assert (await client.patch(path, json=payload, headers={**owner, "Origin": "https://evil.test"})).status_code == 403
    response = await client.patch(path, json=payload, headers=owner)
    assert response.status_code == 200
    assert response.json()["data"]["previous_links"][0]["name"] == publication["id"]
    assert (await client.patch(path, json=payload, headers=owner)).status_code == 409
    alias_path = f"{path}/{publication['id']}"
    assert (await client.patch(alias_path, content=b"malformed", headers=manage)).status_code == 403
    assert (await client.patch(alias_path, json={"preview_mode": "off"})).status_code == 403
    edited = await client.patch(alias_path, json={"preview_mode": "off"}, headers=owner)
    assert edited.status_code == 200
    assert edited.json()["data"]["previous_links"][0]["preview_mode"] == "off"
    assert (await client.delete(alias_path, headers=manage)).status_code == 403
    response = await client.delete(alias_path, headers=owner)
    assert response.status_code == 200 and not response.json()["data"]["previous_links"]

    auth = application.state.auth
    validate_csrf = auth.validate_csrf

    def revoke_session(raw, token):
        allowed = validate_csrf(raw, token)
        with auth.connection(write=True) as connection:
            connection.execute("DELETE FROM auth_sessions")
        return allowed

    monkeypatch.setattr(auth, "validate_csrf", revoke_session)
    payload.update(name="another-name", expected_name="renamed")
    assert (await client.patch(path, json=payload, headers=owner)).status_code == 401
    assert application.state.core.get_publication(publication["id"])["link_name"] == "renamed"


def test_preview_modes_follow_exact_address_and_default(core):
    original = publish(core, key="original")
    material_id = original["id"]
    settings = core.update_settings({"defaults": {"link_previews": False}})
    assert settings["defaults"]["link_previews"] is False
    assert core.publication_link(material_id)["preview_default"] is False
    assert core.get_linked_publication(material_id)["link_preview_enabled"] is False
    with pytest.raises(PublishError) as failed:
        core.update_settings({"defaults": {"link_previews": 1}})
    assert (failed.value.code, failed.value.field) == ("invalid_boolean", "link_previews")
    with pytest.raises(PublishError) as failed:
        core.rename_publication_link(material_id, material_id, True, material_id,
                                     preview_mode=None, authorize=authorized)
    assert (failed.value.code, failed.value.field) == ("invalid_preview_mode", "preview_mode")

    def rename_with_mode(name, expected, **options):
        return core.rename_publication_link(material_id, name, True, expected,
                                            authorize=authorized, **options)

    assert rename_with_mode(material_id, material_id, preview_mode="on")["preview_mode"] == "on"
    assert core.get_linked_publication(material_id)["link_preview_enabled"] is True
    inherited = rename_with_mode("new-name", material_id)
    assert inherited["preview_mode"] == inherited["previous_links"][0]["preview_mode"] == "on"
    updated = core.set_alias_preview(material_id, material_id, "off", authorize=authorized)
    assert updated["preview_mode"] == "on"
    assert updated["previous_links"][0]["preview_mode"] == "off"
    assert not core.link_preview_enabled(material_id, publication_id=material_id)
    assert core.link_preview_enabled("new-name", publication_id=material_id)
    assert not core.link_preview_enabled("new-name", publication_id="another-material")
    assert not core.link_preview_enabled("missing")
    core.disable_publication_alias(material_id, material_id, authorize=authorized)
    with pytest.raises(PublishError, match="publication link not found"):
        core.set_alias_preview(material_id, material_id, "on", authorize=authorized)
    reclaimed = rename_with_mode(material_id, "new-name")
    assert reclaimed["preview_mode"] == "off"
    assert reclaimed["previous_links"][0]["preview_mode"] == "on"
    promoted = rename_with_mode("new-name", material_id, preview_mode="inherit")
    assert promoted["preview_mode"] == "inherit"
    assert promoted["previous_links"][0]["preview_mode"] == "off"
    assert not core.link_preview_enabled("new-name")
    core.update_settings({"defaults": {"link_previews": True}})
    assert core.link_preview_enabled("new-name")
    assert not core.link_preview_enabled(material_id)


def test_legacy_backup_restores_addresses_with_inherited_previews(instance, tmp_path):
    config, core, _ = instance
    original = publish(core, key="original")
    rename(core, original, "new-name")
    migrations = files("minder_publish.server").joinpath("migrations/core")
    with sqlite3.connect(config.db_path) as connection:
        connection.executescript(migrations.joinpath("006_down.sql").read_text())
        connection.execute(
            "UPDATE core_settings SET data=json_remove(data,'$.defaults.link_previews')"
        )
    archive = tmp_path / "schema-five.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "restored", secret="")
    restore(target, archive)
    restored = Core(target).initialize()
    snapshot = restored.publication_link(original["id"])
    assert snapshot["preview_default"] is True
    assert snapshot["preview_mode"] == snapshot["previous_links"][0]["preview_mode"] == "inherit"
    assert restored.link_preview_enabled("new-name")
    assert restored.link_preview_enabled(original["id"])


def test_offline_downgrade_preserves_preview_policy(core):
    material = publish(core, key="original")
    material_id = material["id"]
    core.rename_publication_link(material_id, material_id, True, material_id,
                                 preview_mode="off", authorize=authorized)
    with pytest.raises(PublishError) as failed:
        core.repository.downgrade_owner_statistics(3)
    assert failed.value.code == "link_preview_policy_prevents_downgrade"
    assert core.publication_link(material_id)["preview_mode"] == "off"
