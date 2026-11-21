"""Content hosts route complete static trees before all control-only surfaces."""

import io
import zipfile
from urllib.parse import urlsplit

import pytest

from test_http import publish, sign_in

ASSETS = {
    "index.html": b'<script src="/static/app.js"></script>',
    "static/app.js": b"window.authorAsset=true;",
    "api/v1/session": b'{"author":"session-fixture"}',
    "api/v1/settings": b'{"author":"settings-fixture"}',
    "api/v1/auth/login": b'{"author":"login-fixture"}',
    "oauth/consent": b"<h1>Author consent page</h1>",
    ".well-known/oauth-protected-resource/mcp": b'{"author":"metadata-fixture"}',
    "mcp/index.html": b"<h1>Author MCP directory</h1>",
    "healthz": b'{"author":"health-fixture"}',
    "login": b"<h1>Author login page</h1>",
    "downloads/fixture.txt": b"Author download",
    "upload/fixture": b"<h1>Author upload page</h1>",
    "p/fixture": b"<h1>Author nested page</h1>",
    "__mpfoo/index.html": b"<h1>Ordinary author directory</h1>",
}


def site_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in ASSETS.items():
            archive.writestr(path, content)
    return output.getvalue()


async def open_site(application, client, *, guest=False):
    material, headers = await publish(
        application,
        client,
        kind="site",
        body=site_bytes(),
        filename="site.zip",
        access="link" if guest else "private",
    )
    owner = await sign_in(client)
    response = await client.get(material["stable_url"] + ("?guest=1" if guest else ""))
    assert response.status_code == 200
    parts = urlsplit(str(response.url))
    return material, headers, owner, parts.scheme + "://" + parts.netloc


@pytest.mark.parametrize("guest", [False, True])
async def test_author_tree_including_control_names_never_enters_control_router(
    application, client, guest
):
    _, _, _, origin = await open_site(application, client, guest=guest)
    for path, expected in ASSETS.items():
        response = await client.get(origin + "/" + path, headers={"Origin": origin})
        assert response.status_code == 200, path
        assert response.content == expected, path
        assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/static/app.js")).content != ASSETS["static/app.js"]
    assert (await client.get("/api/v1/session")).json()["data"]["authenticated"] is True
    assert (await client.get("/healthz")).json()["status"] == "ok"
    assert (await client.get(origin + "/mcp")).content == ASSETS["mcp/index.html"]


async def test_author_assets_keep_head_range_and_conditional_read_guards(
    application, client
):
    _, _, _, origin = await open_site(application, client)
    target = origin + "/static/app.js"
    partial = await client.get(target, headers={"Range": "bytes=0-5"})
    assert partial.status_code == 206 and partial.content == ASSETS["static/app.js"][:6]
    assert (
        partial.headers["content-range"] == f"bytes 0-5/{len(ASSETS['static/app.js'])}"
    )
    head = await client.head(target, headers={"Range": "bytes=0-5"})
    assert head.status_code == 206 and head.content == b""
    assert head.headers["content-length"] == "6"
    client.cookies.clear()
    denied = await client.get(
        target, headers={"Range": "bytes=0-5", "If-None-Match": partial.headers["etag"]}
    )
    assert denied.status_code == 403 and ASSETS["static/app.js"] not in denied.content


async def test_content_requires_its_reader_not_owner_or_bearer_or_other_revision(
    application, client
):
    _, headers, _, origin = await open_site(application, client)
    config = application.state.config
    reader = next(
        cookie.value
        for cookie in client.cookies.jar
        if cookie.name == config.reader_cookie
        and cookie.domain == urlsplit(origin).hostname
    )
    other, _ = await publish(
        application, client, kind="site", body=site_bytes(), filename="site.zip"
    )
    other_origin = config.content_origin(other["current_revision"])
    response = await client.get(
        other_origin + "/api/v1/session",
        headers={"Cookie": config.reader_cookie + "=" + reader},
    )
    assert response.status_code == 403
    # An owner cookie or a valid management bearer alone is never a content grant.
    client.cookies.clear()
    await sign_in(client)
    owner_cookie = next(
        cookie.value
        for cookie in client.cookies.jar
        if cookie.name == config.owner_cookie
    )
    forged_authority = {**headers, "Cookie": config.owner_cookie + "=" + owner_cookie}
    for path in ("/static/app.js", "/api/v1/session", "/healthz"):
        assert (
            await client.get(origin + path, headers=forged_authority)
        ).status_code == 403


@pytest.mark.parametrize(
    "header,value", [("Service-Worker", "script"), ("Sec-Fetch-Dest", "serviceworker")]
)
async def test_service_worker_denial_precedes_content_dispatch(
    application, client, header, value
):
    _, _, _, origin = await open_site(application, client)
    response = await client.get(origin + "/static/app.js", headers={header: value})
    assert (
        response.status_code == 403
        and response.json()["code"] == "service_workers_disabled"
    )


async def test_revision_mutations_rejected_before_body_and_reserved_namespace_never_spa(
    application, client
):
    material, headers, _, origin = await open_site(application, client)
    assert (
        await client.patch(
            "/api/v1/publications/" + material["id"],
            json={"site_routing": "spa"},
            headers=headers,
        )
    ).status_code == 200
    for path in (
        "/__mp",
        "/__mp/unknown",
        "/__mp/authorize",
        "/__mp/continue",
        "/__mp/start/",
    ):
        response = await client.get(origin + path, headers={"Accept": "text/html"})
        assert response.status_code == 404, path
    # A similarly named author directory is not part of the protocol namespace.
    assert (await client.get(origin + "/__mpfoo/")).content == ASSETS[
        "__mpfoo/index.html"
    ]
    read = False

    async def body():
        nonlocal read
        read = True
        raise AssertionError("Revision mutation consumed a request body")
        yield b""

    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        for path in (
            "/api/v1/uploads",
            "/oauth/consent",
            "/static/app.js",
            "/__mp/start",
        ):
            response = await client.request(method, origin + path, content=body())
            assert response.status_code == 404, (method, path)
    assert not read


async def test_content_dispatch_retains_request_header_bounds(application, client):
    _, _, _, origin = await open_site(application, client)
    assert (
        await client.get(
            origin + "/static/app.js", headers={"Content-Encoding": "gzip"}
        )
    ).status_code == 415
    assert (
        await client.get(origin + "/static/app.js", headers={"Content-Length": "65537"})
    ).status_code == 413

    for path in (
        "/mcp",
        "/api/publish",
        "/api/artifact/fixture/content",
        "/api/v1/uploads/" + "a" * 32 + "/content",
    ):
        response = await client.get(origin + path, headers={"Content-Length": "65537"})
        assert response.status_code == 413, path


async def test_content_oserror_is_generic_503_before_headers(
    application, client, monkeypatch
):
    _, _, _, origin = await open_site(application, client)

    def unavailable(*args):
        raise OSError("synthetic-private-path-must-not-appear")

    monkeypatch.setattr(application.state.core, "resolve_file", unavailable)
    response = await client.get(origin + "/static/app.js")
    assert response.status_code == 503
    assert "synthetic-private-path" not in response.text
    assert response.json()["code"] == "storage_unavailable"
