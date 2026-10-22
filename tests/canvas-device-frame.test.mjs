import test from "node:test";
import assert from "node:assert/strict";
import {runInNewContext} from "node:vm";
import {deviceFrameScript,deviceFrameStyles} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-device-frame.mjs";
function fixture(id="ios~one") {
  const nodes = new Map(), calls=[], listeners={};
  const node = name => {
    if(!nodes.has(name)) nodes.set(name,{value:"",attrs:{},style:{values:{},setProperty(k,v){this.values[k]=v}},
      setAttribute(k,v){this.attrs[k]=v},getAttribute(k){return this.attrs[k]},
      addEventListener(k,fn){listeners[name+":"+k]=fn}});
    return nodes.get(name);
  };
  const desc={id:"skin",model:"Pixel 6 Pro",source:"android-sdk-skin",bodySize:{width:1527,height:3289},
    screenRect:{x:41,y:72,width:1440,height:3120},layers:[{image:"data:image/png;base64,body"}],
    screenMask:{image:"data:image/png;base64,mask"},rotation:0,fallbackReason:null};
  const context={$:node,device:node("device"),image:node("screen"),video:node("video"),rotation:0,
    DEVICE_ID:id,PLATFORM:"android",view:{preset:"default"},localStorage:{getItem(){return null},setItem(k,v){calls.push(["store",k,v])}},
    async post(path,body){calls.push([path,body]);return {result:desc}}};
  runInNewContext(deviceFrameScript(),context);
  return {context,node,calls,listeners,desc};
}
test("descriptor positions only screen surfaces; repeated sizes and tile resizes add no frame work",async()=>{
  const {context,node,calls}=fixture();
  await context.refreshDeviceFrame(1440,3120);
  for(let n=0;n<100;n++)await context.refreshDeviceFrame(1440,3120);
  assert.equal(calls.length,1);
  assert.equal(node("device").style.aspectRatio,"1527 / 3289");
  assert.equal(node("device").style.values["--screen-x"],41/1527*100+"%");
  assert.equal(node("device-frame-source").textContent,"Pixel 6 Pro");
  assert.match(deviceFrameStyles(),/pointer-events:none/);
  assert.doesNotMatch(deviceFrameScript(),/requestAnimationFrame|ResizeObserver|drawImage/);
});
test("None removes layers and masks; capture snapshots and preferences are isolated per device",async()=>{
  const {context,node,calls,listeners}=fixture("ios~a");
  await context.refreshDeviceFrame(1206,2622);
  const snapshot=context.captureFrameOptions();
  node("device-frame-style").checked=false;listeners["device-frame-style:change"]();
  assert.equal(JSON.stringify(context.captureFrameOptions()),"{}");
  assert.equal(snapshot.frame.mode,"auto");
  assert.equal(node("device").attrs["data-frame"],"none");
  assert.equal(node("video").style.values["mask-image"],"none");
  assert.deepEqual(calls.at(-1),["store","autonom.canvas.frame.ios~a","none"]);
  assert.equal(JSON.stringify(fixture("ios~b").context.captureFrameOptions()),"{}");
});
test("late geometry cannot restore a frame after None",async()=>{
  const {context,node,listeners,desc}=fixture();let resolve;
  context.post=()=>new Promise(r=>resolve=r);
  const pending=context.refreshDeviceFrame(1440,3120);
  node("device-frame-style").checked=false;listeners["device-frame-style:change"]();
  resolve({result:desc});await pending;
  assert.equal(node("device").attrs["data-frame"],"none");
  assert.equal(node("device-frame-layer").hidden,true);
});

test("a new geometry never reuses the previous orientation mask while loading",async()=>{
  const {context,node,desc}=fixture();
  await context.refreshDeviceFrame(1440,3120);
  let resolve;
  context.post=()=>new Promise(r=>resolve=r);
  const pending=context.refreshDeviceFrame(3120,1440);
  assert.equal(node("device-frame-layer").hidden,true);
  assert.equal(node("video").style.values["mask-image"],"none");
  assert.equal(node("device").style.aspectRatio,"3120 / 1440");
  resolve({result:desc});await pending;
});

test("selected model changes invalidate same-size frames; unavailable artwork has no generic border",async()=>{
  const {context,node,calls,desc}=fixture("android~one");
  await context.refreshDeviceFrame(1440,3120);
  context.view.preset="pixel-10";
  context.post=async(path,body)=>{calls.push([path,body]);return {result:{...desc,source:"generic",model:"Pixel 10",layers:[],screenMask:null}}};
  await context.refreshDeviceFrame(1440,3120);
  assert.equal(calls.at(-1)[1].payload.preset,"pixel-10");
  assert.equal(node("device-frame-layer").hidden,true);
  assert.equal(node("video").style.values["mask-image"],"none");
  assert.equal(node("device").attrs["data-frame-source"],"none");
  assert.equal(node("device-frame-source").textContent,"Frame unavailable for Pixel 10");
  assert.equal(JSON.stringify(context.captureFrameOptions()),"{}");
  context.view.preset="default";
  await context.refreshDeviceFrame(1440,3120);
  assert.equal(node("device-frame-layer").hidden,false);
  assert.equal(context.captureFrameOptions().frame.preset,"default");
});
