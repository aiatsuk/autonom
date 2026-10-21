import assert from 'node:assert/strict';
import test from 'node:test';
import { runInNewContext, Script } from 'node:vm';
import { themeScript, themeHead, themeStyles } from '../plugins/autonom/skills/android-emulator-browser/scripts/canvas-theme-page.mjs';
import { renderPage } from '../plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs';
import { renderWorkspacePage } from '../plugins/autonom/skills/android-emulator-browser/scripts/canvas-workspace-page.mjs';

function boot({dark=false,saved=null,blocked=false,loading=false,hasMedia=true}={}) {
  const attributes = new Map(), events = new Map(), controlEvents = new Map(), documentEvents = new Map(), written=[];
  let mediaChange;
  const control={value:'system',addEventListener:(name,fn)=>controlEvents.set(name,fn)};
  const media={matches:dark,addEventListener:(_,fn)=>{mediaChange=fn}};
  const root={setAttribute:(name,value)=>attributes.set(name,value)};
  const document={documentElement:root,readyState:loading?'loading':'complete',getElementById:()=>control,addEventListener:(name,fn)=>documentEvents.set(name,fn)};
  const context={document,window:{addEventListener:(name,fn)=>events.set(name,fn)},localStorage:{
    getItem:()=>{if(blocked)throw Error('blocked');return saved},
    setItem:(key,value)=>{if(blocked)throw Error('blocked');written.push([key,value])},
  }};
  if(hasMedia)context.matchMedia=()=>media;
  runInNewContext(themeScript(),context);
  return {attributes,control,written,key: event=>documentEvents.get("keydown")?.(event),ready:()=>documentEvents.get('DOMContentLoaded')?.(),
    os(value){media.matches=value;mediaChange?.()},
    choose(value){control.value=value;controlEvents.get('change')()},
    storage(key,newValue){events.get('storage')({key,newValue})}};
}

test('Auto resolves before DOM readiness and follows live system changes in both directions',()=>{
  const page=boot({dark:true,loading:true});
  assert.equal(page.attributes.get('data-theme'),'dark');
  assert.equal(page.attributes.get('data-theme-preference'),'system');
  page.ready();
  page.os(false);assert.equal(page.attributes.get('data-theme'),'light');
  page.os(true);assert.equal(page.attributes.get('data-theme'),'dark');
  assert.deepEqual(page.written,[],'automatic changes do not overwrite the preference');
});

test('saved manual override survives OS changes; returning to Auto immediately uses the current OS',()=>{
  const page=boot({dark:true,saved:'light'});
  assert.equal(page.attributes.get('data-theme'),'light');
  assert.equal(page.control.value,'light');
  page.os(false);page.os(true);
  assert.equal(page.attributes.get('data-theme'),'light');
  page.choose('dark');assert.equal(page.attributes.get('data-theme'),'dark');
  page.os(false);assert.equal(page.attributes.get('data-theme'),'dark');
  page.choose('system');assert.equal(page.attributes.get('data-theme'),'light');
  assert.deepEqual(page.written.at(-1),['autonom.canvas.theme','system']);
});

test('other Canvas documents synchronize preferences and storage clear restores Auto',()=>{
  const page=boot({dark:true});
  page.storage('unrelated','light');assert.equal(page.attributes.get('data-theme'),'dark');
  page.storage('autonom.canvas.theme','light');
  assert.equal(page.attributes.get('data-theme'),'light');assert.equal(page.control.value,'light');
  page.storage(null,null);assert.equal(page.attributes.get('data-theme'),'dark');assert.equal(page.control.value,'system');
  assert.deepEqual(page.written,[],'storage events do not echo writes');
});

test('invalid preference or unavailable storage/media keeps appearance usable',()=>{
  const invalid=boot({saved:'unexpected',dark:true});assert.equal(invalid.control.value,'system');
  const page=boot({blocked:true,hasMedia:false});
  assert.equal(page.attributes.get('data-theme'),'light');
  page.choose('dark');assert.equal(page.attributes.get('data-theme'),'dark');
});

test('the shared palette and pre-paint initialization reach device, embedded and workspace pages',()=>{
  const page=renderPage({serial:'fixture',options:{platform:'android'}});
  const embedded=renderPage({serial:'fixture',options:{platform:'android'}},{embed:true});
  const workspace=renderWorkspacePage({});
  for(const html of [page,embedded,workspace]){
    assert.ok(html.indexOf(themeHead())<html.indexOf('<style>'));
    assert.ok(html.includes(themeStyles()));
  }
  assert.ok(page.includes('id="canvas-theme"'));assert.ok(workspace.includes('id="canvas-theme"'));
  assert.ok(!embedded.includes('id="canvas-theme"'),'embedded frames share preference without duplicate controls');
  assert.doesNotThrow(()=>new Script(themeScript()));
});
