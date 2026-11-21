"""Run the isolated activity controller in Node with a real EventTarget boundary."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_session_activity_requires_trusted_visible_activity_and_keeps_session_identity():
    node = shutil.which("node")
    if not node:
        pytest.skip(
            "Node is unavailable; browser activity behavior requires its own browser checks"
        )
    module = Path(__file__).parents[1] / "src/minder_publish/server/web/session.js"
    script = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');
const {mountSessionActivity}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Document extends EventTarget {
  visibilityState='visible';
  send(type,{trusted=true,repeat=false}={}) {
    const event=new Event(type);
    Object.defineProperty(event,'isTrusted',{value:trusted});
    Object.defineProperty(event,'repeat',{value:repeat});
    this.dispatchEvent(event);
  }
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
let now=1700000000000;
const fresh=()=>({authenticated:true,expires_at:now/1000+90*86400,absolute_expires_at:now/1000+365*86400,refresh_interval_seconds:900});
let current=null,calls=0,errors=[];
const document=new Document();
const stop=mountSessionActivity({document,clock:()=>now,getSession:()=>current,refresh:async()=>{calls++;return {expires_at:now/1000+90*86400+1};},onError:error=>errors.push(error)});
document.send('pointerdown');await flush();assert.equal(calls,0);
current=fresh();const same=current;
document.send('pointerdown',{trusted:false});await flush();assert.equal(calls,0);
document.visibilityState='hidden';document.send('keydown');await flush();assert.equal(calls,0);
document.visibilityState='visible';
for(const event of ['focus','mousemove','visibilitychange'])document.send(event);
await flush();assert.equal(calls,0);
document.send('pointerdown');await flush();assert.equal(calls,1);assert.equal(current,same);
assert.equal(current.expires_at,now/1000+90*86400+1);
document.send('click');document.send('change');await flush();assert.equal(calls,1);
now+=899999;document.send('keydown');await flush();assert.equal(calls,1);
now+=1;document.send('keydown',{repeat:true});await flush();assert.equal(calls,1);
document.send('keydown');await flush();assert.equal(calls,2);
stop();now+=900000;document.send('pointerdown');await flush();assert.equal(calls,2);

for(const invalid of [{expires_at:now/1000},{absolute_expires_at:now/1000},{refresh_interval_seconds:0}]){
  current={...fresh(),...invalid};let attempts=0;
  const doc=new Document();const dispose=mountSessionActivity({document:doc,clock:()=>now,getSession:()=>current,refresh:async()=>{attempts++;return {};},onError:assert.fail});
  doc.send('click');await flush();assert.equal(attempts,0);dispose();
}

let finish;calls=0;current=fresh();const old=current;const original=old.expires_at;
const doc2=new Document();
const dispose2=mountSessionActivity({document:doc2,clock:()=>now,getSession:()=>current,refresh:()=>{calls++;return new Promise(resolve=>{finish=resolve;});},onError:assert.fail});
doc2.send('click');doc2.send('keydown');await flush();assert.equal(calls,1);
current=fresh();const replacement=current;finish({expires_at:1,authenticated:false});await flush();
assert.equal(current,replacement);assert.equal(current.authenticated,true);assert.equal(old.expires_at,original);
dispose2();

let fail;current=fresh();errors=[];const doc3=new Document();
const dispose3=mountSessionActivity({document:doc3,clock:()=>now,getSession:()=>current,refresh:()=>new Promise((resolve,reject)=>{fail=reject;}),onError:error=>errors.push(error)});
doc3.send('click');await flush();current=fresh();fail(new Error('stale unauthorized response'));await flush();assert.equal(errors.length,0);
doc3.send('click');await flush();fail(new Error('current refresh failed'));await flush();assert.equal(errors.length,1);
dispose3();

let resolveDisposed;current=fresh();const beforeDispose=current.expires_at;const doc4=new Document();
const dispose4=mountSessionActivity({document:doc4,clock:()=>now,getSession:()=>current,refresh:()=>new Promise(resolve=>{resolveDisposed=resolve;}),onError:assert.fail});
doc4.send('click');await flush();dispose4();resolveDisposed({expires_at:1});await flush();assert.equal(current.expires_at,beforeDispose);
assert.equal(errors.length,1);
console.log('trusted/visible/cooldown/expired/inflight/stale/error/dispose checks passed');
"""
    result = subprocess.run(
        [node, "--input-type=module", "-", str(module)],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
