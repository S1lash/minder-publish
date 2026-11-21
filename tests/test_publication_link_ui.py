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
