"""Library status selection gates server queries and late responses consistently."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_library_combines_status_filters_with_search_and_invalidates_hidden_results():
    source = (WEB / "app.js").read_text()
    harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=SOURCE;
const slice=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const line=start=>source.slice(source.indexOf(start),source.indexOf('\n',source.indexOf(start)));
const nodes=new Map(),calls=[];
const node=selector=>nodes.get(selector)||nodes.set(selector,{innerHTML:'',textContent:'',className:'',events:{},attributes:{},addEventListener(type,handler){this.events[type]=handler;},setAttribute(name,value){this.attributes[name]=value;},focus(){this.focused=true;},contains(target){return target===this;}}).get(selector);
const context={state:{session:{authenticated:true},page:'materials',generation:0,items:[],total:0,offset:50,limit:50,query:'private report',kind:'site',states:['active','paused'],layout:'list'},
 URLSearchParams,Date,encodeURIComponent,AbortController,
 t:(key,values={})=>key.replace(/\{(\w+)\}/g,(match,name)=>values[name]??match),
 $:node,$$:()=>[],formChange(){},showError:error=>{throw error;},icon:()=>'<svg></svg>',bytes:()=>'',date:()=>'',safeURL:x=>x,
 api:async path=>{calls.push(path);return {items:[],total:0};}};
vm.createContext(context);
vm.runInContext([line('const escape ='),line('const label ='),line('const accessName ='),line('const libraryStates ='),line('const defaultLibraryStates ='),
 slice('function button(','function field('),
 slice('function statusFilter(','function materialStatus('),slice('function materialStatus(','function policyFields(')].join('\n'),context);
(async()=>{
 await context.loadMaterials();
 let query=new URL(calls.at(-1),'https://publish.test').searchParams;
 assert.equal(query.get('states'),'active,paused');assert.equal(query.has('status'),false);
 assert.equal(query.get('q'),'private report');assert.equal(query.get('kind'),'site');assert.equal(query.get('offset'),'50');
 context.state.states=['active','paused','expired','deleted'];context.state.offset=0;
 await context.loadMaterials();
 query=new URL(calls.at(-1),'https://publish.test').searchParams;
 assert.equal(query.get('states'),'active,paused,expired,deleted');assert.equal(query.get('q'),'private report');
 let resolve,signal;context.api=(path,options)=>new Promise(done=>{resolve=done;signal=options.signal;});
 const pending=context.loadMaterials();
 assert.equal(signal.aborted,false);context.state.states=[];await context.loadMaterials();
 assert.equal(signal.aborted,true,'obsolete list request must be aborted');
 assert.equal(context.state.items.length,0);assert.equal(context.state.total,0);
 assert.match(node('#materials').innerHTML,/Select at least one status to show materials\./);
 resolve({items:[{id:'stale'}],total:1});await pending;
 assert.equal(context.state.items.length,0,'a response from before deselection reappeared');
 const markup=context.statusFilter();
 assert.equal((markup.match(/type="checkbox"/g)||[]).length,4);
 assert.equal(markup.includes(' checked'),false);assert.match(markup,/name="library-state" value="expired"/);
 context.state.states=['active','paused'];
 assert.equal((context.statusFilter().match(/ checked/g)||[]).length,2);
 const cases=[['password','lock','Password-protected'],['link','link','Anyone with the link'],['private','owner','Only me']];
 context.icon=name=>`<svg data-icon="${name}"></svg>`;
 for(const [access,glyph,label] of cases){const badge=context.accessBadge({access});assert.match(badge,new RegExp(`data-icon="${glyph}"`));assert.ok(badge.includes(label));}
 assert.equal(context.materialStatus({library_status:'expired',paused:true}),'Expired');
 assert.equal(context.materialStatus({library_status:'deleted',content_state:'purge_pending'}),'Deletion pending');
 context.state.offset=50;context.api=async path=>{calls.push(path);return {items:[],total:0};};
 context.renderLibrary();context.$$=selector=>selector==='input:checked'?[{value:'expired'},{value:'deleted'}]:[];
 const filter=node('#status-filter');filter.events.change();
 assert.equal(context.state.offset,0,'changing statuses retained an old pagination offset');
 assert.equal(context.state.query,'private report');assert.equal(context.state.kind,'site');
 assert.equal(new URL(calls.at(-1),'https://publish.test').searchParams.get('states'),'expired,deleted');
 assert.match(markup,/aria-labelledby="library-status-label library-status-count"/);
 assert.equal(node('.status-filter-count').textContent,2);
 context.state.offset=50;
 context.api=async path=>{
   calls.push(path);
   return {total:50,items:[]};
 };
 const beforeClamp=calls.length;
 await context.loadMaterials();
 assert.deepEqual(calls.slice(beforeClamp).map(path=>new URL(path,'https://publish.test').searchParams.get('offset')),['50','0']);
 assert.equal(context.state.offset,0,'last-page removal did not return to a valid page');
 assert.doesNotMatch(node('#pagination').innerHTML,/51–50/);
 filter.open=true;let prevented=false;filter.events.keydown({key:'Escape',preventDefault(){prevented=true;}});
 assert.equal(filter.open,false);assert.equal(prevented,true);assert.equal(node('summary').focused,true);
 context.state.page='settings';context.state.states=[];
 node('#materials').innerHTML='departed library';const callsBeforeDeparture=calls.length;
 await context.loadMaterials();
 assert.equal(node('#materials').innerHTML,'departed library','a queued search touched a different page');
 assert.equal(calls.length,callsBeforeDeparture,'a queued search sent an owner request after departure');

 filter.open=true;filter.events.focusout({relatedTarget:null});assert.equal(filter.open,true,'a label pointer blur closed the picker before checkbox activation');
 filter.events.focusout({relatedTarget:filter});assert.equal(filter.open,true);
 filter.events.focusout({relatedTarget:{contains:target=>target===filter}});assert.equal(filter.open,true,'WebKit applies label clicks after moving focus to the containing landmark');
 filter.events.focusout({relatedTarget:{contains:()=>false}});assert.equal(filter.open,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", json.dumps(source), 1)
    result = subprocess.run(["node"], input=harness, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
