"""Audience changes use the loaded snapshot and preserve unknown history."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI state checks")
def test_statistics_switches_audiences_without_fetching_or_fabricating_history():
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');
const {mountStatistics}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
class Button{constructor(dataset={}){this.dataset=dataset;this.events={};}addEventListener(type,handler){this.events[type]=handler;}}
class Root{
 constructor(){this.isConnected=true;this.markup='';this.attributes={};}
 set innerHTML(value){this.markup=value;this.audiences=['both','guest','owner'].map(statisticsAudience=>new Button({statisticsAudience}));this.days=[7,30,90].map(statisticsDays=>new Button({statisticsDays}));this.enabled=[...value.matchAll(/data-statistics-enabled="(\w+)"/g)].map(([,statisticsEnabled])=>new Button({statisticsEnabled}));this.clear=new Button();}
 get innerHTML(){return this.markup;}
 querySelectorAll(selector){return {'[data-statistics-audience]':this.audiences,'[data-statistics-days]':this.days,'[data-statistics-enabled]':this.enabled}[selector];}
 querySelector(){return this.clear;}
 setAttribute(name,value){this.attributes[name]=value;}removeAttribute(name){delete this.attributes[name];}
}
globalThis.document={documentElement:{lang:'en'},dispatchEvent(){}};
const keys=new Set(),requests=[];
const snapshot={total:41,period_total:7,enabled:true,legacy_total:8,last_opened:null,daily:[{day:'2026-11-18',opens:4},{day:'2026-11-19',opens:3}],owner:{total:3,period_total:3,enabled:false,started_at:'2026-11-19T00:00:00Z',last_opened:'2026-11-19T01:00:00Z',collection_interrupted:true,daily:[{day:'2026-11-18',opens:null},{day:'2026-11-19',opens:3}]}};
const options=root=>({root,id:'test',api:async(path,request)=>{requests.push({path,request});return snapshot;},t:(key,values={})=>{keys.add(key);return key.replace(/\{(\w+)\}/g,(match,name)=>values[name]??match);},escape:String,date:String,onError:error=>{throw error;}});
const root=new Root(),mounted=await mountStatistics(options(root));
assert.equal(requests.length,1);assert(root.innerHTML.includes('data-statistics-audience="both" aria-pressed="true"'));
assert(root.innerHTML.includes('Earlier counter'));assert(root.innerHTML.includes('Not collected'));
assert.match(root.innerHTML,/data-statistics-series="guest" d="M4\.00 10\.00h\.01 L596\.00 35\.00"/);
assert.match(root.innerHTML,/data-statistics-series="owner" d="M596\.00 35\.00h\.01"/,'unknown day must be a gap at the shared scale');
assert.match(root.innerHTML,/<td>4<\/td><td>Not collected<\/td>/);
root.audiences[1].events.click();assert.equal(requests.length,1);
assert(root.innerHTML.includes('data-statistics-audience="guest" aria-pressed="true"'));
assert(!root.innerHTML.includes('data-statistics-series="owner"'));
root.audiences[2].events.click();assert.equal(requests.length,1);
assert(root.innerHTML.includes('data-statistics-audience="owner" aria-pressed="true"'));
assert(root.innerHTML.includes('Not collected'));assert(root.innerHTML.includes('Your last opening'));
assert(root.innerHTML.includes('Owner collection may have gaps'));assert(!root.innerHTML.includes('Earlier counter'));
assert(root.innerHTML.includes('Collection is paused'));assert(root.innerHTML.includes('Count my openings'));
root.enabled[0].checked=true;await root.enabled[0].events.change({currentTarget:root.enabled[0]});
assert.deepEqual(requests[1].request.body,{owner_counts:true});assert.equal(requests.length,3);
await root.days[0].events.click();await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.at(-1).path,'/publications/test/statistics?days=7');
assert(root.innerHTML.includes('data-statistics-audience="owner" aria-pressed="true"'));
const count=requests.length;document.documentElement.lang='ru';mounted.render();assert.equal(requests.length,count);
root.audiences[0].events.click();assert(root.innerHTML.includes('Count guest openings'));
const next=new Root();await mountStatistics(options(next));assert(next.innerHTML.includes('data-statistics-audience="both" aria-pressed="true"'));
let resolve;const stale=new Root();const loading=mountStatistics({...options(stale),api:()=>new Promise(r=>resolve=r)});
stale.isConnected=false;resolve(snapshot);await loading;assert.equal(stale.innerHTML,'');
console.log(JSON.stringify([...keys]));
'''
    result = subprocess.run(["node", "--input-type=module", "-", str(WEB / "statistics.js")], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    catalog = json.loads((WEB / "messages.json").read_text())
    assert set(json.loads(result.stdout)) <= catalog.keys()
