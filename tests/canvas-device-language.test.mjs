import test from "node:test";
import assert from "node:assert/strict";
import {runInNewContext} from "node:vm";
import {deviceLanguageScript} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-device-language.mjs";
function fixture(platform="android") {
 const nodes=new Map(),calls=[],listeners={};
 const node=id=>{if(!nodes.has(id))nodes.set(id,{value:"",hidden:true,children:[],replaceChildren(...items){this.children=items},append(item){this.children.push(item)},focus(){},addEventListener(event,fn){listeners[id+":"+event]=fn}});return nodes.get(id)};
 const ctx={$:node,PLATFORM:platform,canSendInput:()=>true,note(){},document:{createElement:()=>({})},async post(path,body){calls.push(body);return {result:{language:body.payload.language||"en-US"}}}};
 runInNewContext(deviceLanguageScript(),ctx);return {ctx,node,calls,listeners};
}
test("reads current device language, including tags absent from the preset list",async()=>{
 const f=fixture();f.ctx.post=async()=>({result:{language:"sv-SE"}});await f.ctx.refreshDeviceLanguage();
 assert.equal(f.node("device-language").value,"sv-SE");assert.equal(f.node("device-language").disabled,false);
 assert.equal(f.node("device-language").children.some(o=>o.value==="sv-SE"),true);
});
test("failed change keeps the last observed language and exposes failure",async()=>{
 const f=fixture();await f.ctx.refreshDeviceLanguage();f.ctx.post=async()=>{throw Error("Device disconnected")};
 await f.ctx.applyDeviceLanguage("de-DE");assert.equal(f.node("device-language").value,"en-US");
 assert.equal(f.node("device-language-message").textContent,"Device disconnected");assert.equal(f.node("device-language").disabled,false);
});
test("iOS selection waits for explicit restart and cancel performs no mutation",async()=>{
 const f=fixture("ios");await f.ctx.refreshDeviceLanguage();f.node("device-language").value="de-DE";f.listeners["device-language:change"]();
 assert.equal(f.node("device-language-confirm").hidden,false);assert.equal(f.node("device-language").value,"en-US");assert.equal(f.calls.length,1);
 f.listeners["device-language-cancel:click"]();assert.equal(f.node("device-language-confirm").hidden,true);assert.equal(f.calls.length,1);
});
test("confirmed restart sends the selected language and updates only after reply",async()=>{
 const f=fixture("ios");await f.ctx.refreshDeviceLanguage();f.node("device-language").value="de-DE";f.listeners["device-language:change"]();
 await f.ctx.applyDeviceLanguage("de-DE",true);assert.equal(f.calls.at(-1).op,"device.language.set");assert.equal(f.calls.at(-1).payload.restart,true);assert.equal(f.node("device-language").value,"de-DE");
});
test("late reads cannot undo a successfully applied language",async()=>{
 const f=fixture();await f.ctx.refreshDeviceLanguage();let resolve;
 f.ctx.post=()=>new Promise(r=>resolve=r);const read=f.ctx.refreshDeviceLanguage();
 f.ctx.post=async()=>({result:{language:"de-DE"}});await f.ctx.applyDeviceLanguage("de-DE");resolve({result:{language:"en-US"}});await read;
 assert.equal(f.node("device-language").value,"de-DE");
});
test("lost input lease prevents language changes",async()=>{
 const f=fixture();await f.ctx.refreshDeviceLanguage();f.ctx.canSendInput=()=>false;f.ctx.syncLanguageControl();
 assert.equal(f.node("device-language").disabled,true);await f.ctx.applyDeviceLanguage("de-DE");assert.equal(f.calls.length,1);
});
