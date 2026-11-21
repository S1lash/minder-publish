"""Owner viewer pins a chosen revision and protects pending restore state."""
import shutil
import subprocess
from pathlib import Path

import pytest

VIEWER = Path(__file__).parents[1] / 'src/minder_publish/server/web/viewer.js'


def run_node(script):
    if not shutil.which('node'):
        pytest.skip('Node is required for owner viewer checks')
    result = subprocess.run(['node', '--input-type=module', '-', str(VIEWER)], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_revision_remains_pinned_when_material_changes():
    run_node(r'''
import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');const start=source.indexOf('function explicitRevision()');const end=source.indexOf('function renderContent()',start);assert.ok(start>=0&&end>start);
const context={URL,URLSearchParams,encodeURIComponent,location:{origin:'https://publish.test',search:'?revision=old',hash:'#part'},material:{current_revision:'old',stable_url:'https://publish.test/p/short'},selected:{revision_id:'old'},initialRevision:null,endpoint:'/publications/short'};
vm.createContext(context);vm.runInContext(source.slice(start,end),context);
assert.equal(context.originalURL(),'/api/v1/publications/short/revisions/old/open#part');
context.material.current_revision='new';assert.equal(context.originalURL(),'/api/v1/publications/short/revisions/old/open#part');
context.location.search='';context.material.current_revision='old';assert.equal(context.originalURL(),'https://publish.test/p/short?preview=1#part');
context.initialRevision='old';assert.equal(context.originalURL(),'/api/v1/publications/short/revisions/old/open#part');
''')


def test_restore_waits_blocks_repeat_and_discards_late_failure():
    run_node(r'''
import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');const start=source.indexOf("$('#viewer-restore').addEventListener");const end=source.indexOf("$('#viewer-open-tools').addEventListener",start);assert.ok(start>=0&&end>start);
class N{constructor(){this.events={};this.disabled=false;this.open=false;this.hidden=true;this.textContent=''}addEventListener(k,f){this.events[k]=f}showModal(){this.open=true}close(){this.open=false}setAttribute(k,v){this[k]=v}removeAttribute(k){delete this[k]}}
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,new N());return nodes.get(id)};const owner={authenticated:true};let reject,calls=0,refreshed=0,globalErrors=0;
const c={$,encodeURIComponent,session:owner,selected:{revision_id:'old'},material:{current_revision:'new'},endpoint:'/publications/id',restoreBusy:false,current(){return !!c.session?.authenticated},api:(path,options)=>{calls++;assert.equal(path,'/publications/id/revisions/old/restore');assert.equal(options.body.expected_revision,'new');return new Promise((_,r)=>reject=r)},setRevisionURL(){},refresh:async()=>{refreshed++},showError(){globalErrors++},t:x=>x};
vm.createContext(c);vm.runInContext(source.slice(start,end),c);
$('#viewer-restore').events.click();assert.equal($('#viewer-confirm').open,true);const button=$('#viewer-restore-confirm');const pending=button.events.click({currentTarget:button});assert.equal(calls,1);assert.equal(button.disabled,true);assert.equal($('#viewer-restore-cancel').disabled,true);assert.equal($('#viewer-confirm')['aria-busy'],'true');
$('#viewer-restore-cancel').events.click();let prevented=false;$('#viewer-confirm').events.cancel({preventDefault(){prevented=true}});assert.equal(prevented,true);assert.equal($('#viewer-confirm').open,true);await button.events.click({currentTarget:button});assert.equal(calls,1);
reject(Object.assign(new Error('reauth cancelled'),{status:403}));await pending;assert.equal($('#viewer-confirm-error').textContent,'reauth cancelled');assert.equal($('#viewer-confirm-error').hidden,false);assert.equal(button.disabled,false);assert.equal($('#viewer-restore-cancel').disabled,false);assert.equal($('#viewer-confirm')['aria-busy'],undefined);
const pending2=button.events.click({currentTarget:button});c.session=null;$('#viewer-confirm').close();reject(Error('late failure'));await pending2;assert.equal(globalErrors,0);assert.equal(refreshed,0);assert.equal(button.disabled,false);
''')


def test_selected_revision_controls_preview_type_and_keeps_fragment():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    if not shutil.which('node'):
        pytest.skip('Node is required for viewer UI checks')
    path = Path(__file__).parents[1] / 'src/minder_publish/server/web/viewer.js'
    source = path.read_text()
    functions = source[source.index('function explicitRevision()'):source.index('function render(){')]
    script = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const root={items:[],replaceChildren(){this.items=[]},append(x){this.items.push(x)},querySelector(){return this.items.find(x=>x.tag==='iframe')}};
const context={URL,URLSearchParams,encodeURIComponent,location:{origin:'https://publish.test',search:'?revision=old',hash:'#section'},initialRevision:null,endpoint:'/publications/id',material:{stable_url:'https://publish.test/p/id',current_revision:'new',content_state:'present',kind:'file',preview:'auto',title:'Example'},selected:null,settings:{defaults:{owner_auto_open:true}},$:()=>root,t:x=>x,escape:x=>x,document:{createElement:tag=>({tag,dataset:{},setAttribute(){}})}};
vm.createContext(context);vm.runInContext(SOURCE,context);
function test(selected,expected){context.selected=selected;context.renderContent();assert.equal(root.items[0].tag,expected);return root.items[0];}
const html={available:true,revision_id:'old',kind:'file',content_type:'text/html'};const frame=test(html,'iframe');assert.match(frame.src,/\/old\/open#section$/);
context.renderContent();assert.equal(root.items[0],frame,'unchanged refresh preserves iframe');
context.material.access_version=2;context.renderContent();assert.notEqual(root.items[0],frame,'changed access invalidates iframe');
assert.equal(test({available:true,revision_id:'old',kind:'file',content_type:'application/zip'},'section').className,'viewer-file-fallback');
assert.equal(test({available:true,revision_id:'old',kind:'site',content_type:'text/html'},'iframe').tag,'iframe');
'''.replace('SOURCE', json.dumps(functions), 1)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
