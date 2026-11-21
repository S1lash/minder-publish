"""Host file ingestion pins public endpoints and reserves before consuming bytes."""

import asyncio
import io
import socket
import threading
import time
import zipfile

import pytest

from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError
from minder_publish.mcp.host_files import HostFiles
from minder_publish.mcp.internal_api import InternalApiTransport

HOST = "files.provider.test"
FILE = {
    "file_id": "file_test",
    "download_url": "https://" + HOST + "/file?signature=never-echo-this",
    "file_name": "report.txt",
}


def resolve_public(*args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
    ]


class Source:
    def __init__(
        self, body=b"A finished report", *, headers=None, status=200, before_read=None
    ):
        self.body = io.BytesIO(body)
        self.headers = (
            [("Content-Length", str(len(body)))] if headers is None else headers
        )
        self.status = status
        self.before_read = before_read
        self.reads = 0
        self.closed = False
        self.timeouts = []

    def getheaders(self):
        return self.headers

    def set_timeout(self, timeout):
        self.timeouts.append(timeout)

    def read(self, count):
        self.reads += 1
        if self.before_read:
            self.before_read()
        return self.body.read(count)

    def close(self):
        self.closed = True


def adapter(source, *, resolver=resolve_public, clock=None, total_seconds=120):
    options = {
        "resolver": resolver,
        "opener": lambda target, address, timeout: source,
        "total_seconds": total_seconds,
    }
    if clock:
        options["clock"] = clock
    return HostFiles((HOST,), **options)


@pytest.fixture
def authenticated_client(application):
    auth = application.state.auth
    issued = auth.create_token("Host file test", "materials:manage")
    token = issued["token"]
    transport = InternalApiTransport(application, lambda: auth.token(token))
    with PublishClient("https://publish.test", token, transport=transport) as client:
        yield client, issued


@pytest.mark.parametrize(
    "host",
    [
        "*",
        "*.provider.test",
        "127.0.0.1",
        "https://files.provider.test",
        "files.provider.test:443",
    ],
)
def test_allowlist_accepts_only_exact_dns_names(host):
    with pytest.raises(ValueError):
        HostFiles((host,))


@pytest.mark.parametrize(
    "url",
    [
        "http://files.provider.test/a",
        "https://files.provider.test:8443/a",
        "https://user:password@files.provider.test/a",
        "https://files.provider.test/a#fragment",
        "https://other.test/a",
        "https://files.provider.test/a\r\nX-Test: value",
    ],
)
def test_untrusted_file_urls_are_rejected_before_opening(url):
    source = Source()
    value = {**FILE, "download_url": url}
    with pytest.raises(ClientError) as error:
        adapter(source).publish(None, value, kind="file", metadata={})
    assert "never-echo" not in str(error.value.as_dict())
    assert source.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "224.0.0.1",
        "::ffff:93.184.216.34",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
    ],
)
async def test_private_or_mixed_dns_answers_are_rejected(
    application, authenticated_client, address
):
    source = Source()

    def mixed(*args, **kwargs):
        return resolve_public() + [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443, 0, 0) if ":" in address else (address, 443),
            )
        ]

    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source, resolver=mixed).publish,
            client,
            FILE,
            kind="file",
            metadata={},
        )
    assert error.value.code == "unsafe_host_file_address"
    assert source.reads == 0


@pytest.mark.asyncio
async def test_native_public_ipv6_can_be_pinned(application, authenticated_client):
    source, opened = Source(), []

    def resolver(*args, **kwargs):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            )
        ]

    def opener(target, address, timeout):
        opened.append(address)
        return source

    client, _ = authenticated_client
    await asyncio.to_thread(
        HostFiles((HOST,), resolver=resolver, opener=opener).publish,
        client,
        FILE,
        kind="file",
    )
    assert opened == ["2606:4700:4700::1111"] and source.closed


@pytest.mark.asyncio
async def test_host_filename_cannot_echo_signed_download_url(
    application, authenticated_client
):
    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(Source()).publish,
            client,
            {**FILE, "file_name": FILE["download_url"]},
            kind="file",
        )
    assert "signature" not in str(error.value.as_dict())


@pytest.mark.asyncio
async def test_real_core_reservation_precedes_source_read_and_publishes(
    application, authenticated_client
):
    core = application.state.core
    source = Source(
        before_read=lambda: (core.storage_snapshot()["reserved_bytes"] > 0)
        or pytest.fail("Source consumed before reservation")
    )
    client, _ = authenticated_client
    value = await asyncio.to_thread(
        adapter(source).publish,
        client,
        FILE,
        kind="file",
        metadata={"title": "Report", "summary": "Short description"},
    )
    assert value["stable_url"].startswith("https://publish.test/p/")
    material = core.get_publication(value["id"])
    assert material["summary"] == "Short description"
    assert (
        core.resolve_file(material["current_revision"], material["entry_path"])[
            0
        ].read_bytes()
        == b"A finished report"
    )
    assert source.closed and core.storage_snapshot()["reserved_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [
        ([], 200),
        ([("Content-Length", "999999999999999999")], 200),
        ([("Content-Length", "1"), ("Content-Length", "1")], 200),
        ([("Content-Length", "1"), ("Content-Encoding", "gzip")], 200),
        ([("Content-Length", "1"), ("Transfer-Encoding", "chunked")], 200),
        ([("Content-Length", "0"), ("Location", "http://127.0.0.1/secret")], 302),
    ],
)
async def test_response_policy_rejects_before_reservation_or_body(
    application, authenticated_client, headers, status
):
    source = Source(headers=headers, status=status)
    client, _ = authenticated_client
    with pytest.raises(ClientError):
        await asyncio.to_thread(
            adapter(source).publish, client, FILE, kind="file", metadata={}
        )
    assert source.reads == 0 and source.closed
    assert application.state.core.storage_snapshot()["reserved_bytes"] == 0


@pytest.mark.asyncio
async def test_truncated_source_aborts_known_upload_without_echoing_url(
    application, authenticated_client
):
    source = Source(b"short", headers=[("Content-Length", "20")])
    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source).publish, client, FILE, kind="file", metadata={}
        )
    assert error.value.code == "host_file_size_mismatch"
    assert "signature" not in str(error.value.as_dict())
    assert (
        application.state.core.storage_snapshot()["reserved_bytes"] == 0
        and source.closed
    )


@pytest.mark.asyncio
async def test_sensitive_site_manifest_is_never_committed(
    application, authenticated_client
):
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("index.html", "<h1>Site</h1>")
        archive.writestr("credentials.json", '{"private":"synthetic"}')
    source = Source(body.getvalue())
    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source).publish,
            client,
            {**FILE, "file_name": "site.zip"},
            kind="site",
            metadata={},
        )
    assert error.value.code == "sensitive_host_file"
    assert application.state.core.storage_snapshot()["reserved_bytes"] == 0
    with application.state.core.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM publication").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_stream_total_deadline_closes_and_reclaims_upload(
    application, authenticated_client
):
    now = [0.0]

    def advance():
        now[0] += 0.75

    source = Source(b"a" * 150000, before_read=advance)
    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source, clock=lambda: now[0], total_seconds=1).publish,
            client,
            FILE,
            kind="file",
            metadata={},
        )
    assert error.value.code == "host_file_timeout"
    assert (
        source.closed
        and application.state.core.storage_snapshot()["reserved_bytes"] == 0
    )


@pytest.mark.asyncio
async def test_revocation_between_transfer_and_inspection_never_commits(
    application, authenticated_client
):
    client, issued = authenticated_client
    revoked = [False]

    def revoke():
        if not revoked[0]:
            application.state.auth.revoke_token(issued["id"])
            revoked[0] = True

    source = Source(before_read=revoke)
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source).publish, client, FILE, kind="file", metadata={}
        )
    assert error.value.status == 401 and source.closed
    with application.state.core.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM publication").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_disk_admission_failure_consumes_no_source_bytes(
    application, authenticated_client, monkeypatch
):
    import shutil
    from minder_publish.server import core

    monkeypatch.setattr(
        core.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 100, 0)
    )
    source = Source()
    client, _ = authenticated_client
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(source).publish, client, FILE, kind="file", metadata={}
        )
    assert error.value.status == 507 and source.reads == 0 and source.closed


@pytest.mark.asyncio
async def test_commit_timeout_keeps_committed_publication_and_reports_unknown(
    application, authenticated_client, monkeypatch
):
    client, _ = authenticated_client
    request, operations = client.request, []

    def lost_commit(method, path, **kwargs):
        operations.append((method, path))
        result = request(method, path, **kwargs)
        if path.endswith("/commit"):
            raise ClientError("timeout", "Synthetic lost response")
        return result

    monkeypatch.setattr(client, "request", lost_commit)
    with pytest.raises(ClientError) as error:
        await asyncio.to_thread(
            adapter(Source()).publish, client, FILE, kind="file", metadata={}
        )
    assert error.value.code == "unknown_outcome" and error.value.actual["upload_id"]
    assert not any(method == "DELETE" for method, _ in operations)
    status = await asyncio.to_thread(client.get_upload, error.value.actual["upload_id"])
    assert status["state"] == "committed"
    assert status["stable_url"] == client.base_url + "/p/" + status["publication_id"]
    with application.state.core.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM publication").fetchone()[0] == 1
        assert (
            connection.execute("SELECT state FROM upload").fetchone()[0] == "committed"
        )


def test_dns_deadline_is_bounded_even_when_system_resolver_stalls():
    finish, completed = threading.Event(), threading.Event()

    def stalled(*args, **kwargs):
        try:
            finish.wait(2)
            return resolve_public()
        finally:
            completed.set()

    began = time.monotonic()
    try:
        with pytest.raises(ClientError) as error:
            adapter(Source(), resolver=stalled, total_seconds=0.03).publish(
                None, FILE, kind="file", metadata={}
            )
        assert (
            error.value.code == "host_file_timeout" and time.monotonic() - began < 0.5
        )
    finally:
        finish.set()
        assert completed.wait(1)


def test_tls_connect_pins_address_preserves_hostname_and_hard_deadline(monkeypatch):
    from minder_publish.mcp import host_files

    class Socket:
        def __init__(self):
            self.shutdowns, self.closed, self.handshakes = [], False, 0

        def shutdown(self, how):
            self.shutdowns.append(how)

        def close(self):
            self.closed = True

        def do_handshake(self):
            self.handshakes += 1

    current, calls = Socket(), []

    class Context:
        def wrap_socket(self, source, **kwargs):
            assert source is current
            calls.append(kwargs)
            return source

    monkeypatch.setattr(host_files.ssl, "create_default_context", lambda: Context())

    def connect(endpoint, timeout):
        assert endpoint == ("93.184.216.34", 443) and timeout == 0.2
        return current

    monkeypatch.setattr(host_files.socket, "create_connection", connect)
    connection = host_files._PinnedHTTPSConnection(
        HOST, "93.184.216.34", 0.2, deadline_seconds=0.03
    )
    try:
        connection.connect()
        assert calls == [{"server_hostname": HOST, "do_handshake_on_connect": False}]
        assert current.handshakes == 1
        assert connection.expired.wait(0.5)
        assert current.closed and current.shutdowns == [socket.SHUT_RDWR]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_extra_bytes_or_socket_timeout_aborts_and_redacts_source(
    application, authenticated_client
):
    class BrokenSource(Source):
        def read(self, count):
            raise OSError("https://files.provider.test/file?signature=never-echo-this")

    client, _ = authenticated_client
    for source, code in (
        (
            Source(b"extra", headers=[("Content-Length", "1")]),
            "host_file_size_mismatch",
        ),
        (BrokenSource(), "host_file_transfer_failed"),
    ):
        with pytest.raises(ClientError) as error:
            await asyncio.to_thread(
                adapter(source).publish, client, FILE, kind="file", metadata={}
            )
        assert error.value.code == code and "signature" not in str(
            error.value.as_dict()
        )
        assert (
            source.closed
            and application.state.core.storage_snapshot()["reserved_bytes"] == 0
        )
