"""MCP grants use the existing API without accepting their audience on the wire."""

import json

import anyio
import pytest
from test_http import sign_in

from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError
from minder_publish.mcp.internal_api import InternalApiTransport
from minder_publish.mcp.server import create_mcp
from minder_publish.server.auth import Principal


async def test_internal_mcp_grant_is_scoped_and_rechecked(application, client, tmp_path):
    active = True

    def resolve():
        return Principal("oauth", "test-grant", "materials:manage") if active else None

    artifact = tmp_path / "report.html"
    artifact.write_text("<h1>A real MCP result</h1>")

    def operation():
        with PublishClient(
            "https://publish.test",
            "opaque-oauth-bearer",
            transport=InternalApiTransport(application, resolve),
        ) as api:
            material = api.publish(artifact, kind="file")
            assert api.get(material["id"])["id"] == material["id"]
            with pytest.raises(ClientError) as error:
                api.request("GET", "/settings")
            assert error.value.status == 403
            return material

    material = await anyio.to_thread.run_sync(operation)
    assert material["stable_url"].startswith("https://publish.test/p/")
    external = await client.get(
        "/api/v1/publications",
        headers={
            "Authorization": "Bearer opaque-oauth-bearer",
            "minder_publish.internal_mcp_principal": "oauth:test-grant",
        },
    )
    assert external.status_code == 401
    active = False

    def revoked():
        with PublishClient(
            "https://publish.test",
            "opaque-oauth-bearer",
            transport=InternalApiTransport(application, resolve),
        ) as api:
            with pytest.raises(ClientError) as error:
                api.get(material["id"])
            assert error.value.status == 401

    await anyio.to_thread.run_sync(revoked)


async def test_remote_mcp_replaces_content_by_unchanged_id_after_owner_renames_link(
    application, client
):
    def factory():
        return PublishClient(
            "https://publish.test",
            "opaque-oauth-bearer",
            transport=InternalApiTransport(
                application, lambda: Principal("oauth", "test-grant", "materials:manage")
            ),
        )

    mcp = create_mcp(local=False, client_factory=factory)
    result = await mcp.call_tool(
        "publish_content", {"content": "original", "key": "initial"}
    )
    created = json.loads(result.content[0].text)
    assert created["link_name"] == "initial"
    publication_id = created["id"]

    renamed = await client.patch(
        f"/api/v1/publications/{publication_id}/link",
        json={"name": "review-notes", "keep_previous": False, "expected_name": "initial"},
        headers=await sign_in(client),
    )
    assert renamed.status_code == 200, renamed.text
    result = await mcp.call_tool(
        "update_content", {"id": publication_id, "content": "revised"}
    )
    updated = json.loads(result.content[0].text)
    assert updated["id"] == publication_id
    assert updated["current_revision"] != created["current_revision"]
    assert updated["link_name"] == "review-notes"
    assert updated["stable_url"] == "https://publish.test/p/review-notes"


async def test_internal_read_grant_cannot_publish(application, tmp_path):
    artifact = tmp_path / "report.txt"
    artifact.write_text("Read-only access must stay read-only.")

    def operation():
        with PublishClient(
            "https://publish.test",
            "opaque-read-bearer",
            transport=InternalApiTransport(
                application, lambda: Principal("oauth", "read-grant", "materials:read")
            ),
        ) as api:
            assert api.list()["total"] == 0
            with pytest.raises(ClientError) as error:
                api.publish(artifact, kind="file")
            assert error.value.status == 403

    await anyio.to_thread.run_sync(operation)
