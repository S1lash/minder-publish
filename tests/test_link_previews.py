"""Crawlers see only guest-public metadata, never reader grants or author HTML."""

from html.parser import HTMLParser
from io import BytesIO
import hashlib
from pathlib import Path

import pytest
from PIL import Image
from test_core_domain import bundle
from test_http import publish, sign_in


def asset_url(origin, name):
    path = Path(__file__).parents[1] / "src/minder_publish/server/web" / name
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"{origin}/static/{name}?v={version}"


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.meta = {}
        self.links = {}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            self.meta[values.get("property", values.get("name"))] = values.get(
                "content"
            )
        elif tag == "link":
            self.links[values.get("rel")] = values.get("href")


@pytest.mark.parametrize(
    "agent",
    [
        "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
        "TelegramBot (like TwitterBot)",
    ],
)
@pytest.mark.parametrize("access", ["link", "password", "private"])
async def test_crawler_preview_is_guest_policy_without_grants(
    application, client, agent, access
):
    material, _ = await publish(
        application,
        client,
        access=access,
        password="do-not-preview" if access == "password" else None,
    )
    # Owner cookies cannot make a private unfurl reveal its metadata.
    await sign_in(client)
    response = await client.get(
        material["stable_url"], headers={"User-Agent": agent}, follow_redirects=False
    )
    assert response.status_code == 200
    tags = Tags(response.text).meta
    assert tags["og:site_name"] == "minder publish."
    assert tags["og:title"] != tags["og:site_name"]
    assert tags["og:image"] == asset_url("https://publish.test", "link-preview.png")
    assert tags["og:url"] == material["stable_url"]
    assert "set-cookie" not in response.headers
    assert "/__mp/" not in response.text and "<script" not in response.text
    assert tags["twitter:card"] == "summary"
    if access == "link":
        assert tags["og:title"] == "A result"
        assert tags["og:description"] == "Searchable description"
    else:
        assert tags["og:title"] == "Something worth opening."
        assert tags["og:description"] == (
            "Ideas, documents and sites, shared with Minder Publish."
        )
        assert tags["twitter:title"] == tags["og:title"]
        assert tags["twitter:description"] == tags["og:description"]
        for secret in (
            "A result",
            "Searchable description",
            "index.html",
            "do-not-preview",
        ):
            assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("operation", ["pause", "expire", "delete"])
async def test_unavailable_public_material_has_generic_status_preview(
    application, client, operation
):
    material, headers = await publish(application, client, access="link")
    path = f"/api/v1/publications/{material['id']}"
    if operation == "delete":
        changed = await client.delete(path, headers=headers)
    else:
        changed = await client.post(
            path + "/" + operation,
            json={} if operation == "expire" else {"paused": True},
            headers=headers,
        )
    assert changed.status_code == 200, changed.text
    response = await client.get(
        material["stable_url"], headers={"User-Agent": "TelegramBot"}
    )
    assert response.status_code == 200
    assert Tags(response.text).meta["og:title"] == "Material unavailable"
    assert (
        "A result" not in response.text
        and "Searchable description" not in response.text
    )


async def test_preview_escapes_metadata_and_drops_query_secrets(application, client):
    material, headers = await publish(application, client, access="link")
    title = '"><script>alert(1)</script>&'
    summary = '<img src=x onerror="bad">'
    changed = await client.patch(
        f"/api/v1/publications/{material['id']}",
        json={"title": title, "summary": summary},
        headers=headers,
    )
    assert changed.status_code == 200
    response = await client.get(
        material["stable_url"] + "?token=not-a-preview-secret",
        headers={"User-Agent": "Slackbot-LinkExpanding"},
    )
    tags = Tags(response.text).meta
    assert tags["og:title"] == title and tags["og:description"] == summary
    assert "<script>" not in response.text and "<img src=x" not in response.text
    assert "not-a-preview-secret" not in response.text
    missing = await client.get(
        "/p/unknown-material", headers={"User-Agent": "TelegramBot"}
    )
    assert missing.status_code == 404


async def test_gate_has_generic_metadata_and_png_is_public_packaged_asset(
    application, client
):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    response = await client.get(material["stable_url"])
    tags = Tags(response.text)
    assert tags.meta["og:title"] == "Something worth opening."
    assert tags.meta["og:url"] == material["stable_url"]
    assert tags.links["canonical"] == material["stable_url"]
    assert tags.links["icon"] == asset_url("https://publish.test", "mark.svg")
    assert (await client.get(tags.links["icon"])).status_code == 200
    touch = await client.get(tags.links["apple-touch-icon"])
    with Image.open(BytesIO(touch.content)) as decoded:
        assert decoded.size == (180, 180)
        assert decoded.format == "PNG"
    assert "img-src https://publish.test;" in response.headers["content-security-policy"]
    assert "A result" not in response.text and "Searchable description" not in response.text
    image = await client.get(Tags(response.text).meta["og:image"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    with Image.open(BytesIO(image.content)) as decoded:
        assert decoded.size == (1024, 1024)
        assert decoded.format == "PNG"


async def test_public_cover_revalidates_access_and_pinned_sharing(application, client):
    material, token = await publish(
        application, client, access="link", kind="site", filename="site.zip",
        body=bundle([("index.html", b"<h1>Public site</h1>")]),
    )
    identifier = material["id"]
    revision = material["current_revision"]
    cover = application.state.core.storage.revision(revision) / "thumbnail.jpg"
    source = Image.new("RGB", (640, 360), (199, 23, 45))
    source.paste((20, 35, 210), (0, 0, 24, 360))
    source.paste((20, 190, 35), (616, 0, 640, 360))
    source.save(cover, "JPEG")
    crawler = {"User-Agent": "TelegramBot"}
    preview = await client.get(material["stable_url"], headers=crawler)
    tags = Tags(preview.text).meta
    assert tags["twitter:card"] == "summary"
    assert tags["og:image:width"] == tags["og:image:height"] == "1024"
    image_url = tags["og:image"]
    assert "?v=2-" in image_url
    image = await client.get(image_url)
    head = await client.head(image_url)
    assert image.status_code == head.status_code == 200
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["content-type"] == "image/jpeg"
    assert head.content == b"" and head.headers["content-length"] == str(len(image.content))
    with Image.open(BytesIO(image.content)) as decoded:
        assert decoded.size == (1024, 1024)
        assert decoded.getpixel((512, 448))[0] > 180
        assert decoded.getpixel((96, 448))[2] > 180
        assert decoded.getpixel((928, 448))[1] > 170

    owner = await sign_in(client)
    sharing = f"/api/v1/publications/{identifier}/revisions/{revision}/sharing"
    shared = await client.post(sharing, json={"enabled": True}, headers=owner)
    assert shared.status_code == 200, shared.text
    version_url = shared.json()["data"]["version_url"]
    pinned = await client.get(version_url, headers=crawler)
    pinned_image = Tags(pinned.text).meta["og:image"]
    assert pinned_image != image_url
    assert (await client.get(pinned_image)).content == image.content

    unshared = await client.post(sharing, json={"enabled": False}, headers=owner)
    assert unshared.status_code == 200, unshared.text
    assert (await client.get(pinned_image)).status_code == 404
    assert (await client.get(image_url)).status_code == 200

    changed = await client.patch(
        f"/api/v1/publications/{identifier}", json={"access": "private"}, headers=token,
    )
    assert changed.status_code == 200, changed.text
    assert (await client.get(image_url)).status_code == 404
    private = await client.get(material["stable_url"], headers=crawler)
    private_tags = Tags(private.text).meta
    assert private_tags["og:title"] == "Something worth opening."
    assert private_tags["og:image"] == asset_url("https://publish.test", "link-preview.png")


async def test_preview_get_and_head_do_not_allocate_reader_authority(
    application, client
):
    material, _ = await publish(application, client, access="link")

    def authority_counts():
        with application.state.auth.connection() as connection:
            return [
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "auth_intents",
                    "auth_readers",
                    "auth_viewers",
                    "auth_unlocks",
                )
            ]

    before = authority_counts()
    for method in ("GET", "HEAD"):
        response = await client.request(
            method,
            material["stable_url"],
            headers={"User-Agent": "TelegramBot"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert (
            "set-cookie" not in response.headers and "location" not in response.headers
        )
        if method == "HEAD":
            assert response.content == b""
    assert authority_counts() == before


async def test_opening_page_script_csp_and_indexing_policy(application, client):
    from browser_navigation import continuation_target
    material, headers = await publish(application, client, access="link")
    response = await client.get(material["stable_url"], follow_redirects=False)
    assert continuation_target(response)
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    changed = await client.patch(
        f"/api/v1/publications/{material['id']}", json={"indexing": True}, headers=headers
    )
    assert changed.status_code == 200
    response = await client.get(material["stable_url"], follow_redirects=False)
    assert continuation_target(response)
    assert response.headers["x-robots-tag"] == "index, follow"


@pytest.mark.parametrize("access", ["link", "password", "private"])
async def test_unknown_agent_uses_normal_guest_access(application, client, access):
    material, _ = await publish(
        application,
        client,
        access=access,
        password="material-password" if access == "password" else None,
    )
    response = await client.get(
        material["stable_url"],
        headers={"User-Agent": "UnknownPreviewBot/1.0"},
        follow_redirects=False,
    )
    if access == "link":
        from browser_navigation import opening_target
        assert response.status_code == 200 and opening_target(response)
        assert Tags(response.text).meta["og:title"] == "A result"
    elif access == "password":
        assert response.status_code == 200
        assert Tags(response.text).meta["og:title"] == "Something worth opening."
    else:
        assert response.status_code == 200 and "Owner sign-in" in response.text
    assert "Private result" not in response.text
    if access != "link":
        assert "A result" not in response.text


@pytest.mark.parametrize(
    "origin", ["https://notes.example.org", "https://publish.other.test:8443"]
)
def test_metadata_assets_use_each_instances_configured_origin(tmp_path, origin):
    from minder_publish.server.config import Config
    from minder_publish.server.previews import metadata

    config = Config(tmp_path, base_url=origin, testing=True)
    tags = Tags(metadata(config)).meta
    assert tags["og:image"] == asset_url(origin, "link-preview.png")
    assert tags["og:url"] == origin

@pytest.mark.parametrize('language,theme', [('en','system'),('ru','dark'),('es','light'),('fr','system')])
async def test_opening_fallback_is_unconditional_styled_and_localized(application, client, language, theme):
    import base64
    import hashlib
    import re
    from browser_navigation import continuation_target
    material, _ = await publish(application, client, access='link')
    client.cookies.set('mp_lang', language)
    client.cookies.set('mp_theme', theme)
    response = await client.get(material['stable_url'], follow_redirects=False)
    assert continuation_target(response)
    assert '<meta http-equiv="refresh" content="2;url=' in response.text
    assert '<noscript>' not in response.text
    assert f'<html lang="{language}" data-theme="{theme}">' in response.text
    style = re.search(r'<style>(.*?)</style>', response.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    csp = response.headers['content-security-policy']
    assert "style-src 'sha256-" + digest + "'" in csp
    assert "'unsafe-inline'" not in csp

async def test_opening_escapes_labels_and_fallback_target(application, client):
    from minder_publish.server.previews import opening_page
    from browser_navigation import Continuation
    material, _ = await publish(application, client, access='link')
    target = 'https://r-example.publish.test/__mp/start?intent=token&x="<tag>"'
    page = opening_page(application.state.config, material, target,
                        language='bad"lang', theme='bad"theme',
                        loading_label='<Loading & wait>', open_label='<Open>')
    body = page.body.decode()
    parsed = Continuation(); parsed.feed(body)
    assert parsed.refresh == parsed.link == target
    assert '<html lang="en" data-theme="system">' in body
    assert '&lt;Loading &amp; wait&gt;' in body and '&lt;Open&gt;' in body
    assert '<Loading & wait>' not in body and '<Open>' not in body
