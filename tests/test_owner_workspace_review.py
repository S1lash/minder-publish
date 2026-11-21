"""Boundary regressions for the owner workspace and guest-only counters."""

from datetime import timedelta

from minder_publish.server.core import now
from test_http import publish, sign_in


async def test_daily_retention_runs_without_guest_traffic(application, client):
    material, _ = await publish(application, client, access="link")
    core = application.state.core
    expired_day = (now().date() - timedelta(days=core.config.operations["stats_retention_days"])).isoformat()
    with core.repository.connect(immediate=True) as connection:
        connection.execute("INSERT INTO publication_statistics_daily VALUES(?,?,?)", (material["id"], expired_day, 7))
    core.sweep()
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM publication_statistics_daily WHERE day=?", (expired_day,)).fetchone()[0] == 0


async def test_owner_list_and_detail_report_only_new_guest_openings(application, client):
    material, bearer = await publish(application, client, access="link")
    core = application.state.core
    with core.repository.connect(immediate=True) as connection:
        value = core._publication(connection, material["id"])
        value["view_count"] = 500
        core._save_publication(connection, value)
    await client.get(material["stable_url"])
    path = f"/api/v1/publications/{material['id']}"
    assert "guest_open_count" not in (await client.get(path, headers=bearer)).json()["data"]
    await sign_in(client)
    assert (await client.get(path)).json()["data"]["guest_open_count"] == 1
    assert (await client.get("/api/v1/publications")).json()["data"]["items"][0]["guest_open_count"] == 1


async def test_owner_root_document_opens_workspace_without_changing_author_routes(application, client):
    material, _ = await publish(application, client, access="link")
    await sign_in(client)
    headers = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Accept": "text/html"}
    async def fetch(suffix=""):
        return await client.get(material["stable_url"] + suffix, headers=headers, follow_redirects=False)

    def shell(response):
        return response.status_code == 200 and 'class="owner-viewer"' in response.text

    assert shell(await fetch())
    assert shell(await fetch("?preview=tools"))
    for suffix in ("?preview=1", "?guest=1", "?page=author-query", "/nested.html"):
        assert not shell(await fetch(suffix))
    legacy = await client.get("/viewer/" + material["id"], headers=headers, follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == material["stable_url"] + "?preview=tools"
    client.cookies.set("mp_owner_view", "clean", domain="publish.test", path="/")
    assert not shell(await fetch())
    assert shell(await fetch("?preview=tools"))
    client.cookies.set("mp_owner_view", "invalid", domain="publish.test", path="/")
    assert shell(await fetch())
    client.cookies.clear()
    assert not shell(await fetch())
    assert not shell(await fetch("?preview=tools"))
    crawler = await client.get(material["stable_url"], headers={"User-Agent": "TelegramBot"}, follow_redirects=False)
    assert not shell(crawler) and "owner-viewer" not in crawler.text
