"""Canonical link copying uses a fresh response and discards a closed context."""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for link UI checks")
def test_canonical_copy_retains_gesture_and_falls_back_to_the_fresh_address():
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WEB / "link-copy.js")],
        input=r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[2], 'utf8');
const {copyResolvedLink} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

const canonicalURL = 'https://publish.test/p/renamed';
let active = true;
let resolveURL;
const pendingURL = new Promise(resolve => { resolveURL = resolve; });
const events = [];
globalThis.ClipboardItem = class { constructor(data) { this.data = data; } };
Object.defineProperty(globalThis, 'navigator', {value: {clipboard: {
  async write([item]) {
    events.push('asked-for-clipboard');
    const blob = await item.data['text/plain'];
    events.push(await blob.text());
  },
  async writeText() { throw new Error('clipboard denied'); }
}}});

const copy = load => copyResolvedLink({
  load,
  isCurrent: () => active,
  onCopied: () => events.push('copied'),
  fallback: url => events.push('manual:' + url)
});
const pendingCopy = copy(() => pendingURL);
assert.deepEqual(events, ['asked-for-clipboard']);
resolveURL(canonicalURL);
await pendingCopy;
assert.deepEqual(events, ['asked-for-clipboard', canonicalURL, 'copied']);

events.length = 0;
const closedCopy = copy(async () => canonicalURL);
active = false;
await closedCopy;
assert.deepEqual(events, ['asked-for-clipboard']);

events.length = 0;
active = true;
navigator.clipboard.write = async () => { throw new Error('clipboard denied'); };
await copy(async () => canonicalURL);
assert.deepEqual(events, ['manual:' + canonicalURL]);

events.length = 0;
await assert.rejects(copy(async () => { throw new Error('API error'); }), /API error/);
assert.deepEqual(events, []);
""",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for link UI checks")
def test_renaming_from_owner_keeps_the_selected_revision_and_fragment():
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WEB / "viewer.js")],
        input=r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(process.argv[2], 'utf8');
const pick = (first, last) => source.slice(source.indexOf(first), source.indexOf(last));
const path = 'https://publish.test/p/current-address';
const selected = 'a'.repeat(32);
const newest = 'b'.repeat(32);

function check(location, initialRevision, expectedRevision) {
  const context = {
    URL, URLSearchParams, location: new URL(location), initialRevision,
    history: {replaceState(_state, _title, url) {
      assert.equal(url.pathname, '/p/current-address');
      assert.equal(url.hash, '#heading');
      assert.equal(url.searchParams.get('preview'), 'tools');
      assert.equal(url.searchParams.get('revision'), expectedRevision);
    }},
  };
  vm.createContext(context);
  vm.runInContext(
    pick('function explicitRevision()', 'function originalURL(')
      + pick('function replaceMaterialLinkURL(', 'function updateMaterialLink('),
    context
  );
  context.replaceMaterialLinkURL(path, expectedRevision ?? newest, newest);
  assert.equal(context.initialRevision, null);
}
check('https://publish.test/p/id~1?preview=tools#heading', selected, selected);
check('https://publish.test/p/old?preview=tools&revision=' + selected + '#heading', null, selected);
check('https://publish.test/p/old?preview=tools#heading', null, null);
""",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for link UI checks")
def test_preview_selector_distinguishes_inheritance_and_keeping_destination_policy():
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WEB / "preview-policy.js")],
        input=r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source = readFileSync(process.argv[2], 'utf8');
const {previewModeOptions} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const identity = value => value;
const options = (mode, enabled, settings) => previewModeOptions(identity, identity, mode, enabled, settings);
assert.match(options('inherit', true), /value="inherit" selected>Use instance default \(on\)/);
assert.match(options('inherit', false), /value="inherit" selected>Use instance default \(off\)/);
assert.match(options('on', false), /value="on" selected>On/);
assert.match(options('off', true), /value="off" selected>Off/);
assert.doesNotMatch(options('off', true), /Keep address setting/);
assert.match(options('', true, {renaming: true}), /^<option value="" selected>Keep address setting/);
assert.match(options('inherit', true, {renaming: true}), /value="inherit" selected>/);
""",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for link UI checks")
def test_preview_only_save_returns_to_cabinet_after_success():
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WEB)],
        input=r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import vm from 'node:vm';

const tick = () => new Promise(resolve => setImmediate(resolve));
const identity = value => value;
const session = {authenticated: true};
const snapshot = {
  id: 'sample', link_name: 'sample', stable_url: 'https://publish.test/p/sample',
  preview_default: true, preview_mode: 'inherit', previous_links: []
};

async function setup({cabinet = true, fail = false} = {}) {
  let focused;
  class Element {
    constructor() {
      this.events = {};
      this.nodes = new Map();
      this.disabled = false;
      this.isConnected = true;
      this.parentElement = {hidden: false};
    }
    querySelector(selector) {
      if (!this.nodes.has(selector)) this.nodes.set(selector, new Element());
      return this.nodes.get(selector);
    }
    closest() { return this.parentElement; }
    addEventListener(type, handler) { this.events[type] = handler; }
    setAttribute() {}
    removeAttribute() {}
    focus() { focused = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    remove() { this.isConnected = false; }
  }
  const parent = new Element();
  parent.open = true;
  const source = new Element();
  source.parentElement = parent;
  const dialog = new Element();
  const field = selector => dialog.querySelector(selector);
  const form = field('form');
  const input = new Element();
  const keep = new Element();
  const mode = new Element();
  const copy = field('[data-link-copy]');
  const save = field('[type=submit]');
  form.elements = Object.assign([input, keep, mode, copy, save, field('[data-link-close]')], {
    name: input, keep_previous: keep, preview_mode: mode,
  });
  const context = vm.createContext({
    URL, queueMicrotask, location: new URL('https://publish.test/'),
    document: {createElement: () => dialog, body: {append() {}}},
  });
  for (const file of ['preview-policy.js', 'publication-link.js']) {
    vm.runInContext(readFileSync(join(process.argv[2], file), 'utf8')
      .replace(/^import .*;\n/gm, '').replace(/^export /gm, ''), context);
  }
  const changes = [], notifications = [];
  const editor = context.mountPublicationLink({
    api: async (_path, options) => {
      if (!options) return snapshot;
      if (fail) throw new Error('Try again');
      const {name, preview_mode} = options.body;
      return {...snapshot, link_name: name, stable_url: 'https://publish.test/p/' + name,
        preview_mode: preview_mode ?? snapshot.preview_mode};
    },
    getSession: () => session, t: identity, escape: identity,
    onError: error => {throw error;}, errorMessage: error => error.message,
    ...(cabinet ? {onPreviewSaved: surface => notifications.push(surface)} : {}),
  });
  editor.open(snapshot.id, source, value => changes.push(value));
  await tick();
  return {parent, source, dialog, field, form, input, mode, copy, changes, notifications,
    focused: () => focused,
    submit: async () => {form.events.submit({preventDefault() {}}); await tick();}};
}

const saved = await setup();
saved.mode.value = 'off';
saved.mode.events.change();
await saved.submit();
assert.equal(saved.changes.at(-1).preview_mode, 'off');
assert.equal(saved.dialog.open, false, 'successful cabinet save closes the editor');
assert.equal(saved.parent.open, true);
assert.equal(saved.focused(), saved.source);
assert.deepEqual(saved.notifications, [saved.parent]);

const renamed = await setup();
renamed.input.value = 'named-sample';
renamed.input.events.input();
await renamed.submit();
assert.equal(renamed.dialog.open, true);
assert.equal(renamed.copy.hidden, false);
assert.equal(renamed.focused(), renamed.copy);

const failed = await setup({fail: true});
failed.mode.value = 'off';
failed.mode.events.change();
await failed.submit();
assert.equal(failed.dialog.open, true);
assert.equal(failed.mode.value, 'off');
assert.equal(failed.focused(), failed.field('[data-link-error]'));
assert.equal(failed.notifications.length, 0);

const viewer = await setup({cabinet: false});
viewer.mode.value = 'off';
viewer.mode.events.change();
await viewer.submit();
assert.equal(viewer.dialog.open, true);
assert.equal(viewer.field('[data-link-status]').textContent, 'Link preview setting saved.');
""",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
