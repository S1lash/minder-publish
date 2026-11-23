"""Owner and guest aggregates follow authenticated one-use document openings."""

import sqlite3
from datetime import timedelta
from importlib.resources import files

import pytest

from minder_publish.server.core import now
from minder_publish.server.repository import Repository
from minder_publish.server.statistics import Statistics
from test_http import publish, sign_in
from test_operations import instance


async def test_owner_preview_clean_history_and_shell_count_once(application, client):
    material, _ = await publish(application, client, access="link")
    stats = Statistics(application.state.core)
    identifier, url = material["id"], material["stable_url"]
    await sign_in(client)
    headers = {"Accept": "text/html", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    shell = await client.get(url, headers=headers)
    assert 'class="owner-viewer"' in shell.text
    assert stats.read(identifier)["owner"]["total"] == 0
    for expected, suffix in enumerate(("?preview=1", "?preview=1", "~1?preview=1"), 1):
        assert (await client.get(url + suffix)).status_code == 200
        result = stats.read(identifier)
        assert result["total"] == 0
        assert result["owner"]["total"] == expected
    client.cookies.set("mp_owner_view", "clean", domain="publish.test", path="/")
    assert (await client.get(url, headers=headers)).status_code == 200
    assert stats.read(identifier)["owner"]["total"] == 4
    await client.get(url + "?guest=1")
    assert stats.read(identifier)["owner"]["total"] == 4
    client.cookies.clear()
    await client.get(url + "?preview=1")
    result = stats.read(identifier)
    assert result["total"] == 1 and result["owner"]["total"] == 4
    assert application.state.core.get_publication(identifier)["view_count"] == 1


async def test_owner_collection_is_independent_and_owner_only(application, client):
    material, bearer = await publish(application, client, access="link")
    identifier, url = material["id"], material["stable_url"]
    path = f"/api/v1/publications/{identifier}"
    core, stats = application.state.core, Statistics(application.state.core)
    assert "owner_open_count" not in (await client.get(path, headers=bearer)).json()["data"]
    owner = await sign_in(client)
    assert (await client.patch(path, headers=owner, json={"guest_counts": False})).status_code == 200
    await client.get(url + "?preview=1")
    assert stats.read(identifier)["owner"]["total"] == 1
    assert (await client.patch(path, headers=owner, json={"owner_counts": False})).status_code == 200
    await client.get(url + "?preview=1")
    assert stats.read(identifier)["owner"]["total"] == 1
    assert not stats.read(identifier)["owner"]["enabled"]
    assert (await client.get(path)).json()["data"]["owner_open_count"] == 1
    assert (await client.get("/api/v1/publications")).json()["data"]["items"][0]["owner_open_count"] == 1
    assert (await client.delete(path + "/statistics", headers=owner)).status_code == 200
    assert stats.read(identifier)["owner"]["total"] == 0
    assert core.get_publication(identifier)["view_count"] == 0


async def test_owner_filters_preserve_one_use_handoff(application, client, monkeypatch):
    material, _ = await publish(application, client, access="link")
    await sign_in(client)
    stats, url = Statistics(application.state.core), material["stable_url"]
    for headers in ({"Purpose": "prefetch"}, {"Range": "bytes=0-3"}, {"If-None-Match": '"synthetic"'}, {"Sec-Fetch-Dest": "script"}, {"User-Agent": "TelegramBot"}):
        await client.get(url + "?preview=1", headers=headers)
    await client.head(url + "?preview=1")
    await client.get(url + "/missing.txt?preview=1")
    assert stats.read(material["id"])["owner"]["total"] == 0
    for cookie in list(client.cookies.jar):
        if cookie.name == application.state.config.reader_cookie:
            client.cookies.delete(cookie.name, domain=cookie.domain, path=cookie.path)
    original, captured = application.state.auth.consume_handoff, []
    def capture(*args):
        captured.append(args)
        return original(*args)
    monkeypatch.setattr(application.state.auth, "consume_handoff", capture)
    await client.get(url + "?preview=1")
    assert len(captured) == 1
    from minder_publish.errors import PublishError
    with pytest.raises(PublishError):
        original(*captured[0])
    await client.get(url + "?preview=1")
    assert stats.read(material["id"])["owner"]["total"] == 2


async def test_owner_daily_limit_retention_and_unavailable_history(application, client):
    material, _ = await publish(application, client, access="link")
    core, stats = application.state.core, Statistics(application.state.core)
    identifier = material["id"]
    core.config.operations["stats_daily_row_limit"] = 1
    await client.get(material["stable_url"])
    await sign_in(client)
    await client.get(material["stable_url"] + "?preview=1")
    result = stats.read(identifier)
    assert result["total"] == 1 and result["owner"]["total"] == 1
    assert result["owner"]["incomplete"]
    assert result["owner"]["daily"][-2]["opens"] is None
    expired = (now().date() - timedelta(days=core.config.operations["stats_retention_days"])).isoformat()
    with core.repository.connect(immediate=True) as connection:
        connection.execute("INSERT INTO publication_owner_statistics_daily VALUES(?,?,?)", (identifier, expired, 8))
    core.sweep()
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM publication_owner_statistics_daily WHERE day=?", (expired,)).fetchone()[0] == 0
    with core.repository.connect(immediate=True) as connection:
        connection.execute("DELETE FROM publication WHERE id=?", (identifier,))
        assert connection.execute("SELECT COUNT(*) FROM publication_owner_statistics").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM publication_owner_statistics_daily").fetchone()[0] == 0


def test_schema_four_downgrade_reupgrade_preserves_material_auth_and_both_counts(tmp_path, monkeypatch):
    path = tmp_path / "schema.sqlite3"
    migrations = files("minder_publish.server").joinpath("migrations/core")
    with sqlite3.connect(path) as connection:
        for version in (1, 2, 3):
            connection.executescript(migrations.joinpath(f"{version:03d}_up.sql").read_text())
        connection.execute("INSERT INTO publication VALUES (?,?)", ("example", '{"id":"example","view_count":23,"guest_counts":false}'))
        connection.execute("INSERT INTO publication_statistics(publication_id,started_at,legacy_total,total) VALUES('example','2026-01-01T00:00:00Z',20,3)")
        connection.execute("CREATE TABLE auth_fixture(id TEXT PRIMARY KEY,credential TEXT)")
        connection.execute("INSERT INTO auth_fixture VALUES('owner','hashed-not-secret')")
    repository = Repository(path)
    repository.initialize()
    with repository.connect(immediate=True) as connection:
        assert connection.execute("SELECT version FROM core_schema_version").fetchone()[0] == 6
        assert tuple(connection.execute("SELECT legacy_total,total FROM publication_statistics").fetchone()) == (20, 3)
        connection.execute("INSERT INTO publication_owner_statistics(publication_id,started_at,total) VALUES('example','2026-11-19T00:00:00Z',7)")
        connection.execute("INSERT INTO publication_owner_statistics_daily VALUES('example','2026-11-19',7)")
    with sqlite3.connect(path) as connection:
        connection.executescript(migrations.joinpath("006_down.sql").read_text())
        connection.executescript(migrations.joinpath("005_down.sql").read_text())
        connection.executescript(migrations.joinpath("004_down.sql").read_text())
    import minder_publish.server.repository as module
    with monkeypatch.context() as context:
        context.setattr(module, "SCHEMA_VERSION", 3)
        Repository(path).initialize()
        with repository.connect(immediate=True) as connection:
            connection.execute("UPDATE publication SET data=json_set(data,'$.title','Changed after downgrade')")
    repository.initialize()
    with repository.connect() as connection:
        assert connection.execute("SELECT json_extract(data,'$.title') FROM publication").fetchone()[0] == "Changed after downgrade"
        assert connection.execute("SELECT credential FROM auth_fixture").fetchone()[0] == "hashed-not-secret"
        assert tuple(connection.execute("SELECT legacy_total,total FROM publication_statistics").fetchone()) == (20, 3)
        assert connection.execute("SELECT total FROM publication_owner_statistics").fetchone()[0] == 7
        assert connection.execute("SELECT opens FROM publication_owner_statistics_daily").fetchone()[0] == 7
        assert connection.execute("SELECT interrupted FROM owner_statistics_collection").fetchone()[0] == 1


def test_offline_downgrade_guards_and_backup_restore(instance, tmp_path):
    from dataclasses import replace
    from minder_publish.errors import PublishError
    from minder_publish.server.auth import AuthStore
    from minder_publish.server.backup import backup, instance_lock, restore
    from minder_publish.server.commands import main
    from minder_publish.server.core import Core
    from test_operations import publish as publish_sync

    config, core, auth = instance
    material = publish_sync(core, access="password", password="owner recovery password")
    identifier = material["id"]
    session, _ = auth.login("owner", "correct-long-password", "127.0.0.1")
    token = auth.create_token("synthetic device", "materials:manage")["token"]
    stats = Statistics(core)
    with core.repository.connect(immediate=True) as connection:
        stats.record(connection, {"publication_id": identifier, "count_guest_opening": True})
        stats.record(connection, {"publication_id": identifier, "count_owner_opening": True})
    archive = tmp_path / "both-counters.tar"
    backup(config, archive)
    target = replace(config, data_dir=tmp_path / "recovered", secret="")
    restore(target, archive)
    restored = Core(target).initialize()
    assert Statistics(restored).read(identifier)["owner"]["total"] == 1
    assert Statistics(restored).read(identifier)["total"] == 1
    assert restored.resolve_file(material["current_revision"], "report.txt")[0].read_bytes() == b"unchanged original bytes"
    restored_auth = AuthStore(target)
    restored_auth.initialize()
    assert restored_auth.session(session) is None and restored_auth.token(token) is None
    assert restored_auth.login("owner", "correct-long-password", "127.0.0.1")[0]

    command = ["--data-dir", str(config.data_dir), "--base-url", "http://publish.localhost:8080", "schema-downgrade", "--target", "3"]
    with instance_lock(config):
        assert main(command) == 1
    assert main(command) == 0
    assert auth.session(session) is not None and auth.token(token) is not None
    assert main(command) == 1
    with pytest.raises(PublishError):
        core.repository.downgrade_owner_statistics(2)
    with core.repository.connect() as connection:
        assert connection.execute("SELECT version FROM core_schema_version").fetchone()[0] == 3
    Core(config).initialize()
    assert stats.read(identifier)["owner"]["total"] == 1
    assert stats.read(identifier)["owner"]["collection_interrupted"]
    stats.clear(identifier, lambda connection: None)
    assert not stats.read(identifier)["owner"]["collection_interrupted"]
    assert auth.session(session) is not None and auth.token(token) is not None


async def test_owner_history_and_password_gate_use_live_owner_identity(application, client):
    from browser_navigation import follow_navigation
    from test_guest_view import csrf_from
    from test_http_security import upload_site

    material, _ = await upload_site(application, client, entry="index.html", html="retained owner document")
    await upload_site(application, client, entry="index.html", html="new current document", publication=material)
    await sign_in(client)
    history = f"/api/v1/publications/{material['id']}/revisions/{material['current_revision']}/open"
    assert (await client.get(history)).text == "retained owner document"
    stats = Statistics(application.state.core)
    assert stats.read(material["id"])["owner"]["total"] == 1
    assert stats.read(material["id"])["total"] == 0

    application.state.core.update_settings({"defaults": {"owner_auto_open": False}})
    protected, _ = await publish(application, client, access="password", password="synthetic gate password")
    gate = await client.get(protected["stable_url"] + "?preview=1")
    response = await client.post(protected["stable_url"] + "?preview=1", data={"csrf": csrf_from(gate), "password": "synthetic gate password"}, headers={"Origin": "https://publish.test"}, follow_redirects=False)
    assert (await follow_navigation(client, response)).status_code == 200
    assert stats.read(protected["id"])["owner"]["total"] == 1
    assert stats.read(protected["id"])["total"] == 0


async def test_owner_defaults_validate_and_new_material_policy_is_independent(application, client):
    core = application.state.core
    core.update_settings({"defaults": {"owner_counts": False, "guest_counts": True}})
    material, bearer = await publish(application, client, access="link")
    assert not material["owner_counts"] and material["guest_counts"]
    await sign_in(client)
    await client.get(material["stable_url"] + "?preview=1")
    assert Statistics(core).read(material["id"])["owner"]["total"] == 0
    from minder_publish.errors import PublishError
    with pytest.raises(PublishError) as error:
        core.update_settings({"defaults": {"owner_counts": 1}})
    assert error.value.code == "invalid_boolean"
    invalid = await client.patch(f"/api/v1/publications/{material['id']}", json={"owner_counts": "true"}, headers=bearer)
    assert invalid.status_code == 400


def test_cli_accepts_independent_owner_collection_flags():
    from minder_publish.client.cli import build_parser
    arguments = build_parser().parse_args(["publish", "example.html", "--kind", "file", "--no-owner-counts"])
    assert arguments.owner_counts is False and arguments.guest_counts is None


async def test_password_unlocked_owner_session_expiring_before_accept_is_not_counted(application, client, monkeypatch):
    from browser_navigation import follow_navigation
    from test_guest_view import csrf_from

    application.state.core.update_settings({"defaults": {"owner_auto_open": False}})
    material, _ = await publish(application, client, access="password", password="synthetic protected bytes")
    await sign_in(client)
    url = material["stable_url"] + "?preview=1"
    gate = await client.get(url)
    response = await client.post(url, data={"csrf": csrf_from(gate), "password": "synthetic protected bytes"}, headers={"Origin": "https://publish.test"}, follow_redirects=False)
    original = application.state.auth.consume_handoff
    def expire_owner_before_accept(*args):
        with application.state.auth.connection(write=True) as connection:
            connection.execute("UPDATE auth_sessions SET expires_at=0")
        return original(*args)
    monkeypatch.setattr(application.state.auth, "consume_handoff", expire_owner_before_accept)
    assert (await follow_navigation(client, response)).status_code == 200
    result = Statistics(application.state.core).read(material["id"])
    assert result["total"] == result["owner"]["total"] == 0
