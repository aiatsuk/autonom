import { canvasIcon as svg, canvasTabList, canvasTabPanel, canvasDomScript } from "./canvas-ui.mjs";
// The Actions drawer contains capture controls and gallery (Capture tab), plus the
// command log and the Setup checklist of device tools (Activity tab). App management belongs
// outside Capture.
// Four functions provide markup, styles and behavior through hooks.pageExtensions.
// The markup holds no device data; the script renders everything with textContent, never
// innerHTML. Actions, Tools and the inspector share one column and are mutually exclusive.

const ACTIVITY_ROW_LIMIT = 500;
const ACTIVITY_POLL_MS = 2000;
const RECORD_POLL_MS = 2000;
// Status re-reads after a failed record.start/stop: 150 x 2 s covers a queued install (180 s)
// plus the start's own wait for a limit-ended recording to be saved.
const RECORD_RECHECKS = 150;
const RECORD_LIMIT_S = 180;
// The Setup checklist re-checks this long after its last answer while it is visible and a
// needed tool is not OK; the Canvas grants one uncached check per 5 s (canvas-activity.mjs).
export const SETUP_POLL_MS = 5000;
export const SETUP_GROUPS = Object.freeze([
  Object.freeze({ id: "android", label: "Android" }),
  Object.freeze({ id: "ios", label: "iOS" }),
  Object.freeze({ id: "optional", label: "Optional" }),
]);

export const ACTIONS_TABS = Object.freeze([
  Object.freeze({ id: "capture", label: "Capture" }),
  Object.freeze({ id: "activity", label: "Activity" }),
]);

// Inline SVG paths on the page's 24 px grid; stroke and size come from the page CSS.

function platformOf(context) {
  const value = context && (context.platform || (context.options && context.options.platform));
  return value === "ios" ? "ios" : "android";
}



/**
 * The Setup checklist shared by the Activity tab and the empty workspace: plain JavaScript for
 * an inline script (no backticks, no backslashes, no closing tags) that needs `el` and
 * `makeButton` from canvasDomScript(). It defines:
 * - renderSetupChecklist(root, tools): the /api/health entries grouped Android, iOS, Optional,
 *   each OK, Problem or Missing, with the entry's `fix` command and a Copy button when not OK.
 *   The command is text only: the page copies it and never runs it.
 * - setupMissing(tools, platforms): the needed entries that are not OK. Needed: the Android or
 *   iOS entries of the given platforms, or with none attached every Android and iOS entry
 *   this computer can run. Optional entries never count.
 * - createSetupChecker({fetch(refresh), render(result), visible(), platforms(), error?}): load()
 *   and sync(); while visible() and setupMissing() is not empty it re-checks with a refresh
 *   SETUP_POLL_MS after the last answer, and stops when nothing needed is missing or hidden.
 */
export function setupChecklistScript() {
  const groups = JSON.stringify(SETUP_GROUPS.map((group) => [group.id, group.label]));
  return `const SETUP_POLL_MS=${SETUP_POLL_MS},SETUP_GROUPS=${groups};
const SETUP_GROUP_OF={adb:"android",emulator:"android",scrcpy_server:"android",xcrun_simctl:"ios",idb_companion:"ios",ffmpeg:"optional",mitmdump:"optional"};
const SETUP_LABELS={scrcpy_server:"scrcpy-server",xcrun_simctl:"xcrun simctl"};
function setupGroup(tool){const group=tool&&tool.group;return group==="android"||group==="ios"||group==="optional"?group:SETUP_GROUP_OF[tool&&tool.name]||"optional"}
function setupNeeded(tool,platforms){if(!tool||typeof tool!=="object")return false;const group=setupGroup(tool);if(group==="optional")return false;if(platforms&&platforms.length)return platforms.indexOf(group)>=0;return tool.error!=="macOS only"}
function setupMissing(tools,platforms){return(Array.isArray(tools)?tools:[]).filter(tool=>setupNeeded(tool,platforms)&&!tool.ok)}
function setupState(tool){return tool.ok?"OK":tool.found?"Problem":"Missing"}
async function setupCopy(value){if(typeof navigator==="undefined"||!navigator||!navigator.clipboard||typeof navigator.clipboard.writeText!=="function")return false;try{await navigator.clipboard.writeText(value);return true}catch{return false}}
function setupItem(tool){const state=setupState(tool),label=SETUP_LABELS[tool.name]||String(tool.name||"tool"),row=el("li","setup-item");row.setAttribute("data-state",state.toLowerCase());row.setAttribute("data-tool",String(tool.name||""));row.appendChild(el("span","setup-name",label));const badge=el("span","setup-badge",state);badge.setAttribute("data-ok",String(Boolean(tool.ok)));row.appendChild(badge);const fix=!tool.ok&&typeof tool.fix==="string"?tool.fix.trim():"";const hint=!tool.ok&&tool.hint&&(!fix||String(tool.hint).indexOf(fix)<0)?String(tool.hint):null;const detail=[tool.version||tool.error,hint,tool.needed_for?"For "+tool.needed_for:null].filter(Boolean).join(" · ");row.title=detail;if(!tool.ok&&detail)row.appendChild(el("span","setup-meta",detail));if(fix){const box=el("div","setup-fix"),command=el("code","setup-command",fix),copy=makeButton("Copy","btn small setup-copy",{"aria-label":"Copy the command that fixes "+label,title:"Copy this command to run it yourself"}),status=el("span","setup-copied");status.setAttribute("role","status");copy.addEventListener("click",async()=>{status.textContent=await setupCopy(fix)?"Copied":"Select the command to copy it"});box.appendChild(command);box.appendChild(copy);box.appendChild(status);row.appendChild(box)}return row}
function renderSetupChecklist(root,tools){if(!root)return;const list=Array.isArray(tools)?tools.filter(tool=>tool&&typeof tool==="object"):[];root.textContent="";for(const pair of SETUP_GROUPS){const items=list.filter(tool=>setupGroup(tool)===pair[0]);if(!items.length)continue;const section=el("section","setup-group");section.setAttribute("data-group",pair[0]);section.appendChild(el("h4","setup-group-title",pair[1]));const listNode=el("ul","setup-list");listNode.setAttribute("aria-label",pair[1]);for(const tool of items)listNode.appendChild(setupItem(tool));section.appendChild(listNode);root.appendChild(section)}}
function createSetupChecker(options){const setup={timer:null,busy:false,last:null};function wanted(){return Boolean(setup.last&&options.visible()&&setupMissing(setup.last.tools,options.platforms()).length)}function sync(){const want=wanted();if(want&&!setup.timer&&!setup.busy)setup.timer=setTimeout(()=>{setup.timer=null;if(wanted())load(true);else sync()},SETUP_POLL_MS);if(!want&&setup.timer){clearTimeout(setup.timer);setup.timer=null}}async function load(refresh){if(setup.busy)return;setup.busy=true;if(setup.timer){clearTimeout(setup.timer);setup.timer=null}let result=null,failed=false;try{result=await options.fetch(Boolean(refresh))}catch(error){failed=true;if(options.error)options.error(error)}setup.busy=false;try{if(!failed){setup.last=result||null;options.render(setup.last)}}finally{sync()}}return{load,sync,wanted,get last(){return setup.last},get polling(){return Boolean(setup.timer)}}}`;
}

/** Styles of the Setup checklist on the page tokens (light and dark come from the theme). */
export function setupChecklistStyles() {
  return `.setup-group+.setup-group{margin-top:10px}
.setup-group-title{margin:8px 0 2px;color:var(--text-2);font-size:12px;font-weight:600}
.setup-list{list-style:none;margin:0;padding:0}
.setup-item{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:4px 8px;padding:8px 0;border-top:1px solid var(--line);min-width:0}
.setup-list .setup-item:first-child{border-top:0}
.setup-name{min-width:0;overflow-wrap:anywhere;font-size:13px}
.setup-badge{font-size:12px;font-weight:600;white-space:nowrap}
.setup-badge[data-ok=true]{color:var(--success)}.setup-badge[data-ok=false]{color:var(--danger)}
.setup-meta{grid-column:1/-1;color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
.setup-fix{grid-column:1/-1;display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:wrap}
.setup-command{flex:1 1 180px;min-width:0;padding:4px 8px;border-radius:var(--r-sm);background:var(--fill);font:11.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;user-select:all;-webkit-user-select:all}
.setup-copied{color:var(--text-2);font-size:12px}
.setup-copied:empty{display:none}`;
}

/** Quick capture controls share the drawer's operations and state. */
export function actionsDock() {
  return `<button type="button" id="dock-shot" data-mutate aria-label="Take screenshot" title="Take screenshot">${svg("actions")}</button>` +
    `<button type="button" id="dock-record" data-mutate aria-pressed="false" aria-label="Start recording" title="Start recording" disabled>` +
    `${svg("record")}</button>`;
}

export function actionsButton() {
  return `<button type="button" class="icon actions-toggle" id="actions-toggle" aria-pressed="false" aria-controls="actions" ` +
    `title="Device actions" aria-label="Device actions">${svg("actions")}</button>`;
}

function tabList() { return canvasTabList(ACTIONS_TABS, "actions", "act-tab"); }
function panel(id, body) { return canvasTabPanel(id, body, "actions", "act", id === ACTIONS_TABS[0].id); }

function capturePanel() {
  return panel("capture",
    `<div class="act-group act-capture-summary"><h3>Capture</h3>` +
      `<div class="act-row">` +
        `<button type="button" class="btn primary" id="actions-shot" title="Save a screenshot on this computer" data-mutate>Screenshot</button>` +
        `<button type="button" class="btn" id="actions-record" title="Record video on this computer, up to 3 minutes" data-mutate aria-pressed="false">Record</button>` +
        `<span class="act-time" id="actions-rec-time" aria-live="off"></span>` +
      `</div>` +
    `</div>` +
    `<div class="act-group">` +
      `<div class="act-group-head"><h3 id="actions-gallery-title">Gallery</h3>` +
      `<button type="button" class="btn" id="actions-open-folder">Open in Finder</button>` +
      `<button type="button" class="icon act-icon" id="actions-gallery-refresh" title="Refresh gallery" aria-label="Refresh gallery">${svg("refresh")}</button></div>` +
      `<p class="act-hint" id="actions-gallery-dir" hidden></p>` +
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
      `<div class="act-group-head"><h3 id="actions-health-title">Setup</h3>` +
      `<button type="button" class="icon act-icon" id="actions-health-refresh" title="Check again" aria-label="Check tools again">${svg("refresh")}</button></div>` +
      `<div class="setup" id="actions-health" role="group" aria-labelledby="actions-health-title"></div>` +
      `<p class="act-hint" id="actions-health-time" hidden></p>` +
    `</div>`);
}

/** The drawer: Capture and Activity tabs. Only static, platform-specific markup. */
export function actionsMarkup(context = {}) {
  const platform = platformOf(context);
  return `<aside class="act" id="actions" hidden aria-label="Device actions" data-platform="${platform}">` +
    `<div class="act-head">` +
      `<div class="act-tabs" id="actions-tablist" role="tablist" aria-label="Device actions">${tabList()}</div>` +
      `<button type="button" class="icon act-icon" id="actions-close" title="Close device actions" aria-label="Close device actions">${svg("close")}</button>` +
    `</div>` +
    `<p class="act-lock" id="actions-lock" role="status" aria-live="polite" hidden></p>` +
    `<div class="act-body">${capturePanel()}${activityPanel()}</div>` +
  `</aside>`;
}

/**
 * Styles on the page's own tokens, light and dark. Desktop: the drawer takes the inspector's
 * column while open. Phone (760 px and below): a full-width sheet with 44 px targets and no
 * sideways scroll down to 320 px.
 */
export function actionsStyles() {
  return `

#dock-record .record-stop{display:none}
#dock-record[aria-pressed="true"]{color:var(--danger);background:var(--fill-2)}
#dock-record[aria-pressed="true"] .record-start{display:none}
#dock-record[aria-pressed="true"] .record-stop{display:block;fill:currentColor}

/* actions drawer */
:root{--a-danger:var(--danger);--a-ok:var(--success);--a-sheet-shadow:var(--sheet-shadow)}
body.actions-open,body.actions-open.no-inspector{grid-template-columns:minmax(0,1fr) clamp(400px,32vw,500px);grid-template-areas:"bar bar" "stage side"}
body.actions-open aside.side{display:none}
.act[hidden]{display:none}
.act-lock{margin:0;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--fill);color:var(--text-2);font-size:12px}
.act-capture-summary{display:flex;align-items:center;justify-content:space-between;gap:12px}
.act-capture-summary h3{margin:0}
.act-capture-summary .act-row{margin:0;gap:6px;flex-wrap:nowrap;justify-content:flex-end}
.act-capture-summary .act-row>.btn{flex:none}
.act-time:empty{display:none}
.act-link-field{margin-top:8px}
.act-capture-summary .act-note{grid-column:1/-1;margin:0}
.act-group-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.act-group-head h3{flex:1;margin:0}
.act-group>h3+.act-label{margin-top:0}
.act input[type=text]{display:block;width:100%}
.act-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px;min-width:0}
.act-group>h3+.act-row{margin-top:0}
.act-check{display:inline-flex;align-items:center;gap:6px;color:var(--text-2);font-size:13px}
.act-time{color:var(--a-danger);font-variant-numeric:tabular-nums;font-size:13px}
.act-note,.act-hint,.act-empty{margin:8px 0 0;color:var(--text-2);font-size:12px;line-height:1.4;overflow-wrap:anywhere}
.act-path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.act-notice{margin:8px 0 0;padding:8px 10px;border-radius:var(--r-sm);background:var(--fill);color:var(--text);font-size:12px}
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
.act-thumb{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;border-radius:var(--r-sm);background:var(--fill);border:1px solid var(--line)}
.act-video{display:grid;place-items:center;width:100%;aspect-ratio:4/3;border-radius:var(--r-sm);background:var(--fill);border:1px solid var(--line);color:var(--text);font-size:13px;text-decoration:none}
.act-gallery .act-item-title{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act-log{list-style:none;margin:0;padding:0}
.act-log li{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:2px 8px;padding:7px 0;border-top:1px solid var(--line);font-size:12px;min-width:0}
.act-log li:first-child{border-top:0}
.act-log-time{color:var(--text-2);font-variant-numeric:tabular-nums}
.act-log-name{min-width:0;overflow-wrap:anywhere;font-weight:500}
.act-log-result{white-space:nowrap;font-variant-numeric:tabular-nums}
.act-log li[data-ok=false] .act-log-result{color:var(--a-danger)}
.act-log-detail{grid-column:2/-1;color:var(--text-2);overflow-wrap:anywhere}
${setupChecklistStyles()}
/* Narrow windows retain desktop density in a bounded overlay. */
@media (max-width:760px){
  .actions .icon.actions-toggle{display:grid}
  body.actions-open{overflow:hidden}
}

`;
}

/**
 * The drawer's code: plain JavaScript appended to the page script. It is written inside this
 * template literal, so it avoids backticks and its escapes are doubled. It reads the page
 * globals url, $, csrf, view, canSendInput, note, BASE, EMBED and DEVICE_ID (the last three
 * may be absent on a page without devices routes), and shortcutHint when the device page has
 * shortcuts, and keeps its names in one function scope. The device page's Cmd+S and Cmd+R reach
 * it as an autonom:capture event ({action: "screenshot" | "record"}), which it marks handled.
 */
export function actionsScript() {
  return `(function(){
"use strict";
${setupChecklistScript()}
const ROW_LIMIT=${ACTIVITY_ROW_LIMIT},ACTIVITY_MS=${ACTIVITY_POLL_MS},RECORD_MS=${RECORD_POLL_MS},RECORD_RECHECKS=${RECORD_RECHECKS},RECORD_LIMIT=${RECORD_LIMIT_S};
const drawer=$("actions"),toggle=$("actions-toggle");
if(!drawer||!toggle)return;
const TABS=["capture","activity"];
const tabs=TABS.map(name=>$("actions-tab-"+name)),panels=TABS.map(name=>$("actions-panel-"+name));
const dockShot=$("dock-shot"),dockRecord=$("dock-record");
const lock=$("actions-lock"),inspectorToggle=$("inspector-toggle"),toolsToggle=$("tools-toggle");
const deviceId=(typeof DEVICE_ID!=="undefined"&&DEVICE_ID)?String(DEVICE_ID):null;
const state={open:false,tab:"capture",lockTimer:null,
  record:{active:false,known:false,revision:0,started:0,tick:null,poll:null,busy:false,unsure:0,retry:null},
  gallery:{seq:0},
  activity:{timer:null,busy:false,after:0,seen:0,rows:new Map(),dropped:0,generation:0},
  health:{loaded:false,busy:false}};

function text(id,value){const node=$(id);if(node&&node.textContent!==value)node.textContent=value}
${canvasDomScript()}
function hasCsrf(){return typeof csrf!=="undefined"&&csrf}
function toolsPath(path){return typeof url==="function"?url(path):path}

// Requests keep error_code and hint; /tools/* goes through url() (the device's base), /api/* stays absolute.
function failure(payload,status){const error=new Error(payload&&payload.error||"the request failed"+(status?" ("+status+")":""));error.code=payload&&payload.error_code||null;error.hint=payload&&payload.hint||null;error.status=status||0;return error}
async function request(method,path,body){const init={method,headers:{}};if(method!=="GET"){init.headers["Content-Type"]="application/json";init.headers["X-Autonom-Origin"]="human";if(hasCsrf())init.headers["X-Autonom-CSRF"]=csrf;init.body=JSON.stringify(body||{})}let response;try{response=await fetch(path.indexOf("/api/")===0?path:toolsPath(path),init)}catch(error){throw failure({error:"The Canvas did not answer: "+(error&&error.message||error),error_code:"unreachable"},0)}let payload=null;try{payload=await response.json()}catch{payload=null}if(!response.ok||!payload||payload.ok===false)throw failure(payload||{error:response.statusText||"HTTP "+response.status},response.status);return payload}
async function call(op,payload){const answer=await request("POST","/tools/call",{op,payload:payload||{}});return answer.result||{}}

function message(panel,kind,value,hint){const box=$("actions-msg-"+panel);if(!box)return;box.replaceChildren();if(!value){box.hidden=true;box.removeAttribute("data-kind");return}box.setAttribute("data-kind",kind);box.appendChild(el("p","act-msg-text",value));if(hint)box.appendChild(el("p","act-msg-hint",hint));box.hidden=false}
function errorText(error){return(error&&error.message||String(error))+(error&&error.code?" ("+error.code+")":"")}
function showError(panel,error){message(panel,"error",errorText(error),error&&error.hint||"");if(panel==="capture"&&!state.open&&typeof note==="function")note(errorText(error))}
function showOk(panel,value,hint){message(panel,"ok",value,hint||"");if(panel==="capture"&&!state.open&&typeof note==="function")note(hint?value+" "+hint:value)}
function bytes(value){const n=Number(value)||0;if(n<1024)return n+" B";if(n<1048576)return(n/1024).toFixed(0)+" KB";if(n<1073741824)return(n/1048576).toFixed(1)+" MB";return(n/1073741824).toFixed(2)+" GB"}
function clock(ms){const s=Math.max(0,Math.floor(ms/1000));return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")}
function timeOf(iso){const d=new Date(iso);return isNaN(d)?"":d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"})}
// The chord of a capture shortcut for a tooltip, " (⌘S)", when the device page has shortcuts.
function chord(action){try{return typeof shortcutHint==="function"?shortcutHint(action):""}catch{return""}}
function baseName(path){const parts=String(path||"").split("/");return parts[parts.length-1]||String(path||"")}

// Device changes follow the page's control rule; the reason shows while they are off.
function allowed(){try{return Boolean(canSendInput())}catch{return true}}
function lockReason(){if(view&&view.paused)return"Input is paused, so device actions are off. Resume input to use them.";if(view&&view.owner==="agent")return"An agent has control, so device actions are off. Take control to use them.";return"Another client has control, so device actions are off."}
function syncLock(){const ok=allowed();if(lock){lock.hidden=ok;if(!ok)text("actions-lock",lockReason())}for(const control of [...drawer.querySelectorAll("[data-mutate]"),dockShot,dockRecord].filter(Boolean)){const off=!ok||control.getAttribute("data-busy")==="1"||(dockRecord&&!state.record.known&&(control===dockRecord||control===$("actions-record")));if(control.disabled!==off)control.disabled=off}}
async function mutate(control,op,payload){if(!allowed()){syncLock();return null}const peers=op==="capture.screenshot"?[$("actions-shot"),dockShot]:op==="record.start"||op==="record.stop"?[$("actions-record"),dockRecord]:[control];const controls=peers.filter(Boolean);if(controls.some(button=>button.getAttribute("data-busy")==="1"))return null;message("capture");for(const button of controls){button.setAttribute("data-busy","1");button.setAttribute("aria-busy","true")}syncLock();try{return await call(op,payload)}catch(error){showError("capture",error);return null}finally{for(const button of controls){button.removeAttribute("data-busy");button.removeAttribute("aria-busy")}syncLock()}}

// Drawer and tabs (arrows, Home and End move between tabs; Escape closes).
function selectTab(name,focus){const index=TABS.indexOf(name);if(index<0)return;state.tab=name;canvasSelectTab(tabs,panels,index,focus);onTabShown(name);syncPolling()}
function onTabShown(name){if(!state.open)return;if(name==="capture"){loadRecordStatus();loadGallery()}else{pollActivity();if(!state.health.loaded)health.load(false)}}
function closeTools(){if(!document.body.classList.contains("tools-open"))return;const close=$("tools-close");if(close&&typeof close.click==="function")close.click()}
function setOpen(open,returnFocus){open=Boolean(open);if(state.open===open)return;if(open)closeTools();state.open=open;drawer.hidden=!open;document.body.classList.toggle("actions-open",open);toggle.setAttribute("aria-pressed",String(open));toggle.title=open?"Hide device actions":"Device actions";if(inspectorToggle)inspectorToggle.setAttribute("aria-pressed",String(!open&&!document.body.classList.contains("no-inspector")&&!document.body.classList.contains("tools-open")));if(open){syncLock();state.lockTimer=setInterval(syncLock,500);const tab=tabs[TABS.indexOf(state.tab)];if(tab)tab.focus();onTabShown(state.tab)}else{clearInterval(state.lockTimer);state.lockTimer=null;if(returnFocus)toggle.focus()}syncPolling()}
function syncPolling(){const activity=state.open&&state.tab==="activity";if(activity&&!state.activity.timer)state.activity.timer=setInterval(pollActivity,ACTIVITY_MS);if(!activity&&state.activity.timer){clearInterval(state.activity.timer);state.activity.timer=null}health.sync();syncRecordTimers()}
toggle.addEventListener("click",()=>setOpen(!state.open,false));
$("actions-close").addEventListener("click",()=>setOpen(false,true));
// The Tools drawer and the inspector take the same column: opening either closes this one.
if(toolsToggle)toolsToggle.addEventListener("click",()=>{if(state.open&&document.body.classList.contains("tools-open"))setOpen(false,false)});
if(inspectorToggle)inspectorToggle.addEventListener("click",()=>{if(!state.open)return;setOpen(false,false);document.body.classList.remove("no-inspector");inspectorToggle.setAttribute("aria-pressed","true");inspectorToggle.title="Hide inspector"});
bindCanvasTabs($("actions-tablist"),tabs,()=>TABS.indexOf(state.tab),(index,focus)=>selectTab(TABS[index],focus));
drawer.addEventListener("keydown",event=>{if(event.key!=="Escape")return;event.preventDefault();setOpen(false,true)});
// The workspace shell opens and closes this drawer in a tile: {type: "autonom:panel", name: "actions", open}.
if(typeof window!=="undefined"&&typeof window.addEventListener==="function")window.addEventListener("message",event=>{const data=event&&event.data;if(!data||data.type!=="autonom:panel"||data.name!=="actions")return;if(typeof location!=="undefined"&&event.origin!==location.origin)return;if(typeof window.parent!=="undefined"&&window.parent!==window&&event.source!==window.parent)return;setOpen(Boolean(data.open),false)});

// Screenshot.
function savedText(capture){return"Saved "+capture.name}
function pruneText(count){return"Deleted "+count+" oldest capture"+(count===1?"":"s")+" to stay under 200 files / 2 GB"}
function showPrune(count){const box=$("actions-prune");if(!box)return;if(!count){box.hidden=true;return}box.textContent=pruneText(count);box.hidden=false}
async function takeScreenshot(){const result=await mutate($("actions-shot"),"capture.screenshot",typeof captureFrameOptions==="function"?captureFrameOptions():{});if(!result||!result.capture)return;showOk("capture",savedText(result.capture),result.warning||"");if(result.pruned&&result.pruned.length)showPrune(result.pruned.length);loadGallery()}
$("actions-shot").addEventListener("click",takeScreenshot);
if(dockShot)dockShot.addEventListener("click",takeScreenshot);
for(const button of [$("actions-shot"),dockShot])if(button)button.title+=chord("screenshot");

// Recording: one per device; the elapsed time counts locally, the status is re-read every 2 s.
function syncRecordButton(){const button=$("actions-record");button.textContent=state.record.active?"Stop recording":"Record";button.setAttribute("aria-pressed",String(state.record.active));button.title="Record video on this computer, up to 3 minutes"+chord("record");if(dockRecord){const label=state.record.active?"Stop recording":"Start recording";dockRecord.setAttribute("aria-label",label);dockRecord.setAttribute("title",label+chord("record"));dockRecord.setAttribute("aria-pressed",String(state.record.active))}if(!state.record.active)text("actions-rec-time","")}
function renderElapsed(){if(!state.record.active)return;text("actions-rec-time","Recording "+clock(Date.now()-state.record.started)+" / "+clock(RECORD_LIMIT*1000))}
function syncRecordTimers(){const on=(state.open||Boolean(dockRecord))&&state.record.active;if(on&&!state.record.tick){state.record.tick=setInterval(renderElapsed,1000);state.record.poll=setInterval(loadRecordStatus,RECORD_MS)}if(!on&&state.record.tick){clearInterval(state.record.tick);clearInterval(state.record.poll);state.record.tick=null;state.record.poll=null}}
function setRecording(active,startedAt,elapsed){state.record.known=true;state.record.active=active;if(active){const parsed=Date.parse(startedAt||"");state.record.started=typeof elapsed==="number"?Date.now()-elapsed:(isNaN(parsed)?Date.now():parsed)}syncRecordButton();renderElapsed();syncRecordTimers();syncLock()}
async function loadRecordStatus(){if(state.record.busy||$("actions-record").getAttribute("data-busy")==="1")return;state.record.busy=true;const revision=state.record.revision;try{const status=await call("record.status",{});if(revision!==state.record.revision)return;state.record.unsure=0;const was=state.record.active;setRecording(Boolean(status.recording),status.started_at,status.elapsed_ms);if(was&&!status.recording&&status.last){if(status.last.capture)showOk("capture","The recording reached "+clock(RECORD_LIMIT*1000)+" and was saved as "+status.last.capture.name,status.last.warning||"");else if(status.last.error)message("capture","error","The recording ended and could not be saved: "+status.last.error,"");loadGallery()}}catch(error){showError("capture",error)}finally{state.record.busy=false;retryRecordStatus()}}
function retryRecordStatus(){if(state.record.unsure<=0||state.record.retry)return;state.record.unsure-=1;state.record.retry=setTimeout(()=>{state.record.retry=null;loadRecordStatus()},RECORD_MS)}
// After a failed start or stop the tools process may still run the call (it was only
// queued): re-read the status until one read succeeds, at most RECORD_RECHECKS times.
function confirmRecordState(){state.record.unsure=RECORD_RECHECKS;loadRecordStatus()}
async function toggleRecording(){const button=$("actions-record");if(button.getAttribute("data-busy")==="1"||(dockRecord&&!state.record.known))return;state.record.revision+=1;if(!state.record.active){const result=await mutate(button,"record.start",typeof captureFrameOptions==="function"?captureFrameOptions():{});if(result&&result.recording){setRecording(true,result.recording.started_at);showOk("capture","Recording. Press Stop recording to save it.")}else if(!result){confirmRecordState()}return}const result=await mutate(button,"record.stop",{});if(!result){confirmRecordState();return}setRecording(false);if(result.capture)showOk("capture",savedText(result.capture)+" ("+clock(result.duration_ms||0)+")",result.warning||(result.ended_early?"The recording reached its 3 minute limit before you stopped it.":""));if(result.pruned&&result.pruned.length)showPrune(result.pruned.length);loadGallery()}
$("actions-record").addEventListener("click",toggleRecording);
// Cmd+S and Cmd+R on the device page: the same calls as the buttons, under the same rules.
if(typeof window!=="undefined"&&typeof window.addEventListener==="function")window.addEventListener("autonom:capture",event=>{const action=event&&event.detail&&event.detail.action;if(action!=="screenshot"&&action!=="record")return;if(typeof event.preventDefault==="function")event.preventDefault();if(action==="screenshot")takeScreenshot();else toggleRecording()});
if(dockRecord){dockRecord.addEventListener("click",toggleRecording);setInterval(()=>{syncLock();if(hasCsrf()&&!state.record.active)loadRecordStatus()},RECORD_MS)}

$("actions-open-folder").addEventListener("click",async()=>{message("capture");try{const result=await call("captures.open_directory",{});if(result.opened)showOk("capture","Opened the captures folder in Finder.");else message("capture","error","The folder was not opened.","")}catch(error){showError("capture",error)}});
// Gallery: the device's captures, newest first; files are served by /tools/captures/<name>.
async function loadGallery(){const seq=++state.gallery.seq;let result;try{result=await call("captures.list",{limit:200})}catch(error){if(seq===state.gallery.seq)showError("capture",error);return}if(seq!==state.gallery.seq)return;text("actions-gallery-dir",(result.count||0)+" captures · "+bytes(result.total_bytes));$("actions-gallery-dir").hidden=!(result.count>0);$("actions-open-folder").title=result.dir?"Open "+result.dir:"Open capture folder";if(result.last_pruned&&result.last_pruned.count)showPrune(result.last_pruned.count);const list=$("actions-gallery");list.replaceChildren();for(const capture of result.captures||[]){const row=el("li");const href=toolsPath("/tools/captures/"+encodeURIComponent(capture.name));if(capture.kind==="screenshot"){const link=el("a");link.setAttribute("href",href);link.setAttribute("target","_blank");link.setAttribute("rel","noopener");const image=el("img","act-thumb");image.setAttribute("src",href);image.setAttribute("alt",capture.name);image.setAttribute("loading","lazy");link.appendChild(image);row.appendChild(link)}else{const link=el("a","act-video","Open video ("+clock(capture.duration_ms||0)+")");link.setAttribute("href",href);link.setAttribute("target","_blank");link.setAttribute("rel","noopener");row.appendChild(link)}const name=el("span","act-item-title",capture.name);name.title=capture.name;row.appendChild(name);row.appendChild(el("span","act-item-meta",bytes(capture.size)+(capture.width?" · "+capture.width+"×"+capture.height:"")));const remove=makeButton("Delete","btn small",{"aria-label":"Delete "+capture.name});remove.addEventListener("click",async()=>{remove.disabled=true;try{await call("captures.delete",{name:capture.name});loadGallery()}catch(error){remove.disabled=false;showError("capture",error)}});row.appendChild(remove);list.appendChild(row)}$("actions-gallery-empty").hidden=Boolean((result.captures||[]).length)}
$("actions-gallery-refresh").addEventListener("click",()=>loadGallery());

// Activity: this device's commands from the Canvas command log, every 2 s while visible.
function activityQuery(){let query="/api/activity?limit=200&after="+state.activity.after+"&seen="+state.activity.seen;if(deviceId)query+="&device="+encodeURIComponent(deviceId);if($("actions-failures").checked)query+="&failures=1";return query}
function renderActivity(){const list=$("actions-activity");const rows=Array.from(state.activity.rows.values()).sort((a,b)=>b.seq-a.seq).slice(0,ROW_LIMIT);list.replaceChildren();for(const entry of rows){const row=el("li");row.setAttribute("data-ok",String(entry.ok));row.appendChild(el("span","act-log-time",timeOf(entry.at)));row.appendChild(el("span","act-log-name",entry.name));const result=entry.ok===null?"Running":entry.ok?"OK"+(entry.duration_ms!==null?" · "+entry.duration_ms+" ms":""):(entry.error_code||"failed")+(entry.duration_ms!==null?" · "+entry.duration_ms+" ms":"");row.appendChild(el("span","act-log-result",result));const detail=[entry.summary,entry.ok===false?entry.error:null,entry.exit_code!==null&&entry.exit_code!==undefined?"exit "+entry.exit_code:null,entry.origin?"by "+entry.origin:null].filter(Boolean).join(" · ");row.title=detail;if(detail&&entry.ok!==true)row.appendChild(el("span","act-log-detail",detail));list.appendChild(row)}$("actions-activity-empty").hidden=rows.length>0;const dropped=$("actions-activity-dropped");dropped.hidden=!state.activity.dropped;if(state.activity.dropped)dropped.textContent=state.activity.dropped+" older commands left the log before they were shown."}
async function pollActivity(){if(state.activity.busy||!state.open)return;state.activity.busy=true;const generation=state.activity.generation;try{const page=await request("GET",activityQuery());if(generation!==state.activity.generation)return;for(const entry of page.entries||[])state.activity.rows.set(entry.seq,entry);while(state.activity.rows.size>ROW_LIMIT){const oldest=Math.min.apply(null,Array.from(state.activity.rows.keys()));state.activity.rows.delete(oldest)}state.activity.after=typeof page.next==="number"?page.next:state.activity.after;if(typeof page.seen==="number")state.activity.seen=Math.max(state.activity.seen,page.seen);state.activity.dropped+=page.dropped||0;renderActivity()}catch(error){if(generation===state.activity.generation)showError("activity",error)}finally{state.activity.busy=false}}
function resetActivity(){state.activity.generation+=1;state.activity.after=0;state.activity.seen=0;state.activity.rows=new Map();state.activity.dropped=0;state.activity.busy=false;renderActivity()}
$("actions-failures").addEventListener("change",()=>{resetActivity();pollActivity()});
$("actions-activity-clear").addEventListener("click",async()=>{try{await request("POST","/api/activity/clear",{});resetActivity();pollActivity()}catch(error){showError("activity",error)}});

// Setup checklist: this device's platform decides which tools are needed. While the Activity
// tab is visible and one of them is not OK it re-checks itself; it stops once they are OK, or
// while the drawer, the tab or the browser tab is hidden. Fix commands are shown and copied only.
function healthVisible(){return state.open&&state.tab==="activity"&&!(typeof document!=="undefined"&&document.visibilityState==="hidden")}
function healthPlatforms(){return[drawer.getAttribute("data-platform")==="ios"?"ios":"android"]}
function renderHealth(result){state.health.loaded=true;renderSetupChecklist($("actions-health"),result&&result.tools);const parts=[];if(result&&result.checked_at)parts.push("Checked at "+timeOf(result.checked_at));if(result&&result.error)parts.push(result.error);if(health.wanted())parts.push("checking again every "+Math.round(SETUP_POLL_MS/1000)+" s");const value=parts.join(" · ");text("actions-health-time",value);$("actions-health-time").hidden=!value;$("actions-health-refresh").title=value||"Check tools again"}
const health=createSetupChecker({fetch:refresh=>request("GET","/api/health"+(refresh?"?refresh=1":"")),render:renderHealth,visible:healthVisible,platforms:healthPlatforms,error:error=>showError("activity",error)});
$("actions-health-refresh").addEventListener("click",()=>health.load(true));
if(typeof document!=="undefined"&&typeof document.addEventListener==="function")document.addEventListener("visibilitychange",()=>health.sync());
syncRecordButton();
})();`;
}
