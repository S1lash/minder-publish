"""The optional host-file capability is explicit and has the host's file schema."""

import pytest

from minder_publish.mcp.server import create_mcp


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Host:
    def __init__(self):
        self.calls = []

    def publish(self, client, file, **options):
        assert isinstance(client, Client)
        self.calls.append((file, options))
        return {"id": "one", "stable_url": "https://publish.test/p/one"}


@pytest.mark.asyncio
async def test_optional_host_file_tool_schema_and_explicit_source_reference():
    default = create_mcp(local=False, client_factory=Client)
    assert "publish_host_file" not in {tool.name for tool in await default.list_tools()}
    host = Host()
    server = create_mcp(local=False, client_factory=Client, host_files=host)
    tools = {tool.name: tool for tool in await server.list_tools()}
    tool = tools["publish_host_file"].model_dump(by_alias=True)
    assert tool["_meta"]["openai/fileParams"] == ["file"]
    assert tool["_meta"]["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["materials:manage"]}
    ]
    schema = tool["inputSchema"]
    file = schema["properties"]["file"]
    if "$ref" in file:
        file = schema["$defs"][file["$ref"].rsplit("/", 1)[1]]
    assert set(file["required"]) == {"file_id", "download_url"}
    assert {"file_name", "mime_type"} <= set(file["properties"])
    assert file["additionalProperties"] is False
    await server.call_tool(
        "publish_host_file",
        {
            "file": {
                "file_id": "file-test",
                "download_url": "https://files.example.test/reference",
            },
            "summary": "A finished result",
            "access": "private",
        },
    )
    assert host.calls == [
        (
            {
                "file_id": "file-test",
                "download_url": "https://files.example.test/reference",
            },
            {
                "kind": "file",
                "metadata": {"summary": "A finished result", "access": "private"},
                "entry_path": "index.html",
            },
        )
    ]
    polling = tools["get_file_upload"].model_dump(by_alias=True)
    assert polling["_meta"]["securitySchemes"][0]["scopes"] == ["materials:manage"]
    local = create_mcp(local=True, client_factory=Client, host_files=host)
    assert "publish_host_file" not in {tool.name for tool in await local.list_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"download_url": "https://files.example.test/a?sig=TEST_SIGNED_SECRET"},
        {
            "download_url": "https://files.example.test/a?sig=TEST_SIGNED_SECRET",
            "file_id": {"unexpected": "TEST_SIGNED_SECRET"},
        },
        {
            "download_url": "https://files.example.test/a?sig=TEST_SIGNED_SECRET",
            "file_id": "file-test",
            "extra": "TEST_SIGNED_SECRET",
        },
    ],
)
async def test_invalid_host_file_arguments_never_echo_signed_urls(invalid):
    from mcp.server.mcpserver.exceptions import ToolError

    host = Host()
    server = create_mcp(local=False, client_factory=Client, host_files=host)
    with pytest.raises(ToolError) as caught:
        await server.call_tool("publish_host_file", {"file": invalid})
    assert "invalid_tool_arguments" in str(caught.value)
    assert "TEST_SIGNED_SECRET" not in str(caught.value)
    assert "https://files.example.test" not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert host.calls == []


@pytest.mark.asyncio
async def test_invalid_file_reference_is_redacted_on_the_mcp_wire():
    import httpx
    from mcp.types import LATEST_PROTOCOL_VERSION
    from minder_publish.mcp.server import create_remote_app

    server = create_remote_app(
        "https://publish.test",
        lambda token: {"scopes": ["materials:manage"]},
        client_factory=lambda token: Client(),
        host_files=Host(),
    )
    headers = {
        "Authorization": "Bearer synthetic-test-token",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "publish_host_file",
    }
    async with server.lifespan_context(server.inner_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://localhost"
        ) as client:
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "publish_host_file",
                        "arguments": {
                            "file": {
                                "download_url": "https://files.example.test/a?sig=TEST_SIGNED_SECRET"
                            }
                        },
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["result"]["isError"] is True
            assert "invalid_tool_arguments" in response.text
            assert "TEST_SIGNED_SECRET" not in response.text
            assert "https://files.example.test" not in response.text


@pytest.mark.parametrize("tool", ["create_file_upload", "publish_host_file"])
async def test_file_tools_apply_ttl_and_reject_conflicting_expiry_before_mutation(tool):
    from datetime import datetime, timedelta, timezone

    from mcp.server.mcpserver.exceptions import ToolError

    class RecordingClient(Client):
        calls = []

        def create_file_upload(self, intent):
            self.calls.append(intent)
            return {"id": "synthetic-job"}

    class RecordingHost:
        def __init__(self):
            self.calls = []

        def publish(self, client, file, **options):
            self.calls.append(options["metadata"])
            return {"id": "synthetic-material"}

    host = RecordingHost()
    server = create_mcp(local=False, client_factory=RecordingClient, host_files=host)
    arguments = {"ttl": "3d"}
    if tool == "publish_host_file":
        arguments["file"] = {
            "file_id": "synthetic-file",
            "download_url": "https://files.example.test/reference",
        }
    before = datetime.now(timezone.utc)
    await server.call_tool(tool, arguments)
    after = datetime.now(timezone.utc)
    calls = RecordingClient.calls if tool == "create_file_upload" else host.calls
    assert len(calls) == 1
    choice = calls[0]["expiry"]
    assert choice["mode"] == "at"
    assert before + timedelta(days=3) <= datetime.fromisoformat(choice["value"]) <= after + timedelta(days=3)

    with pytest.raises(ToolError, match="conflicting_expiry"):
        await server.call_tool(tool, {**arguments, "expiry": {"mode": "never"}})
    assert len(calls) == 1
