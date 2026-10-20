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
    `<button type="button" class="tools-location-row" data-mutate data-location-action data-loc-preset="${name}" data-location-key="preset:${name}" data-lat="${latitude}" data-lon="${longitude}" aria-label="Set location ${name}"><span>${name}</span><small>${latitude.toFixed(4)}, ${longitude.toFixed(4)}</small></button>`).join("");
  const permissionsNote = ios
    ? `<p class="tools-note" id="tools-perms-note">iOS cannot report whether a permission is granted. Grant, revoke and reset set it without reading it back.</p>`
    : "";
  const locationActions = ios
    ? `<button type="button" class="btn" id="tools-loc-clear" data-mutate data-location-action>Clear</button>`
    : `<button type="button" class="icon tools-icon" id="tools-loc-get" aria-label="Refresh location" title="Refresh location"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 7a7 7 0 0 1 11.5-1L20 9M4 15l2.4 3A7 7 0 0 0 17.9 17"/></svg></button>`;
  const locationHint = ios
    ? "The Simulator cannot report its location; this is the last value set here."
    : "Requested coordinates are delivered once an app subscribes to location updates. Refresh checks delivery.";
  return panel("app",
    `<div class="tools-group">` +
      `<div class="tools-form-row"><label class="tools-label" for="tools-app-id">App id</label>` +
      `<div class="tools-field"><input id="tools-app-id" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${ios ? "com.example.App" : "com.example.app"}">` +
      `<button type="button" class="btn" id="tools-perms-load">Load</button></div></div>` +
      `<p class="tools-hint" id="tools-app-source" hidden>Loading the device context…</p>` +
    `</div>` +
    `<div class="tools-group">` +
      `<h3>Deeplinks</h3>` +
      `<div class="tools-form-row"><label class="tools-label" for="tools-deeplink">URL</label>` +
      `<div class="tools-field"><input id="tools-deeplink" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="myapp://screen" title="${ios ? "iOS chooses the app that handles the URL." : "Opens in the selected app."} Recent links are saved for this app in this browser.">` +
      `<button type="button" class="btn primary" id="tools-deeplink-open" data-mutate>Open</button></div></div>` +
      `<div id="tools-deeplinks-history" hidden><div class="tools-group-head"><h4 id="tools-deeplinks-title">Recent deeplinks</h4><button type="button" class="btn small" id="tools-deeplinks-clear">Clear</button></div>` +
      `<ul class="tools-list" id="tools-deeplinks" aria-labelledby="tools-deeplinks-title"></ul>` +
      `<p class="tools-empty" id="tools-deeplinks-empty">No recent deeplinks for this app.</p></div>` +
    `</div>` +
    `<div class="tools-group">` +
      `<h3 id="tools-perms-title">Permissions</h3>${permissionsNote}` +
      `<ul class="tools-list" id="tools-perms" aria-labelledby="tools-perms-title"></ul>` +
      `<p class="tools-empty" id="tools-perms-empty">Enter an app id to list its permissions.</p>` +
    `</div>` +
    `<div class="tools-group">` +
      `<div class="tools-location-heading"><h3>Location</h3>${locationActions}</div>` +
      `<p class="tools-readout tools-location-status" id="tools-loc-readout" aria-live="polite" title="${locationHint}">${ios ? "No location set here." : "Loading…"}</p>` +
      `<div class="tools-location-coordinates">` +
        `<input id="tools-lat" type="text" inputmode="decimal" autocomplete="off" aria-label="Latitude" title="Latitude" placeholder="Latitude">` +
        `<input id="tools-lon" type="text" inputmode="decimal" autocomplete="off" aria-label="Longitude" title="Longitude" placeholder="Longitude">` +
        `<button type="button" class="btn primary" id="tools-loc-set" aria-label="Set location" data-mutate data-location-action>Set</button>` +
      `</div>` +
      `<div class="tools-group-head"><h4>Saved</h4><button type="button" class="icon tools-icon tools-location-add" id="tools-location-add" aria-label="Add saved location" title="Add saved location" aria-controls="tools-location-form" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button></div>` +
      `<div id="tools-location-form" hidden>` +
      `<input id="tools-location-name" aria-label="Location name" title="Location name" type="text" maxlength="80" placeholder="Location name" autocomplete="off">` +
      `<div id="tools-location-edit-coordinates" hidden>` +
      `<input id="tools-location-edit-lat" aria-label="Saved latitude" title="Latitude" placeholder="Latitude" type="text" inputmode="decimal">` +
      `<input id="tools-location-edit-lon" aria-label="Saved longitude" title="Longitude" placeholder="Longitude" type="text" inputmode="decimal"></div>` +
      `<div class="tools-location-form-actions"><button type="button" class="btn" id="tools-location-edit-cancel">Cancel</button>` +
      `<button type="button" class="btn primary" id="tools-location-save" title="Save in this browser without changing device location">Save</button></div>` +
      `<p class="tools-hint" id="tools-location-save-status" role="status"></p></div>` +
      `<div class="tools-locations" role="group" aria-label="Saved locations"><div id="tools-locations-custom"></div>` +
      `<div role="group" aria-label="Location presets">${presets}</div></div><div id="tools-location-feedback" class="tools-hint" role="status"></div>` +
    `</div>`);
}

function simButton(control, action, label, primary = false, caption = label) {
  return `<button type="button" class="btn${primary ? " primary" : ""}" data-mutate data-sim="${control}" data-action="${action}"${caption !== label ? ` aria-label="${label}" title="${label}"` : ""}>${caption}</button>`;
}

function simulatePanel(ios) {
  const row = (control, title, controls, extra = "", hint = "") =>
    `<div class="tools-group tools-sim-setting" data-sim-group="${control}"><div class="tools-sim-row"><h3${control === "biometric" ? ' id="tools-biometry-title"' : ""}${hint ? ` title="${hint}"` : ""}>${title}</h3><div class="tools-sim-controls">${controls}</div></div>${extra}</div>`;
  const biometricSetup = ios
    ? `<div class="tools-sim-controls">${simButton("biometric", "enroll", "Enroll")}${simButton("biometric", "unenroll", "Unenroll")}</div><p class="tools-note">Enroll, then open the app’s biometric prompt.</p>`
    : `<div class="tools-pair"><div><label class="tools-label" for="tools-finger-id">Enrolled id (success)</label><input id="tools-finger-id" type="number" min="1" max="2147483647" step="1" value="1"></div>` +
      `<div><label class="tools-label" for="tools-finger-failure-id">Unenrolled id (failure)</label><input id="tools-finger-failure-id" type="number" min="1" max="2147483647" step="1" value="2147483647"></div></div>` +
      simButton("biometric", "settings", "Open enrollment settings") +
      `<p class="tools-note">Success: enrolled ID. Failure: unenrolled ID. Open the app’s biometric prompt first.</p>`;
  const biometric = row("biometric", "Biometrics",
    simButton("biometric", "match", "Successful authentication", true, "Success") + simButton("biometric", "nonmatch", "Failed authentication", false, "Failure"),
    `<div class="tools-sim-status"><p class="tools-note" id="tools-biometry-status" role="status">Checking biometrics…</p>` +
    `<button type="button" class="icon tools-icon" id="tools-biometry-refresh" aria-label="Refresh status" title="Refresh status">${svg("refresh")}</button></div>` +
    `<details class="tools-sim-details tools-fingerprint-setup"><summary title="${ios ? "Biometric setup" : "Fingerprint setup"}">Setup</summary>${biometricSetup}</details>`);
  const batteryState = ios
    ? `<select id="tools-battery-state" aria-label="Battery state"><option value="charged">Charged</option><option value="charging">Charging</option><option value="discharging">Discharging</option></select>`
    : "";
  const push = ios ? row("push", "Push notification", simButton("push", "send", "Send push", true, "Send"),
    `<details class="tools-sim-details"><summary>Payload</summary><label class="tools-label" for="tools-push-app">Bundle id</label>` +
    `<input id="tools-push-app" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Same as the App tab">` +
    `<label class="tools-label" for="tools-push-payload">Payload</label>` +
    `<textarea id="tools-push-payload" rows="4" spellcheck="false" title="JSON payload, up to 4 KB">${DEFAULT_PUSH.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</textarea></details>`) : "";
  return panel("simulate", push + biometric +
    row("battery", "Battery", `<input id="tools-battery-level" aria-label="Battery level (%)" title="Battery level (%)" type="number" inputmode="numeric" min="0" max="100" step="1" value="20">` + batteryState +
      simButton("battery", "set", "Set battery", true, "Set") + simButton("battery", "reset", "Reset")) +
    (ios ? "" : row("network", "Network", simButton("network", "online", "Online") + simButton("network", "offline", "Offline"), "", "Turns the emulator’s Wi-Fi and mobile data on or off.")) +
    row("appearance", "Appearance", simButton("appearance", "light", "Light") + simButton("appearance", "dark", "Dark")) +
    row("text-size", "Text size", (ios ? [["small", "Small", "S"], ["large", "Default", "Default"], ["extra-extra-extra-large", "Largest", "XL"], ["accessibility-extra-extra-extra-large", "Accessibility", "AX"]]
      : [["0.85", "85%"], ["1.0", "100%"], ["1.3", "130%"], ["1.5", "150%"], ["2.0", "200%"]])
      .map(([value, label, caption]) => simButton("text-size", value, label, false, caption || label)).join(""), "", "Changes the device’s font scale. Apps may need to be reopened.") +
    (ios ? "" : row("animations", "Animations", simButton("animations", "pin", "Disable animations", false, "Disable") + simButton("animations", "reset", "Restore animations", false, "Restore"), "", "Restore returns to the saved scales. App-owned animations may continue.")));
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
        `<div class="tools-section-bar"><h3>Capture</h3><div class="tools-actions">` +
          `<button type="button" class="btn primary" id="tools-net-start" data-mutate>Start capture</button>` +
          `<button type="button" class="btn" id="tools-net-stop" data-mutate>Stop capture</button>` +
          `<button type="button" class="icon tools-icon" id="tools-net-refresh" title="Refresh status" aria-label="Refresh status">${svg("refresh")}</button>` +
        `</div></div>` +
        `<dl class="tools-net-metrics"><div><dt>Proxy</dt><dd id="tools-net-proxy">—</dd></div><div><dt>Attached</dt><dd id="tools-net-attached">—</dd></div><div><dt>Requests</dt><dd id="tools-net-flows">—</dd></div></dl>` +
        `<p class="tools-note" id="tools-net-fixtures" title="Matching responses use session fixtures instead of the backend." hidden></p>` +
        `<div class="tools-confirm" id="tools-net-confirm" role="alertdialog" aria-modal="false" aria-labelledby="tools-net-confirm-title" aria-describedby="tools-net-confirm-text" hidden>` +
          `<p class="tools-confirm-title" id="tools-net-confirm-title">Decrypt this session's traffic?</p>` +
          `<p class="tools-confirm-text" id="tools-net-confirm-text">Start a local proxy on this Mac and connect this device to it. This decrypts and records the device's HTTP(S) traffic. Android may briefly lose connectivity while its proxy setting is applied. Stop capture restores the previous proxy. Certificates are not installed by this action.</p>` +
          `<div class="tools-actions"><button type="button" class="btn" id="tools-net-cancel">Cancel</button>` +
          `<button type="button" class="btn primary" id="tools-net-confirm-go" data-mutate>Start capture</button></div>` +
        `</div>` +
      `</div>` +
      `<div class="tools-group tools-requests-group">` +
        `<h3 id="tools-req-title">Requests</h3>` +
        `<div class="tools-filters">` +
          `<div class="tools-filter-wide"><input aria-label="Host" title="Filter by host" id="tools-req-host" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Filter by host"></div>` +
          `<div><select aria-label="Method" title="HTTP method" id="tools-req-method">${methodOptions("All methods")}</select></div>` +
          `<div><input aria-label="Status" title="HTTP status code" id="tools-req-status" type="text" inputmode="numeric" autocomplete="off" placeholder="Status"></div>` +
          `<div><select aria-label="Mocked" title="Response source" id="tools-req-mocked"><option value="">All responses</option><option value="true">Mocked</option><option value="false">Not mocked</option></select></div>` +
        `</div>` +
        `<div class="tools-reqs" id="tools-req-list" role="list" aria-labelledby="tools-req-title"></div>` +
        `<p class="tools-empty" id="tools-req-empty">No requests yet. Start capture, then use the app.</p>` +
      `</div>` +
      `<dialog class="tools-detail" id="tools-req-detail" aria-labelledby="tools-req-detail-title" hidden><div class="tools-detail-sheet">` +
        `<div class="tools-detail-head"><h3 id="tools-req-detail-title">Request</h3>` +
        `<button type="button" class="btn small" id="tools-req-mock">Mock this</button>` +
        `<button type="button" class="icon tools-icon" id="tools-req-close" title="Close details" aria-label="Close details">${svg("close")}</button></div>` +
        `<div class="tools-detail-content"><div id="tools-req-detail-body"></div>` +
        `<p class="tools-note" title="Previews are redacted and cut at 2 KB. Copy loads the complete redacted text when captured (up to 4 MB).">Redacted previews · 2 KB limit</p></div>` +
      `</div></dialog>` +
    `</div>`);
}

function mocksPanel() {
  return panel("mocks",
    `<p class="tools-note tools-scope-note" id="tools-mocks-note">Mocks apply to every Autonom session on this Mac.</p>` +
    `<div class="tools-group">` +
      `<div class="tools-section-bar"><h3 id="tools-mocks-title">Mocks</h3><button type="button" class="btn small" id="tools-mock-clear" data-mutate>Clear all</button></div>` +
      `<ul class="tools-list" id="tools-mocks" aria-labelledby="tools-mocks-title"></ul>` +
      `<p class="tools-empty" id="tools-mocks-empty">No mocks yet.</p>` +
      `<div class="tools-confirm" id="tools-mock-confirm" role="alertdialog" aria-modal="false" aria-labelledby="tools-mock-confirm-title" aria-describedby="tools-mock-confirm-text" hidden>` +
        `<p class="tools-confirm-title" id="tools-mock-confirm-title">Remove every mock?</p>` +
        `<p class="tools-confirm-text" id="tools-mock-confirm-text">This removes all mocks on this Mac, including those other sessions use.</p>` +
        `<div class="tools-actions"><button type="button" class="btn" id="tools-mock-cancel">Cancel</button>` +
        `<button type="button" class="btn danger" id="tools-mock-clear-go" data-mutate>Remove all</button></div>` +
      `</div>` +
    `</div>` +
    `<form class="tools-group" id="tools-mock-form" novalidate aria-labelledby="tools-mock-form-title">` +
      `<h3 id="tools-mock-form-title">New mock</h3>` +
      `<input id="tools-mock-glob" aria-label="URL glob" title="URL pattern to match" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="URL pattern · */api/items*">` +
      `<div class="tools-pair tools-mock-match">` +
        `<select id="tools-mock-method" aria-label="Method" title="HTTP method">${methodOptions("Any method")}</select>` +
        `<div class="tools-inline-field"><label for="tools-mock-status">Status</label><input id="tools-mock-status" type="text" inputmode="numeric" autocomplete="off" value="200"></div>` +
      `</div>` +
      `<label class="tools-label" for="tools-mock-body">Response body</label>` +
      `<textarea id="tools-mock-body" rows="3" spellcheck="false" title="Response body, up to 32 KB"></textarea>` +
      `<details class="tools-options" id="tools-mock-options"><summary>Options</summary>` +
      `<label class="tools-label" for="tools-mock-host">Host</label>` +
      `<input id="tools-mock-host" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Any host">` +
      `<label class="tools-check"><input id="tools-mock-ignore-query" type="checkbox"> Ignore query string</label>` +
      `<label class="tools-label" for="tools-mock-headers">Headers</label>` +
      `<textarea id="tools-mock-headers" rows="2" spellcheck="false" title="One Name: value header per line" placeholder="Content-Type: application/json"></textarea>` +
      `<label class="tools-label" for="tools-mock-note">Note</label>` +
      `<input id="tools-mock-note" type="text" autocomplete="off" placeholder="Optional"></details>` +
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
      `<div class="tools-log-filters">` +
        `<div class="tools-field"><input id="tools-log-package" aria-label="${ios ? "Process or bundle" : "App package"}" title="${ios ? "Filter by process or bundle" : "Filter by app package"}" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="All apps">` +
        `<button type="button" class="btn" id="tools-log-apply">Apply</button></div>` +
        `<div class="tools-log-search"><select id="tools-log-level" aria-label="Level" title="Minimum log level">` +
          levels.map(([value, label]) => `<option value="${value}">${label}</option>`).join("") + `</select>` +
        `<input id="tools-log-filter" aria-label="Filter" title="Filter log text" type="search" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Filter logs"></div>` +
      `</div>` +
      `<div class="tools-logbar"><p class="tools-log-state" id="tools-log-state" aria-live="polite">Stopped</p>` +
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
:root{--t-danger:var(--danger);--t-sheet-shadow:var(--sheet-shadow)}
body.tools-open,body.tools-open.no-inspector{grid-template-columns:minmax(0,1fr) clamp(440px,34vw,520px);grid-template-areas:"bar bar" "stage side"}
body.tools-open aside.side{display:none}
.tools{grid-area:side;display:flex;flex-direction:column;min-width:0;min-height:0;border-left:1px solid var(--line);background:var(--surface)}
.tools-head{display:flex;align-items:center;gap:8px;padding:10px 10px 10px 14px;border-bottom:1px solid var(--line)}
.tools-tabs{display:flex;flex:1;min-width:0;gap:2px;padding:3px;border-radius:10px;background:var(--fill);overflow-x:auto;scrollbar-width:none}
.tools-tabs::-webkit-scrollbar{display:none}
.tools-tab{flex:1 0 auto;height:32px;padding:0 10px;border:0;border-radius:7px;background:transparent;color:var(--text-2);font-weight:500;white-space:nowrap}
.tools-tab[aria-selected=true]{background:var(--tab-selected-bg);color:var(--text);box-shadow:var(--tab-shadow)}
.tools-tab:not([aria-selected=true]):active{transform:none;background:var(--fill-2)}
.tools-icon{flex:none}
.tools-body{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;background:var(--surface);scrollbar-width:thin}
.tools-panel{padding:2px 0 16px;outline:none}
.tools-panel:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
.tools-group{padding:12px 14px;margin:0;border:0;border-bottom:1px solid var(--line);border-radius:0;background:var(--surface);min-width:0}
.tools-group h3{margin:0 0 8px;font-size:12px;font-weight:600;color:var(--text);letter-spacing:-.01em}
.tools-label{display:block;margin:8px 0 4px;font-size:12px;color:var(--text-2)}
.tools-group>.tools-label:first-child,.tools-pair .tools-label,.tools-filters .tools-label{margin-top:0}
.tools input:not([type=checkbox]),.tools select,.tools textarea{display:block;width:100%;min-width:0;height:32px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--bg);color:var(--text);font:inherit}
.tools select{padding-right:6px}
.tools textarea{height:auto;min-height:64px;padding:8px 10px;font:12px/1.45 var(--mono);resize:vertical}
.tools input::placeholder,.tools textarea::placeholder{color:var(--text-2);opacity:1}
.tools-form-row{display:grid;grid-template-columns:76px minmax(0,1fr);align-items:center;gap:8px;margin-top:8px}
.tools-form-row:first-child{margin-top:0}
.tools-form-row .tools-label{margin:0}
.tools-field{display:flex;gap:6px;min-width:0}
.tools-field input{flex:1}
.tools-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.tools-group>.tools-pair:first-of-type{margin-top:0}
.tools-section-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.tools-section-bar h3{margin:0}
.tools-section-bar .tools-actions{margin:0;gap:4px;flex-wrap:nowrap}
.tools-net-metrics{display:flex;gap:8px 16px;flex-wrap:wrap;margin:0;font-size:12px}
.tools-net-metrics>div{display:flex;gap:5px}
.tools-net-metrics dt{color:var(--text-2)}
.tools-net-metrics dd{margin:0;font-variant-numeric:tabular-nums}
.tools .tools-scope-note{margin:10px 14px 0}
.tools-options{margin-top:8px}
.tools-options summary{cursor:pointer;font-size:12px;color:var(--text-2);padding:4px 0}
.tools-inline-field{display:flex;align-items:center;gap:6px;min-width:0}
.tools-inline-field label{font-size:12px;color:var(--text-2)}
#tools-mock-form .tools-mock-match{margin-top:8px;grid-template-columns:repeat(2,minmax(0,1fr))}
.tools-log-filters{display:grid;gap:6px}
.tools-log-search{display:grid;grid-template-columns:minmax(100px,1fr) minmax(0,2fr);gap:6px}
.tools-filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:10px}
.tools-filter-wide{grid-column:1/-1}
.tools-fingerprint-setup{width:100%;margin-top:8px}.tools-fingerprint-setup summary{cursor:pointer;color:var(--text-2);padding:8px 0}.tools-fingerprint-setup .btn{margin-top:10px}
.tools-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px}
.tools-actions:first-child{margin-top:0}
/* Simulate is a compact preference list, not a stack of cards. */
#tools-panel-simulate{background:var(--surface);min-height:100%}
#tools-panel-simulate .tools-sim-setting{margin:0;padding:10px 14px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:none}
.tools-sim-row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:32px}
.tools-sim-row h3{margin:0;flex-shrink:0;font-size:12px;font-weight:500}
.tools-sim-controls{display:flex;align-items:center;justify-content:flex-end;gap:4px;min-width:0}
.tools-sim-controls .btn{padding:0 9px;font-size:12px;white-space:nowrap}
.tools-sim-controls input:not([type=checkbox]){width:58px;padding:0 6px;font-variant-numeric:tabular-nums}
.tools-sim-controls select{width:112px;font-size:12px;padding:0 4px}
.tools-sim-status{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:3px}
.tools-sim-status .tools-note{margin:0;font-size:12px}
.tools-sim-status .icon{width:24px;height:24px}
.tools-sim-status svg{width:14px;height:14px}
.tools-sim-details{margin:4px 0 0}
.tools-sim-details summary{cursor:pointer;font-size:12px;color:var(--text-2);padding:3px 0}
.tools-sim-details[open]{padding-bottom:4px}
.tools-sim-details .tools-sim-controls{justify-content:flex-start;margin-top:8px}
.tools-fingerprint-setup>.btn{margin-top:8px}
@media (max-width:400px){.tools-sim-row{gap:6px}.tools-sim-controls{gap:3px}.tools-sim-controls .btn{padding:0 5px;font-size:11px}.tools-sim-controls select{width:88px}.tools-sim-controls input:not([type=checkbox]){width:48px}}
.tools .btn.small{height:28px;padding:0 10px;font-size:12px}
.tools .btn.danger{background:var(--t-danger);border-color:transparent;color:#fff}
.tools-hint,.tools-note,.tools-empty{margin:8px 0 0;color:var(--text-2);font-size:12px;line-height:1.5}
.tools-empty{margin-top:4px;padding:4px 0;border:0;text-align:left}
.tools-check{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:12.5px}
.tools-check input{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.tools-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tools-chip{height:28px;padding:0 11px;border:0;border-radius:999px;background:var(--fill);color:var(--text);font-size:12px;font-weight:500}
.tools-readout{margin:6px 0 0;font-size:12px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
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
.tools-group-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px}
.tools-group-head h4{margin:0;font-size:12px;font-weight:500;color:var(--text-2)}
.tools-location-coordinates{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;align-items:end;gap:6px}
.tools-location-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.tools-location-heading h3{margin:0}
.tools-location-status{margin:0 0 10px;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
.tools-location-form-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:8px}
#tools-location-save-status:empty{display:none}
.tools-location-add[aria-expanded=true] svg{transform:rotate(45deg)}
#tools-location-form{margin:4px 0 8px;padding:10px;background:var(--fill);border-radius:6px}
.tools-locations{margin-top:4px}
.tools-location-entry{position:relative;display:flex;align-items:center;border-bottom:1px solid var(--line)}
.tools-location-entry>.tools-location-row{flex:1;min-width:0;border-bottom:0}
.tools-location-entry:has(>.tools-location-row[aria-pressed="true"]){background:color-mix(in srgb,var(--accent) 8%,transparent)}
.tools-location-entry>.tools-location-row[aria-pressed="true"]{background:transparent}
.tools-location-row[data-loc-preset]{padding-right:32px}
.tools-location-more{flex:none;width:28px;height:28px;font-size:20px;line-height:1}
.tools-location-menu{position:absolute;right:0;top:30px;z-index:5;min-width:128px;padding:4px;border:1px solid var(--line);border-radius:8px;background:var(--surface);box-shadow:0 6px 20px #0002}
.tools-location-menu button{display:block;width:100%;text-align:left;border:0;background:transparent;padding:6px 10px;font-size:12px}
.tools-location-menu button:hover{background:var(--fill)}
.tools-location-menu button:last-child{color:var(--danger)}
#tools-location-edit-coordinates{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:6px;margin-top:6px}
#tools-location-edit-coordinates[hidden]{display:none}
#tools-location-feedback:empty{display:none}
.tools-location-row{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;min-height:32px;padding:6px 4px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;text-align:left;color:var(--text)}
.tools-location-row::before{content:"";flex:0 0 12px;width:12px;color:var(--accent);font-size:12px;text-align:center}
.tools-location-row[aria-pressed="true"]::before{content:"✓"}
.tools-location-row span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-location-row small{font-size:12px;color:var(--text-2);font-variant-numeric:tabular-nums;text-align:right;overflow-wrap:anywhere}
.tools-location-row[aria-pressed="true"]{background:color-mix(in srgb,var(--accent) 8%,transparent);color:var(--accent)}
.tools-location-row:not(:disabled):not([aria-pressed="true"]):hover{background:color-mix(in srgb,var(--text) 3%,transparent)}
.tools-deeplink-row{display:block;width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px}
.tools-list{list-style:none;margin:0;padding:0}
.tools-item{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:6px 10px;padding:9px 0;border-top:1px solid var(--line)}
.tools-item:first-child{border-top:0;padding-top:0}
.tools-item-name{min-width:0}
.tools-item-name b{display:block;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-item-name small{display:block;color:var(--text-2);font:11.5px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-item-actions{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px}
#tools-mocks .tools-item{grid-template-columns:minmax(0,1fr) auto auto;gap:6px;padding:8px 0}
#tools-mocks .tools-item-actions{grid-column:auto;gap:4px;flex-wrap:nowrap}
#tools-mocks .tools-item-name small{font-family:inherit;font-size:12px;line-height:1.4}
#tools-mocks .tools-mock-icon{width:28px;padding:0;font-size:18px;line-height:1}
#tools-mocks [data-mock-action=remove]{color:var(--t-danger)}
#tools-mocks .tools-mock-icon::before{content:"";display:block;width:14px;height:14px;margin:auto;background:currentColor;mask:var(--mock-icon) center/contain no-repeat;-webkit-mask:var(--mock-icon) center/contain no-repeat}
#tools-mocks .tools-mock-edit{--mock-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.8%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m15%205%204%204M4%2020l4-1L20%207a2.8%202.8%200%200%200-4-4L4%2015z%22%2F%3E%3C%2Fsvg%3E")}
#tools-mocks .tools-mock-remove{--mock-icon:url("data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22black%22%20stroke-width%3D%221.8%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M4%207h16M9%207V4h6v3M6%207l1%2013h10l1-13M10%2010v7M14%2010v7%22%2F%3E%3C%2Fsvg%3E")}

.tools-badge{padding:2px 8px;border-radius:999px;background:var(--fill);color:var(--text-2);font-size:11.5px;font-weight:500;white-space:nowrap}
.tools-badge[data-state=on]{color:var(--live)}
.tools-badge[data-state=off]{color:var(--t-danger)}
.tools-switch[aria-pressed=true]{color:var(--live)}
.tools-dl{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 10px}
.tools-dl dt{color:var(--text-2)}
.tools-dl dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tools-confirm{margin-top:12px;padding:12px;border-radius:12px;border:1px solid var(--line-2);background:var(--raised);box-shadow:var(--confirm-shadow)}
.tools-confirm-title{margin:0;font-weight:600}
.tools-confirm-text{margin:4px 0 0;color:var(--text-2);font-size:12.5px}
#tools-panel-network:not([hidden]){display:flex;flex-direction:column;height:100%;min-height:360px;padding:0;box-sizing:border-box}
#tools-net-body:not([hidden]){display:flex;flex-direction:column;flex:1;min-height:0}
#tools-net-body>.tools-group{flex:none}
#tools-net-body>.tools-requests-group{display:flex;flex-direction:column;flex:1;min-height:120px;padding-bottom:0;border-bottom:0}
.tools-requests-group>h3,.tools-requests-group>.tools-filters,.tools-requests-group>.tools-empty{flex:none}
.tools-reqs{display:grid;align-content:start;gap:1px;flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;margin:0 -8px;padding-bottom:8px;scrollbar-width:thin}
.tools-reqs:empty{display:none}
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
.tools-detail{position:fixed;inset:0 0 0 auto;width:min(680px,100vw);height:100dvh;max-width:100vw;max-height:none;margin:0;padding:0;border:0;border-left:1px solid var(--line);background:var(--surface);color:var(--text);box-shadow:var(--detail-shadow)}
.tools-detail::backdrop{background:var(--detail-backdrop)}
.tools-detail-sheet{display:flex;flex-direction:column;height:100%;min-height:0}
.tools-detail-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line)}
.tools-detail-content{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:12px;padding-bottom:max(12px,env(safe-area-inset-bottom))}
.tools-body-head{display:flex;align-items:center;gap:6px;margin:14px 0 6px}
.tools-detail .tools-body-head h4{flex:1;margin:0}
.tools-copy-status{margin:4px 0 0;font-size:12px;color:var(--text-2)}
.tools-copy-status:empty{display:none}
.tools-detail .tools-pre{max-height:50vh}
.tools-detail-head h3{font-size:14px;font-weight:600;flex:1;min-width:0;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
/* Narrow windows use an overlay without increasing desktop control sizes. */
@media (max-width:760px){
  .actions .icon.tools-toggle{display:grid}
  .tools{position:fixed;left:auto;right:8px;bottom:8px;top:60px;z-index:6;width:var(--canvas-panel-width);border:1px solid var(--line-2);border-radius:12px;
    box-shadow:var(--t-sheet-shadow);padding-bottom:env(safe-area-inset-bottom)}
  body.tools-open{overflow:hidden}
  .tools-head{padding:8px 8px 8px 12px}
  .tools-filters{grid-template-columns:repeat(2,minmax(0,1fr))}
  .tools-req{grid-template-columns:48px 34px minmax(0,1fr)}
  .tools-req-meta{display:none}
  .tools-log{height:52vh}
}
@media (max-width:360px){.tools-pair{grid-template-columns:minmax(0,1fr)}.tools-tab{padding:0 7px}}
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
const state={open:false,tab:"app",context:null,contextBusy:false,lockTimer:null,lastLocation:null,locationSeq:0,locationBusy:false,savedLocations:null,deeplinks:{busy:false,cache:new Map()},perms:{app:null,seq:0},
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
function syncLock(){const ok=allowed();if(lock){lock.hidden=ok;if(!ok)text("tools-lock",lockReason())}for(const control of drawer.querySelectorAll("[data-mutate]")){const off=!ok||control.getAttribute("data-busy")==="1"||(state.locationBusy&&control.getAttribute("data-location-action")!==null);if(control.disabled!==off)control.disabled=off}}
// A timed-out change may still have been applied and is never retried: say the outcome is unknown and
// re-read what the change may have touched (permissions.list, location.get where the device can report
// it, network.status, mocks.list) so the panel is not left stale. Each re-read resolves to its own
// error or null, so the verdict never depends on what another request wrote into the message box.
function isTimeout(error){return Boolean(error)&&(error.code==="timeout"||error.status===504)}
const LOCATION_OPS=["location.set","location.clear"];
function locationReadable(){return Boolean($("tools-loc-get"))&&(!state.context||state.context.location_readable===true)}
function rereadFor(panel,op){if(op==="app.open_url")return null;if(LOCATION_OPS.includes(op))return locationReadable()?readLocation:null;if(panel==="app")return state.perms.app?()=>loadPermissions(state.perms.app):null;if(panel==="network")return refreshStatus;if(panel==="mocks")return loadMocks;return null}
async function unknownOutcome(panel,op){const reread=rereadFor(panel,op),location=LOCATION_OPS.includes(op);let hint;if(reread){message(panel);let failed=null;try{failed=await reread()}catch(error){failed=error||localError("the re-read failed")}hint=failed?"Re-reading the current state also failed: "+errorText(failed):location?"The location was read back; check it before trying again.":"The current state was re-read; check it before trying again."}else if(location){text("tools-loc-readout","Unknown: the last location change did not answer.");hint="This device cannot report its location, so it could not be re-read; check the device before trying again."}else hint="Nothing here can be re-read, so check the device before trying again.";message(panel,"error","No answer within 60 s, so the outcome is unknown: the change may or may not have been applied (timeout).",hint)}
async function mutate(panel,control,op,payload){if(!allowed()){syncLock();return null}message(panel);if(control){control.setAttribute("data-busy","1");control.setAttribute("aria-busy","true")}syncLock();try{return await call(op,payload)}catch(error){if(isTimeout(error))await unknownOutcome(panel,op);else showError(panel,error);return null}finally{if(control){control.removeAttribute("data-busy");control.removeAttribute("aria-busy")}syncLock()}}

// Drawer and tabs (automatic activation; arrows, Home and End move between tabs).
function selectTab(name,focus){const index=TABS.indexOf(name);if(index<0)return;if(name!=="network")closeRequest(false);state.tab=name;tabs.forEach((tab,i)=>{const on=i===index;tab.setAttribute("aria-selected",String(on));tab.setAttribute("tabindex",on?"0":"-1");panels[i].hidden=!on});if(focus)tabs[index].focus();onTabShown(name);syncActivity()}
function onTabShown(name){if(!state.open)return;if(name==="network"&&!state.net.checked)refreshStatus();if(name==="mocks")loadMocks();if(name==="simulate"&&state.context)refreshBiometry();if(name==="app"){renderSavedLocations();if(state.context)refreshLocation()}}
function setOpen(open,returnFocus){if(state.open===open)return;state.open=open;drawer.hidden=!open;document.body.classList.toggle("tools-open",open);toggle.setAttribute("aria-pressed",String(open));toggle.title=open?"Hide tools":"Show tools";if(inspectorToggle)inspectorToggle.setAttribute("aria-pressed",String(!open&&!document.body.classList.contains("no-inspector")));if(open){syncLock();state.lockTimer=setInterval(syncLock,500);loadContext();const tab=tabs[TABS.indexOf(state.tab)];if(tab)tab.focus();onTabShown(state.tab)}else{clearInterval(state.lockTimer);state.lockTimer=null;state.net.checked=false;state.net.ready=false;closeRequest(false);hideConfirms();deactivateFeed();if(returnFocus)toggle.focus()}syncActivity()}
function syncActivity(){syncNetworkPolling();syncLogPolling()}
toggle.addEventListener("click",()=>setOpen(!state.open,false));
$("tools-close").addEventListener("click",()=>setOpen(false,true));
if(inspectorToggle)inspectorToggle.addEventListener("click",()=>{if(!state.open)return;setOpen(false,false);document.body.classList.remove("no-inspector");inspectorToggle.setAttribute("aria-pressed","true");inspectorToggle.title="Hide inspector"});
tabs.forEach((tab,i)=>tab.addEventListener("click",()=>selectTab(TABS[i],false)));
$("tools-tablist").addEventListener("keydown",event=>{const index=TABS.indexOf(state.tab),moves={ArrowRight:index+1,ArrowLeft:index-1,Home:0,End:TABS.length-1},next=moves[event.key];if(next===undefined)return;event.preventDefault();selectTab(TABS[(next+TABS.length)%TABS.length],true)});
drawer.addEventListener("keydown",event=>{if(event.key!=="Escape")return;event.preventDefault();if(!$("tools-req-detail").hidden){event.stopPropagation();closeRequest(true);return}if(hideConfirms())return;setOpen(false,true)});

// Context: fetched when the drawer first opens; it fills the app id and narrows the controls.
async function loadContext(){if(state.context||state.contextBusy)return;state.contextBusy=true;try{const context=await call("context");state.context=context;applyContext(context)}catch(error){text("tools-app-source","The device context is not available.");showError("app",error)}finally{state.contextBusy=false}}
function applyContext(context){const appField=$("tools-app-id"),session=context.session||null;if(appField&&!appField.value&&context.app_id)appField.value=String(context.app_id);text("tools-app-source",session&&session.app_id?"From the Autonom session on this device. Edit it to use another app.":context.app_id?"The best-known app on this device. Edit it to use another app.":"No app is known for this device; enter an app id.");if(appField)appField.title=$("tools-app-source").textContent;const simulate=context.simulate&&typeof context.simulate==="object"?context.simulate:null;if(simulate){for(const group of drawer.querySelectorAll("[data-sim-group]"))group.hidden=!Array.isArray(simulate[group.getAttribute("data-sim-group")]);for(const control of drawer.querySelectorAll("[data-sim]")){const actions=simulate[control.getAttribute("data-sim")];control.hidden=!(Array.isArray(actions)&&actions.includes(control.getAttribute("data-action")))}}const clear=$("tools-loc-clear");if(clear)clear.hidden=context.location_clearable!==true;const readBack=$("tools-loc-get");if(readBack)readBack.hidden=context.location_readable!==true;if(context.network_available===false)setNoSession(true,null);renderDeeplinks();refreshLocation();if(state.tab==="simulate")refreshBiometry();if(appField&&appField.value)loadPermissions()}

// App: permissions.
function appId(){const field=$("tools-app-id");return field?field.value.trim():""}
function shortName(name){const value=String(name||"");return value.startsWith("android.permission.")?value.slice(19):value}
function clearPermissions(value){const perms=state.perms;perms.seq+=1;perms.app=null;$("tools-perms").replaceChildren();text("tools-perms-empty",value);$("tools-perms-empty").hidden=false}
async function loadPermissions(target){const id=target||appId(),perms=state.perms;if(!id){clearPermissions("Enter an app id to list its permissions.");return null}perms.seq+=1;const seq=perms.seq;try{const result=await call("permissions.list",{app_id:id});if(seq!==perms.seq)return null;perms.app=id;renderPermissions(result);return null}catch(error){if(seq!==perms.seq)return null;showError("app",error);return error}}
function renderPermissions(result){const list=$("tools-perms"),empty=$("tools-perms-empty"),items=Array.isArray(result.permissions)?result.permissions:[],readable=result.readable!==false;list.replaceChildren();empty.hidden=items.length>0;if(!items.length)text("tools-perms-empty",PLATFORM==="ios"?"No privacy services were reported.":"This app requests no runtime permissions.");for(const item of items){const name=String(item.name||""),label=item.alias?String(item.alias):shortName(name),row=el("li","tools-item"),title=el("div","tools-item-name");title.appendChild(el("b",null,label));if(label!==name)title.appendChild(el("small",null,name));row.appendChild(title);if(readable){const badge=el("span","tools-badge",item.granted===true?"Granted":item.granted===false?"Denied":"Unknown");badge.setAttribute("data-state",item.granted===true?"on":item.granted===false?"off":"unknown");row.appendChild(badge)}const actions=el("div","tools-item-actions");for(const action of ["grant","revoke","reset"])actions.appendChild(makeButton(capital(action),"btn small",{"data-mutate":"","data-perm":name,"data-perm-label":label,"data-perm-action":action,"aria-label":capital(action)+" "+label}));row.appendChild(actions);list.appendChild(row)}syncLock()}
$("tools-perms-load").addEventListener("click",()=>{message("app");loadPermissions()});
$("tools-app-id").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();message("app");loadPermissions()}});
$("tools-app-id").addEventListener("input",()=>{$("tools-deeplink").value="";renderDeeplinks();const perms=state.perms;if(perms.app!==null&&appId()!==perms.app)clearPermissions("Press Load to list this app's permissions.")});
$("tools-perms").addEventListener("click",async event=>{const control=event.target&&event.target.closest?event.target.closest("[data-perm-action]"):null;if(!control||control.disabled)return;const id=state.perms.app,action=control.getAttribute("data-perm-action"),label=control.getAttribute("data-perm-label");if(!id){showError("app",localError("Enter an app id first.","invalid_value"));return}const result=await mutate("app",control,"permissions.set",{app_id:id,service:control.getAttribute("data-perm"),action});if(!result)return;showOk("app",capital(action)+" "+label+": done"+(PLATFORM==="ios"?" (iOS does not report the new state).":"."));if(PLATFORM==="android"&&state.perms.app===id)loadPermissions(id)});

// App: deeplinks. History is local to this browser, bounded and keyed by platform + app.
function deeplinkKey(id){return "autonom.canvas.deeplinks.v1:"+PLATFORM+":"+encodeURIComponent(id)}
function validDeeplink(value){return typeof value==="string"&&value.length<=2048&&/^[a-zA-Z][a-zA-Z0-9+.\\-]*:/.test(value)&&!/[\\s\\x00-\\x1f\\x7f]/.test(value)}
function recentDeeplinks(id){if(!id)return[];const key=deeplinkKey(id);if(state.deeplinks.cache.has(key))return state.deeplinks.cache.get(key);let items=[];try{const saved=JSON.parse(localStorage.getItem(key)||"[]");if(Array.isArray(saved))items=[...new Set(saved.filter(validDeeplink))].slice(0,5)}catch{}state.deeplinks.cache.set(key,items);return items}
function saveDeeplinks(id,items){const key=deeplinkKey(id);state.deeplinks.cache.set(key,items);try{localStorage.setItem(key,JSON.stringify(items))}catch{}}
function renderDeeplinks(){const id=appId(),list=$("tools-deeplinks"),items=recentDeeplinks(id);list.replaceChildren();for(const link of items){const row=el("li"),button=makeButton(link,"btn tools-deeplink-row",{"data-mutate":"","aria-label":"Open deeplink "+link,title:link});if(state.deeplinks.busy)button.setAttribute("data-busy","1");button.addEventListener("click",()=>{$("tools-deeplink").value=link;openDeeplink(link)});row.appendChild(button);list.appendChild(row)}$("tools-deeplinks-empty").hidden=items.length>0;$("tools-deeplinks-history").hidden=items.length===0;text("tools-deeplinks-empty",id?"No recent deeplinks for this app.":"Enter an App id to see its recent deeplinks.");$("tools-deeplinks-clear").disabled=!items.length;syncLock()}
async function openDeeplink(value){if(state.deeplinks.busy)return;const id=appId(),link=value.trim();if(!id){showError("app",localError("Enter an App id first.","invalid_value"));return}if(!validDeeplink(link)){showError("app",localError("Enter a URL with a scheme, up to 2048 characters, without spaces.","invalid_value"));return}state.deeplinks.busy=true;renderDeeplinks();try{const payload={url:link};if(PLATFORM==="android")payload.app_id=id;const result=await mutate("app",$("tools-deeplink-open"),"app.open_url",payload);if(!result)return;if(!result.opened||(result.warnings&&result.warnings.length)){if(appId()===id)showError("app",localError(result.warnings&&result.warnings[0]&&result.warnings[0].error||"The URL was not opened."));return}saveDeeplinks(id,[link,...recentDeeplinks(id).filter(item=>item!==link)].slice(0,5));if(appId()===id)showOk("app","Opened deeplink"+(result.handled_by&&result.handled_by!=="unknown"?" in "+result.handled_by:"."))}finally{state.deeplinks.busy=false;renderDeeplinks()}}
$("tools-deeplink-open").addEventListener("click",()=>openDeeplink($("tools-deeplink").value));
$("tools-deeplink").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();openDeeplink($("tools-deeplink").value)}});
$("tools-deeplinks-clear").addEventListener("click",()=>{const id=appId();if(id)saveDeeplinks(id,[]);renderDeeplinks()});

// App: location.
function formatCoordinate(value){return Number(value).toFixed(4)}
function coordinates(latId="tools-lat",lonId="tools-lon"){const latText=$(latId).value.trim(),lonText=$(lonId).value.trim(),latitude=Number(latText),longitude=Number(lonText);if(!latText||!Number.isFinite(latitude)||latitude<-90||latitude>90)throw localError("Latitude must be a number from -90 to 90.","invalid_coordinates");if(!lonText||!Number.isFinite(longitude)||longitude<-180||longitude>180)throw localError("Longitude must be a number from -180 to 180.","invalid_coordinates");return{latitude,longitude}}
function showLocation(prefix,latitude,longitude){state.lastLocation={latitude,longitude};text("tools-loc-readout",prefix+formatCoordinate(latitude)+", "+formatCoordinate(longitude));renderSavedLocations()}
async function setLocation(control){if(state.locationBusy)return;let point;try{point=coordinates()}catch(error){showError("app",error);return}state.locationSeq+=1;state.locationBusy=true;syncLock();try{const result=await mutate("app",control,"location.set",point);if(!result)return;state.locationSeq+=1;state.selectedLocation=control.getAttribute("data-location-key");state.lastLocation=point;if(PLATFORM==="ios")showLocation("Last set here: ",point.latitude,point.longitude);else showLocation("Requested: ",point.latitude,point.longitude);showOk("app","Location set.")}finally{state.locationBusy=false;syncLock()}}
$("tools-loc-set").addEventListener("click",event=>setLocation(event.currentTarget||$("tools-loc-set")));
for(const preset of drawer.querySelectorAll("[data-loc-preset]"))preset.addEventListener("click",()=>{$("tools-lat").value=preset.getAttribute("data-lat");$("tools-lon").value=preset.getAttribute("data-lon");if(!locationEditing)$("tools-location-name").value=preset.getAttribute("data-loc-preset");setLocation(preset)});
const readBack=$("tools-loc-get");
async function readLocation(){const seq=++state.locationSeq;try{const result=await call("location.get");if(seq===state.locationSeq){renderReadBack(result);const point=state.lastLocation;if(point&&!$("tools-lat").value&&!$("tools-lon").value){$("tools-lat").value=String(point.latitude);$("tools-lon").value=String(point.longitude)}}return null}catch(error){return seq===state.locationSeq?error:null}}
async function refreshLocation(){if(state.locationBusy||!state.context||state.context.location_readable!==true)return;const error=await readLocation();if(error)text("tools-loc-readout","Current location unavailable: "+errorText(error))}
if(readBack)readBack.addEventListener("click",async()=>{message("app");const error=await readLocation();if(error)showError("app",error)});
function renderReadBack(result){const requested=result.requested&&typeof result.requested==="object"?result.requested:null;if(requested&&result.delivered===false){showLocation("Pending: ",requested.latitude,requested.longitude);return}if(result.latitude===null||result.latitude===undefined){state.lastLocation=null;renderSavedLocations();text("tools-loc-readout","No last known location on the device.");return}showLocation(requested?"Delivered: ":"Current: ",result.latitude,result.longitude)}
const clearLocation=$("tools-loc-clear");
if(clearLocation)clearLocation.addEventListener("click",async()=>{if(state.locationBusy)return;state.locationBusy=true;state.locationSeq+=1;syncLock();try{const result=await mutate("app",clearLocation,"location.clear",{});if(!result)return;state.locationSeq+=1;state.lastLocation=null;renderSavedLocations();text("tools-loc-readout","No location set here.");showOk("app","Location cleared.")}finally{state.locationBusy=false;syncLock()}});

// Named locations are shared across apps in this browser; saving never changes the device.
const LOCATIONS_KEY="autonom.canvas.locations.v1";
function validSavedLocation(point){return point&&typeof point.name==="string"&&point.name.trim().length>0&&point.name.length<=80&&typeof point.latitude==="number"&&Number.isFinite(point.latitude)&&point.latitude>=-90&&point.latitude<=90&&typeof point.longitude==="number"&&Number.isFinite(point.longitude)&&point.longitude>=-180&&point.longitude<=180}
function savedLocations(){if(state.savedLocations!==null)return state.savedLocations;let items=[];try{const saved=JSON.parse(localStorage.getItem(LOCATIONS_KEY)||"[]");if(Array.isArray(saved))items=saved.filter(validSavedLocation).slice(0,50)}catch{}state.savedLocations=items;return items}
let locationEditing=null,locationMenu=null;
function closeLocationMenu(focus=false){if(!locationMenu)return;const {menu,button}=locationMenu;menu.hidden=true;button.setAttribute("aria-expanded","false");locationMenu=null;if(focus)button.focus()}
document.body.addEventListener("click",event=>{if(locationMenu&&!locationMenu.entry.contains(event.target))closeLocationMenu()});
$("tools-locations-custom").addEventListener("keydown",event=>{if(event.key==="Escape"&&locationMenu){event.preventDefault();event.stopPropagation();closeLocationMenu(true)}});
function persistLocations(items){state.savedLocations=items;try{localStorage.setItem(LOCATIONS_KEY,JSON.stringify(items));return true}catch{return false}}
function locationFeedback(value,stored){text("tools-location-feedback",value+(stored?"":" For this page only: browser storage is unavailable."))}
function editLocation(point){closeLocationMenu();locationEditing=point;$("tools-location-name").value=point.name;$("tools-location-edit-lat").value=String(point.latitude);$("tools-location-edit-lon").value=String(point.longitude);$("tools-location-edit-coordinates").hidden=false;showLocationForm(true);text("tools-location-save-status","")}
function deleteLocation(point){closeLocationMenu();const index=savedLocations().indexOf(point);if(index<0)return;const stored=persistLocations(savedLocations().filter(item=>item!==point));if(locationEditing===point)showLocationForm(false);renderSavedLocations();locationFeedback("Deleted "+point.name+". ",stored);const undo=makeButton("Undo","btn small");$("tools-location-feedback").appendChild(undo);undo.addEventListener("click",()=>{const items=[...savedLocations()];if(items.length>=50||items.some(item=>item.name.toLocaleLowerCase()===point.name.toLocaleLowerCase())){locationFeedback("Cannot restore: the name is already used or the list is full.",true);return}items.splice(Math.min(index,items.length),0,point);const restored=persistLocations(items);renderSavedLocations();locationFeedback("Restored "+point.name+".",restored);$("tools-location-add").focus()});undo.focus()}
function renderSavedLocations(){closeLocationMenu();const list=$("tools-locations-custom");list.replaceChildren();for(const point of savedLocations()){
 const entry=el("div","tools-location-entry"),button=makeButton("","tools-location-row",{"data-mutate":"","data-location-action":"","data-location-key":"saved:"+point.name,"data-lat":String(point.latitude),"data-lon":String(point.longitude),"aria-label":"Set location "+point.name});
 button.appendChild(el("span",null,point.name));button.appendChild(el("small",null,formatCoordinate(point.latitude)+", "+formatCoordinate(point.longitude)));button.addEventListener("click",()=>{$("tools-lat").value=String(point.latitude);$("tools-lon").value=String(point.longitude);if(!locationEditing)$("tools-location-name").value=point.name;setLocation(button)});entry.appendChild(button);
 const more=makeButton("⋯","icon tools-location-more",{"aria-label":"Options for "+point.name,"aria-expanded":"false"}),menu=el("div","tools-location-menu");menu.hidden=true;
 const edit=makeButton("Edit","btn",{"data-location-edit":""}),remove=makeButton("Delete","btn",{"data-location-delete":""});menu.appendChild(edit);menu.appendChild(remove);entry.appendChild(more);entry.appendChild(menu);list.appendChild(entry);
 more.addEventListener("click",()=>{const open=menu.hidden;closeLocationMenu();if(open){menu.hidden=false;more.setAttribute("aria-expanded","true");locationMenu={menu,button:more,entry};edit.focus()}});edit.addEventListener("click",()=>editLocation(point));remove.addEventListener("click",()=>deleteLocation(point));
 }const buttons=[...drawer.querySelectorAll("[data-lat]")],point=state.lastLocation;
 const matches=buttons.filter(button=>point&&Math.abs(Number(button.getAttribute("data-lat"))-point.latitude)<0.000001&&Math.abs(Number(button.getAttribute("data-lon"))-point.longitude)<0.000001);
 // Prefer the successfully applied row; on initial read-back prefer the first saved match.
 const selected=matches.find(button=>button.getAttribute("data-location-key")===state.selectedLocation)||matches[0];
 for(const button of buttons)button.setAttribute("aria-pressed",String(button===selected));syncLock()}
function showLocationForm(open){$("tools-location-form").hidden=!open;if(!open){locationEditing=null;$("tools-location-edit-coordinates").hidden=true}const button=$("tools-location-add");button.setAttribute("aria-expanded",String(open));button.setAttribute("aria-label",open?"Close location form":"Add saved location");button.title=open?"Close location form":"Add saved location";if(open)$("tools-location-name").focus();else button.focus()}
$("tools-location-add").addEventListener("click",()=>{const open=$("tools-location-form").hidden;if(open){locationEditing=null;$("tools-location-edit-coordinates").hidden=true;text("tools-location-save-status","")}showLocationForm(open)});
$("tools-location-edit-cancel").addEventListener("click",()=>showLocationForm(false));
$("tools-location-form").addEventListener("keydown",event=>{if(event.key==="Escape"){event.preventDefault();event.stopPropagation();showLocationForm(false)}});
$("tools-location-save").addEventListener("click",()=>{let point;const name=$("tools-location-name").value.trim();try{point=locationEditing?coordinates("tools-location-edit-lat","tools-location-edit-lon"):coordinates();if(!name||name.length>80)throw localError("Enter a location name (1–80 characters).","invalid_value")}catch(error){text("tools-location-save-status",errorText(error));return}
 const existing=savedLocations();let items;
 if(locationEditing){if(existing.some(item=>item!==locationEditing&&item.name.toLocaleLowerCase()===name.toLocaleLowerCase())){text("tools-location-save-status","A saved location already uses this name.");return}items=existing.map(item=>item===locationEditing?{name,...point}:item)}
 else{items=existing.filter(item=>item.name.toLocaleLowerCase()!==name.toLocaleLowerCase());if(items.length>=50){text("tools-location-save-status","Up to 50 saved locations. Use an existing name to update it.");return}items=[{name,...point},...items]}
 const editing=Boolean(locationEditing),stored=persistLocations(items);renderSavedLocations();if(editing){showLocationForm(false);locationFeedback("Saved "+name+".",stored)}else text("tools-location-save-status",stored?"Saved "+name+".":"Saved for this page only: browser storage is unavailable.")});

// Simulate: only the controls and actions the context lists for this platform.
let biometrySeq=0;
async function refreshBiometry(){if(!state.context||!state.context.simulate||!state.context.simulate.biometric)return;const seq=++biometrySeq;try{const result=await call("biometric.status");if(seq!==biometrySeq)return;const kind=result.biometry;const label=kind==="face"?"Face ID":kind==="touch"?"Touch ID":kind==="fingerprint"?"Fingerprint":"Biometrics";text("tools-biometry-title",label);text("tools-biometry-status",(result.enrolled===true?"Enrolled":result.enrolled===false?"Not enrolled":"Enrollment unavailable")+(Number.isInteger(result.count)&&result.count>0?" · "+result.count+" saved":"")+(result.operation==="idle"?" · No active prompt":result.operation==="enroll"?" · Enrollment waiting":result.operation==="authenticate"?" · Authentication active":""));}catch(error){if(seq===biometrySeq)text("tools-biometry-status",error.message||"Biometric status unavailable")}}
$("tools-biometry-refresh").addEventListener("click",refreshBiometry);
function verdict(result){if(result&&result.verified===true)return"confirmed on the device";if(result&&result.verification==="mismatch")return"sent, but the device read back another value";return"sent (the device cannot confirm it)"}
function simValues(control,action){if(control==="biometric"&&(action==="match"||action==="nonmatch")&&$("tools-finger-id")){const finger=Number($(action==="match"?"tools-finger-id":"tools-finger-failure-id").value);if(!Number.isInteger(finger)||finger<1||finger>2147483647)throw localError("Enter a whole fingerprint id from 1 to 2147483647.","invalid_value");if(action==="nonmatch"&&finger===Number($("tools-finger-id").value))throw localError("Failure must use a different, unenrolled fingerprint id.","invalid_value");return{finger_id:finger}}if(control==="push"){const pushApp=$("tools-push-app"),app=(pushApp&&pushApp.value.trim())||appId(),raw=$("tools-push-payload").value;if(!app)throw localError("Enter a bundle id here or on the App tab.","invalid_value");if(utf8(raw)>PUSH_LIMIT)throw localError("The payload is larger than 4 KB.","invalid_value");let payload;try{payload=JSON.parse(raw)}catch{throw localError("The payload is not valid JSON.","invalid_value")}if(!payload||typeof payload!=="object"||Array.isArray(payload))throw localError("The payload must be a JSON object.","invalid_value");return{app_id:app,payload}}if(control==="battery"&&action==="set"){const level=Number($("tools-battery-level").value);if(!Number.isInteger(level)||level<0||level>100)throw localError("The battery level must be a whole number from 0 to 100.","invalid_value");const values={level};const stateField=$("tools-battery-state");if(stateField)values.state=stateField.value;return values}return{}}
$("tools-panel-simulate").addEventListener("click",async event=>{const control=event.target&&event.target.closest?event.target.closest("[data-sim]"):null;if(!control||control.disabled)return;const name=control.getAttribute("data-sim"),action=control.getAttribute("data-action");let values;try{values=simValues(name,action)}catch(error){showError("simulate",error);return}const result=await mutate("simulate",control,"simulate",{control:name,action,values});if(result)showOk("simulate",result.message||capital(name)+" "+action+": "+verdict(result)+".");if(name==="biometric")refreshBiometry()});

// Network: status, consent before start, attach, detach, stop and a live request list.
function setNoSession(show,error){const box=$("tools-net-nosession"),body=$("tools-net-body");if(show)state.net.ready=false;if(!show){box.hidden=true;body.hidden=false;return}text("tools-net-nosession-text",error&&error.message?error.message:"Network capture needs an Autonom session on this device.");text("tools-net-nosession-hint",error&&error.hint?error.hint:"Start one with autonom session start for this device.");box.hidden=false;body.hidden=true}
function attachedText(value){return value===true?"Yes":value===false?"No":value==="unknown"?"Unknown":"—"}
function captureUnknown(){const value=state.net.status||{};return value.proxy&&value.proxy.running===true&&value.attached!==true&&value.attached!==false}
function renderStatus(result){state.net.statusAt=Date.now();state.net.status=result;const fixtureCount=result.session_mocks&&result.session_mocks.active||0;text("tools-net-fixtures",fixtureCount?"Session fixtures: "+fixtureCount+" active":"");$("tools-net-fixtures").hidden=!fixtureCount;const proxy=result.proxy||{},running=proxy.running===true,attached=result.attached;text("tools-net-proxy",running?"Running"+(proxy.port?" on port "+proxy.port:""):"Stopped");text("tools-net-attached",attachedText(attached));text("tools-net-flows",result.recent_flow_count===undefined||result.recent_flow_count===null?"—":String(result.recent_flow_count));$("tools-net-start").hidden=running&&attached===true;text("tools-net-start",captureUnknown()?"Refresh connection":running?"Connect and capture":"Start capture");$("tools-net-stop").hidden=!running}
async function refreshStatus(){const revision=(state.net.statusRevision||0)+1;state.net.statusRevision=revision;state.net.checked=true;let failed=null;try{const result=await call("network.status");if(revision!==state.net.statusRevision)return null;state.net.ready=true;setNoSession(false,null);renderStatus(result)}catch(error){failed=error;if(revision!==state.net.statusRevision)return failed;if(NO_SESSION.includes(error.code))setNoSession(true,error);else showError("network",error)}syncNetworkPolling();return failed}
function hideConfirms(){let closed=false;for(const id of ["tools-net-confirm","tools-mock-confirm"]){const box=$(id);if(box&&!box.hidden){box.hidden=true;closed=true}}state.net.pending=null;return closed}
function askConsent(){state.net.pending="network.capture";text("tools-net-confirm-go","Start capture");$("tools-net-confirm").hidden=false;syncLock();$("tools-net-cancel").focus()}
async function runNetwork(control,op,payload,done){const revision=state.net.statusRevision||0;const result=await mutate("network",control,op,payload);if(!result){if(revision===(state.net.statusRevision||0))refreshStatus();return}showOk("network",done);refreshStatus()}
$("tools-net-start").addEventListener("click",()=>{if(captureUnknown()){refreshStatus();return}askConsent()});
$("tools-net-cancel").addEventListener("click",()=>{hideConfirms();$("tools-net-start").focus()});
$("tools-net-confirm-go").addEventListener("click",async()=>{if(state.net.pending!=="network.capture")return;hideConfirms();const revision=state.net.statusRevision||0;const result=await mutate("network",$("tools-net-start"),"network.capture",{acknowledged:true});if(result){renderStatus(result);showOk("network",result.attached===true?"Proxy configured on the device. Requests appear below as the app sends them.":"Proxy started; device connection is not confirmed.")}if(result||revision===(state.net.statusRevision||0))refreshStatus()});
$("tools-net-stop").addEventListener("click",()=>runNetwork($("tools-net-stop"),"network.stop",{},"Capture stopped; the previous device proxy was restored."));
$("tools-net-refresh").addEventListener("click",()=>{message("network");refreshStatus()});
$("tools-net-recheck").addEventListener("click",()=>refreshStatus());
function requestFilters(){const filters={},host=$("tools-req-host").value.trim(),method=$("tools-req-method").value,status=$("tools-req-status").value.trim(),mocked=$("tools-req-mocked").value;if(host)filters.host=host;if(method)filters.method=method;if(status&&/^[1-5][0-9][0-9]$/.test(status))filters.status=Number(status);if(mocked==="true"||mocked==="false")filters.mocked=mocked==="true";return filters}
function syncNetworkPolling(){const net=state.net,want=state.open&&state.tab==="network"&&net.ready;if(want&&!net.timer){net.timer=setInterval(pollRequests,1000);pollRequests()}else if(!want&&net.timer){clearInterval(net.timer);net.timer=null}}
function resetRequests(){const net=state.net;net.generation+=1;net.cursor=null;closeRequest(false);net.requests.clear();$("tools-req-list").replaceChildren();$("tools-req-empty").hidden=false;$("tools-req-detail").hidden=true}
async function pollRequests(){const net=state.net;if(net.busy||!net.timer)return;if(Date.now()-(net.statusAt||0)>5000)refreshStatus();net.busy=true;const generation=net.generation;try{const payload=Object.assign({max:100},requestFilters());if(net.cursor)payload.since_id=net.cursor;const result=await call("network.requests",payload);if(generation!==net.generation)return;const warnings=Array.isArray(result.warnings)?result.warnings:[];if(net.cursor&&warnings.some(item=>item&&item.code==="since_id_not_found")){resetRequests();net.generation=generation}addRequests(Array.isArray(result.requests)?result.requests:[])}catch(error){if(NO_SESSION.includes(error.code)){setNoSession(true,error);syncNetworkPolling()}else showError("network",error)}finally{net.busy=false}}
function tone(status){const value=Number(status);if(value>=500)return"server";if(value>=400)return"client";if(value>=300)return"redirect";if(value>=200)return"ok";return"other"}
function requestRow(item){const row=el("div","tools-req-item");row.setAttribute("role","listitem");const control=makeButton("","tools-req",{"data-req":String(item.id)});control.appendChild(el("span","tools-req-method",item.method||"—"));const status=el("span","tools-req-status",item.status===undefined||item.status===null?"—":String(item.status));status.setAttribute("data-tone",tone(item.status));control.appendChild(status);control.appendChild(el("span","tools-req-url",(item.host||"")+(item.path||"")||item.url||""));control.appendChild(el("span","tools-req-meta",(item.mocked?"mocked · ":"")+(typeof item.duration_ms==="number"?item.duration_ms+" ms":"")));control.title=String(item.url||"");row.appendChild(control);return row}
function addRequests(items){const net=state.net,list=$("tools-req-list");if(!items.length)return;net.cursor=String(items[0].id);for(let i=items.length-1;i>=0;i--){const item=items[i];if(!item||item.id===undefined||net.requests.has(String(item.id)))continue;net.requests.set(String(item.id),item);list.insertBefore(requestRow(item),list.firstChild)}while(list.children.length>REQUEST_LIMIT){const last=list.lastChild,control=last&&last.firstChild;if(control)net.requests.delete(control.getAttribute("data-req"));list.removeChild(last)}$("tools-req-empty").hidden=list.children.length>0}
for(const id of ["tools-req-host","tools-req-status"])$(id).addEventListener("change",()=>{resetRequests();if(state.net.timer)pollRequests()});
for(const id of ["tools-req-method","tools-req-mocked"])$(id).addEventListener("change",()=>{resetRequests();if(state.net.timer)pollRequests()});
$("tools-req-list").addEventListener("click",event=>{const control=event.target&&event.target.closest?event.target.closest("[data-req]"):null;if(control)showRequest(control.getAttribute("data-req"))});
function closeRequest(returnFocus){const net=state.net,id=net.selected,detail=$("tools-req-detail");net.selected=null;net.detailRevision=(net.detailRevision||0)+1;if(!detail.hidden){detail.close();detail.hidden=true}for(const control of $("tools-req-list").querySelectorAll("[data-req]")){control.removeAttribute("aria-current");if(returnFocus&&control.getAttribute("data-req")===id)control.focus()}}
async function showRequest(id){const net=state.net,revision=(net.detailRevision||0)+1;net.detailRevision=revision;net.selected=id;for(const control of $("tools-req-list").querySelectorAll("[data-req]")){if(control.getAttribute("data-req")===id)control.setAttribute("aria-current","true");else control.removeAttribute("aria-current")}const detail=$("tools-req-detail");text("tools-req-detail-title","Request");$("tools-req-detail-body").replaceChildren(el("p","tools-note","Loading request…"));$("tools-req-mock").disabled=true;if(detail.hidden){detail.hidden=false;detail.showModal();$("tools-req-close").focus()}const listed=net.requests.get(id)||null;let item=listed;try{const result=await call("network.request",{id}),flow=result&&result.request&&typeof result.request==="object"?result.request:null;if(flow)item=Object.assign({},listed||{},flow)}catch(error){if(net.detailRevision===revision){$("tools-req-detail-body").replaceChildren(el("p","tools-note",errorText(error)));return}}if(!item||net.selected!==id||net.detailRevision!==revision)return;if(net.requests.has(id))net.requests.set(id,item);renderDetail(item);$("tools-req-mock").disabled=false}
function addPairs(parent,title,pairs){parent.appendChild(el("h4",null,title));const list=el("dl","tools-dl");const keys=pairs&&typeof pairs==="object"?Object.keys(pairs):[];if(!keys.length){parent.appendChild(el("p","tools-note","None recorded."));return}for(const key of keys){list.appendChild(el("dt",null,key));list.appendChild(el("dd",null,String(pairs[key])))}parent.appendChild(list)}
// Validate JSON, then indent its original tokens: parsing/stringifying would round large numbers.
function prettyJson(raw,partial=false){if(!partial)JSON.parse(raw);let output="",depth=0,string=false,escaped=false;const newline=()=>"\\n"+"  ".repeat(depth);for(let i=0;i<raw.length;i++){if(output.length>8*1024*1024)throw new Error("Formatted body is too large");const char=raw[i];if(string){output+=char;if(escaped)escaped=false;else if(char==="\\\\")escaped=true;else if(char==='"')string=false;continue}if(/\\s/.test(char))continue;if(char==='"'){string=true;output+=char}else if(char==="{"||char==="["){output+=char;depth++;let next=i+1;while(next<raw.length&&/\\s/.test(raw[next]))next++;if(raw[next]!=="}"&&raw[next]!=="]")output+=newline()}else if(char==="}"||char==="]"){depth--;if(depth<0)throw new Error("Invalid JSON preview");const previous=output[output.length-1];if(previous!=="{"&&previous!=="[")output+=newline();output+=char}else if(char===",")output+=char+newline();else if(char===":")output+=": ";else output+=char}return output}
function addPreview(parent,title,value,headers,flowId,which){const head=el("div","tools-body-head"),raw=value===null||value===undefined?"":String(value),pretty=makeButton("Pretty","btn small",{"aria-label":"Pretty "+title.toLowerCase(),"aria-pressed":"false"}),copy=makeButton("Copy","btn small",{"aria-label":"Copy "+title.toLowerCase()}),preview=el("pre","tools-pre",raw),status=el("p","tools-copy-status");status.setAttribute("role","status");const marker="…[truncated]",partial=raw.endsWith(marker),jsonType=Object.entries(headers||{}).some(([key,value])=>key.toLowerCase()==="content-type"&&/(?:\\/|\\+)json(?:\\s*;|\\s*$)/i.test(String(value)));const heading=el("h4",null,title+(partial?" · Incomplete":""));if(partial)heading.title="Only part of the body was captured. The preview is limited to 2 KB.";head.appendChild(heading);head.appendChild(pretty);head.appendChild(copy);parent.appendChild(head);let formatted=null;try{formatted=prettyJson(raw)}catch{if(partial){const fragment=raw.slice(0,-marker.length);if(jsonType||/^\\s*[\\[{]/.test(fragment)){try{formatted=prettyJson(fragment,true)+"\\n"+marker}catch{}}}}pretty.disabled=formatted===null;pretty.title=formatted===null?"Pretty is available for JSON previews.":"Format JSON";copy.disabled=!raw;copy.title="Copy complete body (redacted)";parent.appendChild(raw?preview:el("p","tools-note","Empty."));parent.appendChild(status);function setPretty(on){pretty.setAttribute("aria-pressed",String(on));pretty.textContent=on?"Raw":"Pretty";pretty.setAttribute("aria-label",(on?"Raw ":"Pretty ")+title.toLowerCase());if(formatted!==null)pretty.title=on?"Show original text":"Format JSON";preview.textContent=on?formatted:raw;status.textContent=""}setPretty(formatted!==null);pretty.addEventListener("click",()=>setPretty(pretty.getAttribute("aria-pressed")!=="true"));copy.addEventListener("click",async()=>{copy.disabled=true;const usePretty=pretty.getAttribute("aria-pressed")==="true";let bodyError="";const bodyText=call("network.request",{id:flowId,body:which}).then(result=>{if(!result||!result.complete||typeof result.body!=="string"){bodyError=result&&result.reason==="too_large"?"Full body exceeds the 4 MB copy limit.":result&&result.reason==="binary"?"Binary bodies cannot be copied as text.":"Full body was not saved. Capture this request again to copy it in full.";throw new Error(bodyError)}let value=result.body;if(usePretty){try{value=prettyJson(value)}catch{}}return value});try{if(navigator.clipboard.write&&typeof ClipboardItem!=="undefined"){await navigator.clipboard.write([new ClipboardItem({"text/plain":bodyText.then(value=>new Blob([value],{type:"text/plain"}))})])}else await navigator.clipboard.writeText(await bodyText);status.textContent="Full body copied."}catch{status.textContent=bodyError||"Could not copy the full body. Try Copy again."}finally{bodyText.catch(()=>{});copy.disabled=false}})}
function renderDetail(item){const body=$("tools-req-detail-body");body.replaceChildren();text("tools-req-detail-title",(item.method||"")+" "+(item.status===undefined?"":item.status));const summary=el("dl","tools-dl");const rows=[["URL",item.url||""],["Duration",typeof item.duration_ms==="number"?item.duration_ms+" ms":"—"],["Mocked",item.mocked?"Yes"+(item.mock_id?" (mock "+item.mock_id+")":""):"No"],["Started",item.started_at||"—"]];for(const pair of rows){summary.appendChild(el("dt",null,pair[0]));summary.appendChild(el("dd",null,String(pair[1])))}body.appendChild(summary);addPairs(body,"Request headers",item.request_headers_preview);addPreview(body,"Request body",item.request_body_preview,item.request_headers_preview,item.id,"request");addPairs(body,"Response headers",item.response_headers_preview);addPreview(body,"Response body",item.response_body_preview,item.response_headers_preview,item.id,"response");$("tools-req-detail").hidden=false}
$("tools-req-close").addEventListener("click",()=>closeRequest(true));
$("tools-req-detail").addEventListener("cancel",event=>{event.preventDefault();closeRequest(true)});
$("tools-req-detail").addEventListener("click",event=>{if(event.target===$("tools-req-detail"))closeRequest(true)});
$("tools-req-mock").addEventListener("click",()=>{const item=state.net.selected&&state.net.requests.get(state.net.selected);if(!item)return;fillMockFromRequest(item);selectTab("mocks",false);$("tools-mock-glob").focus();showOk("mocks","Filled from request "+(item.id||state.net.selected)+". Set the response, then add the mock.")});

// Mocks: the machine-wide registry; hits count only this device's session.
function resetMockForm(){state.mocks.editing=null;$("tools-mock-options").removeAttribute("open");for(const id of ["tools-mock-glob","tools-mock-host","tools-mock-headers","tools-mock-body","tools-mock-note"])$(id).value="";$("tools-mock-method").value="";$("tools-mock-status").value="200";$("tools-mock-ignore-query").checked=false;$("tools-mock-body").placeholder="";text("tools-mock-form-title","New mock");text("tools-mock-save","Add mock");$("tools-mock-cancel-edit").hidden=true}
function fillMockFromRequest(item){resetMockForm();const address=String(item.url||""),query=address.indexOf("?");$("tools-mock-glob").value=query>=0?address.slice(0,query):address;$("tools-mock-ignore-query").checked=query>=0;$("tools-mock-method").value=HTTP_METHOD(item.method);$("tools-mock-host").value=item.host?String(item.host):"";if(item.status)$("tools-mock-status").value=String(item.status);if(query>=0||item.host)$("tools-mock-options").setAttribute("open","")}
function HTTP_METHOD(value){const method=String(value||"").toUpperCase();return["GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"].includes(method)?method:""}
function parseHeaders(raw){const headers={};for(const line of raw.split(/\\r?\\n/)){if(!line.trim())continue;const colon=line.indexOf(":");if(colon<1)throw localError("Each header line needs a name, a colon and a value.","invalid_value");headers[line.slice(0,colon).trim()]=line.slice(colon+1).trim()}return headers}
function mockPayload(){const glob=$("tools-mock-glob").value.trim(),statusText=$("tools-mock-status").value.trim(),status=Number(statusText),body=$("tools-mock-body").value,editing=state.mocks.editing;if(!glob)throw localError("Enter a URL glob, for example */api/items*.","selector_required");if(!/^[0-9]+$/.test(statusText)||status<100||status>599)throw localError("The status must be a whole number from 100 to 599.","invalid_value");if(utf8(body)>BODY_LIMIT)throw localError("The body is larger than 32 KB.","invalid_value");const payload={url_glob:glob,status,ignore_query:$("tools-mock-ignore-query").checked,headers:parseHeaders($("tools-mock-headers").value)};const method=$("tools-mock-method").value,host=$("tools-mock-host").value.trim(),note=$("tools-mock-note").value.trim();if(method||editing)payload.method=method;if(host||editing)payload.host=host;if(note||editing)payload.note=note;if(body||!editing)payload.body=body;if(editing)payload.id=editing;return payload}
async function loadMocks(){try{renderMocks((await call("mocks.list")).mocks||[]);return null}catch(error){showError("mocks",error);return error}}
function renderMocks(items){state.mocks.items=Array.isArray(items)?items:[];const list=$("tools-mocks");list.replaceChildren();$("tools-mocks-empty").hidden=state.mocks.items.length>0;for(const mock of state.mocks.items){const match=mock.match||{},response=mock.response||{},id=String(mock.id),enabled=mock.enabled!==false,row=el("li","tools-item"),title=el("div","tools-item-name");title.appendChild(el("b",null,(match.method?match.method+" ":"")+(match.url_glob||"")));title.title=(match.url_glob||"")+" · #"+id+(match.host?" · "+match.host:"")+(mock.note?" · "+mock.note:"");title.appendChild(el("small",null,(response.status||"—")+" · "+(Number(mock.hits)||0)+" hits"));row.appendChild(title);const toggleButton=makeButton(enabled?"On":"Off","btn small tools-switch",{"data-mutate":"","data-mock":id,"data-mock-action":enabled?"disable":"enable","aria-pressed":String(enabled),"aria-label":"Mock "+id+" enabled"});row.appendChild(toggleButton);const actions=el("div","tools-item-actions");actions.appendChild(makeButton("","btn small tools-mock-icon tools-mock-edit",{"data-mock":id,"data-mock-action":"edit","aria-label":"Edit mock "+id,title:"Edit mock "+id}));actions.appendChild(makeButton("","btn small tools-mock-icon tools-mock-remove",{"data-mutate":"","data-mock":id,"data-mock-action":"remove","aria-label":"Remove mock "+id,title:"Remove mock "+id}));row.appendChild(actions);list.appendChild(row)}syncLock()}
function editMock(id){const mock=state.mocks.items.find(item=>String(item.id)===id);if(!mock)return;resetMockForm();const match=mock.match||{},response=mock.response||{},headers=response.headers&&typeof response.headers==="object"?response.headers:{};state.mocks.editing=id;$("tools-mock-glob").value=match.url_glob||"";$("tools-mock-method").value=HTTP_METHOD(match.method);$("tools-mock-host").value=match.host||"";$("tools-mock-ignore-query").checked=match.ignore_query===true;$("tools-mock-status").value=String(response.status||200);$("tools-mock-headers").value=Object.keys(headers).map(key=>key+": "+headers[key]).join("\\n");$("tools-mock-note").value=mock.note||"";if(match.host||match.ignore_query||Object.keys(headers).length||mock.note)$("tools-mock-options").setAttribute("open","");$("tools-mock-body").placeholder="Leave empty to keep the current body";text("tools-mock-form-title","Edit mock "+id);text("tools-mock-save","Save changes");$("tools-mock-cancel-edit").hidden=false;$("tools-mock-glob").focus()}
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
async function pollLogs(){const logs=state.logs;if(logs.busy||logs.starting||!logs.timer)return;logs.busy=true;const generation=logs.generation;try{const data=await request("GET","/tools/logs?after="+encodeURIComponent(String(logs.after))+"&limit=500");if(generation!==logs.generation)return;const lines=Array.isArray(data.lines)?data.lines:[];if(typeof data.next==="number")logs.after=data.next;else if(lines.length&&typeof lines[lines.length-1].seq==="number")logs.after=lines[lines.length-1].seq;logs.dropped+=Math.max(0,Number(data.dropped)||0);if(lines.length){for(const line of lines)logs.buffer.push(line);if(logs.buffer.length>LOG_LIMIT)logs.buffer.splice(0,logs.buffer.length-LOG_LIMIT);if(logs.paused)logs.unseen+=lines.length;else appendRows(lines)}$("tools-log-state").title=logs.paused?"New lines are kept and shown on resume.":"Following the device log"+(data.package?" for "+data.package:"");if(data.running)logState(null,logs.paused?"Paused":"Live");else logState(data.error?"error":null,data.error?String(data.error):"Stopped");logCount()}catch(error){if(generation===logs.generation)logState("error",error.message+(error.hint?" "+error.hint:""))}finally{logs.busy=false}}
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
