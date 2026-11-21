"""Connection instructions never outlive their owner view or expose credentials."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(
    not shutil.which("node"), reason="Node is required for UI state checks"
)
def test_provider_setup_uses_real_assets_and_ignores_stale_async_results():
    source = (WEB / "connection.js").read_bytes()
    script = r"""
import assert from 'node:assert/strict';
const {mountConnection} = await import('data:text/javascript;base64,'+SOURCE);
globalThis.document={documentElement:{lang:'en'}};
globalThis.location={href:'https://publish.test/'};
const copied=[];let openedLimits=0;
Object.defineProperty(globalThis,'navigator',{value:{clipboard:{writeText:async value=>copied.push(value)}},configurable:true});
const keys=new Set();
function disclosureFixture(){
 const element=()=>({events:{},addEventListener(name,handler){this.events[name]=handler;},emit(name,event={}){this.events[name]?.(event);}});
 const trigger={...element(),attributes:{},setAttribute(key,value){this.attributes[key]=value;},focus(){document.activeElement=this;this.emit('focus');}};
 const panel={hidden:true};
 return {...element(),trigger,panel,matches:()=>true,querySelector(selector){return selector==='[aria-controls]'?trigger:panel;},contains(target){return target===trigger||target===panel;}};
}
const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const t=(key,values={})=>{keys.add(key);return key.replace(/\{(\w+)\}/g,(match,name)=>values[name]??match);};
class Root {
  constructor(){this.innerHTML='';this.dataset={};this.isConnected=true;this.handlers=new Set();this.status={hidden:true,textContent:''};this.output={value:'',focus(){},select(){}};this.fallback={hidden:true,innerHTML:'',querySelector:()=>this.output};}
  addEventListener(type,handler,{signal}){this.handlers.add(handler);signal.addEventListener('abort',()=>this.handlers.delete(handler));}
  contains(){return true;}
  querySelector(selector){if(selector==='[data-upload-limits]')return this.innerHTML.includes('data-upload-limits')?(this.disclosure=disclosureFixture()):null;return selector==='.connection-copy-status'?this.status:this.fallback;}
  async click(dataset={},attributes=[]){const button={dataset,disabled:false,isConnected:true,hasAttribute:name=>attributes.includes(name)};button.closest=()=>button;for(const handler of this.handlers)await handler({target:button});return button;}
}
const distribution={instance_url:'https://publish.test',mcp_url:'https://publish.test/mcp',client:{available:true,version:'0.2.0',filename:'client.whl',sha256:'a'.repeat(64)},assets:{'client.whl':{url:'https://publish.test/downloads/client.whl'},'minder-publish-skill.zip':{url:'https://publish.test/downloads/minder-publish-skill.zip'},'minder-publish-plugin.zip':{url:'https://publish.test/downloads/minder-publish-plugin.zip',sha256:'b'.repeat(64)}},install_prompts:{codex:'INSTALL_CODEX_PUBLIC',claude:'INSTALL_CLAUDE_PUBLIC'},skill_prompts:{codex:'SKILL_CODEX_PUBLIC',claude:'SKILL_CLAUDE_PUBLIC'}};
const values={ '/capabilities':{limits:{file_bytes:109,upload_payload_bytes:180,site_expanded_bytes:560,site_files:50}},'/distribution':distribution,'/connections':{enabled:true,connections:[{id:'one',name:'Test app',scopes:['materials:manage'],created_at:'2026-11-18T00:00:00Z'}]},'/tokens':[{id:'a',name:'Computer one',scope:'materials:manage',token:'MUST_NEVER_RENDER'},{id:'b',name:'Computer two',scope:'materials:read'}] };
function options(root,api=async path=>values[path],state={active:true}){return{root,api,t,escape,date:x=>x,bytes:x=>String(x)+" units",onOpenLimits:()=>openedLimits++,onCreateToken:()=>{},onError:error=>{throw error;},isCurrent:()=>state.active};}
const root=new Root();await mountConnection(options(root));
assert(root.innerHTML.includes('data-connection-provider="codex"'));
assert(!root.innerHTML.includes('MUST_NEVER_RENDER'));
function assertLocal(markup){
 const main=markup.split('<ol class="connection-setup-steps">')[1].split('</ol>')[0];
 assert.equal((main.match(/<li class="connection-step">/g)||[]).length,2);
 assert(main.includes('data-connection-copy="cli"'));
 assert(!main.includes('data-connection-copy="skill"'));
 const repair=markup.split('<details class="connection-release-details">')[1].split('</details>')[0];
 assert(repair.includes('data-connection-copy="skill"'));
 assert(!markup.includes('data-connection-copy="configure"'));
 assert(!main.includes('minder-publish configure')&&!main.includes('doctor --json'));
}
assertLocal(root.innerHTML);
await root.click({connectionCopy:'cli'});assert.equal(copied.at(-1),'INSTALL_CODEX_PUBLIC');
await root.click({connectionProvider:'claude-code'});assertLocal(root.innerHTML);await root.click({connectionCopy:'cli'});assert.equal(copied.at(-1),'INSTALL_CLAUDE_PUBLIC');await root.click({connectionCopy:'skill'});assert.equal(copied.at(-1),'SKILL_CLAUDE_PUBLIC');
await root.click({connectionProvider:'chatgpt'});
function assertCloud(markup){
 const main=markup.split('<ol class="connection-setup-steps">')[1].split('</ol>')[0];
 assert.equal((main.match(/<li class="connection-step">/g)||[]).length,5);
 assert(main.indexOf('Connect Publish') < main.indexOf('Choose tool permissions'));
 assert(main.indexOf('Choose tool permissions') < main.indexOf('Check the connection'));
 assert(main.includes('ask before each tool use') && main.includes('separate from the access'));
 assert(main.includes('A skill adds instructions'));
 assert(main.includes('data-connection-copy="cloud-check"'));
 assert(!main.includes('data-connection-copy="cloud-skill"'));
 assert(main.indexOf('Choose tool permissions') < main.indexOf('Install the publishing skill'));
 assert(main.indexOf('Install the publishing skill') < main.indexOf('Check the connection'));
 assert(main.includes('href="https://publish.test/downloads/minder-publish-skill.zip"'));
 assert(!main.includes('minder-publish-plugin.zip')&&!main.includes('Bearer ')&&!main.includes('Create token'));
 assert(!main.includes('1 MiB') && main.includes('browser upload creates a new material'));
 assert(!markup.includes('1 MiB'));
 assert(markup.includes('109 units') && markup.includes('180 units') && markup.includes('560 units'));
 assert(markup.includes('Open upload settings') && markup.includes('data-upload-limits'));
}
assertCloud(root.innerHTML);assert(root.innerHTML.includes('automatic client registration (DCR)'));assert(root.innerHTML.includes('ChatGPT, review the Publish app'));
await root.click({connectionCopy:'cloud-check'});
assert(copied.at(-1).includes('at most three materials'));
assert(copied.at(-1).includes('Do not publish, edit, delete or upload'));
assert(copied.at(-1).includes('without requesting credentials'));
const {trigger,panel}=root.disclosure;
assert.equal(panel.hidden,true);
root.disclosure.emit('pointerenter',{pointerType:'mouse'});
assert.equal(panel.hidden,false);
root.disclosure.emit('pointerleave');
assert.equal(panel.hidden,true);
root.disclosure.emit('pointerenter',{pointerType:'touch'});
assert.equal(panel.hidden,true);
trigger.emit('click');
assert.equal(panel.hidden,false);
assert.equal(trigger.attributes['aria-expanded'],'true');
root.disclosure.emit('keydown',{key:'Escape',preventDefault(){}});
assert.equal(panel.hidden,true);
assert.equal(trigger.attributes['aria-expanded'],'false');
assert.equal(document.activeElement,trigger);
await root.click({},['data-open-upload-limits']);assert.equal(openedLimits,1);
await root.click({connectionCopy:'file-request'});
assert.equal(copied.at(-1),'Use Minder Publish to create an upload link for my file. I will choose the file and confirm publication in the browser.');
await root.click({connectionCopy:'mcp'});assert.equal(copied.at(-1),'https://publish.test/mcp');
assert(root.innerHTML.includes('Plugins → Skills → Create → Upload from your computer'));
await root.click({connectionProvider:'claude'});assertCloud(root.innerHTML);assert(root.innerHTML.includes('Customize → Connectors → Publish → Permissions'));assert(root.innerHTML.includes('Register automatically'));assert(root.innerHTML.includes('No sign in'));assert(root.innerHTML.includes('Customize (or Plugins) → Skills → + → Create skill → Upload a skill'));await root.click({connectionCopy:'cloud-check'});assert(copied.at(-1).includes('Do not publish, edit, delete or upload'));
assert(!root.innerHTML.includes('MUST_NEVER_RENDER'));
await root.click({connectionProvider:'other'});
await root.click({connectionProvider:'codex'});
navigator.clipboard.writeText=async()=>{throw new Error('Clipboard not available');};
await root.click({connectionCopy:'cli'});
assert.equal(root.output.value,'INSTALL_CODEX_PUBLIC');assert.equal(root.fallback.hidden,false);
await root.click({},['data-connection-close-copy']);assert.equal(root.fallback.hidden,true);
let rejectCopy;
navigator.clipboard.writeText=()=>new Promise((resolve,reject)=>{rejectCopy=reject;});
const staleCopy=root.click({connectionCopy:'cli'});
navigator.clipboard.writeText=async()=>{};
await root.click({connectionCopy:'example'});
rejectCopy(new Error('Old clipboard request failed'));await staleCopy;
assert.equal(root.fallback.hidden,true,'old copy replaced a newer result');
assert.equal(root.status.hidden,false,'old copy hid the newer confirmation');
const empty=new Root();await mountConnection(options(empty,async path=>path==='/connections'?{enabled:false,connections:[]}:path==='/tokens'?[]:values[path]));
await empty.click({connectionProvider:'claude'});
const state={active:true},stale=new Root(),pending=[];let errors=0;
const waiting=mountConnection({...options(stale,path=>new Promise(resolve=>pending.push(()=>resolve(values[path]))),state),onError:()=>errors++});
state.active=false;for(const resolve of pending)resolve();await waiting;
assert(!stale.innerHTML.includes('Computer one'));assert.equal(errors,0);
const shared=new Root(),older=[];
const previous=mountConnection(options(shared,path=>new Promise(resolve=>older.push(()=>resolve(path==='/tokens'?[{id:'old',name:'OLD_TOKEN_NAME'}]:values[path])))));
await mountConnection(options(shared));for(const resolve of older)resolve();await previous;
assert(shared.innerHTML.includes('Computer one'));assert(!shared.innerHTML.includes('OLD_TOKEN_NAME'));
assert.equal(shared.handlers.size,1);
const failedRoot=new Root(),failedState={active:true};let reject;
const failure=mountConnection({...options(failedRoot,()=>new Promise((resolve,fail)=>{reject=fail;}),failedState),onError:()=>errors++});
failedState.active=false;reject(new Error('Expired session'));await failure;assert.equal(errors,0);
console.log(JSON.stringify({keys:[...keys]}));
""".replace("SOURCE", json.dumps(base64.b64encode(source).decode()), 1)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    required = set(json.loads(result.stdout)["keys"])
    catalog = json.loads((WEB / "messages.json").read_text())
    assert required <= catalog.keys(), required - catalog.keys()
    for key in required:
        assert all(catalog[key].get(language) for language in ("en", "ru", "es", "fr")), key
