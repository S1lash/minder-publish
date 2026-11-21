"""Legacy metadata keeps its raw title while every display surface has a name."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src/minder_publish/server/web"


class RenderedElements(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(element)
        if tag not in {"input", "img", "br", "hr"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    def one(self, attribute, value):
        matches = [
            element
            for element in self.elements
            if element["attrs"].get(attribute) == value
        ]
        assert len(matches) == 1, matches
        return matches[0]


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for UI checks")
def test_migrated_material_display_names_do_not_rewrite_editable_metadata():
    source = (WEB / "app.js").read_text()
    password_editor = (WEB / "password-editor.js").read_text()
    harness = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=SOURCE;
const slice=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
const line=start=>source.slice(source.indexOf(start),source.indexOf('\n',source.indexOf(start)));
const nodes=new Map();
const modal={open:false,dataset:{},classList:{toggle(){}},close(){this.open=false;},showModal(){this.open=true;}};
const form={elements:{},addEventListener(){}};
const context={state:{session:{authenticated:true},dialogEpoch:0,items:[],layout:'list',offset:0,total:1,limit:50,settings:{defaults:{},operations:{change_summary_characters:500},limits:{title_characters:200,summary_characters:500,password_hint_characters:200}}},
 document:{documentElement:{lang:'en'}},location:{href:'https://publish.test/'},URL,encodeURIComponent,
 t:(key,values={})=>key.replace(/\{(\w+)\}/g,(match,name)=>values[name]??match),
 $:selector=>selector==='#dialog'?modal:selector==='#material-form'||selector==='#upload-form'?form:(nodes.get(selector)||nodes.set(selector,{innerHTML:'',textContent:'',addEventListener(){}}).get(selector)),
 $$:()=>[],publicationLink:{close(){}},clearPasswordEditors(){},mountPasswordEditors(){},mountStatistics:async()=>{},icon:()=>'<svg></svg>',bytes:()=>'',date:()=>'',formChange(){},syncForm(){},policyFields:()=>'',expiryFields:()=>'',showError(){},api:async()=>context.material};
vm.createContext(context);
vm.runInContext(PASSWORD_CONTROL,context);
vm.runInContext([line('const escape ='),line('const label ='),line('const accessName ='),
 slice('function button(','function safeURL('),slice('function safeURL(','async function copyText('),
 slice('function materialStatus(','async function loadMaterials('),
 slice('async function details(','async function renderUploadJob(')].join('\n'),context);
const cases=[
 {id:'legacy-file',title:'',filename:'legacy & "report".html',content_type:'text/html'},
 {id:'legacy-no-name',title:' \t ',filename:'   ',content_type:'text/html'},
 {id:'named',title:'  Owner <chosen> title  ',filename:'named.html',content_type:'text/html'},
 {id:'download',title:null,filename:'document.pdf',preview:'download',content_type:'application/pdf',access:'password',has_password:true,password_copy_available:true}];
(async()=>{const result=[];
 for(const entry of cases){
  const material={kind:'file',access:'link',summary:'Description with <angles> & a full sentence.',content_state:'present',files:[],guest_open_count:12,owner_open_count:3,stable_url:'https://publish.test/p/'+entry.id,...entry};
  const original=JSON.stringify(material);context.material=material;context.state.items=[material];
  const rendered={raw:entry};
  for(const layout of ['list','grid']){context.state.layout=layout;context.renderRows();rendered[layout]=nodes.get('#materials').innerHTML;}
  await context.details(material.id);rendered.details=modal.innerHTML;
  await context.preview(material.id);rendered.preview=modal.innerHTML;
  context.uploadDialog(material);rendered.replacement=modal.innerHTML;
  context.uploadDialog();rendered.initial=modal.innerHTML;
  assert.equal(JSON.stringify(material),original,'rendering rewrote publication metadata');
  result.push(rendered);
 }
 console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", json.dumps(source), 1).replace("PASSWORD_CONTROL", json.dumps(password_editor[password_editor.index("export function passwordEditorControl"):password_editor.index("export function generatePublicationPassword")].replace("export function", "function")), 1)
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    expected_names = (
        'legacy & "report".html',
        "legacy-no-name",
        "Owner <chosen> title",
        "document.pdf",
    )
    for item, expected in zip(rendered, expected_names):
        for layout in ("list", "grid"):
            dom = RenderedElements(item[layout])
            title = dom.one("class", "material-title")
            assert title["text"] == expected
            assert title["attrs"]["data-tooltip-overflow"] == ".material-summary"
            assert dom.one("class", "material-summary")["text"] == "Description with <angles> & a full sentence."
            count = dom.one("class", "material-statistics")
            assert count["text"] == "15"
            assert count["attrs"]["data-tooltip"] == "Guests 12 · You 3"
            assert "data-tooltip-toggle" in count["attrs"]
            labels = [e["attrs"]["aria-label"] for e in dom.elements if "aria-label" in e["attrs"]]
            assert labels == [
                f"Open details: {expected}",
                "Opening statistics · 15",
                f"Preview: {expected}",
                f"Open in a new tab: {expected}",
                f"Copy link: {expected}",
                f"Details: {expected}",
            ]
        # Both creation and editing expose draft tools without embedding a password.
        for surface in ("details", "initial"):
            controls = RenderedElements(item[surface]).elements
            inputs = [e for e in controls if "data-draft-password" in e["attrs"]]
            assert len(inputs) == 1 and inputs[0]["attrs"]["type"] == "password"
            assert inputs[0]["attrs"]["name"] == "password"
            assert "value" not in inputs[0]["attrs"]
            for attribute in ("data-copy-draft-password", "data-generate-password"):
                buttons = [e for e in controls if attribute in e["attrs"]]
                assert len(buttons) == 1 and buttons[0]["attrs"]["type"] == "button"
            manual = [e for e in controls if "data-draft-password-copy" in e["attrs"]]
            assert len(manual) == 1 and "name" not in manual[0]["attrs"]
        initial = RenderedElements(item["initial"])
        custom_key = initial.one("name", "key")["attrs"]
        assert custom_key["minlength"] == "2" and custom_key["maxlength"] == "64"
        assert custom_key["placeholder"] == "test1" and "required" not in custom_key
        assert not [e for e in RenderedElements(item["replacement"]).elements if e["attrs"].get("name") == "key"]
        details = RenderedElements(item["details"])
        assert expected in details.one("id", "dialog-title")["text"]
        assert details.one("id", "material-title")["attrs"]["value"] == (item["raw"]["title"] or "")
        assert details.one("id", "material-summary")["text"] == "Description with <angles> & a full sentence."
        assert item["details"].index('role="tablist"') < item["details"].index('class="dialog-body"')
        overview = details.one("id", "material-pane-overview")
        assert "hidden" not in overview["attrs"]
        assert "Guests" in overview["text"] and "You" in overview["text"]
        assert details.one("data-opening-count", "guest")["text"] == "12"
        assert details.one("data-opening-count", "owner")["text"] == "3"
        for key in ("access", "files", "statistics"):
            assert "hidden" in details.one("id", f"material-pane-{key}")["attrs"]
            assert details.one("id", f"material-tab-{key}")["attrs"]["aria-controls"] == f"material-pane-{key}"
        preview = RenderedElements(item["preview"])
        assert preview.one("id", "dialog-title")["text"] == expected
        frames = [element for element in preview.elements if element["tag"] == "iframe"]
        if item["raw"].get("preview") != "download":
            assert len(frames) == 1
            assert frames[0]["attrs"]["title"] == f"Preview: {expected}"
        else:
            assert not frames
        assert expected in RenderedElements(item["replacement"]).one("class", "dialog-body")["text"]
