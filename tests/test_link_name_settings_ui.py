"""Random link-name controls follow server bounds and reject inverted ranges."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / "src/minder_publish/server/web"


def test_link_name_settings_use_discovered_bounds_and_validate_before_saving():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for settings UI checks")
    source = (WEB / "app.js").read_text()
    script = "const source=" + json.dumps(source) + ";\n" + r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const slice=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const line=start=>source.slice(source.indexOf(start),source.indexOf('\n',source.indexOf(start)));
const value=initial=>({value:String(initial),validationMessage:'',setCustomValidity(message){this.validationMessage=message;}});
const elements={history_previous_versions:value(0),history_retention_days:value(12),history_budget_percent:value(5),access:value('private'),private_ttl_days:value(''),external_ttl_days:value(7),purge_mode:value('never'),owner_auto_open:{checked:true},preview:value('auto'),guest_counts:{checked:true},owner_counts:{checked:true},indexing:{checked:false},timezone:value('browser'),site_entry:value('index.html'),site_routing:value('strict'),external_resources:value('self_only'),file_mib:value(2),payload_mib:value(4),site_mib:value(8),site_files:value(100),publication_count:value(250),quota_gib:value(2),concurrent_uploads:value(2),publication_key_min_characters:value(10),publication_key_max_characters:value(20)};
const form={elements,events:{},addEventListener(name,handler){this.events[name]=handler;},reportValidity(){return !elements.publication_key_max_characters.validationMessage;}};
const view={innerHTML:''},lifecycle={innerHTML:''},submit={disabled:false},calls=[];
const context={state:{session:{authenticated:true},page:'settings',settings:{defaults:{history_previous_versions:0,history_retention_days:12,history_budget_percent:5,access:'private',private_ttl_seconds:null,external_ttl_seconds:604800,timezone:'browser',publication_key_min_characters:10,publication_key_max_characters:20},limits:{file_bytes:2097152,upload_payload_bytes:4194304,site_expanded_bytes:8388608,site_files:100,publication_count:250,content_quota_bytes:2147483648,concurrent_uploads:2},ceilings:{history_previous_versions:17,history_retention_days:123,history_budget_percent:100,file_bytes:10485760,upload_payload_bytes:20971520,site_expanded_bytes:41943040,site_files:1000,publication_count:10000,content_quota_bytes:8589934592,concurrent_uploads:8,retention_days:365},storage:{used_bytes:0,reserved_bytes:0},operations:{publication_key_bounds:{minimum:6,maximum:30}}}},
 MiB:1048576,GiB:1073741824,DAY:86400,Intl,document:{documentElement:{lang:'en'}},
 t:(key,values={})=>key.replace(/\{(\w+)\}/g,(match,name)=>values[name]??match),
 $:selector=>selector==='#view'?view:selector==='#settings-form'?form:selector==='#lifecycle'?lifecycle:submit,
 syncForm(){},capacity:{render(){}},policyFields:()=>'',bytes:()=>'',date:()=>'',notify(){},showError(error){throw error;},readPolicy:()=>({purge_mode:'never'}),
 api:async(path,options)=>{calls.push({path,options});return context.state.settings;}};
vm.createContext(context);
vm.runInContext([line('const escape ='),line('const label ='),line('const accessName ='),
 slice('function field(','function passwordEditor('),slice('function nonNegativeInteger(','function syncForm('),
 slice('function settingsRow(','function createTokenDialog(')].join('\n'),context);
(async()=>{
 context.renderSettings();
 assert.match(view.innerHTML,/Example publication/);
 assert.match(lifecycle.innerHTML,/Only me/);
 elements.access.value='link';form.events.input();
 assert.match(lifecycle.innerHTML,/Anyone with the link/);
 assert.equal(calls.length,0,'editing the example saved settings');
 assert.match(view.innerHTML,/<h2>Link names<\/h2>/);
 for(const [name,initial] of [['publication_key_min_characters',10],['publication_key_max_characters',20]]){
   const tag=view.innerHTML.match(new RegExp('<input id="'+name+'"[^>]+>'))?.[0];
   assert.ok(tag);assert.ok(tag.includes('min="6"'));assert.ok(tag.includes('max="30"'));assert.ok(tag.includes('step="1"'));assert.ok(tag.includes('value="'+initial+'"'));
 }
 for(const [name,maximum] of [['history_previous_versions',17],['history_retention_days',123],['history_budget_percent',100]]){
   const tag=view.innerHTML.match(new RegExp('<input id="'+name+'"[^>]+>'))?.[0];
   assert.ok(tag.includes('min="0"'));assert.ok(tag.includes('max="'+maximum+'"'));
 }
 const event={preventDefault(){}};
 elements.publication_key_min_characters.value='21';
 await form.events.submit(event);
 assert.equal(calls.length,0,'inverted bounds reached the server');assert.ok(elements.publication_key_max_characters.validationMessage);
 elements.publication_key_min_characters.value='10';form.events.input();
 assert.equal(elements.publication_key_max_characters.validationMessage,'');
 await form.events.submit(event);
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/settings');assert.equal(calls[0].options.method,'PATCH');
 assert.equal(calls[0].options.body.limits.publication_count,250);
 assert.equal(calls[0].options.body.defaults.history_previous_versions,0);
 assert.equal(calls[0].options.body.defaults.history_retention_days,12);
 assert.equal(calls[0].options.body.defaults.history_budget_percent,5);
 assert.equal(calls[0].options.body.defaults.publication_key_min_characters,10);
 assert.equal(calls[0].options.body.defaults.publication_key_max_characters,20);
 assert.equal(submit.disabled,false);
 elements.publication_key_min_characters.value='8';elements.publication_key_max_characters.value='8';form.events.change();
 await form.events.submit(event);assert.equal(calls.length,2,'an equal min/max should allow a fixed random length');
 assert.equal(calls[1].options.body.defaults.publication_key_min_characters,8);assert.equal(calls[1].options.body.defaults.publication_key_max_characters,8);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
