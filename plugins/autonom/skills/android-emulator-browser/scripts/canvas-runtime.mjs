/** Shared Canvas limits, response encoding and command logging. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { CONTROL_MESSAGE_TYPE } from "./scrcpy-lib.mjs";
import { DEFAULT_VIDEO_OPTIONS } from "./ios-idb-companion.mjs";

export const execFileAsync = promisify(execFile);
export const BOUNDARY = "autonom-frame";

// The most unsent bytes the Canvas holds per client: past it a video client drops
// packets until a key frame, and a control client that does not read its replies is closed.
export const CLIENT_BACKLOG_BYTES = 4 * 1024 * 1024;
// Any control message can change the state that goes to every client, and a state
// message is a snapshot: a client with this much unsent data gets no new one, only the
// current state once its socket drains or ahead of the next packet or reply it is sent
// (catchUpState). Many small messages cost far more memory than their bytes, so this
// bound is small.
export const STATE_BACKLOG_BYTES = 64 * 1024;
// Backpressure instead of buffering: a control connection stops reading new messages
// while the device has not read this many control bytes, while the connection has not
// read this many reply bytes, or while this many of its journal records are not yet
// answered by the bridge (or the Canvas journal is full). It reads again once each is
// back under half its bound.
export const DEVICE_BACKLOG_PAUSE_BYTES = 256 * 1024;
export const REPLY_BACKLOG_PAUSE_BYTES = 256 * 1024;
export const JOURNAL_CLIENT_PAUSE_RECORDS = 64;
// Journal records not yet answered by the bridge, per Canvas. Every completed action is
// its own record: the bridge contract has no way to fold repeats or to journal a gap.
// Input pauses once this many are pending, so only a record that finds the journal
// already full, such as a pointer lifted by a takeover during a flood, is dropped;
// /status counts those.
export const JOURNAL_QUEUE_RECORDS = 256;
// Of those, records written to the bridge before its answers come back. The bridge
// handles its lines one by one, so order holds and the round trip stops being the limit.
export const JOURNAL_IN_FLIGHT = 16;
// Messages parsed from the same socket read as the one that paused a connection wait
// for it to read again. One read holds far less than this, so passing it means pausing
// did not stop the input: the connection is then closed with 1013 (try again later).
export const HELD_INPUT_BYTES = 1024 * 1024;
// What a held message costs besides its text, so many tiny messages count too.
export const HELD_MESSAGE_OVERHEAD_BYTES = 64;
// A held queue drops the slots of messages it already handled once this many pile up in
// front (or they are half of it), so a connection that always keeps one message held,
// paste after paste, cannot grow it without bound.
export const HELD_COMPACT_SLOTS = 64;
// The bridge refuses a record with a value outside these ranges (record_summary in
// scripts/autonom_canvas_bridge.py), so a completed action is clamped to them instead of
// going unrecorded.
export const RECORD_MAX_POINTERS = 100;
export const RECORD_MAX_COUNT = 1_000_000;
export const RECORD_MAX_DURATION_MS = 86_400_000;
export const RECORD_MAX_SCROLL = 1_000_000;
export const CLOSE_TRY_AGAIN_LATER = 1013;
// Packets since the last key frame, replayed to a client that joins late.
export const KEY_FRAME_CACHE_BYTES = 8 * 1024 * 1024;
// A multipart client this far behind skips frames instead of buffering them.
export const MULTIPART_BACKLOG_BYTES = 2 * 1024 * 1024;
export const SESSION_IDLE_STOP_MS = 15_000;
export const SHUTDOWN_STOP_MS = 3000;
// How long a stopping Canvas waits for session starts still running, so it can end them.
export const SHUTDOWN_SESSION_START_WAIT_MS = 20_000;
export const SCROLL_BURST_IDLE_MS = 400;
// scrcpy-server answers GET_CLIPBOARD only when the clipboard holds text, so a
// request that hears nothing in this time is answered as empty.
export const CLIPBOARD_ANSWER_MS = 2000;
// scrcpy-server acknowledges a SET_CLIPBOARD with a sequence (ACK_CLIPBOARD) once it has
// set the clipboard and injected the paste key, before the app has handled that key, and
// the app reads the clipboard only then. So no other device input is written until the
// ack and this settle delay, or, without an ack, until the timeout. The settle is a
// heuristic: an app slower than this to handle the paste key can still lose text.
export const CLIPBOARD_SETTLE_MS = 200;
export const CLIPBOARD_ACK_TIMEOUT_MS = 1000;
// While only that wait holds input, a connection is still read, so input from several
// connections keeps the order it arrived in, until this much of it waits; then its socket
// is paused like for any other bound.
export const CLIPBOARD_HOLD_READ_BYTES = 64 * 1024;
// scrcpy-server ignores a positional event made for another video size than its current
// one. An up written to the device this long before the Canvas learns of a new size (a
// rotation), or not written yet, may be ignored, so it is sent again with the new size.
export const RECENT_UP_MS = 1000;
// Pointer ids of a connection the Canvas lifted itself, kept to refuse their later moves.
export const LIFTED_POINTERS_MAX = 64;
export const SCREENRECORD_PROBE_MS = 5000;
export const VIDEO_CLIENT_MAX_MESSAGE_BYTES = 1024;
// Sec-Fetch-Dest values of a page loaded into another page; the page refuses all of them.
export const FRAME_DESTINATIONS = new Set(["iframe", "frame", "embed", "object", "fencedframe"]);
export const VIDEO_MESSAGE = Object.freeze({ SESSION: 1, CONFIG: 2, PACKET: 3 });
// Printable ASCII goes through INJECT_TEXT; anything else needs the clipboard (DEC-005).
export const INJECTABLE_TEXT = /^[\t\n\x20-\x7e]+$/;
export const SYSTEM_KEYS = Object.freeze({
  home: 3,
  "app-switch": 187,
  power: 26,
  "volume-up": 24,
  "volume-down": 25,
  wake: 224,
});
export const SYSTEM_PANELS = Object.freeze({
  notifications: CONTROL_MESSAGE_TYPE.EXPAND_NOTIFICATION_PANEL,
  "quick-settings": CONTROL_MESSAGE_TYPE.EXPAND_SETTINGS_PANEL,
  collapse: CONTROL_MESSAGE_TYPE.COLLAPSE_PANELS,
  rotate: CONTROL_MESSAGE_TYPE.ROTATE_DEVICE,
});
// Android display presets (DISPLAY-001), from the SDK device profiles small_phone,
// pixel_fold and pixel_tablet and the published Pixel 11 display. `default` resets the
// target to its own size and density. The order is the order pages and /status show.
export const DISPLAY_PRESETS = Object.freeze([
  { id: "small", label: "Small phone", width: 720, height: 1280, density: 320 },
  { id: "pixel-11", label: "Pixel 11", width: 1080, height: 2424, density: 420 },
  { id: "pixel-fold", label: "Pixel Fold (open)", width: 2208, height: 1840, density: 420 },
  { id: "tablet", label: "Tablet", width: 2560, height: 1600, density: 320 },
  { id: "default", label: "Device default" },
].map((preset) => Object.freeze(preset)));
export const DISPLAY_PRESET_IDS = Object.freeze(DISPLAY_PRESETS.map((preset) => preset.id));
// The presets as every state message carries them, serialized once: a flood of handoff
// changes sends one state message per change to every client.
export const DISPLAY_PRESETS_JSON = JSON.stringify(DISPLAY_PRESETS);
// Each `wm` call; the stop-time restore runs inside SHUTDOWN_STOP_MS as a whole.
export const WM_TIMEOUT_MS = 5000;
// How long a Canvas stop waits for a running Rotate before it restores the rotation.
export const ROTATION_STOP_WAIT_MS = 1000;
// /status and the state message show a `wm` reading at most this old (DISPLAY-001). Pages
// poll /status about every second, so readings are shared instead of made per request.
export const DISPLAY_READ_MAX_AGE_MS = 2000;
// State messages go to WebSocket clients, which need not ask /status: while one is
// connected the reading is renewed once it is this old, so it stays within the age above
// as long as adb answers a reading within the difference.
export const DISPLAY_WATCH_AGE_MS = 1500;
// The largest display side `dw` and `dh` of POST /tap and /swipe may name, as the journal's.
export const MAX_SENT_DISPLAY_SIDE = 10_000;
// Transports whose video and input go over /ws/video and /ws/control: scrcpy on Android,
// idb (one idb_companion H.264 stream and its HID stream) on the iOS Simulator.
export const FAST_TRANSPORTS = new Set(["scrcpy", "idb"]);
// A fast stream that sends no frame this long after it opened, or after a restart on the
// same call (a forced key frame, a profile change, the Simulator booting again), is ended
// and opened again.
export const IDB_FIRST_FRAME_MS = 10_000;
// The companion has no key frame on demand; it sends one every DEFAULT_VIDEO_OPTIONS
// .keyFrameRate seconds (DEC-004). A key frame asked for and not seen within this wait is
// forced with Stop and Start on the same stream, at most once per IDB_FORCED_KEY_FRAME_MS.
export const IDB_KEY_FRAME_WAIT_MS = 2500;
export const IDB_FORCED_KEY_FRAME_MS = 5000;
// The Simulator state is read this often while the fast stream runs, so a reboot restarts
// the frozen stream (IOSF-006); the orientation is read this often for rotated pictures.
export const IDB_SIMULATOR_WATCH_MS = 2000;
export const IDB_ORIENTATION_MS = 1000;
export const IDB_RETRY_MIN_MS = 500;
export const IDB_RETRY_MAX_MS = 5000;
// A stream that ran this long resets the restart backoff.
export const IDB_STABLE_MS = 10_000;
// The page's system buttons the Simulator has a hardware button for.
export const IOS_BUTTONS = Object.freeze({
  home: "HOME", power: "LOCK", "volume-up": "VOLUME_UP", "volume-down": "VOLUME_DOWN",
});
// Clockwise degrees that turn the Simulator's portrait picture upright for each
// orientation; the picture itself stays portrait when the Simulator rotates.
export const IOS_ROTATION = Object.freeze({ LANDSCAPE_LEFT: 270, LANDSCAPE_RIGHT: 90, PORTRAIT_UPSIDE_DOWN: 180 });
// A wheel burst is one synthetic drag: the finger moves this many points per wheel step and
// lifts once the wheel has been still IOS_WHEEL_IDLE_MS, so iOS sees no fling at the end.
// It never taps: every drag goes down at an anchor at least IOS_WHEEL_MARGIN_POINTS inside
// each edge and its first move goes at least IOS_WHEEL_MIN_POINTS in the wheel's direction,
// past the iOS tap slop (about 10 points); its points stay IOS_WHEEL_EDGE_POINTS inside the screen.
export const IOS_WHEEL_POINTS = 24;
export const IOS_WHEEL_MIN_POINTS = 16;
export const IOS_WHEEL_MARGIN_POINTS = 32;
export const IOS_WHEEL_EDGE_POINTS = 2;
export const IOS_WHEEL_IDLE_MS = 150;
// A HID write the companion has not taken within this long no longer holds the input queue;
// the HID stream still keeps its order, and the companion is restarted when it is gone.
export const IOS_HID_WRITE_MS = 5000;
export const IOS_ONE_FINGER = "The iOS Simulator takes one finger at a time: this pointer was refused while another is down";
// Two pointers of one connection are one pinch: idb has no live second finger, only a canned
// HIDPinch sent when either pointer lifts. The page's mirrored pinch finger has the id
// -1000 - id and comes before its partner, so nothing goes down before the pair is complete.
export const IOS_MIRROR_ID_MAX = -1000;
// A pinch replays at most this long on the Simulator, so a long hold cannot stall the HID
// stream; its fingers stay at least IOS_PINCH_MIN_RADIUS points from its center.
export const IOS_PINCH_MAX_S = 2;
export const IOS_PINCH_MIN_RADIUS = 8;
// The Simulator clipboard is read and set with `xcrun simctl pbpaste|pbcopy UDID`. A paste
// that fits the HID text path is also typed: at most IOS_TEXT_MAX_BYTES and only characters
// idb's `ui text` has a key for (IOS_TYPEABLE_TEXT: printable ASCII and newline; any other
// character fails with "No keycode found"). Other text is only set, and long-press Paste
// inserts it (idb Cmd+V does not paste text set on the Mac).
export const IOS_TEXT_MAX_BYTES = 300;
export const IOS_TYPEABLE_TEXT = /^[\n\x20-\x7e]*$/;
export const IOS_SIMCTL_MS = 5000;
export const IOS_CLIPBOARD_MAX_BYTES = 256 * 1024;
export const IOS_EMPTY_PASTEBOARD = "There are no items on the device's pasteboard.";
export const IOS_PASTE_INSERT = "touch and hold a text field and choose Paste to insert it";
export const IOS_PASTE_TOO_LONG = "The Simulator clipboard was set but the text is longer than " +
  `${IOS_TEXT_MAX_BYTES} bytes, so it was not typed: ${IOS_PASTE_INSERT}`;
export const IOS_PASTE_NOT_TYPEABLE = "The Simulator clipboard was set but the text has characters " +
  `other than plain ASCII, which the Simulator cannot type, so it was not typed: ${IOS_PASTE_INSERT}`;
// The stop of each device context (closeDeviceContext), so a second call waits for the first.
export const deviceClosings = new WeakMap();
// Workspace action bridge restarts (contract 3.8).
export const BRIDGE_RESTART_MIN_MS = 1000;
export const BRIDGE_RESTART_MAX_MS = 30_000;
export const BRIDGE_STABLE_MS = 60_000;
export const BRIDGE_MAX_RESTARTS = 10;

// ----- command log ------------------------------------------------------------------------------

export const NULL_END = Object.freeze({ end() {} });

export function beginLog(server, fields) {
  try {
    return server.commandLog?.begin({ device: null, ...fields }) ?? NULL_END;
  } catch {
    return NULL_END;
  }
}

export function logEvent(server, fields, end) {
  try {
    beginLog(server, fields).end(end);
  } catch {}
}

/** Run an HTTP input route and record it in the command log with its outcome. */
export async function logged(context, kind, name, origin, response, work) {
  const log = beginLog(context.server ?? {}, { device: context.id ?? null, kind, name, origin, summary: name });
  try {
    await work();
    const ok = response.statusCode < 400;
    log.end(ok ? { ok } : { ok, errorCode: "http_" + response.statusCode, error: `HTTP ${response.statusCode}` });
  } catch (error) {
    log.end({ ok: false, errorCode: "http_error", error: error.message });
    throw error;
  }
}

/** A value as a JavaScript string literal that is safe inside an inline script. */
export function scriptString(value) {
  if (value === null || value === undefined) return "null";
  return JSON.stringify(String(value)).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

export function sendJson(response, status, value, headers = {}) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": body.length,
    ...headers,
  });
  response.end(body);
}

export function sendHtml(response, html, { frameable = false } = {}) {
  const body = Buffer.from(html);
  response.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    // Another local port is the same site, so its pages share the cookie: a framed
    // Canvas would work for them and could be clickjacked. A device page (/d/<id>/) is a
    // tile of the workspace page, so only this origin may frame it.
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; " +
      (frameable ? "frame-ancestors 'self'" : "frame-ancestors 'none'"),
    "X-Frame-Options": frameable ? "SAMEORIGIN" : "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Length": body.length,
  });
  response.end(body);
}

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

export function isPng(buffer) {
  return Buffer.isBuffer(buffer) && buffer.length > 8 && buffer.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
}

export function httpError(statusCode, message) {
  const error = new Error(message);
  error.statusCode = statusCode;
  return error;
}

export function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
