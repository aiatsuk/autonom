// Shared Canvas palette and appearance. Auto follows the OS; overrides stay browser-local.
const LIGHT = `
  color-scheme:light dark;
  --bg:#f5f5f7;--surface:#fff;--raised:#fff;--text:#1d1d1f;--text-2:#6e6e73;--line:rgba(0,0,0,.09);--line-2:rgba(0,0,0,.14);
  --fill:rgba(0,0,0,.045);--fill-2:rgba(0,0,0,.08);--accent:#0071e3;--accent-fill:#0071e3;--on-accent:#fff;--live:#1f9d55;--warn:#b25000;--idle:#8e8e93;
  --bezel:#1d1d1f;--shadow:0 1px 1px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.08);--device-shadow:0 2px 6px rgba(0,0,0,.08),0 24px 60px rgba(0,0,0,.16);
  --canvas-panel-width:min(440px,calc(100vw - 16px));--material:rgba(255,255,255,.96);--r-sm:8px;--video-ratio:.4615;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
--danger:#d70015;--success:#248a3d;--sheet-shadow:0 -12px 40px rgba(0,0,0,.16);--tab-selected-bg:var(--raised);--tab-shadow:0 1px 2px rgba(0,0,0,.08),0 0 0 .5px rgba(0,0,0,.04);--confirm-shadow:var(--shadow);--detail-backdrop:rgba(0,0,0,.22);--detail-shadow:-12px 0 40px rgba(0,0,0,.16);
`;
const DARK = `
  --bg:#0b0b0c;--surface:#161617;--raised:#1f1f21;--text:#f5f5f7;--text-2:#a1a1a6;--line:rgba(255,255,255,.09);--line-2:rgba(255,255,255,.16);
  --fill:rgba(255,255,255,.06);--fill-2:rgba(255,255,255,.11);--accent:#0a84ff;--accent-fill:#0068d6;--live:#32d74b;--warn:#ff9f0a;
  --bezel:#1c1c1e;--shadow:0 1px 1px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.45);--device-shadow:0 0 0 1px rgba(255,255,255,.16),0 24px 70px rgba(0,0,0,.7);
  --material:rgba(36,36,38,.95);
color-scheme:dark;--danger:#ff453a;--success:#30d158;--sheet-shadow:0 -12px 40px rgba(0,0,0,.6);--tab-selected-bg:var(--fill-2);--tab-shadow:0 0 0 .5px rgba(255,255,255,.08);--confirm-shadow:0 0 0 1px rgba(255,255,255,.06),var(--shadow);--detail-backdrop:rgba(0,0,0,.5);--detail-shadow:-12px 0 40px rgba(0,0,0,.6);
`;

export function themeStyles() {
  return `:root{${LIGHT}}
@media (prefers-color-scheme:dark){:root{${DARK}}}
:root[data-theme="light"]{${LIGHT}color-scheme:light;}
:root[data-theme="dark"]{${DARK}}
.canvas-theme-control{display:inline-flex;align-items:center;flex:none}
.canvas-theme{width:84px;height:32px;padding:0 8px;border:1px solid var(--line-2);border-radius:var(--r-sm);background:var(--raised);color:var(--text);font:12px var(--font)}
@media screen and (max-width:760px){.canvas-theme{width:76px}}
`;
}
export function themeControl() {
  return '<label class="canvas-theme-control"><span class="sr">Theme</span><select class="canvas-theme" id="canvas-theme" aria-label="Theme" title="Theme: follow system or choose an appearance"><option value="system">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select></label>';
}
export function themeScript() {
  return `(function(){
"use strict";
const KEY="autonom.canvas.theme",root=document.documentElement;
const normalize=value=>value==="light"||value==="dark"?value:"system";
const media=typeof matchMedia==="function"?matchMedia("(prefers-color-scheme: dark)"):null;
let preference="system";
try{preference=normalize(localStorage.getItem(KEY))}catch{}
function apply(){const effective=preference==="system"?(media&&media.matches?"dark":"light"):preference;root.setAttribute("data-theme",effective);root.setAttribute("data-theme-preference",preference);const control=document.getElementById("canvas-theme");if(control)control.value=preference}
function bind(){const control=document.getElementById("canvas-theme");if(!control)return;control.value=preference;control.addEventListener("change",()=>{preference=normalize(control.value);apply();try{localStorage.setItem(KEY,preference)}catch{}})}
apply();
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",bind,{once:true});else bind();
if(media){const changed=()=>{if(preference==="system")apply()};if(media.addEventListener)media.addEventListener("change",changed);else if(media.addListener)media.addListener(changed)}
window.addEventListener("storage",event=>{if(event.key===KEY||event.key===null){preference=normalize(event.newValue);apply()}});
})();`;
}
export function themeHead() {
  return '<meta name="color-scheme" content="light dark"><script data-canvas-theme>'+themeScript()+'</script>';
}
