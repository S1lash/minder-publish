"""OAuth preference controls and owner confirmation keep authority on the server."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_owner_confirmation_uses_session_login_and_clears_password_after_submit():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for owner confirmation checks")
    module = Path(__file__).parents[1] / "src/minder_publish/server/web/reauth.js"
    script = r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(process.argv[2],'utf8');
const {confirmOwner}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
let modal,submitted=[],removed=false;
const password={value:'fixture-only password',focus(){}},submit={disabled:false},alert={hidden:true};
const form={elements:{password,username:{value:'tampered field'}},reportValidity:()=>true,
 querySelector:()=>submit,addEventListener(name,callback){this[name]=callback;}};
globalThis.document={createElement:()=>modal={setAttribute(){},
 querySelector:selector=>selector==='form'?form:selector==='[role=alert]'?alert:{addEventListener(){}},
 addEventListener(){},showModal(){},close(){},remove(){removed=true;}},body:{append(){}}};
const session={username:'known" owner<&',authenticated:true};
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
const pending=confirmOwner({session,getSession:()=>session,t:value=>value,escape,
 request:async(...args)=>{submitted.push(args);return {recent_auth:true};}});
assert.match(modal.innerHTML, /name="username" type="text" autocomplete="username" value="known&quot; owner&lt;&amp;" readonly/);
assert.match(modal.innerHTML, /for="owner-confirm-username"/);
assert.match(modal.innerHTML, /name="password" type="password" autocomplete="current-password"/);
assert.match(modal.innerHTML, /method="post" action="\/api\/v1\/auth\/reauth"/);
await form.submit({preventDefault(){}});
assert.deepEqual(await pending,{recent_auth:true});
assert.deepEqual(submitted,[['fixture-only password']]);
assert.equal(password.value,'');assert.equal(removed,true);assert.equal(session.recent_auth,true);
console.log('trusted login association, escaped markup, password-only request and clearing passed');
'''
    result = subprocess.run(
        [node, "--input-type=module", "-", str(module)], input=script,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_oauth_preferences_only_write_existing_theme_and_language_cookies():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for OAuth preference checks")
    module = Path(__file__).parents[1] / "src/minder_publish/server/web/oauth.js"
    script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[2],'utf8');let writes=[],reloads=0;
const controls={hidden:true,addEventListener(name,callback){this[name]=callback;},querySelectorAll:()=>[]};
vm.runInNewContext(source,{document:{querySelector:()=>controls,addEventListener(){}},
 window:{PublishPreferences:{write:(...args)=>writes.push(args)}},location:{reload:()=>reloads++}});
assert.equal(controls.hidden,false);
const choose=(name,value)=>controls.click({target:{closest:()=>({dataset:{oauthPreference:name},value})}});
choose('mp_theme','dark');choose('mp_lang','fr');choose('owner_cookie','attacker');
assert.deepEqual(writes,[['mp_theme','dark'],['mp_lang','fr']]);assert.equal(reloads,2);
console.log('only theme/language cookies changed; no auth transport or target override');
'''
    result = subprocess.run(
        [node, "-", str(module)], input=script,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
