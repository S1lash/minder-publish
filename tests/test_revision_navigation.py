"""A raw revision URL can find its control gate without becoming read authority."""

import io
import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import follow_navigation
from test_guest_view import csrf_from
from test_http import publish, sign_in

NAVIGATION = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Accept": "text/html"}


@pytest.mark.parametrize("guest", [False, True])
async def test_raw_revision_document_uses_stable_gate_before_fresh_handoff(
    application, client, guest
):
    material, _ = await publish(application, client, access="link")
    config = application.state.config
    origin = config.content_origin(material["current_revision"], guest=guest)
    response = await client.get(origin, headers=NAVIGATION, follow_redirects=False)
    expected = material["stable_url"] + "~1" + ("?guest=1" if guest else "")
    assert response.status_code == 303
    assert response.headers["location"] == expected
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert not response.headers.get_list("set-cookie")
    assert b"Private result" not in response.content
    with application.state.auth.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_readers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_intents").fetchone()[0] == 0
    opened = await client.get(expected)
    assert opened.status_code in (403, 404, 410)
    assert b"Private result" not in opened.content
    assert application.state.core.get_publication(material["id"])["view_count"] == 0


@pytest.mark.parametrize("guest", [False, True])
async def test_raw_revision_password_navigation_preserves_path_query_and_probe_realm(
    application, client, guest
):
    path = "guide/résumé page.html"
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("index.html", "<h1>Entry</h1>")
        archive.writestr(path, "<h1>Nested protected result</h1>")
    material, _ = await publish(
        application, client, kind="site", filename="site.zip", body=body.getvalue(),
        access="password", password="synthetic material password",
    )
    config = application.state.config
    owner_headers = await sign_in(client)
    share = f"/api/v1/publications/{material['id']}/revisions/{material['current_revision']}/sharing"
    assert (await client.post(share, json={"enabled": True}, headers=owner_headers)).status_code == 200
    client.cookies.clear()
    origin = config.content_origin(material["current_revision"], guest=guest)
    if guest:
        await sign_in(client)
    response = await client.get(
        origin + "/guide/r%C3%A9sum%C3%A9%20page.html?mode=compact&label=a%26b&label=&guest=1",
        headers=NAVIGATION, follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert urlsplit(location).path == urlsplit(material["stable_url"]).path + "~1/guide/r%C3%A9sum%C3%A9%20page.html"
    assert parse_qs(urlsplit(location).query, keep_blank_values=True) == {
        "mode": ["compact"], "label": ["a&b", ""], **({"guest": ["1"]} if guest else {}),
    }
    gate = await client.get(location)
    assert gate.status_code == 200 and 'type="password"' in gate.text
    assert "Nested protected result" not in gate.text
    response = await client.post(
        location, data={"csrf": csrf_from(gate), "password": "synthetic material password"},
        headers={"Origin": config.control_origin}, follow_redirects=False,
    )
    opened = await follow_navigation(client, response)
    assert opened.content == b"<h1>Nested protected result</h1>"
    assert opened.url.host == urlsplit(origin).hostname
    assert parse_qs(opened.url.query.decode(), keep_blank_values=True) == {
        "mode": ["compact"], "label": ["a&b", ""],
    }


async def test_raw_private_document_does_not_treat_owner_cookie_or_bearer_as_reader(
    application, client
):
    material, bearer = await publish(application, client)
    config = application.state.config
    origin = config.content_origin(material["current_revision"])
    private = await client.get(origin, headers=NAVIGATION, follow_redirects=False)
    gate = await client.get(private.headers["location"])
    assert gate.status_code in (403, 404, 410) and "Private result" not in gate.text
    assert "Private result" not in gate.text
    await sign_in(client)
    owner_cookie = next(cookie.value for cookie in client.cookies.jar if cookie.name == config.owner_cookie)
    forged = await client.get(
        origin, headers={**NAVIGATION, **bearer, "Cookie": config.owner_cookie + "=" + owner_cookie},
        follow_redirects=False,
    )
    assert forged.status_code == 303 and not forged.headers.get_list("set-cookie")
    assert "Private result" not in forged.text
    opened = await client.get(forged.headers["location"])
    assert opened.content == b"<h1>Private result</h1>"


@pytest.mark.parametrize("request_headers", [
    {}, {"Accept": "application/json"}, {"Accept": "*/*"},
    {"Accept": "text/html", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    {"Accept": "text/html", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"},
    {**NAVIGATION, "Range": "bytes=0-2"},
    {**NAVIGATION, "If-None-Match": '"fixture"'},
    {**NAVIGATION, "If-Modified-Since": "Tue, 01 Jan 2000 00:00:00 GMT"},
    {"Accept": "text/html;q=0"},
])
async def test_missing_reader_resource_fetch_and_conditional_requests_stay_denied(
    application, client, request_headers
):
    material, _ = await publish(application, client, access="link")
    origin = application.state.config.content_origin(material["current_revision"])
    for method in ("GET", "HEAD"):
        response = await client.request(method, origin + "/index.html", headers=request_headers, follow_redirects=False)
        assert response.status_code == 403 and "location" not in response.headers
        assert "Private result" not in response.text


@pytest.mark.parametrize("request_headers", [
    {"Accept": "text/html"},
    {"Accept": "application/xhtml+xml"},
    {"Sec-Fetch-Mode": "navigate"},
    {"Sec-Fetch-Dest": "iframe"},
])
async def test_document_metadata_and_legacy_html_accept_can_find_control_gate(
    application, client, request_headers
):
    material, _ = await publish(application, client, access="link")
    origin = application.state.config.content_origin(material["current_revision"])
    response = await client.get(origin, headers=request_headers, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == material["stable_url"] + "~1"


async def test_unknown_revision_and_reserved_protocol_are_not_navigation_shortcuts(
    application, client
):
    config = application.state.config
    unknown = config.content_origin("a" * 32)
    response = await client.get(unknown, headers=NAVIGATION, follow_redirects=False)
    assert response.status_code == 403 and "location" not in response.headers
    material, _ = await publish(application, client, access="link")
    origin = config.content_origin(material["current_revision"])
    for path in ("/__mp", "/__mp/unknown", "/__mp/authorize", "/__mp/continue"):
        response = await client.get(origin + path, headers=NAVIGATION, follow_redirects=False)
        assert response.status_code == 404 and "location" not in response.headers


async def test_revoked_reader_document_returns_to_password_gate_but_asset_stays_denied(
    application, client
):
    material, headers = await publish(
        application, client, access="password", password="previous password"
    )
    gate = await client.get(material["stable_url"])
    response = await client.post(
        material["stable_url"], data={"csrf": csrf_from(gate), "password": "previous password"},
        headers={"Origin": application.state.config.control_origin}, follow_redirects=False,
    )
    opened = await follow_navigation(client, response)
    assert opened.content == b"<h1>Private result</h1>"
    await client.patch(
        "/api/v1/publications/" + material["id"], headers=headers,
        json={"password": "different password"},
    )
    raw = application.state.config.content_origin(material["current_revision"])
    asset = await client.get(raw + "/index.html", follow_redirects=False)
    assert asset.status_code == 403
    navigation = await client.get(raw + "/index.html", headers=NAVIGATION, follow_redirects=False)
    assert navigation.status_code == 303
    assert navigation.headers["location"] == material["stable_url"] + "~1/index.html"
    new_gate = await client.get(navigation.headers["location"])
    assert new_gate.status_code in (403, 404, 410)
    assert "Private result" not in new_gate.text


@pytest.mark.parametrize("agent", ["TelegramBot (like TwitterBot)", "Slackbot-LinkExpanding 1.0"])
async def test_raw_revision_crawler_finds_control_metadata_without_a_reader(
    application, client, agent
):
    material, _ = await publish(application, client, access="password", password="not for crawler")
    origin = application.state.config.content_origin(material["current_revision"])
    response = await client.get(origin, headers={"User-Agent": agent}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == material["stable_url"] + "~1"
    teaser = await client.get(response.headers["location"], headers={"User-Agent": agent})
    assert teaser.status_code in (403, 404, 410)
    assert "A result" not in teaser.text and "Searchable description" not in teaser.text
    assert "Private result" not in teaser.text and "not for crawler" not in teaser.text
    conditional = await client.get(origin, headers={"User-Agent": agent, "Range": "bytes=0-2"}, follow_redirects=False)
    assert conditional.status_code == 403 and "location" not in conditional.headers


async def test_raw_retired_document_without_reader_never_opens_current_revision(application, client):
    from test_http_security import upload_site

    previous, _ = await upload_site(application, client, entry="index.html", html="old html")
    current, _ = await upload_site(application, client, entry="index.html", html="current html", publication=previous)
    client.cookies.clear()
    origin = application.state.config.content_origin(previous["current_revision"])
    response = await client.get(origin, headers=NAVIGATION, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == previous["stable_url"] + "~1"
    opened = await client.get(response.headers["location"])
    assert opened.status_code in (403, 404, 410)
    assert b"old html" not in opened.content and b"current html" not in opened.content


async def test_open_throttle_precedes_lookup_for_unknown_closed_and_crawler_links(
    application, client, monkeypatch
):
    public, _ = await publish(application, client, access="link")
    private, _ = await publish(application, client)
    expired, _ = await publish(application, client, access="link")
    application.state.core.expire(expired["id"])
    calls = []
    original_throttle = application.state.auth.throttle
    original_lookup = application.state.core.get_linked_publication

    def throttle(bucket, *, policy, namespace):
        def observe_policy(connection):
            assert connection.in_transaction
            limits = policy(connection)
            calls.append(("throttle", bucket, limits, namespace))
            return limits

        return original_throttle(bucket, policy=observe_policy, namespace=namespace)

    def lookup(identifier, *args, **kwargs):
        assert calls and calls[-1][0] == "throttle"
        calls.append(("lookup", identifier))
        return original_lookup(identifier, *args, **kwargs)

    monkeypatch.setattr(application.state.auth, "throttle", throttle)
    monkeypatch.setattr(application.state.core, "get_linked_publication", lookup)
    for material, expected in ((public, 200), (private, 200), (expired, 410), ({"stable_url": "/p/unknown-material"}, 404)):
        calls.clear()
        response = await client.get(material["stable_url"], follow_redirects=False)
        assert response.status_code == expected
        assert calls[0] == ("throttle", "open:127.0.0.1", (300, 60), "publication")
        assert calls[1][0] == "lookup"
    calls.clear()
    crawler = await client.get(private["stable_url"], headers={"User-Agent": "TelegramBot"})
    assert crawler.status_code == 200
    assert calls[0] == ("throttle", "open:127.0.0.1", (300, 60), "publication")
    assert calls[1][0] == "lookup"


async def test_raw_revision_document_head_redirects_without_content_or_grants(application, client):
    material, _ = await publish(application, client, access="password", password="not from HEAD")
    config = application.state.config
    origin = config.content_origin(material["current_revision"])
    response = await client.head(origin, headers=NAVIGATION, follow_redirects=False)
    assert response.status_code == 303 and response.content == b""
    assert response.headers["location"] == material["stable_url"] + "~1"
    assert not response.headers.get_list("set-cookie")
    with application.state.auth.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_readers").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM auth_intents").fetchone()[0] == 0
