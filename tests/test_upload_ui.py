"""Late upload responses cannot repaint a different owner session."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_late_commit_and_cancel_leave_a_new_owner_dialog_untouched():
    source = (WEB / "app.js").read_text()
    functions = source[
        source.index("async function completePrepared(") : source.index(
            "async function upload("
        )
    ]
    harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
async function scenario(name,fail=false){
 let release,reject,closed=0,navigated=0,errors=0;
 const button={disabled:false,textContent:''},dialog={close(){closed++;}};
 const current={id:'old-upload',phase:'review',controller:new AbortController(),cancelling:false};
 const session={authenticated:true};
 const state={session,upload:current,page:'materials'};
 const context={state,AbortController,encodeURIComponent,t:x=>x,
  $:selector=>selector==='#dialog'?dialog:button,$$:()=>[],
  api:()=>new Promise((resolve,fail)=>{release=resolve;reject=fail;}),
  notify:()=>{},navigate:async()=>{navigated++;},loadMaterials:async()=>{navigated++;},
  showError:()=>errors++};
 vm.createContext(context);vm.runInContext(CODE,context);
 const operation=context[name]();
 const replacement={id:'new-upload'};state.upload=replacement;state.session={authenticated:true};
 if(fail)reject(new Error('Old request failed'));else release({state:'aborted'});
 await operation;
 assert.equal(state.upload,replacement,name+' replaced new upload');
 assert.equal(closed,0,name+' closed new dialog');assert.equal(navigated,0);assert.equal(errors,0);
}
(async()=>{for(const name of ['commitPrepared','cancelUpload']){await scenario(name);await scenario(name,true);}})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("CODE", json.dumps(functions), 1)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_login_continuations_are_only_local_oauth_consent_or_upload_jobs():
    source = (WEB / "routes.js").read_text().replace("export function", "function")
    harness = (
        source
        + r"""
const assert=require('node:assert/strict');
for(const value of [null,'https://evil.test/','//evil.test/','/\\evil.test/',
 '/oauth/consent?request='+ 'a'.repeat(43)+'&next=https://evil.test',
 '/oauth/consent?request='+ 'a'.repeat(42),'/upload/'+ 'a'.repeat(32)+'/extra',
 '/upload/'+ 'a'.repeat(32)+'?next=evil','javascript:alert(1)','/login','/'])assert.equal(safeReturnTo(value),null);
for(const value of ['/upload/'+ 'a'.repeat(32),'/oauth/consent?request='+ 'a'.repeat(43)])assert.equal(safeReturnTo(value),value);
"""
    )
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_lost_commit_response_checks_the_same_upload_without_republishing():
    source = (WEB / "app.js").read_text()
    functions = source[
        source.index("async function completePrepared(") : source.index(
            "async function cancelUpload()"
        )
    ]
    harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
(async()=>{
 for(const status of ['committed','prepared','committing']){
  let closed=0,navigated=0,capacityInvalidations=0;const calls=[];
  const button={disabled:false,textContent:'',dataset:{action:'commit-upload'}};
  const current={id:'known-upload',phase:'review',controller:new AbortController(),cancelling:false};
  const state={session:{authenticated:true},upload:current};
  const context={state,encodeURIComponent,t:x=>x,RequestError:class extends Error{},
   capacity:{invalidate(){capacityInvalidations++;}},
   $:selector=>selector==='#dialog'?{close(){closed++;}}:button,
   api:async(path,options={})=>{calls.push([path,options.method||'GET']);if(options.method==='POST')throw new TypeError('Response lost');return {state:status};},
   notify:()=>{},showError:()=>{},navigate:async()=>{navigated++;},renderUploadJob:async()=>{navigated++;}};
  vm.createContext(context);vm.runInContext(CODE,context);await context.commitPrepared();
  assert.deepEqual(calls,[['/uploads/known-upload/commit','POST'],['/uploads/known-upload','GET']]);
  assert.equal(capacityInvalidations,status==='committed'?1:0);
  if(status==='committed'){assert.equal(closed,1);assert.equal(navigated,1);assert.equal(state.upload,null);}
  else {assert.equal(closed,0);assert.equal(navigated,0);assert.equal(state.upload,current);assert.equal(current.phase,status==='prepared'?'review':'unknown');assert.equal(button.dataset.action,status==='prepared'?'commit-upload':'check-upload');}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("CODE", json.dumps(functions), 1)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_dependency_review_does_not_claim_incomplete_or_empty_scan_is_clean():
    source = (WEB / "app.js").read_text()
    function = source[
        source.index("function dependencyReport(") : source.index("function preparedReview(")
    ]
    harness = r"""
const assert=require('node:assert/strict');
const escape=String,label=String,t=String,bytes=String;
const warning='Some resources were not checked';
const clean='No issues found';
const incomplete=dependencyReport({
  static_scan_complete:false,truncated:false,scanned_files:1
});
assert.ok(incomplete.includes(warning));
assert.ok(!incomplete.includes(clean));
const empty=dependencyReport({scanned_files:0});
assert.ok(empty.includes('No HTML or CSS was checked'));
assert.ok(!empty.includes(clean));
assert.ok(dependencyReport({scanned_files:1}).includes(clean));
assert.ok(dependencyReport({truncated:true,scanned_files:1}).includes(warning));
"""
    result = subprocess.run(
        ["node"], input=function+harness, text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
