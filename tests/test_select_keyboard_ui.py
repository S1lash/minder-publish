"""Enhanced select menus preserve sequential keyboard navigation."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_select_tab_moves_through_page_and_wraps_modal_boundaries():
    binary = shutil.which("node")
    if not binary:
        pytest.skip("Node is required for select keyboard checks")
    source = Path(__file__).parents[1] / "src/minder_publish/server/web/controls.js"
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function scenario(modal, position, backwards, blocked=false) {
  let focused;
  const before = {id:'before'}, after = {id:'after'};
  class Element {
    constructor(tag) {
      this.tagName=tag; this.children=[]; this.attributes={}; this.style={};
      this.classList={add(){}}; this.tabIndex=0; this.disabled=false; this.isConnected=true;
    }
    setAttribute(name,value){this.attributes[name]=String(value);}
    getAttribute(name){return this.attributes[name] ?? null;}
    removeAttribute(name){delete this.attributes[name];}
    append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
    before(host){this.host=host;}
    remove(){this.isConnected=false;}
    focus(){focused=this;}
    getClientRects(){return [1];}
    getBoundingClientRect(){return {top:20,bottom:50,left:10,width:120};}
    closest(selector){if(selector==='dialog')return modal ? scope : null;return null;}
    addEventListener(){}
    dispatchEvent(){}
    querySelector(selector){return this.children.find(item=>item.attributes['aria-selected']==='true') || this.children[0];}
    querySelectorAll(){return this.children.filter(item=>!item.disabled);}
    contains(node){return this.children.includes(node);}
    get firstElementChild(){return this.children[0];}
  }
  const select = new Element('select'); select.id='fixture'; select.name='fixture';
  select.value='one'; select.multiple=false; select.size=0;
  select.options=[{value:'one',textContent:'One',selected:true,disabled:false}];
  select.selectedOptions=select.options;
  const scope={append(item){this.popup=item;},querySelectorAll(){return targets;}};
  const body={append(item){scope.popup=item;}};
  const document={body,get activeElement(){return focused;},createElement:tag=>new Element(tag),
    querySelector(){return null;},querySelectorAll(selector){if(selector==='select')return [select];if(selector==='.select-trigger')return [];return targets;},addEventListener(){}};
  const context={document,window:{addEventListener(){}},MutationObserver:class{observe(){}},
    innerHeight:700,innerWidth:320,CSS:{escape:x=>x},Event:class{},Date};
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
  const trigger=select.host.children.find(child=>child.className==='select-trigger');
  for(const item of [before,after])Object.assign(item,{tabIndex:0,disabled:false,getClientRects:()=>[1],closest:()=>null,focus(){focused=this;}});
  const targets=position==='first' ? [trigger,after] : position==='last' ? [before,trigger] : [before,trigger,after];
  if(blocked) targets.splice(targets.indexOf(trigger)+(backwards?0:1),0,{id:'unfocusable',tabIndex:0,disabled:false,getClientRects:()=>[1],closest:()=>null,focus(){}});
  trigger.onclick();
  const option=scope.popup.children[0];
  let prevented=false;
  option.onkeydown({key:'Tab',shiftKey:backwards,preventDefault(){prevented=true;}});
  if(!prevented){
    const index=targets.indexOf(focused), next=index+(backwards?-1:1);
    focused=targets[modal ? (next+targets.length)%targets.length : next];
  }
  const expected=!modal && (backwards && position==='first' || !backwards && position==='last') ? undefined : backwards ? (position==='first' ? after : before) : (position==='last' ? before : after);
  return focused===expected ? null : `${modal?'modal':'page'} ${position} ${backwards?'Shift+Tab':'Tab'}: expected ${expected?.id}, got ${focused?.id}`;
}
const failures=[];
for(const backwards of [false,true]) {
  failures.push(scenario(true, backwards?'first':'last', backwards));
  failures.push(scenario(true, 'middle', backwards));
  failures.push(scenario(false, 'middle', backwards));
  failures.push(scenario(true, 'middle', backwards, true));
  failures.push(scenario(false, backwards?'first':'last', backwards));
}
assert.deepEqual(failures.filter(Boolean),[]);
'''
    result = subprocess.run(
        [binary, "-", str(source)], input=script, text=True,
        capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
