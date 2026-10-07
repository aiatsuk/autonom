// The Tools drawer of the Mobile Canvas page (contract section 3 of run canvas-web-controls):
// permissions and location (App), device simulations, network capture, network mocks and the
// live device log. Four pure functions return strings that renderPage inlines: the header
// toggle, the drawer markup, its styles and its script. The markup holds no device or
// network data; the script renders all of that with textContent, never innerHTML.

const LOG_ROW_LIMIT = 2000;
const REQUEST_ROW_LIMIT = 500;
const PUSH_PAYLOAD_LIMIT = 4096;
const MOCK_BODY_LIMIT = 32 * 1024;

export const TOOLS_TABS = Object.freeze([
  Object.freeze({ id: "app", label: "App" }),
  Object.freeze({ id: "simulate", label: "Simulate" }),
  Object.freeze({ id: "network", label: "Network" }),
  Object.freeze({ id: "mocks", label: "Mocks" }),
  Object.freeze({ id: "logs", label: "Logs" }),
]);

// Inline SVG paths on the page's 24 px grid; stroke and size come from the page CSS.
const TOOLS_ICONS = Object.freeze({
  tools: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  close: '<path d="m7 7 10 10M17 7 7 17"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5v4.5H15"/>',
});

// Location presets: name, latitude, longitude (Mountain View is the emulator's default fix).
const LOCATION_PRESETS = Object.freeze([
  ["Mountain View", 37.422, -122.084],
  ["Cupertino", 37.3349, -122.009],
  ["London", 51.5072, -0.1276],
  ["Amsterdam", 52.3702, 4.8952],
  ["Tokyo", 35.6764, 139.65],
  ["Sydney", -33.8688, 151.2093],
]);

const DEFAULT_PUSH = '{"aps":{"alert":{"title":"Autonom","body":"Hello from the Canvas"},"sound":"default"}}';
const HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];

function platformOf(context) {
  const value = context && (context.platform || (context.options && context.options.platform));
  return value === "ios" ? "ios" : "android";
}

function svg(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${TOOLS_ICONS[name]}</svg>`;
}

function methodOptions(anyLabel) {
  return `<option value="">${anyLabel}</option>` + HTTP_METHODS.map((method) => `<option value="${method}">${method}</option>`).join("");
}

/** The header toggle: an icon button in the page's toolbar style (aria-pressed follows the drawer). */
export function toolsButton() {
  return `<button type="button" class="icon tools-toggle" id="tools-toggle" aria-pressed="false" aria-controls="tools" ` +
    `title="Tools" aria-label="Tools">${svg("tools")}</button>`;
}

function tabList() {
  return TOOLS_TABS.map(({ id, label }, index) =>
    `<button type="button" role="tab" class="tools-tab" id="tools-tab-${id}" aria-controls="tools-panel-${id}" ` +
    `aria-selected="${index === 0}" tabindex="${index === 0 ? 0 : -1}">${label}</button>`).join("");
}

function panel(id, body) {
  const first = id === TOOLS_TABS[0].id;
  return `<section class="tools-panel" role="tabpanel" id="tools-panel-${id}" aria-labelledby="tools-tab-${id}" tabindex="0"${first ? "" : " hidden"}>` +
    `<div class="tools-msg" id="tools-msg-${id}" role="status" aria-live="polite" hidden></div>${body}</section>`;
}

function appPanel(ios) {
  const presets = LOCATION_PRESETS.map(([name, latitude, longitude]) =>
    `<button type="button" class="tools-chip" data-mutate data-loc-preset="${name}" data-lat="${latitude}" data-lon="${longitude}">${name}</button>`).join("");
  const permissionsNote = ios
    ? `<p class="tools-note" id="tools-perms-note">iOS cannot report whether a permission is granted. Grant, revoke and reset set it without reading it back.</p>`
    : "";
  const locationActions = ios
    ? `<button type="button" class="btn" id="tools-loc-clear" data-mutate>Clear</button>`
    : `<button type="button" class="btn" id="tools-loc-get">Read back</button>`;
  const locationNote = ios
    ? `<p class="tools-note">The Simulator cannot report its location, so this shows the last value set here.</p>`
    : `<p class="tools-note">The emulator moves its position only once an app subscribes to location updates; Read back says whether the fix was delivered or only requested.</p>`;
  return panel("app",
    `<div class="tools-group">` +
      `<label class="tools-label" for="tools-app-id">App id</label>` +
      `<div class="tools-field"><input id="tools-app-id" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${ios ? "com.example.App" : "com.example.app"}">` +
      `<button type="button" class="btn" id="tools-perms-load">Load</button></div>` +
      `<p class="tools-hint" id="tools-app-source">Loading the device context…</p>` +
    `</div>` +
    `<div class="tools-group">` +
      `<h3 id="tools-perms-title">Permissions</h3>${permissionsNote}` +
      `<ul class="tools-list" id="tools-perms" aria-labelledby="tools-perms-title"></ul>` +
      `<p class="tools-empty" id="tools-perms-empty">Enter an app id to list its permissions.</p>` +
    `</div>` +
    `<div class="tools-group">` +
      `<h3>Location</h3>` +
      `<div class="tools-pair">` +
        `<div><label class="tools-label" for="tools-lat">Latitude</label><input id="tools-lat" type="text" inputmode="decimal" autocomplete="off" placeholder="37.4220"></div>` +
        `<div><label class="tools-label" for="tools-lon">Longitude</label><input id="tools-lon" type="text" inputmode="decimal" autocomplete="off" placeholder="-122.0840"></div>` +
      `</div>` +
      `<div class="tools-chips" role="group" aria-label="Location presets">${presets}</div>` +
      `<div class="tools-actions"><button type="button" class="btn primary" id="tools-loc-set" data-mutate>Set location</button>${locationActions}</div>` +
      `<p class="tools-readout" id="tools-loc-readout" aria-live="polite"></p>${locationNote}` +
    `</div>`);
}

function simButton(control, action, label, primary = false) {
  return `<button type="button" class="btn${primary ? " primary" : ""}" data-mutate data-sim="${control}" data-action="${action}">${label}</button>`;
}

function simulatePanel(ios) {
  const push = ios
    ? `<div class="tools-group" data-sim-group="push">` +
        `<h3>Push notification</h3>` +
        `<label class="tools-label" for="tools-push-app">Bundle id</label>` +
        `<input id="tools-push-app" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Same as the App tab">` +
        `<label class="tools-label" for="tools-push-payload">Payload (JSON, at most 4 KB)</label>` +
        `<textarea id="tools-push-payload" rows="4" spellcheck="false">${DEFAULT_PUSH.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</textarea>` +
        `<div class="tools-actions">${simButton("push", "send", "Send push", true)}</div>` +
      `</div>`
    : "";
  const biometric = ios
    ? simButton("biometric", "match", "Matching face or finger") + simButton("biometric", "nonmatch", "Non-matching") +
      simButton("biometric", "enroll", "Enroll") + simButton("biometric", "unenroll", "Unenroll")
    : simButton("biometric", "match", "Matching fingerprint");
  const batteryState = ios
    ? `<div><label class="tools-label" for="tools-battery-state">State</label><select id="tools-battery-state">` +
      `<option value="charged">Charged</option><option value="charging">Charging</option><option value="discharging">Discharging</option></select></div>`
    : "";
  const network = ios
    ? ""
    : `<div class="tools-group" data-sim-group="network"><h3>Network</h3>` +
      `<div class="tools-actions">${simButton("network", "online", "Online")}${simButton("network", "offline", "Offline")}</div>` +
      `<p class="tools-note">Turns the emulator's Wi-Fi and mobile data on or off.</p></div>`;
  return panel("simulate",
    push +
    `<div class="tools-group" data-sim-group="biometric"><h3>Biometrics</h3><div class="tools-actions">${biometric}</div></div>` +
    `<div class="tools-group" data-sim-group="battery"><h3>Battery</h3>` +
      `<div class="tools-pair"><div><label class="tools-label" for="tools-battery-level">Level (%)</label>` +
      `<input id="tools-battery-level" type="number" inputmode="numeric" min="0" max="100" step="1" value="20"></div>${batteryState}</div>` +
      `<div class="tools-actions">${simButton("battery", "set", "Set battery", true)}${simButton("battery", "reset", "Reset")}</div></div>` +
    network +
    `<div class="tools-group" data-sim-group="appearance"><h3>Appearance</h3>` +
      `<div class="tools-actions">${simButton("appearance", "light", "Light")}${simButton("appearance", "dark", "Dark")}</div></div>`);
}

function networkPanel() {
  return panel("network",
    `<div class="tools-callout" id="tools-net-nosession" hidden>` +
      `<p class="tools-callout-title" id="tools-net-nosession-text">Network capture needs an Autonom session on this device.</p>` +
      `<p class="tools-callout-hint" id="tools-net-nosession-hint"></p>` +
      `<button type="button" class="btn small" id="tools-net-recheck">Check again</button>` +
    `</div>` +
    `<div id="tools-net-body">` +
      `<div class="tools-group">` +
        `<h3>Capture</h3>` +
        `<dl class="tools-dl"><dt>Proxy</dt><dd id="tools-net-proxy">—</dd><dt>Device attached</dt><dd id="tools-net-attached">—</dd><dt>Recent requests</dt><dd id="tools-net-flows">—</dd></dl>` +
        `<div class="tools-actions">` +
          `<button type="button" class="btn primary" id="tools-net-start" data-mutate>Start capture</button>` +
          `<button type="button" class="btn" id="tools-net-attach" data-mutate>Attach device</button>` +
          `<button type="button" class="btn" id="tools-net-detach" data-mutate>Detach</button>` +
          `<button type="button" class="btn" id="tools-net-stop" data-mutate>Stop capture</button>` +
          `<button type="button" class="icon tools-icon" id="tools-net-refresh" title="Refresh status" aria-label="Refresh status">${svg("refresh")}</button>` +
        `</div>` +
        `<div class="tools-confirm" id="tools-net-confirm" role="alertdialog" aria-modal="false" aria-labelledby="tools-net-confirm-title" aria-describedby="tools-net-confirm-text" hidden>` +
          `<p class="tools-confirm-title" id="tools-net-confirm-title">Decrypt this session's traffic?</p>` +
          `<p class="tools-confirm-text" id="tools-net-confirm-text">Capture decrypts and records this session's HTTP(S) traffic through a local proxy on this Mac (a man-in-the-middle). Only redacted previews of headers and bodies are kept.</p>` +
          `<div class="tools-actions"><button type="button" class="btn" id="tools-net-cancel">Cancel</button>` +
          `<button type="button" class="btn primary" id="tools-net-confirm-go" data-mutate>Start capture</button></div>` +
        `</div>` +
      `</div>` +
      `<div class="tools-group">` +
        `<h3 id="tools-req-title">Requests</h3>` +
        `<div class="tools-filters">` +
          `<div class="tools-filter-wide"><label class="tools-label" for="tools-req-host">Host</label><input id="tools-req-host" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Any host"></div>` +
          `<div><label class="tools-label" for="tools-req-method">Method</label><select id="tools-req-method">${methodOptions("Any")}</select></div>` +
          `<div><label class="tools-label" for="tools-req-status">Status</label><input id="tools-req-status" type="text" inputmode="numeric" autocomplete="off" placeholder="Any"></div>` +
          `<div><label class="tools-label" for="tools-req-mocked">Mocked</label><select id="tools-req-mocked"><option value="">Any</option><option value="true">Mocked</option><option value="false">Not mocked</option></select></div>` +
        `</div>` +
        `<div class="tools-reqs" id="tools-req-list" role="list" aria-labelledby="tools-req-title"></div>` +
        `<p class="tools-empty" id="tools-req-empty">No requests yet. Start capture and attach the device, then use the app.</p>` +
      `</div>` +
      `<div class="tools-group tools-detail" id="tools-req-detail" hidden>` +
        `<div class="tools-detail-head"><h3 id="tools-req-detail-title">Request</h3>` +
        `<button type="button" class="btn small" id="tools-req-mock">Mock this</button>` +
        `<button type="button" class="icon tools-icon" id="tools-req-close" title="Close details" aria-label="Close details">${svg("close")}</button></div>` +
        `<div id="tools-req-detail-body"></div>` +
        `<p class="tools-note">Previews are redacted at capture and cut at 2 KB; full bodies stay out of the browser.</p>` +
      `</div>` +
    `</div>`);
}

function mocksPanel() {
  return panel("mocks",
    `<p class="tools-callout tools-machine" id="tools-mocks-note">Mocks apply to every Autonom session on this Mac.</p>` +
    `<div class="tools-group">` +
      `<h3 id="tools-mocks-title">Mocks</h3>` +
      `<ul class="tools-list" id="tools-mocks" aria-labelledby="tools-mocks-title"></ul>` +
      `<p class="tools-empty" id="tools-mocks-empty">No mocks yet.</p>` +
      `<div class="tools-actions"><button type="button" class="btn" id="tools-mock-clear" data-mutate>Clear all</button></div>` +
      `<div class="tools-confirm" id="tools-mock-confirm" role="alertdialog" aria-modal="false" aria-labelledby="tools-mock-confirm-title" aria-describedby="tools-mock-confirm-text" hidden>` +
        `<p class="tools-confirm-title" id="tools-mock-confirm-title">Remove every mock?</p>` +
        `<p class="tools-confirm-text" id="tools-mock-confirm-text">This removes all mocks on this Mac, including those other sessions use.</p>` +
        `<div class="tools-actions"><button type="button" class="btn" id="tools-mock-cancel">Cancel</button>` +
        `<button type="button" class="btn danger" id="tools-mock-clear-go" data-mutate>Remove all</button></div>` +
      `</div>` +
    `</div>` +
    `<form class="tools-group" id="tools-mock-form" novalidate aria-labelledby="tools-mock-form-title">` +
      `<h3 id="tools-mock-form-title">New mock</h3>` +
      `<label class="tools-label" for="tools-mock-glob">URL glob</label>` +
      `<input id="tools-mock-glob" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="*/api/items*">` +
      `<div class="tools-pair">` +
        `<div><label class="tools-label" for="tools-mock-method">Method</label><select id="tools-mock-method">${methodOptions("Any method")}</select></div>` +
        `<div><label class="tools-label" for="tools-mock-status">Status</label><input id="tools-mock-status" type="text" inputmode="numeric" autocomplete="off" value="200"></div>` +
      `</div>` +
      `<label class="tools-label" for="tools-mock-host">Host (optional)</label>` +
      `<input id="tools-mock-host" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="api.example.com">` +
      `<label class="tools-check"><input id="tools-mock-ignore-query" type="checkbox"> Ignore the query string</label>` +
      `<label class="tools-label" for="tools-mock-headers">Response headers (one "Name: value" per line)</label>` +
      `<textarea id="tools-mock-headers" rows="2" spellcheck="false" placeholder="Content-Type: application/json"></textarea>` +
      `<label class="tools-label" for="tools-mock-body">Response body (at most 32 KB)</label>` +
      `<textarea id="tools-mock-body" rows="4" spellcheck="false"></textarea>` +
      `<label class="tools-label" for="tools-mock-note">Note (optional)</label>` +
      `<input id="tools-mock-note" type="text" autocomplete="off">` +
      `<div class="tools-actions"><button type="submit" class="btn primary" id="tools-mock-save" data-mutate>Add mock</button>` +
      `<button type="button" class="btn" id="tools-mock-cancel-edit" hidden>Cancel editing</button></div>` +
    `</form>`);
}

function logsPanel(ios) {
  const levels = ios
    ? [["0", "All levels"], ["1", "Info and above"], ["2", "Default and above"], ["3", "Errors and faults"]]
    : [["0", "Verbose"], ["1", "Debug"], ["2", "Info"], ["3", "Warning"], ["4", "Error"]];
  return panel("logs",
    `<div class="tools-group">` +
      `<div class="tools-filters">` +
        `<div class="tools-filter-wide"><label class="tools-label" for="tools-log-package">${ios ? "Process or bundle (optional)" : "App package (optional)"}</label>` +
        `<div class="tools-field"><input id="tools-log-package" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="All apps">` +
        `<button type="button" class="btn" id="tools-log-apply">Apply</button></div></div>` +
        `<div><label class="tools-label" for="tools-log-level">Level</label><select id="tools-log-level">` +
          levels.map(([value, label]) => `<option value="${value}">${label}</option>`).join("") + `</select></div>` +
        `<div class="tools-filter-wide"><label class="tools-label" for="tools-log-filter">Filter</label>` +
        `<input id="tools-log-filter" type="search" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Text in the line"></div>` +
      `</div>` +
      `<div class="tools-logbar"><p class="tools-log-state" id="tools-log-state" aria-live="polite">Not following.</p>` +
        `<button type="button" class="btn small" id="tools-log-pause" aria-pressed="false">Pause</button>` +
        `<button type="button" class="btn small" id="tools-log-clear">Clear</button></div>` +
      `<div class="tools-log" id="tools-log" role="log" aria-live="off" aria-label="Device log" tabindex="0"></div>` +
      `<div class="tools-logfoot"><span id="tools-log-count">0 lines</span>` +
        `<button type="button" class="btn small" id="tools-log-latest" hidden>Jump to latest</button></div>` +
    `</div>`);
}

/**
 * The drawer: a tab list of App, Simulate, Network, Mocks and Logs, one tab panel each. Only
 * static, platform-specific markup; the script fills in device and network data as text.
 */
export function toolsMarkup(context = {}) {
  const platform = platformOf(context);
  const ios = platform === "ios";
  return `<aside class="tools" id="tools" hidden aria-label="Tools" data-platform="${platform}">` +
    `<div class="tools-head">` +
      `<div class="tools-tabs" id="tools-tablist" role="tablist" aria-label="Tools">${tabList()}</div>` +
      `<button type="button" class="icon tools-icon" id="tools-close" title="Close tools" aria-label="Close tools">${svg("close")}</button>` +
    `</div>` +
    `<p class="tools-lock" id="tools-lock" role="status" aria-live="polite" hidden></p>` +
    `<div class="tools-body">` +
      appPanel(ios) + simulatePanel(ios) + networkPanel() + mocksPanel() + logsPanel(ios) +
    `</div>` +
  `</aside>`;
}

/**
 * Styles on the page's own tokens (:root in renderPage), light and dark. Desktop: the drawer
 * takes the inspector's column, wider, while open. Phone (760 px and below, the page's own
 * one-column width, which covers 720 px): a full-width sheet.
 */
export function toolsStyles() {
  return `
/* tools drawer */
:root{--t-danger:#d70015;--t-sheet-shadow:0 -12px 40px rgba(0,0,0,.16)}
body.tools-open,body.tools-open.no-inspector{grid-template-columns:minmax(0,1fr) clamp(440px,34vw,520px);grid-template-areas:"bar bar" "stage side"}
body.tools-open aside.side{display:none}
.tools{grid-area:side;display:flex;flex-direction:column;min-width:0;min-height:0;border-left:1px solid var(--line);background:var(--surface)}
.tools-head{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 14px;border-bottom:1px solid var(--line)}
.tools-tabs{display:flex;flex:1;min-width:0;gap:2px;padding:2px;border-radius:9px;background:var(--fill);overflow-x:auto;scrollbar-width:none}
.tools-tabs::-webkit-scrollbar{display:none}
.tools-tab{flex:1 0 auto;height:28px;padding:0 10px;border:0;border-radius:7px;background:transparent;color:var(--text-2);font-weight:500;white-space:nowrap}
.tools-tab[aria-selected=true]{background:var(--raised);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.08),0 0 0 .5px rgba(0,0,0,.04)}
.tools-tab:not([aria-selected=true]):active{transform:none;background:var(--fill-2)}
.tools-icon{flex:none}
.tools-body{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain}
.tools-panel{padding:0 0 28px;outline:none}
.tools-panel:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
.tools-group{padding:14px 16px;border-bottom:1px solid var(--line)}
.tools-group:last-child{border-bottom:0}
.tools-group h3{margin:0 0 10px;font-size:12px;font-weight:600;color:var(--text-2);letter-spacing:.01em}
.tools-label{display:block;margin:10px 0 5px;font-size:12px;color:var(--text-2)}
.tools-group>.tools-label:first-child,.tools-pair .tools-label,.tools-filters .tools-label{margin-top:0}
.tools input:not([type=checkbox]),.tools select,.tools textarea{display:block;width:100%;min-width:0;height:32px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--bg);color:var(--text);font:inherit}
.tools select{padding-right:6px}
.tools textarea{height:auto;min-height:64px;padding:8px 10px;font:12px/1.45 var(--mono);resize:vertical}
.tools input::placeholder,.tools textarea::placeholder{color:var(--text-2);opacity:1}
.tools-field{display:flex;gap:6px}
.tools-field input{flex:1}
.tools-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.tools-group>.tools-pair:first-of-type{margin-top:0}
.tools-filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:10px}
.tools-filter-wide{grid-column:1/-1}
.tools-actions{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:10px}
.tools-actions:first-child{margin-top:0}
.tools .btn.small{height:28px;padding:0 10px;font-size:12px}
.tools .btn.danger{background:var(--t-danger);border-color:transparent;color:#fff}
.tools-hint,.tools-note,.tools-empty{margin:8px 0 0;color:var(--text-2);font-size:12px;line-height:1.4}
.tools-empty{margin-top:4px}
.tools-check{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:12.5px}
.tools-check input{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.tools-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tools-chip{height:28px;padding:0 11px;border:0;border-radius:999px;background:var(--fill);color:var(--text);font-size:12px;font-weight:500}
.tools-readout{margin:10px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tools-readout:empty{display:none}
.tools-msg{margin:12px 16px 0;padding:9px 12px;border-radius:10px;background:var(--fill);font-size:12.5px;overflow-wrap:anywhere}
.tools-msg[data-kind=error]{background:color-mix(in srgb,var(--t-danger) 10%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--t-danger) 22%,transparent)}
.tools-msg p{margin:0}
.tools-msg-hint{margin-top:3px!important;color:var(--text-2)}
.tools-lock{margin:10px 14px 0;padding:8px 12px;border-radius:10px;background:color-mix(in srgb,var(--warn) 12%,transparent);font-size:12.5px}
.tools-callout{margin:12px 16px 0;padding:10px 12px;border-radius:10px;background:var(--fill);font-size:12.5px;overflow-wrap:anywhere}
.tools-callout p{margin:0}
.tools-callout-hint{margin:4px 0 8px!important;color:var(--text-2);font:11.5px/1.45 var(--mono)}
.tools-machine{margin-bottom:2px;color:var(--text)}
.tools-list{list-style:none;margin:0;padding:0}
.tools-item{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:6px 10px;padding:9px 0;border-top:1px solid var(--line)}
.tools-item:first-child{border-top:0;padding-top:0}
.tools-item-name{min-width:0}
.tools-item-name b{display:block;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-item-name small{display:block;color:var(--text-2);font:11.5px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-item-actions{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px}
.tools-badge{padding:2px 8px;border-radius:999px;background:var(--fill);color:var(--text-2);font-size:11.5px;font-weight:500;white-space:nowrap}
.tools-badge[data-state=on]{color:var(--live)}
.tools-badge[data-state=off]{color:var(--t-danger)}
.tools-switch[aria-pressed=true]{color:var(--live)}
.tools-dl{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:7px 12px}
.tools-dl dt{color:var(--text-2)}
.tools-dl dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tools-confirm{margin-top:12px;padding:12px;border-radius:12px;border:1px solid var(--line-2);background:var(--raised);box-shadow:var(--shadow)}
.tools-confirm-title{margin:0;font-weight:600}
.tools-confirm-text{margin:4px 0 0;color:var(--text-2);font-size:12.5px}
.tools-reqs{display:grid;gap:1px;max-height:min(46vh,420px);overflow:auto;margin:0 -8px}
.tools-req{display:grid;grid-template-columns:54px 36px minmax(0,1fr) auto;align-items:center;gap:8px;width:100%;height:32px;padding:0 8px;border:0;border-radius:var(--r-sm);background:none;text-align:left;font-variant-numeric:tabular-nums}
.tools-req:active{transform:none}
.tools-req[aria-current=true]{background:var(--fill-2)}
.tools-req-method{font:600 11px var(--mono);color:var(--text-2)}
.tools-req-status{font:12px var(--mono)}
.tools-req-status[data-tone=ok]{color:var(--live)}
.tools-req-status[data-tone=client]{color:var(--warn)}
.tools-req-status[data-tone=server]{color:var(--t-danger)}
.tools-req-url{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-req-meta{color:var(--text-2);font-size:11.5px;white-space:nowrap}
.tools-detail-head{display:flex;align-items:center;gap:8px}
.tools-detail-head h3{flex:1;min-width:0;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-detail h4{margin:14px 0 6px;font-size:12px;font-weight:600;color:var(--text-2)}
.tools-pre{margin:0;max-height:220px;overflow:auto;padding:8px 10px;border-radius:var(--r-sm);background:var(--bg);border:1px solid var(--line);font:11.5px/1.45 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.tools-logbar{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.tools-log-state{flex:1;min-width:0;margin:0;color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
.tools-log-state[data-kind=error]{color:var(--t-danger)}
.tools-log{height:min(58vh,560px);overflow:auto;padding:6px 0;border-radius:10px;border:1px solid var(--line);background:var(--bg);font:11.5px/1.5 var(--mono);overscroll-behavior:contain}
.tools-log-row{padding:0 10px;white-space:pre-wrap;overflow-wrap:anywhere}
.tools-log-row[data-level=V],.tools-log-row[data-level=D],.tools-log-row[data-level=Debug]{color:var(--text-2)}
.tools-log-row[data-level=W]{color:var(--warn)}
.tools-log-row[data-level=E],.tools-log-row[data-level=F],.tools-log-row[data-level=Error],.tools-log-row[data-level=Fault]{color:var(--t-danger)}
.tools-log-row[data-kind=warning]{color:var(--warn);font-style:italic}
.tools-logfoot{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:28px;margin-top:6px;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
@media (hover:hover){
  .tools-tab:not([aria-selected=true]):hover{color:var(--text)}
  .tools-chip:not(:disabled):hover,.tools-req:hover{background:var(--fill-2)}
}
@media (prefers-color-scheme:dark){
  :root{--t-danger:#ff453a;--t-sheet-shadow:0 -12px 40px rgba(0,0,0,.6)}
  .tools-tab[aria-selected=true]{background:var(--fill-2);box-shadow:0 0 0 .5px rgba(255,255,255,.08)}
  .tools-confirm{box-shadow:0 0 0 1px rgba(255,255,255,.06),var(--shadow)}
}
/* phone: the page switches to its one-column layout at 760 px, so the drawer becomes a
   full-width sheet over the page from the same width (this covers the 720 px phone range);
   44 px targets, 16 px inputs so iOS does not zoom; the toggle stays visible */
@media (max-width:760px){
  .actions .icon.tools-toggle{display:grid;width:44px;height:44px}
  .tools{position:fixed;left:0;right:0;bottom:0;top:calc(58px + env(safe-area-inset-top));z-index:6;width:100%;border:0;border-top:1px solid var(--line-2);border-radius:14px 14px 0 0;
    box-shadow:var(--t-sheet-shadow);padding-bottom:env(safe-area-inset-bottom);animation:tools-sheet .24s cubic-bezier(.32,.72,0,1)}
  body.tools-open{overflow:hidden}
  .tools-head{padding:8px 8px 8px 12px}
  .tools-tab{height:36px;padding:0 9px}
  .tools-icon{width:44px;height:44px}
  .tools input:not([type=checkbox]),.tools select{height:44px;font-size:16px}
  .tools textarea{font-size:16px}
  .tools .btn,.tools-chip{height:44px}
  .tools .btn.small{height:36px}
  .tools-filters{grid-template-columns:repeat(2,minmax(0,1fr))}
  .tools-req{height:44px;grid-template-columns:48px 34px minmax(0,1fr)}
  .tools-req-meta{display:none}
  .tools-log{height:52vh}
}
@media (max-width:360px){.tools-pair{grid-template-columns:minmax(0,1fr)}.tools-tab{padding:0 7px}}
@keyframes tools-sheet{from{transform:translateY(24px);opacity:0}}
@media (prefers-reduced-motion:reduce){.tools{animation:none}}
`;
}

/**
 * The drawer's code: plain JavaScript appended after pageScript() in the same script element.
 * It is written inside this template literal, so it avoids backticks and its escapes are
 * doubled. It reads the page globals csrf, url, $, view, canSendInput and note, and keeps its
 * own names inside one function scope.
 */
export function toolsScript() {
  return `(function(){
"use strict";
const LOG_LIMIT=${LOG_ROW_LIMIT},REQUEST_LIMIT=${REQUEST_ROW_LIMIT},PUSH_LIMIT=${PUSH_PAYLOAD_LIMIT},BODY_LIMIT=${MOCK_BODY_LIMIT};
const drawer=$("tools"),toggle=$("tools-toggle");
if(!drawer||!toggle)return;
const PLATFORM=drawer.getAttribute("data-platform")==="ios"?"ios":"android";
const TABS=["app","simulate","network","mocks","logs"];
const tabs=TABS.map(name=>$("tools-tab-"+name)),panels=TABS.map(name=>$("tools-panel-"+name));
const lock=$("tools-lock"),inspectorToggle=$("inspector-toggle");
const NO_SESSION=["no_active_session","session_target_mismatch"];
const state={open:false,tab:"app",context:null,contextBusy:false,lockTimer:null,lastLocation:null,perms:{app:null,seq:0},
  net:{ready:false,checked:false,timer:null,busy:false,cursor:null,generation:0,requests:new Map(),selected:null,consented:false,pending:null},
  mocks:{items:[],editing:null},
  logs:{timer:null,busy:false,after:0,buffer:[],paused:false,feed:false,pkg:null,dropped:0,unseen:0,generation:0,starting:false}};

function text(id,value){const node=$(id);if(node&&node.textContent!==value)node.textContent=value}
function el(tag,className,value){const node=document.createElement(tag);if(className)node.className=className;if(value!==undefined&&value!==null)node.textContent=String(value);return node}
function makeButton(label,className,attributes){const node=el("button",className||"btn small",label);node.setAttribute("type","button");if(attributes)for(const key of Object.keys(attributes))node.setAttribute(key,attributes[key]);return node}
function capital(value){return value.charAt(0).toUpperCase()+value.slice(1)}
function utf8(value){return new TextEncoder().encode(value).length}
function hasCsrf(){return typeof csrf!=="undefined"&&csrf}

// Requests: the page's post() only throws the message, so this wrapper keeps error_code and hint.
function failure(payload,status){const error=new Error(payload&&payload.error||"the request failed"+(status?" ("+status+")":""));error.code=payload&&payload.error_code||null;error.hint=payload&&payload.hint||null;error.status=status||0;return error}
async function request(method,path,body){const init={method,headers:{}};if(method!=="GET"){init.headers["Content-Type"]="application/json";init.headers["X-Autonom-Origin"]="human";if(hasCsrf())init.headers["X-Autonom-CSRF"]=csrf;init.body=JSON.stringify(body||{})}let response;try{response=await fetch(url(path),init)}catch(error){throw failure({error:"The Canvas did not answer: "+(error&&error.message||error),error_code:"unreachable"},0)}let payload=null;try{payload=await response.json()}catch{payload=null}if(!response.ok||!payload||payload.ok===false)throw failure(payload||{error:response.statusText||null},response.status);return payload}
async function call(op,payload){const answer=await request("POST","/tools/call",{op,payload:payload||{}});return answer.result||{}}
function localError(message,code,hint){return failure({error:message,error_code:code||null,hint:hint||null},0)}

// Messages: one line for the outcome, a second for the hint; text only.
function message(panel,kind,value,hint){const box=$("tools-msg-"+panel);if(!box)return;box.replaceChildren();if(!value){box.hidden=true;box.removeAttribute("data-kind");return}box.setAttribute("data-kind",kind);box.appendChild(el("p","tools-msg-text",value));if(hint)box.appendChild(el("p","tools-msg-hint",hint));box.hidden=false}
function errorText(error){return(error&&error.message||String(error))+(error&&error.code?" ("+error.code+")":"")}
function showError(panel,error){message(panel,"error",errorText(error),error&&error.hint||"")}
function showOk(panel,value){message(panel,"ok",value,"")}

// Device changes follow the page's control rule; the reason shows while they are off.
function allowed(){try{return Boolean(canSendInput())}catch{return true}}
function lockReason(){if(view.paused)return"Input is paused, so device changes are off. Resume input to change the device.";if(view.owner==="agent")return"An agent has control, so device changes are off. Take control to change the device.";return"Another client has control, so device changes are off."}
function syncLock(){const ok=allowed();if(lock){lock.hidden=ok;if(!ok)text("tools-lock",lockReason())}for(const control of drawer.querySelectorAll("[data-mutate]")){const off=!ok||control.getAttribute("data-busy")==="1";if(control.disabled!==off)control.disabled=off}}
// A timed-out change may still have been applied and is never retried: say the outcome is unknown and
// re-read what the change may have touched (permissions.list, location.get where the device can report
// it, network.status, mocks.list) so the panel is not left stale. Each re-read resolves to its own
// error or null, so the verdict never depends on what another request wrote into the message box.
function isTimeout(error){return Boolean(error)&&(error.code==="timeout"||error.status===504)}
const LOCATION_OPS=["location.set","location.clear"];
function locationReadable(){return Boolean($("tools-loc-get"))&&(!state.context||state.context.location_readable===true)}
function rereadFor(panel,op){if(LOCATION_OPS.includes(op))return locationReadable()?readLocation:null;if(panel==="app")return state.perms.app?()=>loadPermissions(state.perms.app):null;if(panel==="network")return refreshStatus;if(panel==="mocks")return loadMocks;return null}
async function unknownOutcome(panel,op){const reread=rereadFor(panel,op),location=LOCATION_OPS.includes(op);let hint;if(reread){message(panel);let failed=null;try{failed=await reread()}catch(error){failed=error||localError("the re-read failed")}hint=failed?"Re-reading the current state also failed: "+errorText(failed):location?"The location was read back; check it before trying again.":"The current state was re-read; check it before trying again."}else if(location){text("tools-loc-readout","Unknown: the last location change did not answer.");hint="This device cannot report its location, so it could not be re-read; check the device before trying again."}else hint="Nothing here can be re-read, so check the device before trying again.";message(panel,"error","No answer within 60 s, so the outcome is unknown: the change may or may not have been applied (timeout).",hint)}
async function mutate(panel,control,op,payload){if(!allowed()){syncLock();return null}message(panel);if(control){control.setAttribute("data-busy","1");control.setAttribute("aria-busy","true")}syncLock();try{return await call(op,payload)}catch(error){if(isTimeout(error))await unknownOutcome(panel,op);else showError(panel,error);return null}finally{if(control){control.removeAttribute("data-busy");control.removeAttribute("aria-busy")}syncLock()}}

// Drawer and tabs (automatic activation; arrows, Home and End move between tabs).
function selectTab(name,focus){const index=TABS.indexOf(name);if(index<0)return;state.tab=name;tabs.forEach((tab,i)=>{const on=i===index;tab.setAttribute("aria-selected",String(on));tab.setAttribute("tabindex",on?"0":"-1");panels[i].hidden=!on});if(focus)tabs[index].focus();onTabShown(name);syncActivity()}
function onTabShown(name){if(!state.open)return;if(name==="network"&&!state.net.checked)refreshStatus();if(name==="mocks")loadMocks()}
function setOpen(open,returnFocus){if(state.open===open)return;state.open=open;drawer.hidden=!open;document.body.classList.toggle("tools-open",open);toggle.setAttribute("aria-pressed",String(open));toggle.title=open?"Hide tools":"Show tools";if(inspectorToggle)inspectorToggle.setAttribute("aria-pressed",String(!open&&!document.body.classList.contains("no-inspector")));if(open){syncLock();state.lockTimer=setInterval(syncLock,500);loadContext();const tab=tabs[TABS.indexOf(state.tab)];if(tab)tab.focus();onTabShown(state.tab)}else{clearInterval(state.lockTimer);state.lockTimer=null;state.net.checked=false;state.net.ready=false;hideConfirms();deactivateFeed();if(returnFocus)toggle.focus()}syncActivity()}
function syncActivity(){syncNetworkPolling();syncLogPolling()}
toggle.addEventListener("click",()=>setOpen(!state.open,false));
$("tools-close").addEventListener("click",()=>setOpen(false,true));
if(inspectorToggle)inspectorToggle.addEventListener("click",()=>{if(!state.open)return;setOpen(false,false);document.body.classList.remove("no-inspector");inspectorToggle.setAttribute("aria-pressed","true");inspectorToggle.title="Hide inspector"});
tabs.forEach((tab,i)=>tab.addEventListener("click",()=>selectTab(TABS[i],false)));
$("tools-tablist").addEventListener("keydown",event=>{const index=TABS.indexOf(state.tab),moves={ArrowRight:index+1,ArrowLeft:index-1,Home:0,End:TABS.length-1},next=moves[event.key];if(next===undefined)return;event.preventDefault();selectTab(TABS[(next+TABS.length)%TABS.length],true)});
drawer.addEventListener("keydown",event=>{if(event.key!=="Escape")return;event.preventDefault();if(hideConfirms())return;setOpen(false,true)});

// Context: fetched when the drawer first opens; it fills the app id and narrows the controls.
async function loadContext(){if(state.context||state.contextBusy)return;state.contextBusy=true;try{const context=await call("context");state.context=context;applyContext(context)}catch(error){text("tools-app-source","The device context is not available.");showError("app",error)}finally{state.contextBusy=false}}
function applyContext(context){const appField=$("tools-app-id"),session=context.session||null;if(appField&&!appField.value&&context.app_id)appField.value=String(context.app_id);text("tools-app-source",session&&session.app_id?"From the Autonom session on this device. Edit it to use another app.":context.app_id?"The best-known app on this device. Edit it to use another app.":"No app is known for this device; enter an app id.");const simulate=context.simulate&&typeof context.simulate==="object"?context.simulate:null;if(simulate){for(const group of drawer.querySelectorAll("[data-sim-group]"))group.hidden=!Array.isArray(simulate[group.getAttribute("data-sim-group")]);for(const control of drawer.querySelectorAll("[data-sim]")){const actions=simulate[control.getAttribute("data-sim")];control.hidden=!(Array.isArray(actions)&&actions.includes(control.getAttribute("data-action")))}}const clear=$("tools-loc-clear");if(clear)clear.hidden=context.location_clearable!==true;const readBack=$("tools-loc-get");if(readBack)readBack.hidden=context.location_readable!==true;if(context.network_available===false)setNoSession(true,null);if(appField&&appField.value)loadPermissions()}

// App: permissions.
function appId(){const field=$("tools-app-id");return field?field.value.trim():""}
function shortName(name){const value=String(name||"");return value.startsWith("android.permission.")?value.slice(19):value}
function clearPermissions(value){const perms=state.perms;perms.seq+=1;perms.app=null;$("tools-perms").replaceChildren();text("tools-perms-empty",value);$("tools-perms-empty").hidden=false}
async function loadPermissions(target){const id=target||appId(),perms=state.perms;if(!id){clearPermissions("Enter an app id to list its permissions.");return null}perms.seq+=1;const seq=perms.seq;try{const result=await call("permissions.list",{app_id:id});if(seq!==perms.seq)return null;perms.app=id;renderPermissions(result);return null}catch(error){if(seq!==perms.seq)return null;showError("app",error);return error}}
function renderPermissions(result){const list=$("tools-perms"),empty=$("tools-perms-empty"),items=Array.isArray(result.permissions)?result.permissions:[],readable=result.readable!==false;list.replaceChildren();empty.hidden=items.length>0;if(!items.length)text("tools-perms-empty",PLATFORM==="ios"?"No privacy services were reported.":"This app requests no runtime permissions.");for(const item of items){const name=String(item.name||""),label=item.alias?String(item.alias):shortName(name),row=el("li","tools-item"),title=el("div","tools-item-name");title.appendChild(el("b",null,label));if(label!==name)title.appendChild(el("small",null,name));row.appendChild(title);if(readable){const badge=el("span","tools-badge",item.granted===true?"Granted":item.granted===false?"Denied":"Unknown");badge.setAttribute("data-state",item.granted===true?"on":item.granted===false?"off":"unknown");row.appendChild(badge)}const actions=el("div","tools-item-actions");for(const action of ["grant","revoke","reset"])actions.appendChild(makeButton(capital(action),"btn small",{"data-mutate":"","data-perm":name,"data-perm-label":label,"data-perm-action":action,"aria-label":capital(action)+" "+label}));row.appendChild(actions);list.appendChild(row)}syncLock()}
$("tools-perms-load").addEventListener("click",()=>{message("app");loadPermissions()});
$("tools-app-id").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();message("app");loadPermissions()}});
$("tools-app-id").addEventListener("input",()=>{const perms=state.perms;if(perms.app!==null&&appId()!==perms.app)clearPermissions("Press Load to list this app's permissions.")});
$("tools-perms").addEventListener("click",async event=>{const control=event.target&&event.target.closest?event.target.closest("[data-perm-action]"):null;if(!control||control.disabled)return;const id=state.perms.app,action=control.getAttribute("data-perm-action"),label=control.getAttribute("data-perm-label");if(!id){showError("app",localError("Enter an app id first.","invalid_value"));return}const result=await mutate("app",control,"permissions.set",{app_id:id,service:control.getAttribute("data-perm"),action});if(!result)return;showOk("app",capital(action)+" "+label+": done"+(PLATFORM==="ios"?" (iOS does not report the new state).":"."));if(PLATFORM==="android"&&state.perms.app===id)loadPermissions(id)});

// App: location.
function formatCoordinate(value){return Number(value).toFixed(4)}
function coordinates(){const latText=$("tools-lat").value.trim(),lonText=$("tools-lon").value.trim(),latitude=Number(latText),longitude=Number(lonText);if(!latText||!Number.isFinite(latitude)||latitude<-90||latitude>90)throw localError("Latitude must be a number from -90 to 90.","invalid_coordinates");if(!lonText||!Number.isFinite(longitude)||longitude<-180||longitude>180)throw localError("Longitude must be a number from -180 to 180.","invalid_coordinates");return{latitude,longitude}}
function showLocation(prefix,latitude,longitude){text("tools-loc-readout",prefix+formatCoordinate(latitude)+", "+formatCoordinate(longitude))}
async function setLocation(control){let point;try{point=coordinates()}catch(error){showError("app",error);return}const result=await mutate("app",control,"location.set",point);if(!result)return;state.lastLocation=point;if(PLATFORM==="ios")showLocation("Last set here: ",point.latitude,point.longitude);else showLocation("Requested: ",point.latitude,point.longitude);showOk("app","Location set.")}
$("tools-loc-set").addEventListener("click",event=>setLocation(event.currentTarget||$("tools-loc-set")));
for(const preset of drawer.querySelectorAll("[data-loc-preset]"))preset.addEventListener("click",()=>{$("tools-lat").value=preset.getAttribute("data-lat");$("tools-lon").value=preset.getAttribute("data-lon");setLocation(preset)});
const readBack=$("tools-loc-get");
async function readLocation(){try{renderReadBack(await call("location.get"));return null}catch(error){return error}}
if(readBack)readBack.addEventListener("click",async()=>{message("app");const error=await readLocation();if(error)showError("app",error)});
function renderReadBack(result){const requested=result.requested&&typeof result.requested==="object"?result.requested:null;if(requested&&result.delivered===false){showLocation("Requested only (the emulator updates its position once an app subscribes): ",requested.latitude,requested.longitude);return}if(result.latitude===null||result.latitude===undefined){text("tools-loc-readout","No last known location on the device.");return}showLocation(requested?"Delivered: ":"Current: ",result.latitude,result.longitude)}
const clearLocation=$("tools-loc-clear");
if(clearLocation)clearLocation.addEventListener("click",async()=>{const result=await mutate("app",clearLocation,"location.clear",{});if(!result)return;state.lastLocation=null;text("tools-loc-readout","No location set here.");showOk("app","Location cleared.")});

// Simulate: only the controls and actions the context lists for this platform.
function verdict(result){if(result&&result.verified===true)return"confirmed on the device";if(result&&result.verification==="mismatch")return"sent, but the device read back another value";return"sent (the device cannot confirm it)"}
function simValues(control,action){if(control==="push"){const pushApp=$("tools-push-app"),app=(pushApp&&pushApp.value.trim())||appId(),raw=$("tools-push-payload").value;if(!app)throw localError("Enter a bundle id here or on the App tab.","invalid_value");if(utf8(raw)>PUSH_LIMIT)throw localError("The payload is larger than 4 KB.","invalid_value");let payload;try{payload=JSON.parse(raw)}catch{throw localError("The payload is not valid JSON.","invalid_value")}if(!payload||typeof payload!=="object"||Array.isArray(payload))throw localError("The payload must be a JSON object.","invalid_value");return{app_id:app,payload}}if(control==="battery"&&action==="set"){const level=Number($("tools-battery-level").value);if(!Number.isInteger(level)||level<0||level>100)throw localError("The battery level must be a whole number from 0 to 100.","invalid_value");const values={level};const stateField=$("tools-battery-state");if(stateField)values.state=stateField.value;return values}return{}}
$("tools-panel-simulate").addEventListener("click",async event=>{const control=event.target&&event.target.closest?event.target.closest("[data-sim]"):null;if(!control||control.disabled)return;const name=control.getAttribute("data-sim"),action=control.getAttribute("data-action");let values;try{values=simValues(name,action)}catch(error){showError("simulate",error);return}const result=await mutate("simulate",control,"simulate",{control:name,action,values});if(result)showOk("simulate",capital(name)+" "+action+": "+verdict(result)+".")});

// Network: status, consent before start, attach, detach, stop and a live request list.
function setNoSession(show,error){const box=$("tools-net-nosession"),body=$("tools-net-body");if(show)state.net.ready=false;if(!show){box.hidden=true;body.hidden=false;return}text("tools-net-nosession-text",error&&error.message?error.message:"Network capture needs an Autonom session on this device.");text("tools-net-nosession-hint",error&&error.hint?error.hint:"Start one with autonom session start for this device.");box.hidden=false;body.hidden=true}
function attachedText(value){return value===true?"Yes":value===false?"No":value==="unknown"?"Unknown":"—"}
function renderStatus(result){const proxy=result.proxy||{},running=proxy.running===true,attached=result.attached;text("tools-net-proxy",running?"Running"+(proxy.port?" on port "+proxy.port:""):"Stopped");text("tools-net-attached",attachedText(attached));text("tools-net-flows",result.recent_flow_count===undefined||result.recent_flow_count===null?"—":String(result.recent_flow_count));$("tools-net-start").hidden=running;$("tools-net-stop").hidden=!running;$("tools-net-attach").hidden=!running||attached===true;$("tools-net-detach").hidden=attached===false}
async function refreshStatus(){state.net.checked=true;let failed=null;try{const result=await call("network.status");state.net.ready=true;setNoSession(false,null);renderStatus(result)}catch(error){failed=error;if(NO_SESSION.includes(error.code))setNoSession(true,error);else showError("network",error)}syncNetworkPolling();return failed}
function hideConfirms(){let closed=false;for(const id of ["tools-net-confirm","tools-mock-confirm"]){const box=$(id);if(box&&!box.hidden){box.hidden=true;closed=true}}state.net.pending=null;return closed}
function askConsent(op){state.net.pending=op;text("tools-net-confirm-go",op==="network.attach"?"Attach device":"Start capture");$("tools-net-confirm").hidden=false;syncLock();$("tools-net-cancel").focus()}
async function runNetwork(control,op,payload,done){const result=await mutate("network",control,op,payload);if(!result)return;showOk("network",done);refreshStatus()}
$("tools-net-start").addEventListener("click",()=>askConsent("network.start"));
$("tools-net-attach").addEventListener("click",()=>{if(state.net.consented)runNetwork($("tools-net-attach"),"network.attach",{acknowledged:true},"The device is attached to the proxy.");else askConsent("network.attach")});
$("tools-net-cancel").addEventListener("click",()=>{const op=state.net.pending;hideConfirms();const back=$(op==="network.attach"?"tools-net-attach":"tools-net-start");if(back)back.focus()});
$("tools-net-confirm-go").addEventListener("click",()=>{const op=state.net.pending;if(!op)return;hideConfirms();state.net.consented=true;if(op==="network.attach")runNetwork($("tools-net-attach"),op,{acknowledged:true},"The device is attached to the proxy.");else runNetwork($("tools-net-start"),op,{acknowledged:true},"Capture started. Attach the device to route its traffic through the proxy.")});
$("tools-net-detach").addEventListener("click",()=>runNetwork($("tools-net-detach"),"network.detach",{},"The device is detached from the proxy."));
$("tools-net-stop").addEventListener("click",()=>runNetwork($("tools-net-stop"),"network.stop",{},"Capture stopped."));
$("tools-net-refresh").addEventListener("click",()=>{message("network");refreshStatus()});
$("tools-net-recheck").addEventListener("click",()=>refreshStatus());
function requestFilters(){const filters={},host=$("tools-req-host").value.trim(),method=$("tools-req-method").value,status=$("tools-req-status").value.trim(),mocked=$("tools-req-mocked").value;if(host)filters.host=host;if(method)filters.method=method;if(status&&/^[1-5][0-9][0-9]$/.test(status))filters.status=Number(status);if(mocked==="true"||mocked==="false")filters.mocked=mocked==="true";return filters}
function syncNetworkPolling(){const net=state.net,want=state.open&&state.tab==="network"&&net.ready;if(want&&!net.timer){net.timer=setInterval(pollRequests,1000);pollRequests()}else if(!want&&net.timer){clearInterval(net.timer);net.timer=null}}
function resetRequests(){const net=state.net;net.generation+=1;net.cursor=null;net.requests.clear();net.selected=null;$("tools-req-list").replaceChildren();$("tools-req-empty").hidden=false;$("tools-req-detail").hidden=true}
async function pollRequests(){const net=state.net;if(net.busy||!net.timer)return;net.busy=true;const generation=net.generation;try{const payload=Object.assign({max:100},requestFilters());if(net.cursor)payload.since_id=net.cursor;const result=await call("network.requests",payload);if(generation!==net.generation)return;const warnings=Array.isArray(result.warnings)?result.warnings:[];if(net.cursor&&warnings.some(item=>item&&item.code==="since_id_not_found")){resetRequests();net.generation=generation}addRequests(Array.isArray(result.requests)?result.requests:[])}catch(error){if(NO_SESSION.includes(error.code)){setNoSession(true,error);syncNetworkPolling()}else showError("network",error)}finally{net.busy=false}}
function tone(status){const value=Number(status);if(value>=500)return"server";if(value>=400)return"client";if(value>=300)return"redirect";if(value>=200)return"ok";return"other"}
function requestRow(item){const row=el("div","tools-req-item");row.setAttribute("role","listitem");const control=makeButton("","tools-req",{"data-req":String(item.id)});control.appendChild(el("span","tools-req-method",item.method||"—"));const status=el("span","tools-req-status",item.status===undefined||item.status===null?"—":String(item.status));status.setAttribute("data-tone",tone(item.status));control.appendChild(status);control.appendChild(el("span","tools-req-url",(item.host||"")+(item.path||"")||item.url||""));control.appendChild(el("span","tools-req-meta",(item.mocked?"mocked · ":"")+(typeof item.duration_ms==="number"?item.duration_ms+" ms":"")));control.title=String(item.url||"");row.appendChild(control);return row}
function addRequests(items){const net=state.net,list=$("tools-req-list");if(!items.length)return;net.cursor=String(items[0].id);for(let i=items.length-1;i>=0;i--){const item=items[i];if(!item||item.id===undefined||net.requests.has(String(item.id)))continue;net.requests.set(String(item.id),item);list.insertBefore(requestRow(item),list.firstChild)}while(list.children.length>REQUEST_LIMIT){const last=list.lastChild,control=last&&last.firstChild;if(control)net.requests.delete(control.getAttribute("data-req"));list.removeChild(last)}$("tools-req-empty").hidden=list.children.length>0}
for(const id of ["tools-req-host","tools-req-status"])$(id).addEventListener("change",()=>{resetRequests();if(state.net.timer)pollRequests()});
for(const id of ["tools-req-method","tools-req-mocked"])$(id).addEventListener("change",()=>{resetRequests();if(state.net.timer)pollRequests()});
$("tools-req-list").addEventListener("click",event=>{const control=event.target&&event.target.closest?event.target.closest("[data-req]"):null;if(control)showRequest(control.getAttribute("data-req"))});
async function showRequest(id){const net=state.net;net.selected=id;for(const control of $("tools-req-list").querySelectorAll("[data-req]")){if(control.getAttribute("data-req")===id)control.setAttribute("aria-current","true");else control.removeAttribute("aria-current")}const listed=net.requests.get(id)||null;let item=listed;try{const result=await call("network.request",{id}),flow=result&&result.request&&typeof result.request==="object"?result.request:null;if(flow)item=Object.assign({},listed||{},flow)}catch(error){showError("network",error)}if(!item||net.selected!==id)return;if(net.requests.has(id))net.requests.set(id,item);renderDetail(item)}
function addPairs(parent,title,pairs){parent.appendChild(el("h4",null,title));const list=el("dl","tools-dl");const keys=pairs&&typeof pairs==="object"?Object.keys(pairs):[];if(!keys.length){parent.appendChild(el("p","tools-note","None recorded."));return}for(const key of keys){list.appendChild(el("dt",null,key));list.appendChild(el("dd",null,String(pairs[key])))}parent.appendChild(list)}
function addPreview(parent,title,value){parent.appendChild(el("h4",null,title));if(value===null||value===undefined||value===""){parent.appendChild(el("p","tools-note","Empty."));return}parent.appendChild(el("pre","tools-pre",String(value)))}
function renderDetail(item){const body=$("tools-req-detail-body");body.replaceChildren();text("tools-req-detail-title",(item.method||"")+" "+(item.status===undefined?"":item.status));const summary=el("dl","tools-dl");const rows=[["URL",item.url||""],["Duration",typeof item.duration_ms==="number"?item.duration_ms+" ms":"—"],["Mocked",item.mocked?"Yes"+(item.mock_id?" (mock "+item.mock_id+")":""):"No"],["Started",item.started_at||"—"]];for(const pair of rows){summary.appendChild(el("dt",null,pair[0]));summary.appendChild(el("dd",null,String(pair[1])))}body.appendChild(summary);addPairs(body,"Request headers",item.request_headers_preview);addPreview(body,"Request body",item.request_body_preview);addPairs(body,"Response headers",item.response_headers_preview);addPreview(body,"Response body",item.response_body_preview);$("tools-req-detail").hidden=false}
$("tools-req-close").addEventListener("click",()=>{state.net.selected=null;$("tools-req-detail").hidden=true});
$("tools-req-mock").addEventListener("click",()=>{const item=state.net.selected&&state.net.requests.get(state.net.selected);if(!item)return;fillMockFromRequest(item);selectTab("mocks",false);$("tools-mock-glob").focus();showOk("mocks","Filled from request "+(item.id||state.net.selected)+". Set the response, then add the mock.")});

// Mocks: the machine-wide registry; hits count only this device's session.
function resetMockForm(){state.mocks.editing=null;for(const id of ["tools-mock-glob","tools-mock-host","tools-mock-headers","tools-mock-body","tools-mock-note"])$(id).value="";$("tools-mock-method").value="";$("tools-mock-status").value="200";$("tools-mock-ignore-query").checked=false;$("tools-mock-body").placeholder="";text("tools-mock-form-title","New mock");text("tools-mock-save","Add mock");$("tools-mock-cancel-edit").hidden=true}
function fillMockFromRequest(item){resetMockForm();const address=String(item.url||""),query=address.indexOf("?");$("tools-mock-glob").value=query>=0?address.slice(0,query):address;$("tools-mock-ignore-query").checked=query>=0;$("tools-mock-method").value=HTTP_METHOD(item.method);$("tools-mock-host").value=item.host?String(item.host):"";if(item.status)$("tools-mock-status").value=String(item.status)}
function HTTP_METHOD(value){const method=String(value||"").toUpperCase();return["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"].includes(method)?method:""}
function parseHeaders(raw){const headers={};for(const line of raw.split(/\\r?\\n/)){if(!line.trim())continue;const colon=line.indexOf(":");if(colon<1)throw localError("Each header line needs a name, a colon and a value.","invalid_value");headers[line.slice(0,colon).trim()]=line.slice(colon+1).trim()}return headers}
function mockPayload(){const glob=$("tools-mock-glob").value.trim(),statusText=$("tools-mock-status").value.trim(),status=Number(statusText),body=$("tools-mock-body").value,editing=state.mocks.editing;if(!glob)throw localError("Enter a URL glob, for example */api/items*.","selector_required");if(!/^[0-9]+$/.test(statusText)||status<100||status>599)throw localError("The status must be a whole number from 100 to 599.","invalid_value");if(utf8(body)>BODY_LIMIT)throw localError("The body is larger than 32 KB.","invalid_value");const payload={url_glob:glob,status,ignore_query:$("tools-mock-ignore-query").checked,headers:parseHeaders($("tools-mock-headers").value)};const method=$("tools-mock-method").value,host=$("tools-mock-host").value.trim(),note=$("tools-mock-note").value.trim();if(method||editing)payload.method=method;if(host||editing)payload.host=host;if(note||editing)payload.note=note;if(body||!editing)payload.body=body;if(editing)payload.id=editing;return payload}
async function loadMocks(){try{renderMocks((await call("mocks.list")).mocks||[]);return null}catch(error){showError("mocks",error);return error}}
function renderMocks(items){state.mocks.items=Array.isArray(items)?items:[];const list=$("tools-mocks");list.replaceChildren();$("tools-mocks-empty").hidden=state.mocks.items.length>0;for(const mock of state.mocks.items){const match=mock.match||{},response=mock.response||{},id=String(mock.id),enabled=mock.enabled!==false,row=el("li","tools-item"),title=el("div","tools-item-name");title.appendChild(el("b",null,(match.method?match.method+" ":"")+(match.url_glob||"")));title.appendChild(el("small",null,"#"+id+" · "+(response.status||"—")+" · "+(Number(mock.hits)||0)+" hits"+(match.host?" · "+match.host:"")+(mock.note?" · "+mock.note:"")));row.appendChild(title);const toggleButton=makeButton(enabled?"On":"Off","btn small tools-switch",{"data-mutate":"","data-mock":id,"data-mock-action":enabled?"disable":"enable","aria-pressed":String(enabled),"aria-label":"Mock "+id+" enabled"});row.appendChild(toggleButton);const actions=el("div","tools-item-actions");actions.appendChild(makeButton("Edit","btn small",{"data-mock":id,"data-mock-action":"edit","aria-label":"Edit mock "+id}));actions.appendChild(makeButton("Remove","btn small",{"data-mutate":"","data-mock":id,"data-mock-action":"remove","aria-label":"Remove mock "+id}));row.appendChild(actions);list.appendChild(row)}syncLock()}
function editMock(id){const mock=state.mocks.items.find(item=>String(item.id)===id);if(!mock)return;resetMockForm();const match=mock.match||{},response=mock.response||{},headers=response.headers&&typeof response.headers==="object"?response.headers:{};state.mocks.editing=id;$("tools-mock-glob").value=match.url_glob||"";$("tools-mock-method").value=HTTP_METHOD(match.method);$("tools-mock-host").value=match.host||"";$("tools-mock-ignore-query").checked=match.ignore_query===true;$("tools-mock-status").value=String(response.status||200);$("tools-mock-headers").value=Object.keys(headers).map(key=>key+": "+headers[key]).join("\\n");$("tools-mock-note").value=mock.note||"";$("tools-mock-body").placeholder="Leave empty to keep the current body";text("tools-mock-form-title","Edit mock "+id);text("tools-mock-save","Save changes");$("tools-mock-cancel-edit").hidden=false;$("tools-mock-glob").focus()}
$("tools-mocks").addEventListener("click",async event=>{const control=event.target&&event.target.closest?event.target.closest("[data-mock-action]"):null;if(!control||control.disabled)return;const id=control.getAttribute("data-mock"),action=control.getAttribute("data-mock-action");if(action==="edit"){editMock(id);return}const result=await mutate("mocks",control,"mocks."+action,{id});if(!result)return;if(action==="remove"&&state.mocks.editing===id)resetMockForm();showOk("mocks","Mock "+id+(action==="remove"?" removed.":action==="enable"?" enabled.":" disabled."));loadMocks()});
$("tools-mock-form").addEventListener("submit",async event=>{event.preventDefault();const save=$("tools-mock-save");if(save.disabled)return;let payload;try{payload=mockPayload()}catch(error){showError("mocks",error);return}const editing=state.mocks.editing,result=await mutate("mocks",save,editing?"mocks.update":"mocks.add",payload);if(!result)return;showOk("mocks",editing?"Mock "+editing+" saved.":"Mock "+(result.id||"")+" added.");resetMockForm();loadMocks()});
$("tools-mock-cancel-edit").addEventListener("click",()=>{resetMockForm();message("mocks")});
$("tools-mock-clear").addEventListener("click",()=>{$("tools-mock-confirm").hidden=false;syncLock();$("tools-mock-cancel").focus()});
$("tools-mock-cancel").addEventListener("click",()=>{$("tools-mock-confirm").hidden=true;$("tools-mock-clear").focus()});
$("tools-mock-clear-go").addEventListener("click",async()=>{$("tools-mock-confirm").hidden=true;const result=await mutate("mocks",$("tools-mock-clear"),"mocks.clear",{});if(!result)return;resetMockForm();showOk("mocks","Removed "+(Number(result.removed)||0)+" mocks.");loadMocks()});

// Logs: the Canvas's log feed, polled only while the Logs tab is visible; rows are capped.
const LEVELS=PLATFORM==="ios"?{Debug:0,Info:1,Default:2,Error:3,Fault:3}:{V:0,D:1,I:2,W:3,E:4,F:4,A:4};
const IOS_SHORT={Db:"Debug",Df:"Default",I:"Info",E:"Error",F:"Fault",A:"Fault"};
const iosRecords=new WeakMap();
function iosRecord(entry){if(PLATFORM!=="ios"||!entry||typeof entry!=="object"||typeof entry.text!=="string")return null;if(iosRecords.has(entry))return iosRecords.get(entry);let record=null;const raw=entry.text.trim();if(raw.charAt(0)==="{"){try{const parsed=JSON.parse(raw);if(parsed&&typeof parsed==="object"&&!Array.isArray(parsed))record=parsed}catch{record=null}}iosRecords.set(entry,record);return record}
function iosLine(record){const image=typeof record.processImagePath==="string"?record.processImagePath.split("/").pop():"",parts=[record.timestamp,image||record.process,record.messageType,record.subsystem,record.eventMessage].filter(part=>part!==undefined&&part!==null&&part!=="");return parts.map(String).join(" ")}
function levelOf(entry){if(entry.kind==="warning")return PLATFORM==="ios"?"Error":"W";if(typeof entry.level==="string"&&entry.level in LEVELS)return entry.level;const record=iosRecord(entry);if(record){const type=String(record.messageType||"");return type in LEVELS?type:null}const line=typeof entry.text==="string"?entry.text:"";let found;if(PLATFORM==="ios"){found=/^\\S+\\s+\\S+\\s+(Db|Df|I|E|F|A)\\s/.exec(line);if(found)return IOS_SHORT[found[1]];found=/\\s(Default|Info|Debug|Error|Fault)\\s/.exec(line);return found?found[1]:null}found=/^\\S+\\s+\\S+\\s+\\d+\\s+\\d+\\s+([VDIWEFA])\\s/.exec(line)||/(?:^|\\s)([VDIWEFA])\\/[^\\s(:]*\\s*[(:]/.exec(line);return found?found[1]:null}
function lineText(entry){const record=iosRecord(entry);if(record){const line=iosLine(record);if(line)return line}if(typeof entry.text==="string")return entry.text;if(typeof entry.message==="string")return entry.message;if(entry.kind==="warning")return"warning: "+(entry.error||entry.code||"");try{return JSON.stringify(entry)}catch{return String(entry)}}
function logMatches(entry){const minimum=Number($("tools-log-level").value)||0,level=levelOf(entry),filter=$("tools-log-filter").value.trim().toLowerCase();if(minimum>0&&(level===null||LEVELS[level]<minimum))return false;return!filter||lineText(entry).toLowerCase().includes(filter)}
function logRow(entry){const row=el("div","tools-log-row",lineText(entry)),level=levelOf(entry);if(level)row.setAttribute("data-level",level);if(entry.kind==="warning")row.setAttribute("data-kind","warning");return row}
function nearBottom(box){return box.scrollTop+box.clientHeight>=box.scrollHeight-24}
function appendRows(entries){const box=$("tools-log"),stick=nearBottom(box);for(const entry of entries)if(logMatches(entry))box.appendChild(logRow(entry));while(box.children.length>LOG_LIMIT)box.removeChild(box.firstChild);if(stick)box.scrollTop=box.scrollHeight;$("tools-log-latest").hidden=stick||nearBottom(box)}
function rerenderLogs(){const box=$("tools-log");box.replaceChildren();state.logs.unseen=0;appendRows(state.logs.buffer.slice(-LOG_LIMIT));box.scrollTop=box.scrollHeight;$("tools-log-latest").hidden=true;logCount()}
function logCount(){const logs=state.logs,shown=$("tools-log").children.length;text("tools-log-count",shown+(shown===1?" line":" lines")+(logs.paused&&logs.unseen?" · "+logs.unseen+" new while paused":"")+(logs.dropped?" · "+logs.dropped+" dropped by the Canvas buffer":""))}
function logState(kind,value){const node=$("tools-log-state");if(kind)node.setAttribute("data-kind",kind);else node.removeAttribute("data-kind");text("tools-log-state",value)}
function logPackage(){const value=$("tools-log-package").value.trim();if(!value)return null;if(!/^[A-Za-z0-9_.]{1,255}$/.test(value))throw localError("Use letters, digits, dots and underscores only.","invalid_value");return value}
async function activateFeed(){let pkg;try{pkg=logPackage()}catch(error){showError("logs",error);return}const logs=state.logs,changed=pkg!==logs.pkg;if(changed){logs.generation+=1;logs.buffer=[];logs.after=0;logs.dropped=0;logs.unseen=0;$("tools-log").replaceChildren();$("tools-log-latest").hidden=true;logCount()}const generation=logs.generation;logs.pkg=pkg;logs.feed=true;if(changed)logs.starting=true;try{await request("POST","/tools/logs",{active:true,package:pkg})}catch(error){showError("logs",error)}finally{if(generation===logs.generation)logs.starting=false}}
function deactivateFeed(){const logs=state.logs;if(!logs.feed)return;logs.feed=false;request("POST","/tools/logs",{active:false}).catch(()=>{})}
function syncLogPolling(){const logs=state.logs,want=state.open&&state.tab==="logs";if(want&&!logs.timer){logs.timer=setInterval(pollLogs,700);activateFeed().then(pollLogs)}else if(!want&&logs.timer){clearInterval(logs.timer);logs.timer=null}}
async function pollLogs(){const logs=state.logs;if(logs.busy||logs.starting||!logs.timer)return;logs.busy=true;const generation=logs.generation;try{const data=await request("GET","/tools/logs?after="+encodeURIComponent(String(logs.after))+"&limit=500");if(generation!==logs.generation)return;const lines=Array.isArray(data.lines)?data.lines:[];if(typeof data.next==="number")logs.after=data.next;else if(lines.length&&typeof lines[lines.length-1].seq==="number")logs.after=lines[lines.length-1].seq;logs.dropped+=Math.max(0,Number(data.dropped)||0);if(lines.length){for(const line of lines)logs.buffer.push(line);if(logs.buffer.length>LOG_LIMIT)logs.buffer.splice(0,logs.buffer.length-LOG_LIMIT);if(logs.paused)logs.unseen+=lines.length;else appendRows(lines)}if(data.running)logState(null,logs.paused?"Paused. New lines are kept and shown on resume.":"Following the device log"+(data.package?" for "+data.package:"")+".");else logState(data.error?"error":null,data.error?String(data.error):"The log feed is stopped.");logCount()}catch(error){if(generation===logs.generation)logState("error",error.message+(error.hint?" "+error.hint:""))}finally{logs.busy=false}}
$("tools-log-apply").addEventListener("click",()=>{message("logs");if(state.logs.timer)activateFeed()});
$("tools-log-package").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();message("logs");if(state.logs.timer)activateFeed()}});
$("tools-log-level").addEventListener("change",rerenderLogs);
$("tools-log-filter").addEventListener("input",rerenderLogs);
$("tools-log-pause").addEventListener("click",()=>{const logs=state.logs;logs.paused=!logs.paused;const pause=$("tools-log-pause");pause.setAttribute("aria-pressed",String(logs.paused));text("tools-log-pause",logs.paused?"Resume":"Pause");if(!logs.paused)rerenderLogs();else logCount()});
$("tools-log-clear").addEventListener("click",()=>{const logs=state.logs;logs.buffer=[];logs.unseen=0;$("tools-log").replaceChildren();$("tools-log-latest").hidden=true;logCount()});
$("tools-log").addEventListener("scroll",()=>{$("tools-log-latest").hidden=nearBottom($("tools-log"))});
$("tools-log-latest").addEventListener("click",()=>{const box=$("tools-log");box.scrollTop=box.scrollHeight;$("tools-log-latest").hidden=true});
window.autonomTools=Object.freeze({open:()=>setOpen(true,false),close:()=>setOpen(false,false),select:name=>selectTab(name,false)});
})();`;
}
