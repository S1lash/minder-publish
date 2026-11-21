"""Custom publication keys and sparse password hints across real boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_core_domain as domain
import test_operations as operations
from test_core_domain import chunks, publish

from minder_publish.client import cli
from minder_publish.client.errors import ClientError
from minder_publish.errors import PublishError
from minder_publish.server.storage import safe_id

core = domain.core
legacy = operations.legacy


def test_create_only_key_collision_and_tombstone(core):
    plan = dict(kind="file", filename="note.txt", size_bytes=1, key="my-report")

    def claim():
        try:
            return core.create_upload(plan, "owner")
        except PublishError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    upload = next(value for value in results if isinstance(value, dict))
    assert "publication_exists" in results
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    item = core.commit_upload(upload["id"], "owner")
    assert item["id"] == "my-report"
    with pytest.raises(PublishError) as error:
        core.create_upload(
            {
                **plan,
                "publication_id": item["id"],
                "expected_revision": item["current_revision"],
            },
            "owner",
        )
    assert error.value.code == "invalid_requested_id"
    with pytest.raises(PublishError):
        core.patch_publication(item["id"], {"key": "new-report"})
    core.delete_publication(item["id"])
    with pytest.raises(PublishError) as error:
        core.create_upload(plan, "owner")
    assert error.value.code == "publication_exists"


@pytest.mark.parametrize(
    "key",
    [
        "login",
        "oauth",
        "mcp",
        "api",
        "healthz",
        "downloads",
        "upload",
        "UPPER",
        "a",
        "a_b",
        "x" * 65,
        None,
    ],
)
def test_custom_key_validation_does_not_tighten_imported_ids(core, key):
    assert safe_id("Old_Key") == "Old_Key"
    with pytest.raises(PublishError):
        core.create_upload(
            dict(kind="file", filename="x", size_bytes=0, key=key), "owner"
        )
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload").fetchone()[0] == 0


def test_hint_sparse_rotation_clear_and_validation(core):
    item = publish(
        core, access="password", password="first password", password_hint="A clue"
    )
    assert item["password_hint"] == "A clue"
    for patch in ({"title": "New"}, {"password": "second password"}):
        item = core.patch_publication(item["id"], patch)
        assert item["password_hint"] == "A clue"
    item = core.patch_publication(item["id"], {"password_hint": ""})
    assert item["password_hint"] == ""
    core.patch_publication(item["id"], {"password_hint": "new clue"})
    item = core.patch_publication(item["id"], {"access": "link"})
    assert item["password_hint"] == "" and not item["has_password"]
    assert (
        core.patch_publication(item["id"], {"password_hint": "saved for later"})[
            "password_hint"
        ]
        == "saved for later"
    )
    for value in (None, 42, "x" * 201, "\ud800"):
        with pytest.raises(PublishError):
            core.patch_publication(item["id"], {"password_hint": value})
    assert core.get_publication(item["id"])["password_hint"] == "saved for later"


def test_cli_create_key_and_sparse_hint(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "publish",
            "x",
            "--kind",
            "file",
            "--key",
            "my-report",
            "--hint",
            "",
            "--password-prompt",
        ]
    )
    monkeypatch.setattr(cli, "_secret", lambda **kwargs: "guest passphrase")
    assert cli._metadata(args) == {
        "key": "my-report",
        "password_hint": "",
        "password": "guest passphrase",
        "access": "password",
    }
    assert cli._metadata(parser.parse_args(["set", "id", "--title", "hello"])) == {
        "title": "hello"
    }
    for command in (["set", "id"], ["update", "id", "x"]):
        with pytest.raises(ClientError):
            parser.parse_args(command + ["--key", "other-key"])


async def test_legacy_hint_roundtrip_and_escaped_guest_gate(application, client):
    token = application.state.auth.create_token("legacy", "materials:manage")["token"]
    headers = {"Authorization": "Bearer " + token}
    hint = '<img src=x onerror="alert(1)"> & clue'
    response = await client.post(
        "/api/publish",
        headers=headers,
        data={"key": "my-report", "password": "guest passphrase", "hint": hint},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["id"] == "my-report"
    assert response.json()["password_hint"] == hint
    gate = await client.get("/p/my-report")
    assert gate.status_code == 200
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; clue" in gate.text
    assert hint not in gate.text
    for data in ({"title": "changed"}, {"password": "rotated passphrase"}):
        response = await client.patch(
            "/api/artifact/my-report", headers=headers, data=data
        )
        assert response.status_code == 200, response.text
        assert response.json()["password_hint"] == hint
    response = await client.patch(
        "/api/artifact/my-report", headers=headers, data={"hint": ""}
    )
    assert response.json()["password_hint"] == ""
    response = await client.patch(
        "/api/artifact/my-report", headers=headers, data={"hint": "again"}
    )
    assert response.json()["password_hint"] == "again"
    response = await client.patch(
        "/api/artifact/my-report", headers=headers, data={"clear_password": "true"}
    )
    assert (
        response.json()["password_hint"] == "" and not response.json()["has_password"]
    )


async def test_canonical_api_key_hint_and_replacement_preservation(application, client):
    token = application.state.auth.create_token("api", "materials:manage")["token"]
    headers = {"Authorization": "Bearer " + token}
    response = await client.post(
        "/api/v1/uploads",
        headers=headers,
        json={
            "kind": "file",
            "filename": "one.txt",
            "size_bytes": 3,
            "key": "canonical-key",
            "access": "password",
            "password": "guest passphrase",
            "password_hint": "original clue",
        },
    )
    assert response.status_code == 201, response.text
    upload = response.json()["data"]["id"]
    assert (
        await client.put(
            f"/api/v1/uploads/{upload}/content", headers=headers, content=b"one"
        )
    ).status_code == 200
    response = await client.post(f"/api/v1/uploads/{upload}/commit", headers=headers)
    assert response.status_code == 200, response.text
    item = response.json()["data"]
    assert item["id"] == "canonical-key" and item["password_hint"] == "original clue"
    response = await client.put(
        "/api/artifact/canonical-key/content",
        headers=headers,
        files={"file": ("two.txt", b"two", "text/plain")},
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["has_password"]
        and response.json()["password_hint"] == "original clue"
    )
    response = await client.get("/api/v1/publications/canonical-key", headers=headers)
    assert response.json()["data"]["current_revision"] != item["current_revision"]
    response = await client.patch(
        "/api/v1/publications/canonical-key",
        headers=headers,
        json={"password_hint": ""},
    )
    assert (
        response.status_code == 200 and response.json()["data"]["password_hint"] == ""
    )
    for key in ("login", "oauth", "mcp", "downloads", "upload"):
        response = await client.post(
            "/api/v1/uploads",
            headers=headers,
            json={"kind": "file", "filename": "x", "size_bytes": 0, "key": key},
        )
        assert response.status_code == 400 and response.json()["code"] == "reserved_key"


def test_existing_settings_hint_limit_upgrade_preserves_operator_values(core):
    from minder_publish.server.core import Core, dumps

    item = publish(core, title="existing")
    with core.repository.connect(immediate=True) as connection:
        settings = core._settings(connection)
        del settings["limits"]["password_hint_characters"]
        settings["defaults"]["external_ttl_seconds"] = 12345
        connection.execute(
            "UPDATE core_settings SET data=? WHERE id=1", (dumps(settings),)
        )
        saved = core._publication(connection, item["id"])
        del saved["password_hint"]
        core._save_publication(connection, saved)
    restarted = Core(core.config).initialize()
    assert (
        restarted.settings()["limits"]["password_hint_characters"]
        == core.config.limits["password_hint_characters"]
    )
    assert restarted.settings()["defaults"]["external_ttl_seconds"] == 12345
    assert restarted.get_publication(item["id"])["password_hint"] == ""
    assert (
        restarted.patch_publication(item["id"], {"password_hint": "clue"})[
            "password_hint"
        ]
        == "clue"
    )
    with pytest.raises(PublishError) as error:
        restarted.update_settings({"limits": {"password_hint_characters": 1000}})
    assert error.value.code == "operator_setting_required"


def test_import_preserves_reserved_existing_key_and_hint(tmp_path, legacy):
    import sqlite3
    from minder_publish.server.config import Config
    from minder_publish.server.core import Core
    from minder_publish.server.migration import migrate

    database, blobs, password_hash = legacy
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE artifact SET id='login' WHERE id='Legacy7'")
    (blobs / "Legacy7").rename(blobs / "login")
    config = Config(tmp_path / "imported", secret="new-secret-" * 4, testing=True)
    report = migrate(config, database, blobs, apply=True)
    assert "login" in report["imported_ids"]
    item = Core(config).initialize().get_publication("login")
    assert item["password_hash"] == password_hash and item["password_hint"] == "hint"
    assert Core(config).initialize().get_publication("old-site")["password_hint"] == ""
