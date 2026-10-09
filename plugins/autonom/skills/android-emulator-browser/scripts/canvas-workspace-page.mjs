// The workspace page of the Mobile Canvas (contract section 4 of run canvas-multi-device): the
// in-page tab bar (each tab a split of 1-4 tiles with its own /c/<tabId> URL), the tiles (each a
// same-origin iframe of the device page in embed mode), the tile headers, the empty-tile picker
// (attach, boot with progress, "In tab X" with Move here, absent devices), focus routing and the
// Ctrl+Alt shortcuts. The server serves renderWorkspacePage(model) through hooks.renderWorkspacePage.
//
// The markup holds no device data: the script reads everything from /api/* and renders it with
// textContent and setAttribute, never innerHTML. The script is plain JavaScript inside a template
// literal, so it has no backticks and writes escapes with a doubled backslash, as pageScript does.

export const LAYOUTS = Object.freeze({
  1: Object.freeze({ columns: 1, rows: 1 }),
  2: Object.freeze({ columns: 2, rows: 1 }),
  3: Object.freeze({ columns: 3, rows: 1 }),
  4: Object.freeze({ columns: 2, rows: 2 }),
});

export const EMPTY_WORKSPACE_TEXT = "Attach a device or boot one. The Canvas keeps running when devices leave; " +
  "stop it with Ctrl+C or `autonom canvas stop`.";

const TAB_ID_PATTERN = /^t_[0-9a-f]{8}$/;
const WORKSPACE_NAME_PATTERN = /^[A-Za-z0-9._-]{1,40}$/;
const DEFAULT_LIMITS = Object.freeze({ max_tabs: 8, max_tiles: 4, max_devices: 8 });

const LAYOUT_LABELS = Object.freeze({
  1: "One tile",
  2: "Two tiles side by side",
  3: "Three tiles side by side",
  4: "Four tiles in a grid",
});

// Inline SVG on a 24 px grid; stroke and size come from the page CSS. The script clones these
// from the hidden #ws-icons set, so it never builds markup from strings.
const ICONS = Object.freeze({
  mark: '<path d="M7 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M10 17h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  more: '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>',
  close: '<path d="m7 7 10 10M17 7 7 17"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  tools: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  actions: '<rect x="4" y="6" width="16" height="13" rx="2.5"/><path d="M9 6l1.2-2h3.6L15 6"/><circle cx="12" cy="12.5" r="3"/>',
  detach: '<path d="M14 5h4a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-4"/><path d="M10 8l-4 4 4 4M6 12h9"/>',
  layout1: '<rect x="4" y="5" width="16" height="14" rx="2"/>',
  layout2: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M12 5v14"/>',
  layout3: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M9.3 5v14M14.7 5v14"/>',
  layout4: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M12 5v14M4 12h16"/>',
});

function svg(name, attributes = "") {
  return `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"${attributes}>${ICONS[name]}</svg>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

/** "/c/" + the tab id. */
export function tabPath(id) {
  return "/c/" + encodeURIComponent(String(id));
}

/** "/c/t_xxxxxxxx" -> the tab id; "/" -> null; anything else -> undefined. */
export function parseTabPath(pathname) {
  if (pathname === "/") return null;
  const match = /^\/c\/(t_[0-9a-f]{8})$/.exec(typeof pathname === "string" ? pathname : "");
  return match ? match[1] : undefined;
}

/**
 * The workspace shortcuts: Ctrl+Alt+1..4 focuses tile N, Ctrl+Alt+Left/Right switches tabs. The
 * physical key (event.code) is read first, as Alt changes event.key on macOS.
 */
export function hotkey(event) {
  if (!event || !event.ctrlKey || !event.altKey || event.metaKey || event.shiftKey) return null;
  const code = typeof event.code === "string" ? event.code : "";
  const key = typeof event.key === "string" ? event.key : "";
  const digit = /^Digit([1-4])$/.exec(code) || (code ? null : /^([1-4])$/.exec(key));
  if (digit) return { tile: Number(digit[1]) };
  if (code === "ArrowLeft" || (!code && key === "ArrowLeft")) return { tab: -1 };
  if (code === "ArrowRight" || (!code && key === "ArrowRight")) return { tab: 1 };
  return null;
}

/** The model the server passes, reduced to known fields with safe values. */
export function normalizeModel(model) {
  const source = model && typeof model === "object" ? model : {};
  const limits = source.limits && typeof source.limits === "object" ? source.limits : {};
  const count = (value, fallback, max) => (Number.isInteger(value) && value > 0 && value <= max ? value : fallback);
  const title = typeof source.title === "string" && source.title.trim() ? source.title.trim().slice(0, 80) : "Autonom Canvas";
  const platforms = (Array.isArray(source.platforms) ? source.platforms : ["android", "ios"])
    .filter((platform, index, all) => (platform === "android" || platform === "ios") && all.indexOf(platform) === index);
  return {
    title,
    workspace: typeof source.workspace === "string" && WORKSPACE_NAME_PATTERN.test(source.workspace) ? source.workspace : "default",
    initial_tab: typeof source.initial_tab === "string" && TAB_ID_PATTERN.test(source.initial_tab) ? source.initial_tab : null,
    limits: {
      max_tabs: count(limits.max_tabs, DEFAULT_LIMITS.max_tabs, 64),
      max_tiles: count(limits.max_tiles, DEFAULT_LIMITS.max_tiles, 4),
      max_devices: count(limits.max_devices, DEFAULT_LIMITS.max_devices, 64),
    },
    platforms,
  };
}

/** The page's styles; the tokens are the device page's, so tiles and shell match in both themes. */
export function workspaceStyles() {
  return `:root{
  color-scheme:light dark;
  --bg:#f5f5f7;--surface:#fff;--raised:#fff;--text:#1d1d1f;--text-2:#6e6e73;--line:rgba(0,0,0,.09);--line-2:rgba(0,0,0,.14);
  --fill:rgba(0,0,0,.045);--fill-2:rgba(0,0,0,.08);--accent:#0071e3;--accent-fill:#0071e3;--on-accent:#fff;--live:#1f9d55;--warn:#b25000;--idle:#8e8e93;--danger:#d70015;
  --shadow:0 1px 1px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.08);--material:rgba(255,255,255,.96);--r-sm:8px;--r-md:12px;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0b0c;--surface:#161617;--raised:#1f1f21;--text:#f5f5f7;--text-2:#a1a1a6;--line:rgba(255,255,255,.09);--line-2:rgba(255,255,255,.16);
  --fill:rgba(255,255,255,.06);--fill-2:rgba(255,255,255,.11);--accent:#0a84ff;--accent-fill:#0068d6;--live:#32d74b;--warn:#ff9f0a;--danger:#ff453a;
  --shadow:0 1px 1px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.45);--material:rgba(36,36,38,.95);
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 var(--font);-webkit-font-smoothing:antialiased;
  display:grid;grid-template-rows:auto minmax(0,1fr);overflow:hidden}
[hidden]{display:none!important}
button,input{font:inherit;color:inherit}
button{cursor:pointer;-webkit-tap-highlight-color:transparent;touch-action:manipulation;transition:transform .1s ease-out,background-color .12s ease-out}
button:not(:disabled):active{transform:scale(.96)}
button:disabled{cursor:default;opacity:.4}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none}
.sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.ws-icon{width:32px;height:32px;display:grid;place-items:center;border-radius:var(--r-sm);border:0;background:transparent;color:var(--text-2);padding:0}
.ws-icon[aria-pressed=true]{color:var(--accent)}
.btn{height:30px;padding:0 12px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--raised);font-weight:500;white-space:nowrap}
.btn.primary{background:var(--accent-fill);border-color:transparent;color:var(--on-accent)}
.btn.primary:focus-visible{outline-color:var(--text)}
.btn.small{height:28px;padding:0 10px;font-size:12px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--idle);flex:none}
.dot[data-state=live]{background:var(--live);box-shadow:0 0 0 3px color-mix(in srgb,var(--live) 18%,transparent)}
.dot[data-state=warn]{background:var(--warn)}
.dot[data-state=error]{background:var(--danger)}

/* header: brand, tab bar, layout, tab menu */
.ws-bar{position:relative;z-index:4;display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;grid-template-areas:"brand tabs layout menu";
  align-items:center;gap:12px;min-height:52px;padding:0 14px;border-bottom:1px solid var(--line);background:var(--surface)}
.ws-brand{grid-area:brand;display:flex;align-items:center;gap:10px;min-width:0}
.ws-mark{width:22px;height:22px;border-radius:6px;background:var(--text);color:var(--bg);display:grid;place-items:center;flex:none}
.ws-mark svg{width:14px;height:14px;stroke-width:2}
.ws-brand b{font-weight:600;font-size:14px}
.ws-name{color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:14ch}
.ws-tabbar{grid-area:tabs;display:flex;align-items:center;gap:4px;min-width:0}
.ws-tabs{display:flex;align-items:center;gap:2px;min-width:0;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:none;padding:2px}
.ws-tabs::-webkit-scrollbar{display:none}
.ws-tab{position:relative;display:flex;align-items:center;flex:none;border-radius:var(--r-sm)}
.ws-tab-button{display:flex;align-items:center;gap:6px;height:30px;max-width:200px;padding:0 28px 0 12px;border:0;border-radius:var(--r-sm);background:transparent;color:var(--text-2);font-weight:500;white-space:nowrap}
.ws-tab-button[aria-selected=true]{background:var(--fill-2);color:var(--text)}
.ws-tab-label{overflow:hidden;text-overflow:ellipsis}
.ws-tab-close{position:absolute;right:4px;top:50%;width:20px;height:20px;margin-top:-10px;display:grid;place-items:center;padding:0;border:0;border-radius:5px;background:transparent;color:var(--text-2);opacity:0}
.ws-tab-close svg{width:12px;height:12px;stroke-width:2}
.ws-tab:hover .ws-tab-close,.ws-tab-button[aria-selected=true]+.ws-tab-close,.ws-tab-close:focus-visible{opacity:1}
.ws-tab-input{width:160px;height:30px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--accent);background:var(--bg);outline:none}
.ws-layouts{grid-area:layout;display:flex;align-items:center;gap:2px;padding:2px;border-radius:var(--r-sm);background:var(--fill)}
.ws-layouts .ws-icon{width:30px;height:26px;border-radius:6px}
.ws-layouts .ws-icon[aria-pressed=true]{background:var(--raised);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.ws-menu-wrap{grid-area:menu;position:relative}
.ws-menu{position:absolute;top:calc(100% + 6px);right:0;z-index:6;min-width:200px;padding:6px;border-radius:var(--r-md);background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-2);box-shadow:var(--shadow)}
.ws-menu-item{display:flex;align-items:center;width:100%;height:32px;padding:0 10px;border:0;border-radius:7px;background:none;text-align:left;text-decoration:none;font-weight:400;transition:none}
.ws-menu-item:active{transform:none}
.ws-menu-item:focus{outline:none}
.ws-menu-item:focus-visible{background:var(--accent-fill);color:var(--on-accent)}
.ws-menu-item.danger{color:var(--danger)}
.ws-menu hr{border:0;border-top:1px solid var(--line);margin:5px 6px}

/* stage and tiles */
.ws-stage{display:flex;flex-direction:column;gap:12px;min-height:0;padding:12px;overflow:hidden}
.ws-lead{margin:0;padding:10px 14px;border-radius:var(--r-md);background:var(--surface);border:1px solid var(--line);color:var(--text);max-width:75ch}
.ws-grid{flex:1;min-height:0;display:grid;gap:12px;grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr)}
.ws-grid[data-layout="2"]{grid-template-columns:repeat(2,minmax(0,1fr))}
.ws-grid[data-layout="3"]{grid-template-columns:repeat(3,minmax(0,1fr))}
.ws-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr))}
.tile{position:relative;display:flex;flex-direction:column;min-width:0;min-height:0;border-radius:var(--r-md);background:var(--surface);border:1px solid var(--line);overflow:hidden;container-type:inline-size;
  transition:box-shadow .15s ease-out,border-color .15s ease-out}
.tile[data-focused=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.tile-head{display:flex;align-items:center;gap:8px;height:40px;padding:0 4px 0 12px;border-bottom:1px solid var(--line);flex:none;min-width:0}
.tile-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.badge{flex:none;height:18px;padding:0 6px;border-radius:5px;background:var(--fill);color:var(--text-2);font-size:11px;font-weight:500;line-height:18px}
.tile-stats{flex:none;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
.tile-spacer{flex:1;min-width:0}
.tile-control{position:relative;flex:none}
.tile-chip{display:flex;align-items:center;gap:4px;height:26px;padding:0 6px 0 10px;border:0;border-radius:999px;background:var(--fill);color:var(--text-2);font-weight:500;font-size:12px;white-space:nowrap}
.tile-chip svg{width:12px;height:12px}
.tile-pop{position:absolute;top:calc(100% + 4px);right:0;z-index:3;display:grid;gap:2px;min-width:150px;padding:5px;border-radius:10px;background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-2);box-shadow:var(--shadow)}
.tile-pop-item{height:30px;padding:0 10px;border:0;border-radius:6px;background:none;text-align:left;white-space:nowrap;transition:none}
.tile-pop-item:active{transform:none}
.tile-alert{display:flex;align-items:center;gap:10px;padding:6px 8px 6px 12px;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--warn) 10%,var(--surface));font-size:12px;flex:none}
.tile-alert[data-kind=error]{background:color-mix(in srgb,var(--danger) 10%,var(--surface))}
.tile-alert span{flex:1;min-width:0;overflow-wrap:anywhere}
.tile-body{position:relative;flex:1;min-height:0;display:flex}
.tile-frame{flex:1;width:100%;height:100%;border:0;background:var(--bg)}
.tile-wait{margin:auto;color:var(--text-2)}
.picker,.absent{margin:auto;width:min(420px,100%);padding:20px 16px;overflow:auto;max-height:100%}
.picker-title{margin:0 0 10px;font-size:13px;font-weight:600}
.picker-list{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.picker-row{display:flex;align-items:center;gap:10px;min-height:44px;padding:6px 6px 6px 10px;border-radius:var(--r-sm);background:var(--fill)}
.picker-text{display:grid;flex:1;min-width:0}
.picker-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.picker-meta{color:var(--text-2);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.picker-meta code{font:11.5px var(--mono)}
.picker-status{flex:none;max-width:45%;color:var(--text-2);font-size:12px;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.picker-note{margin:10px 0 0;color:var(--text-2);font-size:12px}
.absent{text-align:center}
.absent-title{margin:0;font-size:14px;font-weight:600;overflow-wrap:anywhere}
.absent-meta{margin:4px 0 14px;color:var(--text-2);font-size:12px;overflow-wrap:anywhere}
.absent-actions{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap}
.absent-status{color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
.ws-notice{position:fixed;top:64px;left:50%;z-index:8;transform:translateX(-50%);width:max-content;max-width:calc(100% - 32px);margin:0;padding:7px 14px;border-radius:var(--r-md);
  background:var(--material);border:1px solid var(--line-2);box-shadow:var(--shadow);font-size:12.5px;text-align:center;overflow-wrap:anywhere}
.ws-notice[data-kind=error]{border-color:color-mix(in srgb,var(--danger) 45%,transparent)}
.ws-confirm{position:fixed;top:64px;left:50%;z-index:9;transform:translateX(-50%);width:min(360px,calc(100% - 32px));padding:16px;border-radius:14px;background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-2);box-shadow:var(--shadow)}
.ws-confirm-title{margin:0;font-weight:600;font-size:14px}
.ws-confirm-text{margin:6px 0 14px;color:var(--text-2)}
.ws-confirm-actions{display:flex;justify-content:flex-end;gap:8px}
.btn.danger{background:var(--danger);border-color:transparent;color:#fff}
.btn.danger:focus-visible{outline-color:var(--text)}
@container (max-width:460px){.tile-stats{display:none}}
@container (max-width:360px){.badge{display:none}.tile-chip{padding:0 6px}}

@media (hover:hover){
  .ws-icon:not(:disabled):hover{background:var(--fill);color:var(--text)}
  .ws-layouts .ws-icon:not(:disabled):hover{background:var(--fill-2)}
  .ws-tab-button:not([aria-selected=true]):hover{background:var(--fill);color:var(--text)}
  .ws-tab-close:hover{background:var(--fill-2);color:var(--text)}
  .btn:not(.primary):not(.danger):not(:disabled):hover,.tile-chip:hover,.tile-pop-item:hover{background:var(--fill-2)}
  .ws-menu-item:hover{background:var(--accent-fill);color:var(--on-accent)}
  .ws-menu-item.danger:hover{background:var(--danger);color:#fff}
}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){:root{--material:var(--raised)}}

/* phone: the tab bar scrolls inside itself, tiles stack, 16 px gutters, 44 px targets */
@media (max-width:760px){
  body{display:block;height:auto;min-height:100%;overflow-x:hidden;overflow-y:auto}
  .ws-bar{position:sticky;top:0;grid-template-columns:auto minmax(0,1fr) auto;grid-template-areas:"brand tabs menu" "layout layout layout";
    gap:8px 10px;padding:env(safe-area-inset-top) 16px 8px;min-height:calc(52px + env(safe-area-inset-top))}
  .ws-brand b,.ws-name{display:none}
  .ws-layouts{justify-self:end}
  .ws-layouts .ws-icon{width:44px;height:44px}
  .ws-tab-button{height:44px;padding-right:46px}
  .ws-tab-close{opacity:1;width:44px;height:44px;margin-top:-22px;right:0}
  .ws-icon{width:44px;height:44px}
  .ws-stage{display:block;padding:16px;overflow:visible}
  .ws-lead{margin-bottom:16px}
  .ws-grid,.ws-grid[data-layout]{display:flex;flex-direction:column;gap:16px}
  .tile{height:max(460px,calc(100svh - 150px))}
  .tile-head{height:48px}
  .btn,.btn.small,.tile-pop-item,.ws-menu-item{height:44px}
  .tile-chip{height:44px}
  .ws-tab-input{font-size:16px;height:44px}
  .ws-notice,.ws-confirm{top:calc(108px + env(safe-area-inset-top))}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}button:active{transform:none!important}}`;
}

/** The page body: header, stage, notice, confirmation and the hidden icon set. No device data. */
export function workspaceMarkup(model) {
  const { workspace, limits } = normalizeModel(model);
  const layouts = Object.keys(LAYOUTS).filter((value) => Number(value) <= limits.max_tiles).map((value) =>
    `<button type="button" class="ws-icon" data-layout-choice="${value}" aria-pressed="false" aria-label="${LAYOUT_LABELS[value]}" title="${LAYOUT_LABELS[value]}">${svg(`layout${value}`)}</button>`).join("");
  const icons = Object.keys(ICONS).map((name) => svg(name, ` data-icon="${name}"`)).join("");
  return `<header class="ws-bar">
  <div class="ws-brand"><span class="ws-mark" aria-hidden="true">${svg("mark")}</span><b>Autonom</b><span class="ws-name" id="ws-name" title="Workspace">${escapeHtml(workspace)}</span></div>
  <div class="ws-tabbar">
    <div class="ws-tabs" id="ws-tabs" role="tablist" aria-label="Canvas tabs"></div>
    <button type="button" class="ws-icon" id="ws-new-tab" aria-label="New tab" title="New tab">${svg("plus")}</button>
  </div>
  <div class="ws-layouts" id="ws-layouts" role="group" aria-label="Layout">${layouts}</div>
  <div class="ws-menu-wrap">
    <button type="button" class="ws-icon" id="ws-menu-button" aria-haspopup="menu" aria-expanded="false" aria-controls="ws-menu" aria-label="Tab menu" title="Tab menu">${svg("more")}</button>
    <div class="ws-menu" id="ws-menu" role="menu" aria-label="Tab" hidden>
      <button type="button" class="ws-menu-item" role="menuitem" id="ws-menu-rename" tabindex="-1">Rename…</button>
      <a class="ws-menu-item" role="menuitem" id="ws-menu-open" href="/" target="_blank" rel="noopener" tabindex="-1">Open in new window</a>
      <hr>
      <button type="button" class="ws-menu-item danger" role="menuitem" id="ws-menu-close" tabindex="-1">Close tab</button>
    </div>
  </div>
</header>
<main class="ws-stage" id="ws-stage" role="tabpanel" tabindex="-1">
  <p class="ws-lead" id="ws-empty" hidden>${escapeHtml(EMPTY_WORKSPACE_TEXT)}</p>
  <div class="ws-grid" id="ws-grid" data-layout="1"></div>
</main>
<p class="ws-notice" id="ws-notice" role="status" aria-live="polite" hidden></p>
<div class="ws-confirm" id="ws-confirm" role="alertdialog" aria-modal="false" aria-labelledby="ws-confirm-title" aria-describedby="ws-confirm-text" hidden>
  <p class="ws-confirm-title" id="ws-confirm-title">Close tab?</p>
  <p class="ws-confirm-text" id="ws-confirm-text">The devices in it are detached, and the Autonom sessions the Canvas started for them end.</p>
  <div class="ws-confirm-actions"><button type="button" class="btn" id="ws-confirm-cancel">Cancel</button><button type="button" class="btn danger" id="ws-confirm-go">Close tab</button></div>
</div>
<noscript><p class="ws-lead">The Canvas workspace needs JavaScript.</p></noscript>
<div id="ws-icons" hidden aria-hidden="true">${icons}</div>`;
}

/** The page's script: plain JavaScript, no backticks, no innerHTML. */
export function workspaceScript(model) {
  const data = JSON.stringify(normalizeModel(model))
    .replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
  return `"use strict";
const MODEL=${data};
const LAYOUTS=${JSON.stringify(LAYOUTS)};
const OWNERS={shared:"Shared",human:"People",agent:"Agent",replay:"Replay",system:"System"};
const STATE_LABELS={attaching:"Attaching",live:"Live",offline:"Offline",failed:"Failed",detaching:"Detaching"};
const FRAME_STATES=["live","offline","failed"];
${tabPath.toString()}
${parseTabPath.toString()}
${hotkey.toString()}
const $=id=>document.getElementById(id);
const fragment=new URLSearchParams(location.hash.slice(1));
const bootstrapToken=fragment.get("token")||"";
const requestedTab=parseTabPath(location.pathname);
let currentTab=requestedTab||MODEL.initial_tab||null;
// The token leaves the address bar before anything else runs.
history.replaceState(null,"",currentTab?tabPath(currentTab):location.pathname+location.search);
const ui={tabs:$("ws-tabs"),newTab:$("ws-new-tab"),layouts:[...document.querySelectorAll("[data-layout-choice]")],menuButton:$("ws-menu-button"),menu:$("ws-menu"),
  rename:$("ws-menu-rename"),open:$("ws-menu-open"),close:$("ws-menu-close"),stage:$("ws-stage"),grid:$("ws-grid"),empty:$("ws-empty"),notice:$("ws-notice"),
  confirm:$("ws-confirm"),confirmTitle:$("ws-confirm-title"),confirmGo:$("ws-confirm-go"),confirmCancel:$("ws-confirm-cancel"),icons:$("ws-icons")};
let csrf=null,authed=false,ws=null,targets=null,focusedId=null,autoFocus=true,renaming=null,menuOpen=false,pendingClose=null;
let noticeTimer=null,workspaceTimer=null,targetsTimer=null,jobTimer=null,refreshing=false,refreshAgain=false,lastPollError="";
const tiles=[];
const tabEls=new Map();
const live=new Map();
const panels=new Map();
const jobs=new Map();
const settledJobs=new Set();
let postsDone=0,lastRevision=null,lowerRevisions=0,lastJobError="",lastTargetsError="";
const busy=new Set();
const closingTo=new Map();

// --- small helpers -----------------------------------------------------------------------
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined&&text!==null)node.textContent=String(text);return node}
function button(className,text,label){const node=el("button",className,text);node.setAttribute("type","button");if(label){node.setAttribute("aria-label",label);node.title=label}return node}
function icon(name){const source=ui.icons.querySelector('[data-icon="'+name+'"]');const copy=source.cloneNode(true);copy.removeAttribute("data-icon");return copy}
function iconButton(name,label){const node=button("ws-icon",null,label);node.appendChild(icon(name));return node}
function setText(node,value){const text=String(value);if(node&&node.textContent!==text)node.textContent=text}
function setAttr(node,name,value){const text=String(value);if(node.getAttribute(name)!==text)node.setAttribute(name,text)}
function enc(value){return encodeURIComponent(String(value))}
function devicePath(id){return "/d/"+enc(id)}
function deviceApi(id){return "/api/devices/"+enc(id)}
function count(value){return typeof value==="number"&&isFinite(value)&&value>=0?Math.round(value):null}
function limit(name){const limits=ws&&ws.limits&&typeof ws.limits==="object"?ws.limits:MODEL.limits;return count(limits[name])||MODEL.limits[name]}
function platformLabel(platform){return platform==="ios"?"iOS":"Android"}
function tabsList(){return ws&&Array.isArray(ws.tabs)?ws.tabs:[]}
function findTab(id){return tabsList().find(tab=>tab.id===id)||null}
function tabName(id){const tab=findTab(id);return tab?tab.name:"another tab"}
function devicesList(){return ws&&Array.isArray(ws.devices)?ws.devices:[]}
function absentList(){return ws&&Array.isArray(ws.absent)?ws.absent:[]}
function deviceById(id){return devicesList().find(device=>device.id===id)||null}
function slotsOf(tab){return tab&&Array.isArray(tab.slots)?tab.slots:[]}
function filledCount(tab){return slotsOf(tab).filter(Boolean).length}
function presentCount(tab){return slotsOf(tab).filter(ref=>ref&&ref.present!==false).length}
function atDeviceLimit(){return devicesList().length>=limit("max_devices")}
function limitText(){const max=limit("max_devices");return max+" devices are attached, the most one Canvas holds. Detach one to add another."}
function errorText(value){if(!value)return"";if(typeof value==="string")return value;return String(value.error||value.error_code||"unknown error")}

function notice(text,isError){clearTimeout(noticeTimer);setText(ui.notice,text||"");ui.notice.setAttribute("data-kind",isError?"error":"info");ui.notice.hidden=!text;if(text)noticeTimer=setTimeout(()=>{ui.notice.hidden=true;setText(ui.notice,"")},8000)}
function showError(error){notice(String(error&&error.message||error)+(error&&error.hint?" — "+error.hint:""),true)}

async function api(method,path,body){
  const init={method,headers:{}};
  if(method!=="GET"){init.headers["Content-Type"]="application/json";init.headers["X-Autonom-Origin"]="human";if(csrf)init.headers["X-Autonom-CSRF"]=csrf;init.body=JSON.stringify(body||{})}
  let response;
  try{response=await fetch(path,init)}finally{if(method!=="GET")postsDone++}
  const payload=await response.json().catch(()=>({}));
  if(!response.ok||payload.ok===false){const error=new Error(payload.error||response.statusText||"Request failed");error.hint=payload.hint||null;error.code=payload.error_code||null;error.status=response.status;throw error}
  return payload;
}
function act(key,promise,after){busy.add(key);renderSoon();return promise.then(result=>{if(after)after(result);return result}).catch(showError).finally(()=>{busy.delete(key);refresh();if(targetsTimer)loadTargets()})}
let renderQueued=false;
function renderSoon(){if(renderQueued)return;renderQueued=true;Promise.resolve().then(()=>{renderQueued=false;render()})}

// --- sign-in and polling ---------------------------------------------------------------------
async function authenticate(){
  const response=await fetch("/auth",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(bootstrapToken?{token:bootstrapToken}:{})});
  const payload=await response.json().catch(()=>({}));
  if(response.ok){csrf=payload.csrf||null;authed=true;return true}
  notice(bootstrapToken?(payload.error||"Authentication failed"):"Open the Canvas URL with its #token to sign in.",true);
  return false;
}
function visible(){return document.visibilityState!=="hidden"}
async function refresh(){
  if(!authed)return;
  if(refreshing){refreshAgain=true;return}
  refreshing=true;
  // A snapshot is dropped only when a POST of this window finished while it was read: that
  // answer may already be shown and the snapshot may predate it (a new tab would look closed).
  // A POST still in flight does not hold polling back, so a slow attach never freezes sync.
  // A snapshot whose revision is older than the last one applied is skipped too.
  const doneAt=postsDone;
  try{
    const snapshot=await api("GET","/api/workspace");lastPollError="";
    if(postsDone!==doneAt){refreshAgain=true;return}
    const revision=typeof snapshot.revision==="number"&&isFinite(snapshot.revision)?snapshot.revision:null;
    if(revision!==null&&lastRevision!==null&&revision<lastRevision&&++lowerRevisions<3)return;
    lowerRevisions=0;if(revision!==null)lastRevision=revision;
    ws=snapshot;render();
  }
  catch(error){const text=String(error.message);if(text!==lastPollError){lastPollError=text;showError(error)}}
  finally{refreshing=false;if(refreshAgain){refreshAgain=false;refresh()}}
}
async function loadTargets(){
  if(!authed)return;
  try{
    const result=await api("GET","/api/targets");
    targets={running:Array.isArray(result.running)?result.running:[],bootable:Array.isArray(result.bootable)?result.bootable:[]};
    for(const entry of targets.bootable){
      if(!entry)continue;
      const ref=entry.job,object=ref&&typeof ref==="object",id=object?ref.id:ref;
      if(typeof id!=="string"||!id||jobs.has(id)||settledJobs.has(id))continue;
      if(object&&finished(ref)){settledJobs.add(id);continue}
      jobs.set(id,{job:object?{...ref,kind:ref.kind||entry.kind,name:ref.name!==undefined?ref.name:entry.name,udid:ref.udid!==undefined?ref.udid:entry.udid}:{id,kind:entry.kind,name:entry.name,udid:entry.udid,state:"queued",elapsed_ms:0}});
    }
    lastTargetsError="";render();
  }catch(error){if(String(error.message)!==lastTargetsError){lastTargetsError=String(error.message);showError(error)}}
}
function finished(job){return job.state==="done"||job.state==="failed"}
async function pollJobs(){
  for(const [id,entry] of jobs){
    if(finished(entry.job))continue;
    try{
      const result=await api("GET","/api/boot/"+enc(id));
      lastJobError="";
      if(result.job&&typeof result.job==="object")entry.job=result.job;
      if(entry.job.state==="done"){jobs.delete(id);settledJobs.add(id);autoFocus=autoFocus||!focusedId;refresh();loadTargets()}
      else if(entry.job.state==="failed"){notice("Boot failed: "+(entry.job.error||entry.job.error_code||"unknown error"),true);refresh()}
    }catch(error){
      // Only a job the server no longer knows is over; a network error keeps polling it.
      if(error.status===404){entry.job={...entry.job,state:"failed",error:error.message};settledJobs.add(id)}
      if(String(error.message)!==lastJobError){lastJobError=String(error.message);showError(error)}
    }
  }
  render();
}
function syncTimers(){
  const on=authed&&visible();
  if(on&&!workspaceTimer)workspaceTimer=setInterval(refresh,1000);
  if(!on&&workspaceTimer){clearInterval(workspaceTimer);workspaceTimer=null}
  const picker=on&&tiles.some(tile=>tile&&tile.kind==="empty");
  if(picker&&!targetsTimer){targetsTimer=setInterval(loadTargets,3000);loadTargets()}
  if(!picker&&targetsTimer){clearInterval(targetsTimer);targetsTimer=null}
  const running=on&&[...jobs.values()].some(entry=>!finished(entry.job));
  if(running&&!jobTimer)jobTimer=setInterval(pollJobs,1000);
  if(!running&&jobTimer){clearInterval(jobTimer);jobTimer=null}
}

// --- tab bar ---------------------------------------------------------------------------------
function makeTab(id){
  const wrap=el("div","ws-tab");wrap.setAttribute("role","presentation");
  const tab=button("ws-tab-button");tab.setAttribute("role","tab");tab.setAttribute("id","ws-tab-"+id);tab.setAttribute("aria-controls","ws-stage");
  const label=el("span","ws-tab-label");tab.appendChild(label);
  const close=button("ws-tab-close",null,"Close tab");close.setAttribute("tabindex","-1");close.appendChild(icon("close"));
  wrap.appendChild(tab);wrap.appendChild(close);
  tab.addEventListener("click",()=>selectTab(id,true));
  tab.addEventListener("dblclick",()=>startRename(id));
  tab.addEventListener("keydown",event=>onTabKey(event,id));
  close.addEventListener("click",event=>{event.stopPropagation();requestClose(id)});
  return{wrap,tab,label,close};
}
function renderTabs(){
  const tabs=tabsList(),seen=new Set();
  tabs.forEach((item,index)=>{
    let entry=tabEls.get(item.id);
    if(!entry){entry=makeTab(item.id);tabEls.set(item.id,entry)}
    seen.add(item.id);
    const selected=item.id===currentTab;
    setAttr(entry.tab,"aria-selected",selected);setAttr(entry.tab,"tabindex",selected?0:-1);
    if(renaming!==item.id&&!busy.has("tab:rename:"+item.id))setText(entry.label,item.name);
    setAttr(entry.close,"aria-label","Close "+item.name);entry.close.title="Close "+item.name;
    if(ui.tabs.children[index]!==entry.wrap)ui.tabs.insertBefore(entry.wrap,ui.tabs.children[index]||null);
  });
  for(const [id,entry] of tabEls)if(!seen.has(id)){entry.wrap.remove();tabEls.delete(id)}
  const full=tabs.length>=limit("max_tabs");
  ui.newTab.disabled=full;ui.newTab.title=full?"At most "+limit("max_tabs")+" tabs":"New tab";
  const tab=findTab(currentTab);
  if(tab){setAttr(ui.stage,"aria-labelledby","ws-tab-"+tab.id);ui.open.setAttribute("href",tabPath(tab.id));document.title=tab.name+" · "+MODEL.title}
}
function onTabKey(event,id){
  const tabs=tabsList(),index=tabs.findIndex(tab=>tab.id===id);let next=null;
  if(event.key==="ArrowLeft")next=tabs[(index-1+tabs.length)%tabs.length];
  else if(event.key==="ArrowRight")next=tabs[(index+1)%tabs.length];
  else if(event.key==="Home")next=tabs[0];
  else if(event.key==="End")next=tabs[tabs.length-1];
  else if(event.key==="F2"){event.preventDefault();startRename(id);return}
  if(!next||hotkey(event))return;
  event.preventDefault();selectTab(next.id,true);const entry=tabEls.get(next.id);if(entry)entry.tab.focus();
}
function selectTab(id,push){
  if(!findTab(id)){
    // An id that is not (or no longer) a tab, e.g. Back to a tab closed meanwhile: render shows
    // "This tab was closed" and opens the active tab, replacing the address.
    if(ws&&typeof id==="string"&&id&&id!==currentTab){closeMenu(false);currentTab=id;render()}
    return;
  }
  closeMenu(false);
  if(id!==currentTab){currentTab=id;autoFocus=true;if(push)history.pushState(null,"",tabPath(id));else history.replaceState(null,"",tabPath(id))}
  render();
  api("POST","/api/tabs/"+enc(id)+"/activate",{}).catch(showError);
}
function newTab(){
  if(ui.newTab.disabled||busy.has("tab:new"))return;
  act("tab:new",api("POST","/api/tabs",{}),result=>{if(result.tab&&result.tab.id){ws.tabs=tabsList().filter(item=>item.id!==result.tab.id).concat([result.tab]);selectTab(result.tab.id,true)}});
}
function startRename(id){
  const entry=tabEls.get(id),tab=findTab(id);if(!entry||!tab||renaming)return;
  closeMenu(false);renaming=id;
  const input=el("input","ws-tab-input");input.setAttribute("type","text");input.setAttribute("aria-label","Tab name");input.setAttribute("maxlength","40");
  input.setAttribute("autocomplete","off");input.setAttribute("spellcheck","false");input.value=tab.name;
  entry.tab.hidden=true;entry.close.hidden=true;entry.wrap.insertBefore(input,entry.tab);
  let done=false;
  const finish=save=>{
    if(done)return;done=true;renaming=null;
    const name=String(input.value).trim();
    input.remove();entry.tab.hidden=false;entry.close.hidden=false;entry.tab.focus();
    if(save&&name&&name!==tab.name){setText(entry.label,name);act("tab:rename:"+id,api("POST","/api/tabs/"+enc(id),{name}))}
    else setText(entry.label,tab.name);
  };
  input.addEventListener("keydown",event=>{event.stopPropagation();if(event.key==="Enter"){event.preventDefault();finish(true)}else if(event.key==="Escape"){event.preventDefault();finish(false)}});
  input.addEventListener("blur",()=>finish(true));
  input.focus();if(input.select)input.select();
}
function requestClose(id){
  const tab=findTab(id);if(!tab)return;
  closeMenu(false);
  const devices=presentCount(tab);
  if(!devices){closeTab(id);return}
  pendingClose=id;
  setText(ui.confirmTitle,"Close tab and detach "+devices+(devices===1?" device?":" devices?"));
  ui.confirm.hidden=false;ui.confirmCancel.focus();
}
function cancelClose(){pendingClose=null;ui.confirm.hidden=true}
function closeTab(id){
  if(busy.has("tab:close:"+id))return;
  const tabs=tabsList(),index=tabs.findIndex(tab=>tab.id===id);
  const neighbour=tabs[index+1]||tabs[index-1]||null;
  closingTo.set(id,neighbour?neighbour.id:null);
  act("tab:close:"+id,api("POST","/api/tabs/"+enc(id)+"/close",{}),result=>{
    const created=result.created&&result.created.id?result.created:null;
    ws.tabs=tabsList().filter(tab=>tab.id!==id&&!(created&&tab.id===created.id));
    if(created)ws.tabs.push(created);
    if(currentTab===id){const next=neighbour&&neighbour.id!==id?neighbour:result.created;if(next&&next.id){currentTab=next.id;autoFocus=true;history.replaceState(null,"",tabPath(next.id));api("POST","/api/tabs/"+enc(next.id)+"/activate",{}).catch(showError)}}
    render();
  }).finally(()=>closingTo.delete(id));
}
function setLayout(layout){
  const tab=findTab(currentTab);if(!tab||tab.layout===layout)return;
  act("tab:layout",api("POST","/api/tabs/"+enc(tab.id),{layout}),result=>{if(result.tab&&result.tab.id){ws.tabs=tabsList().map(item=>item.id===result.tab.id?result.tab:item);render()}});
}
function renderLayout(tab){
  const filled=filledCount(tab);
  for(const choice of ui.layouts){
    const value=Number(choice.getAttribute("data-layout-choice"));
    setAttr(choice,"aria-pressed",value===tab.layout);
    const disabled=value<filled;
    if(choice.disabled!==disabled)choice.disabled=disabled;
  }
}
function openMenu(){if(!findTab(currentTab))return;menuOpen=true;ui.menu.hidden=false;ui.menuButton.setAttribute("aria-expanded","true");ui.rename.focus()}
function closeMenu(refocus){if(!menuOpen)return;menuOpen=false;ui.menu.hidden=true;ui.menuButton.setAttribute("aria-expanded","false");if(refocus)ui.menuButton.focus()}

// --- tiles -----------------------------------------------------------------------------------
function describe(tab,ref,slot){
  if(!ref||typeof ref.device!=="string")return{kind:"empty",key:tab.id+"|empty|"+slot};
  const device=ref.present===false?null:deviceById(ref.device);
  if(ref.present===false){const absent=absentList().find(item=>item.id===ref.device)||{id:ref.device,name:ref.device,bootable:null};return{kind:"absent",key:tab.id+"|absent|"+ref.device,id:ref.device,absent}}
  return{kind:"device",key:tab.id+"|device|"+ref.device,id:ref.device,device:device||{id:ref.device,name:ref.device,state:"attaching"}};
}
function renderTiles(tab){
  const slots=slotsOf(tab);
  const total=Math.max(1,Math.min(limit("max_tiles"),4,Number(tab.layout)||slots.length||1));
  setAttr(ui.grid,"data-layout",total);
  for(let slot=0;slot<total;slot++){
    const want=describe(tab,slots[slot]||null,slot);
    let tile=tiles[slot];
    if(!tile||tile.key!==want.key){if(tile)dropTile(tile);tile=buildTile(want);tiles[slot]=tile}
    tile.slot=slot;tile.tab=tab.id;updateTile(tile,want);
    if(ui.grid.children[slot]!==tile.el)ui.grid.insertBefore(tile.el,ui.grid.children[slot]||null);
  }
  while(tiles.length>total)dropTile(tiles.pop());
  if(focusedId&&!tileFor(focusedId))focusedId=null;
  if(autoFocus&&!focusedId){const first=tiles.find(tile=>tile.kind==="device");if(first){autoFocus=false;focusDevice(first.id)}}
  markFocus();
}
function dropTile(tile){tile.el.remove();tile.iframe=null}
function tileFor(id){return tiles.find(tile=>tile&&tile.kind==="device"&&tile.id===id)||null}
function buildTile(want){
  const tile={kind:want.kind,key:want.key,id:want.id||null,el:el("section","tile"),iframe:null,want};
  tile.el.setAttribute("role","group");
  tile.body=el("div","tile-body");
  if(want.kind==="device")buildDevice(tile);
  else if(want.kind==="absent")buildAbsent(tile);
  else buildPicker(tile);
  tile.el.appendChild(tile.body);
  tile.el.addEventListener("pointerdown",()=>{if(tile.kind==="device")focusDevice(tile.id)});
  return tile;
}
function updateTile(tile,want){
  tile.want=want;
  setAttr(tile.el,"data-slot",tile.slot);
  setAttr(tile.el,"aria-label","Tile "+(tile.slot+1));
  if(tile.kind==="device")updateDevice(tile,want.device);
  else if(tile.kind==="absent")updateAbsent(tile,want.absent);
  else updatePicker(tile);
}

function buildDevice(tile){
  const head=el("div","tile-head");
  const dot=el("i","dot");dot.setAttribute("aria-hidden","true");
  const name=el("span","tile-name"),state=el("span","sr"),badge=el("span","badge"),stats=el("span","tile-stats"),spacer=el("span","tile-spacer");
  const control=el("div","tile-control");
  const chip=button("tile-chip");chip.setAttribute("aria-haspopup","true");chip.setAttribute("aria-expanded","false");
  const chipText=el("span");chip.appendChild(chipText);chip.appendChild(icon("chevron"));
  const pop=el("div","tile-pop");pop.setAttribute("role","group");pop.setAttribute("aria-label","Control");pop.hidden=true;
  const pause=button("tile-pop-item"),take=button("tile-pop-item");
  pop.appendChild(pause);pop.appendChild(take);control.appendChild(chip);control.appendChild(pop);
  const tools=iconButton("tools","Tools");tools.setAttribute("aria-pressed","false");
  const actions=iconButton("actions","Actions");actions.setAttribute("aria-pressed","false");
  const detach=iconButton("detach","Detach");
  for(const node of [dot,name,state,badge,stats,spacer,control,tools,actions,detach])head.appendChild(node);
  const alert=el("div","tile-alert");alert.setAttribute("role","status");alert.hidden=true;
  const alertText=el("span"),reconnect=button("btn small","Reconnect");
  alert.appendChild(alertText);alert.appendChild(reconnect);
  const wait=el("p","tile-wait");
  tile.body.appendChild(wait);
  tile.el.appendChild(head);tile.el.appendChild(alert);
  tile.parts={dot,name,state,badge,stats,chip,chipText,pop,pause,take,tools,actions,detach,alert,alertText,reconnect,wait};
  chip.addEventListener("click",()=>{const open=pop.hidden;closePops();if(open){pop.hidden=false;chip.setAttribute("aria-expanded","true");pause.focus()}});
  pause.addEventListener("click",()=>setControl(tile,controlState(tile).paused?"resume":"pause"));
  take.addEventListener("click",()=>setControl(tile,controlState(tile).owner==="human"?"release":"takeover"));
  tools.addEventListener("click",()=>togglePanel(tile,"tools"));
  actions.addEventListener("click",()=>togglePanel(tile,"actions"));
  detach.addEventListener("click",()=>act("detach:"+tile.id,api("POST",deviceApi(tile.id)+"/detach",{})));
  reconnect.addEventListener("click",()=>act("reconnect:"+tile.id,api("POST",deviceApi(tile.id)+"/reconnect",{}),()=>notice("Reconnecting "+(tile.want.device.name||tile.id)+"…",false)));
}
function controlState(tile){
  const device=tile.want.device||{},state=live.get(tile.id)||{};
  return{owner:typeof state.owner==="string"?state.owner:(device.control_owner||"shared"),paused:typeof state.paused==="boolean"?state.paused:Boolean(device.input_paused)};
}
function updateDevice(tile,device){
  const parts=tile.parts,state=String(device.state||"attaching"),name=String(device.name||device.target||tile.id);
  setText(parts.name,name);parts.name.title=device.target?name+" ("+device.target+")":name;
  setText(parts.state,STATE_LABELS[state]||state);
  setText(parts.badge,platformLabel(device.platform));
  setAttr(parts.dot,"data-state",state==="live"?"live":state==="failed"?"error":state==="attaching"||state==="offline"?"warn":"idle");
  const status=live.get(tile.id)||{};
  const width=count(status.width),height=count(status.height),fps=count(status.fps);
  let stats="";
  if(width&&height)stats=width+"×"+height+(fps?" · "+fps+" fps":"");
  else if(count(device.fps_cap))stats=count(device.fps_cap)+" fps max";
  setText(parts.stats,stats);parts.stats.hidden=!stats;
  parts.stats.title=device.profile==="background"?"Background profile: lower frame rate and size":"Focused profile";
  const control=controlState(tile);
  setText(parts.chipText,(OWNERS[control.owner]||control.owner)+(control.paused?" · Paused":""));
  setAttr(parts.chip,"aria-label","Control: "+(OWNERS[control.owner]||control.owner)+(control.paused?", input paused":""));
  setText(parts.pause,control.paused?"Resume input":"Pause input");
  setText(parts.take,control.owner==="human"?"Release":"Take over");
  const ready=state==="live";
  for(const node of [parts.chip,parts.pause,parts.take,parts.tools,parts.actions])if(node.disabled===ready)node.disabled=!ready;
  if(!ready)closePop(tile);
  const panel=panels.get(tile.id)||{};
  setAttr(parts.tools,"aria-pressed",Boolean(panel.tools));setAttr(parts.actions,"aria-pressed",Boolean(panel.actions));
  parts.detach.disabled=state==="detaching"||busy.has("detach:"+tile.id);
  let alert="",kind="warn";
  if(state==="failed"){alert="Device failed"+(device.error?": "+errorText(device.error):".");kind="error"}
  else if(state==="offline")alert="Device is offline. It reconnects when the target is back.";
  else if(device.session_error)alert="No Autonom session: "+errorText(device.session_error);
  setText(parts.alertText,alert);parts.alert.hidden=!alert;setAttr(parts.alert,"data-kind",kind);
  parts.reconnect.disabled=busy.has("reconnect:"+tile.id);
  if(authed&&!tile.iframe&&FRAME_STATES.includes(state)){
    const frame=el("iframe","tile-frame");
    frame.setAttribute("src",devicePath(tile.id)+"/?embed=1");
    frame.setAttribute("title",name+" screen");
    tile.iframe=frame;tile.body.appendChild(frame);
  }
  if(tile.iframe)setAttr(tile.iframe,"title",name+" screen");
  parts.wait.hidden=Boolean(tile.iframe);
  setText(parts.wait,state==="detaching"?"Detaching "+name+"…":"Attaching "+name+"…");
}
function closePop(tile){if(tile.parts&&!tile.parts.pop.hidden){tile.parts.pop.hidden=true;tile.parts.chip.setAttribute("aria-expanded","false")}}
function closePops(){for(const tile of tiles)if(tile&&tile.kind==="device")closePop(tile)}
function setControl(tile,mode){
  closePops();
  act("control:"+tile.id,api("POST",devicePath(tile.id)+"/control",{mode}),result=>{
    const state=live.get(tile.id)||{};
    if(typeof result.control_owner==="string")state.owner=result.control_owner;
    if(typeof result.input_paused==="boolean")state.paused=result.input_paused;
    live.set(tile.id,state);
  });
}
function send(tile,message){try{const target=tile.iframe&&tile.iframe.contentWindow;if(target)target.postMessage(message,location.origin)}catch(error){}}
function togglePanel(tile,name){
  const panel=panels.get(tile.id)||{tools:false,actions:false},open=!panel[name];
  panel.tools=false;panel.actions=false;panel[name]=open;panels.set(tile.id,panel);
  if(name==="tools")send(tile,{type:"autonom:tools",open});else send(tile,{type:"autonom:panel",name:"actions",open});
  updateTile(tile,tile.want);
}

function buildAbsent(tile){
  const box=el("div","absent");
  const title=el("p","absent-title"),meta=el("p","absent-meta"),row=el("div","absent-actions");
  const status=el("span","absent-status"),boot=button("btn primary small","Boot"),remove=button("btn small","Remove");
  row.appendChild(status);row.appendChild(boot);row.appendChild(remove);
  box.appendChild(title);box.appendChild(meta);box.appendChild(row);tile.body.appendChild(box);
  tile.parts={title,meta,status,boot,remove};
  boot.addEventListener("click",()=>{const bootable=tile.want.absent.bootable;if(bootable)startBoot(bootable,{tab:tile.tab},"absent:"+tile.id)});
  remove.addEventListener("click",()=>act("clear:"+tile.tab+":"+tile.slot,api("POST","/api/tabs/"+enc(tile.tab)+"/slots/"+tile.slot+"/clear",{})));
}
function updateAbsent(tile,absent){
  const parts=tile.parts,name=String(absent.name||absent.target||absent.id);
  setText(parts.title,name+" is not running");
  setText(parts.meta,platformLabel(absent.platform)+(absent.target?" · "+absent.target:"")+". It is attached again when it starts.");
  const bootable=absent.bootable&&typeof absent.bootable==="object"?absent.bootable:null;
  const job=bootable?jobFor(bootable):null;
  const running=job&&!finished(job);
  setText(parts.status,running?jobText(job):job&&job.state==="failed"?"Boot failed":"");parts.status.hidden=!running&&!(job&&job.state==="failed");
  parts.boot.hidden=!bootable||Boolean(running);
  parts.boot.disabled=atDeviceLimit()||busy.has("absent:"+tile.id);
  parts.boot.title=atDeviceLimit()?limitText():"Boot "+name;
  parts.remove.disabled=busy.has("clear:"+tile.tab+":"+tile.slot);
}

function buildPicker(tile){
  const box=el("div","picker");
  const title=el("h3","picker-title","Add a device");
  const list=el("ul","picker-list");list.setAttribute("aria-label","Devices");
  const note=el("p","picker-note");
  box.appendChild(title);box.appendChild(list);box.appendChild(note);tile.body.appendChild(box);
  tile.parts={list,note,rows:new Map()};
}
function pickerItems(){
  const items=[];if(!targets)return items;
  for(const target of targets.running)if(target&&typeof target.target==="string")items.push({key:"run|"+target.platform+"~"+target.target,type:"running",target});
  for(const entry of targets.bootable){if(!entry||entry.running)continue;items.push({key:"boot|"+entry.kind+"|"+(entry.kind==="simulator"?entry.udid:entry.name),type:"bootable",bootable:entry})}
  return items;
}
function updatePicker(tile){
  const parts=tile.parts,items=pickerItems(),seen=new Set();
  items.forEach((item,index)=>{
    let row=parts.rows.get(item.key);
    if(!row){row=buildRow(tile);parts.rows.set(item.key,row)}
    seen.add(item.key);row.item=item;updateRow(tile,row);
    if(parts.list.children[index]!==row.li)parts.list.insertBefore(row.li,parts.list.children[index]||null);
  });
  for(const [key,row] of parts.rows)if(!seen.has(key)){row.li.remove();parts.rows.delete(key)}
  let note="";
  if(!targets)note="Looking for devices…";
  else if(!items.length)note="No devices are running. Start an emulator or a Simulator and it shows up here.";
  else if(atDeviceLimit())note=limitText();
  setText(parts.note,note);parts.note.hidden=!note;
  parts.list.hidden=!items.length;
}
function buildRow(tile){
  const li=el("li","picker-row"),text=el("div","picker-text"),name=el("span","picker-name"),meta=el("span","picker-meta");
  const status=el("span","picker-status"),action=button("btn small");
  text.appendChild(name);text.appendChild(meta);li.appendChild(text);li.appendChild(status);li.appendChild(action);
  const row={li,name,meta,status,action,item:null};
  action.addEventListener("click",()=>rowAction(tile,row));
  return row;
}
function rowPlan(row){
  const item=row.item,full=atDeviceLimit();
  if(item.type==="running"){
    const target=item.target,id=target.platform+"~"+target.target;
    if(target.attached&&target.tab===currentTab)return{status:"In this tab"};
    if(target.attached)return{status:"In tab "+tabName(target.tab),action:"Move here",busyText:"Moving…",key:"move:"+id,op:"move",id};
    if(target.attachable===false)return{status:target.reason||target.state||"Not available"};
    return{action:"Attach",busyText:"Attaching…",key:"attach:"+id,op:"attach",disabled:full,title:full?limitText():""};
  }
  const job=jobFor(item.bootable);
  if(job&&!finished(job))return{status:jobText(job)};
  return{status:job&&job.state==="failed"?"Boot failed":"",action:"Boot",busyText:"Starting…",key:"boot:"+item.key,op:"boot",disabled:full,title:full?limitText():""};
}
function updateRow(tile,row){
  const item=row.item;
  if(item.type==="running"){
    setText(row.name,item.target.name||item.target.target);
    setText(row.meta,platformLabel(item.target.platform)+" · "+item.target.target);
  }else{
    const entry=item.bootable;
    setText(row.name,entry.label||entry.name||entry.udid||"Device");
    setText(row.meta,(entry.kind==="simulator"?"iOS Simulator":"Android emulator")+(count(entry.port)?" · port "+count(entry.port):""));
  }
  const plan=rowPlan(row),pending=plan.key&&busy.has(plan.key);
  setText(row.status,plan.status||"");row.status.hidden=!plan.status;
  row.action.hidden=!plan.action;
  if(plan.action){setText(row.action,pending?plan.busyText:plan.action);const disabled=Boolean(plan.disabled||pending);if(row.action.disabled!==disabled)row.action.disabled=disabled;row.action.title=plan.title||""}
}
function rowAction(tile,row){
  const plan=rowPlan(row);if(!plan.op||plan.disabled||busy.has(plan.key))return;
  if(plan.op==="attach"){const target=row.item.target;act(plan.key,api("POST","/api/devices",{platform:target.platform,target:target.target,tab:tile.tab,slot:tile.slot}),()=>{autoFocus=true})}
  else if(plan.op==="move")act(plan.key,api("POST",deviceApi(plan.id)+"/move",{tab:tile.tab,slot:tile.slot}),()=>{autoFocus=true});
  else if(plan.op==="boot")startBoot(row.item.bootable,{tab:tile.tab,slot:tile.slot},plan.key);
}
function bootBody(entry){return entry.kind==="simulator"?{kind:"simulator",udid:entry.udid}:{kind:"avd",name:entry.name}}
function startBoot(entry,place,key){
  const body={...bootBody(entry),attach:true};
  if(place){body.tab=place.tab;if(place.slot!==undefined)body.slot=place.slot}
  act(key,api("POST","/api/boot",body),result=>{if(result.job&&typeof result.job.id==="string"){settledJobs.delete(result.job.id);jobs.set(result.job.id,{job:result.job});syncTimers()}});
}
function jobFor(entry){
  let found=null;
  for(const item of jobs.values()){const job=item.job;if(job.kind!==entry.kind)continue;if(entry.kind==="simulator"?job.udid===entry.udid:job.name===entry.name)found=job}
  return found;
}
function jobText(job){if(job.state==="attaching")return"Attaching…";return"Booting… "+Math.max(0,Math.round((count(job.elapsed_ms)||0)/1000))+" s"}

// --- focus -----------------------------------------------------------------------------------
function markFocus(){for(const tile of tiles)if(tile)setAttr(tile.el,"data-focused",tile.kind==="device"&&tile.id===focusedId)}
function broadcastFocus(){for(const tile of tiles)if(tile&&tile.iframe)send(tile,{type:"autonom:focus",focused:tile.id===focusedId})}
function focusDevice(id,moveFocus){
  const tile=tileFor(id);if(!tile)return;
  if(focusedId!==id){focusedId=id;markFocus();broadcastFocus();api("POST","/api/focus",{id}).catch(showError)}
  if(moveFocus&&tile.iframe)tile.iframe.focus();
}
function runHotkey(action){
  if(!action)return;
  if(action.tile){
    const tile=tiles[action.tile-1];if(!tile)return;
    if(tile.kind==="device")focusDevice(tile.id,true);
    else{const target=tile.el.querySelector("button");if(target)target.focus()}
  }else if(action.tab){
    const tabs=tabsList(),index=tabs.findIndex(tab=>tab.id===currentTab);if(tabs.length<2)return;
    const next=tabs[(index+action.tab+tabs.length)%tabs.length];if(next)selectTab(next.id,true);
  }
}

// --- render ----------------------------------------------------------------------------------
function render(){
  if(!ws)return;
  const tabs=tabsList();
  if(!tabs.length){notice(ws.mode==="single"?"This Canvas runs one device. Open its page at /.":"This workspace has no tabs.",true);return}
  let tab=findTab(currentTab);
  if(!tab){
    // A poll may show this window's own close before its answer: go to the neighbour quietly.
    const closing=Boolean(currentTab)&&busy.has("tab:close:"+currentTab);
    if(currentTab&&!closing)notice("This tab was closed",false);
    tab=(closing&&findTab(closingTo.get(currentTab)))||findTab(ws.active_tab)||tabs[0];
    currentTab=tab.id;autoFocus=true;
    history.replaceState(null,"",tabPath(tab.id));
    if(closing)api("POST","/api/tabs/"+enc(tab.id)+"/activate",{}).catch(showError);
  }
  renderTabs();
  renderLayout(tab);
  renderTiles(tab);
  ui.empty.hidden=devicesList().length+absentList().length>0;
  syncTimers();
}

// --- events ----------------------------------------------------------------------------------
ui.newTab.addEventListener("click",newTab);
for(const choice of ui.layouts)choice.addEventListener("click",()=>setLayout(Number(choice.getAttribute("data-layout-choice"))));
ui.menuButton.addEventListener("click",()=>{if(menuOpen)closeMenu(true);else openMenu()});
ui.rename.addEventListener("click",()=>startRename(currentTab));
ui.open.addEventListener("click",()=>closeMenu(false));
ui.close.addEventListener("click",()=>requestClose(currentTab));
ui.confirmCancel.addEventListener("click",cancelClose);
ui.confirmGo.addEventListener("click",()=>{const id=pendingClose;cancelClose();if(id)closeTab(id)});
document.addEventListener("pointerdown",event=>{
  if(menuOpen&&!ui.menu.contains(event.target)&&!ui.menuButton.contains(event.target))closeMenu(false);
  for(const tile of tiles)if(tile&&tile.kind==="device"&&!tile.parts.chip.contains(event.target)&&!tile.parts.pop.contains(event.target))closePop(tile);
});
document.addEventListener("keydown",event=>{
  if(event.key==="Escape"){if(!ui.confirm.hidden){cancelClose();return}if(menuOpen){closeMenu(true);return}closePops();return}
  const action=hotkey(event);if(!action)return;
  event.preventDefault();runHotkey(action);
});
// iOS Safari applies :active (the press feedback) only while the document has a touch listener.
document.addEventListener("touchstart",()=>{},{passive:true});
document.addEventListener("visibilitychange",()=>{if(visible())refresh();syncTimers()});
window.addEventListener("focus",()=>{if(focusedId&&tileFor(focusedId))api("POST","/api/focus",{id:focusedId}).catch(()=>{})});
window.addEventListener("popstate",()=>{const id=parseTabPath(location.pathname);if(id===undefined)return;selectTab(id===null?(ws&&ws.active_tab)||currentTab:id,false)});
window.addEventListener("message",event=>{
  if(event.origin!==location.origin)return;
  const data=event.data;if(!data||typeof data!=="object"||typeof data.type!=="string")return;
  const tile=tiles.find(item=>item&&item.iframe&&item.iframe.contentWindow===event.source);if(!tile)return;
  if(data.id!==undefined&&data.id!==tile.id)return;
  if(data.type==="autonom:ready"){send(tile,{type:"autonom:focus",focused:tile.id===focusedId});const panel=panels.get(tile.id);if(panel&&panel.tools)send(tile,{type:"autonom:tools",open:true});if(panel&&panel.actions)send(tile,{type:"autonom:panel",name:"actions",open:true})}
  else if(data.type==="autonom:focus-request")focusDevice(tile.id,false);
  else if(data.type==="autonom:state"){live.set(tile.id,{live:Boolean(data.live),session:typeof data.session==="string"?data.session:null,transport:typeof data.transport==="string"?data.transport:null,owner:typeof data.owner==="string"?data.owner:undefined,paused:typeof data.paused==="boolean"?data.paused:undefined,fps:count(data.fps),width:count(data.width),height:count(data.height)});updateTile(tile,tile.want)}
  else if(data.type==="autonom:hotkey"){const key=String(data.key);runHotkey(/^[1-4]$/.test(key)?{tile:Number(key)}:key==="ArrowLeft"?{tab:-1}:key==="ArrowRight"?{tab:1}:null)}
});

async function bootstrap(){if(!(await authenticate()))return;await refresh();syncTimers()}
bootstrap().catch(showError);`;
}

/** The complete workspace document (string) for hooks.renderWorkspacePage. */
export function renderWorkspacePage(model) {
  const normalized = normalizeModel(model);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>${escapeHtml(normalized.title)}</title>
<link rel="icon" href="data:,">
<style>
${workspaceStyles()}
</style>
</head>
<body>
${workspaceMarkup(normalized)}
<script>
${workspaceScript(normalized)}
</script>
</body>
</html>`;
}
