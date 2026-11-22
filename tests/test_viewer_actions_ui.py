"""Quick action requests freeze their dialog and cannot report into a newer one."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / 'src/minder_publish/server/web'


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        ("en", ["+1 day", "+7 days", "+30 days"]),
        ("ru", ["+1 день", "+7 дней", "+30 дней"]),
        ("es", ["+1 día", "+7 días", "+30 días"]),
        ("fr", ["+1 jour", "+7 jours", "+30 jours"]),
    ],
)
def test_expiry_presets_render_localized_day_counts(language, labels):
    if not shutil.which("node"):
        pytest.skip("Node is required for viewer UI checks")
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import vm from 'node:vm';

const [web, lang, labels] = process.argv.slice(2);
const read = name => readFileSync(join(web, name), 'utf8');
const document = {documentElement: {lang}, dispatchEvent() {}};
const root = {
  isConnected: true,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
};
const context = vm.createContext({
  document, Event, window: {},
  fetch: async () => ({ok: true, json: async () => JSON.parse(read('messages.json'))}),
});
const load = name => vm.runInContext(
  read(name).replace(/^import .*;\n/gm, '').replace(/^export /gm, ''), context,
);
load('i18n.js');
await context.loadMessages();
load('viewer-actions.js');
context.mountViewerActions({
  root,
  getMaterial: () => ({
    id: 'test', access: 'link', content_state: 'present',
    expires_at: new Date(Date.now() + 86400000).toISOString(),
  }),
  getSettings: () => ({ceilings: {retention_days: 365}}),
  getSession: () => ({authenticated: true}),
  t: context.t, escape: value => value, date: value => value,
}).render();

const buttons = [...root.innerHTML.matchAll(
  /data-owner-action="extend" data-days="(\d+)"[^>]*>([^<]*)<\/button>/g,
)];
assert.deepEqual(buttons.map(match => Number(match[1])), [1, 7, 30]);
assert.deepEqual(buttons.map(match => match[2].replace(/\s/g, ' ')), JSON.parse(labels));
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WEB), language, json.dumps(labels)],
        input=script, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_delayed_action_locks_controls_and_discards_stale_error():
    if not shutil.which('node'):
        pytest.skip('Node is required for viewer UI checks')
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(process.argv[2],'utf8').replace(/^import .*;\n/gm,'').replace('export function mountViewerActions','function mountViewerActions');
class N {constructor(){this.events={};this.disabled=false;this.hidden=false;this.value='';this.dataset={};this.isConnected=true;this.open=false;this.textContent='';} addEventListener(k,f){this.events[k]=f} setAttribute(k,v){this[k]=v} removeAttribute(k){delete this[k]} focus(){} close(){this.open=false;this.events.close?.()} remove(){this.isConnected=false} showModal(){this.open=true} }
let dialogs=[];
function makeDialog(){const d=new N(), form=new N(), cancel=new N(), submit=new N(), select=new N(), days=new N(), panel=new N(), error=new N(); select.value='never';days.value='7';days.disabled=true;panel.querySelector=()=>days; const list=[select,days,cancel,submit];list.expiry=select;list.days=days;form.elements=list;form.reportValidity=()=>true;form.querySelector=()=>submit;d.querySelector=s=>({'form':form,'[data-action-cancel]':cancel,'[data-action-error]':error,'[name=expiry]':select,'[data-expiry-days]':panel,'input,select,[type=submit]':select}[s]);d.querySelectorAll=()=>[]; d.parts={form,cancel,submit,select,days,error};dialogs.push(d);return d;}
const document={documentElement:{lang:'en'},createElement:makeDialog,body:{append(){}},dispatchEvent(){}};
const root=new N();root.isConnected=true;root.contains=()=>true;root.querySelectorAll=()=>[];root.querySelector=()=>null;
const session={authenticated:true};let reject,errors=0,changed=0,requested;
const context={document,Event:class{},Date,Number,encodeURIComponent,copyOwnerSecret(){},clearPasswordEditors(){},mountPasswordEditors(){},passwordEditorControl(){return ''}};
vm.createContext(context);vm.runInContext(source,context);
const actions=context.mountViewerActions({root,drawer:{open:true},api:(url,options)=>{requested={url,options};return new Promise((_,r)=>reject=r)},getMaterial:()=>({id:'abc',access:'link',content_state:'present'}),getSettings:()=>({ceilings:{retention_days:365}}),getSession:()=>session,t:x=>x,escape:x=>x,date:x=>x,onChanged:()=>changed++,onDeleted(){},onError:()=>errors++,notify(){}});
const button={dataset:{ownerAction:'expiry'},disabled:false,closest(){return this}};
await root.events.click({target:button});let d=dialogs.at(-1),p=d.parts;
const pending=p.form.events.submit({preventDefault(){},currentTarget:p.form});
assert.equal(requested.options.body.expiry.mode,'never');assert.equal(p.form['aria-busy'],'true');assert.ok([...p.form.elements].every(x=>x.disabled));
p.cancel.events.click();let prevented=false;d.events.cancel({preventDefault(){prevented=true}});assert.ok(prevented);assert.equal(d.open,true);
reject(new Error('retry'));await pending;assert.equal(p.error.textContent,'retry');assert.equal(p.error.hidden,false);assert.equal(p.select.disabled,false);assert.equal(p.days.disabled,true);assert.equal(p.cancel.disabled,false);assert.equal(p.form['aria-busy'],undefined);
const pending2=p.form.events.submit({preventDefault(){},currentTarget:p.form});actions.close();await root.events.click({target:button});assert.equal(dialogs.length,1);reject(new Error('stale'));await pending2;await root.events.click({target:button});let next=dialogs.at(-1);assert.notEqual(next,d);assert.equal(next.parts.error.textContent,'');assert.equal(errors,0);assert.equal(changed,0);
'''
    result = subprocess.run(['node', '--input-type=module', '-', str(WEB / 'viewer-actions.js')],
                            input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sharing_does_not_copy_after_session_ends_during_refresh():
    if not shutil.which('node'):
        pytest.skip('Node is required for viewer UI checks')
    script = r'''
import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';import vm from 'node:vm';
const source=readFileSync(process.argv[2],'utf8').replace('export function mountVersionSharing','function mountVersionSharing');
class N{constructor(){this.events={};this.isConnected=true;this.open=false;this.disabled=false;this.hidden=false;this.dataset={}}addEventListener(k,f){this.events[k]=f}setAttribute(){}removeAttribute(){}close(){this.open=false;this.events.close?.()}remove(){this.isConnected=false}showModal(){this.open=true}focus(){}}
let dialog,resolveRefresh,writes=[];const session={authenticated:true};let currentSession=session;
const root=new N();root.contains=()=>true;root.querySelector=()=>null;root.replaceChildren=()=>{};
const document={body:{append(){}},createElement(){const d=new N(),form=new N(),cancel=new N(),submit=new N(),err=new N();form.elements=[cancel,submit];d.querySelector=s=>({'form':form,'[data-sharing-cancel]':cancel,'[type=submit]':submit,'[role=alert]':err}[s]);d.parts={form,cancel,submit};dialog=d;return d;}};
const navigator={clipboard:{writeText:async s=>writes.push(s)}};const context={document,navigator,encodeURIComponent};vm.createContext(context);vm.runInContext(source,context);
let call;context.mountVersionSharing({root,api:async(u,o)=>{call={u,o};return {version_url:'https://publish.test/p/id~1'}},getMaterial:()=>({id:'id',access:'link'}),getSelected:()=>({revision_id:'rev',sequence:1,available:true,shared:false}),getSession:()=>currentSession,t:x=>x,escape:x=>x,onChanged:()=>new Promise(r=>resolveRefresh=r),onError(){},notify(){}});
const button={dataset:{versionAction:'share'},closest(){return this}};await root.events.click({target:button});const pending=dialog.parts.form.events.submit({preventDefault(){},currentTarget:dialog.parts.form});await new Promise(resolve=>setImmediate(resolve));assert.equal(call.u,'/publications/id/revisions/rev/sharing');assert.equal(call.o.body.enabled,true);currentSession=null;resolveRefresh();await pending;assert.deepEqual(writes,[]);
'''
    result = subprocess.run(['node', '--input-type=module', '-', str(WEB / 'viewer-sharing.js')],
                            input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
