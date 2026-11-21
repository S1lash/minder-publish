"""Secret delivery checks authority again after async network and clipboard work."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_secret_copy_drops_stale_responses_and_clipboard_fallbacks():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for secret delivery checks')
    module = Path(__file__).parents[1] / 'src/minder_publish/server/web/secret-copy.js'
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');
const {copyOwnerSecret}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let current=true,writes=[],fallbacks=[],copied=0,loads=0;
const options={isCurrent:()=>current,load:async()=>{loads++;return {password:'fixture-only'};},
 write:async value=>writes.push(value),fallback:value=>fallbacks.push(value),onCopied:()=>copied++};
await copyOwnerSecret(options);assert.deepEqual(writes,['fixture-only']);assert.equal(copied,1);
current=false;await copyOwnerSecret(options);assert.equal(loads,1);
current=true;let finish;
const pending=copyOwnerSecret({...options,load:()=>new Promise(resolve=>finish=resolve)});
current=false;finish({password:'stale'});await pending;
assert.deepEqual(writes,['fixture-only']);assert.deepEqual(fallbacks,[]);
current=true;
await copyOwnerSecret({...options,write:async()=>{throw Error('Clipboard unavailable');}});
assert.deepEqual(fallbacks,['fixture-only']);assert.equal(copied,1);
let rejectWrite;
const awaitingClipboard=copyOwnerSecret({...options,write:()=>new Promise((resolve,reject)=>rejectWrite=reject)});
await new Promise(resolve=>setImmediate(resolve));current=false;rejectWrite(Error('denied'));await awaitingClipboard;
assert.deepEqual(fallbacks,['fixture-only']);assert.equal(copied,1);
current=true;
for(const value of [null,{}, {password:''},{password:17}]){
 await assert.rejects(copyOwnerSecret({...options,load:async()=>value}));
}
assert.deepEqual(writes,['fixture-only']);assert.deepEqual(fallbacks,['fixture-only']);
console.log('current/stale/network/clipboard/fallback/malformed secret delivery passed');
'''
    result = subprocess.run([node, '--input-type=module', '-', str(module)], input=script,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_navigation_clears_an_already_revealed_password_without_touching_upload_state():
    import json

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for secret navigation checks')
    source = (Path(__file__).parents[1] / 'src/minder_publish/server/web/app.js').read_text()
    script = 'const source=' + json.dumps(source) + ';\n' + r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const start=source.indexOf('async function navigate('),end=source.indexOf('function statusFilter(',start);
const input={value:'fixture-secret'},panel={hidden:false},modal={open:true};
const upload={fixture:true}, pending={aborted:false,abort(){this.aborted=true;}};
const context={state:{session:{authenticated:true},page:'materials',dialogEpoch:0,generation:0,upload,listRequest:pending},
 pageNames:{settings:'Settings'},location:{pathname:'/'},
 $:selector=>selector==='#saved-password-copy'?input:selector==='[data-password-fallback]'?panel:selector==='#dialog'?modal:{hidden:false},
 refreshChrome(){},renderSettings(){},formChange(){},cancelOwnerConfirmation(){},clearPasswordEditors(){}};
vm.createContext(context);
const helperStart=source.indexOf('function clearPublicationPasswordFields('),helperEnd=source.indexOf('async function copyMaterialPassword(',helperStart);
if(helperStart>=0)vm.runInContext(source.slice(helperStart,helperEnd),context);
const cancelStart=source.indexOf('function cancelMaterialRequest('),cancelEnd=source.indexOf('async function loadMaterials(',cancelStart);
vm.runInContext(source.slice(cancelStart,cancelEnd),context);
vm.runInContext(source.slice(start,end),context);
(async()=>{await context.navigate('settings');assert.equal(input.value,'');assert.equal(panel.hidden,true);assert.equal(context.state.upload,upload);assert.equal(pending.aborted,true);})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
