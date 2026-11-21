import asyncio
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import LATEST_PROTOCOL_VERSION

from minder_publish.mcp import server as module


class FakeClient:
    calls = []
    tokens = []

    def __init__(self, base=None, token=None):
        self.token = token
        if token is not None:
            self.tokens.append(token)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def publish(self, path, **kwargs):
        assert Path(path).exists()
        self.calls.append(("publish", Path(path), Path(path).read_text(), kwargs))
        return {"id": "one", "stable_url": "https://example.test/p/one"}

    def patch(self, id, changes):
        self.calls.append(("patch", id, changes))
        return {"id": id}

    def get(self, id):
        self.calls.append(("get", id, self.token))
        return {"id": id, "stable_url": f"https://example.test/p/{id}"}


@pytest.fixture(autouse=True)
def reset():
    FakeClient.calls = []
    FakeClient.tokens = []


@pytest.mark.asyncio
async def test_remote_registry_has_no_local_filesystem_tools():
    local = module.create_mcp(local=True, client_factory=FakeClient)
    remote = module.create_mcp(local=False, client_factory=FakeClient)
    assert {"publish_file", "update_file"} <= {
        tool.name for tool in await local.list_tools()
    }
    names = {tool.name for tool in await remote.list_tools()}
    assert not {"publish_file", "update_file"} & names
    assert {
        "publish_content",
        "list_artifacts",
        "get_artifact",
        "set_meta",
        "update_content",
        "extend_artifact",
        "delete_artifact",
    } <= names


@pytest.mark.asyncio
async def test_inline_content_bound_path_validation_and_temp_cleanup():
    server = module.create_mcp(client_factory=FakeClient)
    await server.call_tool(
        "publish_content", {"content": "hello", "summary": "", "access": "private"}
    )
    _, path, content, options = FakeClient.calls[0]
    assert content == "hello" and not path.exists()
    assert options["metadata"] == {"summary": "", "access": "private"}
    for filename in ("../x.txt", "..", "folder\\secret.txt", "C:secret.txt"):
        with pytest.raises(ToolError):
            await server.call_tool(
                "publish_content", {"content": "x", "filename": filename}
            )
    with pytest.raises(ToolError):
        await server.call_tool(
            "publish_content", {"content": "x" * (module.MAX_INLINE_BYTES + 1)}
        )
    assert len(FakeClient.calls) == 1


@pytest.mark.asyncio
async def test_summary_omission_empty_and_null_are_distinct():
    server = module.create_mcp(client_factory=FakeClient)
    await server.call_tool("set_meta", {"id": "one", "title": "new"})
    await server.call_tool("set_meta", {"id": "one", "summary": ""})
    assert FakeClient.calls == [
        ("patch", "one", {"title": "new"}),
        ("patch", "one", {"summary": ""}),
    ]
    with pytest.raises(ToolError):
        await server.call_tool("set_meta", {"id": "one", "summary": None})
    assert len(FakeClient.calls) == 2


@pytest.mark.asyncio
async def test_clear_password_requires_explicit_access():
    server = module.create_mcp(client_factory=FakeClient)
    with pytest.raises(ToolError):
        await server.call_tool("set_meta", {"id": "one", "clear_password": True})
    assert not FakeClient.calls


@pytest.mark.asyncio
async def test_remote_http_auth_and_request_credentials_are_isolated(monkeypatch):
    monkeypatch.setattr(module, "PublishClient", FakeClient)

    async def validator(token):
        return (
            {"scopes": ["materials:read"]} if token in ("token-a", "token-b") else None
        )

    app = module.create_remote_app("https://example.test", validator)
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    async with app.lifespan_context(app.inner_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            assert (
                await client.post("/mcp", headers=headers, json=body)
            ).status_code == 401
            assert (
                await client.post(
                    "/mcp",
                    headers={**headers, "Authorization": "Bearer invalid"},
                    json=body,
                )
            ).status_code == 401

            async def call(token, id):
                response = await client.post(
                    "/mcp",
                    headers={
                        **headers,
                        "Authorization": f"Bearer {token}",
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": "get_artifact",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": id,
                        "method": "tools/call",
                        "params": {
                            "name": "get_artifact",
                            "arguments": {"id": str(id)},
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
                                "io.modelcontextprotocol/clientCapabilities": {},
                            },
                        },
                    },
                )
                assert response.status_code == 200, response.text
                assert not response.json().get("error"), response.text

            await asyncio.gather(call("token-a", 2), call("token-b", 3))
    assert {tuple(call) for call in FakeClient.calls} == {
        ("get", "2", "token-a"),
        ("get", "3", "token-b"),
    }
    assert module._request_token.get() is None


@pytest.mark.asyncio
async def test_remote_admission_rejects_excess_work_before_shared_worker_starvation():
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    release = asyncio.Event()
    all_running = asyncio.Event()
    running = [0]

    async def hold(request):
        running[0] += 1
        if running[0] == module.MAX_REMOTE_REQUESTS:
            all_running.set()
        await release.wait()
        return Response("ok")

    async def validate(token):
        return {"scopes": ["materials:read"]}

    inner = Starlette(routes=[Route("/", hold)])
    app = module.AuthenticatedRemoteApp(inner, module.create_mcp(local=False), validate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        pending = [
            asyncio.create_task(
                client.get("/", headers={"Authorization": "Bearer token"})
            )
            for _ in range(module.MAX_REMOTE_REQUESTS)
        ]
        await asyncio.wait_for(all_running.wait(), timeout=5)
        try:
            overflow = await client.get("/", headers={"Authorization": "Bearer token"})
            assert (
                overflow.status_code == 429 and overflow.headers["retry-after"] == "1"
            )
        finally:
            release.set()
            assert all(
                response.status_code == 200
                for response in await asyncio.gather(*pending)
            )


@pytest.mark.asyncio
async def test_inline_update_can_explicitly_restore_purged_content_without_clearing_other_policy():
    server = module.create_mcp(client_factory=FakeClient)
    await server.call_tool(
        "update_content",
        {"id": "one", "content": "restored", "expiry": {"mode": "default"}},
    )
    assert FakeClient.calls[0][3]["metadata"] == {"expiry": {"mode": "default"}}
    assert "expected_revision" not in FakeClient.calls[0][3]
