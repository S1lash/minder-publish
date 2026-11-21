import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError

LIMITS = {
    "file_bytes": 1048576,
    "upload_payload_bytes": 1048576,
    "site_expanded_bytes": 2097152,
    "site_files": 100,
    "path_utf8_bytes": 1024,
    "path_depth": 32,
}


def test_commit_accepts_legacy_stable_url_with_an_explicit_default_port():
    legacy_url = "https://owner.example:443/p/one"
    with PublishClient(
        "https://owner.example:443",
        "test-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": {"id": "one", "stable_url": legacy_url}}
            )
        ),
    ) as client:
        assert client.commit_upload("upload-one")["stable_url"] == legacy_url


@pytest.mark.parametrize("named_links", [False, True])
def test_streaming_commit_uses_envelope_and_server_url_without_policy_defaults(
    tmp_path, monkeypatch, named_links
):
    source = tmp_path / "résultat.txt"
    source.write_bytes(b"hello" * 20000)
    requests = []
    capabilities = {"limits": LIMITS}
    committed = {"id": "one", "stable_url": "https://owner.example/p/one"}
    update = {}
    if named_links:
        capabilities["link_names"] = True
        committed.update(
            link_name="updated-name", stable_url="https://owner.example/p/updated-name"
        )
        update = {"publication_id": "one", "expected_revision": "existing-revision"}

    def transport(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer device-token"
        if request.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"data": capabilities})
        if request.url.path.endswith("/uploads"):
            plan = json.loads(request.content)
            assert plan["summary"] == ""
            assert "expiry" not in plan and "access" not in plan
            if named_links:
                assert plan["supports_link_names"] is True
                assert {field: plan[field] for field in update} == update
            else:
                assert "supports_link_names" not in plan
            return httpx.Response(200, json={"data": {"id": "upload1"}})
        if request.url.path.endswith("/inspect"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "upload1",
                        "state": "prepared",
                        "dependency_report": {
                            "complete": False,
                            "external_origins": [],
                            "missing_assets": [],
                        },
                    }
                },
            )
        if request.method == "PUT":
            assert request.content == b"hello" * 20000
            assert request.headers["content-length"] == "100000"
            return httpx.Response(200, json={"data": {"state": "received"}})
        return httpx.Response(
            200,
            json={"data": committed},
        )

    monkeypatch.setattr(
        Path, "read_bytes", lambda self: pytest.fail("whole-file read is forbidden")
    )
    with PublishClient(
        "https://owner.example",
        "device-token",
        transport=httpx.MockTransport(transport),
    ) as client:
        result = client.publish(source, kind="file", metadata={"summary": ""}, **update)
    assert {field: result[field] for field in committed} == committed
    assert [request.method for request in requests] == [
        "GET",
        "POST",
        "PUT",
        "POST",
        "POST",
    ]


@pytest.mark.parametrize(
    ("link_name", "url"),
    [
        ("my-report", "https://elsewhere.example/p/my-report"),
        ("my-report", "https://owner.example:444/p/my-report"),
        ("my-report", "https://owner.example/subdir/p/my-report"),
        ("my-report", "https://owner.example/p/one"),
        ("my-report", "https://owner.example/p/my-report?preview=1"),
        ("my-report", "https://owner.example/p/my-report#fragment"),
        ("my-report", "\x00https://owner.example/p/my-report"),
        ("extra/my-report", "https://owner.example/p/extra/my-report"),
        (None, "https://owner.example/p/one"),
    ],
)
def test_commit_rejects_mismatched_canonical_publication_urls(link_name, url):
    def transport(request):
        assert request.method == "POST"
        return httpx.Response(
            200, json={"data": {"id": "one", "link_name": link_name, "stable_url": url}}
        )

    with PublishClient(
        "https://owner.example", "token", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(ClientError, match="commit outcome") as error:
            client.commit_upload("upload-one")
    assert error.value.code == "unknown_outcome"


def test_commit_failure_aborts_upload_and_preserves_original_error(tmp_path):
    source = tmp_path / "x.txt"
    source.write_text("x")
    paths = []

    def transport(request):
        paths.append((request.method, request.url.path))
        if request.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"data": {"limits": LIMITS}})
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {"id": "u1"}})
        if request.url.path.endswith("/inspect"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "state": "prepared",
                        "dependency_report": {"complete": False},
                    }
                },
            )
        if request.url.path.endswith("/commit"):
            return httpx.Response(
                409,
                json={
                    "code": "revision_conflict",
                    "detail": "Current revision changed.",
                },
            )
        return httpx.Response(200, json={"data": {}})

    with PublishClient(
        "http://localhost:8080", "token", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(ClientError) as raised:
            client.publish(
                source, kind="file", publication_id="p1", expected_revision="old"
            )
    assert raised.value.code == "revision_conflict" and raised.value.exit_code == 5
    assert paths[-1] == ("DELETE", "/api/v1/uploads/u1")


@pytest.mark.parametrize("abort_status", [200, 503])
def test_interrupted_upload_closes_snapshot_before_abort_even_when_request_is_retained(
    tmp_path, monkeypatch, abort_status
):
    from minder_publish.client import preflight

    source = tmp_path / "source.bin"
    source.write_bytes(b"reviewed" * 30000)
    opened_snapshots = []
    original_open = preflight.open_regular

    @contextmanager
    def tracked_open(path):
        with original_open(path) as handle:
            if path != source:
                opened_snapshots.append((path, handle))
            yield handle

    monkeypatch.setattr(preflight, "open_regular", tracked_open)

    class InterruptedTransport(httpx.BaseTransport):
        retained_error = None
        retained_request = None
        closed_before_abort = None

        def handle_request(self, request):
            if request.url.path.endswith("capabilities"):
                return httpx.Response(200, json={"data": {"limits": LIMITS}})
            if request.url.path.endswith("/uploads"):
                return httpx.Response(201, json={"data": {"id": "partial"}})
            if request.method == "PUT":
                # MockTransport consumes all bytes before dispatch, hiding partial-stream failures.
                self.retained_request = request
                first = next(iter(request.stream))
                assert len(first) < source.stat().st_size
                self.retained_error = httpx.WriteError("interrupted", request=request)
                raise self.retained_error
            assert request.method == "DELETE"
            self.closed_before_abort = all(
                handle.closed for _, handle in opened_snapshots
            )
            return httpx.Response(abort_status, json={"data": {}})

    transport = InterruptedTransport()
    with PublishClient(
        "https://owner.example", "device-token", transport=transport
    ) as client:
        with pytest.raises(ClientError) as raised:
            client.publish(source, kind="file")
    assert raised.value.code == "transport_error"
    assert raised.value.__cause__ is transport.retained_error
    assert transport.retained_request is not None
    assert opened_snapshots
    assert transport.closed_before_abort is True
    assert all(handle.closed and not path.exists() for path, handle in opened_snapshots)
    assert source.read_bytes() == b"reviewed" * 30000


def test_sparse_patch_empty_summary_is_distinct_from_omitted_summary():
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {}})

    with PublishClient(
        "https://example.test", "token", transport=httpx.MockTransport(transport)
    ) as client:
        client.patch("one", {"title": "updated"})
        client.patch("one", {"summary": ""})
    assert captured == [{"title": "updated"}, {"summary": ""}]


def test_non_json_error_does_not_reflect_response_body_or_credentials():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(502, text="SECRET must never be echoed")
    )
    with PublishClient(
        "https://example.test", "private-token", transport=transport
    ) as client:
        with pytest.raises(ClientError) as raised:
            client.get("one")
    assert "SECRET" not in str(raised.value) and "private-token" not in str(
        raised.value
    )


def test_redirect_is_not_followed_with_device_credentials():
    calls = []

    def transport(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://other.test/stolen"})

    with PublishClient(
        "https://example.test", "token", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(ClientError):
            client.get("one")
    assert calls == ["example.test"]


@pytest.mark.parametrize("explicit", [False, True])
def test_purged_reupload_sends_explicit_null_revision_and_expiry(tmp_path, explicit):
    source = tmp_path / "restored.txt"
    source.write_text("restored")
    methods = []
    plans = []

    def transport(request):
        methods.append((request.method, request.url.path))
        if request.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"data": {"limits": LIMITS}})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "one",
                        "current_revision": None,
                        "content_state": "purged",
                    }
                },
            )
        if request.url.path.endswith("/uploads"):
            plans.append(json.loads(request.content))
            return httpx.Response(201, json={"data": {"id": "upload1"}})
        if request.url.path.endswith("/inspect"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "state": "prepared",
                        "dependency_report": {"complete": False},
                    }
                },
            )
        if request.url.path.endswith("/commit"):
            return httpx.Response(
                200,
                json={
                    "data": {"id": "one", "stable_url": "https://example.test/p/one"}
                },
            )
        return httpx.Response(200, json={"data": {}})

    with PublishClient(
        "https://example.test", "token", transport=httpx.MockTransport(transport)
    ) as client:
        options = {"expected_revision": None} if explicit else {}
        client.publish(
            source,
            kind="file",
            publication_id="one",
            metadata={"expiry": {"mode": "default"}},
            **options,
        )
    assert plans[0]["expected_revision"] is None and "expected_revision" in plans[0]
    assert plans[0]["expiry"] == {"mode": "default"}
    assert (("GET", "/api/v1/publications/one") in methods) is not explicit


def test_operator_prepare_limit_controls_inspection_and_commit_timeouts(tmp_path):
    source = tmp_path / "slow-preparation.txt"
    source.write_text("small fixture, large permitted server preparation window")
    deadlines = []

    def transport(request):
        if request.url.path.endswith("capabilities"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limits": {**LIMITS, "prepare_seconds": 1800},
                        "operations": {
                            "prepare_queue_seconds": 43,
                            "commit_seconds": 29,
                        },
                    }
                },
            )
        if request.url.path.endswith("/uploads"):
            return httpx.Response(201, json={"data": {"id": "slow"}})
        if request.url.path.endswith(("/inspect", "/commit")):
            deadlines.append(request.extensions["timeout"]["read"])
            if request.url.path.endswith("/inspect"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "state": "prepared",
                            "dependency_report": {"complete": False},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {"id": "one", "stable_url": "https://example.test/p/one"}
                },
            )
        return httpx.Response(200, json={"data": {}})

    with PublishClient(
        "https://example.test", "token", transport=httpx.MockTransport(transport)
    ) as client:
        client.publish(source, kind="file")
    assert deadlines == [1800 + 43 + 29 + 60] * 2


def test_local_preparation_uses_limits_namespace_not_operations(tmp_path, monkeypatch):
    from minder_publish.client import api

    source = tmp_path / "source.txt"
    source.write_text("fixture")
    observed = []
    original = api.prepare

    def checked_prepare(*args, **kwargs):
        observed.append(kwargs["limits"]["prepare_seconds"])
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "prepare", checked_prepare)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "limits": {**LIMITS, "prepare_seconds": 1800},
                    "operations": {"prepare_seconds": 1},
                }
            },
        )
    )
    with PublishClient("https://example.test", "token", transport=transport) as client:
        with client.prepared(source, kind="file"):
            pass
    assert observed == [1800]


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "network",
        "500",
        "malformed",
        "invalid_data",
        "wrong_url",
        "unstructured_400",
    ],
)
def test_uncertain_commit_preserves_operation_id_and_never_aborts_or_retries(
    tmp_path, failure
):
    source = tmp_path / "note.txt"
    source.write_text("x")
    calls = []

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("capabilities"):
            return httpx.Response(200, json={"data": {"limits": LIMITS}})
        if request.url.path.endswith("/uploads"):
            return httpx.Response(201, json={"data": {"id": "uncertain"}})
        if request.url.path.endswith("/inspect"):
            return httpx.Response(
                200, json={"data": {"state": "prepared", "dependency_report": {}}}
            )
        if request.url.path.endswith("/commit"):
            if failure == "timeout":
                raise httpx.ReadTimeout("response lost", request=request)
            if failure == "network":
                raise httpx.ReadError("response lost", request=request)
            if failure == "500":
                return httpx.Response(500, json={"code": "server_error"})
            if failure == "unstructured_400":
                return httpx.Response(400, text="proxy error")
            if failure == "wrong_url":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "one",
                            "stable_url": "https://untrusted.example/one",
                        }
                    },
                )
            if failure == "malformed":
                return httpx.Response(200, text="not JSON")
            return httpx.Response(200, json={"data": None})
        return httpx.Response(200, json={"data": {}})

    with PublishClient(
        "https://owner.example", "token", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(ClientError) as error:
            client.publish(source, kind="file")
    assert error.value.code == "unknown_outcome"
    assert error.value.actual == {"upload_id": "uncertain"}
    assert "minder-publish upload-status uncertain" in error.value.action
    assert "same --instance option or environment" in error.value.action
    assert "do not create duplicate" in error.value.action
    assert sum(path.endswith("/commit") for _, path in calls) == 1
    assert not any(method == "DELETE" for method, _ in calls)


def test_client_upload_status_uses_read_only_subject_bound_endpoint():
    def transport(request):
        assert (
            request.method == "GET" and request.url.path == "/api/v1/uploads/operation"
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "operation",
                    "state": "committed",
                    "publication_id": "one",
                }
            },
        )

    with PublishClient(
        "https://owner.example", "token", transport=httpx.MockTransport(transport)
    ) as client:
        assert client.get_upload("operation")["publication_id"] == "one"
