"""Revision notes reach uploads without becoming publication metadata patches."""

import pytest

from minder_publish.client import cli
from minder_publish.client.config import Connection
from minder_publish.client.errors import ClientError


@pytest.mark.parametrize("arguments", [
    ["publish", "example.txt", "--kind", "file", "--change-summary", "Initial"],
    ["update", "item", "example.txt", "--change-summary", "Initial"],
])
def test_cli_forwards_revision_note(monkeypatch, arguments):
    seen = []

    class Client:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, identifier): return {"kind": "file", "current_revision": "old"}
        def publish(self, path, **kwargs):
            seen.append(kwargs)
            return {"id": "item"}

    monkeypatch.setattr(cli.config, "connection", lambda _: Connection("home", "https://publish.test", "token"))
    monkeypatch.setattr(cli, "PublishClient", Client)
    cli.run(cli.build_parser().parse_args(arguments))
    assert seen[0]["metadata"] == {"change_summary": "Initial"}
    if arguments[0] == "update":
        assert seen[0]["publication_id"] == "item"
        assert seen[0]["expected_revision"] == "old"


def test_cli_set_does_not_accept_revision_note():
    with pytest.raises(ClientError):
        cli.build_parser().parse_args(["set", "item", "--change-summary", "wrong scope"])


async def test_mcp_revision_notes_reach_both_inline_and_local_uploads(tmp_path):
    pytest.importorskip("mcp")
    from minder_publish.mcp.server import create_mcp

    calls = []

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, identifier): return {"kind": "file", "entry_path": "index.html"}
        def publish(self, path, **kwargs):
            calls.append(kwargs)
            return {"id": "item"}

    source = tmp_path / "example.txt"
    source.write_text("synthetic")
    server = create_mcp(local=True, client_factory=Client)
    await server.call_tool("publish_content", {"content": "hello", "change_summary": "one"})
    await server.call_tool("update_content", {"id": "item", "content": "hello", "change_summary": "two"})
    await server.call_tool("publish_file", {"path": str(source), "kind": "file", "change_summary": "three"})
    await server.call_tool("update_file", {"id": "item", "path": str(source), "kind": "file", "change_summary": "four"})
    assert [call["metadata"]["change_summary"] for call in calls] == ["one", "two", "three", "four"]
    assert [call.get("publication_id") for call in calls] == [None, "item", None, "item"]
