/** Shared Canvas primitives. Dependency-free; browser helpers serialize into inline pages. */
export const CANVAS_ICONS = Object.freeze({
  edit: '<path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/>',
  record: '<g class="record-start"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/></g><rect class="record-stop" x="6" y="6" width="12" height="12" rx="2"/>',
  mark: '<path d="M12 3 4 20h4l4-9 4 9h4z"/>',
  settings: '<path d="m9 3-.5 2-2 .8-1.8-.5-2 3.4 1.4 1.5v2.3L2.7 14l2 3.4 2-.5 1.8 1L9 20h4l.5-2 2-1 1.8.5 2-3.4-1.4-1.5v-2.3l1.4-1.5-2-3.4-2 .5-1.8-.8L13 3z"/><circle cx="11" cy="11.5" r="3"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  phoneLandscape: '<rect x="2.5" y="7" width="19" height="10" rx="2.5"/><path d="M18.5 11v2"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  people: '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.5a3 3 0 0 1 0 5.5M18 19a5.5 5.5 0 0 0-2.5-4.6"/>',
  close: '<path d="m7 7 10 10M17 7 7 17"/>',
  inspector: '<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="M15 4.5v15"/>',
  back: '<path d="M15 5 8 12l7 7"/>',
  home: '<circle cx="12" cy="12" r="6.5"/>',
  recent: '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
  rotate: '<path d="M4.5 12a7.5 7.5 0 0 1 13-5.1L20 9.5M20 4.5v5h-5"/><path d="M19.5 12a7.5 7.5 0 0 1-13 5.1L4 14.5M4 19.5v-5h5"/>',
  volumeDown: '<path d="M4 9.5h3l4.5-4v13L7 14.5H4z"/><path d="M16 12h4"/>',
  volumeUp: '<path d="M4 9.5h3l4.5-4v13L7 14.5H4z"/><path d="M16 12h4M18 10v4"/>',
  power: '<path d="M12 3.5v8"/><path d="M7 6.8a7 7 0 1 0 10 0"/>',
  up: '<path d="m6 14 6-6 6 6"/>',
  left: '<path d="m14 6-6 6 6 6"/>',
  right: '<path d="m10 6 6 6-6 6"/>',
  down: '<path d="m6 10 6 6 6-6"/>',
  delete: '<path d="M9 6h10a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 19 18H9l-5.5-6z"/><path d="m11.5 9.5 5 5M16.5 9.5l-5 5"/>',
  wake: '<path d="M12 4v2M12 18v2M4 12h2M18 12h2M6.3 6.3l1.4 1.4M16.3 16.3l1.4 1.4M6.3 17.7l1.4-1.4M16.3 7.7l1.4-1.4"/><circle cx="12" cy="12" r="3"/>',
  notifications: '<path d="M6 15V10a6 6 0 0 1 12 0v5l1.5 2.5h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  quickSettings: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>',
  collapse: '<path d="m7 14 5-5 5 5"/><path d="M5 19h14"/>',
  clipboard: '<rect x="7" y="4" width="10" height="16" rx="2"/><path d="M10 4V3h4v1"/>',
  tools: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5v4.5H15"/>',
  actions: '<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.8l1.4-2h4.6l1.4 2h1.8A2.5 2.5 0 0 1 20 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  more: '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>',
  detach: '<path d="M14 5h4a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-4"/><path d="M10 8l-4 4 4 4M6 12h9"/>',
  layout1: '<rect x="4" y="5" width="16" height="14" rx="2"/>',
  layout2: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M12 5v14"/>',
  layout3: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M9.3 5v14M14.7 5v14"/>',
  layout4: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M12 5v14M4 12h16"/>',
});

function escape(value) {
  return String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

/** Decorative only: the containing control supplies its accessible name. */
export function canvasIcon(name, className = "", attributes = "") {
  if (!Object.hasOwn(CANVAS_ICONS, name)) throw new Error("Unknown Canvas icon: " + name);
  const classes = className ? ' class="' + escape(className) + '"' : "";
  return '<svg' + classes + ' viewBox="0 0 24 24" aria-hidden="true" focusable="false"' + attributes + '>' + CANVAS_ICONS[name] + '</svg>';
}

/** Attributes are trusted source markup, never device or user data. */
export function canvasIconButton(name, label, attributes = "", className = "") {
  const classes = className ? ' class="' + escape(className) + '"' : "";
  return '<button type="button"' + classes + ' ' + attributes + ' title="' + escape(label) + '" aria-label="' + escape(label) + '">' + canvasIcon(name) + '</button>';
}
export function canvasTabList(tabs, prefix, className) {
  return tabs.map(({id,label}, index) =>
    '<button type="button" role="tab" class="' + escape(className) + '" id="' + prefix + '-tab-' + id + '" aria-controls="' + prefix + '-panel-' + id + '" aria-selected="' + (index === 0) + '" tabindex="' + (index === 0 ? 0 : -1) + '">' + escape(label) + '</button>').join("");
}
export function canvasTabPanel(id, body, prefix, className, first) {
  return '<section class="' + className + '-panel" role="tabpanel" id="' + prefix + '-panel-' + id + '" aria-labelledby="' + prefix + '-tab-' + id + '" tabindex="0"' + (first ? "" : " hidden") + '>' +
    '<div class="' + className + '-msg" id="' + prefix + '-msg-' + id + '" role="status" aria-live="polite" hidden></div>' + body + '</section>';
}

/** DOM factories never interpolate user/device text into markup. */
export function canvasElement(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}
export function canvasButton(label, className = "btn small", attributes) {
  const node = document.createElement("button");
  node.className = className || "btn small";
  if (label !== undefined && label !== null) node.textContent = String(label);
  node.setAttribute("type", "button");
  if (attributes) for (const key of Object.keys(attributes)) node.setAttribute(key, attributes[key]);
  return node;
}

/** Roving tab state is shared; loading/polling remains the owning panel's responsibility. */
export function canvasSelectTab(tabs, panels, index, focus = false) {
  if (index < 0 || index >= tabs.length || tabs[index].disabled) return false;
  tabs.forEach((tab, i) => {
    tab.setAttribute("aria-selected", String(i === index));
    tab.setAttribute("tabindex", i === index ? "0" : "-1");
    if (panels[i]) panels[i].hidden = i !== index;
  });
  if (focus) tabs[index].focus();
  return true;
}
export function canvasTabKey(event, tabs, index) {
  if (event.altKey || event.ctrlKey || event.metaKey) return null;
  const enabled = tabs.map((tab,i) => tab.disabled ? -1 : i).filter(i => i >= 0);
  if (!enabled.length) return null;
  const current = enabled.indexOf(index);
  const moves = {
    ArrowRight: enabled[(current + 1) % enabled.length],
    ArrowLeft: enabled[(current - 1 + enabled.length) % enabled.length],
    Home: enabled[0], End: enabled[enabled.length - 1],
  };
  if (!Object.hasOwn(moves, event.key)) return null;
  event.preventDefault();
  return moves[event.key];
}
export function bindCanvasTabs(list, tabs, current, select) {
  tabs.forEach((tab,index) => tab.addEventListener("click", () => {
    if (!tab.disabled) select(index, false);
  }));
  list.addEventListener("keydown", event => {
    const index = canvasTabKey(event, tabs, current());
    if (index !== null) select(index, true);
  });
}

/** Embedded pages execute the exported implementations, not hand-maintained copies. */
export function canvasDomScript() {
  return "const el=" + canvasElement.toString() + ",makeButton=" + canvasButton.toString() + ";\n" +
    "const canvasTabKey=" + canvasTabKey.toString() + ",canvasSelectTab=" + canvasSelectTab.toString() + ",bindCanvasTabs=" + bindCanvasTabs.toString() + ";";
}

export function canvasUiStyles() {
return `
:root{--control-height:32px;--control-small:28px;--icon-size:18px;--group-radius:12px;--motion-duration:120ms;--motion-easing:ease-out}
.canvas-theme-control{display:inline-flex;align-items:center;flex:none}
.canvas-theme{width:84px}
/* Desktop preferences share surfaces, control geometry and disclosure icons. */
html:root :is(select:not([multiple]),.popup){height:var(--control-height);min-height:var(--control-height);padding:0 10px;border:1px solid var(--line-2);border-radius:var(--r-sm);background-color:var(--raised);color:var(--text);font:400 13px/1.4 var(--font);box-shadow:none}
html:root select:not([multiple]){appearance:none;padding-right:30px;background-image:url("${canvasIconData("chevron")}");background-repeat:no-repeat;background-position:right 10px center;background-size:12px}
html:root .popup .chev{margin-left:auto;width:12px;height:12px;color:var(--text-2)}
[hidden]{display:none!important}
button,input,select,textarea{font:inherit;color:inherit}
button,select,summary,.ws-menu-item{-webkit-tap-highlight-color:transparent;touch-action:manipulation;transition:background-color var(--motion-duration) var(--motion-easing),color var(--motion-duration) var(--motion-easing),border-color var(--motion-duration) var(--motion-easing),filter var(--motion-duration) var(--motion-easing)}
button,select,summary,.ws-menu-item{cursor:pointer}
button:disabled,input:disabled,select:disabled,textarea:disabled{cursor:default;opacity:.4;filter:none;transform:none}
button:not(:disabled):active,select:not(:disabled):active{transform:none;filter:brightness(.9)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button[aria-busy=true]{cursor:progress}
svg{width:var(--icon-size);height:var(--icon-size);fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none}
.sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.btn{height:var(--control-height);padding:0 12px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--raised);font-weight:500;white-space:nowrap}
.btn.small{height:var(--control-small);padding:0 10px;font-size:12px}
.btn.primary{background:var(--action-fill);border-color:transparent;color:var(--action-text)}
.btn.danger{background:color-mix(in srgb,var(--danger) 9%,var(--raised));color:var(--danger);border-color:transparent}
:is(.icon,.ws-icon){width:var(--control-height);height:var(--control-height);padding:0;display:grid;place-items:center;border-radius:var(--r-sm);border:0;background:transparent;color:var(--text-2)}
:is(.icon,.ws-icon)[aria-pressed=true]{color:var(--text);background:var(--fill-2)}
:is(.field,.tools-field,.act-field){display:flex;gap:6px;min-width:0}
:is(.field,.tools-field,.act-field) input{flex:1}
input:not([type=checkbox]):not([type=radio]):not([type=range]),select,textarea{min-width:0;height:var(--control-height);padding:0 10px;border:1px solid var(--line-2);border-radius:var(--r-sm);background-color:var(--raised)}
input::placeholder,textarea::placeholder{color:var(--text-2);opacity:1}
input:is([type=checkbox],[type=radio]){accent-color:var(--accent)}
:is(.group,.tools-group,.act-group){padding:12px;margin:10px;min-width:0;border:1px solid var(--line-2);border-radius:var(--group-radius);background:var(--raised)}
:is(.group h2,.tools-group h3,.act-group h3){margin:0 0 8px;font-size:13px;font-weight:600;color:var(--text);letter-spacing:normal}
:is(.tools-label,.act-label){display:block;margin:8px 0 4px;font-size:12px;color:var(--text-2)}
:is(.tools-tabs,.act-tabs){display:flex;flex:1;min-width:0;gap:3px;padding:0;background:transparent;overflow-x:auto;scrollbar-width:none}
:is(.tools-tabs,.act-tabs)::-webkit-scrollbar{display:none}
:is(.tools-tab,.act-tab){flex:1 0 auto;height:var(--control-small);padding:0 10px;border:0;border-radius:var(--r-sm);background:transparent;color:var(--text-2);font-weight:500;white-space:nowrap}
:is(.tools-tab,.act-tab):focus-visible{outline-offset:-2px}
:is(.tools-tab,.act-tab)[aria-selected=true]{background:var(--fill-2);color:var(--text);box-shadow:none}
:is(.tools-head,.act-head){display:flex;align-items:center;gap:8px;padding:8px;border-bottom:1px solid var(--line)}
:is(.tools-panel,.act-panel){padding:2px 0 16px;min-width:0;outline:none}
:is(.tools-panel,.act-panel):focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
:is(.tools-msg,.act-msg){margin:10px;padding:8px 10px;border-radius:var(--r-sm);background:var(--fill);font-size:13px;overflow-wrap:anywhere}
:is(.tools-msg,.act-msg)[data-kind=error]{color:var(--danger);background:color-mix(in srgb,var(--danger) 9%,var(--raised))}
:is(.side,.tools,.tools-body,.act,.act-body){background:var(--bg)}
:is(.tools,.act){grid-area:side;display:flex;flex-direction:column;min-width:0;min-height:0;border-left:1px solid var(--line)}
:is(.tools-body,.act-body){flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;scrollbar-width:thin}
:is(.tools-icon,.act-icon){flex:none}
.dot{width:7px;height:7px;border-radius:50%;background:var(--idle);flex:none}
.dot[data-state=live]{background:var(--live);box-shadow:0 0 0 3px color-mix(in srgb,var(--live) 18%,transparent)}
.dot[data-state=warn]{background:var(--warn)}
.dot[data-state=error]{background:var(--danger)}
[role=menuitem]:focus-visible{outline:none;background:var(--fill-2)}
@media (max-width:760px){
  :is(.side,.tools,.act){position:fixed;inset:60px 8px 8px auto;width:var(--canvas-panel-width);z-index:6;border:1px solid var(--line-2);border-radius:var(--group-radius);box-shadow:var(--sheet-shadow)}
  :is(.tools,.act){padding-bottom:env(safe-area-inset-bottom)}
  :is(.tools-head,.act-head){padding:8px 8px 8px 12px}
}
@media (hover:hover){
  :is(.btn,.popup,.canvas-dropdown-trigger,.icon,.ws-icon,.row,.key,.volume-key,.device-tile,.dock button,.tools-chip,.tile-chip,.ws-menu-item,.tile-pop-item):not(:disabled):not(.primary):not(.danger):hover{background-color:var(--fill-2)}
  :is(.icon,.ws-icon,.tools-tab,.act-tab):not(:disabled):hover{color:var(--text)}
  :is(.tools-tab,.act-tab):not(:disabled):not([aria-selected=true]):hover{background:var(--fill-2)}
  .btn:is(.primary,.danger):not(:disabled):hover:not(:active){filter:brightness(.92)}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}button:active{transform:none!important}}
@media (forced-colors:active){[role=menuitem]:focus-visible{outline:2px solid CanvasText}}
`;
}

/** CSS masks and native select fallback use the same icon paths as buttons. */
export function canvasIconData(name) {
  const svg = canvasIcon(name).replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"').replace('aria-hidden="true"', 'fill="none" stroke="#626262" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"');
  return "data:image/svg+xml," + encodeURIComponent(svg).replace(/'/g, "%27");
}
