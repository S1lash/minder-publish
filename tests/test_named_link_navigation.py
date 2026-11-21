"""Named addresses route to the same material, access policy and owner workspace."""

from test_guest_view import csrf_from
from test_http import publish, sign_in
from test_http_security import upload_site
from test_link_previews import Tags


async def rename(client, material, name, headers, *, keep_previous=False):
    response = await client.patch(
        f"/api/v1/publications/{material['id']}/link",
        json={"name": name, "expected_name": material["link_name"], "keep_previous": keep_previous},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_renamed_site_opens_current_bytes_at_both_names_but_pinned_version_stays_fixed(application, client):
    old, _ = await upload_site(application, client, entry="index.html", html="previous revision")
    owner = await sign_in(client)
    sharing = f"/api/v1/publications/{old['id']}/revisions/{old['current_revision']}/sharing"
    shared = await client.post(sharing, json={"enabled": True}, headers=owner)
    assert shared.status_code == 200, shared.text
    pinned_url = shared.json()["data"]["version_url"]
    first = await rename(client, old, "release-notes", owner)
    await upload_site(
        application, client, entry="index.html", html="current revision", publication=old,
        supports_link_names=True,
    )
    latest = await rename(client, first, "release-notes-final", owner, keep_previous=True)
    assert latest["id"] == old["id"]

    legacy = await client.get(f"/viewer/{old['id']}", follow_redirects=False)
    assert legacy.headers["location"] == latest["stable_url"] + "?preview=tools"
    owner_view = await client.get(first["stable_url"], headers={
        "Accept": "text/html", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"
    }, follow_redirects=False)
    assert f'data-publication-id="{old["id"]}"' in owner_view.text
    client.cookies.clear()

    for link in (first["stable_url"], latest["stable_url"]):
        assert (await client.get(link)).text == "current revision"
    assert (await client.get(pinned_url)).text == "previous revision"
    inactive = await client.get(old["stable_url"], headers={"Accept": "text/html"}, follow_redirects=False)
    assert inactive.status_code == 404
    assert "This publication is unavailable." in inactive.text
    assert "release-notes-final" not in inactive.text


async def test_kept_alias_reuses_password_gate_and_guest_counts(application, client):
    original, _ = await publish(application, client, access="password", password="a guest passphrase")
    owner = await sign_in(client)
    renamed = await rename(client, original, "named-report", owner, keep_previous=True)
    client.cookies.clear()

    crawler = {"User-Agent": "TelegramBot"}
    preview = await client.get(original["stable_url"], headers=crawler, follow_redirects=False)
    tags = Tags(preview.text).meta
    assert tags["og:url"] == original["stable_url"]
    assert tags["og:title"] == "Something worth opening."
    assert "named-report" not in preview.text and "Searchable description" not in preview.text

    gate = await client.get(original["stable_url"])
    assert 'type="password"' in gate.text
    await client.post(original["stable_url"], headers={"Origin": "https://publish.test"}, data={
        "csrf": csrf_from(gate), "password": "a guest passphrase",
    })
    assert (await client.get(renamed["stable_url"])).text == "<h1>Private result</h1>"
    detail_path = f"/api/v1/publications/{original['id']}"
    owner = await sign_in(client)
    assert (await client.get(detail_path)).json()["data"]["guest_open_count"] >= 1
    disabled = await client.delete(f"{detail_path}/link/{original['link_name']}", headers=owner)
    assert disabled.status_code == 200, disabled.text
    assert (await client.get(original["stable_url"], headers=crawler)).status_code == 404
