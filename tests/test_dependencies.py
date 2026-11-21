"""Static hints never become an execution, URL-fetch, or completeness guarantee."""

from __future__ import annotations

import asyncio
import io
import json
import socket
from datetime import timedelta

import pytest
from test_core_domain import bundle, chunks, publish

from minder_publish.dependencies import DEFAULT_LIMITS, scan_dependencies
from minder_publish.errors import PublishError
from minder_publish.server.core import iso, now, parse

pytest_plugins = ("test_core_domain",)


def scan(files, **options):
    manifest = [
        {"path": path, "size_bytes": len(content)} for path, content in files.items()
    ]
    return scan_dependencies(manifest, lambda path: io.BytesIO(files[path]), **options)


def test_html_css_resources_origins_missing_and_no_secrets():
    result = scan(
        {
            "index.html": b"""<link rel="stylesheet" href="assets/site.css"><script src="https://user:secret@CDN.example:443/code.js?token=never#hidden"></script>
        <img src="missing.png?secret=hide#fragment"><img srcset="assets/ok.png 1x, //images.example:8443/large.png?token=hidden 2x">
        <video poster="//cdn.example/poster.jpg"><source src="https://video.example/file.mp4"></video>
        <a href="https://ordinary-navigation.example/">Link</a><style>body{background:url('/not-here.svg?password=hidden')}</style>""",
            "assets/site.css": b"""@import "https://styles.example/private.css?token=hide"; @font-face{src:url(https://fonts.example/font.woff2#private)}
        div{background:url(ok.png)} /* url(https://comment.example/nope) */ p{content:"url(https://text.example/nope)"}""",
            "assets/ok.png": b"not parsed as image",
        }
    )
    assert result["external_origins"] == [
        "https://cdn.example",
        "https://fonts.example",
        "https://images.example:8443",
        "https://styles.example",
        "https://video.example",
    ]
    assert result["missing_assets"] == ["missing.png", "not-here.svg"]
    serialized = json.dumps(result)
    for secret in (
        "user:",
        "secret@",
        "never",
        "token=",
        "password=",
        "hidden",
        "private.css",
        "ordinary-navigation.example",
        "comment.example",
        "text.example",
    ):
        assert secret not in serialized
    assert result["complete"] is False and result["dynamic_code_analyzed"] is False
    assert result["static_scan_complete"] is True
    assert result["scanned_files"] == 2


def test_relative_paths_base_directory_index_fragment_and_css_escapes():
    result = scan(
        {
            "guide/index.html": b"""<base href="/guide/"><img src="../assets/picture.png"><link rel="stylesheet" href="/assets/site.css"><img src="#local"><img src="sub/"><img src="https://publication.invalid/assets/picture.png">""",
            "assets/site.css": b"""@import url("https\\3a //cdn.example/base.css"); div {background:url(../assets/picture.png)}""",
            "assets/picture.png": b"bytes",
            "guide/sub/index.html": b"plain",
        },
        entry_path="guide/index.html",
    )
    assert result["external_origins"] == ["https://cdn.example"]
    assert result["missing_assets"] == []


def test_external_base_and_ipv6_canonical_origin_strip_credentials():
    result = scan(
        {
            "index.html": b"""<base href="https://user:secret@CDN.example/directory/?token=private"><img src="picture.png?access=secret"><iframe src="http://[::1]:80/private?key=hide"></iframe>"""
        }
    )
    assert result["external_origins"] == ["http://[::1]", "https://cdn.example"]
    assert "secret" not in str(result) and "private" not in str(result)


def test_no_network_or_javascript_execution_and_only_manifest_opened(monkeypatch):
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: pytest.fail("scanner attempted networking"),
    )
    files = {
        "index.html": b"""<script>throw new Error('not executed');fetch('https://dynamic.example/private?token=x')</script><img src="../../outside.txt">""",
        "outside.bin": b"not scanned",
    }
    opened = []

    def opener(path):
        opened.append(path)
        return io.BytesIO(files[path])

    result = scan_dependencies(
        [{"path": p, "size_bytes": len(v)} for p, v in files.items()], opener
    )
    assert opened == ["index.html"]
    assert result["external_origins"] == [] and result["missing_assets"] == [
        "outside.txt"
    ]
    assert result["reasons"] == ["dynamic_behavior_not_analyzed"]
    assert result["complete"] is False


def test_file_total_and_count_limits_bound_reads():
    files = {"a.html": b"x" * 100, "b.html": b"y" * 100, "c.css": b"z" * 100}
    reads = []

    class Observed(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    manifest = [{"path": p, "size_bytes": len(v)} for p, v in files.items()]
    report = scan_dependencies(
        manifest,
        lambda path: Observed(files[path]),
        limits={"file_bytes": 32, "total_bytes": 48},
    )
    assert report["scanned_bytes"] == 48 and sum(reads) == 48
    assert report["truncated"] and {"file_byte_limit", "total_byte_limit"} <= set(
        report["reasons"]
    )
    report = scan(files, limits={"files": 1})
    assert report["scanned_files"] == 1 and "file_count_limit" in report["reasons"]


def test_origin_and_missing_output_caps_are_unique():
    html = "".join(
        f'<img src="https://cdn-{n}.example/a?secret={n}"><img src="missing-{n}.png">'
        for n in range(10)
    ).encode()
    report = scan(
        {"index.html": html}, limits={"external_origins": 2, "missing_assets": 3}
    )
    assert len(report["external_origins"]) == 2 and len(report["missing_assets"]) == 3
    assert report["truncated"] and {
        "external_origin_limit",
        "missing_asset_limit",
    } <= set(report["reasons"])


def test_deadline_and_malformed_html_are_explicit_incomplete():
    ticks = [0]

    def clock():
        ticks[0] += 0.01
        return ticks[0]

    report = scan(
        {"index.html": b'<img src="missing">' * 100},
        limits={"seconds": 0.015},
        clock=clock,
    )
    assert report["truncated"] and "scan_deadline" in report["reasons"]
    report = scan({"index.html": b"<!--" + b"x" * 100000})
    assert report["truncated"] and "html_token_limit" in report["reasons"]
    report = scan(
        {
            "index.html": b'<img src="https://bad.example:wrong/token"><style>a{background:url(unclosed</style>'
        }
    )
    assert not report["static_scan_complete"] and not report["complete"]


@pytest.mark.parametrize(
    "limits",
    [
        {"file_bytes": 1.5},
        {"seconds": float("nan")},
        {"seconds": float("inf")},
        {"files": True},
        {"total_bytes": DEFAULT_LIMITS["total_bytes"] + 1},
    ],
)
def test_scan_limits_cannot_be_broadened_or_nan(limits):
    with pytest.raises(ValueError):
        scan({}, limits=limits)


def test_unsupported_encoding_and_complex_srcset_never_claim_full_analysis():
    report = scan(
        {
            "index.html": b'\xff<img srcset="data:image/png,x 1x, https://cdn.example/image 2x">'
        }
    )
    assert not report["static_scan_complete"]
    assert {"text_encoding_not_utf8", "complex_srcset_not_analyzed"} <= set(
        report["reasons"]
    )


def test_inspect_is_durable_private_precommit_and_commit_reuses_preparation(
    core, monkeypatch
):
    payload = bundle(
        [
            (
                "app.html",
                b'<link rel="stylesheet" href="https://cdn.example/private.css?token=hidden"><img src="missing.png">',
            ),
            ("assets/style.css", b"body{color:red}"),
        ]
    )
    upload = core.create_upload(
        {
            "kind": "site",
            "filename": "site.zip",
            "size_bytes": len(payload),
            "entry_path": "app.html",
        },
        "owner",
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(payload), "owner"))
    inspected = core.inspect_upload(upload["id"], "owner")
    assert inspected["state"] == "prepared" and len(inspected["files"]) == 2
    assert inspected["dependency_report"]["external_origins"] == ["https://cdn.example"]
    assert inspected["dependency_report"]["missing_assets"] == ["missing.png"]
    assert core.list_publications()["total"] == 0
    assert core.storage_snapshot()["reserved_bytes"] > 0
    with pytest.raises(PublishError):
        core.inspect_upload(upload["id"], "stranger")
    from minder_publish.server.core import Core

    restarted = Core(core.config).initialize()
    replay = restarted.inspect_upload(upload["id"], "owner")
    assert replay == inspected
    monkeypatch.setattr(
        restarted.storage,
        "prepare",
        lambda *args, **kwargs: pytest.fail("prepared ZIP was prepared twice"),
    )
    publication = restarted.commit_upload(upload["id"], "owner")
    assert publication["dependency_report"] == inspected["dependency_report"]
    assert "dependency_report" not in restarted.get_publication(publication["id"])
    assert (
        restarted.get_publication(publication["id"], manifest=True)["dependency_report"]
        == inspected["dependency_report"]
    )
    assert restarted.storage_snapshot()["reserved_bytes"] == 0


def test_prepared_review_deadline_and_abort_cleanup(core):
    upload = core.create_upload(
        {"kind": "file", "filename": "file.txt", "size_bytes": 1}, "owner"
    )
    asyncio.run(core.receive_upload(upload["id"], chunks(b"x"), "owner"))
    deadline = iso(now() + timedelta(seconds=60))
    with core.repository.connect(immediate=True) as connection:
        connection.execute(
            "UPDATE upload SET hard_deadline=? WHERE id=?", (deadline, upload["id"])
        )
    report = core.inspect_upload(upload["id"], "owner")
    assert report["expires_at"] == deadline
    assert core.abort_upload(upload["id"], "owner")["state"] == "aborted"
    assert (
        not core.storage.stage(upload["id"]).exists()
        and core.storage_snapshot()["reserved_bytes"] == 0
    )


def test_direct_commit_remains_compatible_with_uninspected_upload(core):
    material = publish(
        core, b'<img src="https://cdn.example/a?token=secret">', "index.html"
    )
    assert material["dependency_report"]["external_origins"] == ["https://cdn.example"]
    assert "token=" not in str(material["dependency_report"])


def test_purge_metadata_clock_starts_after_files_deleted(core, monkeypatch):
    import minder_publish.server.lifecycle as lifecycle

    material = publish(
        core,
        purge_mode="immediate",
        metadata_policy="after_purge",
        metadata_after_purge_seconds=180,
    )
    core.expire(material["id"])
    clock = [now()]
    monkeypatch.setattr(lifecycle, "now", lambda: clock[0])
    original = lifecycle.shutil.rmtree

    def delayed(path, *args, **kwargs):
        if path.exists():
            clock[0] += timedelta(minutes=10)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", delayed)
    core.sweep()
    result = core.get_publication(material["id"])
    assert parse(result["purged_at"]) == clock[0]
    assert parse(result["metadata_due_at"]) == clock[0] + timedelta(seconds=180)
