"""Real transfer disconnects must release leases only after producers stop."""

import asyncio
import io
import threading
import zipfile

import pytest
from starlette.requests import ClientDisconnect
from test_http import publish

from minder_publish.server.content import LeasedFile
from minder_publish.server.exports import site_export

SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.4"},
    "method": "GET",
    "path": "/",
    "headers": [],
    "extensions": {},
}


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def leases(core, kind):
    with core.repository.connect() as connection:
        return connection.execute(
            "SELECT count(*) FROM read_lease WHERE kind=?", (kind,)
        ).fetchone()[0]


async def test_file_disconnect_releases_active_read_lease(application, client):
    material, _ = await publish(application, client)
    core = application.state.core
    path, _ = core.resolve_file(material["current_revision"], material["entry_path"])
    response = LeasedFile(core, material["current_revision"], path)

    async def broken_send(message):
        assert leases(core, "read") == 1
        if message["type"] == "http.response.body":
            raise OSError("disposable client disconnected")

    with pytest.raises(OSError):
        await response(SCOPE, receive, broken_send)
    assert leases(core, "read") == 0


async def test_cancelled_file_transfer_releases_read_lease(application, client):
    material, _ = await publish(application, client)
    core = application.state.core
    path, _ = core.resolve_file(material["current_revision"], material["entry_path"])
    response = LeasedFile(core, material["current_revision"], path)
    started = asyncio.Event()

    async def blocked_send(message):
        if message["type"] == "http.response.body":
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(response(SCOPE, receive, blocked_send))
    await asyncio.wait_for(started.wait(), 2)
    assert leases(core, "read") == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leases(core, "read") == 0


async def test_export_disconnect_stops_producer_before_releasing_export_lease(
    application, client
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.html", "A complete static site")
        archive.writestr("large.txt", b"x" * 2**20)
    material, _ = await publish(
        application, client, kind="site", filename="site.zip", body=buffer.getvalue()
    )
    core = application.state.core
    response = await site_export(core, application.state.config, material)

    async def broken_send(message):
        assert leases(core, "export") == 1
        if message["type"] == "http.response.body":
            raise OSError("disposable client disconnected")

    with pytest.raises(ClientDisconnect):
        await response(SCOPE, receive, broken_send)
    assert leases(core, "export") == 0
    assert not any(
        thread.name == "publish-export" and thread.is_alive()
        for thread in threading.enumerate()
    )
