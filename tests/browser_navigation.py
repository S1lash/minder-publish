"""Follow real server-owned HTML continuation semantics in ASGI browser tests."""

import base64
import hashlib
from html.parser import HTMLParser
from urllib.parse import urlsplit


class Continuation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refresh = None
        self.link = None
        self.scripts = []
        self.inside_script = False
        self.inside_noscript = False
        self.refresh_in_noscript = False
        self.delay = None

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        assert tag != "form"
        if tag == "script":
            self.inside_script = True
            self.scripts.append("")
        if tag == "noscript":
            self.inside_noscript = True
        if tag == "meta" and attributes.get("http-equiv") == "refresh":
            delay, separator, target = attributes["content"].partition(";url=")
            assert separator and delay in ("0", "2")
            self.delay = delay
            self.refresh = target
            self.refresh_in_noscript = self.inside_noscript
        if tag == "a":
            assert "noreferrer" in attributes.get("rel", "").split()
            self.link = attributes["href"]

    def handle_data(self, data):
        if self.inside_script:
            self.scripts[-1] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside_script = False
        if tag == "noscript":
            self.inside_noscript = False


def continuation_target(response):
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "form-action 'none'" in response.headers["content-security-policy"]
    assert "location" not in response.headers
    parser = Continuation()
    parser.feed(response.text)
    assert parser.refresh and parser.refresh == parser.link
    if parser.scripts:
        assert len(parser.scripts) == 1
        assert (parser.delay, parser.refresh_in_noscript) == ("2", False)
        digest = base64.b64encode(
            hashlib.sha256(parser.scripts[0].encode()).digest()
        ).decode()
        assert (
            "script-src 'sha256-" + digest + "'"
            in response.headers["content-security-policy"]
        )
        assert "'unsafe-inline'" not in response.headers["content-security-policy"]
        assert "'unsafe-eval'" not in response.headers["content-security-policy"]
    else:
        assert not parser.refresh_in_noscript
    return parser.refresh


def opening_target(response):
    """Validate the public opening page before simulating a browser refresh."""
    if (response.status_code != 200 or not urlsplit(str(response.url)).path.startswith("/p/")
            or 'id="mp-continue"' not in response.text or '<meta http-equiv="refresh"' not in response.text):
        return None
    class Opening(HTMLParser):
        def __init__(self):
            super().__init__()
            self.refresh = self.link = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and attrs.get("http-equiv") == "refresh":
                assert attrs["content"].startswith("2;url=")
                self.refresh = attrs["content"][6:]
            if tag == "a":
                self.link = attrs.get("href")
    parsed = Opening()
    parsed.feed(response.text)
    assert parsed.refresh == parsed.link and parsed.refresh
    target = urlsplit(parsed.refresh)
    assert target.path == "/__mp/start" and target.query.startswith("intent=")
    assert target.scheme in ("http", "https")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    return parsed.refresh


async def navigation_target(client, response):
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert urlsplit(location).netloc == client.base_url.netloc.decode()
    assert urlsplit(location).path == "/__mp/continue"
    page = await client.get(location, follow_redirects=False)
    return continuation_target(page)


async def follow_navigation(client, response):
    if urlsplit(str(response.url)).path == "/__mp/continue":
        target = continuation_target(response)
    else:
        target = await navigation_target(client, response)
    return await client.get(target)
