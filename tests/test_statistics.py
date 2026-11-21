"""Guest opening statistics follow the real access and handoff paths."""

import time
import sqlite3
import io
import zipfile
from datetime import timedelta
from importlib.resources import files

import pytest

from minder_publish.server.core import now
from minder_publish.server.statistics import Statistics
from minder_publish.server.repository import Repository
from minder_publish.errors import PublishError
from browser_navigation import follow_navigation
from test_guest_view import csrf_from
from test_http import publish, sign_in


async def test_guest_opening_filters_and_missing_target(application, client):
    material, _ = await publish(application, client, access="link")
    stats = Statistics(application.state.core)
    identifier = material["id"]
    url = material["stable_url"]
    initial = await client.get(url, headers={"User-Agent": "UnknownPreviewBot/1.0"}, follow_redirects=False)
    from browser_navigation import opening_target
    assert initial.status_code == 200 and opening_target(initial)
    assert stats.read(identifier)["total"] == 0
    assert (await client.get(url + "/missing.txt")).status_code == 404
    assert (await client.head(url)).status_code in (200, 303)
    assert (await client.get(url, headers={"User-Agent": "TelegramBot"})).status_code == 200
    assert (await client.get(url, headers={"Purpose": "prefetch"})).status_code == 200
    assert (await client.get(url + "?guest=1")).status_code == 200
    assert stats.read(identifier)["total"] == 0
    assert (await client.get(url)).status_code == 200
    assert stats.read(identifier)["total"] == 1
    await sign_in(client)
    assert (await client.get(url)).status_code == 200
    assert stats.read(identifier)["total"] == 1


async def test_range_conditional_and_site_resource_handoffs_do_not_count(application, client):
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("index.html", "<h1>Entry</h1>")
        archive.writestr("app.js", "console.log('synthetic')")
    material, _ = await publish(application, client, access="link", kind="site", filename="site.zip", body=body.getvalue())
    url = material["stable_url"]
    resource = await client.get(url + "/app.js", headers={"Sec-Fetch-Dest": "script"})
    assert resource.status_code == 200 and "synthetic" in resource.text
    ranged = await client.get(url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b"<h1>"
    await client.get(url, headers={"If-None-Match": '"not-a-match"'})
    assert Statistics(application.state.core).read(material["id"])["total"] == 0


async def test_statistics_owner_only_periods_and_recent_reset(application, client):
    material, bearer = await publish(application, client, access="link")
    path = f"/api/v1/publications/{material['id']}/statistics"
    await client.get(material["stable_url"])
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=bearer)).status_code == 403
    headers = await sign_in(client)
    for days in (7, 30, 90):
        result = await client.get(path, params={"days": days})
        assert result.status_code == 200
        data = result.json()["data"]
        assert len(data["daily"]) == days and data["period_total"] == 1
    assert (await client.get(path, params={"days": 8})).status_code == 400
    auth = application.state.auth
    with auth.connection(write=True) as connection:
        connection.execute("UPDATE auth_sessions SET authenticated_at=?", (
            time.time() - application.state.config.operations["owner_recent_auth_seconds"] - 1,
        ))
    assert (await client.delete(path, headers=headers)).status_code == 403
    assert (await client.post("/api/v1/auth/reauth", headers=headers, json={"password": "a long test passphrase"})).status_code == 200
    assert (await client.delete(path, headers=headers)).status_code == 200
    data = (await client.get(path)).json()["data"]
    assert data["total"] == data["legacy_total"] == data["period_total"] == 0


async def test_owner_password_gate_does_not_count_as_guest(application, client):
    application.state.core.update_settings({"defaults": {"owner_auto_open": False}})
    material, _ = await publish(application, client, access="password", password="material-password")
    await sign_in(client)
    gate = await client.get(material["stable_url"])
    assert 'type="password"' in gate.text
    response = await client.post(
        material["stable_url"], data={"csrf": csrf_from(gate), "password": "material-password"},
        headers={"Origin": "https://publish.test"}, follow_redirects=False,
    )
    assert (await follow_navigation(client, response)).status_code == 200
    assert Statistics(application.state.core).read(material["id"])["total"] == 0


async def test_password_unlock_counts_guest_but_not_guest_probe(application, client):
    material, _ = await publish(application, client, access="password", password="material-password")
    stats = Statistics(application.state.core)
    for suffix, expected in (("", 1), ("?guest=1", 1)):
        client.cookies.clear()
        gate = await client.get(material["stable_url"] + suffix)
        response = await client.post(
            material["stable_url"] + suffix,
            data={"csrf": csrf_from(gate), "password": "material-password"},
            headers={"Origin": "https://publish.test"}, follow_redirects=False,
        )
        assert (await follow_navigation(client, response)).status_code == 200
        assert stats.read(material["id"])["total"] == expected


async def test_replayed_handoff_cannot_increment_again(application, client, monkeypatch):
    material, _ = await publish(application, client, access="link")
    auth = application.state.auth
    original = auth.consume_handoff
    captured = []

    def capture(*args):
        captured.append(args)
        return original(*args)

    monkeypatch.setattr(auth, "consume_handoff", capture)
    assert (await client.get(material["stable_url"])).status_code == 200
    assert len(captured) == 1
    with pytest.raises(PublishError):
        original(*captured[0])
    assert Statistics(application.state.core).read(material["id"])["total"] == 1


async def test_daily_retention_capacity_and_cascade(application, client):
    material, _ = await publish(application, client, access="link")
    core = application.state.core
    identifier = material["id"]
    stats = Statistics(core)
    core.config.operations["stats_daily_row_limit"] = 1
    old_day = (now().date() - timedelta(days=core.config.operations["stats_retention_days"])).isoformat()
    with core.repository.connect(immediate=True) as connection:
        connection.execute("INSERT INTO publication_statistics_daily VALUES(?,?,?)", (identifier, old_day, 3))
        stats.record(connection, {"publication_id": identifier, "count_guest_opening": True})
    assert stats.read(identifier)["period_total"] == 1
    other, _ = await publish(application, client, access="link")
    with core.repository.connect(immediate=True) as connection:
        stats.record(connection, {"publication_id": other["id"], "count_guest_opening": True})
    assert stats.read(other["id"])["total"] == 1
    assert stats.read(other["id"])["incomplete"] is True
    with core.repository.connect(immediate=True) as connection:
        connection.execute("DELETE FROM publication WHERE id=?", (identifier,))
        assert connection.execute("SELECT COUNT(*) FROM publication_statistics_daily WHERE publication_id=?", (identifier,)).fetchone()[0] == 0


def test_migration_preserves_legacy_total_without_inventing_daily_counts(tmp_path):
    path = tmp_path / "old.sqlite3"
    migrations = files("minder_publish.server").joinpath("migrations/core")
    with sqlite3.connect(path) as connection:
        connection.executescript(migrations.joinpath("001_up.sql").read_text())
        connection.executescript(migrations.joinpath("002_up.sql").read_text())
        connection.execute("INSERT INTO publication VALUES (?,?)", ("example", '{"id":"example","view_count":23}'))
    repository = Repository(path)
    repository.initialize()
    with repository.connect() as connection:
        row = connection.execute("SELECT legacy_total,total FROM publication_statistics WHERE publication_id='example'").fetchone()
        assert tuple(row) == (23, 0)
        assert connection.execute("SELECT COUNT(*) FROM publication_statistics_daily").fetchone()[0] == 0
