"""Credential-free link cards derived only from guest-visible publication policy."""

from __future__ import annotations

import base64
import hashlib
import html
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from starlette.responses import HTMLResponse

from minder_publish.errors import PublishError

from .brand import LOCKUP, SYSTEM_CSS, THEME_CSS
from .core import guest_available
from .history import retained_available
from .navigation import PUBLICATION_NAVIGATION_SCRIPT, PUBLICATION_SCRIPT_HASH
from .publication_links import stable_url

# User-Agent selects representation, never authority. Spoofing it cannot grant access.
_CRAWLERS = re.compile(
    r"(?:Slackbot-LinkExpanding|TelegramBot|Twitterbot|facebookexternalhit|LinkedInBot|Discordbot)",
    re.IGNORECASE,
)
_BRAND = "minder publish."
_GENERIC_CARD = (
    "Something worth opening.",
    "Ideas, documents and sites, shared with Minder Publish.",
)
_WEB = Path(__file__).parent / "web"
_CARD_SIZE = (1024, 1024)
_COVER_BOUNDS = (864, 736)
_TOUCH_ICON = "apple-touch-icon.png"
_PREVIEW_ASSET = "link-preview.png"
_ASSET_VERSIONS = {
    name: hashlib.sha256((_WEB / name).read_bytes()).hexdigest()[:16]
    for name in (_PREVIEW_ASSET, _TOUCH_ICON, "mark.svg")
}
_OPENING_STYLES = THEME_CSS + SYSTEM_CSS + """
@font-face{font-family:Commissioner;src:url('/static/fonts/commissioner.ttf') format('truetype');font-weight:100 900;font-display:swap}
@font-face{font-family:Golos;src:url('/static/fonts/golostext.ttf') format('truetype');font-weight:100 900;font-display:swap}
.system-card{text-align:center}
.brand{justify-content:center}
h1{font:500 17px/1.4 var(--ui-font),system-ui,sans-serif;color:var(--muted);margin:20px 0}
.system-action{min-width:112px}
"""
_OPENING_STYLE_HASH = base64.b64encode(
    hashlib.sha256(_OPENING_STYLES.encode()).digest()
).decode()


def _attribute(value):
    return html.escape(str(value), quote=True)


def _asset_url(config, name):
    return f"{config.base_url}/static/{name}?v={_ASSET_VERSIONS[name]}"


def favicon_links(config):
    icon = _attribute(_asset_url(config, "mark.svg"))
    touch_icon = _attribute(_asset_url(config, _TOUCH_ICON))
    return (
        f'<link rel="icon" type="image/svg+xml" href="{icon}">\n'
        f'<link rel="apple-touch-icon" sizes="180x180" href="{touch_icon}">'
    )


def is_preview_request(request):
    return request.method in ("GET", "HEAD") and bool(
        _CRAWLERS.search(request.headers.get("user-agent", ""))
    )


def public_metadata(publication):
    return bool(
        publication and publication["access"] == "link" and guest_available(publication)
    )


def card_text(publication=None, *, gate=False):
    if gate or (publication and publication["access"] in ("password", "private")):
        return _GENERIC_CARD
    if publication is None:
        return (
            "Files, documents and sites",
            "Open a material using the link shared with you.",
        )
    if not guest_available(publication):
        return "Material unavailable", "This material is not currently available."
    if public_metadata(publication):
        return (
            publication["title"] or "Published material",
            publication["summary"] or "Open this material.",
        )
    return _GENERIC_CARD


def cover_url(config, core, publication, revision=None, sequence=None, *, link_name=None):
    selected = revision if revision is not None else publication
    if not public_metadata(publication) or selected["kind"] != "site":
        return None
    if revision is not None and not revision.get("shared", False):
        return None
    revision_id = revision["id"] if revision is not None else publication["current_revision"]
    if not (core.storage.revision(revision_id) / "thumbnail.jpg").is_file():
        return None
    identifier = quote(publication["id"], safe="")
    url = f"{config.base_url}/__mp/preview/{identifier}/{revision_id}.jpg"
    query = f"?v=2-{_ASSET_VERSIONS[_TOUCH_ICON]}"
    if sequence is not None:
        return url + query + f"&sequence={sequence}"
    return url + query + f"&name={quote(link_name, safe='')}" if link_name is not None else None


@lru_cache(maxsize=16)
def _compose_cover(thumbnail):
    from PIL import Image, ImageOps

    colors = dict(re.findall(r"--([\w-]+): (#[\da-fA-F]{6});", THEME_CSS))
    card = Image.new("RGB", _CARD_SIZE, colors["surface2"])
    with Image.open(BytesIO(thumbnail), formats=["JPEG"]) as picture:
        picture = ImageOps.contain(picture, _COVER_BOUNDS, Image.Resampling.LANCZOS).convert("RGB")
    left = (card.width - picture.width) // 2
    top = 80 + (_COVER_BOUNDS[1] - picture.height) // 2
    card.paste(picture, (left, top))

    with Image.open(_WEB / _TOUCH_ICON, formats=["PNG"]) as mark:
        mark = mark.resize((88, 88), Image.Resampling.LANCZOS)
        card.paste(mark, ((card.width - mark.width) // 2, 850))

    output = BytesIO()
    card.save(output, "JPEG", quality=86, optimize=True)
    return output.getvalue()


def public_cover(core, identifier, revision_id, sequence, *, link_name=None):
    unavailable = PublishError("thumbnail_not_found", 404)
    if (link_name is None) == (sequence is None):
        raise unavailable
    maximum = core.config.thumbnails["output_bytes"]
    # Bound the read and serialize it with retention and access transitions.
    with core.repository.connect(immediate=True) as connection:
        if not core.link_preview_enabled(
            link_name, publication_id=identifier, sequence=sequence, connection=connection
        ):
            raise unavailable
        publication = core._publication(connection, identifier)
        if not public_metadata(publication):
            raise unavailable
        if sequence is None:
            if publication["current_revision"] != revision_id:
                raise unavailable
        else:
            match = connection.execute(
                "SELECT revision_id FROM revision_history WHERE publication_id=? AND sequence=?",
                (identifier, sequence),
            ).fetchone()
            if not match or match["revision_id"] != revision_id:
                raise unavailable
        revision = core._revision(connection, revision_id, manifest=False)
        if (
            revision["kind"] != "site"
            or not retained_available(publication, revision)
            or (sequence is not None and not revision.get("shared", False))
        ):
            raise unavailable
        target = revision["path"] / "thumbnail.jpg"
        if target.is_symlink() or not target.is_file():
            raise unavailable
        try:
            with target.open("rb") as source:
                thumbnail = source.read(maximum + 1)
        except OSError:
            raise unavailable from None
        if not thumbnail or len(thumbnail) > maximum:
            raise unavailable
    try:
        return _compose_cover(thumbnail)
    except OSError:
        raise unavailable from None


def metadata(config, publication=None, *, gate=False, canonical=None, cover=None, enabled=True):
    if canonical is None:
        canonical = config.base_url
        if publication:
            canonical = stable_url(config.base_url, publication["link_name"])
    links = [f'<link rel="canonical" href="{_attribute(canonical)}">', favicon_links(config)]
    if not enabled:
        return "\n".join(links)
    title, description = card_text(publication, gate=gate)
    cover = cover if not gate and public_metadata(publication) else None
    image = cover or _asset_url(config, _PREVIEW_ASSET)
    width, height = _CARD_SIZE
    image_alt = f"{title} · {_BRAND}" if cover else _BRAND
    values = {
        "og:type": "website",
        "og:site_name": _BRAND,
        "og:title": title,
        "og:description": description,
        "og:url": canonical,
        "og:image": image,
        "og:image:type": "image/jpeg" if cover else "image/png",
        "og:image:width": width,
        "og:image:height": height,
        "og:image:alt": image_alt,
    }
    tags = [
        f'<meta property="{key}" content="{_attribute(value)}">'
        for key, value in values.items()
    ]
    tags.extend(
        f'<meta name="{key}" content="{_attribute(value)}">'
        for key, value in {
            "twitter:card": "summary",
            "twitter:title": title,
            "twitter:description": description,
            "twitter:image": image,
            "twitter:image:alt": image_alt,
        }.items()
    )
    return "\n".join(tags + links)


def preview_page(config, publication, *, canonical=None, cover=None):
    title, _ = card_text(publication)
    url = canonical or stable_url(config.base_url, publication["link_name"])
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + metadata(config, publication, canonical=canonical, cover=cover)
        + f"<title>{html.escape(title)}</title></head><body>"
        + f'<p>{_BRAND}</p><a href="{html.escape(url, quote=True)}">Open material</a>'
        + "</body></html>",
        headers={
            "Cache-Control": "no-store",
            "Vary": "User-Agent",
            "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )


def opening_page(
    config, publication, target, *, language="en", theme="system",
    loading_label="Loading…", open_label="Open",
    canonical=None, cover=None, preview_enabled=True,
):
    """Expose cards before cookie-bound navigation, including to unknown crawlers."""
    language = language if language in ("en", "ru", "es", "fr") else "en"
    theme = theme if theme in ("system", "light", "dark") else "system"
    title = card_text(publication)[0] if preview_enabled else f"{open_label} · {_BRAND}"
    return HTMLResponse(
        f'<!doctype html><html lang="{language}" data-theme="{theme}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + metadata(config, publication, canonical=canonical, cover=cover, enabled=preview_enabled)
        + f'<meta http-equiv="refresh" content="2;url={_attribute(target)}">'
        + f'<title>{html.escape(title)}</title>'
        + f'<style>{_OPENING_STYLES}</style>'
        + '</head><body><main class="system-card">'
        + LOCKUP
        + f'<h1>{html.escape(loading_label)}</h1>'
        + f'<a id="mp-continue" class="system-action" href="{_attribute(target)}" rel="noreferrer">{html.escape(open_label)}</a></main>'
        + f'<script>{PUBLICATION_NAVIGATION_SCRIPT}</script></body></html>',
        headers={
            "Cache-Control": "no-store",
            "Vary": "User-Agent, Cookie",
            "X-Robots-Tag": "index, follow" if public_metadata(publication) and publication.get("indexing") else "noindex, nofollow",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; font-src 'self'; style-src 'sha256-" + _OPENING_STYLE_HASH + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; script-src 'sha256-" + PUBLICATION_SCRIPT_HASH + "'",
            "Referrer-Policy": "no-referrer",
        },
    )
