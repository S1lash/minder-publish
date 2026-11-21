"""Decisive Linux decoder tests; macOS fallback never proves raster generation."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import test_core_domain
from PIL import Image, PngImagePlugin

from minder_publish.server.config import policy_section
from minder_publish.server.core import Core
from minder_publish.server.thumbnails import generate_thumbnail
from test_http import publish as publish_http, sign_in

core = test_core_domain.core
publish = test_core_domain.publish
bundle = test_core_domain.bundle
publish_site = test_core_domain.publish_site

LINUX_DECODER = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Positive/resource-enforcement decoder evidence requires the production Linux RLIMIT implementation.",
)


@pytest.fixture
def thumbnail_limits():
    return policy_section("thumbnails")


@LINUX_DECODER
@pytest.mark.parametrize("format,extension", [("JPEG", "jpg"), ("PNG", "png")])
def test_linux_real_worker_generates_bounded_rgb_jpeg_with_default_memory_budget(
    tmp_path, thumbnail_limits, format, extension
):
    source = tmp_path / f"original.{extension}"
    target = tmp_path / "thumbnail.jpg"
    image = Image.new("RGB", (1200, 800), (30, 90, 160))
    if format == "PNG":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("owner-note", "private fixture metadata")
        image.save(source, format, pnginfo=metadata)
    else:
        image.save(source, format)
    assert thumbnail_limits["decoder_memory_bytes"] == 256 * 1024**2
    assert generate_thumbnail(source, target, thumbnail_limits) is True
    assert 0 < target.stat().st_size <= thumbnail_limits["output_bytes"]
    with Image.open(target) as result:
        result.load()
        assert result.format == "JPEG" and result.mode == "RGB"
        assert result.width <= 640 and result.height <= 480
        assert not result.getexif()
        assert "owner-note" not in result.info
    assert b"private fixture metadata" not in target.read_bytes()
    assert not Path(str(target) + ".tmp").exists()


@LINUX_DECODER
def test_linux_exif_orientation_applied_before_resize_and_metadata_stripped(
    tmp_path, thumbnail_limits
):
    source = tmp_path / "camera.jpg"
    target = tmp_path / "thumbnail.jpg"
    image = Image.new("RGB", (1200, 800), (190, 70, 40))
    exif = Image.Exif()
    exif[274] = 6  # The encoded landscape raster is displayed as a portrait.
    exif[315] = "private-camera-owner"
    image.save(source, "JPEG", exif=exif)
    assert generate_thumbnail(source, target, thumbnail_limits) is True
    with Image.open(target) as result:
        result.load()
        assert result.size == (320, 480)
        assert not result.getexif()
    assert b"private-camera-owner" not in target.read_bytes()


@LINUX_DECODER
def test_linux_animated_png_uses_icon_fallback(tmp_path, thumbnail_limits):
    source = tmp_path / "animation.png"
    target = tmp_path / "thumbnail.jpg"
    first = Image.new("RGB", (32, 32), "red")
    second = Image.new("RGB", (32, 32), "blue")
    first.save(
        source, "PNG", save_all=True, append_images=[second], duration=100, loop=0
    )
    with Image.open(source) as original:
        assert original.n_frames == 2
    assert generate_thumbnail(source, target, thumbnail_limits) is False
    assert not target.exists() and not Path(str(target) + ".tmp").exists()


@LINUX_DECODER
@pytest.mark.parametrize(
    "guard",
    ["source_pixels", "source_dimension", "output_bytes", "decoder_memory_bytes"],
)
def test_linux_worker_failure_is_bounded_and_leaves_no_partial_output(
    tmp_path, thumbnail_limits, guard
):
    source = tmp_path / "original.png"
    target = tmp_path / "thumbnail.jpg"
    Image.new("RGB", (64, 64), "green").save(source, "PNG")
    thumbnail_limits[guard] = {
        "source_pixels": 100,
        "source_dimension": 16,
        "output_bytes": 64,
        "decoder_memory_bytes": 8 * 1024**2,
    }[guard]
    assert generate_thumbnail(source, target, thumbnail_limits) is False
    assert not target.exists() and not Path(str(target) + ".tmp").exists()


def test_source_byte_guard_rejects_before_any_decoder_starts(
    tmp_path, thumbnail_limits, monkeypatch
):
    source = tmp_path / "oversized.png"
    source.write_bytes(b"x" * 100)
    thumbnail_limits["source_bytes"] = 99
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "decoder started before source-byte admission"
        ),
    )
    assert (
        generate_thumbnail(source, tmp_path / "thumbnail.jpg", thumbnail_limits)
        is False
    )


def test_deadline_failure_removes_partial_worker_file(
    tmp_path, thumbnail_limits, monkeypatch
):
    source = tmp_path / "source.png"
    source.write_bytes(b"fixture")
    target = tmp_path / "thumbnail.jpg"
    temporary = Path(str(target) + ".tmp")

    def timed_out(command, **kwargs):
        assert kwargs["timeout"] == thumbnail_limits["decoder_seconds"] + 1
        temporary.write_bytes(b"incomplete")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        generate_thumbnail(source, target, thumbnail_limits)
    assert not target.exists() and not temporary.exists()


def test_worker_failure_preserves_committed_original_and_releases_real_revision_lease(
    core,
):
    # This checks graceful fallback on every OS, not successful decoding on macOS.
    encoded = io.BytesIO()
    Image.new("RGB", (100, 60), "orange").save(encoded, "JPEG")
    original = encoded.getvalue()
    core.config.thumbnails["decoder_memory_bytes"] = 8 * 1024**2
    material = publish(core, original, "photo.jpg")
    core.close()
    saved = core.get_publication(material["id"])
    assert (
        saved["content_state"] == "present" and saved["thumbnail_state"] == "fallback"
    )
    path, _ = core.resolve_file(saved["current_revision"], "photo.jpg")
    assert path.read_bytes() == original
    with core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM read_lease WHERE kind='thumbnail'"
            ).fetchone()[0]
            == 0
        )


def raster(format="PNG", color="orange"):
    encoded = io.BytesIO()
    Image.new("RGB", (48, 27), color).save(encoded, format)
    return encoded.getvalue()


@pytest.mark.parametrize(
    "extra",
    [
        [],
        [("publish-cover.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")],
        [("publish-cover.pdf", b"%PDF-1.7")],
        [("assets/publish-cover.png", raster())],
        [("Publish-cover.png", raster())],
    ],
)
def test_site_requires_an_exact_bundled_root_asset(core, extra, monkeypatch):
    calls = []

    def unexpected_generation(*args):
        calls.append(args)

    monkeypatch.setattr(
        "minder_publish.server.thumbnails.generate_thumbnail", unexpected_generation
    )
    material = publish_site(core, bundle([("index.html", b"<h1>Site</h1>"), *extra]))
    core.close()
    saved = core.get_publication(material["id"])
    assert saved["content_state"] == "present"
    assert saved["thumbnail_state"] == "fallback"
    assert not calls


def test_site_cover_worker_start_failure_commits_without_lease_or_startup_backfill(
    core, monkeypatch
):
    start = threading.Thread.start
    attempts = []

    def start_thread(thread):
        if thread.name == "publish-thumbnail":
            attempts.append(thread.name)
            raise RuntimeError("test thumbnail thread unavailable")
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_thread)
    material = publish_site(
        core,
        bundle([("index.html", b"<h1>Site</h1>"), ("publish-cover.png", raster())]),
    )
    core.close()
    restarted = Core(core.config).initialize()
    saved = restarted.get_publication(material["id"])
    assert saved["content_state"] == "present"
    assert saved["thumbnail_state"] == "fallback"
    with restarted.repository.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM read_lease WHERE kind='thumbnail'"
        ).fetchone()
    restarted.close()
    assert attempts == ["publish-thumbnail"]


@LINUX_DECODER
def test_site_cover_replacement_and_restore_use_only_the_committed_revision(core):
    # Put the JPEG first in ZIP order and the entry point in a subdirectory.
    original = [
        ("pages/start.html", b"<h1>Site</h1>"),
        ("publish-cover.jpg", raster("JPEG", "blue")),
        ("publish-cover.png", raster(color="red")),
    ]
    first = publish_site(core, bundle(original), entry_path="pages/start.html")
    core.close()
    first_id = first["current_revision"]
    first_cover = core.storage.revision(first_id) / "thumbnail.jpg"
    with Image.open(first_cover) as cover:
        red, green, blue = cover.getpixel((0, 0))
        assert red > 240 and green < 15 and blue < 15
    source, _ = core.resolve_file(first_id, "publish-cover.png")
    assert source.read_bytes() == original[2][1]

    restarted = Core(core.config).initialize()
    assert restarted.get_publication(first["id"])["thumbnail_state"] == "ready"
    second = publish_site(
        restarted,
        bundle([("index.html", b"<h1>Replacement</h1>")]),
        publication_id=first["id"],
        expected_revision=first_id,
        entry_path="index.html",
    )
    assert second["thumbnail_state"] == "fallback"
    assert first_cover.is_file()

    third = restarted.restore_revision(
        first["id"],
        first_id,
        second["current_revision"],
        "owner",
        authorize=lambda connection, upload_id: None,
    )
    restarted.close()
    assert third["current_revision"] not in (first_id, second["current_revision"])
    assert restarted.get_publication(first["id"])["thumbnail_state"] == "ready"
    restored_cover = (
        restarted.storage.revision(third["current_revision"]) / "thumbnail.jpg"
    )
    assert restored_cover.read_bytes() == first_cover.read_bytes()


@LINUX_DECODER
def test_site_cover_with_unrecognized_bytes_keeps_publication_and_uses_fallback(core):
    source = bundle(
        [
            ("index.html", b"<h1>Site</h1>"),
            ("publish-cover.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
            ("publish-cover.webp", raster("WEBP")),
        ]
    )
    material = publish_site(core, source)
    core.close()
    saved = core.get_publication(material["id"])
    assert saved["content_state"] == "present"
    assert saved["thumbnail_state"] == "fallback"
    page, _ = core.resolve_file(saved["current_revision"], "index.html")
    assert page.read_bytes() == b"<h1>Site</h1>"


@LINUX_DECODER
async def test_site_cover_makes_public_branded_card_and_keeps_library_api_private(
    application, client
):
    material, _ = await publish_http(
        application,
        client,
        access="link",
        kind="site",
        filename="site.zip",
        body=bundle(
            [("index.html", b"<h1>Site</h1>"), ("publish-cover.webp", raster("WEBP"))]
        ),
    )
    application.state.core.close()
    thumbnail = f"/api/v1/publications/{material['id']}/thumbnail"
    assert (await client.get(thumbnail)).status_code == 401

    preview = await client.get(
        material["stable_url"],
        headers={"User-Agent": "TelegramBot"},
        follow_redirects=False,
    )
    assert preview.status_code == 200
    from test_link_previews import Tags

    card_url = Tags(preview.text).meta["og:image"]
    assert "/__mp/preview/" in card_url
    assert thumbnail not in preview.text
    card = await client.get(card_url)
    assert card.status_code == 200
    with Image.open(io.BytesIO(card.content)) as image:
        assert image.size == (1024, 1024)
        assert image.format == "JPEG"

    read_token = application.state.auth.create_token("read", "materials:read")["token"]
    device = await client.get(
        thumbnail, headers={"Authorization": f"Bearer {read_token}"}
    )
    assert device.status_code == 200
    assert device.headers["content-type"] == "image/jpeg"
    assert device.headers["cache-control"] == "no-store"
    with Image.open(io.BytesIO(device.content)) as cover:
        assert cover.format == "JPEG"
        assert cover.size == (48, 27)

    await sign_in(client)
    owner = await client.get(thumbnail)
    assert owner.status_code == 200
    assert owner.content == device.content
