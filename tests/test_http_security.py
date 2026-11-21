"""Independent policy regressions through the real application and SQLite boundary."""

import io
import re
import zipfile

import pytest
from test_http import publish, sign_in

from minder_publish.errors import PublishError
from minder_publish.server.http import Admission


async def upload_site(
    application, client, *, entry, html, publication=None, routing="spa", supports_link_names=False
):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(entry, html)
        archive.writestr("assets/app.js", html.replace("html", "javascript"))
    body = stream.getvalue()
    token = application.state.auth.create_token("site-test", "materials:manage")[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    plan = {
        "kind": "site",
        "filename": "bundle.zip",
        "size_bytes": len(body),
        "entry_path": entry,
    }
    if publication:
        plan.update(
            publication_id=publication["id"],
            expected_revision=publication["current_revision"],
        )
    else:
        plan.update(access="link", site_routing=routing)
    if supports_link_names:
        plan["supports_link_names"] = True
    response = await client.post("/api/v1/uploads", headers=headers, json=plan)
    assert response.status_code == 201, response.text
    upload = response.json()["data"]["id"]
    response = await client.put(
        f"/api/v1/uploads/{upload}/content", headers=headers, content=body
    )
    assert response.status_code == 200, response.text
    response = await client.post(f"/api/v1/uploads/{upload}/commit", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"], headers


async def test_duplicate_host_and_authorization_headers_are_rejected(
    application, client
):
    response = await client.get(
        "/api/v1/session", headers=[("Host", "evil.test"), ("Host", "publish.test")]
    )
    assert response.status_code == 400
    token = application.state.auth.create_token("test", "materials:read")["token"]
    response = await client.get(
        "/api/v1/publications",
        headers=[
            ("Authorization", "Bearer invalid"),
            ("Authorization", f"Bearer {token}"),
        ],
    )
    assert response.status_code in (400, 401)


@pytest.mark.parametrize("hint", ["x" * 201, "😀" * 202])
async def test_oversize_legacy_hint_is_rejected_before_publication_creation(
    application, client, hint
):
    token = application.state.auth.create_token("legacy", "materials:manage")["token"]
    response = await client.post(
        "/api/publish",
        headers={"Authorization": "Bearer " + token},
        data={"hint": hint, "password": "a guest passphrase", "ttl": "7d"},
        files={"file": ("report.html", b"new html", "text/html")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "text_limit"
    assert response.json()["field"] == "password_hint"
    with application.state.core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM publication").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM upload").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM external_reservation").fetchone()[
                0
            ]
            == 0
        )


async def test_oversize_legacy_hint_patch_or_replacement_does_not_mutate_existing_material(
    application, client
):
    material, headers = await publish(application, client, access="link")
    response = await client.patch(
        f"/api/artifact/{material['id']}",
        headers=headers,
        data={"hint": "x" * 201, "title": "must not change", "ttl": "30d"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "text_limit"
    response = await client.put(
        f"/api/artifact/{material['id']}/content",
        headers=headers,
        data={"hint": "x" * 201},
        files={"file": ("replacement.html", b"replacement", "text/html")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "text_limit"
    current = (
        await client.get(f"/api/v1/publications/{material['id']}", headers=headers)
    ).json()["data"]
    for field in (
        "title",
        "expires_at",
        "current_revision",
        "access_version",
        "updated_at",
    ):
        assert current[field] == material[field]


async def test_password_gate_shares_bounded_argon_admission(
    application, client, monkeypatch
):
    material, _ = await publish(
        application, client, access="password", password="a guest passphrase"
    )
    gate = await client.get(material["stable_url"])
    csrf = re.search('name="csrf" value="([a-f0-9]+)"', gate.text).group(1)
    auth = application.state.auth
    assert auth.password_slots.acquire(blocking=False)
    assert auth.password_slots.acquire(blocking=False)

    class SpyHasher:
        def verify(self, *args):
            pytest.fail("password work started despite exhausted admission")

    monkeypatch.setattr(auth, "hasher", SpyHasher())
    try:
        response = await client.post(
            material["stable_url"],
            data={"csrf": csrf, "password": "wrong"},
            headers={"Origin": "https://publish.test"},
        )
        assert response.status_code == 429
    finally:
        auth.password_slots.release()
        auth.password_slots.release()


async def test_self_only_csp_restricts_forms_and_guest_counts_exclude_assets(
    application, client
):
    material, headers = await publish(application, client, access="link")
    response = await client.patch(
        f"/api/v1/publications/{material['id']}",
        headers=headers,
        json={"external_resources": "self_only"},
    )
    assert response.status_code == 200
    opened = await client.get(material["stable_url"])
    csp = opened.headers["content-security-policy"]
    form = next(
        (
            value.strip()
            for value in csp.split(";")
            if value.strip().startswith("form-action ")
        ),
        "",
    )
    assert form == "form-action 'self'"
    before = (
        await client.get(f"/api/v1/publications/{material['id']}", headers=headers)
    ).json()["data"]["view_count"]
    assert before == 1
    await client.get(opened.url, headers={"Range": "bytes=0-2"})
    after = (
        await client.get(f"/api/v1/publications/{material['id']}", headers=headers)
    ).json()["data"]["view_count"]
    assert after == before
    await sign_in(client)
    await client.get(material["stable_url"])
    assert (
        await client.get(f"/api/v1/publications/{material['id']}", headers=headers)
    ).json()["data"]["view_count"] == before


async def test_retired_spa_navigation_uses_its_own_revision_entry(application, client):
    original, _ = await upload_site(
        application, client, entry="old-start.html", html="old html"
    )
    opened = await client.get(original["stable_url"])
    old_origin = application.state.config.content_origin(original["current_revision"])
    assert opened.text == "old html"
    await upload_site(
        application,
        client,
        entry="new-start.html",
        html="new html",
        publication=original,
    )
    old_script = await client.get(old_origin + "/assets/app.js")
    assert old_script.text == "old javascript"
    old_route = await client.get(
        old_origin + "/some-route", headers={"Accept": "text/html"}
    )
    assert old_route.status_code == 200 and old_route.text == "old html"


async def test_retired_navigation_after_drain_redirects_but_asset_fails(
    application, client
):
    original, _ = await upload_site(
        application, client, entry="index.html", html="old html"
    )
    await client.get(original["stable_url"])
    await upload_site(
        application, client, entry="index.html", html="new html", publication=original
    )
    with application.state.core.repository.connect(immediate=True) as connection:
        connection.execute(
            "UPDATE revision SET drain_until='2000-01-01T00:00:00+00:00' WHERE id=?",
            (original["current_revision"],),
        )
    old_origin = application.state.config.content_origin(original["current_revision"])
    navigation = await client.get(
        old_origin + "/",
        headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"},
        follow_redirects=False,
    )
    assert navigation.status_code in (302, 303, 307)
    assert navigation.headers["location"].startswith(original["stable_url"])
    asset = await client.get(
        old_origin + "/assets/app.js", headers={"Accept": "*/*"}, follow_redirects=False
    )
    assert asset.status_code in (403, 404, 410)


async def test_every_conditional_head_and_range_rechecks_policy(application, client):
    material, headers = await publish(application, client, access="link")
    opened = await client.get(material["stable_url"])
    etag = opened.headers["etag"]
    assert (await client.head(opened.url)).status_code == 200
    await client.post(
        f"/api/v1/publications/{material['id']}/pause",
        headers=headers,
        json={"paused": True},
    )
    for method, request_headers in [
        ("GET", {"If-None-Match": etag}),
        ("GET", {"Range": "bytes=0-2"}),
        ("HEAD", {}),
    ]:
        response = await client.request(method, opened.url, headers=request_headers)
        assert response.status_code == 403
    with application.state.core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM read_lease WHERE kind='read'"
            ).fetchone()[0]
            == 0
        )


async def test_security_cookies_are_host_only_and_preference_handoff_is_allowlisted(
    application, client
):
    material, _ = await publish(application, client)
    login = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://publish.test"},
        json={"username": "owner", "password": "a long test passphrase"},
    )
    cookies = login.headers.get_list("set-cookie")
    owner_cookie = next(
        value for value in cookies if value.startswith("__Host-mp-owner=")
    )
    for part in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"):
        assert part.casefold() in owner_cookie.casefold()
    assert "domain=" not in owner_cookie.casefold()
    client.cookies.set("mp_lang", "not-a-language", domain="publish.test", path="/")
    client.cookies.set("mp_theme", "not-a-theme", domain="publish.test", path="/")
    opened = await client.get(material["stable_url"])
    preference_cookies = [
        cookie
        for response in opened.history
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(("mp_lang=", "mp_theme="))
    ]
    assert any(cookie.startswith("mp_lang=en;") for cookie in preference_cookies)
    assert any(cookie.startswith("mp_theme=system;") for cookie in preference_cookies)
    assert all(
        "domain=" not in cookie.casefold() and "samesite=lax" in cookie.casefold()
        for cookie in preference_cookies
    )


async def test_guest_password_system_chrome_has_language_picker_and_localized_labels(
    application, client
):
    material, _ = await publish(
        application, client, access="password", password="a guest passphrase"
    )
    client.cookies.set("mp_lang", "fr", domain="publish.test", path="/")
    response = await client.get(material["stable_url"])
    assert 'name="language"' in response.text
    assert "Mot de passe" in response.text
    assert (
        ">Open<" not in response.text and 'aria-label="Password"' not in response.text
    )


async def test_midstream_failure_never_starts_a_second_http_response(application):
    messages = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )
        raise PublishError("lease_expired", 410)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "path": "/",
        "method": "GET",
        "scheme": "https",
        "headers": [(b"host", b"publish.test")],
        "query_string": b"",
        "state": {},
    }
    boundary = Admission(inner, application.state.config, application.state.auth)
    try:
        await boundary(scope, receive, send)
    except PublishError:
        pass
    assert (
        len(
            [
                message
                for message in messages
                if message["type"] == "http.response.start"
            ]
        )
        == 1
    )


async def test_expire_racing_with_upload_never_resurrects_pre_expiry_guest_grant(
    application, client, monkeypatch
):
    material, headers = await publish(application, client, access="link")
    opened = await client.get(material["stable_url"])
    core = application.state.core
    planned = {}
    original_patch = core.patch_publication

    def plan_between_expiry_and_revocation(identifier, changes):
        result = original_patch(identifier, changes)
        if identifier == material["id"] and "expiry" in changes and not planned:
            planned["upload"] = core.create_upload(
                {
                    "kind": "file",
                    "filename": "new.txt",
                    "size_bytes": 3,
                    "publication_id": identifier,
                    "expected_revision": result["current_revision"],
                },
                "race-subject",
            )
        return result

    monkeypatch.setattr(core, "patch_publication", plan_between_expiry_and_revocation)
    response = await client.post(
        f"/api/v1/publications/{material['id']}/expire", headers=headers
    )
    assert response.status_code == 200
    expired = response.json()["data"]
    if not planned:
        # An atomic expiry has no intermediate patch boundary. Plan a normal later update instead.
        planned["upload"] = core.create_upload(
            {
                "kind": "file",
                "filename": "new.txt",
                "size_bytes": 3,
                "publication_id": material["id"],
                "expected_revision": expired["current_revision"],
            },
            "race-subject",
        )

    async def body():
        yield b"new"

    await core.receive_upload(planned["upload"]["id"], body(), "race-subject")
    try:
        updated = core.commit_upload(planned["upload"]["id"], "race-subject")
    except PublishError as error:
        assert (
            error.status == 409
        )  # A stale policy snapshot may be rejected explicitly.
    else:
        assert updated["access_version"] >= expired["access_version"]
    extended = await client.post(
        f"/api/v1/publications/{material['id']}/extend",
        headers=headers,
        json={"seconds": 86400},
    )
    assert extended.status_code == 200
    assert (await client.get(opened.url)).status_code == 403
