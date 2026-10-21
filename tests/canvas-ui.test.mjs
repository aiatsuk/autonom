import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { canvasIcon, canvasIconButton, canvasTabList, canvasTabPanel, canvasDomScript, canvasSelectTab, canvasTabKey, bindCanvasTabs } from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-ui.mjs";

function node() {
  return {disabled:false, hidden:false, attributes:{}, listeners:{},
    setAttribute(k,v){this.attributes[k]=v},focus(){this.focused=true},
    addEventListener(k,f){this.listeners[k]=f}};
}
test("shared tabs wrap, skip disabled tabs and leave modified or unrelated keys to the host",()=>{
  const tabs=[node(),node(),node()];tabs[1].disabled=true;
  const key=(key,index,extra={})=>canvasTabKey({key,preventDefault(){},...extra},tabs,index);
  assert.equal(key("ArrowRight",0),2);assert.equal(key("ArrowRight",2),0);
  assert.equal(key("ArrowLeft",0),2);assert.equal(key("Home",2),0);assert.equal(key("End",0),2);
  assert.equal(key("Tab",0),null);assert.equal(key("ArrowRight",0,{altKey:true}),null);
  tabs.forEach(t=>t.disabled=true);assert.equal(key("Home",0),null);
});
test("shared tab controller selects one panel, moves focus and never activates disabled tabs",()=>{
  const tabs=[node(),node(),node()],panels=[node(),node(),node()],list=node();
  let selected=0,calls=0;
  bindCanvasTabs(list,tabs,()=>selected,(i,focus)=>{selected=i;calls++;canvasSelectTab(tabs,panels,i,focus)});
  tabs[1].listeners.click();assert.equal(selected,1);assert.equal(calls,1);
  assert.deepEqual(panels.map(p=>p.hidden),[true,false,true]);
  assert.deepEqual(tabs.map(t=>t.attributes.tabindex),["-1","0","-1"]);
  list.listeners.keydown({key:"End",preventDefault(){}});
  assert.equal(selected,2);assert.equal(tabs[2].focused,true);
  tabs[0].disabled=true;tabs[0].listeners.click();assert.equal(calls,2);
  assert.equal(canvasSelectTab(tabs,panels,0),false);
});
test("inline DOM factories preserve literal user text, button type and accessible attributes",()=>{
  const result=runInNewContext(canvasDomScript()+';({element:el("span",null,"<img src=x>"),button:makeButton("<script>","btn",{"aria-label":"Save"})})',
    {document:{createElement:tag=>({...node(),tag})}});
  assert.equal(result.element.textContent,"<img src=x>");
  assert.equal(result.button.textContent,"<script>");
  assert.equal(result.button.attributes.type,"button");
  assert.equal(result.button.attributes["aria-label"],"Save");
});
test("markup primitives escape labels and connect tabs to their panels",()=>{
  assert.match(canvasIconButton("close",'Close "<x>"'),/aria-label="Close &quot;&lt;x&gt;&quot;"/);
  assert.match(canvasIcon("close"),/aria-hidden="true" focusable="false"/);
  assert.throws(()=>canvasIcon("missing"),/Unknown Canvas icon/);
  const list=canvasTabList([{id:"a",label:"<A>"},{id:"b",label:"B"}],"example","tab");
  assert.match(list,/aria-controls="example-panel-a"/);assert.match(list,/&lt;A&gt;/);
  assert.match(canvasTabPanel("b","content","example","panel",false),/aria-labelledby="example-tab-b" tabindex="0" hidden/);
});
