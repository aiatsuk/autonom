/** The device page, rendered as self-contained HTML with inline styles and scripts. */
import { themeControl, themeHead, themeStyles } from "./canvas-theme-page.mjs";
import { MAX_CONTROL_MESSAGE_BYTES } from "./browser-lib.mjs";
import {
  ANDROID_KEYCODES,
  META_STATE,
  US_KEY_CHARS,
  keyInputFor,
  metaStateFor,
} from "./scrcpy-lib.mjs";
import {
  toolsButton,
  toolsMarkup,
  toolsScript,
  toolsStyles,
} from "./canvas-tools-page.mjs";
import { DISPLAY_PRESETS, escapeHtml, scriptString } from "./canvas-runtime.mjs";

// Inline SVG paths of the page's icons, on a 24 px grid; stroke and size come from the CSS.
const PAGE_ICONS = Object.freeze({
  mark: '<path d="M12 3 4 20h4l4-9 4 9h4z"/>',
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
});

/** A decorative icon: the control around it carries the accessible name. */
function icon(name, className = "") {
  const classes = className ? ` class="${className}"` : "";
  return `<svg${classes} viewBox="0 0 24 24" aria-hidden="true" focusable="false">${PAGE_ICONS[name]}</svg>`;
}

/** An icon-only button, named for assistive technology and in its tooltip alike (PAGE-004). */
function iconButton(name, label, attributes, className = "") {
  const classes = className ? ` class="${className}"` : "";
  return `<button type="button"${classes} ${attributes} title="${label}" aria-label="${label}">${icon(name)}</button>`;
}

/**
 * The Size menu of an Android page (DISPLAY-004, PAGE-001): a popup button over a listbox of
 * the presets with their sizes. The button is enabled once the page may send input.
 */
function displayPicker(context) {
  if (context.options.platform !== "android") return "";
  const options = DISPLAY_PRESETS.map(({ id, label, width, height }) =>
    `${id === "default" ? '<hr aria-hidden="true">' : ""}` +
    `<button type="button" class="option" role="option" data-preset="${id}" aria-selected="false" tabindex="-1">` +
    `${icon("check", "check")}<span>${escapeHtml(label)}</span>` +
    `<small${id === "default" ? ' id="display-default-size"' : ""}>${width ? `${width} × ${height}` : ""}</small></button>`).join("");
  return `<div class="display-picker"><button type="button" class="popup" id="display-preset" aria-haspopup="listbox" aria-expanded="false" ` +
    `aria-controls="display-list" title="Display size" disabled>${icon("phone", "portrait")}${icon("phoneLandscape", "landscape")}<span class="sr">Display size: </span>` +
    `<span id="display-label">Size</span>${icon("chevron", "chev")}</button>` +
    `<div class="menu" id="display-menu"><div role="listbox" id="display-list" aria-label="Display size">${options}</div>` +
    `<p class="note">Changes the device's screen size and density. Restored when the Canvas stops.</p></div></div>`;
}

/**
 * The Canvas page (PAGE-001..004): toolbar, the device in a frame that follows the video's
 * aspect, a floating pill of device buttons, an inspector and the Tools drawer
 * (canvas-tools-page.mjs), which takes the inspector's column while it is open. Styles, icons and code are
 * inline (the CSP loads nothing else); device and status strings reach the DOM as text only.
 */
function renderPage(context, { base = "", embed = false } = {}) {
  const serial = escapeHtml(context.serial);
  // Extensions of the page (contract 3.12): each adds a button, markup, styles and script
  // right after the Tools drawer's.
  const extensions = context.server?.hooks?.pageExtensions ?? [];
  const extension = (part, ...args) => extensions.map((item) => item[part]?.(...args) ?? "").join("");
  const extensionContext = { platform: context.options.platform, id: context.id ?? null, base, embed, captureOnly: context.server?.mode === "single" };
  const android = context.options.platform === "android";
  const platform = android ? "Android" : "iOS";
  // iOS refuses the Android key buttons, so its page keeps them out of sight (PAGE-003);
  // Home and Power stay, as the bridge presses the Simulator's Home and Lock for them.
  const androidOnly = android ? "" : " hidden";
  // Volume buttons: scrcpy on Android; on iOS only the idb transport presses them, so they
  // show there (data-idb) and stay out of sight on screencap.
  const volume = android ? " data-scrcpy" : " data-idb hidden";
  const volumeControls = `<div class="volume-controls" role="group" aria-label="Volume"${android ? "" : " data-idb hidden"}>` +
    `<span>Volume</span>` +
    iconButton("volumeDown", "Volume down", `data-system="volume-down"${volume}`, "volume-key") +
    iconButton("volumeUp", "Volume up", `data-system="volume-up"${volume}`, "volume-key") + `</div>`;
  return `<!doctype html>
<html lang="en"${embed ? ' data-embed="1"' : ""}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Autonom Canvas · ${serial}</title>
<link rel="icon" href="data:,">
${themeHead()}
<style>
${themeStyles()}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 var(--font);-webkit-font-smoothing:antialiased;
  display:grid;grid-template-rows:52px minmax(0,1fr);grid-template-columns:minmax(0,1fr) 300px;grid-template-areas:"bar bar" "stage side"}
body.no-inspector{grid-template-columns:minmax(0,1fr);grid-template-areas:"bar" "stage"}
body.no-inspector .side{display:none}
[hidden]{display:none!important}
button,input{font:inherit;color:inherit}
button{cursor:pointer;-webkit-tap-highlight-color:transparent;touch-action:manipulation;transition:transform .1s ease-out,background-color .12s ease-out}
button:not(:disabled):active{transform:scale(.96)}
button:disabled,input:disabled{cursor:default;opacity:.4}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none}
.sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* toolbar */
.bar{grid-area:bar;position:relative;z-index:4;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;padding:0 14px;border-bottom:1px solid var(--line);background:var(--surface)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--idle);flex:none}
.dot[data-state=live]{background:var(--live);box-shadow:0 0 0 3px color-mix(in srgb,var(--live) 18%,transparent)}
.dot[data-state=warn]{background:var(--warn)}
.center{position:relative;display:flex;align-items:center;gap:10px;min-width:0}
.popup{display:flex;align-items:center;gap:7px;height:30px;padding:0 10px 0 9px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--raised);font-weight:500;white-space:nowrap;box-shadow:0 1px 1px rgba(0,0,0,.04)}
.popup .chev{width:12px;height:12px;color:var(--text-2)}
.popup .landscape,.popup.wide .portrait{display:none}
.popup.wide .landscape{display:block}
.actions{display:flex;justify-content:flex-end;align-items:center;gap:6px;min-width:0}
.icon{width:32px;height:32px;display:grid;place-items:center;border-radius:var(--r-sm);border:0;background:transparent;color:var(--text-2)}
.icon[aria-pressed=true]{color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,transparent)}

/* size menu: anchor to the trigger, not the full toolbar column. */
.display-picker{position:relative;min-width:0;max-width:100%}
.menu{position:absolute;top:calc(100% + 5px);left:0;z-index:5;width:min(288px,calc(100vw - 28px));max-height:calc(100dvh - 80px);overflow-y:auto;padding:6px;border-radius:12px;background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-2);box-shadow:var(--shadow);
  opacity:0;visibility:hidden;transform:scale(.98);transform-origin:0 0;transition:opacity .12s ease-out,transform .12s ease-out,visibility 0s linear .12s}
.menu.open{opacity:1;visibility:visible;transform:scale(1);transition-delay:0s}
.option{display:grid;grid-template-columns:18px minmax(0,1fr) auto;align-items:center;gap:8px;width:100%;height:34px;padding:0 10px 0 8px;border:0;border-radius:7px;background:none;text-align:left;transition:none}
.menu .option:active{transform:none}
/* The fill follows the keyboard (focus-visible) and the pointer (hover); a menu opened by pointer
   or touch focuses the selected row without it, and the check mark alone shows the selection. */
.option:focus{outline:none}
.option:focus-visible{background:var(--accent-fill);color:var(--on-accent)}
.option small{color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums}
.option:focus-visible small{color:inherit}
.check{width:14px;height:14px;stroke-width:2.2;visibility:hidden}
.option[aria-selected=true] .check{visibility:visible}
.menu hr{border:0;border-top:1px solid var(--line);margin:5px 6px}
.menu .note{margin:0;padding:6px 10px 4px;color:var(--text-2);font-size:11.5px;line-height:1.35}

/* stage: the stream already contains the device corners; never mask screen pixels. */
.stage{grid-area:stage;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-width:0;min-height:0;padding:28px 24px 96px;overflow:hidden}
.device{position:relative;flex:none;box-sizing:content-box;aspect-ratio:9/19.5;padding:9px;border-radius:12px;background:var(--bezel);box-shadow:var(--device-shadow);touch-action:none;
  width:min(calc(100% - 18px),calc((100vh - 224px) * var(--video-ratio)));width:min(calc(100% - 18px),calc((100dvh - 224px) * var(--video-ratio)))}
.surface{position:absolute;inset:9px;display:block;width:calc(100% - 18px);height:calc(100% - 18px);object-fit:contain;border-radius:0;background:#000;
  user-select:none;-webkit-user-select:none;-webkit-user-drag:none;touch-action:none;cursor:crosshair;outline:none}
.surface:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.caption{margin:0;min-height:17px;color:var(--text-2);font-size:12px;font-variant-numeric:tabular-nums;text-align:center}
.notice{position:absolute;top:12px;left:50%;z-index:3;transform:translateX(-50%);width:max-content;max-width:calc(100% - 32px);margin:0;padding:6px 12px;border-radius:12px;
  background:var(--material);border:1px solid var(--line);box-shadow:var(--shadow);font-size:12px;text-align:center;overflow-wrap:anywhere}
.notice:empty{display:none}
.dock{position:absolute;left:50%;bottom:22px;z-index:3;transform:translateX(-50%);display:flex;align-items:center;gap:2px;padding:5px;border-radius:999px;background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line);box-shadow:var(--shadow)}
.dock button{width:40px;height:40px;display:grid;place-items:center;border:0;border-radius:999px;background:transparent;color:var(--text)}
.dock .div{width:1px;height:20px;margin:0 4px;background:var(--line-2)}

/* inspector */
.side{grid-area:side;border-left:1px solid var(--line);background:var(--surface);overflow:auto;padding:2px 0 16px;scrollbar-width:thin}
.inspector-head{display:none}
.group{padding:12px 14px;margin:0;border:0;border-bottom:1px solid var(--line);border-radius:0;background:var(--surface);min-width:0}
.group h2{margin:0 0 8px;font-size:12px;font-weight:600;color:var(--text);letter-spacing:-.01em}
.field{display:flex;gap:6px}
.field input{flex:1;min-width:0;height:32px;padding:0 10px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--bg)}
.field input::placeholder{color:var(--text-2);opacity:1}
.btn{height:32px;padding:0 12px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--raised);font-weight:500}
.btn.primary{background:var(--accent-fill);border-color:transparent;color:var(--on-accent)}
.btn.primary:focus-visible{outline-color:var(--text)}
.hint{margin:8px 0 0;color:var(--text-2);font-size:12px}
.keys{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
.key{height:34px;display:flex;align-items:center;justify-content:center;gap:6px;border-radius:var(--r-sm);border:0;background:var(--fill);font-weight:500}
.key svg{width:16px;height:16px}
.list{display:grid;gap:2px}
.row{display:flex;align-items:center;gap:10px;width:calc(100% + 16px);height:32px;margin:0 -8px;padding:0 8px;border:0;border-radius:var(--r-sm);background:none;text-align:left}
.row:not(:disabled):active{transform:none;background:var(--fill-2)}
.row svg{width:16px;height:16px;color:var(--text-2)}
.volume-controls{display:grid;grid-template-columns:minmax(0,1fr) 32px 32px;align-items:center;gap:4px;padding:0;margin-bottom:8px;border:0;background:none}
.volume-key{display:grid;place-items:center;width:32px;height:28px;padding:0;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
.volume-key svg{width:18px;height:18px}
.device-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px}
.device-tile{display:flex;align-items:center;justify-content:flex-start;gap:6px;min-width:0;min-height:32px;padding:6px;border:0;border-radius:6px;background:var(--fill);font-size:12px;text-align:left;line-height:1.3}
.device-tile svg{width:16px;height:16px;color:var(--text-2);flex-shrink:0}
.device-tile:last-child{grid-column:1/-1;flex-direction:row;min-height:32px}
.input-control summary{cursor:pointer;color:var(--text-2);font-size:12px;font-weight:600;min-height:24px;line-height:24px}
.input-control .owner{margin-top:12px}
.volume-controls + .input-control{margin-top:14px}
.owner{display:flex;align-items:flex-start;gap:8px;margin:0;color:var(--text-2)}
.owner .dot{margin-top:5px}
.owner strong{color:var(--text);font-weight:600}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
dl{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:5px 10px}
dt{color:var(--text-2)}
dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dd code{font:12px var(--mono)}
.linkbtn{margin-top:12px;padding:0;border:0;background:none;color:var(--accent);font-weight:500}
.inspector-stream{position:relative}
.inspector-stream h2{min-height:24px;padding-right:32px;display:flex;align-items:center}
.inspector-reconnect{position:absolute;top:8px;right:10px}
#stream-details>dl{margin-top:8px}
.diag{margin-top:8px}
.diag summary{width:max-content;cursor:pointer;color:var(--text-2);font-size:12px}
.status{margin:8px 0 0;font:11.5px/1.45 var(--mono);color:var(--text-2);white-space:pre-wrap;overflow-wrap:anywhere}

@media (hover:hover){
  .option:hover{background:var(--accent-fill);color:var(--on-accent)}
  .option:hover small{color:inherit}
  .popup:not(:disabled):hover,.icon:hover,.btn:not(.primary):not(:disabled):hover,.row:not(:disabled):hover{background:var(--fill)}
  .icon:hover{color:var(--text)}
  .volume-key:not(:disabled):hover,.device-tile:not(:disabled):hover,.key:not(:disabled):hover,.dock button:not(:disabled):hover{background:var(--fill-2)}
}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){:root{--material:var(--raised)}}
@media (forced-colors:active){.option:focus-visible{outline:2px solid CanvasText}}

/* Narrow desktop window: overlay panels retain the same compact controls. */
@media (max-width:760px){
  body{display:block;height:auto;min-height:100%}
  .bar{position:sticky;top:0;grid-template-columns:minmax(0,1fr) auto;min-height:calc(52px + env(safe-area-inset-top));padding:env(safe-area-inset-top) 12px 0}
  .actions .icon{display:none}
  .actions #inspector-toggle{display:grid}
  .center{justify-content:flex-start}
  .center .popup{min-width:0;max-width:100%}
  #display-label{min-width:0;overflow:hidden;text-overflow:ellipsis}
  .menu{width:min(320px,calc(100vw - 24px))}
  .stage{padding:16px 16px 92px;overflow:visible}
  .device{padding:7px;border-radius:10px;width:min(calc(100% - 14px),calc((100svh - 208px) * var(--video-ratio)))}
  .surface{inset:7px;width:calc(100% - 14px);height:calc(100% - 14px);border-radius:0}
  .notice{position:fixed;top:calc(60px + env(safe-area-inset-top))}
  .dock{position:fixed;bottom:calc(14px + env(safe-area-inset-bottom));z-index:4}
  .side{position:fixed;inset:60px 8px 8px auto;width:var(--canvas-panel-width);z-index:6;border:1px solid var(--line-2);border-radius:12px;padding:0 0 8px;overflow:auto;overscroll-behavior:contain;box-shadow:var(--sheet-shadow)}
  .inspector-head{position:sticky;top:0;z-index:1;min-height:55px;display:flex;align-items:center;justify-content:space-between;padding:8px 12px 8px 16px;background:var(--surface);border-bottom:1px solid var(--line)}
  body:not(.no-inspector){overflow:hidden}
  body.no-inspector .side{display:none}
}
@media (max-width:360px){.dock{gap:0;padding:2px}.dock .div{display:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}button:active{transform:none!important}}
${toolsStyles()}${extension("styles")}${embed ? EMBED_STYLES : ""}
</style>
</head>
<body>
<header class="bar">
  <div class="center">
    ${displayPicker(context)}
  </div>
  <div class="actions">
    ${embed ? "" : themeControl()}${toolsButton()}${extension("button")}
    ${iconButton("inspector", "Inspector", 'id="inspector-toggle" aria-pressed="true" aria-controls="inspector"', "icon")}
  </div>
</header>
<main class="stage">
  <p class="notice" id="notice" role="status" aria-live="polite"></p>
  <div class="device" id="device"><canvas id="video" class="surface" tabindex="0" aria-label="${platform} device screen" hidden></canvas><img id="screen" class="surface" tabindex="0" alt="${platform} device screen"></div>
  <p class="caption" id="caption">Connecting…</p>
  <nav class="dock" aria-label="Device buttons">
    ${iconButton("back", "Back", `data-key="KEYCODE_BACK" data-system="back"${androidOnly}`)}
    ${iconButton("home", "Home", 'data-key="KEYCODE_HOME" data-system="home"')}
    ${iconButton("recent", "Recent apps", `data-key="KEYCODE_APP_SWITCH" data-system="app-switch"${androidOnly}`)}
    <span class="div" aria-hidden="true"${androidOnly}></span>
    ${extension("dock", extensionContext)}
    ${iconButton("rotate", "Rotate", `data-system="rotate" data-scrcpy${androidOnly}`)}
    ${embed ? iconButton("volumeDown", "Volume down", `data-system="volume-down"${volume}`) + iconButton("volumeUp", "Volume up", `data-system="volume-up"${volume}`) : ""}
    ${iconButton("power", "Power", 'data-key="KEYCODE_POWER" data-system="power"')}
  </nav>
</main>
<aside class="side" id="inspector" aria-label="Inspector">
  <div class="inspector-head"><strong>Inspector</strong>${iconButton("close", "Close inspector", 'id="inspector-close"', "icon")}</div>
  <section class="group">
    <div class="field"><input id="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Text to type" aria-label="Text to type"><button type="button" id="sendText" class="btn primary">Send</button></div>
    <p class="hint" id="text-hint" hidden>Any language. Longer text is pasted through the device clipboard.</p>
  </section>
  <section class="group"${androidOnly}>
    <h2>Device</h2>
    ${android && !embed ? volumeControls : ""}
    <div class="device-grid">
      <button type="button" class="device-tile" data-key="KEYCODE_WAKEUP" data-system="wake">${icon("wake")}Wake screen</button>
      <button type="button" class="device-tile" data-system="notifications" data-scrcpy>${icon("notifications")}Notifications</button>
      <button type="button" class="device-tile" data-system="quick-settings" data-scrcpy>${icon("quickSettings")}Quick settings</button>
      <button type="button" class="device-tile" data-system="collapse" data-scrcpy>${icon("collapse")}Collapse panels</button>
      <button type="button" class="device-tile" id="clipboard" data-scrcpy aria-label="Copy device clipboard" title="Copy device clipboard">${icon("clipboard")}Copy clipboard</button>
    </div>
  </section>
  <section class="group">
    ${!android && !embed ? volumeControls : ""}
    <details class="input-control">
    <summary>Input control</summary>
    <p class="owner"><i class="dot" id="owner-dot" aria-hidden="true"></i><span><strong id="owner-name">Shared</strong> <span id="owner-detail">· people and agents can send input</span></span></p>
    <div class="pair"><button type="button" class="btn" id="control-take" disabled>Take control</button><button type="button" class="btn" id="control-pause" disabled>Pause input</button></div>
    </details>
  </section>
  <section class="group inspector-stream">
    <h2>Stream</h2>
    ${iconButton("rotate", "Reconnect stream", 'id="refresh"', "icon inspector-reconnect")}
    <dl>
      <dt>Frames</dt><dd id="stream-frames">—</dd>
      <dt>Video</dt><dd id="stream-video">—</dd>
    </dl>
    <details class="diag" id="stream-details"><summary>Details</summary>
      <dl>
        <dt>Transport</dt><dd id="stream-transport">connecting</dd>
        <dt>Codec</dt><dd><code id="stream-codec">—</code></dd>
        <dt>Display</dt><dd id="stream-display">—</dd>
        <dt>Clients</dt><dd id="stream-clients">—</dd>
      </dl>
      <details class="diag"><summary>Diagnostics</summary><pre class="status" id="status">Connecting…</pre></details>
    </details>
  </section>
</aside>
${toolsMarkup({ platform: context.options.platform })}${extension("markup", extensionContext)}
<script>
${pageScript({ base, embed, deviceId: context.id ?? null })}
${toolsScript()}${extension("script")}
</script>
</body>
</html>`;
}

/**
 * The page's own code. It is plain JavaScript inside this template literal, so it
 * avoids backticks, and escapes such as newlines are written with a doubled backslash.
 */
function pageScript({ base = "", embed = false, deviceId = null } = {}) {
  return `"use strict";
const BASE=${scriptString(base)},EMBED=${embed ? "true" : "false"},DEVICE_ID=${scriptString(deviceId)};
const fragmentParams=new URLSearchParams(location.hash.slice(1));
const bootstrapToken=fragmentParams.get("token")||"";
history.replaceState(null,"",location.pathname+location.search);
const KEYCODES=${JSON.stringify(ANDROID_KEYCODES)};
const US_KEY_CHARS=${JSON.stringify(US_KEY_CHARS)};
const META_STATE=${JSON.stringify(META_STATE)};
const MAX_CONTROL_BYTES=${MAX_CONTROL_MESSAGE_BYTES};
${metaStateFor.toString()}
${keyInputFor.toString()}
const $=id=>document.getElementById(id);
const image=$("screen"),video=$("video"),device=$("device"),statusEl=$("status"),textInput=$("text"),sizePicker=$("display-preset");
const sizeMenu=$("display-menu"),sizeLabel=$("display-label"),defaultSize=$("display-default-size"),sendButton=$("sendText");
const ui={inspector:$("inspector-toggle"),caption:$("caption"),notice:$("notice"),hint:$("text-hint"),ownerDot:$("owner-dot"),ownerName:$("owner-name"),ownerDetail:$("owner-detail"),take:$("control-take"),pause:$("control-pause"),transport:$("stream-transport"),codec:$("stream-codec"),frames:$("stream-frames"),videoSize:$("stream-video"),display:$("stream-display"),clients:$("stream-clients")};
const PRESET_LABELS=${JSON.stringify(Object.fromEntries(DISPLAY_PRESETS.map(({ id, label }) => [id, label])))};
const WIDE_PRESETS=${JSON.stringify(DISPLAY_PRESETS.filter(({ width, height }) => width > height).map(({ id }) => id))};
const OWNERS={shared:["Shared","people and agents can send input"],human:["People","agents cannot send input"],agent:["Agent","this page cannot send input"]};
const NOTICE_MS=8000;
const sizeOptions=[...document.querySelectorAll("[data-preset]")];
// Every device input control; the clipboard read, handoff and Reconnect act for anyone.
const inputControls=[...document.querySelectorAll("[data-system],[data-code],[data-key]"),textInput,sendButton];
const ctx=video.getContext("2d");
let csrf=null,logicalDisplay=null,reconnectTimer=null,pointer=null,displayBusy=false,refocusPicker=false,menuOpen=false,noteAt=0,frameRatio=0,rotation=0;
const view={transport:null,mode:null,reason:"",status:null,owner:"shared",paused:false,session:"idle",clients:null,width:0,height:0,note:"",rtt:null,preset:null,density:null,clipboard:false,rotateBusy:false,lease:null,sessionStopped:false};
const stats={decoder:"none",codec:null,framesDecoded:0,framesRendered:0,framesDropped:0,packets:0,bytes:0,errors:0,renderTimes:[],lastFrameAt:0};
let videoSocket=null,controlSocket=null,decoder=null,config=null,waitingKey=true,drawScheduled=false,configuring=false,pendingChunks=[];
const MAX_QUEUED_FRAMES=2,frameQueue=[];
let videoRetry=500,controlRetry=500,videoRefusals=0;
// A fallback the page keeps (no VideoDecoder, a codec it rejects, the video WebSocket refused).
// Any other multipart start (a /status that failed, a transport not fast yet) gives way to
// WebCodecs once the transport is fast: a tile whose first poll failed while its Simulator
// was still booting otherwise kept the multipart stream and never opened /ws/video.
let fallbackKept=false;
const supportedCodecs=new Map(),activePointers=new Map(),heldKeys=new Map();
function setStatus(value){statusEl.textContent=value}
function url(path,cacheBust=false){const params=new URLSearchParams();if(cacheBust)params.set("ts",String(Date.now()));const query=params.toString();return BASE+path+(query?"?"+query:"")}
function wsUrl(path){const params=new URLSearchParams();if(csrf)params.set("csrf",csrf);params.set("origin","human");const query=params.toString();return(location.protocol==="https:"?"wss://":"ws://")+location.host+BASE+path+(query?"?"+query:"")}
async function post(path,body){const headers={"Content-Type":"application/json","X-Autonom-Origin":"human"};if(csrf)headers["X-Autonom-CSRF"]=csrf;const response=await fetch(url(path),{method:"POST",headers,body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.error||response.statusText);return payload}
function scrcpyActive(){return view.transport==="scrcpy"}
// Video over /ws/video and input over /ws/control: scrcpy on Android, idb on the iOS Simulator.
function fastActive(){return view.transport==="scrcpy"||view.transport==="idb"}
function iosFast(){return view.transport==="idb"}
function surface(){return view.mode==="webcodecs"?video:image}
function restart(){clearTimeout(reconnectTimer);image.src=url("/stream.mjpeg",true)}
function note(message){view.note=message;noteAt=Date.now()}
function setText(element,value){if(element&&element.textContent!==value)element.textContent=value}
function setState(element,state){if(element&&element.dataset.state!==state)element.dataset.state=state}

// Multipart path (and HTTP input when the transport is not scrcpy).
// HTTP input names the display size its points were computed for once the page knows one,
// so the Canvas moves them to a size changed meanwhile, before the next status poll here.
function sentSize(){return logicalDisplay&&logicalDisplay.width&&logicalDisplay.height?{dw:logicalDisplay.width,dh:logicalDisplay.height}:null}
function point(event){const rect=image.getBoundingClientRect(),nw=image.naturalWidth,nh=image.naturalHeight;if(!nw||!nh)return null;const sent=sentSize(),ratio=Math.min(rect.width/nw,rect.height/nh),rw=nw*ratio,rh=nh*ratio,xoff=(rect.width-rw)/2,yoff=(rect.height-rh)/2,px=(event.clientX-rect.left-xoff)/ratio,py=(event.clientY-rect.top-yoff)/ratio,dw=sent?sent.dw:nw,dh=sent?sent.dh:nh,x=Math.round(px*dw/nw),y=Math.round(py*dh/nh);if(x<0||y<0||x>dw||y>dh)return null;return sent?{x,y,...sent}:{x,y}}
image.addEventListener("load",()=>note("video connected"));image.addEventListener("error",()=>{if(view.mode!=="multipart")return;note("stream reconnecting");reconnectTimer=setTimeout(restart,600)});
function startMultipart(reason,keep=false){if(keep)fallbackKept=true;view.mode="multipart";view.reason=reason||"";stats.decoder="multipart";closeVideoSocket();if(decoder&&decoder.state!=="closed")decoder.close();decoder=null;dropQueuedFrames();video.hidden=true;image.hidden=false;restart()}

// Fast video (scrcpy, idb): WebSocket packets decoded by WebCodecs and drawn on animation frames.
function startWebCodecs(){view.mode="webcodecs";view.reason="";stats.decoder="webcodecs";clearTimeout(reconnectTimer);image.removeAttribute("src");image.hidden=true;video.hidden=false;connectVideo()}
function closeVideoSocket(){const socket=videoSocket;videoSocket=null;if(socket)socket.close()}
function connectVideo(){if(videoSocket||view.mode!=="webcodecs")return;const socket=new WebSocket(wsUrl("/ws/video"));let opened=false;socket.binaryType="arraybuffer";videoSocket=socket;socket.onopen=()=>{opened=true;videoRetry=500;videoRefusals=0};socket.onmessage=event=>onVideoMessage(event.data);socket.onclose=()=>{if(videoSocket!==socket)return;videoSocket=null;waitingKey=true;if(!opened&&++videoRefusals>=4){fallback("the video WebSocket was refused");return}if(view.mode==="webcodecs"&&fastActive()){setTimeout(connectVideo,videoRetry);videoRetry=Math.min(5000,videoRetry*2)}}}
function onVideoMessage(data){if(typeof data==="string"){try{applyState(JSON.parse(data))}catch{}return}const bytes=new Uint8Array(data);if(!bytes.length)return;const view8=new DataView(data);if(bytes[0]===1&&bytes.length>=9){setVideoSize(view8.getUint32(1),view8.getUint32(5));waitingKey=true}else if(bytes[0]===2){configure(bytes.slice(1))}else if(bytes[0]===3&&bytes.length>=10){stats.packets+=1;stats.bytes+=bytes.length-10;decode((bytes[1]&1)===1,Number(view8.getBigUint64(2)),bytes.subarray(10))}}
function setVideoSize(width,height){if(!width||!height)return;view.width=width;view.height=height;const size=upright(width,height);if(video.width!==size.width||video.height!==size.height){video.width=size.width;video.height=size.height}fitFrame(size.width,size.height)}
// The iOS picture stays portrait when the Simulator rotates: the canvas holds it turned upright,
// so pointer positions on the canvas are already in the orientation the Simulator uses.
function upright(width,height){return rotation%180?{width:height,height:width}:{width,height}}
function setRotation(value){const next=[0,90,180,270].includes(value)?value:0;if(next===rotation)return;rotation=next;if(view.width)setVideoSize(view.width,view.height)}
// The device frame takes the picture's aspect; its width comes from the ratio and the room left.
function fitFrame(width,height){const ratio=width/height;if(!ratio||ratio===frameRatio)return;frameRatio=ratio;device.style.aspectRatio=width+" / "+height;device.style.setProperty("--video-ratio",String(ratio))}
function codecFromSps(data){for(let i=0;i+3<data.length;i++){if(data[i]===0&&data[i+1]===0&&data[i+2]===1&&(data[i+3]&31)===7){const out=[];let zeros=0;for(let j=i+3;j<data.length&&out.length<4;j++){const b=data[j];if(zeros>=2&&b===3){zeros=0;continue}zeros=b===0?zeros+1:0;out.push(b)}if(out.length<4)return null;return"avc1."+[out[1],out[2],out[3]].map(b=>b.toString(16).toUpperCase().padStart(2,"0")).join("")}}return null}
function fallback(reason){note(reason);startMultipart(reason,true)}
function configure(data){config=data;waitingKey=true;const codec=codecFromSps(data);if(!codec){fallback("the stream has no H.264 SPS");return}stats.codec=codec;const known=supportedCodecs.get(codec);if(known===true){applyDecoderConfig(codec);return}if(known===false){fallback("VideoDecoder rejects "+codec);return}configuring=true;VideoDecoder.isConfigSupported({codec,optimizeForLatency:true}).then(result=>{configuring=false;supportedCodecs.set(codec,Boolean(result.supported));if(!result.supported){pendingChunks=[];fallback("VideoDecoder rejects "+codec);return}applyDecoderConfig(codec);const queued=pendingChunks;pendingChunks=[];for(const item of queued)decode(item[0],item[1],item[2])}).catch(error=>{configuring=false;pendingChunks=[];fallback("VideoDecoder check failed: "+error.message)})}
function applyDecoderConfig(codec){if(view.mode!=="webcodecs")return;if(!decoder||decoder.state==="closed")decoder=new VideoDecoder({output:onFrame,error:onDecoderError});decoder.configure({codec,optimizeForLatency:true});waitingKey=true}
function decode(key,pts,data){if(configuring){if(pendingChunks.length<300)pendingChunks.push([key,pts,data]);else{pendingChunks=[];waitingKey=true}return}if(!decoder||decoder.state!=="configured"){stats.framesDropped+=1;waitingKey=true;return}if(waitingKey&&!key){stats.framesDropped+=1;return}if(!key&&decoder.decodeQueueSize>30){waitingKey=true;stats.framesDropped+=1;sendControl({t:"system",op:"keyframe"});return}let chunk=data;if(key&&config){chunk=new Uint8Array(config.length+data.length);chunk.set(config);chunk.set(data,config.length)}try{decoder.decode(new EncodedVideoChunk({type:key?"key":"delta",timestamp:pts,data:chunk}));waitingKey=false}catch(error){onDecoderError(error)}}
// Decoded frames waiting for an animation frame, on every fast transport (scrcpy and idb). They
// are shown in order: at most MAX_QUEUED_FRAMES wait, one is drawn per animation frame and another
// is asked for while frames remain, and the oldest beyond that is closed and counted as dropped.
// Two frames decoded between two refreshes are then both shown instead of losing the first.
function scheduleDraw(){if(!drawScheduled){drawScheduled=true;requestAnimationFrame(draw)}}
function dropQueuedFrames(){while(frameQueue.length)frameQueue.shift().close()}
function onFrame(frame){stats.framesDecoded+=1;frameQueue.push(frame);while(frameQueue.length>MAX_QUEUED_FRAMES){frameQueue.shift().close();stats.framesDropped+=1}scheduleDraw()}
function draw(){drawScheduled=false;const frame=frameQueue.shift();if(!frame)return;if(frameQueue.length)scheduleDraw();const size=upright(frame.displayWidth,frame.displayHeight);if(video.width!==size.width||video.height!==size.height){video.width=size.width;video.height=size.height;fitFrame(size.width,size.height)}if(rotation){ctx.setTransform(1,0,0,1,video.width/2,video.height/2);ctx.rotate(rotation*Math.PI/180);ctx.drawImage(frame,-frame.displayWidth/2,-frame.displayHeight/2,frame.displayWidth,frame.displayHeight);ctx.setTransform(1,0,0,1,0,0)}else ctx.drawImage(frame,0,0,video.width,video.height);frame.close();const now=performance.now();stats.framesRendered+=1;stats.lastFrameAt=now;stats.renderTimes.push(now);while(stats.renderTimes.length&&now-stats.renderTimes[0]>1000)stats.renderTimes.shift()}
function onDecoderError(error){stats.errors+=1;note("decoder: "+(error&&error.message||error));try{if(decoder&&decoder.state!=="closed")decoder.close()}catch{}decoder=null;waitingKey=true;if(stats.codec&&supportedCodecs.get(stats.codec))applyDecoderConfig(stats.codec);sendControl({t:"system",op:"keyframe"})}

// scrcpy control: one WebSocket carrying touch, wheel, keys, text and system actions.
function connectControl(){if(controlSocket||!fastActive())return;const socket=new WebSocket(wsUrl("/ws/control"));controlSocket=socket;socket.onopen=()=>{controlRetry=500};socket.onmessage=event=>{let message;try{message=JSON.parse(event.data)}catch{return}onControlMessage(message)};socket.onclose=()=>{if(controlSocket!==socket)return;controlSocket=null;activePointers.clear();displayBusy=false;view.rotateBusy=false;syncControls();if(fastActive()){setTimeout(connectControl,controlRetry);controlRetry=Math.min(5000,controlRetry*2)}}}
function closeControlSocket(){const socket=controlSocket;controlSocket=null;if(socket)socket.close()}
function onControlMessage(message){if(message.t==="state")applyState(message);else if(message.t==="display")onDisplay(message.display);else if(message.t==="rotation"){view.rotateBusy=false;syncControls();note(message.legacy?"Rotation requested through scrcpy":"Device rotated")}else if(message.t==="error"){stats.errors+=1;note(message.message);if(message.for==="rotate"){view.rotateBusy=false;syncControls()}if(message.for==="display"){displayBusy=false;syncPicker()}}else if(message.t==="clipboard"){if(typeof message.text!=="string"){note("the device clipboard has no text");return}textInput.value=message.text;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(message.text).catch(()=>{});note("device clipboard copied")}else if(message.t==="paste"){if(message.message)note(message.message)}else if(message.t==="pong"){view.rtt=Math.round(performance.now()-message.ts)}}
function sendControl(message){if(!controlSocket||controlSocket.readyState!==1)return false;controlSocket.send(JSON.stringify(message));return true}
function applyLease(data){if("lease" in data)view.lease=data.lease&&typeof data.lease==="object"?data.lease:null;if("session_stopped" in data)view.sessionStopped=data.session_stopped===true}
function applyState(message){view.owner=message.owner;view.paused=message.paused;view.session=message.session;view.clients=message.clients;applyLease(message);if("clipboard" in message){view.clipboard=message.clipboard===true;if(iosFast())applyHint()}if("preset" in message){view.preset=message.preset;view.density=message.density}syncPicker();if("rotation" in message)setRotation(message.rotation);if(view.mode==="webcodecs"&&message.width&&message.height)setVideoSize(message.width,message.height);if(message.transport&&message.transport!==view.transport){view.transport=message.transport;applyTransport()}}
function applyHint(){setText(ui.hint,iosFast()?(view.clipboard?"Plain ASCII up to 300 bytes is typed into the Simulator; longer or other text is set on its clipboard: touch and hold, then Paste.":"Typed into the Simulator, up to 300 bytes at a time."):scrcpyActive()?"Any language. Longer text is pasted through the device clipboard.":"Letters, digits and punctuation of plain ASCII on this transport.");textInput.title=ui.hint.textContent}
function applyTransport(){textInput.placeholder=fastActive()?"Text to type":"ASCII text to type";applyHint();for(const button of document.querySelectorAll("[data-scrcpy]"))button.hidden=!scrcpyActive();for(const button of document.querySelectorAll("[data-idb]"))button.hidden=!iosFast();if(fastActive()){connectControl();if(view.mode!=="webcodecs"&&!fallbackKept){if(typeof VideoDecoder==="function"&&typeof EncodedVideoChunk==="function")startWebCodecs();else startMultipart("VideoDecoder is not available in this browser",true)}}else{closeControlSocket();if(view.mode!=="multipart")startMultipart("")}}
// Size picker (Android): the preset in effect is selected; a choice goes the way other input goes.
function leaseMine(){return Boolean(view.lease&&view.lease.mine)}
function foreignHeld(){const lease=view.lease;return Boolean(lease&&lease.state==="held"&&!lease.mine)}
function idleText(seconds){const value=Number(seconds)||0;return value%60===0?value/60+" min":value+" s"}
function canSendInput(){return !view.paused&&(view.owner==="shared"||view.owner==="human")&&!foreignHeld()&&!view.sessionStopped}
// Called every render too: nothing already shown is set again, so an open menu stays as it is.
// A button disabled while focused drops the focus to the page; when a change chosen from it
// is answered and the button is enabled again, the focus comes back unless it moved on.
function syncPicker(){syncControls();if(!sizePicker)return;const disabled=displayBusy||!view.status||!canSendInput(),value=view.preset||"";if(sizePicker.disabled!==disabled){sizePicker.disabled=disabled;if(!disabled&&refocusPicker&&(!document.activeElement||document.activeElement===document.body))sizePicker.focus()}if(!displayBusy)refocusPicker=false;if(disabled)closeMenu(false);if(!displayBusy&&sizePicker.value!==value)sizePicker.value=value;setText(sizeLabel,!view.status?"Size":PRESET_LABELS[sizePicker.value]||"Other size");const wide=wideDisplay();if(sizePicker.classList.contains("wide")!==wide)sizePicker.classList.toggle("wide",wide);for(const option of sizeOptions){const selected=String(option.dataset.preset===sizePicker.value);if(option.getAttribute("aria-selected")!==selected)option.setAttribute("aria-selected",selected)}}
// The size button shows a landscape device for a landscape preset; for the device default or
// a size this Canvas did not set, the orientation of the size in effect.
function wideDisplay(){const value=sizePicker.value;if(value&&value!=="default"&&PRESET_LABELS[value])return WIDE_PRESETS.includes(value);return Boolean(logicalDisplay&&logicalDisplay.width>logicalDisplay.height)}
function onDisplay(display){displayBusy=false;if(display){view.preset=display.preset;view.density=display.density;if(display.width&&display.height)logicalDisplay={width:display.width,height:display.height};rememberDefaultSize(display)}syncPicker()}
// The menu shows the device's own size beside "Device default" once it has been in effect.
function rememberDefaultSize(display){if(display&&display.preset==="default")setText(defaultSize,sizeText(display.width,display.height))}
async function chooseDisplay(preset){if(!preset)return;displayBusy=true;refocusPicker=document.activeElement===sizePicker;syncPicker();if(scrcpyActive()&&sendControl({t:"display",preset}))return;try{onDisplay((await post("/display",{preset})).display)}catch(error){displayBusy=false;note(error.message);syncPicker()}}
// The Size menu is a popup listbox (PAGE-004): a click or Enter opens it, the keyboard on its
// first option (ArrowUp: the last), a pointer on the preset in effect; arrows, Home and End
// move, Enter or a click chooses, Escape, Tab or a click elsewhere closes it unchanged.
function openMenu(start){if(!sizePicker||sizePicker.disabled||menuOpen)return;menuOpen=true;sizeMenu.classList.add("open");sizePicker.setAttribute("aria-expanded","true");const selected=sizeOptions.findIndex(option=>option.dataset.preset===sizePicker.value),index=start==="last"?sizeOptions.length-1:start==="selected"&&selected>=0?selected:0;if(sizeOptions[index])sizeOptions[index].focus()}
function closeMenu(refocus){if(!menuOpen)return;menuOpen=false;sizeMenu.classList.remove("open");sizePicker.setAttribute("aria-expanded","false");if(refocus)sizePicker.focus()}
function optionOf(target){return sizeOptions.find(option=>option.contains(target))||null}
function chooseFromMenu(preset){closeMenu(true);if(!preset||preset===view.preset)return;sizePicker.value=preset;chooseDisplay(preset)}
function onMenuKey(event){const index=sizeOptions.indexOf(event.target),moves={ArrowDown:index+1,ArrowUp:index-1,Home:0,End:sizeOptions.length-1},next=moves[event.key];if(next!==undefined){event.preventDefault();sizeOptions[Math.min(sizeOptions.length-1,Math.max(0,next))].focus()}else if(event.key==="Escape"){event.preventDefault();closeMenu(true)}else if(event.key==="Tab")closeMenu(false)}
if(sizePicker){
  sizePicker.addEventListener("click",event=>{if(menuOpen)closeMenu(false);else openMenu(event.detail===0?"first":"selected")});
  sizePicker.addEventListener("keydown",event=>{if(event.key==="ArrowDown"||event.key==="ArrowUp"){event.preventDefault();openMenu(event.key==="ArrowUp"?"last":"first")}else if(event.key==="Escape")closeMenu(true)});
  sizeMenu.addEventListener("keydown",onMenuKey);
  sizeMenu.addEventListener("click",event=>{const option=optionOf(event.target);if(option)chooseFromMenu(option.dataset.preset)});
  sizeMenu.addEventListener("pointermove",event=>{const option=optionOf(event.target);if(option&&document.activeElement!==option)option.focus({preventScroll:true})});
  document.addEventListener("pointerdown",event=>{if(menuOpen&&!sizeMenu.contains(event.target)&&!sizePicker.contains(event.target))closeMenu(false)});
}
// Controls that cannot act look and are disabled (PAGE-004); handoff goes the way input goes.
function leaseOwner(){const lease=view.lease,owner=OWNERS[view.owner]||[String(view.owner),"holds control"];if(view.sessionStopped)return["Session stopped","this device's Autonom session has stopped"];if(!lease)return owner;if(foreignHeld()){const holder=lease.holder||{};return[(holder.kind==="agent"?"Agent":"Someone else")+" '"+(holder.label||"")+"' has control","Take over to use the screen"]}if(leaseMine()){const from=lease.took_over_from,idle="eleased after "+idleText(lease.idle_s)+" idle";return["You have control",from?"took over from "+(from.kind==="agent"?"agent":"someone")+" '"+(from.label||from.id||"")+"' · r"+idle:"R"+idle]}if(lease.ended_by==="idle")return[owner[0],"your control ended after "+idleText(lease.idle_s)+" idle"];if(lease.ended_by==="lost")return[owner[0],"control moved to "+(lease.holder&&lease.holder.label?lease.holder.label:"another holder")];return owner}
function takeLabel(){if(foreignHeld())return"Take over";if(leaseMine())return view.lease.took_over_from?"Hand back":"Release";return view.owner==="human"?"Release":"Take control"}
function syncControls(){const ready=Boolean(view.status)&&canSendInput(),owner=leaseOwner();for(const control of inputControls){const disabled=!ready||(control.dataset.system==="rotate"&&view.rotateBusy);if(control.disabled!==disabled)control.disabled=disabled}setText(ui.ownerName,owner[0]);setText(ui.ownerDetail,"· "+(view.paused?"input is paused":owner[1]));setState(ui.ownerDot,ready?"live":"warn");setText(ui.take,takeLabel());setText(ui.pause,view.paused?"Resume input":"Pause input");for(const button of [ui.take,ui.pause])if(button.disabled===Boolean(view.status))button.disabled=!view.status}
function setControl(mode){if(fastActive()&&sendControl({t:"control",mode}))return;post("/control",{mode}).then(result=>{view.owner=result.control_owner;view.paused=result.input_paused;applyLease(result);syncPicker()}).catch(error=>note(error.message))}
ui.take.addEventListener("click",()=>setControl(foreignHeld()?"takeover":(leaseMine()||view.owner==="human")?"release":"takeover"));
ui.pause.addEventListener("click",()=>setControl(view.paused?"resume":"pause"));
function setInspector(open,returnFocus=false){document.body.classList.toggle("no-inspector",!open);ui.inspector.setAttribute("aria-pressed",String(open));ui.inspector.title=open?"Hide inspector":"Show inspector";if(returnFocus)ui.inspector.focus()}
ui.inspector.addEventListener("click",()=>setInspector(document.body.classList.contains("no-inspector")));
$("inspector-close").addEventListener("click",()=>setInspector(false,true));
const inspectorNarrow=typeof matchMedia==="function"?matchMedia("(max-width:760px)"):null;
if(inspectorNarrow){if(inspectorNarrow.matches)setInspector(false);const resized=event=>{if(event.matches)setInspector(false)};if(inspectorNarrow.addEventListener)inspectorNarrow.addEventListener("change",resized);else if(inspectorNarrow.addListener)inspectorNarrow.addListener(resized)}
document.addEventListener("keydown",event=>{if(event.key==="Escape"&&!event.defaultPrevented&&inspectorNarrow&&inspectorNarrow.matches&&!document.body.classList.contains("no-inspector")&&!document.body.classList.contains("tools-open")&&!document.body.classList.contains("actions-open")){event.preventDefault();setInspector(false,true)}});
// iOS Safari shows :active press feedback only while the document has a touch listener.
document.addEventListener("touchstart",()=>{},{passive:true});
function clamp01(value){return Math.min(1,Math.max(0,value))}
function normalized(event,clampInside){const element=surface(),rect=element.getBoundingClientRect(),width=view.mode==="webcodecs"?video.width:image.naturalWidth,height=view.mode==="webcodecs"?video.height:image.naturalHeight;if(!width||!height||!rect.width||!rect.height)return null;const ratio=Math.min(rect.width/width,rect.height/height),rw=width*ratio,rh=height*ratio,x=(event.clientX-rect.left-(rect.width-rw)/2)/rw,y=(event.clientY-rect.top-(rect.height-rh)/2)/rh;if(clampInside)return{x:clamp01(x),y:clamp01(y)};if(x<0||y<0||x>1||y>1)return null;return{x,y}}
function pressure(event){return event.pointerType==="mouse"||!event.pressure?1:Math.min(1,event.pressure)}
// Ctrl- or Alt-drag adds a second finger mirrored around the centre: a pinch. On iOS the
// mirrored finger goes first, so the Canvas holds the pair back and sends one pinch at release.
function mirrorId(id){return -1000-id}
function sendTouch(action,id,at,pinch,p){const mirror=()=>sendControl({t:"touch",a:action,id:mirrorId(id),x:1-at.x,y:1-at.y,p});if(pinch&&iosFast())mirror();sendControl({t:"touch",a:action,id,x:at.x,y:at.y,p});if(pinch&&!iosFast())mirror()}
// iOS takes two fingers at most, sent as one pinch when either lifts.
function iosFingers(){let count=0;for(const active of activePointers.values())count+=active.pinch?2:1;return count}
function onPointerDown(event){if(foreignHeld()){note("Take over to use the screen");return}if(!fastActive()){legacyPointerDown(event);return}if(event.pointerType==="mouse"&&event.button!==0)return;const at=normalized(event,false);if(!at)return;event.preventDefault();const pinch=event.pointerType==="mouse"&&(event.ctrlKey||event.altKey);if(iosFast()&&iosFingers()+(pinch?2:1)>2){note("the iOS Simulator takes two fingers at most, sent as one pinch when one lifts");return}surface().focus();surface().setPointerCapture(event.pointerId);activePointers.set(event.pointerId,{pinch});sendTouch("down",event.pointerId,at,pinch,pressure(event))}
function onPointerMove(event){const active=activePointers.get(event.pointerId);if(!active)return;const at=normalized(event,true);if(at)sendTouch("move",event.pointerId,at,active.pinch,pressure(event))}
function onPointerEnd(event,action){const active=activePointers.get(event.pointerId);if(!active)return;activePointers.delete(event.pointerId);const at=normalized(event,true)||{x:0.5,y:0.5};sendTouch(action,event.pointerId,at,active.pinch,0)}
function legacyPointerDown(event){const p=point(event);if(!p)return;image.setPointerCapture(event.pointerId);pointer={...p,time:performance.now(),id:event.pointerId}}
// A size that changed between down and up: the start moves to the size of the end.
async function legacyPointerUp(event){if(!pointer)return;const end=point(event)||pointer,start=pointer;pointer=null;const both=Boolean(start.dw&&end.dw),sx=both?Math.round(start.x*end.dw/start.dw):start.x,sy=both?Math.round(start.y*end.dh/start.dh):start.y,size=end.dw?{dw:end.dw,dh:end.dh}:{},duration=Math.max(1,Math.round(performance.now()-start.time)),distance=Math.hypot(end.x-sx,end.y-sy);try{if(distance<12&&duration<500)await post("/tap",{x:end.x,y:end.y,...size});else await post("/swipe",{x1:sx,y1:sy,x2:end.x,y2:end.y,duration:Math.min(5000,duration),...(both?size:{})})}catch(error){note(error.message)}}
for(const element of [image,video]){
  element.addEventListener("pointerdown",onPointerDown);
  element.addEventListener("pointermove",onPointerMove);
  element.addEventListener("pointerup",event=>{if(fastActive())onPointerEnd(event,"up");else legacyPointerUp(event)});
  element.addEventListener("pointercancel",event=>{if(fastActive())onPointerEnd(event,"cancel");else pointer=null});
  element.addEventListener("contextmenu",event=>{if(fastActive())event.preventDefault()});
  element.addEventListener("wheel",onWheel,{passive:false});
  element.addEventListener("keydown",onKeyDown);
  element.addEventListener("keyup",onKeyUp);
  element.addEventListener("blur",releaseKeys);
  element.addEventListener("paste",onPaste);
}
async function onWheel(event){event.preventDefault();if(fastActive()){const at=normalized(event,false);if(!at)return;const scale=event.deltaMode===1?1/3:event.deltaMode===2?1:1/100;const dx=Math.max(-16,Math.min(16,event.deltaX*scale)),dy=Math.max(-16,Math.min(16,-event.deltaY*scale));if(dx||dy)sendControl({t:"scroll",x:at.x,y:at.y,dx,dy});return}if(!image.naturalWidth)return;const sent=sentSize(),width=sent?sent.dw:image.naturalWidth,height=sent?sent.dh:image.naturalHeight,x=Math.round(width/2),y1=Math.round(height*.55),y2=Math.round(height*(event.deltaY>0?.25:.78));try{await post("/swipe",{x1:x,y1,x2:x,y2,duration:220,...sent})}catch(error){note(error.message)}}
function metaFor(event){return metaStateFor({shiftKey:event.shiftKey,ctrlKey:event.ctrlKey,altKey:event.altKey,metaKey:event.metaKey})}
// iOS: a printable character typed on the screen goes as text; Android keycodes have no equivalent there.
function onIosKey(event){if(event.ctrlKey||event.metaKey||event.isComposing||typeof event.key!=="string"||[...event.key].length!==1)return;event.preventDefault();sendControl({t:"text",text:event.key})}
function onKeyDown(event){if(embedHotkey(event))return;if(iosFast()){onIosKey(event);return}if(!scrcpyActive())return;const input=keyInputFor(event,KEYCODES,US_KEY_CHARS);if(!input)return;event.preventDefault();if(input.text!==undefined){sendControl({t:"text",text:input.text});return}const repeat=event.repeat?(heldKeys.get(event.code)||0)+1:0;heldKeys.set(event.code,repeat);sendControl({t:"key",a:"down",code:input.code,meta:metaFor(event),repeat})}
function onKeyUp(event){if(!scrcpyActive()||!heldKeys.has(event.code))return;event.preventDefault();heldKeys.delete(event.code);sendControl({t:"key",a:"up",code:KEYCODES[event.code],meta:metaFor(event),repeat:0})}
function releaseKeys(){for(const name of heldKeys.keys()){const code=KEYCODES[name];if(code!==undefined)sendControl({t:"key",a:"up",code,meta:0,repeat:0})}heldKeys.clear()}
function onPaste(event){if(!fastActive())return;const text=event.clipboardData&&event.clipboardData.getData("text/plain");if(!text)return;event.preventDefault();if(iosFast()&&!view.clipboard){if(utf8Length(text)>300){note("the iOS Simulator takes at most 300 bytes of text at a time; nothing was sent");return}sendControl({t:"text",text});return}const message={t:"paste",text};if(!fitsControl(message,"clipboard text"))return;sendControl(message)}
function utf8Length(text){return new TextEncoder().encode(text).length}
function iosTypeable(text){return /^[\\n\\x20-\\x7e]*$/.test(text)}
function fitsControl(message,what){const size=utf8Length(JSON.stringify(message));if(size<=MAX_CONTROL_BYTES)return true;note(what+" is too large: "+size+" bytes as a control message after JSON escaping, at most "+MAX_CONTROL_BYTES+"; nothing was sent");return false}
for(const button of document.querySelectorAll("[data-system],[data-code],[data-key]")){button.addEventListener("click",()=>{if(fastActive()){if(button.dataset.system){const sent=sendControl({t:"system",op:button.dataset.system});if(button.dataset.system==="rotate"&&sent){view.rotateBusy=true;syncControls();note("Rotating device…")}}else if(button.dataset.code){const code=Number(button.dataset.code);sendControl({t:"key",a:"down",code,meta:0,repeat:0});sendControl({t:"key",a:"up",code,meta:0,repeat:0})}return}if(button.dataset.key)post("/key",{key:button.dataset.key}).catch(error=>note(error.message))})}
$("clipboard").onclick=()=>sendControl({t:"clipboard-get"});
$("refresh").onclick=()=>{if(view.mode==="webcodecs"){closeVideoSocket();waitingKey=true;videoRetry=500;connectVideo()}else restart()};
async function sendText(){const value=textInput.value;if(!value)return;if(iosFast()&&!view.clipboard){if(utf8Length(value)>300){note("the iOS Simulator takes at most 300 bytes of text at a time; nothing was sent");return}if(sendControl({t:"text",text:value}))textInput.value="";return}if(fastActive()){const message=utf8Length(value)<=300&&(!iosFast()||iosTypeable(value))?{t:"text",text:value}:{t:"paste",text:value};if(!fitsControl(message,"the text"))return;const ok=sendControl(message);if(ok)textInput.value="";return}try{await post("/text",{text:value});textInput.value=""}catch(error){note(error.message)}}
$("sendText").onclick=sendText;textInput.addEventListener("keydown",event=>{if(event.key==="Enter")sendText()});
function transportLabel(){if(!view.transport)return"connecting";if(!fastActive())return view.transport;if(view.mode==="webcodecs")return scrcpyActive()?"scrcpy (webcodecs)":view.transport+" (webcodecs)";return view.transport+" (multipart"+(view.reason?": "+view.reason:"")+")"}
function currentFps(){const now=performance.now();while(stats.renderTimes.length&&now-stats.renderTimes[0]>1000)stats.renderTimes.shift();return stats.renderTimes.length}
function render(){currentFps();const data=view.status||{},lines=["platform: "+(data.platform||"unknown"),"transport: "+transportLabel()];if(data.fallback_reason)lines.push("fallback: "+data.fallback_reason);if(view.mode==="webcodecs"){lines.push("decoder: "+(stats.codec||"waiting for config"));lines.push("fps: "+stats.renderTimes.length+" rendered, "+stats.framesDecoded+" decoded, "+stats.framesDropped+" dropped, queue "+(decoder?decoder.decodeQueueSize:0));lines.push("video: "+(view.width?view.width+"x"+view.height:"waiting")+", session "+view.session)}else{lines.push("frames: "+(data.frames_sent??0)+"\\nstream clients: "+(data.stream_clients??0))}if(view.clients)lines.push("clients: video "+view.clients.video+", control "+view.clients.control);lines.push("display: "+(data.display&&data.display.width?data.display.width+"x"+data.display.height+(view.density?" @ "+view.density:"")+(sizePicker?" ("+(view.preset||"other")+")":""):"unknown"));lines.push("control: "+view.owner+(view.paused?" (paused)":""));if(data.last_error)lines.push("last error: "+data.last_error);if(view.note)lines.push("note: "+view.note);setStatus(lines.join("\\n"));syncPicker();renderChrome()}
// The toolbar, caption, notice and Stream details; every string reaches the page as text.
function sizeText(width,height){return width&&height?width+" × "+height:""}
function displayText(){const display=logicalDisplay||{},size=sizeText(display.width,display.height);return size&&view.density?size+" · "+view.density+" dpi":size}
function liveState(){if(!view.status)return"idle";if(view.paused)return"warn";if(view.mode==="webcodecs")return view.session==="streaming"&&stats.framesRendered?"live":"idle";return view.mode==="multipart"&&image.naturalWidth?"live":"idle"}
function frameRateText(){const fps=currentFps();return !fps&&stats.framesRendered&&view.session==="streaming"?"0 fps · no new frames":fps+" fps"}
function captionText(){if(view.mode==="webcodecs")return frameRateText()+(view.rtt!=null?" · "+view.rtt+" ms":"")+" · WebCodecs";if(view.mode==="multipart")return(view.transport?view.transport+" · ":"")+"multipart stream";return"Connecting…"}
function renderChrome(){const data=view.status||{},webcodecs=view.mode==="webcodecs";setText(ui.caption,captionText());setText(ui.notice,view.note&&Date.now()-noteAt<NOTICE_MS?view.note:"");setText(ui.transport,fastActive()?view.transport+" · "+(webcodecs?"WebCodecs":"multipart"):view.transport||"connecting");setText(ui.codec,webcodecs?stats.codec||"waiting":"—");setText(ui.frames,webcodecs?currentFps()+" fps"+(stats.framesDropped?" · "+stats.framesDropped+" dropped":""):(data.frames_sent??0)+" sent");setText(ui.videoSize,(webcodecs?sizeText(view.width,view.height):sizeText(image.naturalWidth,image.naturalHeight))||"waiting");setText(ui.display,displayText()||"unknown");setText(ui.clients,view.clients?view.clients.video+" video · "+view.clients.control+" control":(data.stream_clients??0)+" stream");if(view.mode==="multipart"&&image.naturalWidth)fitFrame(image.naturalWidth,image.naturalHeight)}
async function poll(){try{let response=await fetch(url("/status"));if(response.status===401&&bootstrapToken){await authenticate();videoRefusals=0;videoRetry=500;closeVideoSocket();if(fastActive()&&typeof VideoDecoder!=="undefined")startWebCodecs();else if(view.mode==="multipart")restart();}if(response.status===401){response=await fetch(url("/status"));}const data=await response.json();if(!response.ok)throw new Error(data.error||response.statusText);view.status=data;logicalDisplay=data.display;view.owner=data.control_owner;view.paused=data.input_paused;applyLease(data);if(data.display&&"preset" in data.display){view.preset=data.display.preset;view.density=data.display.density}rememberDefaultSize(data.display);syncPicker();if(data.transport!==view.transport){view.transport=data.transport;applyTransport()}}catch(error){note(error.message);if(!view.mode)startMultipart("status unavailable")}finally{setTimeout(poll,1200)}}
function snapshot(){return{transport:view.transport,mode:view.mode,decoder:stats.decoder,fallbackReason:view.reason||null,codec:stats.codec,framesDecoded:stats.framesDecoded,framesRendered:stats.framesRendered,framesDropped:stats.framesDropped,decodeQueueSize:decoder?decoder.decodeQueueSize:0,fps:currentFps(),packets:stats.packets,bytes:stats.bytes,errors:stats.errors,lastFrameAt:stats.lastFrameAt,width:view.width,height:view.height,rotation,session:view.session,owner:view.owner,paused:view.paused,clients:view.clients,control:controlSocket?controlSocket.readyState:-1,video:videoSocket?videoSocket.readyState:-1,rtt:view.rtt,note:view.note,display:{preset:view.preset,density:view.density,picker:sizePicker?sizePicker.value:null,pickerDisabled:sizePicker?sizePicker.disabled:null},status:statusEl.textContent}}
window.autonomCanvas=Object.freeze({stats:snapshot,send:sendControl});
async function authenticate(){const body=bootstrapToken?{token:bootstrapToken}:{};const response=await fetch("/auth",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(response.ok){csrf=payload.csrf;return}if(bootstrapToken)throw new Error(payload.error||"Authentication failed");note("open the Canvas URL with its #token to sign in")}
async function bootstrap(){await authenticate();setInterval(render,500);await poll()}
bootstrap().catch(error=>{setStatus(error.message);setText(ui.notice,error.message)});
${EMBED_SCRIPT}`;
}

// A tile of the workspace page (embed mode): header and inspector hidden; the surface, the
// button row, notices and the drawers stay.
const EMBED_STYLES = "html[data-embed=\"1\"] .bar,html[data-embed=\"1\"] #inspector{display:none!important}" +
  "html[data-embed=\"1\"] body{min-height:0}";

// The tile side of the workspace messages (contract 3.7). Both sides check the origin, a tile
// also that the message comes from its parent; unknown types are ignored. Ctrl+Alt+1..4 and
// Ctrl+Alt+Left/Right go to the shell instead of the device.
const EMBED_SCRIPT = [
  "const EMBED_KEYS={Digit1:\"1\",Digit2:\"2\",Digit3:\"3\",Digit4:\"4\",ArrowLeft:\"ArrowLeft\",ArrowRight:\"ArrowRight\"};",
  "function toShell(message){if(EMBED&&window.parent!==window)window.parent.postMessage(message,location.origin)}",
  "function embedHotkey(event){if(!EMBED||!event.ctrlKey||!event.altKey)return false;const key=EMBED_KEYS[event.code];if(!key)return false;event.preventDefault();if(event.type===\"keydown\"&&!event.repeat)toShell({type:\"autonom:hotkey\",id:DEVICE_ID,key});return true}",
  "let lastStateAt=0,stateTimer=null;",
  "function tileState(){stateTimer=null;lastStateAt=Date.now();toShell({type:\"autonom:state\",id:DEVICE_ID,live:liveState()===\"live\",session:view.session,transport:view.transport,owner:view.owner,paused:view.paused,fps:stats.renderTimes.length,width:view.width,height:view.height})}",
  "function queueTileState(){if(!EMBED||stateTimer)return;stateTimer=setTimeout(tileState,Math.max(0,500-(Date.now()-lastStateAt)))}",
  "if(EMBED){",
  "window.addEventListener(\"message\",event=>{if(event.origin!==location.origin||event.source!==window.parent)return;const data=event.data;if(!data||typeof data!==\"object\")return;",
  "if(data.type===\"autonom:focus\"){document.documentElement.toggleAttribute(\"data-focused\",Boolean(data.focused));if(data.focused)(document.getElementById(\"video\").hidden?document.getElementById(\"screen\"):document.getElementById(\"video\")).focus({preventScroll:true})}",
  "else if(data.type===\"autonom:tools\"){const toggle=document.getElementById(\"tools-toggle\");if(toggle&&(toggle.getAttribute(\"aria-pressed\")===\"true\")!==Boolean(data.open))toggle.click()}",
  "else if(data.type===\"autonom:panel\"){window.dispatchEvent(new CustomEvent(\"autonom:panel\",{detail:{name:String(data.name||\"\"),open:Boolean(data.open)}}))}});",
  "document.addEventListener(\"pointerdown\",()=>toShell({type:\"autonom:focus-request\",id:DEVICE_ID}),true);",
  "document.addEventListener(\"keyup\",event=>{embedHotkey(event)},true);",
  "setInterval(queueTileState,500);",
  "toShell({type:\"autonom:ready\",id:DEVICE_ID});",
  "}",
].join("\n");


export { renderPage, pageScript };
