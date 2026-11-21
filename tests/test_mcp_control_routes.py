"""Canonical MCP endpoints stay separate from revision author paths."""

import asyncio
import io
import json
import re
import zipfile

import httpx
import pytest

pytest.importorskip("mcp")

from test_http import publish, sign_in
from test_oauth import code, exchange, register, tokens

from minder_publish.server.app import create_app
from minder_publish.server.config import Config


@pytest.fixture
async def remote_application(tmp_path):
    app = create_app(
        Config(
            tmp_path,
            base_url="https://publish.test",
            secret="test-only-secret-" * 4,
            testing=True,
            remote_mcp_enabled=True,
            legacy_hosts=("legacy.test",),
        )
    )
    startup = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def lifespan():
        try:
            async with app.router.lifespan_context(app):
                app.state.auth.bootstrap("owner", "a long test passphrase")
                startup.set_result(None)
                await stop.wait()
        except BaseException as error:
            if not startup.done():
                startup.set_exception(error)
            raise

    task = asyncio.create_task(lifespan())
    await startup
    try:
        yield app
    finally:
        stop.set()
        await task


@pytest.fixture
async def remote_browser(remote_application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=remote_application),
        base_url="https://publish.test",
        follow_redirects=False,
    ) as browser:
        yield browser


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_canonical_mcp_authentication_precedes_body(remote_browser, path):
    received = False

    async def body():
        nonlocal received
        received = True
        yield b"not valid JSON"

    response = await remote_browser.post(path + "?transport=fixture", content=body())
    assert response.status_code == 401
    assert "location" not in response.headers
    assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]
    assert not received


async def test_challenge_scope_authorized_by_owner_can_publish(remote_application, remote_browser):
    challenge = await remote_browser.post("/mcp", content=b"not parsed")
    assert challenge.status_code == 401
    match = re.search(r'scope="([^"]+)"', challenge.headers["www-authenticate"])
    assert match is not None
    recommended_scope = match.group(1)
    assert recommended_scope == "materials:manage"

    await sign_in(remote_browser)
    owner_raw = next(
        cookie.value
        for cookie in remote_browser.cookies.jar
        if cookie.name == remote_application.state.config.owner_cookie
    )
    oauth = (remote_application.state.oauth, remote_application, owner_raw)
    registered = await register(remote_browser)
    authorization_code = await code(
        remote_browser, oauth, registered, scope=recommended_scope
    )
    issued = await remote_browser.post(
        "/oauth/token", data=exchange(registered, authorization_code)
    )
    assert issued.status_code == 200
    headers = {
        "Authorization": "Bearer " + issued.json()["access_token"],
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    response = await remote_browser.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "publish_content",
                "arguments": {"content": "Synthetic publishing acceptance", "filename": "acceptance.txt"},
            },
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["stable_url"].startswith("https://publish.test/p/")


@pytest.mark.parametrize("credential", ["device", "oauth"])
async def test_both_mcp_forms_call_real_tools_without_redirect(
    remote_application, remote_browser, credential
):
    material, _ = await publish(remote_application, remote_browser)
    if credential == "device":
        raw = remote_application.state.auth.create_token("test-mcp", "materials:read")[
            "token"
        ]
    else:
        await sign_in(remote_browser)
        owner_raw = next(
            cookie.value
            for cookie in remote_browser.cookies.jar
            if cookie.name == remote_application.state.config.owner_cookie
        )
        _, issued = await tokens(
            remote_browser,
            (remote_application.state.oauth, remote_application, owner_raw),
        )
        raw = issued["access_token"]
    headers = {
        "Authorization": "Bearer " + raw,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    for path in ("/mcp", "/mcp/"):
        initialized = await remote_browser.post(
            path,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "route-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert "result" in initialized.json() and "location" not in initialized.headers
        called = await remote_browser.post(
            path + "?transport=fixture",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_artifact", "arguments": {"id": material["id"]}},
            },
        )
        assert called.status_code == 200, called.text
        assert not called.json()["result"].get("isError")
        payload = json.loads(called.json()["result"]["content"][0]["text"])
        assert payload["id"] == material["id"]
        assert "location" not in called.headers


@pytest.mark.parametrize("directory", [False, True])
async def test_revision_mcp_author_file_and_directory_keep_content_authorization(
    remote_application, remote_browser, directory
):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as site:
        site.writestr("index.html", "<h1>Private site</h1>")
        site.writestr("mcp/index.html" if directory else "mcp", "Author MCP path")
    material, _ = await publish(
        remote_application,
        remote_browser,
        kind="site",
        filename="site.zip",
        body=archive.getvalue(),
    )
    await sign_in(remote_browser)
    opened = await remote_browser.get(material["stable_url"], follow_redirects=True)
    assert opened.status_code == 200
    origin = remote_application.state.config.content_origin(
        material["current_revision"]
    )
    path = "/mcp/" if directory else "/mcp"
    response = await remote_browser.get(origin + path, follow_redirects=True)
    assert response.status_code == 200 and response.text == "Author MCP path"
    assert "www-authenticate" not in response.headers
    head = await remote_browser.head(origin + path, follow_redirects=True)
    assert head.status_code == 200 and head.content == b""
    if directory:
        direct = await remote_browser.get(origin + "/mcp/index.html")
        assert direct.status_code == 200 and direct.text == "Author MCP path"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=remote_application)
    ) as anonymous:
        assert (await anonymous.get(origin + path)).status_code == 403
    for method in ("POST", "OPTIONS"):
        denied = await remote_browser.request(method, origin + path)
        assert denied.status_code == 404
        assert "www-authenticate" not in denied.headers
    for host, expected in (("legacy.test", 404), ("untrusted.test", 421)):
        for path in ("/mcp", "/mcp/"):
            denied = await remote_browser.post("https://" + host + path)
            assert denied.status_code == expected
            assert "www-authenticate" not in denied.headers


async def test_mcp_browser_handoff_accepts_file_above_inline_limit(
    remote_application, remote_browser
):
    owner = await sign_in(remote_browser)
    raw = remote_application.state.auth.create_token("test-mcp", "materials:manage")[
        "token"
    ]
    mcp_headers = {
        "Authorization": "Bearer " + raw,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    async def call(name, arguments):
        response = await remote_browser.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    job = await call("create_file_upload", {"kind": "file", "title": "Binary fixture"})
    assert job["upload_url"] == f"https://publish.test/upload/{job['id']}"
    assert (await remote_browser.get(job["upload_url"])).status_code == 200

    body = bytes(range(256)) * 8192
    start = await remote_browser.post(
        f"/api/v1/upload-jobs/{job['id']}/start",
        headers=owner,
        json={"kind": "file", "filename": "fixture.bin", "size_bytes": len(body)},
    )
    assert start.status_code == 201, start.text
    upload_url = f"/api/v1/uploads/{start.json()['data']['id']}"
    received = await remote_browser.put(upload_url + "/content", headers=owner, content=body)
    assert received.status_code == 200, received.text
    inspected = await remote_browser.post(upload_url + "/inspect", headers=owner, json={})
    assert inspected.status_code == 200, inspected.text
    committed = await remote_browser.post(upload_url + "/commit", headers=owner, json={})
    assert committed.status_code == 200, committed.text

    material = committed.json()["data"]
    status = await call("get_file_upload", {"id": job["id"]})
    assert status["state"] == "published"
    assert status["stable_url"] == material["stable_url"]
    assert status["stable_url"].startswith("https://publish.test/p/")
    downloaded = await remote_browser.get(
        f"/api/v1/publications/{material['id']}/download", headers=owner
    )
    assert downloaded.status_code == 200
    assert downloaded.content == body

    lower_limit = len(body) - 1
    settings = await remote_browser.patch(
        "/api/v1/settings", headers=owner, json={"limits": {"file_bytes": lower_limit}}
    )
    assert settings.status_code == 200, settings.text
    next_job = await call("create_file_upload", {"kind": "file"})
    plan = {"kind": "file", "filename": "fixture.bin", "size_bytes": len(body)}
    rejected_handoff = await remote_browser.post(
        f"/api/v1/upload-jobs/{next_job['id']}/start", headers=owner, json=plan
    )
    rejected_api = await remote_browser.post("/api/v1/uploads", headers=owner, json=plan)
    assert rejected_handoff.status_code == rejected_api.status_code == 413
    assert rejected_handoff.json()["code"] == rejected_api.json()["code"]
    assert rejected_handoff.json()["field"] == rejected_api.json()["field"] == "size_bytes"
