// The Display group of Device settings (QW-04, QW-05): Frame rate and Size of this device's
// stream. A choice is kept per device in the browser and sent as {t:"stream", fps, size} on
// the control socket, at once and again whenever the socket opens (a reload, a reconnect);
// the server applies it to this device only. A choice the server refuses is forgotten.
import { STREAM_FPS, STREAM_FPS_ANDROID, STREAM_SIZES } from "./browser-lib.mjs";
import { escapeHtml } from "./canvas-runtime.mjs";

export const DISPLAY_STORAGE_PREFIX = "autonom.canvas.display.";
// How long the page shows "Reconnecting…" after an Android change at most (QW-04: until
// frames return, at most 10 s), counted from the pick.
export const DISPLAY_RECONNECT_MS = 10_000;

/** The browser storage key of a device's choice: `autonom.canvas.display.<device id>`. */
export function displayStorageKey(id) {
  return DISPLAY_STORAGE_PREFIX + id;
}

/** The frame rates the menu offers on `platform`; 0 (On change) only on iOS. */
export function displayFpsChoices(platform) {
  return [...(platform === "ios" ? STREAM_FPS : STREAM_FPS_ANDROID)];
}

export function fpsLabel(fps) {
  return fps === 0 ? "On change" : `${fps} fps`;
}

/**
 * The stored choice in `text` (JSON {fps, size}) when `platform` may use it, else null. fps
 * null is a Size picked alone: the stream keeps its own frame rate. It is copied into the
 * page script, so it uses nothing from this module.
 */
export function parseStoredChoice(text, platform) {
  if (typeof text !== "string" || !text) return null;
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const rates = platform === "ios" ? [60, 30, 15, 0] : [60, 30, 15];
  if ((value.fps !== null && !rates.includes(value.fps)) || (value.size !== "full" && value.size !== "half")) return null;
  return { fps: value.fps, size: value.size };
}

/**
 * The Display group's markup: two selects (enhanced by the shared CanvasDropdown) and a
 * status line. An --fps outside the menu shows as a disabled entry, so the menu can show the
 * rate the stream uses without offering it.
 */
export function displayMenuMarkup(platform, options = {}) {
  const rates = displayFpsChoices(platform);
  const extra = options.fpsExplicit && Number.isInteger(options.fps) && !rates.includes(options.fps)
    ? `<option value="${options.fps}" disabled>${escapeHtml(fpsLabel(options.fps))} (--fps)</option>` : "";
  const fps = rates.map((value) => `<option value="${value}">${escapeHtml(fpsLabel(value))}</option>`).join("");
  const sizes = STREAM_SIZES.map((value) => `<option value="${value}">${value === "half" ? "Half" : "Full"}</option>`).join("");
  return `<div class="display-group" role="group" aria-labelledby="display-group-title">` +
    `<h3 class="canvas-settings-group" id="display-group-title">Display</h3>` +
    `<div class="canvas-setting"><label for="display-fps">Frame rate</label>` +
    `<select id="display-fps" aria-label="Frame rate" disabled>${extra}${fps}</select></div>` +
    `<div class="canvas-setting"><label for="display-size">Size</label>` +
    `<select id="display-size" aria-label="Stream size" disabled>${sizes}</select></div>` +
    `<p id="display-stream" class="hint" role="status"></p></div>`;
}

export function displayMenuStyles() {
  return `
.canvas-settings-group{margin:10px 0 0;font-size:12px;font-weight:600;color:var(--text-2)}
.display-group{border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:4px}
.display-group .hint:empty{display:none}
`;
}

/**
 * The page side. It runs inside the device page script (pageScript), which provides $, view,
 * stats, note, sendControl, controlSocket, PLATFORM and DEVICE_ID; the page calls
 * streamMenuOpened when the control socket opens, onStreamReply and onStreamRefused for the
 * server's answers, and syncStreamMenu on every render.
 */
export function displayMenuScript() {
  return `
const streamFps=$("display-fps"),streamSize=$("display-size"),streamLine=$("display-stream");
const STREAM_KEY=${JSON.stringify(DISPLAY_STORAGE_PREFIX)}+(DEVICE_ID||PLATFORM),STREAM_RECONNECT_MS=${DISPLAY_RECONNECT_MS};
const parseStoredChoice=${parseStoredChoice.toString()};
let streamChoice=null,streamWait=null,streamApplied=null,streamError="";
try{streamChoice=parseStoredChoice(localStorage.getItem(STREAM_KEY),PLATFORM)}catch{}
function storeStreamChoice(choice){try{if(choice)localStorage.setItem(STREAM_KEY,JSON.stringify(choice));else localStorage.removeItem(STREAM_KEY)}catch{}}
function fpsText(fps){return fps===0?"frames on change":fps+" fps"}
function streamSettingsText(settings){if(!settings)return"";const parts=[fpsText(settings.fps)];if(settings.max_size)parts.push(settings.max_size+" px on the long side");else if(settings.max_size===0)parts.push("native size");if(settings.scale_factor&&settings.scale_factor!==1)parts.push(Math.round(settings.scale_factor*100)+"% scale");return"Streaming at "+parts.join(", ")}
// A pick on Android waits for the restart (streamWait); the stored choice sent again when the
// socket opens restarts nothing unless the Canvas lost it, so it shows no wait.
function sendStreamChoice(choice,wait){if(!sendControl({t:"stream",fps:choice.fps,size:choice.size}))return false;if(wait){streamWait=PLATFORM==="android"?{at:performance.now(),left:false}:null;if(streamWait)armStreamDeadline(streamWait)}syncStreamMenu();return true}
// The notice ends at STREAM_RECONNECT_MS by a timer of its own, not at the next render after it.
function armStreamDeadline(wait){if(typeof setTimeout!=="function")return;setTimeout(()=>{if(streamWait!==wait)return;streamWait=null;syncStreamMenu();if(typeof renderChrome==="function")renderChrome()},STREAM_RECONNECT_MS)}
function streamMenuOpened(){if(streamChoice)sendStreamChoice(streamChoice,false)}
function onStreamReply(message){streamApplied=message.applied||null;syncStreamMenu()}
function onStreamRefused(message){streamChoice=null;streamWait=null;storeStreamChoice(null);streamApplied=null;streamError=message.message||"The stream settings were refused";syncStreamMenu()}
// A Size picked alone keeps the stream's own frame rate (fps null): the Frame rate select may
// show a rate the stream uses but the menu does not offer (an --fps, a background tile's 30).
function chooseStreamSetting(which){const fps=which==="fps"?Number(streamFps.value):streamChoice?streamChoice.fps:null;const choice={fps,size:streamSize.value};if(streamChoice&&streamChoice.fps===choice.fps&&streamChoice.size===choice.size)return;streamChoice=choice;streamError="";storeStreamChoice(choice);sendStreamChoice(choice,true)}
// Android restarts scrcpy for a change: Reconnecting… until frames return after the stream
// stopped, or STREAM_RECONNECT_MS passed. iOS changes the stream in place.
function streamReconnecting(){if(!streamWait)return false;const now=performance.now();if(view.session!=="streaming")streamWait.left=true;else if(streamWait.left&&stats.lastFrameAt>streamWait.at){streamWait=null;return false}if(now-streamWait.at>=STREAM_RECONNECT_MS||(!streamWait.left&&now-streamWait.at>4000)){streamWait=null;return false}return true}
function syncStreamMenu(){
  if(!streamFps||!streamSize)return;
  const ready=Boolean(controlSocket&&controlSocket.readyState===1);
  streamFps.disabled=!ready;streamSize.disabled=!ready;
  const settings=view.status&&view.status.stream_settings||null;
  const fps=streamChoice&&streamChoice.fps!==null?streamChoice.fps:settings?settings.fps:null;
  if(fps!==null&&fps!==undefined&&document.activeElement!==streamFps)streamFps.value=String(fps);
  if(document.activeElement!==streamSize)streamSize.value=streamChoice?streamChoice.size:"full";
  const reconnecting=streamReconnecting();
  if(reconnecting)note("Reconnecting…");else if(view.note==="Reconnecting…")view.note="";
  if(streamLine)streamLine.textContent=streamError||(reconnecting?"Reconnecting…":!ready?"Available while the scrcpy or idb stream runs.":streamSettingsText(settings||streamApplied));
}
if(streamFps&&streamSize){streamFps.addEventListener("change",()=>chooseStreamSetting("fps"));streamSize.addEventListener("change",()=>chooseStreamSetting("size"))}
`;
}
