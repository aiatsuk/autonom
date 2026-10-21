import assert from 'node:assert/strict';
import test from 'node:test';
import { CanvasDropdown } from '../plugins/autonom/skills/android-emulator-browser/scripts/canvas-dropdown.mjs';

function fixture() {
  const doc = {activeElement:null, listeners:new Map(), addEventListener(type, fn) { this.listeners.set(type, [...this.listeners.get(type) || [], fn]); }};
  function node() {
    const classes = new Set(), attrs = new Map();
    return {ownerDocument:doc, disabled:false, listeners:new Map(),
      classList:{add:v=>classes.add(v),remove:v=>classes.delete(v),contains:v=>classes.has(v)},
      setAttribute:(k,v)=>attrs.set(k,v),getAttribute:k=>attrs.get(k),
      focus(){doc.activeElement=this},contains(other){return other===this},
      addEventListener:doc.addEventListener};
  }
  const trigger=node(),menu=node(),items=['a','b','c'].map(value=>({value,node:node()})),changes=[];
  menu.contains=target=>target===menu||items.some(item=>item.node===target);
  let value='b';
  const dropdown=new CanvasDropdown({trigger,menu,items:()=>items,value:()=>value,choose:v=>{value=v;changes.push(v)}});
  function fire(target,type,extra={}) {
    const event={target,defaultPrevented:false,stopped:false,detail:1,...extra,preventDefault(){this.defaultPrevented=true},stopPropagation(){this.stopped=true}};
    for(const fn of target.listeners.get(type)||[])fn(event);
    return event;
  }
  const key=key=>fire(menu,'keydown',{key,target:doc.activeElement});
  return {doc,trigger,menu,items,changes,dropdown,fire,key};
}

test('shared dropdown opens on selection with pointer, navigates enabled items and changes exactly once',()=>{
  const f=fixture();f.items[1].node.disabled=true;
  f.fire(f.trigger,'click');assert.equal(f.doc.activeElement,f.items[0].node);
  f.key('ArrowDown');assert.equal(f.doc.activeElement,f.items[2].node);
  f.key('ArrowDown');assert.equal(f.doc.activeElement,f.items[2].node);
  f.key('Home');assert.equal(f.doc.activeElement,f.items[0].node);
  f.key('End');f.key('Enter');assert.deepEqual(f.changes,['c']);assert.equal(f.dropdown.opened,false);
  assert.equal(f.doc.activeElement,f.trigger);
  f.fire(f.trigger,'click');f.key('Enter');assert.deepEqual(f.changes,['c']);
});

test('Escape consumes the event, Tab preserves navigation, outside click closes without a change',()=>{
  const f=fixture();f.fire(f.trigger,'keydown',{key:'ArrowUp'});
  assert.equal(f.doc.activeElement,f.items[2].node);
  const escape=f.key('Escape');assert.equal(escape.defaultPrevented,true);assert.equal(escape.stopped,true);
  f.fire(f.trigger,'keydown',{key:'Enter'});assert.equal(f.doc.activeElement,f.items[0].node);
  const tab=f.key('Tab');assert.equal(tab.defaultPrevented,false);assert.equal(f.dropdown.opened,false);assert.equal(f.doc.activeElement,f.trigger);
  f.fire(f.trigger,'click');f.fire(f.doc,'pointerdown',{target:{}});assert.equal(f.dropdown.opened,false);assert.deepEqual(f.changes,[]);
});

test('disabled trigger does not open or mutate selection',()=>{
  const f=fixture();f.trigger.disabled=true;f.fire(f.trigger,'click');
  assert.equal(f.dropdown.opened,false);assert.deepEqual(f.changes,[]);
});
