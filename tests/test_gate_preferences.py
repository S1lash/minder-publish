"""Guest preference controls submit their real form without changing reader context."""

import io
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_http import publish

pytest_plugins = ("test_http",)


class GateForms(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.forms = []
        self.summaries = []
        self.details = []
        self.scripts = []
        self.links = []
        self.current_form = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.current_form = {"attributes": attributes, "hidden": {}, "buttons": []}
            self.forms.append(self.current_form)
        elif (
            tag == "input"
            and self.current_form
            and attributes.get("type") == "hidden"
        ):
            self.current_form["hidden"][attributes["name"]] = attributes["value"]
        elif tag == "button" and self.current_form:
            self.current_form["buttons"].append(attributes)
        elif tag == "summary":
            self.summaries.append(attributes)
        elif tag == "details":
            self.details.append(attributes)
        elif tag == "script":
            self.scripts.append(attributes)
        elif tag == "a":
            self.links.append((attributes, self.current_form is not None))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current_form = None

    def preference_form(self, field):
        return next(
            form
            for form in self.forms
            if any(button.get("name") == field for button in form["buttons"])
        )


PROJECT_LABEL = "Made available via minder publish."


def source_footer_markup(response):
    return response.text.split('<footer class="source-footer">')[1].split("</footer>")[0]


async def test_project_attribution_replaces_source_link_on_all_guest_surfaces(tmp_path, monkeypatch):
    import httpx

    from minder_publish.server.app import create_app
    from minder_publish.server.config import Config

    source = "https://github.com/example/publish"
    project = "https://example.org/publish/"
    monkeypatch.setenv("PUBLISH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_BASE_URL", "https://publish.test")
    monkeypatch.setenv("PUBLISH_SECRET", "test-only-secret-" * 4)
    monkeypatch.setenv("PUBLISH_SOURCE_URL", source)
    monkeypatch.setenv("PUBLISH_PROJECT_URL", project)
    app = create_app(Config.from_env())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://publish.test",
    ) as client:
        for path in ("/", "/login"):
            response = await client.get(path)
            assert response.status_code == 200
            footer = source_footer_markup(response)
            assert "GitHub" not in footer and source not in footer
            assert f'href="{project}"' in footer
            assert f'data-i18n="{PROJECT_LABEL}">{PROJECT_LABEL}<' in footer
        material, _ = await publish(app, client, access="password", password="gate-password")
        client.cookies.set("mp_lang", "ru")
        gate = await client.get(urlsplit(material["stable_url"]).path)
        assert gate.status_code == 200
        assert "location" not in gate.headers
        assert "GitHub" not in source_footer_markup(gate)
        assert 'Доступно через minder publish.' in gate.text
        project_link, = GateForms(gate.text).links
        assert project_link == ({
            "class": "project-attribution",
            "href": project,
            "referrerpolicy": "no-referrer",
            "data-i18n": PROJECT_LABEL,
        }, False)
        assert gate.headers["Referrer-Policy"] == "same-origin"
        policy = gate.headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "form-action 'self'" in policy
        assert project not in policy


@pytest.mark.parametrize("url", [
    "http://example.org/",
    "//example.org/",
    "https://user@example.org/",
    "https://example.org/path?",
    "https://example.org/path#",
    "https://example.org:bogus/",
    "https://example.org:/",
    "https://example.org/space here",
    "https://example.org\\evil.org/",
    "https://publish.localhost/",
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://host/",
    "https://%65xample.org/",
])
def test_project_url_requires_public_https_shape(tmp_path, url):
    from minder_publish.errors import PublishError
    from minder_publish.server.config import Config

    with pytest.raises(PublishError, match="public HTTPS project site") as raised:
        Config(data_dir=tmp_path, project_url=url)
    assert raised.value.field == "PUBLISH_PROJECT_URL"


@pytest.mark.parametrize("guest", [False, True])
@pytest.mark.parametrize(
    "language,theme", [("fr", "dark"), ("es", "light"), ("ru", "system")]
)
async def test_gate_corner_preferences_preserve_other_choice_and_reader_realm(
    application, client, guest, language, theme
):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    client.cookies.set("mp_lang", language, domain="publish.test", path="/")
    client.cookies.set("mp_theme", theme, domain="publish.test", path="/")
    target = urlsplit(material["stable_url"]).path + ("?guest=1" if guest else "")
    gate = await client.get(target)
    forms = GateForms(gate.text)
    assert len(forms.details) == len(forms.summaries) == 1
    assert all(summary.get("aria-label") for summary in forms.summaries)
    assert "<select" not in gate.text
    footer = source_footer_markup(gate)
    catalog_path = Path(__file__).parents[1] / "src/minder_publish/server/web/messages.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert "GitHub" not in footer
    assert catalog[PROJECT_LABEL][language] in footer
    assert forms.links == [({
        "class": "project-attribution",
        "href": application.state.config.control_origin + "/",
        "referrerpolicy": "no-referrer",
        "data-i18n": PROJECT_LABEL,
    }, False)]
    assert forms.scripts == [
        {"type": "module", "src": "/static/gate-preferences.js"}
    ]
    assert "default-src 'none'" in gate.headers["content-security-policy"]
    script_policy = next(
        value.strip()
        for value in gate.headers["content-security-policy"].split(";")
        if value.strip().startswith("script-src ")
    )
    assert script_policy == "script-src 'self'"
    assert "form-action 'self'" in gate.headers["content-security-policy"]
    assert gate.headers["referrer-policy"] == "same-origin"
    assert 'type="password"' in gate.text

    language_form = forms.preference_form("language")
    theme_form = forms.preference_form("theme")
    selected = [button["value"] for button in language_form["buttons"] if button.get("aria-current") == "true"]
    assert selected == [language]
    for form in (language_form, theme_form):
        assert form["attributes"]["action"] == "/p/_preferences"
        assert form["attributes"]["method"] == "post"
        assert form["hidden"]["return_to"] == target
    assert {button["value"] for button in theme_form["buttons"]} == {"light", "dark"}
    assert all(button.get("aria-label") for button in theme_form["buttons"])

    changed_language = await client.post(
        language_form["attributes"]["action"],
        data={**language_form["hidden"], "language": "en"},
        headers={"Origin": application.state.config.control_origin},
        follow_redirects=False,
    )
    assert changed_language.status_code == 303
    assert (
        changed_language.headers["location"]
        == application.state.config.base_url + target
    )
    assert client.cookies.get("mp_lang") == "en"
    assert client.cookies.get("mp_theme") == theme
    translated_gate = await client.get(changed_language.headers["location"])
    assert 'lang="en"' in translated_gate.text
    assert f'data-theme="{theme}"' in translated_gate.text
    assert 'type="password"' in translated_gate.text

    current_theme_form = GateForms(translated_gate.text).preference_form("theme")
    changed_theme = await client.post(
        current_theme_form["attributes"]["action"],
        data={**current_theme_form["hidden"], "theme": "dark"},
        headers={"Origin": application.state.config.control_origin},
        follow_redirects=False,
    )
    assert changed_theme.status_code == 303
    assert changed_theme.headers["location"] == application.state.config.base_url + target
    assert client.cookies.get("mp_lang") == "en"
    assert client.cookies.get("mp_theme") == "dark"


async def test_gate_binary_theme_fallback_supports_system_and_explicit_choices(application, client):
    material, _ = await publish(application, client, access="password", password="material-password")
    for theme in ("system", "light", "dark"):
        client.cookies.set("mp_theme", theme, domain="publish.test", path="/")
        gate = await client.get(material["stable_url"])
        form = GateForms(gate.text).preference_form("theme")
        assert {button["value"] for button in form["buttons"]} == {"light", "dark"}
        assert all(button.get("aria-label") for button in form["buttons"])
        assert 'prefers-color-scheme:light' in gate.text
        assert 'prefers-color-scheme:dark' in gate.text
        assert 'data-theme="' + theme + '"' in gate.text
        assert "form-action 'self'" in gate.headers["content-security-policy"]


async def test_gate_enhancement_asset_stays_on_control_origin(application, client):
    script = await client.get("/static/gate-preferences.js")
    assert script.status_code == 200
    assert "mountGatePreferences" in script.text
    assert "javascript" in script.headers["content-type"]
    archive = io.BytesIO()
    author_script = b"window.authorGatePreferencesFixture=true;"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("index.html", "<h1>Author site</h1>")
        bundle.writestr("static/gate-preferences.js", author_script)
    material, _ = await publish(
        application, client, access="link", kind="site", body=archive.getvalue(),
        filename="site.zip",
    )
    origin = application.state.config.content_origin(material["current_revision"])
    denied = await client.get(origin + "/static/gate-preferences.js")
    assert denied.status_code == 403 and "mountGatePreferences" not in denied.text
    opened = await client.get(material["stable_url"])
    assert opened.content == b"<h1>Author site</h1>"
    author = await client.get(origin + "/static/gate-preferences.js")
    assert author.status_code == 200 and author.content == author_script
    assert "mountGatePreferences" not in author.text


@pytest.mark.parametrize("path", ["/", "/login"])
async def test_guest_shell_omits_attribution_without_configured_destination(client, path):
    response = await client.get(path)
    assert response.status_code == 200
    assert '<footer class="source-footer">' not in response.text
    if path == "/":
        assert "Shared files and sites" in response.text
        assert "Open the link you received to view a material." in response.text
