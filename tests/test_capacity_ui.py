"""Exercise the capacity disclosure and owner snapshot lifecycle."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_capacity_uses_highest_ratio_and_keeps_refresh_scoped_to_owner():
    source = (WEB / "capacity.js").read_text().replace("export function", "function")
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');

class Element {
  handlers = {};
  attributes = {};
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  focus() { this.focused = true; }
  contains(element) { return element === this; }
}

async function verify() {
  const root = new Element();
  const children = new Map();
  root.querySelector = selector => {
    if (!children.has(selector)) children.set(selector, new Element());
    return children.get(selector);
  };
  const field = key => root.querySelector(`[data-capacity="${key}"]`);
  root.contains = element => element === root || [...children.values()].includes(element);
  const document = new Element();
  document.documentElement = {lang: 'en'};
  let now = 100000;
  const context = {document, Date: {now: () => now}};
  vm.createContext(context);
  vm.runInContext(SOURCE, context);

  const settings = (used, count) => ({
    limits: {publication_count: 10},
    storage: {used_bytes: used, quota_bytes: 100, publication_count: count, reserved_bytes: 2}
  });
  let snapshot = settings(60, 3);
  let release;
  let requests = 0;
  const capacity = context.mountCapacity({
    root,
    t: (key, values = {}) => key.replace('{time}', values.time ?? ''),
    bytes: value => `${value} B`,
    getSettings: () => snapshot,
    refreshSettings: isCurrent => new Promise(resolve => {
      requests++;
      release = next => {
        if (isCurrent()) snapshot = next;
        resolve();
      };
    })
  });

  capacity.render(snapshot);
  assert.equal(root.hidden, false);
  assert.equal(root.querySelector('summary').getAttribute('aria-label'), '60% Usage');
  assert.equal(root.querySelector('.capacity-fill').getAttribute('stroke-dasharray'), '60 100');
  assert.equal(field('storage').textContent, '60 B / 100 B');
  assert.equal(field('records').textContent, '3 / 10');
  assert.equal(field('reserved').textContent, '2 B');
  assert.match(field('updated').textContent, /^Updated /);

  function openDetails() {
    root.open = true;
    root.handlers.toggle();
  }
  openDetails();
  assert.equal(requests, 0, 'opening a fresh snapshot should not rescan storage');
  let prevented = false;
  root.handlers.keydown({key: 'Escape', preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(root.open, false);
  assert.equal(root.querySelector('summary').focused, true);
  now += 30001;
  openDetails();
  assert.equal(field('refresh').getAttribute('aria-disabled'), 'true');
  await field('refresh').handlers.click();
  assert.equal(requests, 1, 'opening and clicking while pending should share one request');
  release(settings(35, 8));
  await new Promise(setImmediate);
  assert.equal(root.querySelector('.capacity-percent').textContent, '80%');
  assert.equal(field('records').textContent, '8 / 10');
  assert.equal(field('refresh').getAttribute('aria-disabled'), undefined);

  document.handlers.focusin({target: field('refresh')});
  assert.equal(root.open, true);
  document.handlers.focusin({target: {contains: element=>element===root}});
  assert.equal(root.open, true, 'containing landmark focus must preserve the disclosure');
  document.handlers.focusin({target: new Element()});
  assert.equal(root.open, false);
  capacity.invalidate();
  openDetails();
  assert.equal(requests, 2, 'publication invalidation should refresh without waiting 30 seconds');
  snapshot = null;
  capacity.render(null);
  release(settings(99, 9));
  await new Promise(setImmediate);
  assert.equal(snapshot, null, 'old request cannot restore signed-out owner data');
  assert.equal(root.hidden, true);
  assert.equal(root.open, false);
}

verify().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SOURCE", json.dumps(source), 1)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
