// The Actions drawer of the Mobile Canvas device page (contract section 5.5): screenshot,
// screen recording, install from the Canvas's --install-root folders, launch, open link, app
// language and the captures gallery (Capture tab), and the command log with tool health
// (Activity tab). Four pure functions return strings that the page inlines through
// `hooks.pageExtensions`: the header toggle, the drawer markup, its styles and its script.
// The markup holds no device data; the script renders everything with textContent, never
// innerHTML. Actions, Tools and the inspector share one column and are mutually exclusive.

const ACTIVITY_ROW_LIMIT = 500;
const ACTIVITY_POLL_MS = 2000;
const RECORD_POLL_MS = 2000;
// Status re-reads after a failed record.start/stop: 150 x 2 s covers a queued install (180 s)
// plus the start's own wait for a limit-ended recording to be saved.
const RECORD_RECHECKS = 150;
const RECORD_LIMIT_S = 180;

export const ACTIONS_TABS = Object.freeze([
  Object.freeze({ id: "capture", label: "Capture" }),
  Object.freeze({ id: "activity", label: "Activity" }),
]);

// Inline SVG paths on the page's 24 px grid; stroke and size come from the page CSS.
const ACTIONS_ICONS = Object.freeze({
  actions: '<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.8l1.4-2h4.6l1.4 2h1.8A2.5 2.5 0 0 1 20 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.5"/>',
  close: '<path d="m7 7 10 10M17 7 7 17"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5v4.5H15"/>',
});

function platformOf(context) {
  const value = context && (context.platform || (context.options && context.options.platform));
  return value === "ios" ? "ios" : "android";
}

function svg(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ACTIONS_ICONS[name]}</svg>`;
}

/** The header toggle, in the page's toolbar style (aria-pressed follows the drawer). */
export function actionsButton() {
  return `<button type="button" class="icon actions-toggle" id="actions-toggle" aria-pressed="false" aria-controls="actions" ` +
    `title="Device actions" aria-label="Device actions">${svg("actions")}</button>`;
}

function tabList() {
  return ACTIONS_TABS.map(({ id, label }, index) =>
    `<button type="button" role="tab" class="act-tab" id="actions-tab-${id}" aria-controls="actions-panel-${id}" ` +
    `aria-selected="${index === 0}" tabindex="${index === 0 ? 0 : -1}">${label}</button>`).join("");
}

function panel(id, body) {
  const first = id === ACTIONS_TABS[0].id;
  return `<section class="act-panel" role="tabpanel" id="actions-panel-${id}" aria-labelledby="actions-tab-${id}" tabindex="0"${first ? "" : " hidden"}>` +
    `<div class="act-msg" id="actions-msg-${id}" role="status" aria-live="polite" hidden></div>${body}</section>`;
}

function capturePanel(ios) {
  const installPlaceholder = ios ? "/path/to/build/Runner.app" : "/path/to/build/app-debug.apk";
  const downgrade = ios
    ? ""
    : `<label class="act-check"><input id="actions-downgrade" type="checkbox"> Allow downgrade</label>`;
  return panel("capture",
    `<div class="act-group">` +
      `<h3>Capture</h3>` +
      `<div class="act-row">` +
        `<button type="button" class="btn primary" id="actions-shot" data-mutate>Screenshot</button>` +
        `<button type="button" class="btn" id="actions-record" data-mutate aria-pressed="false">Record</button>` +
        `<span class="act-time" id="actions-rec-time" aria-live="off"></span>` +
      `</div>` +
      `<p class="act-note">Recordings stop by themselves after 3 minutes. Captures are saved on this computer.</p>` +
    `</div>` +
    `<div class="act-group" id="actions-install">` +
      `<h3>Install</h3>` +
      `<div class="act-callout" id="actions-install-off" hidden>` +
        `<p class="act-callout-title">Installing is off for this Canvas.</p>` +
        `<p class="act-callout-hint" id="actions-install-hint">Start the Canvas with --install-root &lt;folder&gt;, for example autonom canvas --install-root ~/src/app/build. Builds are installed only from those folders.</p>` +
      `</div>` +
      `<div id="actions-install-on">` +
        `<label class="act-label" for="actions-install-path">Build path</label>` +
        `<div class="act-field"><input id="actions-install-path" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${installPlaceholder}">` +
        `<button type="button" class="btn" id="actions-install-go" data-mutate>Install</button></div>` +
        downgrade +
        `<p class="act-hint" id="actions-roots"></p>` +
        `<ul class="act-list" id="actions-candidates" aria-label="Recent builds"></ul>` +
        `<p class="act-empty" id="actions-candidates-empty" hidden>No builds found in the install folders yet.</p>` +
      `</div>` +
    `</div>` +
    `<div class="act-group">` +
      `<h3>App</h3>` +
      `<label class="act-label" for="actions-app-id">${ios ? "Bundle id" : "Package"}</label>` +
      `<input id="actions-app-id" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${ios ? "com.example.App" : "com.example.app"}">` +
      `<div class="act-row">` +
        `<button type="button" class="btn" id="actions-launch" data-mutate>Launch</button>` +
        `<label class="act-check"><input id="actions-fresh" type="checkbox"> Fresh start</label>` +
      `</div>` +
      `<label class="act-label" for="actions-locale">App language</label>` +
      `<div class="act-field"><input id="actions-locale" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="de-DE">` +
      `<button type="button" class="btn" id="actions-locale-go" data-mutate>Set language</button></div>` +
      `<p class="act-note">${ios ? "The app restarts in that language." : "Per-app language needs Android 13 or later."}</p>` +
      `<label class="act-label" for="actions-url">Open link</label>` +
      `<div class="act-field"><input id="actions-url" type="text" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="myapp://profile/42">` +
      `<button type="button" class="btn" id="actions-url-go" data-mutate>Open</button></div>` +
    `</div>` +
    `<div class="act-group">` +
      `<div class="act-group-head"><h3 id="actions-gallery-title">Gallery</h3>` +
      `<button type="button" class="icon act-icon" id="actions-gallery-refresh" title="Refresh gallery" aria-label="Refresh gallery">${svg("refresh")}</button></div>` +
      `<p class="act-hint act-path" id="actions-gallery-dir"></p>` +
      `<p class="act-notice" id="actions-prune" role="status" hidden></p>` +
      `<ul class="act-gallery" id="actions-gallery" aria-labelledby="actions-gallery-title"></ul>` +
      `<p class="act-empty" id="actions-gallery-empty">No captures yet.</p>` +
    `</div>`);
}

function activityPanel() {
  return panel("activity",
    `<div class="act-group">` +
      `<div class="act-group-head"><h3 id="actions-activity-title">Activity</h3>` +
      `<label class="act-check"><input id="actions-failures" type="checkbox"> Failures only</label>` +
      `<button type="button" class="btn small" id="actions-activity-clear">Clear</button></div>` +
      `<ol class="act-log" id="actions-activity" aria-labelledby="actions-activity-title"></ol>` +
      `<p class="act-empty" id="actions-activity-empty">No commands yet.</p>` +
      `<p class="act-hint" id="actions-activity-dropped" hidden></p>` +
    `</div>` +
    `<div class="act-group">` +
      `<div class="act-group-head"><h3 id="actions-health-title">Tool health</h3>` +
      `<button type="button" class="icon act-icon" id="actions-health-refresh" title="Check again" aria-label="Check tools again">${svg("refresh")}</button></div>` +
      `<ul class="act-list" id="actions-health" aria-labelledby="actions-health-title"></ul>` +
      `<p class="act-hint" id="actions-health-time"></p>` +
    `</div>`);
}

/** The drawer: Capture and Activity tabs. Only static, platform-specific markup. */
export function actionsMarkup(context = {}) {
  const platform = platformOf(context);
  const ios = platform === "ios";
  return `<aside class="act" id="actions" hidden aria-label="Device actions" data-platform="${platform}">` +
    `<div class="act-head">` +
      `<div class="act-tabs" id="actions-tablist" role="tablist" aria-label="Device actions">${tabList()}</div>` +
      `<button type="button" class="icon act-icon" id="actions-close" title="Close device actions" aria-label="Close device actions">${svg("close")}</button>` +
    `</div>` +
    `<p class="act-lock" id="actions-lock" role="status" aria-live="polite" hidden></p>` +
    `<div class="act-body">${capturePanel(ios)}${activityPanel()}</div>` +
  `</aside>`;
}

/**
 * Styles on the page's own tokens, light and dark. Desktop: the drawer takes the inspector's
 * column while open. Phone (760 px and below): a full-width sheet with 44 px targets and no
 * sideways scroll down to 320 px.
 */
export function actionsStyles() {
  return `
/* actions drawer */
:root{--a-danger:#d70015;--a-ok:#248a3d;--a-sheet-shadow:0 -12px 40px rgba(0,0,0,.16)}
body.actions-open,body.actions-open.no-inspector{grid-template-columns:minmax(0,1fr) clamp(400px,32vw,500px);grid-template-areas:"bar bar" "stage side"}
body.actions-open aside.side{display:none}
.act{grid-area:side;display:flex;flex-direction:column;min-width:0;min-height:0;border-left:1px solid var(--line);background:var(--surface)}
.act[hidden]{display:none}
.act-head{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 14px;border-bottom:1px solid var(--line)}
.act-tabs{display:flex;flex:1;min-width:0;gap:2px;padding:2px;border-radius:9px;background:var(--fill)}
.act-tab{flex:1 0 auto;height:28px;padding:0 10px;border:0;border-radius:7px;background:transparent;color:var(--text-2);font-weight:500;white-space:nowrap}
.act-tab[aria-selected=true]{background:var(--raised);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.08),0 0 0 .5px rgba(0,0,0,.04)}
.act-icon{flex:none}
.act-lock{margin:0;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--fill);color:var(--text-2);font-size:12px}
.act-body{flex:1;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain}
.act-panel{padding:0 0 28px;outline:none;min-width:0}
.act-panel:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
.act-group{padding:14px 16px;border-bottom:1px solid var(--line);min-width:0}
.act-group:last-child{border-bottom:0}
.act-group h3{margin:0 0 10px;font-size:12px;font-weight:600;color:var(--text-2);letter-spacing:.01em}
.act-group-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.act-group-head h3{flex:1;margin:0}
.act-label{display:block;margin:12px 0 5px;font-size:12px;color:var(--text-2)}
.act-group>h3+.act-label{margin-top:0}
.act input[type=text]{display:block;width:100%;min-width:0;height:32px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--bg);color:var(--text);font:inherit}
.act-field{display:flex;gap:8px;min-width:0}
.act-field input{flex:1}
.act-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px;min-width:0}
.act-group>h3+.act-row{margin-top:0}
.act-check{display:inline-flex;align-items:center;gap:6px;color:var(--text-2);font-size:13px}
.act-time{color:var(--a-danger);font-variant-numeric:tabular-nums;font-size:13px}
.act-note,.act-hint,.act-empty{margin:8px 0 0;color:var(--text-2);font-size:12px;line-height:1.4;overflow-wrap:anywhere}
.act-path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.act-notice{margin:8px 0 0;padding:8px 10px;border-radius:var(--r-sm);background:var(--fill);color:var(--text);font-size:12px}
.act-msg{margin:12px 16px 0;padding:8px 10px;border-radius:var(--r-sm);background:var(--fill);font-size:13px;overflow-wrap:anywhere}
.act-msg[data-kind=error]{color:var(--a-danger)}
.act-msg p{margin:0}.act-msg p+p{margin-top:4px;color:var(--text-2);font-size:12px}
.act-callout{padding:10px 12px;border-radius:var(--r-sm);background:var(--fill)}
.act-callout p{margin:0;font-size:13px}.act-callout-hint{margin-top:4px!important;color:var(--text-2);font-size:12px!important;overflow-wrap:anywhere}
.act-list{list-style:none;margin:8px 0 0;padding:0}
.act-list li{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:4px 8px;padding:8px 0;border-top:1px solid var(--line);min-width:0}
.act-list li:first-child{border-top:0}
.act-item-title{min-width:0;overflow-wrap:anywhere;font-size:13px}
.act-item-meta{grid-column:1/-1;color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
.act-badge{font-size:12px;font-weight:600;white-space:nowrap}
.act-badge[data-ok=true]{color:var(--a-ok)}.act-badge[data-ok=false]{color:var(--a-danger)}
.act-gallery{list-style:none;margin:8px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(132px,100%),1fr));gap:10px}
.act-gallery li{display:flex;flex-direction:column;gap:6px;min-width:0}
.act-thumb{display:block;width:100%;aspect-ratio:9/16;object-fit:contain;border-radius:var(--r-sm);background:var(--fill);border:1px solid var(--line)}
.act-video{display:grid;place-items:center;width:100%;aspect-ratio:9/16;border-radius:var(--r-sm);background:var(--fill);border:1px solid var(--line);color:var(--text);font-size:13px;text-decoration:none}
.act-gallery .act-item-title{font-size:12px}
.act-log{list-style:none;margin:0;padding:0}
.act-log li{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:2px 8px;padding:7px 0;border-top:1px solid var(--line);font-size:12px;min-width:0}
.act-log li:first-child{border-top:0}
.act-log-time{color:var(--text-2);font-variant-numeric:tabular-nums}
.act-log-name{min-width:0;overflow-wrap:anywhere;font-weight:500}
.act-log-result{white-space:nowrap;font-variant-numeric:tabular-nums}
.act-log li[data-ok=false] .act-log-result{color:var(--a-danger)}
.act-log-detail{grid-column:2/-1;color:var(--text-2);overflow-wrap:anywhere}
@media (hover:hover){.act-tab:not([aria-selected=true]):hover{color:var(--text)}}
@media (prefers-color-scheme:dark){
  :root{--a-danger:#ff453a;--a-ok:#30d158;--a-sheet-shadow:0 -12px 40px rgba(0,0,0,.6)}
  .act-tab[aria-selected=true]{background:var(--fill-2);box-shadow:0 0 0 .5px rgba(255,255,255,.08)}
}
/* phone: a full-width sheet from the page's 760 px one-column width; 44 px targets,
   16 px inputs so iOS does not zoom; nothing wider than the screen down to 320 px */
@media (max-width:760px){
  .actions .icon.actions-toggle{display:grid;width:44px;height:44px}
  .act{position:fixed;left:0;right:0;bottom:0;top:calc(58px + env(safe-area-inset-top));z-index:6;width:100%;max-width:100vw;border:0;border-top:1px solid var(--line-2);border-radius:14px 14px 0 0;
    box-shadow:var(--a-sheet-shadow);padding-bottom:env(safe-area-inset-bottom);animation:act-sheet .24s cubic-bezier(.32,.72,0,1)}
  body.actions-open{overflow:hidden}
  .act-head{padding:8px 8px 8px 12px}
  .act-tab{height:36px}
  .act-icon{width:44px;height:44px}
  .act input[type=text]{height:44px;font-size:16px}
  .act .btn{height:44px}
  .act .btn.small{height:36px}
  .act-group{padding:12px}
}
@media (max-width:360px){.act-field{flex-wrap:wrap}.act-field .btn{flex:1}}
@keyframes act-sheet{from{transform:translateY(24px);opacity:0}}
@media (prefers-reduced-motion:reduce){.act{animation:none}}
`;
}

/**
 * The drawer's code: plain JavaScript appended to the page script. It is written inside this
 * template literal, so it avoids backticks and its escapes are doubled. It reads the page
 * globals url, $, csrf, view, canSendInput, note, BASE, EMBED and DEVICE_ID (the last three
 * may be absent on a page without devices routes) and keeps its names in one function scope.
 */
export function actionsScript() {
  return `(function(){
"use strict";
const ROW_LIMIT=${ACTIVITY_ROW_LIMIT},ACTIVITY_MS=${ACTIVITY_POLL_MS},RECORD_MS=${RECORD_POLL_MS},RECORD_RECHECKS=${RECORD_RECHECKS},RECORD_LIMIT=${RECORD_LIMIT_S};
const drawer=$("actions"),toggle=$("actions-toggle");
if(!drawer||!toggle)return;
const TABS=["capture","activity"];
const tabs=TABS.map(name=>$("actions-tab-"+name)),panels=TABS.map(name=>$("actions-panel-"+name));
const lock=$("actions-lock"),inspectorToggle=$("inspector-toggle"),toolsToggle=$("tools-toggle");
const deviceId=(typeof DEVICE_ID!=="undefined"&&DEVICE_ID)?String(DEVICE_ID):null;
const state={open:false,tab:"capture",lockTimer:null,
  record:{active:false,started:0,tick:null,poll:null,busy:false,unsure:0,retry:null},
  install:{loaded:false},gallery:{seq:0},
  activity:{timer:null,busy:false,after:0,seen:0,rows:new Map(),dropped:0,generation:0},
  health:{loaded:false,busy:false}};

function text(id,value){const node=$(id);if(node&&node.textContent!==value)node.textContent=value}
function el(tag,className,value){const node=document.createElement(tag);if(className)node.className=className;if(value!==undefined&&value!==null)node.textContent=String(value);return node}
function makeButton(label,className,attributes){const node=el("button",className||"btn small",label);node.setAttribute("type","button");if(attributes)for(const key of Object.keys(attributes))node.setAttribute(key,attributes[key]);return node}
function hasCsrf(){return typeof csrf!=="undefined"&&csrf}
function toolsPath(path){return typeof url==="function"?url(path):path}

// Requests keep error_code and hint; /tools/* goes through url() (the device's base), /api/* stays absolute.
function failure(payload,status){const error=new Error(payload&&payload.error||"the request failed"+(status?" ("+status+")":""));error.code=payload&&payload.error_code||null;error.hint=payload&&payload.hint||null;error.status=status||0;return error}
async function request(method,path,body){const init={method,headers:{}};if(method!=="GET"){init.headers["Content-Type"]="application/json";init.headers["X-Autonom-Origin"]="human";if(hasCsrf())init.headers["X-Autonom-CSRF"]=csrf;init.body=JSON.stringify(body||{})}let response;try{response=await fetch(path.indexOf("/api/")===0?path:toolsPath(path),init)}catch(error){throw failure({error:"The Canvas did not answer: "+(error&&error.message||error),error_code:"unreachable"},0)}let payload=null;try{payload=await response.json()}catch{payload=null}if(!response.ok||!payload||payload.ok===false)throw failure(payload||{error:response.statusText||"HTTP "+response.status},response.status);return payload}
async function call(op,payload){const answer=await request("POST","/tools/call",{op,payload:payload||{}});return answer.result||{}}

function message(panel,kind,value,hint){const box=$("actions-msg-"+panel);if(!box)return;box.replaceChildren();if(!value){box.hidden=true;box.removeAttribute("data-kind");return}box.setAttribute("data-kind",kind);box.appendChild(el("p","act-msg-text",value));if(hint)box.appendChild(el("p","act-msg-hint",hint));box.hidden=false}
function errorText(error){return(error&&error.message||String(error))+(error&&error.code?" ("+error.code+")":"")}
function showError(panel,error){message(panel,"error",errorText(error),error&&error.hint||"")}
function showOk(panel,value,hint){message(panel,"ok",value,hint||"")}
function bytes(value){const n=Number(value)||0;if(n<1024)return n+" B";if(n<1048576)return(n/1024).toFixed(0)+" KB";if(n<1073741824)return(n/1048576).toFixed(1)+" MB";return(n/1073741824).toFixed(2)+" GB"}
function clock(ms){const s=Math.max(0,Math.floor(ms/1000));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")}
function timeOf(iso){const d=new Date(iso);return isNaN(d)?"":d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"})}
function baseName(path){const parts=String(path||"").split("/");return parts[parts.length-1]||String(path||"")}

// Device changes follow the page's control rule; the reason shows while they are off.
function allowed(){try{return Boolean(canSendInput())}catch{return true}}
function lockReason(){if(view&&view.paused)return"Input is paused, so device actions are off. Resume input to use them.";if(view&&view.owner==="agent")return"An agent has control, so device actions are off. Take control to use them.";return"Another client has control, so device actions are off."}
function syncLock(){const ok=allowed();if(lock){lock.hidden=ok;if(!ok)text("actions-lock",lockReason())}for(const control of drawer.querySelectorAll("[data-mutate]")){const off=!ok||control.getAttribute("data-busy")==="1";if(control.disabled!==off)control.disabled=off}}
async function mutate(control,op,payload){if(!allowed()){syncLock();return null}message("capture");if(control){control.setAttribute("data-busy","1");control.setAttribute("aria-busy","true")}syncLock();try{return await call(op,payload)}catch(error){showError("capture",error);return null}finally{if(control){control.removeAttribute("data-busy");control.removeAttribute("aria-busy")}syncLock()}}

// Drawer and tabs (arrows, Home and End move between tabs; Escape closes).
function selectTab(name,focus){const index=TABS.indexOf(name);if(index<0)return;state.tab=name;tabs.forEach((tab,i)=>{const on=i===index;tab.setAttribute("aria-selected",String(on));tab.setAttribute("tabindex",on?"0":"-1");panels[i].hidden=!on});if(focus)tabs[index].focus();onTabShown(name);syncPolling()}
function onTabShown(name){if(!state.open)return;if(name==="capture"){loadRecordStatus();loadCandidates();loadGallery()}else{pollActivity();if(!state.health.loaded)loadHealth(false)}}
function closeTools(){if(!document.body.classList.contains("tools-open"))return;const close=$("tools-close");if(close&&typeof close.click==="function")close.click()}
function setOpen(open,returnFocus){open=Boolean(open);if(state.open===open)return;if(open)closeTools();state.open=open;drawer.hidden=!open;document.body.classList.toggle("actions-open",open);toggle.setAttribute("aria-pressed",String(open));toggle.title=open?"Hide device actions":"Device actions";if(inspectorToggle)inspectorToggle.setAttribute("aria-pressed",String(!open&&!document.body.classList.contains("no-inspector")&&!document.body.classList.contains("tools-open")));if(open){syncLock();state.lockTimer=setInterval(syncLock,500);state.install.loaded=false;const tab=tabs[TABS.indexOf(state.tab)];if(tab)tab.focus();onTabShown(state.tab)}else{clearInterval(state.lockTimer);state.lockTimer=null;if(returnFocus)toggle.focus()}syncPolling()}
function syncPolling(){const activity=state.open&&state.tab==="activity";if(activity&&!state.activity.timer)state.activity.timer=setInterval(pollActivity,ACTIVITY_MS);if(!activity&&state.activity.timer){clearInterval(state.activity.timer);state.activity.timer=null}syncRecordTimers()}
toggle.addEventListener("click",()=>setOpen(!state.open,false));
$("actions-close").addEventListener("click",()=>setOpen(false,true));
// The Tools drawer and the inspector take the same column: opening either closes this one.
if(toolsToggle)toolsToggle.addEventListener("click",()=>{if(state.open&&document.body.classList.contains("tools-open"))setOpen(false,false)});
if(inspectorToggle)inspectorToggle.addEventListener("click",()=>{if(!state.open)return;setOpen(false,false);document.body.classList.remove("no-inspector");inspectorToggle.setAttribute("aria-pressed","true");inspectorToggle.title="Hide inspector"});
tabs.forEach((tab,i)=>tab.addEventListener("click",()=>selectTab(TABS[i],false)));
$("actions-tablist").addEventListener("keydown",event=>{const index=TABS.indexOf(state.tab),moves={ArrowRight:index+1,ArrowLeft:index-1,Home:0,End:TABS.length-1},next=moves[event.key];if(next===undefined)return;event.preventDefault();selectTab(TABS[(next+TABS.length)%TABS.length],true)});
drawer.addEventListener("keydown",event=>{if(event.key!=="Escape")return;event.preventDefault();setOpen(false,true)});
// The workspace shell opens and closes this drawer in a tile: {type: "autonom:panel", name: "actions", open}.
if(typeof window!=="undefined"&&typeof window.addEventListener==="function")window.addEventListener("message",event=>{const data=event&&event.data;if(!data||data.type!=="autonom:panel"||data.name!=="actions")return;if(typeof location!=="undefined"&&event.origin!==location.origin)return;if(typeof window.parent!=="undefined"&&window.parent!==window&&event.source!==window.parent)return;setOpen(Boolean(data.open),false)});

// Screenshot.
function savedText(capture){return"Saved "+capture.name}
function pruneText(count){return"Deleted "+count+" oldest capture"+(count===1?"":"s")+" to stay under 200 files / 2 GB"}
function showPrune(count){const box=$("actions-prune");if(!box)return;if(!count){box.hidden=true;return}box.textContent=pruneText(count);box.hidden=false}
$("actions-shot").addEventListener("click",async()=>{const result=await mutate($("actions-shot"),"capture.screenshot",{});if(!result||!result.capture)return;showOk("capture",savedText(result.capture));if(result.pruned&&result.pruned.length)showPrune(result.pruned.length);loadGallery()});

// Recording: one per device; the elapsed time counts locally, the status is re-read every 2 s.
function syncRecordButton(){const button=$("actions-record");button.textContent=state.record.active?"Stop recording":"Record";button.setAttribute("aria-pressed",String(state.record.active));if(!state.record.active)text("actions-rec-time","")}
function renderElapsed(){if(!state.record.active)return;text("actions-rec-time","Recording "+clock(Date.now()-state.record.started)+" / "+clock(RECORD_LIMIT*1000))}
function syncRecordTimers(){const on=state.open&&state.record.active;if(on&&!state.record.tick){state.record.tick=setInterval(renderElapsed,1000);state.record.poll=setInterval(loadRecordStatus,RECORD_MS)}if(!on&&state.record.tick){clearInterval(state.record.tick);clearInterval(state.record.poll);state.record.tick=null;state.record.poll=null}}
function setRecording(active,startedAt,elapsed){state.record.active=active;if(active){const parsed=Date.parse(startedAt||"");state.record.started=typeof elapsed==="number"?Date.now()-elapsed:(isNaN(parsed)?Date.now():parsed)}syncRecordButton();renderElapsed();syncRecordTimers()}
async function loadRecordStatus(){if(state.record.busy)return;state.record.busy=true;try{const status=await call("record.status",{});state.record.unsure=0;const was=state.record.active;setRecording(Boolean(status.recording),status.started_at,status.elapsed_ms);if(was&&!status.recording&&status.last){if(status.last.capture)showOk("capture","The recording reached "+clock(RECORD_LIMIT*1000)+" and was saved as "+status.last.capture.name);else if(status.last.error)message("capture","error","The recording ended and could not be saved: "+status.last.error,"");loadGallery()}}catch(error){showError("capture",error)}finally{state.record.busy=false;retryRecordStatus()}}
function retryRecordStatus(){if(state.record.unsure<=0||state.record.retry)return;state.record.unsure-=1;state.record.retry=setTimeout(()=>{state.record.retry=null;loadRecordStatus()},RECORD_MS)}
// After a failed start or stop the tools process may still run the call (it was only
// queued): re-read the status until one read succeeds, at most RECORD_RECHECKS times.
function confirmRecordState(){state.record.unsure=RECORD_RECHECKS;loadRecordStatus()}
$("actions-record").addEventListener("click",async()=>{const button=$("actions-record");if(!state.record.active){const result=await mutate(button,"record.start",{});if(result&&result.recording){setRecording(true,result.recording.started_at);showOk("capture","Recording. Press Stop recording to save it.")}else if(!result){confirmRecordState()}return}const result=await mutate(button,"record.stop",{});if(!result){confirmRecordState();return}setRecording(false);if(result.capture)showOk("capture",savedText(result.capture)+" ("+clock(result.duration_ms||0)+")",result.ended_early?"The recording reached its 3 minute limit before you stopped it.":"");if(result.pruned&&result.pruned.length)showPrune(result.pruned.length);loadGallery()});

// Install: a path inside the Canvas's --install-root folders, or one of the builds listed there.
async function loadCandidates(){if(state.install.loaded)return;state.install.loaded=true;let result;try{result=await call("apps.candidates",{})}catch(error){state.install.loaded=false;showError("capture",error);return}const off=!result.configured;$("actions-install-off").hidden=!off;$("actions-install-on").hidden=off;if(off){if(result.hint)text("actions-install-hint",result.hint+". Builds are installed only from those folders.");return}text("actions-roots","Install folders: "+(result.roots||[]).join(", ")+(result.truncated?" (not every folder was searched)":""));const list=$("actions-candidates");list.replaceChildren();for(const item of result.candidates||[]){const row=el("li");const title=el("span","act-item-title",baseName(item.path));const pick=makeButton("Use","btn small");pick.addEventListener("click",()=>{$("actions-install-path").value=item.path;$("actions-install-path").focus()});row.appendChild(title);row.appendChild(pick);row.appendChild(el("span","act-item-meta",item.path+" · "+bytes(item.size)+" · "+timeOf(item.modified_at)));list.appendChild(row)}$("actions-candidates-empty").hidden=Boolean((result.candidates||[]).length)}
$("actions-install-go").addEventListener("click",async()=>{const path=$("actions-install-path").value.trim();if(!path){message("capture","error","Enter the path of a build to install.","");return}const downgrade=$("actions-downgrade");const payload={path};if(downgrade&&downgrade.checked)payload.allow_downgrade=true;const result=await mutate($("actions-install-go"),"app.install",payload);if(!result)return;if(result.app_id)$("actions-app-id").value=result.app_id;showOk("capture","Installed "+baseName(result.installed)+" in "+clock(result.duration_ms||0),result.app_id?"App id: "+result.app_id:"")});

// Launch, open link, app language.
function appId(){const value=$("actions-app-id").value.trim();if(!value)message("capture","error","Enter the app's package or bundle id first.","");return value}
$("actions-launch").addEventListener("click",async()=>{const id=appId();if(!id)return;const fresh=$("actions-fresh").checked;const result=await mutate($("actions-launch"),"app.launch",fresh?{app_id:id,fresh:true}:{app_id:id});if(result)showOk("capture","Launched "+id+(result.mode==="fresh"?" (fresh start)":""))});
$("actions-url-go").addEventListener("click",async()=>{const link=$("actions-url").value.trim();if(!link){message("capture","error","Enter a link with a scheme, such as myapp://profile/42.","");return}const result=await mutate($("actions-url-go"),"app.open_url",{url:link});if(result)showOk("capture","Opened the link"+(result.handled_by&&result.handled_by!=="unknown"?" in "+result.handled_by:""))});
$("actions-locale-go").addEventListener("click",async()=>{const id=appId();if(!id)return;const locale=$("actions-locale").value.trim();if(!locale){message("capture","error","Enter a language tag such as de-DE.","");return}const result=await mutate($("actions-locale-go"),"app.locale",{app_id:id,locale});if(result)showOk("capture",id+" now uses "+locale+(result.restarted?"; the app was restarted":""))});

// Gallery: the device's captures, newest first; files are served by /tools/captures/<name>.
async function loadGallery(){const seq=++state.gallery.seq;let result;try{result=await call("captures.list",{limit:200})}catch(error){if(seq===state.gallery.seq)showError("capture",error);return}if(seq!==state.gallery.seq)return;text("actions-gallery-dir",result.dir?"Folder: "+result.dir+" · "+(result.count||0)+" captures, "+bytes(result.total_bytes):"");if(result.last_pruned&&result.last_pruned.count)showPrune(result.last_pruned.count);const list=$("actions-gallery");list.replaceChildren();for(const capture of result.captures||[]){const row=el("li");const href=toolsPath("/tools/captures/"+encodeURIComponent(capture.name));if(capture.kind==="screenshot"){const link=el("a");link.setAttribute("href",href);link.setAttribute("target","_blank");link.setAttribute("rel","noopener");const image=el("img","act-thumb");image.setAttribute("src",href);image.setAttribute("alt",capture.name);image.setAttribute("loading","lazy");link.appendChild(image);row.appendChild(link)}else{const link=el("a","act-video","Open video ("+clock(capture.duration_ms||0)+")");link.setAttribute("href",href);link.setAttribute("target","_blank");link.setAttribute("rel","noopener");row.appendChild(link)}row.appendChild(el("span","act-item-title",capture.name));row.appendChild(el("span","act-item-meta",bytes(capture.size)+(capture.width?" · "+capture.width+"×"+capture.height:"")));const remove=makeButton("Delete","btn small",{"aria-label":"Delete "+capture.name});remove.addEventListener("click",async()=>{remove.disabled=true;try{await call("captures.delete",{name:capture.name});loadGallery()}catch(error){remove.disabled=false;showError("capture",error)}});row.appendChild(remove);list.appendChild(row)}$("actions-gallery-empty").hidden=Boolean((result.captures||[]).length)}
$("actions-gallery-refresh").addEventListener("click",()=>loadGallery());

// Activity: this device's commands from the Canvas command log, every 2 s while visible.
function activityQuery(){let query="/api/activity?limit=200&after="+state.activity.after+"&seen="+state.activity.seen;if(deviceId)query+="&device="+encodeURIComponent(deviceId);if($("actions-failures").checked)query+="&failures=1";return query}
function renderActivity(){const list=$("actions-activity");const rows=Array.from(state.activity.rows.values()).sort((a,b)=>b.seq-a.seq).slice(0,ROW_LIMIT);list.replaceChildren();for(const entry of rows){const row=el("li");row.setAttribute("data-ok",String(entry.ok));row.appendChild(el("span","act-log-time",timeOf(entry.at)));row.appendChild(el("span","act-log-name",entry.name));const result=entry.ok===null?"Running":entry.ok?"OK"+(entry.duration_ms!==null?" · "+entry.duration_ms+" ms":""):(entry.error_code||"failed")+(entry.duration_ms!==null?" · "+entry.duration_ms+" ms":"");row.appendChild(el("span","act-log-result",result));const detail=[entry.summary,entry.ok===false?entry.error:null,entry.exit_code!==null&&entry.exit_code!==undefined?"exit "+entry.exit_code:null,entry.origin?"by "+entry.origin:null].filter(Boolean).join(" · ");if(detail)row.appendChild(el("span","act-log-detail",detail));list.appendChild(row)}$("actions-activity-empty").hidden=rows.length>0;const dropped=$("actions-activity-dropped");dropped.hidden=!state.activity.dropped;if(state.activity.dropped)dropped.textContent=state.activity.dropped+" older commands left the log before they were shown."}
async function pollActivity(){if(state.activity.busy||!state.open)return;state.activity.busy=true;const generation=state.activity.generation;try{const page=await request("GET",activityQuery());if(generation!==state.activity.generation)return;for(const entry of page.entries||[])state.activity.rows.set(entry.seq,entry);while(state.activity.rows.size>ROW_LIMIT){const oldest=Math.min.apply(null,Array.from(state.activity.rows.keys()));state.activity.rows.delete(oldest)}state.activity.after=typeof page.next==="number"?page.next:state.activity.after;if(typeof page.seen==="number")state.activity.seen=Math.max(state.activity.seen,page.seen);state.activity.dropped+=page.dropped||0;renderActivity()}catch(error){if(generation===state.activity.generation)showError("activity",error)}finally{state.activity.busy=false}}
function resetActivity(){state.activity.generation+=1;state.activity.after=0;state.activity.seen=0;state.activity.rows=new Map();state.activity.dropped=0;state.activity.busy=false;renderActivity()}
$("actions-failures").addEventListener("change",()=>{resetActivity();pollActivity()});
$("actions-activity-clear").addEventListener("click",async()=>{try{await request("POST","/api/activity/clear",{});resetActivity();pollActivity()}catch(error){showError("activity",error)}});

// Tool health.
async function loadHealth(refresh){if(state.health.busy)return;state.health.busy=true;try{const result=await request("GET","/api/health"+(refresh?"?refresh=1":""));state.health.loaded=true;const list=$("actions-health");list.replaceChildren();for(const tool of result.tools||[]){const row=el("li");row.appendChild(el("span","act-item-title",tool.name));const badge=el("span","act-badge",tool.ok?"OK":tool.found?"Problem":"Missing");badge.setAttribute("data-ok",String(Boolean(tool.ok)));row.appendChild(badge);const detail=[tool.version||tool.error,tool.ok?null:tool.hint,"For "+tool.needed_for].filter(Boolean).join(" · ");row.appendChild(el("span","act-item-meta",detail));list.appendChild(row)}text("actions-health-time",result.checked_at?"Checked at "+timeOf(result.checked_at)+(result.error?" · "+result.error:""):(result.error||""))}catch(error){showError("activity",error)}finally{state.health.busy=false}}
$("actions-health-refresh").addEventListener("click",()=>loadHealth(true));
syncRecordButton();
})();`;
}
