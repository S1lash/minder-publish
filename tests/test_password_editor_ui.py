"""Draft publication passwords use browser cryptography and never replace form edits."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / "src/minder_publish/server/web"


def node(script, *arguments):
    binary = shutil.which("node")
    if not binary:
        pytest.skip("Node is required for password editor checks")
    result = subprocess.run(
        [binary, "--input-type=module", "-", *map(str, arguments)],
        input=script, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_draft_password_generation_copy_fallback_and_context_cleanup():
    module = WEB / "password-editor.js"
    assert module.is_file(), "Publication password generation is not implemented"
    node(r'''
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const {generatePublicationPassword,mountPasswordEditors,clearPasswordEditors}=await import(pathToFileURL(process.argv[2]));
let entropyCalls=0;
const random={getRandomValues(bytes){entropyCalls++;return webcrypto.getRandomValues(bytes);}};
const passwords=new Set(Array.from({length:100},()=>generatePublicationPassword(random)));
assert.equal(passwords.size,100);assert.equal(entropyCalls,100);
for(const value of passwords)assert.match(value,/^[A-Za-z0-9_-]{24}$/);
assert.throws(()=>generatePublicationPassword(null));
assert.throws(()=>generatePublicationPassword({getRandomValues(){throw Error('No secure source');}}));
class Node {
 constructor(){this.events={};this.value='';this.disabled=false;this.hidden=false;this.textContent='';}
 addEventListener(type,listener){this.events[type]=listener;}
 dispatchEvent(event){return this.events[event.type]?.(event);}
 focus(){this.focused=true;}
 select(){this.selected=true;}
}
const input=new Node(),copy=new Node(),generate=new Node(),status=new Node(),fallback=new Node(),manual=new Node(),hide=new Node();
const map=new Map([['[data-draft-password]',input],['[data-copy-draft-password]',copy],['[data-generate-password]',generate],['[data-password-editor-status]',status],['[data-draft-password-fallback]',fallback],['[data-draft-password-copy]',manual],['[data-hide-draft-password]',hide]]);
const editor={querySelector:selector=>map.get(selector)};
const root={open:true,contains:element=>element===editor,querySelectorAll:selector=>selector==='[data-password-editor]'?[editor]:selector==='[data-draft-password], [data-draft-password-copy]'?[input,manual]:selector==='[data-draft-password-fallback]'?[fallback]:selector==='[data-copy-draft-password]'?[copy]:[]};
const owner={authenticated:true};let session=owner,epoch=1,writes=[],copied=0,errors=0;
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async value=>{writes.push(value);}}}});
mountPasswordEditors({root,getSession:()=>session,getDialogEpoch:()=>epoch,t:x=>x,onCopied:()=>copied++,onError:()=>errors++});
assert.equal(copy.disabled,true);
const originalRoot=root,unsavedTitle='Title stays untouched';
generate.events.click();
assert.match(input.value,/^[A-Za-z0-9_-]{24}$/);assert.equal(copy.disabled,false);assert.ok(status.textContent);
const generated=input.value;
await copy.events.click();assert.deepEqual(writes,[generated]);assert.equal(copied,1);assert.equal(root,originalRoot);assert.equal(unsavedTitle,'Title stays untouched');
input.value='manually-entered';input.dispatchEvent(new Event('input'));await copy.events.click();assert.equal(writes.at(-1),'manually-entered');
navigator.clipboard.writeText=async()=>{throw Error('Clipboard unavailable');};
await copy.events.click();assert.equal(fallback.hidden,false);assert.equal(manual.value,'manually-entered');assert.equal(manual.selected,true);assert.equal(input.value,'manually-entered');
hide.events.click();assert.equal(fallback.hidden,true);assert.equal(manual.value,'');assert.equal(input.value,'manually-entered');
let reject;
navigator.clipboard.writeText=()=>new Promise((resolve,fail)=>reject=fail);
const pending=copy.events.click();await new Promise(resolve=>setImmediate(resolve));
input.value='edited-during-copy';input.dispatchEvent(new Event('input'));reject(Error('Denied'));await pending;
assert.equal(fallback.hidden,true);assert.equal(manual.value,'');
const closed=copy.events.click();await new Promise(resolve=>setImmediate(resolve));
root.open=false;clearPasswordEditors(root);reject(Error('Denied'));await closed;
assert.equal(input.value,'');assert.equal(manual.value,'');assert.equal(fallback.hidden,true);
root.open=true;input.value='logout-draft';input.dispatchEvent(new Event('input'));
const logout=copy.events.click();await new Promise(resolve=>setImmediate(resolve));
session={authenticated:false};clearPasswordEditors(root);reject(Error('Denied'));await logout;
assert.equal(fallback.hidden,true);assert.equal(manual.value,'');assert.equal(copy.disabled,true);assert.equal(errors,0);
session=owner;input.value='epoch-draft';input.dispatchEvent(new Event('input'));
const replaced=copy.events.click();await new Promise(resolve=>setImmediate(resolve));epoch++;
reject(Error('Denied'));await replaced;assert.equal(fallback.hidden,true);assert.equal(manual.value,'');
input.value='';input.dispatchEvent(new Event('input'));assert.equal(copy.disabled,true);
''', module)


def test_custom_link_name_is_sent_only_for_nonempty_new_publication_plans():
    source = (WEB / "app.js").read_text()
    start = source.index("async function upload(")
    end = source.index("function settingsRow(", start)
    node("const code=" + json.dumps(source[start:end]) + ";\n" + r'''
import assert from 'node:assert/strict';
import vm from 'node:vm';
async function scenario(key,replacement=null,job=null){
 const calls=[],elements={file:{files:[{name:'report.html',size:42}]},kind:{value:'file'},title:{value:'Draft report'},summary:{value:'Unsaved description'},access:{value:'password'},password:{value:'draft-password',dispatchEvent(){}},password_hint:{value:''},key:{value:key}};
 const form={elements},control={hidden:true,disabled:false,textContent:''};
 const context={state:{session:{authenticated:true},settings:{limits:{file_bytes:1000,upload_payload_bytes:1000}},upload:null},AbortController,Event,encodeURIComponent,
 $:()=>control,t:x=>x,readExpiry:()=>({mode:'never'}),readPolicy:()=>({purge_mode:'immediate'}),RequestError:class extends Error{},
 api:async(path,options)=>{calls.push({path,body:options.body});throw Error('Stop before file delivery');},showError(){}};
 vm.createContext(context);vm.runInContext(code,context);await context.upload(form,replacement,job);
 return calls[0];
}
const custom=await scenario('test1');assert.equal(custom.body.key,'test1');assert.equal(custom.body.password,'draft-password');
assert.equal(Object.hasOwn((await scenario('')).body,'key'),false);
assert.equal(Object.hasOwn((await scenario('test1',{id:'existing',current_revision:'revision',content_state:'present'})).body,'key'),false);
const job=await scenario('agent-report',null,{id:'fixture-job'});assert.equal(job.path,'/upload-jobs/fixture-job/start');assert.equal(job.body.key,'agent-report');
''')
