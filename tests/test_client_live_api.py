"""The public client against the actual API over a loopback TCP socket.

The server starts its real application lifespan and acquires its instance lock.
"""

import socket
import threading
import time
import zipfile

import pytest
import uvicorn

from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError
from minder_publish.server.app import create_app
from minder_publish.server.config import Config


SERVER_START_TIMEOUT = 15


@pytest.fixture
def live_api(tmp_path, request):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = Config(
            data_dir=tmp_path / "instance",
            base_url=f"http://127.0.0.1:{port}",
            secret="test-secret-" * 8,
            testing=True,
            remote_mcp_enabled=getattr(request, "param", False),
        )
        app = create_app(config)
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="on", log_level="warning", access_log=False)
        )
        thread = threading.Thread(
            target=lambda: server.run(sockets=[listener]), daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + SERVER_START_TIMEOUT
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "actual API failed to start"
            app.state.auth.bootstrap("owner", "a long test passphrase")
            manage = app.state.auth.create_token("test-client", "materials:manage")["token"]
            read = app.state.auth.create_token("test-reader", "materials:read")["token"]
            yield config, manage, read
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "actual API did not stop"


def test_real_http_publish_update_summary_access_and_read_profile(live_api, tmp_path):
    config, manage, read = live_api
    source = tmp_path / "report.txt"
    source.write_text("original bytes")
    with PublishClient(config.base_url, manage) as client:
        created = client.publish(
            source,
            kind="file",
            metadata={"title": "Café", "summary": "Searchable context"},
        )
        assert created["access"] == "private" and created["expires_at"] is None
        assert created["stable_url"] == f"{config.base_url}/p/{created['id']}"
        assert client.list(query="searchable")["total"] == 1
        with pytest.raises(ClientError) as raised:
            client.patch(created["id"], {"access": "link"})
        assert raised.value.code == "external_expiry_required"
        shared = client.patch(
            created["id"], {"access": "link", "expiry": {"mode": "default"}}
        )
        source.write_text("updated bytes")
        updated = client.publish(
            source,
            kind="file",
            publication_id=created["id"],
            expected_revision=shared["current_revision"],
        )
        assert updated["summary"] == "Searchable context"
        assert (
            updated["access"] == "link"
            and updated["expires_at"] == shared["expires_at"]
        )
        assert updated["stable_url"] == created["stable_url"]
        assert updated["current_revision"] != shared["current_revision"]
        cleared = client.patch(created["id"], {"summary": ""})
        assert cleared["summary"] == ""
        client.pause(created["id"])
        assert client.extend(created["id"], 86400)["paused"] is True
    with PublishClient(config.base_url, read) as client:
        assert client.get(created["id"])["id"] == created["id"]
        with pytest.raises(ClientError) as raised:
            client.patch(created["id"], {"summary": "forbidden"})
        assert raised.value.status == 403
        with pytest.raises(ClientError) as raised:
            client.request("GET", "settings")
        assert raised.value.status == 403


def test_real_http_zip_file_static_site_and_stale_update(live_api, tmp_path):
    config, manage, _ = live_api
    archive = tmp_path / "ordinary.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("index.html", "<h1>hello</h1>")
    with PublishClient(config.base_url, manage) as client:
        ordinary = client.publish(archive, kind="file")
        assert ordinary["kind"] == "file"
        site = client.publish(
            archive, kind="site", metadata={"summary": "A whole site"}
        )
        assert site["kind"] == "site"
        assert any(
            row["path"] == "index.html" for row in client.get(site["id"])["files"]
        )
        first_revision = site["current_revision"]
        replacement = tmp_path / "replacement.zip"
        with zipfile.ZipFile(replacement, "w") as output:
            output.writestr("index.html", "<h1>updated</h1>")
        current = client.publish(
            replacement,
            kind="site",
            publication_id=site["id"],
            expected_revision=first_revision,
        )
        with pytest.raises(ClientError) as raised:
            client.publish(
                archive,
                kind="site",
                publication_id=site["id"],
                expected_revision=first_revision,
            )
        assert raised.value.status == 409
        assert client.get(site["id"])["current_revision"] == current["current_revision"]


@pytest.mark.parametrize("live_api", [True], indirect=True)
def test_real_mounted_mcp_uses_callers_device_token_without_server_privilege(live_api):
    import httpx
    from mcp.types import LATEST_PROTOCOL_VERSION

    config, manage, read = live_api
    meta = {
        "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    with httpx.Client(
        base_url=config.base_url, timeout=10, follow_redirects=True
    ) as http:

        def rpc(method, params, token, id):
            response = http.post(
                "/mcp/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                    "Mcp-Method": method,
                    **({"Mcp-Name": params["name"]} if "name" in params else {}),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": id,
                    "method": method,
                    "params": {**params, "_meta": meta},
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        tools = rpc("tools/list", {}, read, 1)
        names = {tool["name"] for tool in tools["result"]["tools"]}
        assert "publish_file" not in names and "update_file" not in names
        denied = rpc(
            "tools/call",
            {"name": "publish_content", "arguments": {"content": "not allowed"}},
            read,
            2,
        )
        assert denied["result"]["isError"] is True
        with PublishClient(config.base_url, manage) as client:
            assert client.list()["total"] == 0
        allowed = rpc(
            "tools/call",
            {
                "name": "publish_content",
                "arguments": {
                    "content": "real MCP bytes",
                    "filename": "mcp.txt",
                    "summary": "via MCP",
                },
            },
            manage,
            3,
        )
        assert not allowed["result"].get("isError"), allowed
        assert manage not in str(allowed) and read not in str(allowed)
        with PublishClient(config.base_url, read) as client:
            assert client.list(query="via MCP")["total"] == 1


def test_lost_commit_response_is_reconciled_without_duplicate_publication(
    live_api, tmp_path
):
    import httpx

    config, manage, _ = live_api
    source = tmp_path / "original.txt"
    source.write_bytes(b"reviewed bytes")

    class LostResponse(httpx.BaseTransport):
        def __init__(self):
            self.network = httpx.HTTPTransport()
            self.calls = []

        def handle_request(self, request):
            self.calls.append((request.method, request.url.path))
            response = self.network.handle_request(request)
            if request.url.path.endswith("/commit"):
                response.read()
                assert response.status_code == 200
                response.close()
                raise httpx.ReadTimeout(
                    "Commit succeeded but response was lost", request=request
                )
            return response

        def close(self):
            self.network.close()

    transport = LostResponse()
    with PublishClient(config.base_url, manage, transport=transport) as client:
        with pytest.raises(ClientError) as error:
            client.publish(source, kind="file")
        assert error.value.code == "unknown_outcome"
        status = client.get_upload(error.value.actual["upload_id"])
        assert status["state"] == "committed"
        assert client.list()["total"] == 1
        assert client.get(status["publication_id"])["filename"] == "original.txt"
        assert sum(path.endswith("/commit") for _, path in transport.calls) == 1
        assert not any(method == "DELETE" for method, _ in transport.calls)
