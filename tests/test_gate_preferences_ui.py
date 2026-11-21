"""Gate menu dismissal preserves native summary and preference form actions."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_gate_menus_outside_pointer_keyboard_exclusivity_and_native_submission():
    module = Path(__file__).parents[1] / "src/minder_publish/server/web/gate-preferences.js"
    assert module.is_file(), "Gate menus do not dismiss outside their native details"
    binary = shutil.which("node")
    if not binary:
        pytest.skip("Node is required for gate preference event checks")
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {mountGatePreferences}=await import(pathToFileURL(process.argv[2]));
class Node {
  constructor(parent=null){this.parent=parent;this.handlers=new Map();this.focused=0;}
  addEventListener(name,handler){const list=this.handlers.get(name)||[];list.push(handler);this.handlers.set(name,list);}
  removeEventListener(name,handler){this.handlers.set(name,(this.handlers.get(name)||[]).filter(value=>value!==handler));}
  contains(node){for(let value=node;value;value=value.parent)if(value===this)return true;return false;}
  focus(){this.focused++;}
  send(name,target=this,extra={}){
    const event={target,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},...extra};
    for(const handler of this.handlers.get(name)||[])handler(event);
    return event;
  }
}
const document=new Node();
function menu(){
  const value=new Node(document);value.open=false;
  value.summary=new Node(value);value.button=new Node(value);
  value.querySelector=selector=>{assert.equal(selector,'summary');return value.summary;};
  return value;
}
const language=menu(),theme=menu(),outside=new Node(document),field=new Node(document);
document.querySelectorAll=selector=>{assert.equal(selector,'.preferences .preference');return [language,theme];};
const unmount=mountGatePreferences(document);
function activate(value){
  value.summary.send('click');
  value.open=!value.open;
  value.send('toggle');
}
activate(language);assert.equal(language.open,true);
const insidePointer=document.send('pointerdown',language.button);
const insideClick=document.send('click',language.button);
assert.equal(language.open,true);assert.equal(insidePointer.defaultPrevented,false);assert.equal(insideClick.defaultPrevented,false);
let submissions=0;if(!insideClick.defaultPrevented)submissions++;
assert.equal(submissions,1,'A preference button retains its native form submission');
document.send('pointerdown',outside);assert.equal(language.open,false);
activate(language);document.send('click',outside);assert.equal(language.open,false,'Keyboard/AT outside click also dismisses');
activate(language);
activate(theme);assert.equal(theme.open,true);assert.equal(language.open,false,'Native summary activation closes the other menu');
const escape=document.send('keydown',theme.button,{key:'Escape'});
assert.equal(escape.defaultPrevented,true);assert.equal(theme.open,false);assert.equal(theme.summary.focused,1);
const ignored=document.send('keydown',field,{key:'Escape'});assert.equal(ignored.defaultPrevented,false,'An unrelated Escape keeps its native action');
activate(language);
const tab=document.send('keydown',language.button,{key:'Tab'});assert.equal(tab.defaultPrevented,false);assert.equal(language.open,true);
const enter=document.send('keydown',language.summary,{key:'Enter'});assert.equal(enter.defaultPrevented,false,'Native keyboard activation must not be replaced');
activate(theme);assert.equal(language.open,false);assert.equal(theme.open,true);
const outsideEscape=document.send('keydown',field,{key:'Escape'});
assert.equal(outsideEscape.defaultPrevented,true);assert.equal(theme.open,false);assert.equal(theme.summary.focused,2);
language.open=true;language.send('toggle');theme.open=true;theme.send('toggle');
assert.equal(language.open,false);assert.equal(theme.open,true,'Programmatic/native toggle also enforces exclusivity');
unmount();document.send('pointerdown',outside);assert.equal(theme.open,true,'Unmount removes document listeners');
console.log('pointer/click/escape/focus/native forms/keyboard/toggle gate menu checks passed');
'''
    result = subprocess.run(
        [binary, "--input-type=module", "-", str(module)], input=script,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_theme_enhancement_preserves_password_focus_and_language_preference():
    module = Path(__file__).parents[1] / "src/minder_publish/server/web/gate-preferences.js"
    binary = shutil.which("node")
    if not binary:
        pytest.skip("Node is required for gate preference event checks")
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {mountGateTheme}=await import(pathToFileURL(process.argv[2]));
const handlers=new Map();
const password={value:'typed secret',focus(){root.activeElement=this;}};
const target={focus(){root.activeElement=this;}};
const languageTheme={value:'system'},meta={content:'light dark'};
const form={addEventListener:(key,fn)=>handlers.set(key,fn),removeEventListener:key=>handlers.delete(key),contains:node=>node===target,querySelector:()=>target};
const root={documentElement:{dataset:{theme:'system'}},defaultView:{matchMedia:()=>({matches:true}),location:{protocol:'https:'}},activeElement:password,body:{},cookie:'',querySelector(selector){return ({'.gate-theme-toggle':form,'.preference input[name="theme"]':languageTheme,'meta[name="color-scheme"]':meta})[selector]||null;}};
const unmount=mountGateTheme(root);
handlers.get('pointerdown')();
root.activeElement=target;
let prevented=false;handlers.get('submit')({preventDefault(){prevented=true;}});
assert.equal(prevented,true);assert.equal(root.documentElement.dataset.theme,'light');
assert.equal(password.value,'typed secret');assert.equal(root.activeElement,password);
assert.equal(languageTheme.value,'light');assert.equal(meta.content,'light');
assert.match(root.cookie,/^mp_theme=light; Path=\/; Max-Age=31536000; SameSite=Lax; Secure$/);
handlers.get('pointerdown')();handlers.get('submit')({preventDefault(){}});
assert.equal(root.documentElement.dataset.theme,'dark');assert.equal(languageTheme.value,'dark');
unmount();assert.equal(handlers.size,0);
'''
    result = subprocess.run(
        [binary, "--input-type=module", "-", str(module)], input=script,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
