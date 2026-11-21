"""A guest check has a separate reader origin and never inherits owner/unlock state."""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import zipfile
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import follow_navigation, navigation_target
from test_http import publish, sign_in

from minder_publish.server.core import iso, now

pytest_plugins = ("test_http",)


def named_cookies(client, name):
    return {
        cookie.domain: cookie.value
        for cookie in client.cookies.jar
        if cookie.name == name
    }


def csrf_from(response):
    return re.search('name="csrf" value="([a-f0-9]+)"', response.text).group(1)


async def test_owner_private_check_denies_and_does_not_touch_owner_reader(
    application, client
):
    material, _ = await publish(application, client)
    await sign_in(client)
    config = application.state.config
    opened = await client.get(material["stable_url"])
    assert opened.status_code == 200 and opened.url.host.startswith("r-")
    original_readers = named_cookies(client, config.reader_cookie)
    original_owner = named_cookies(client, config.owner_cookie)
    checked = await client.get(material["stable_url"] + "?guest=1")
    assert checked.status_code == 200 and "Owner sign-in" in checked.text and "Private result" not in checked.text
    assert named_cookies(client, config.reader_cookie) == original_readers
    assert named_cookies(client, config.owner_cookie) == original_owner
    assert (await client.get(opened.url)).status_code == 200
    assert (await client.get("/api/v1/session")).json()["data"]["authenticated"]


async def test_password_check_ignores_owner_prior_normal_unlock_and_prior_probe(
    application, client
):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    config = application.state.config
    gate = await client.get(material["stable_url"])
    normal_guest = await client.post(
        material["stable_url"],
        data={"csrf": csrf_from(gate), "password": "material-password"},
        headers={"Origin": config.control_origin},
    )
    normal_guest = await follow_navigation(client, normal_guest)
    assert normal_guest.status_code == 200
    previous_viewer = named_cookies(client, config.viewer_cookie)
    await sign_in(client)
    normal_owner = await client.get(material["stable_url"])
    assert normal_owner.status_code == 200 and normal_owner.url.host.startswith("r-")
    owner_readers = named_cookies(client, config.reader_cookie)
    checked = await client.get(material["stable_url"] + "?guest=1")
    assert checked.status_code == 200 and 'type="password"' in checked.text
    assert named_cookies(client, config.viewer_cookie) == previous_viewer
    first_probe = named_cookies(client, config.probe_viewer_cookie)
    assert first_probe and first_probe != previous_viewer
    wrong = await client.post(
        material["stable_url"] + "?guest=1",
        data={
            "csrf": application.state.auth.csrf(next(iter(previous_viewer.values()))),
            "password": "material-password",
        },
        headers={"Origin": config.control_origin},
    )
    assert wrong.status_code == 403
    guest_opened = await client.post(
        material["stable_url"] + "?guest=1",
        data={"csrf": csrf_from(checked), "password": "material-password"},
        headers={"Origin": config.control_origin},
    )
    guest_opened = await follow_navigation(client, guest_opened)
    assert guest_opened.status_code == 200 and guest_opened.url.host.startswith("g-")
    assert guest_opened.content == normal_owner.content
    current = named_cookies(client, config.reader_cookie)
    for host, value in owner_readers.items():
        assert current[host] == value
    assert any(host.startswith("g-") for host in current)
    assert (await client.get(normal_owner.url)).status_code == 200
    again = await client.get(material["stable_url"] + "?guest=1")
    assert 'type="password"' in again.text
    assert named_cookies(client, config.probe_viewer_cookie) != first_probe
    assert named_cookies(client, config.viewer_cookie) == previous_viewer
    probe = next(
        cookie
        for cookie in client.cookies.jar
        if cookie.name == config.probe_viewer_cookie
    )
    assert not probe.domain_specified and probe.path == "/" and probe.secure


@pytest.mark.parametrize("guest", [False, True])
async def test_handoff_nonce_and_payload_are_bound_to_r_or_g_mode(
    application, client, guest
):
    material, _ = await publish(application, client, access="link")
    config = application.state.config
    if not guest:
        await sign_in(client)
    start = await client.get(
        material["stable_url"] + ("?guest=1" if guest else ""), follow_redirects=False
    )
    from browser_navigation import opening_target
    start_url = opening_target(start) if guest else start.headers["location"]
    expected = config.content_origin(material["current_revision"], guest=guest)
    other = config.content_origin(material["current_revision"], guest=not guest)
    wrong_start = await client.get(
        start_url.replace(expected, other), follow_redirects=False
    )
    assert wrong_start.status_code == 403
    started = await client.get(start_url, follow_redirects=False)
    authorized = await client.get(started.headers["location"], follow_redirects=False)
    assert authorized.status_code == 303 and authorized.headers["location"].startswith(
        expected
    )
    nonce = next(
        cookie.value
        for cookie in client.cookies.jar
        if cookie.name == config.bridge_cookie
        and cookie.domain == urlsplit(expected).hostname
    )
    wrong_accept = await client.get(
        authorized.headers["location"].replace(expected, other),
        headers={"Cookie": config.bridge_cookie + "=" + nonce},
        follow_redirects=False,
    )
    assert wrong_accept.status_code == 403
    accepted = await client.get(authorized.headers["location"])
    assert accepted.status_code == 200 and str(accepted.url).startswith(expected)
    reader = next(
        cookie.value
        for cookie in client.cookies.jar
        if cookie.name == config.reader_cookie
        and cookie.domain == urlsplit(expected).hostname
    )
    wrong_read = await client.get(
        other + "/", headers={"Cookie": config.reader_cookie + "=" + reader}
    )
    assert wrong_read.status_code == 403


async def test_guest_origin_never_accepts_owner_grant(application, client):
    material, _ = await publish(application, client, access="link")
    await sign_in(client)
    config = application.state.config
    raw = next(iter(named_cookies(client, config.owner_cookie).values()))
    principal = application.state.auth.session(raw)
    intent = application.state.auth.intent(
        {
            "publication_id": material["id"],
            "revision_id": material["current_revision"],
            "access_version": material["access_version"],
            "subject_kind": "owner",
            "subject_id": principal.id,
            "view_mode": "guest",
        }
    )
    denied = await client.get(
        config.content_origin(material["current_revision"], guest=True)
        + "/__mp/start?intent="
        + intent
    )
    assert denied.status_code == 403


async def test_guest_origin_exposes_no_control_endpoints(application, client):
    material, _ = await publish(application, client, access="link")
    origin = application.state.config.content_origin(
        material["current_revision"], guest=True
    )
    for path in (
        "/api/v1/session",
        "/api/v1/settings",
        "/static/app.js",
        "/metrics",
        "/healthz",
        "/__mp/authorize",
    ):
        response = await client.get(origin + path)
        if path.startswith("/__mp/"):
            assert response.status_code == 404
        else:
            # Content-host filenames never enter control handlers. Ordinary
            # author paths require a reader before existence is disclosed.
            assert response.status_code == 403, path
            assert response.json()["code"] == "content_access_required"
    # These are author paths on a revision, never the control MCP transport.
    for path in ("/mcp", "/mcp/messages"):
        response = await client.get(origin + path)
        assert (
            response.status_code == 403 and "www-authenticate" not in response.headers
        )
        for method in ("POST", "OPTIONS"):
            assert (await client.request(method, origin + path)).status_code == 404
    assert (
        await client.post(
            origin + "/p/_preferences",
            headers={"Origin": application.state.config.control_origin},
            data={},
        )
    ).status_code == 404


async def test_probe_preferences_return_keeps_guest_context(application, client):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    await sign_in(client)
    checked = await client.get(material["stable_url"] + "?guest=1")
    path = urlsplit(material["stable_url"]).path + "?guest=1"
    assert 'name="return_to" value="' + path + '"' in checked.text
    saved = await client.post(
        "/p/_preferences",
        data={"return_to": path, "theme": "dark", "language": "fr"},
        headers={"Origin": application.state.config.control_origin},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].endswith("?guest=1")
    again = await client.get(saved.headers["location"])
    assert 'type="password"' in again.text and 'lang="fr"' in again.text
    invalid = await client.post(
        "/p/_preferences",
        data={
            "return_to": path + "&next=https://evil.test",
            "theme": "dark",
            "language": "fr",
        },
        headers={"Origin": application.state.config.control_origin},
    )
    assert invalid.status_code == 400


@pytest.mark.parametrize("guest", [False, True])
async def test_native_password_and_preference_forms_preserve_strict_origin_checks(
    application, client, guest
):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    config = application.state.config
    url = material["stable_url"] + ("?guest=1" if guest else "")
    gate = await client.get(url)
    # Native form POSTs use navigate mode: no-referrer would serialize Origin as null.
    assert gate.headers.get_list("referrer-policy") == ["same-origin"]
    form = {"csrf": csrf_from(gate), "password": "material-password"}
    for origin in ("null", "https://attacker.example"):
        denied = await client.post(url, data=form, headers={"Origin": origin})
        assert denied.status_code == 403 and denied.json()["code"] == "csrf_rejected"
    assert (
        await client.post(
            url,
            data={"password": "material-password"},
            headers={"Origin": config.control_origin},
        )
    ).status_code == 403
    retry = await client.post(
        url,
        data={**form, "password": "incorrect"},
        headers={"Origin": config.control_origin},
    )
    assert retry.status_code == 401
    assert retry.headers.get_list("referrer-policy") == ["same-origin"]
    preferences = {
        "return_to": urlsplit(url).path + ("?guest=1" if guest else ""),
        "theme": "dark",
        "language": "fr",
    }
    assert (
        await client.post(
            "/p/_preferences", data=preferences, headers={"Origin": "null"}
        )
    ).status_code == 403
    saved = await client.post(
        "/p/_preferences",
        data=preferences,
        headers={"Origin": config.control_origin},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["referrer-policy"] == "no-referrer"
    opened = await client.post(
        url,
        data={"csrf": csrf_from(retry), "password": "material-password"},
        headers={"Origin": config.control_origin},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert opened.headers["referrer-policy"] == "no-referrer"
    target = await navigation_target(client, opened)
    content = await client.get(target)
    assert (
        content.status_code == 200
        and content.headers["referrer-policy"] == "no-referrer"
    )


async def site(application, client, body=b"first", publication=None):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("guide/app.html", body)
        archive.writestr("guide/sub/index.html", b"subpage")
    payload = content.getvalue()
    token = application.state.auth.create_token("site-test", "materials:manage")[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    plan = {
        "kind": "site",
        "filename": "site.zip",
        "size_bytes": len(payload),
        "access": "link",
        "entry_path": "guide/app.html",
    }
    if publication:
        plan.update(
            publication_id=publication["id"],
            expected_revision=publication["current_revision"],
        )
    created = await client.post("/api/v1/uploads", json=plan, headers=headers)
    identifier = created.json()["data"]["id"]
    assert (
        await client.put(
            f"/api/v1/uploads/{identifier}/content", content=payload, headers=headers
        )
    ).status_code == 200
    committed = await client.post(
        f"/api/v1/uploads/{identifier}/commit", headers=headers
    )
    assert committed.status_code == 200, committed.text
    return committed.json()["data"]


async def test_guest_canonical_and_retired_redirects_keep_mode_and_application_query(
    application, client
):
    material = await site(application, client)
    config = application.state.config
    opened = await client.get(material["stable_url"] + "?guest=1&theme=red")
    origin = config.content_origin(material["current_revision"], guest=True)
    assert str(opened.url) == origin + "/guide/app.html?theme=red"
    directory = await client.get(
        origin + "/guide/sub?view=compact", follow_redirects=False
    )
    assert directory.headers["location"] == origin + "/guide/sub/?view=compact"
    assert (await client.get(directory.headers["location"])).content == b"subpage"
    newer = await site(application, client, b"second", material)
    with application.state.core.repository.connect(immediate=True) as connection:
        connection.execute(
            "UPDATE revision SET drain_until=? WHERE id=?",
            (iso(now() - timedelta(seconds=1)), material["current_revision"]),
        )
    retired = await client.get(
        origin + "/guide/app.html?view=compact",
        headers={"Sec-Fetch-Mode": "navigate"},
        follow_redirects=False,
    )
    assert retired.status_code == 303
    assert parse_qs(urlsplit(retired.headers["location"]).query) == {
        "view": ["compact"],
        "guest": ["1"],
    }
    assert urlsplit(retired.headers["location"]).path.endswith("~1/guide/app.html")
    followed = await client.get(retired.headers["location"])
    assert followed.status_code in (403, 404, 410)
    assert b"second" not in followed.content


async def test_request_actor_is_public_and_auth_database_errors_fail_closed(
    application, client, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="minder_publish.http")
    material, headers = await publish(application, client)
    token_id = next(
        token["id"]
        for token in application.state.auth.list_tokens()
        if token["name"] == "test"
    )
    await client.get("/api/v1/publications", headers=headers)
    await sign_in(client)
    await client.get("/api/v1/session")
    raw = next(
        iter(named_cookies(client, application.state.config.owner_cookie).values())
    )
    owner_hash = application.state.auth.session(raw).id
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "minder_publish.http"
    ]
    assert any(row["actor"] == "device:" + token_id for row in records)
    assert any(row["actor"] == "owner" for row in records)
    assert (
        raw not in str(records)
        and owner_hash not in str(records)
        and headers["Authorization"] not in str(records)
    )

    def unavailable(*args):
        raise sqlite3.OperationalError("secret-path-and-query")

    monkeypatch.setattr(application.state.auth, "session", unavailable)
    denied = await client.get("/api/v1/settings")
    assert denied.status_code == 503 and denied.json()["code"] == "storage_unavailable"
    assert "secret-path-and-query" not in denied.text
