"""HTTP policy tests use real SQLite and files through the ASGI boundary."""

import io
import zipfile

from browser_navigation import follow_navigation


async def publish(
    application,
    client,
    *,
    access="private",
    password=None,
    kind="file",
    body=b"<h1>Private result</h1>",
    filename="index.html",
):
    token = application.state.auth.create_token("test", "materials:manage")["token"]
    headers = {"Authorization": "Bearer " + token}
    plan = {
        "kind": kind,
        "filename": filename,
        "size_bytes": len(body),
        "title": "A result",
        "summary": "Searchable description",
        "access": access,
    }
    if password:
        plan["password"] = password
    response = await client.post("/api/v1/uploads", json=plan, headers=headers)
    assert response.status_code == 201, response.text
    identifier = response.json()["data"]["id"]
    response = await client.put(
        f"/api/v1/uploads/{identifier}/content", content=body, headers=headers
    )
    assert response.status_code == 200, response.text
    response = await client.post(
        f"/api/v1/uploads/{identifier}/commit", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"], headers


async def sign_in(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "owner", "password": "a long test passphrase"},
        headers={"Origin": "https://publish.test"},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": "https://publish.test",
        "X-CSRF-Token": response.json()["data"]["csrf_token"],
    }


async def test_real_publish_search_download_and_device_scope(application, client):
    material, headers = await publish(application, client)
    assert "password_hash" not in str(material) and "defaults_snapshot" not in material
    result = await client.get("/api/v1/publications?q=DESCRIPTION", headers=headers)
    assert result.json()["data"]["items"][0]["id"] == material["id"]
    result = await client.get(
        f"/api/v1/publications/{material['id']}/download", headers=headers
    )
    assert result.content == b"<h1>Private result</h1>"
    assert result.headers["content-disposition"].startswith("attachment")
    assert (await client.get("/api/v1/settings", headers=headers)).status_code == 403
    read = application.state.auth.create_token("read", "materials:read")["token"]
    assert (
        await client.post(
            "/api/v1/uploads",
            content=b"not parsed",
            headers={"Authorization": "Bearer " + read},
        )
    ).status_code == 403
    assert (
        await client.get(
            "/api/v1/capabilities", headers={"Authorization": "Bearer " + read}
        )
    ).status_code == 200


async def test_authentication_precedes_body_and_csrf_is_session_bound(
    application, client
):
    assert (
        await client.post("/api/v1/uploads", content=b"bad json")
    ).status_code == 401
    headers = await sign_in(client)
    assert (
        await client.patch("/api/v1/settings", json={"defaults": {"language": "ru"}})
    ).status_code == 403
    bad = {**headers, "X-CSRF-Token": "wrong"}
    assert (
        await client.patch("/api/v1/settings", json={}, headers=bad)
    ).status_code == 403
    assert (
        await client.patch(
            "/api/v1/settings", json={"defaults": {"language": "ru"}}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.post("/api/v1/auth/logout", headers=headers)
    ).status_code == 200
    assert (await client.get("/api/v1/settings")).status_code == 401


async def test_owner_opens_private_content_on_isolated_origin_and_logout_revokes(
    application, client
):
    material, _ = await publish(application, client)
    denied = await client.get(material["stable_url"])
    assert denied.status_code == 200 and "Owner sign-in" in denied.text and "Private result" not in denied.text
    headers = await sign_in(client)
    opened = await client.get(material["stable_url"])
    assert opened.status_code == 200, opened.text
    assert opened.url.host.startswith("r-") and opened.url.host.endswith(
        ".publish.test"
    )
    assert opened.content == b"<h1>Private result</h1>"
    assert (
        await client.get(str(opened.url).rstrip("/") + "/api/v1/session")
    ).status_code == 404
    ranged = await client.get(opened.url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b"<h1>"
    await client.post("/api/v1/auth/logout", headers=headers)
    assert (
        await client.get(opened.url, headers={"Range": "bytes=0-3"})
    ).status_code == 403


async def test_link_guest_handoff_service_workers_and_policy_revocation(
    application, client
):
    material, headers = await publish(application, client, access="link")
    direct = application.state.config.content_origin(material["current_revision"])
    assert (await client.get(direct)).status_code == 403
    opened = await client.get(material["stable_url"])
    assert opened.status_code == 200, opened.text
    assert (
        await client.get(opened.url, headers={"Service-Worker": "script"})
    ).status_code == 403
    assert (
        await client.get(opened.url, headers={"Sec-Fetch-Dest": "serviceworker"})
    ).status_code == 403
    await client.post(
        f"/api/v1/publications/{material['id']}/pause",
        json={"paused": True},
        headers=headers,
    )
    assert (await client.get(opened.url)).status_code == 403


async def test_password_unlock_reuses_guest_session_but_not_other_material(
    application, client
):
    first, _ = await publish(
        application, client, access="password", password="a guest passphrase"
    )
    second, _ = await publish(
        application, client, access="password", password="another passphrase"
    )
    gate = await client.get(first["stable_url"])
    assert gate.status_code == 200 and 'type="password"' in gate.text
    import re

    csrf = re.search('name="csrf" value="([a-f0-9]+)"', gate.text).group(1)
    rejected = await client.post(
        first["stable_url"],
        data={"csrf": csrf, "password": "wrong material passphrase"},
        headers={"Origin": "https://publish.test"},
    )
    assert rejected.status_code == 401
    assert "<h1>Password-protected</h1>" in rejected.text
    assert 'aria-describedby="password-error" aria-invalid="true" autofocus' in rejected.text
    assert "Incorrect material password. Try again." in rejected.text
    assert "wrong material passphrase" not in rejected.text
    opened = await client.post(
        first["stable_url"],
        data={"csrf": csrf, "password": "a guest passphrase"},
        headers={"Origin": "https://publish.test"},
    )
    opened = await follow_navigation(client, opened)
    assert (
        opened.status_code == 200 and opened.content == b"<h1>Private result</h1>"
    ), opened.text
    assert (await client.get(first["stable_url"])).content == opened.content
    assert 'type="password"' in (await client.get(second["stable_url"])).text


async def test_content_response_does_not_hold_leases_after_range_error(
    application, client
):
    material, _ = await publish(application, client, access="link")
    opened = await client.get(material["stable_url"])
    result = await client.get(opened.url, headers={"Range": "bytes=99999-"})
    assert result.status_code == 416
    with application.state.core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM read_lease WHERE kind='read'"
            ).fetchone()[0]
            == 0
        )


async def test_host_isolation_unknown_host_and_oversized_metadata(application, client):
    assert (await client.get("https://evil.test/")).status_code == 421
    headers = await sign_in(client)
    assert (
        await client.patch("/api/v1/settings", content=b"x" * 65537, headers=headers)
    ).status_code == 413
    assert (await client.get("/")).status_code == 200
    assert b"/static/app.js" in (await client.get("/")).content


async def test_static_site_export_preserves_manifest_paths(application, client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "<h1>A site</h1>")
        archive.writestr("assets/style.css", "body{color:blue}")
    material, headers = await publish(
        application,
        client,
        kind="site",
        filename="site.zip",
        body=buffer.getvalue(),
        access="link",
    )
    opened = await client.get(material["stable_url"])
    assert opened.text == "<h1>A site</h1>"
    assert (
        await client.get(str(opened.url) + "assets/style.css")
    ).text == "body{color:blue}"
    downloaded = await client.get(
        f"/api/v1/publications/{material['id']}/download", headers=headers
    )
    assert downloaded.status_code == 200, downloaded.text
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert set(archive.namelist()) == {"index.html", "assets/style.css"}
        assert archive.read("assets/style.css") == b"body{color:blue}"
    with application.state.core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM read_lease WHERE kind='export'"
            ).fetchone()[0]
            == 0
        )


async def test_legacy_client_publish_custom_key_and_sparse_update(application, client):
    token = application.state.auth.create_token("legacy client", "materials:manage")[
        "token"
    ]
    headers = {"Authorization": "Bearer " + token}
    result = await client.post(
        "/api/publish",
        headers=headers,
        data={
            "key": "my-report",
            "title": "Old CLI",
            "password": "a guest passphrase",
            "ttl": "7d",
        },
        files={"file": ("report.html", b"old html", "text/html")},
    )
    assert result.status_code == 201, result.text
    before = result.json()
    assert before["id"] == "my-report" and before["has_password"]
    changed = await client.put(
        "/api/artifact/my-report/content",
        headers=headers,
        files={"file": ("report.html", b"new html", "text/html")},
    )
    assert changed.status_code == 200, changed.text
    assert (
        changed.json()["expires_at"] == before["expires_at"]
        and changed.json()["has_password"]
    )
    assert changed.json()["title"] == "Old CLI"
    assert (await client.get("/my-report", follow_redirects=False)).headers[
        "location"
    ] == "https://publish.test/p/my-report"
    with application.state.core.repository.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM external_reservation").fetchone()[
                0
            ]
            == 0
        )


async def test_metrics_require_credentials_and_reveal_no_material_names(
    application, client
):
    assert (await client.get("/metrics")).status_code == 401
    material, headers = await publish(application, client)
    response = await client.get("/metrics", headers=headers)
    assert (
        response.status_code == 200 and "minder_publish_publications 1" in response.text
    )
    assert (
        material["title"] not in response.text and material["id"] not in response.text
    )


async def test_directory_index_and_spa_paths_open_through_stable_entry(
    application, client
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "home")
        archive.writestr("about/index.html", "about")
    material, headers = await publish(
        application,
        client,
        kind="site",
        filename="site.zip",
        body=buffer.getvalue(),
        access="link",
    )
    result = await client.get(material["stable_url"] + "/about/")
    assert result.status_code == 200 and result.text == "about", result.text
    assert (await client.get(material["stable_url"] + "/about")).text == "about"
    await client.patch(
        f"/api/v1/publications/{material['id']}",
        json={"site_routing": "spa"},
        headers=headers,
    )
    result = await client.get(
        material["stable_url"] + "/dashboard/profile", headers={"Accept": "text/html"}
    )
    assert result.status_code == 200 and result.text == "home", result.text
    assert (
        await client.get(material["stable_url"] + "/assets/missing.js")
    ).status_code == 404


async def test_large_site_reads_without_loading_the_full_manifest(application, client, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "home")
        archive.writestr("about/index.html", "about")
        for index in range(1000):
            archive.writestr(f"assets/theme-{index}.css", "body { color: black; }")
    material, headers = await publish(
        application,
        client,
        kind="site",
        filename="site.zip",
        body=buffer.getvalue(),
        access="link",
    )
    updated = await client.patch(
        f"/api/v1/publications/{material['id']}",
        json={"site_routing": "spa"},
        headers=headers,
    )
    assert updated.status_code == 200
    opened = await client.get(material["stable_url"])
    assert opened.status_code == 200 and opened.text == "home"

    core = application.state.core
    original_revision = core._revision

    def revision_without_manifest(connection, revision_id, *, manifest=True):
        assert not manifest, "Published reads should resolve exact paths"
        return original_revision(connection, revision_id, manifest=manifest)

    monkeypatch.setattr(core, "_revision", revision_without_manifest)
    origin = application.state.config.content_origin(material["current_revision"])
    asset = await client.get(origin + "/assets/theme-999.css", headers={"Range": "bytes=0-3"})
    assert asset.status_code == 206 and asset.content == b"body"
    index = await client.get(origin + "/about?section=team", follow_redirects=False)
    assert index.status_code == 303
    assert index.headers["location"] == origin + "/about/?section=team"
    assert (await client.get(index.headers["location"])).text == "about"
    page = await client.get(origin + "/dashboard/profile", headers={"Accept": "text/html"})
    assert page.status_code == 200 and page.text == "home"
    missing = await client.get(origin + "/assets/unknown.css", headers={"Accept": "text/html"})
    assert missing.status_code == 404


async def test_download_first_policy_controls_file_opening(application, client):
    material, headers = await publish(application, client, access="link")
    await client.patch(
        f"/api/v1/publications/{material['id']}",
        json={"preview": "download"},
        headers=headers,
    )
    result = await client.get(material["stable_url"])
    assert result.status_code == 200
    assert result.headers["content-disposition"].startswith("attachment")
    assert result.content == b"<h1>Private result</h1>"


async def test_retired_site_keeps_its_routing_when_replaced_with_a_file(
    application, client
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "old site")
    material, headers = await publish(
        application,
        client,
        kind="site",
        filename="site.zip",
        body=buffer.getvalue(),
        access="link",
    )
    await client.patch(
        f"/api/v1/publications/{material['id']}",
        json={"site_routing": "spa"},
        headers=headers,
    )
    opened = await client.get(material["stable_url"])
    plan = {
        "kind": "file",
        "filename": "new.txt",
        "size_bytes": 3,
        "publication_id": material["id"],
        "expected_revision": material["current_revision"],
    }
    upload = (await client.post("/api/v1/uploads", json=plan, headers=headers)).json()[
        "data"
    ]["id"]
    await client.put(
        f"/api/v1/uploads/{upload}/content", content=b"new", headers=headers
    )
    committed = await client.post(f"/api/v1/uploads/{upload}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    pinned = await client.get(
        str(opened.url) + "dashboard", headers={"Accept": "text/html"}
    )
    assert pinned.status_code == 200 and pinned.text == "old site", pinned.text
    assert (await client.get(material["stable_url"])).content == b"new"


async def test_health_fails_closed_without_creating_a_missing_database(
    application, client
):
    database = application.state.config.db_path
    retained = database.with_suffix(".retained")
    assert (await client.get("/healthz")).status_code == 200
    database.rename(retained)
    try:
        response = await client.get("/healthz")
        assert response.status_code == 503 and response.json()["status"] == "degraded"
        assert not database.exists()
    finally:
        retained.rename(database)
    assert (await client.get("/healthz")).status_code == 200


async def test_public_home_and_owner_login_shell_share_control_policy(client):
    home = await client.get("/")
    login = await client.get("/login")
    assert home.status_code == login.status_code == 200
    assert (
        home.headers["content-security-policy"]
        == login.headers["content-security-policy"]
    )
    assert "frame-ancestors 'none'" in login.headers["content-security-policy"]
    assert "no-store" in login.headers["cache-control"]
    assert (await client.get("/api/v1/publications")).status_code == 401
    assert (await client.get("/api/v1/settings")).status_code == 401


async def test_private_publication_sign_in_link_opens_owner_login(application, client):
    material, _ = await publish(application, client)
    denied = await client.get(material["stable_url"])
    assert denied.status_code == 200 and "Private result" not in denied.text
    from urllib.parse import quote
    login_url = "https://publish.test/login?return_to=" + quote("/p/" + material["id"], safe="")
    assert f'href="{login_url}"' in denied.text
    login = await client.get(login_url)
    assert login.status_code == 200
    assert 'data-surface="public"' in login.text


async def test_owner_login_route_preserves_an_author_site_login_path(
    application, client
):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("index.html", "Author site home")
        output.writestr("login/index.html", "Author login page")
    material, _ = await publish(
        application,
        client,
        access="link",
        kind="site",
        body=archive.getvalue(),
        filename="site.zip",
    )
    opened = await client.get(material["stable_url"])
    login = await client.get(str(opened.url).rstrip("/") + "/login")
    assert login.status_code == 200 and login.text == "Author login page"
    assert login.url.path == "/login/"
    assert "Minder Publish" not in login.text


async def test_owner_login_shell_is_not_served_on_a_legacy_content_host(
    application, client
):
    application.state.config.legacy_hosts = ("old.publish.test",)
    result = await client.get("https://old.publish.test/login", follow_redirects=False)
    assert result.status_code == 303
    assert result.headers["location"] == "https://publish.test/p/login"
