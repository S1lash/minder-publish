"""Wire-visible OAuth scopes, linking challenges and legacy option semantics."""

import httpx
import pytest

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError

from minder_publish.client.errors import ClientError
from minder_publish.mcp.server import create_mcp, create_remote_app

RESOURCE_METADATA = "https://publish.test/.well-known/oauth-protected-resource/mcp"


async def test_remote_tools_advertise_exact_scopes_through_the_public_sdk_meta_api():
    server = create_mcp(local=False)
    tools = await server.list_tools()
    reads = {"list_artifacts", "get_artifact", "get_upload"}
    expected = reads | {
        "get_file_upload",
        "publish_content",
        "create_file_upload",
        "cancel_file_upload",
        "update_content",
        "set_meta",
        "extend_artifact",
        "delete_artifact",
        "pause_artifact",
        "expire_artifact",
    }
    assert {tool.name for tool in tools} == expected
    for tool in tools:
        wire = tool.model_dump(mode="json", by_alias=True)
        assert wire["_meta"]["securitySchemes"] == [
            {
                "type": "oauth2",
                "scopes": [
                    "materials:read" if tool.name in reads else "materials:manage"
                ],
            }
        ]
    local = create_mcp(local=True)
    assert all(
        not tool.meta or "securitySchemes" not in tool.meta
        for tool in await local.list_tools()
    )


@pytest.mark.parametrize(
    "status,code,tool,args,scope",
    [
        (401, "invalid_credentials", "get_artifact", {"id": "one"}, "materials:read"),
        (
            403,
            "insufficient_scope",
            "set_meta",
            {"id": "one", "title": "Updated"},
            "materials:manage",
        ),
    ],
)
async def test_remote_tool_auth_failures_return_linking_meta_without_sensitive_details(
    status, code, tool, args, scope
):
    class Rejected:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args):
            raise ClientError(code, "must-not-return-sensitive-response", status=status)

        def patch(self, *args):
            raise ClientError(code, "must-not-return-sensitive-response", status=status)

    server = create_mcp(
        local=False, client_factory=Rejected, resource_metadata=RESOURCE_METADATA
    )
    result = await server.call_tool(tool, args)
    wire = result.model_dump(mode="json", by_alias=True)
    assert wire["isError"] is True
    challenge = wire["_meta"]["mcp/www_authenticate"][0]
    assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
    assert f'scope="{scope}"' in challenge
    assert "error=" in challenge and "error_description=" in challenge
    assert "must-not-return-sensitive-response" not in str(wire)


async def test_non_auth_denial_does_not_create_an_oauth_retry_loop():
    class Rejected:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args):
            raise ClientError(
                "creator_mismatch", "Not this application's request.", status=403
            )

    server = create_mcp(
        local=False, client_factory=Rejected, resource_metadata=RESOURCE_METADATA
    )
    with pytest.raises(ToolError):
        await server.call_tool("get_artifact", {"id": "one"})


async def test_legacy_key_hint_and_password_options_map_to_canonical_sparse_metadata():
    captured = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def publish(self, path, **kwargs):
            captured.append(kwargs["metadata"])
            return {"id": "report", "stable_url": "https://publish.test/p/report"}

        def patch(self, identifier, changes):
            captured.append(changes)
            return {"id": identifier}

    server = create_mcp(local=False, client_factory=Client)
    await server.call_tool(
        "publish_content",
        {
            "content": "reviewed",
            "key": "old-report",
            "password": "publication password",
            "hint": "a hint",
        },
    )
    await server.call_tool("set_meta", {"id": "old-report", "hint": ""})
    await server.call_tool("set_meta", {"id": "old-report", "title": "Updated"})
    assert captured == [
        {
            "key": "old-report",
            "password": "publication password",
            "password_hint": "a hint",
            "access": "password",
        },
        {"password_hint": ""},
        {"title": "Updated"},
    ]


async def test_explicit_access_choice_is_preserved_when_a_password_is_provided():
    captured = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def publish(self, path, **kwargs):
            captured.append(kwargs["metadata"])
            return {"id": "report"}

    server = create_mcp(local=True, client_factory=Client)
    await server.call_tool(
        "publish_content",
        {"content": "reviewed", "password": "a password", "access": "private"},
    )
    assert captured[0]["access"] == "private"


async def test_remote_protocol_challenges_recommend_publishing_scope():
    app = create_remote_app(
        "https://publish.test", lambda token: None, resource_metadata=RESOURCE_METADATA
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post("/mcp", content=b"not parsed")
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert f'resource_metadata="{RESOURCE_METADATA}"' in challenge
        assert 'scope="materials:manage"' in challenge
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer rejected"}
        )
        assert response.status_code == 401
        assert 'error="invalid_token"' in response.headers["www-authenticate"]
        app.validator = lambda token: {"scopes": ["owner"]}
        response = await client.post(
            "/mcp", headers={"Authorization": "Bearer rejected"}
        )
        assert response.status_code == 403
        assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
