"""Release checks for the packaged, CSP-compatible and localized cabinet assets."""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "minder_publish" / "server" / "web"


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.messages = []
        self.inline_handlers = []
        self.scripts = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        for name in ("src", "href"):
            value = attributes.get(name, "")
            if value.startswith("/static/"):
                self.references.append(value.removeprefix("/static/"))
        if "data-i18n" in attributes:
            self.messages.append(attributes["data-i18n"])
        self.inline_handlers.extend(
            name for name in attributes if name.startswith("on")
        )
        if tag == "script":
            self.scripts.append(attributes)


def test_catalog_covers_static_and_literal_dynamic_messages():
    messages = json.loads((WEB / "messages.json").read_text())
    parser = AssetParser()
    for entry in WEB.glob("*.html"):
        parser.feed(entry.read_text())
    required = set(parser.messages)
    for path in WEB.glob("*.js"):
        required.update(re.findall(r"\b(?:t|label)\('([^']+)'", path.read_text()))
    assert required <= messages.keys(), required - messages.keys()
    for source, translations in messages.items():
        assert set(translations) == {"en", "ru", "es", "fr"}
        assert translations["en"] == source
        assert all(
            isinstance(value, str) and value.strip() for value in translations.values()
        )
        placeholders = set(re.findall(r"\{\w+\}", source))
        assert all(
            set(re.findall(r"\{\w+\}", value)) == placeholders
            for value in translations.values()
        )


@pytest.mark.parametrize("entry", ["index.html", "viewer.html"])
def test_entry_uses_packaged_resources_and_external_scripts_only(entry):
    parser = AssetParser()
    markup = (WEB / entry).read_text()
    parser.feed(markup)
    assert '<html lang="en"' in markup
    assert not parser.inline_handlers
    assert all(
        script.get("src", "").startswith("/static/") for script in parser.scripts
    )
    assert any(script.get("src") == "/static/controls.js" and "defer" in script for script in parser.scripts)
    assert all((WEB / resource).is_file() for resource in parser.references)
    for font in (WEB / "fonts").glob("*.ttf"):
        assert (font.parent / f"{font.stem}-OFL.txt").is_file()
    assert "http://" not in markup and "https://" not in markup


def test_browser_storage_is_limited_to_non_authentication_preferences():
    sources = "\n".join(path.read_text() for path in WEB.glob("*.js"))
    assert "localStorage" not in sources and "sessionStorage" not in sources
    assert "publication-password" not in sources and "contract.fixture" not in sources
    assert "X-CSRF-Token" in sources
    assert "credentials:'same-origin'" in sources
    assert "allow-top-navigation" not in sources


def test_theme_roles_keep_text_and_control_contrast_in_both_schemes():
    theme = (WEB / "theme.css").read_text()
    palette = dict(re.findall(r"--([\w-]+):\s*#([\da-f]{6});", theme, re.I))
    dark = dict(re.findall(r"--([\w-]+):\s*var\(--(dark-[\w-]+)\);", theme))

    def luminance(value):
        channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [
            channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return sum(component * weight for component, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    text_pairs = [
        (foreground, background)
        for foreground in ("ink", "muted", "accent")
        for background in ("canvas", "surface", "surface2", "softAccent")
    ] + [("accentInk", "accent"), ("danger", "dangerBg"), ("warning", "warningBg")]
    control_pairs = [("field-line", "canvas"), ("field-line", "surface")]
    for scheme, roles in (("light", {}), ("dark", dark)):
        for pairs, minimum in ((text_pairs, 4.5), (control_pairs, 3)):
            for first, second in pairs:
                low, high = sorted(luminance(palette[roles.get(role, role)]) for role in (first, second))
                ratio = (high + 0.05) / (low + 0.05)
                assert ratio >= minimum, (scheme, first, second, ratio, minimum)


@pytest.mark.skipif(
    not shutil.which("node"), reason="Node is an optional asset syntax checker"
)
def test_javascript_module_syntax():
    for path in WEB.glob("*.js"):
        subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=path.read_text(),
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.mark.skipif(
    not shutil.which("node"), reason="Node is required for the UI state regression"
)
def test_delayed_token_response_never_reopens_secrets_after_owner_state_changes():
    source = (WEB / "app.js").read_text()
    harness = "const source = " + json.dumps(source) + ";\n" + r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const start = source.indexOf('function createTokenDialog()');
const end = source.indexOf('async function confirmAction(', start);
assert(start >= 0 && end > start);
const code = source.slice(start, end);
async function scenario(nextSession,failed=false,closed=false) {
  let submitHandler, resolveToken, rejectToken;
  const calls = [];
  let connectionRenders = 0;
  let errorRenders = 0;
  const owner = {authenticated:true};
  const form = {reportValidity:()=>true, elements:{name:{value:'device'}, scope:{value:'materials:manage'}}};
  const context = {
    state:{session:owner,dialogEpoch:0}, t:x=>x, label:x=>x, escape:x=>x,
    field:()=>'', select:()=>'', button:()=>'', setupCommand:()=>'',
    dialog:(title,body)=>{context.state.dialogEpoch++;calls.push({title,secret:body.includes('test-only-secret')});},
    $:selector=>selector==='#token-form'
      ? {addEventListener:(type,handler)=>{submitHandler=handler;}}
      : {disabled:false},
    api:()=>new Promise((resolve,reject)=>{resolveToken=resolve;rejectToken=reject;}),
    renderConnection:async()=>{connectionRenders++;},
    showError:()=>{errorRenders++;}
  };
  vm.createContext(context);
  vm.runInContext(code,context);
  context.createTokenDialog();
  const pending = submitHandler({preventDefault(){},currentTarget:form});
  if(nextSession !== null)context.state.session=nextSession;
  if(closed)context.state.dialogEpoch++;
  if(failed)rejectToken(new Error('Expired request'));
  else resolveToken({token:'test-only-secret'});
  await pending;
  return {secretDialogs:calls.filter(call=>call.secret).length,connectionRenders,errorRenders};
}
(async()=>{
  const ignored={secretDialogs:0,connectionRenders:0,errorRenders:0};
  assert.deepEqual(await scenario({authenticated:false}),ignored);
  assert.deepEqual(await scenario({authenticated:true}),ignored);
  assert.deepEqual(await scenario({authenticated:false},true),ignored);
  assert.deepEqual(await scenario({authenticated:true},true),ignored);
  assert.deepEqual(await scenario(null,false,true),ignored);
  assert.deepEqual(await scenario(null,true,true),ignored);
  assert.deepEqual(await scenario(null),{secretDialogs:1,connectionRenders:1,errorRenders:0});
  assert.deepEqual(await scenario(null,true),{secretDialogs:0,connectionRenders:0,errorRenders:1});
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node"], input=harness, text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
