#!/usr/bin/env node
import { access, constants } from "node:fs/promises";
import { ServerResponse, createServer } from "node:http";
import { homedir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { env, exit, platform } from "node:process";
import { execFile, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { promisify } from "node:util";

import {
  DEFAULT_SCRCPY_MAX_FPS,
  MAX_BODY_BYTES,
  MAX_CONTROL_MESSAGE_BYTES,
  MAX_POINTERS,
  ORIGINS,
  encodeAdbText,
  extractJpegFrames,
  generateToken,
  isAllowedHost,
  isCanvasOrigin,
  isSafeKeyCode,
  normalizeCoordinate,
  parseArgs,
  parseControlMessage,
  parseCookies,
} from "./browser-lib.mjs";
import {
  ANDROID_KEYCODES,
  CONTROL_MESSAGE_TYPE,
  COPY_KEY,
  KEY_ACTION,
  META_STATE,
  MOTION_ACTION,
  SCRCPY_PROTOCOL_VERSION,
  US_KEY_CHARS,
  encodeBackOrScreenOn,
  encodeEmpty,
  encodeGetClipboard,
  encodeKeycode,
  encodeScroll,
  encodeSetClipboard,
  encodeText,
  encodeTouch,
  keyInputFor,
  metaStateFor,
} from "./scrcpy-lib.mjs";
import {
  SCRCPY_CAPABILITY,
  SCRCPY_INSTALL_HINT,
  ScrcpySession,
  resolveScrcpyServer,
} from "./scrcpy-session.mjs";
import { CLOSE_CODE, READY_STATE, acceptWebSocket, rejectUpgrade } from "./ws.mjs";

const execFileAsync = promisify(execFile);
const BOUNDARY = "autonom-frame";

// The most unsent bytes the Canvas holds per client: past it a video client drops
// packets until a key frame, and a control client that does not read its replies is closed.
const CLIENT_BACKLOG_BYTES = 4 * 1024 * 1024;
// Any control message can change the state that goes to every client, and a state
// message is a snapshot: a client with this much unsent data gets no new one, only the
// current state once it has caught up. Many small messages cost far more memory than
// their bytes, so this bound is small.
const STATE_BACKLOG_BYTES = 64 * 1024;
// Backpressure instead of buffering: a control connection stops reading new messages
// while the device has not read this many control bytes, while the connection has not
// read this many reply bytes, or while this many of its journal records are not yet
// answered by the bridge (or the Canvas journal is full). It reads again once each is
// back under half its bound.
const DEVICE_BACKLOG_PAUSE_BYTES = 256 * 1024;
const REPLY_BACKLOG_PAUSE_BYTES = 256 * 1024;
const JOURNAL_CLIENT_PAUSE_RECORDS = 64;
// Journal records not yet answered by the bridge, per Canvas. Every completed action is
// its own record: the bridge contract has no way to fold repeats or to journal a gap.
// Input pauses once this many are pending, so only a record that finds the journal
// already full, such as a pointer lifted by a takeover during a flood, is dropped;
// /status counts those.
const JOURNAL_QUEUE_RECORDS = 256;
// Of those, records written to the bridge before its answers come back. The bridge
// handles its lines one by one, so order holds and the round trip stops being the limit.
const JOURNAL_IN_FLIGHT = 16;
// Messages parsed from the same socket read as the one that paused a connection wait
// for it to read again. One read holds far less than this, so passing it means pausing
// did not stop the input: the connection is then closed with 1013 (try again later).
const HELD_INPUT_BYTES = 1024 * 1024;
// What a held message costs besides its text, so many tiny messages count too.
const HELD_MESSAGE_OVERHEAD_BYTES = 64;
// A held queue drops the slots of messages it already handled once this many pile up in
// front (or they are half of it), so a connection that always keeps one message held,
// paste after paste, cannot grow it without bound.
const HELD_COMPACT_SLOTS = 64;
// The bridge refuses a record with a value outside these ranges (record_summary in
// scripts/autonom_canvas_bridge.py), so a completed action is clamped to them instead of
// going unrecorded.
const RECORD_MAX_POINTERS = 100;
const RECORD_MAX_COUNT = 1_000_000;
const RECORD_MAX_DURATION_MS = 86_400_000;
const RECORD_MAX_SCROLL = 1_000_000;
const CLOSE_TRY_AGAIN_LATER = 1013;
// Packets since the last key frame, replayed to a client that joins late.
const KEY_FRAME_CACHE_BYTES = 8 * 1024 * 1024;
// A multipart client this far behind skips frames instead of buffering them.
const MULTIPART_BACKLOG_BYTES = 2 * 1024 * 1024;
const SESSION_IDLE_STOP_MS = 15_000;
const SHUTDOWN_STOP_MS = 3000;
const SCROLL_BURST_IDLE_MS = 400;
// scrcpy-server answers GET_CLIPBOARD only when the clipboard holds text, so a
// request that hears nothing in this time is answered as empty.
const CLIPBOARD_ANSWER_MS = 2000;
// scrcpy-server acknowledges a SET_CLIPBOARD with a sequence (ACK_CLIPBOARD) once it has
// set the clipboard and injected the paste key, before the app has handled that key, and
// the app reads the clipboard only then. So no other device input is written until the
// ack and this settle delay, or, without an ack, until the timeout. The settle is a
// heuristic: an app slower than this to handle the paste key can still lose text.
const CLIPBOARD_SETTLE_MS = 200;
const CLIPBOARD_ACK_TIMEOUT_MS = 1000;
// While only that wait holds input, a connection is still read, so input from several
// connections keeps the order it arrived in, until this much of it waits; then its socket
// is paused like for any other bound.
const CLIPBOARD_HOLD_READ_BYTES = 64 * 1024;
// scrcpy-server ignores a positional event made for another video size than its current
// one. An up written to the device this long before the Canvas learns of a new size (a
// rotation), or not written yet, may be ignored, so it is sent again with the new size.
const RECENT_UP_MS = 1000;
// Pointer ids of a connection the Canvas lifted itself, kept to refuse their later moves.
const LIFTED_POINTERS_MAX = 64;
const SCREENRECORD_PROBE_MS = 5000;
const VIDEO_CLIENT_MAX_MESSAGE_BYTES = 1024;
// Sec-Fetch-Dest values of a page loaded into another page; the page refuses all of them.
const FRAME_DESTINATIONS = new Set(["iframe", "frame", "embed", "object", "fencedframe"]);
const VIDEO_MESSAGE = Object.freeze({ SESSION: 1, CONFIG: 2, PACKET: 3 });
// Printable ASCII goes through INJECT_TEXT; anything else needs the clipboard (DEC-005).
const INJECTABLE_TEXT = /^[\t\n\x20-\x7e]+$/;
const SYSTEM_KEYS = Object.freeze({
  home: 3,
  "app-switch": 187,
  power: 26,
  "volume-up": 24,
  "volume-down": 25,
  wake: 224,
});
const SYSTEM_PANELS = Object.freeze({
  notifications: CONTROL_MESSAGE_TYPE.EXPAND_NOTIFICATION_PANEL,
  "quick-settings": CONTROL_MESSAGE_TYPE.EXPAND_SETTINGS_PANEL,
  collapse: CONTROL_MESSAGE_TYPE.COLLAPSE_PANELS,
  rotate: CONTROL_MESSAGE_TYPE.ROTATE_DEVICE,
});

main().catch((error) => {
  console.error(`android-emulator-browser: ${error.message}`);
  exit(1);
});

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }

  const isIos = options.platform === "ios";
  if (isIos && options.transport === "scrcpy") {
    throw scrcpyError("the scrcpy transport mirrors Android targets only");
  }
  const adbPath = isIos
    ? (options.simctl ?? await findExecutable("xcrun"))
    : (options.adb ?? await findAdb());
  const serial = options.target ?? options.serial ?? (isIos
    ? await inferSingleSimulator(adbPath) : await inferSingleDevice(adbPath));
  if (isIos) await assertSimulator(adbPath, serial);
  else await assertDevice(adbPath, serial);

  let ffmpegPath = options.ffmpeg;
  if (!ffmpegPath) ffmpegPath = await findExecutable("ffmpeg").catch(() => null);
  const [scrcpy, screenrecordSupported] = await Promise.all([
    resolveScrcpy(options, isIos),
    isIos ? false : supportsScreenrecord(adbPath, serial),
  ]);
  if (options.transport === "scrcpy" && !scrcpy.available) throw scrcpyError(scrcpy.reason);
  if (options.transport === "screenrecord" && (!ffmpegPath || !screenrecordSupported)) {
    throw new Error("screenrecord transport requires device H.264 output support and ffmpeg on PATH");
  }

  const token = options.noAuth ? "" : (options.token ?? generateToken());
  const state = {
    // Monotonic, like every elapsed time here: a wall-clock step never shows in a duration.
    startedAt: performance.now(),
    framesSent: 0,
    lastFrameAt: null,
    streamClients: 0,
    acceleratedFailed: false,
    scrcpyFailed: null,
    lastError: null,
    controlOwner: "shared",
    inputPaused: false,
  };
  let inputQueue = Promise.resolve();
  const actionBridge = createActionBridge(options, adbPath, serial);

  const context = {
    options,
    adbPath,
    serial,
    ffmpegPath,
    screenrecordSupported,
    scrcpy,
    token,
    state,
    port: null,
    sessions: new Map(),
    actionBridge,
    session: null,
    sessionStopping: null,
    idleTimer: null,
    videoClients: new Set(),
    controlClients: new Set(),
    video: emptyVideoState(),
    devicePointers: new Set(),
    clipboardRequest: null,
    // The paste waiting for its ack, device writes made meanwhile, and the last sequence.
    clipboardHold: null,
    heldWrites: [],
    heldWriteBytes: 0,
    clipboardSequence: 0n,
    // Arrival order of held control messages, across connections.
    heldOrder: 0,
    // Device pointer id → the last up for it: {x, y, at}, `at` null until it is written.
    recentUps: new Map(),
    // Android keycode → the last key-up for it: {at}, `at` null until it is written.
    recentKeyUps: new Map(),
    // Pointers (device id → {x, y}) and keys down when the device server died, lifted
    // through the next server once its video size is known.
    orphans: { pointers: new Map(), keys: new Set() },
    display: null,
    journalQueue: [],
    journalDraining: false,
    journalInFlight: 0,
    journalSlot: null,
    journalDropped: 0,
    broadcaster: null,
    shuttingDown: false,
    enqueueInput(task) {
      inputQueue = inputQueue.catch(() => {}).then(task);
      return inputQueue;
    },
  };
  context.broadcaster = new FrameBroadcaster(context);

  const onRequest = (request, response) => {
    handleRequest(context, request, response).catch((error) => {
      state.lastError = error.message;
      if (!response.headersSent) {
        sendJson(response, error.statusCode ?? 500, { error: error.message });
      } else {
        response.destroy(error);
      }
    });
  };
  // Only a Canvas WebSocket handshake is an upgrade; any other request that carries an
  // Upgrade header (`Upgrade: h2c` from curl --http2 or Java's HttpClient) is plain HTTP.
  const server = createServer({ shouldUpgradeCallback: isCanvasWebSocket }, onRequest);
  server.on("upgrade", (request, socket, head) => {
    // This listener is synchronous: nothing a client sends may end the Canvas before
    // its shutdown cleanup can run.
    try {
      if (isCanvasWebSocket(request)) handleUpgrade(context, request, socket, head);
      // Node versions without shouldUpgradeCallback send every Upgrade request here.
      else serveUpgradeAsRequest(request, socket, onRequest);
    } catch (error) {
      state.lastError = error.message;
      socket.destroy();
    }
  });

  server.listen(options.port, "127.0.0.1", () => {
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : options.port;
    context.port = port;
    const fragment = token ? `#token=${encodeURIComponent(token)}` : "";
    const url = `http://127.0.0.1:${port}/${fragment}`;
    console.log(`autonom Canvas ready for ${options.platform}:${serial}`);
    console.log(`Transport preference: ${options.transport}`);
    console.log(`Transport: ${chooseTransport(context)}`);
    const reason = fallbackReason(context);
    if (reason) console.log(`Fallback reason: ${reason}`);
    console.log(`Preview at ${url}`);
    console.log(`Open this exact URL in the visible Codex side-panel browser: ${url}`);
    if (!token) console.warn("WARNING: authentication is disabled");
  });

  const shutdown = async () => {
    if (context.shuttingDown) return;
    // From here on no upgrade or request is served, so no new device server can start.
    context.shuttingDown = true;
    // Pointers are lifted now, not behind a paste that may never be acknowledged.
    dropClipboardHold(context);
    for (const client of context.controlClients) releaseInput(context, client);
    for (const client of [...context.videoClients, ...context.controlClients]) {
      client.ws.close(CLOSE_CODE.GOING_AWAY, "Canvas is stopping");
    }
    context.broadcaster.stop();
    clearTimeout(context.idleTimer);
    // The device server and the adb forward must be gone before the process is.
    await Promise.race([stopSession(context), sleep(SHUTDOWN_STOP_MS)]);
    actionBridge.close();
    server.close(() => exit(0));
    server.closeAllConnections?.();
    setTimeout(() => exit(0), 1000).unref();
  };
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, shutdown);
  }
}

function printHelp() {
  console.log(`android-emulator-browser

Token-protected Android/iOS device preview and input bridge for the Codex side-panel browser.

Usage:
  android-emulator-browser --platform android --serial <adb-serial> [options]
  android-emulator-browser --platform ios --target <simulator-udid> [options]

Options:
  --serial, -s SERIAL          Explicit adb serial.
  --platform android|ios       Canvas platform (default: android).
  --target ID                  adb serial or iOS Simulator UDID.
  --adb PATH                  adb executable path.
  --simctl PATH               xcrun executable path for iOS screenshots.
  --idb PATH                  idb executable path for iOS input.
  --ffmpeg PATH               ffmpeg executable path.
  --port, -p PORT             Localhost port (default: 3277; 0 chooses a free port).
  --transport MODE            auto, scrcpy, screenrecord, or screencap (default: auto).
                              auto prefers scrcpy on Android when a ${SCRCPY_PROTOCOL_VERSION} server is found.
  --fps FPS                   Frame rate cap (default: 15; ${DEFAULT_SCRCPY_MAX_FPS} on the scrcpy transport).
  --max-size PX               Maximum video width (default: 1280).
  --bit-rate BPS              H.264 bitrate (default: 8000000).
  --scrcpy-server PATH        scrcpy-server file to push (default: AUTONOM_SCRCPY_SERVER,
                              SCRCPY_SERVER_PATH, then the server of scrcpy on PATH).
  --scrcpy-version X.Y        Version of --scrcpy-server when its file name does not say.
  --token TOKEN               Use a supplied access token.
  --python PATH               Python executable for the persistent action bridge.
  --bridge PATH               Override autonom_canvas_bridge.py.
  --no-auth                   Disable token protection (isolated local use only).
`);
}

function scrcpyError(reason) {
  return new Error(`${SCRCPY_CAPABILITY}: --transport scrcpy is unavailable: ${reason}. ${SCRCPY_INSTALL_HINT}`);
}

async function resolveScrcpy(options, isIos) {
  if (isIos || options.transport === "screenrecord" || options.transport === "screencap") {
    return { available: false, reason: null };
  }
  const found = await resolveScrcpyServer({
    serverPath: options.scrcpyServer,
    version: options.scrcpyVersion,
    env,
    findExecutable,
  });
  return {
    available: found.ok,
    serverPath: found.serverPath ?? null,
    version: found.version ?? null,
    source: found.source,
    reason: found.ok ? null : found.reason,
  };
}

/** The request target as a URL, or null for one that is not a valid path. */
function requestUrl(request) {
  try {
    return new URL(request.url ?? "/", "http://127.0.0.1");
  } catch {
    return null;
  }
}

async function handleRequest(context, request, response) {
  // A page on another site that rebinds its own name to 127.0.0.1 still sends its own Host.
  if (!isAllowedHost(request.headers.host, context.port)) {
    sendJson(response, 403, { error: "Host not allowed" });
    return;
  }
  if (context.shuttingDown) {
    sendJson(response, 503, { error: "Canvas is stopping" });
    return;
  }
  const url = requestUrl(request);
  if (!url) {
    sendJson(response, 400, { error: "Bad request target" });
    return;
  }
  if (request.method === "POST" && url.pathname === "/auth") {
    await exchangeToken(context, request, response);
    return;
  }
  // The shell contains no device data or secret. The fragment token is
  // exchanged by its script, then removed from browser history.
  if (request.method === "GET" && url.pathname === "/") {
    if (FRAME_DESTINATIONS.has(request.headers["sec-fetch-dest"])) {
      sendJson(response, 403, { error: "The Canvas page cannot be framed" });
      return;
    }
    sendHtml(response, renderPage(context));
    return;
  }
  const authorization = authorize(context, request, url);
  if (!authorization.ok) {
    sendJson(response, 401, { error: "Unauthorized" });
    return;
  }
  if (request.method === "POST" && authorization.csrf &&
      request.headers["x-autonom-csrf"] !== authorization.csrf) {
    sendJson(response, 403, { error: "CSRF token rejected" });
    return;
  }
  const origin = normalizeOrigin(request.headers["x-autonom-origin"]);
  if (request.method === "GET" && url.pathname === "/status") {
    await sendStatus(context, response);
  } else if (request.method === "GET" && url.pathname === "/frame") {
    await sendFrame(context, response);
  } else if (request.method === "GET" && url.pathname === "/stream.mjpeg") {
    await sendStream(context, request, response);
  } else if (request.method === "GET" && url.pathname === "/stream.h264") {
    await sendH264(context, request, response);
  } else if (request.method === "POST" && url.pathname === "/tap") {
    await tap(context, response, await readJsonBody(request), origin);
  } else if (request.method === "POST" && url.pathname === "/swipe") {
    await swipe(context, response, await readJsonBody(request), origin);
  } else if (request.method === "POST" && url.pathname === "/key") {
    await key(context, response, await readJsonBody(request), origin);
  } else if (request.method === "POST" && url.pathname === "/text") {
    await text(context, response, await readJsonBody(request), origin);
  } else if (request.method === "POST" && url.pathname === "/control") {
    await control(context, response, await readJsonBody(request), origin);
  } else {
    sendJson(response, 404, { error: "Not found" });
  }
}

function bearerToken(request) {
  const authorization = request.headers.authorization ?? "";
  return authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
}

function authorize(context, request, url) {
  if (!context.token) return { ok: true, csrf: null };
  const queryToken = url.searchParams.get("token");
  if (queryToken === context.token || bearerToken(request) === context.token) {
    return { ok: true, csrf: null }; // retained for non-browser API clients
  }
  const session = context.sessions.get(parseCookies(request.headers.cookie).autonom_session);
  return session ? { ok: true, csrf: session.csrf } : { ok: false, csrf: null };
}

async function exchangeToken(context, request, response) {
  if (!context.token) {
    sendJson(response, 200, { ok: true, csrf: null });
    return;
  }
  const body = await readJsonBody(request);
  const token = body && typeof body === "object" ? body.token : undefined;
  if (token === undefined || token === "") {
    resumeSession(context, request, response);
    return;
  }
  if (token !== context.token) {
    sendJson(response, 401, { error: "Unauthorized" });
    return;
  }
  const sessionId = generateToken(18);
  const csrf = generateToken(18);
  context.sessions.set(sessionId, { csrf, createdAt: Date.now() });
  sendJson(response, 200, { ok: true, csrf }, {
    "Set-Cookie": `autonom_session=${sessionId}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`,
  });
}

/**
 * A reloaded page has its cookie but no fragment token, and it needs the session's
 * CSRF value again for POSTs and WebSocket upgrades. Only a page served by this
 * Canvas may ask: another local site shares the cookie (ports are not part of a
 * site), and could not read a cross-origin answer anyway.
 */
function resumeSession(context, request, response) {
  const session = context.sessions.get(parseCookies(request.headers.cookie).autonom_session);
  if (!session) {
    sendJson(response, 401, { error: "Unauthorized" });
    return;
  }
  if (!isSameOriginFetch(request, context.port)) {
    sendJson(response, 403, { error: "Origin not allowed" });
    return;
  }
  sendJson(response, 200, { ok: true, csrf: session.csrf });
}

/**
 * The page sends `Referrer-Policy: no-referrer`, under which browsers may send
 * `Origin: null` on its own POSTs; Sec-Fetch-Site is not affected by that policy.
 * The page asks with fetch(), so a navigation or a frame load is not the page.
 */
function isSameOriginFetch(request, port) {
  const destination = request.headers["sec-fetch-dest"];
  if (destination !== undefined && destination !== "empty") return false;
  const site = request.headers["sec-fetch-site"];
  if (site !== undefined) return site === "same-origin";
  return isCanvasOrigin(request.headers.origin, port);
}

function normalizeOrigin(value) {
  return ORIGINS.includes(value) ? value : "human";
}

/**
 * WebSocket upgrades: browsers do not apply CORS to them, so a cookie alone is never
 * enough — the page must also prove it is ours with the CSRF value and its Origin.
 * API clients use the token and may name their origin with ?origin=.
 */
function authorizeUpgrade(context, request, url) {
  const originParam = url.searchParams.get("origin");
  const apiClient = () => {
    if (originParam === null) return { ok: true, origin: "human" };
    if (!ORIGINS.includes(originParam)) {
      return { ok: false, status: 400, message: "origin must be human, agent, replay, or system" };
    }
    return { ok: true, origin: originParam };
  };
  if (!context.token) return apiClient();
  if (url.searchParams.get("token") === context.token || bearerToken(request) === context.token) {
    return apiClient();
  }
  const session = context.sessions.get(parseCookies(request.headers.cookie).autonom_session);
  if (!session) return { ok: false, status: 401, message: "Unauthorized" };
  if (url.searchParams.get("csrf") !== session.csrf) {
    return { ok: false, status: 403, message: "CSRF token rejected" };
  }
  if (!isCanvasOrigin(request.headers.origin, context.port)) {
    return { ok: false, status: 403, message: "Origin not allowed" };
  }
  return { ok: true, origin: "human" };
}

/** A WebSocket handshake for /ws/video or /ws/control: the Upgrade tokens include `websocket`. */
function isCanvasWebSocket(request) {
  const tokens = String(request.headers.upgrade ?? "").split(",").map((token) => token.trim().toLowerCase());
  if (!tokens.includes("websocket")) return false;
  const url = requestUrl(request);
  return url !== null && (url.pathname === "/ws/video" || url.pathname === "/ws/control");
}

/**
 * Answer a request that reached the 'upgrade' event without being a Canvas WebSocket
 * handshake, as on Node versions that ignore shouldUpgradeCallback, with the normal
 * handler, on a response for its socket that closes after it. Those versions skip the
 * request body of an upgrade request, so a request with a body is refused and must be
 * resent without the Upgrade header.
 */
function serveUpgradeAsRequest(request, socket, onRequest) {
  socket.on("error", () => {});
  const response = new ServerResponse(request);
  response.shouldKeepAlive = false;
  response.assignSocket(socket);
  response.on("finish", () => {
    response.detachSocket(socket);
    socket.end();
  });
  const length = request.headers["content-length"];
  if (request.headers["transfer-encoding"] !== undefined || (length !== undefined && length.trim() !== "0")) {
    sendJson(response, 400, {
      error: "This Canvas cannot read a request body sent with an Upgrade header; " +
        "resend the request without the Upgrade header",
    });
    return;
  }
  onRequest(request, response);
}

function handleUpgrade(context, request, socket, head) {
  socket.on("error", () => {});
  if (!isAllowedHost(request.headers.host, context.port)) {
    rejectUpgrade(socket, 403, "Host not allowed");
    return;
  }
  if (context.shuttingDown) {
    rejectUpgrade(socket, 503, "Canvas is stopping");
    return;
  }
  const url = requestUrl(request);
  if (!url) {
    rejectUpgrade(socket, 400, "Bad request target");
    return;
  }
  const isVideo = url.pathname === "/ws/video";
  if (!isVideo && url.pathname !== "/ws/control") {
    rejectUpgrade(socket, 404, "Not found");
    return;
  }
  // Any page that names a foreign origin is refused, whatever credential it carries.
  if (request.headers.origin !== undefined && !isCanvasOrigin(request.headers.origin, context.port)) {
    rejectUpgrade(socket, 403, "Origin not allowed");
    return;
  }
  const authorization = authorizeUpgrade(context, request, url);
  if (!authorization.ok) {
    rejectUpgrade(socket, authorization.status, authorization.message);
    return;
  }
  if (chooseTransport(context) !== "scrcpy") {
    rejectUpgrade(socket, 409, "The scrcpy transport is not active on this Canvas");
    return;
  }
  const ws = acceptWebSocket(request, socket, head, {
    maxMessageBytes: isVideo ? VIDEO_CLIENT_MAX_MESSAGE_BYTES : MAX_CONTROL_MESSAGE_BYTES,
  });
  if (!ws) return;
  if (isVideo) openVideoClient(context, ws, socket);
  else openControlClient(context, ws, authorization.origin, socket);
}

function chooseTransport(context) {
  const { options, scrcpy, state } = context;
  if (options.transport === "scrcpy") return "scrcpy";
  if (options.transport === "auto" && scrcpy.available && !state.scrcpyFailed) return "scrcpy";
  return multipartTransport(context);
}

/** The transport behind /stream.mjpeg, also used when a page cannot decode scrcpy video. */
function multipartTransport(context) {
  const { options, ffmpegPath, screenrecordSupported, state } = context;
  if (options.transport === "screencap") return "screencap";
  if (options.transport === "screenrecord") return "screenrecord";
  if (ffmpegPath && screenrecordSupported && !state.acceleratedFailed) return "screenrecord";
  return "screencap";
}

/** Why auto mode on Android is not on its first choice, or null. */
function fallbackReason(context) {
  const { options, scrcpy, state, ffmpegPath, screenrecordSupported } = context;
  if (options.transport !== "auto" || options.platform !== "android") return null;
  const chosen = chooseTransport(context);
  if (chosen === "scrcpy") return null;
  const reasons = [state.scrcpyFailed ? `scrcpy failed to start: ${state.scrcpyFailed}` : scrcpy.reason];
  if (chosen === "screencap") {
    if (!ffmpegPath) reasons.push("screenrecord needs ffmpeg on PATH");
    else if (!screenrecordSupported) reasons.push("device screenrecord has no H.264 output");
    else if (state.acceleratedFailed) reasons.push("screenrecord produced no frames");
  }
  return reasons.filter(Boolean).join("; ") || null;
}

async function sendStatus(context, response) {
  const measured = await context.actionBridge.call("screen-size", {}, "system");
  if (measured.display) context.display = measured.display;
  const transport = chooseTransport(context);
  sendJson(response, 200, {
    platform: context.options.platform,
    serial: context.serial,
    display: measured.display,
    transport,
    requested_transport: context.options.transport,
    fallback_reason: fallbackReason(context),
    ffmpeg: Boolean(context.ffmpegPath),
    screenrecord_h264: context.screenrecordSupported,
    direct_h264_url: context.screenrecordSupported ? "/stream.h264" : null,
    frames_sent: context.state.framesSent,
    last_frame_at: context.state.lastFrameAt,
    stream_clients: context.state.streamClients,
    uptime_seconds: Math.round((performance.now() - context.state.startedAt) / 1000),
    last_error: context.state.lastError,
    control_owner: context.state.controlOwner,
    input_paused: context.state.inputPaused,
    scrcpy: scrcpyStatus(context),
  });
}

function scrcpyStatus(context) {
  if (!context.scrcpy.available) return null;
  const stats = context.session?.stats;
  return {
    version: context.scrcpy.version,
    server_path: context.scrcpy.serverPath,
    source: context.scrcpy.source,
    session_state: context.session?.state ?? "idle",
    width: context.video.size?.width ?? null,
    height: context.video.size?.height ?? null,
    packets: stats?.packets ?? 0,
    bytes: stats?.bytes ?? 0,
    key_frames: stats?.keyFrames ?? 0,
    restarts: stats?.restarts ?? 0,
    video_clients: context.videoClients.size,
    control_clients: context.controlClients.size,
    journal_pending: journalPending(context),
    journal_dropped: context.journalDropped,
  };
}

async function sendFrame(context, response) {
  const stdout = await capturePng(context);
  if (!isPng(stdout)) throw httpError(502, "adb screencap did not return PNG data");
  response.writeHead(200, {
    "Content-Type": "image/png",
    "Cache-Control": "no-store",
    "Content-Length": stdout.length,
  });
  response.end(stdout);
}

async function sendStream(context, request, response) {
  response.writeHead(200, {
    "Content-Type": `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
    "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
  });
  context.state.streamClients += 1;
  try {
    await new Promise((resolvePromise) => {
      request.once("close", resolvePromise);
      response.once("close", resolvePromise);
      context.broadcaster.add(response);
    });
  } finally {
    context.broadcaster.remove(response);
    context.state.streamClients = Math.max(0, context.state.streamClients - 1);
    if (!response.writableEnded && !response.destroyed) response.end();
  }
}

async function sendH264(context, request, response) {
  if (context.options.platform !== "android" || !context.screenrecordSupported) {
    throw httpError(409, "device screenrecord does not expose H.264 output");
  }
  response.writeHead(200, {
    "Content-Type": "video/h264",
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Autonom-Transport": "annex-b",
  });
  const child = spawn(context.adbPath, [
    "-s", context.serial, "exec-out", "screenrecord", "--output-format=h264",
    "--bit-rate", String(context.options.bitRate), "-",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.pipe(response);
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr = (stderr + chunk.toString()).slice(-2000); });
  const close = () => child.kill("SIGTERM");
  request.once("close", close);
  response.once("close", close);
  await new Promise((resolvePromise, reject) => {
    child.once("error", reject);
    child.once("close", (code) => {
      if (code && !response.destroyed) context.state.lastError =
        `direct H.264 stream exited ${code}: ${stderr.trim()}`;
      resolvePromise();
    });
  });
}

function writeMultipartFrame(context, response, frame, contentType) {
  if (response.destroyed || response.writableEnded) return false;
  // A client that stopped reading skips frames instead of growing the send buffer.
  if (response.writableLength > MULTIPART_BACKLOG_BYTES) return false;
  response.write(`--${BOUNDARY}\r\nContent-Type: ${contentType}\r\nContent-Length: ${frame.length}\r\n\r\n`);
  response.write(frame);
  response.write("\r\n");
  context.state.framesSent += 1;
  context.state.lastFrameAt = new Date().toISOString();
  return true;
}

/**
 * One capture loop for every multipart client: started by the first, stopped when
 * none remain, restarted when it ends while clients still watch (screenrecord stops
 * by itself after three minutes).
 */
class FrameBroadcaster {
  #context;
  #clients = new Set();
  #running = false;
  #stopCapture = null;

  constructor(context) {
    this.#context = context;
  }

  add(response) {
    this.#clients.add(response);
    if (!this.#running) this.#run();
  }

  remove(response) {
    this.#clients.delete(response);
    if (!this.#clients.size) this.#stopCapture?.();
  }

  stop() {
    this.#clients.clear();
    this.#stopCapture?.();
  }

  #broadcast(frame, contentType) {
    for (const response of this.#clients) {
      writeMultipartFrame(this.#context, response, frame, contentType);
    }
  }

  async #run() {
    this.#running = true;
    try {
      while (this.#clients.size) {
        if (multipartTransport(this.#context) === "screenrecord") await this.#screenrecord();
        else await this.#screencap();
        if (this.#clients.size) await sleep(500);
      }
    } catch (error) {
      this.#context.state.lastError = error.message;
    } finally {
      this.#running = false;
      this.#stopCapture = null;
    }
    if (this.#clients.size) {
      setTimeout(() => {
        if (!this.#running && this.#clients.size) this.#run();
      }, 500);
    }
  }

  async #screencap() {
    const context = this.#context;
    let stopped = false;
    let wake = null;
    this.#stopCapture = () => {
      stopped = true;
      wake?.();
    };
    const pause = (milliseconds) => new Promise((resolvePromise) => {
      const timer = setTimeout(resolvePromise, milliseconds);
      wake = () => {
        clearTimeout(timer);
        resolvePromise();
      };
    });
    const fps = Math.min(10, context.options.fps);
    const interval = Math.round(1000 / fps);
    while (!stopped && this.#clients.size && multipartTransport(context) === "screencap") {
      const started = performance.now();
      try {
        const stdout = await capturePng(context);
        if (!isPng(stdout)) throw new Error("screencap returned invalid PNG");
        // Browsers accept PNG frames in a multipart image stream despite the endpoint name.
        this.#broadcast(stdout, "image/png");
      } catch (error) {
        context.state.lastError = error.message;
        await pause(500);
      }
      const remaining = interval - (performance.now() - started);
      if (remaining > 0 && !stopped) await pause(remaining);
    }
  }

  async #screenrecord() {
    const { adbPath, serial, ffmpegPath, options, state } = this.#context;
    const adb = spawn(adbPath, [
      "-s", serial,
      "exec-out", "screenrecord",
      "--output-format=h264",
      "--bit-rate", String(options.bitRate),
      "-",
    ], { stdio: ["ignore", "pipe", "pipe"] });
    const filter = `fps=${options.fps},scale=min(${options.maxSize}\\,iw):-2`;
    const ffmpeg = spawn(ffmpegPath, [
      "-hide_banner", "-loglevel", "error",
      "-f", "h264", "-i", "pipe:0",
      "-an", "-vf", filter,
      "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
    ], { stdio: ["pipe", "pipe", "pipe"] });

    adb.stdout.pipe(ffmpeg.stdin);
    ffmpeg.stdin.on("error", () => {});
    let carry = Buffer.alloc(0);
    let receivedFrame = false;
    let stderr = "";
    const keepStderr = (chunk) => { stderr = (stderr + chunk.toString()).slice(-4000); };
    adb.stderr.on("data", keepStderr);
    ffmpeg.stderr.on("data", keepStderr);
    this.#stopCapture = () => {
      adb.kill("SIGTERM");
      ffmpeg.kill("SIGTERM");
    };

    await new Promise((resolvePromise) => {
      ffmpeg.stdout.on("data", (chunk) => {
        const parsed = extractJpegFrames(chunk, carry);
        carry = parsed.carry;
        for (const frame of parsed.frames) {
          receivedFrame = true;
          this.#broadcast(frame, "image/jpeg");
        }
      });
      const failed = (error) => {
        state.lastError = error.message;
        adb.kill("SIGTERM");
        ffmpeg.kill("SIGTERM");
        resolvePromise();
      };
      ffmpeg.once("error", failed);
      adb.once("error", failed);
      ffmpeg.once("close", (code) => {
        adb.kill("SIGTERM");
        if (!receivedFrame && this.#clients.size) {
          state.acceleratedFailed = true;
          state.lastError = `accelerated stream failed${code === null ? "" : ` (exit ${code})`}: ${stderr.trim()}`;
        }
        resolvePromise();
      });
    });
  }
}

async function capturePng(context) {
  if (context.options.platform === "ios") {
    const result = await execFileAsync(context.adbPath, [
      "simctl", "io", context.serial, "screenshot", "--type=png", "-",
    ], { timeout: 8000, maxBuffer: 32 * 1024 * 1024, encoding: null });
    return result.stdout;
  }
  const { stdout } = await runAdb(context, ["exec-out", "screencap", "-p"], {
    timeout: 8000, encoding: "buffer",
  });
  return stdout;
}

function emptyVideoState() {
  return { size: null, sessionMessage: null, configMessage: null, cache: [], cacheBytes: 0 };
}

function encodeVideoSession({ width, height }) {
  const message = Buffer.alloc(9);
  message[0] = VIDEO_MESSAGE.SESSION;
  message.writeUInt32BE(width, 1);
  message.writeUInt32BE(height, 5);
  return message;
}

function encodeVideoConfig(data) {
  const message = Buffer.allocUnsafe(1 + data.length);
  message[0] = VIDEO_MESSAGE.CONFIG;
  data.copy(message, 1);
  return message;
}

function encodeVideoPacket({ keyFrame, pts, data }) {
  const message = Buffer.allocUnsafe(10 + data.length);
  message[0] = VIDEO_MESSAGE.PACKET;
  message[1] = keyFrame ? 1 : 0;
  message.writeBigUInt64BE(BigInt(pts), 2);
  data.copy(message, 10);
  return message;
}

function stateMessage(context) {
  return JSON.stringify({
    t: "state",
    owner: context.state.controlOwner,
    paused: context.state.inputPaused,
    transport: chooseTransport(context),
    width: context.video.size?.width ?? null,
    height: context.video.size?.height ?? null,
    clients: { video: context.videoClients.size, control: context.controlClients.size },
    session: context.session?.state ?? "idle",
  });
}

function broadcastState(context) {
  const message = stateMessage(context);
  for (const client of context.videoClients) sendState(client, message);
  for (const client of context.controlClients) sendState(client, message);
}

/**
 * A client behind by STATE_BACKLOG_BYTES skips state messages, so a flood of handoff
 * changes leaves at most one bound of them per client; catchUpState sends it the
 * current state when its socket drains (and a video client before its next packet).
 */
function sendState(client, message) {
  if (client.ws.bufferedAmount > STATE_BACKLOG_BYTES) {
    client.stateStale = true;
    return;
  }
  client.stateStale = false;
  client.ws.send(message);
}

function catchUpState(context, client) {
  if (client.stateStale) sendState(client, stateMessage(context));
}

/** Start the device server for the first WebSocket client; at most one per Canvas. */
function ensureSession(context) {
  clearTimeout(context.idleTimer);
  context.idleTimer = null;
  if (context.shuttingDown || context.session || chooseTransport(context) !== "scrcpy") return;
  const { options, adbPath, serial, scrcpy } = context;
  const session = new ScrcpySession({
    adbPath,
    serial,
    serverPath: scrcpy.serverPath,
    version: scrcpy.version,
    maxSize: options.maxSize,
    bitRate: options.bitRate,
    maxFps: options.fpsExplicit ? options.fps : DEFAULT_SCRCPY_MAX_FPS,
  });
  context.session = session;
  session.on("state", (name) => {
    if (name === "restarting") {
      // The server is gone, but what it injected is still down on Android: lifted through
      // the next server. The old server will not answer old requests either.
      for (const client of context.controlClients) orphanInput(context, client);
      orphanRecent(context);
      cancelClipboardRequest(context, "The scrcpy session restarted before the device answered");
      // No ack comes for a paste the old server took along: held input is refused below.
      dropClipboardHold(context);
    }
    if (name === "streaming") refreshDisplay(context);
    broadcastState(context);
    // A new or stopped control socket holds no backlog, and it never drains the old one.
    resumeAllReading(context);
  });
  session.on("control-drain", () => resumeAllReading(context));
  session.on("error", (error) => {
    context.state.lastError = error.message;
  });
  // A session being stopped may still deliver a last event; only the current one counts.
  const current = (handler) => (value) => {
    if (context.session === session) handler(context, value);
  };
  session.on("session", current(onVideoSession));
  session.on("config", current(onVideoConfig));
  session.on("packet", current(onVideoPacket));
  session.on("device-message", current(onDeviceMessage));
  const previous = context.sessionStopping ?? Promise.resolve();
  previous.then(() => {
    if (context.session !== session) return;
    session.start().catch((error) => {
      if (context.session !== session || context.options.transport !== "auto" || session.everStreamed) return;
      // auto promised a picture: fall back instead of retrying a server that never worked.
      context.state.scrcpyFailed = error.message;
      stopSession(context);
      broadcastState(context);
      for (const client of [...context.videoClients, ...context.controlClients]) {
        client.ws.close(CLOSE_CODE.NORMAL, "scrcpy is unavailable; the Canvas falls back");
      }
    });
  });
}

/** The display size puts journaled gestures in device pixels, like HTTP input. */
function refreshDisplay(context) {
  return context.actionBridge.call("screen-size", {}, "system").then((measured) => {
    if (measured?.display) context.display = measured.display;
    return context.display;
  }, () => context.display);
}

function stopSession(context) {
  const session = context.session;
  if (!session) return context.sessionStopping ?? Promise.resolve();
  context.session = null;
  for (const client of context.controlClients) forgetInput(context, client);
  cancelClipboardRequest(context, "The scrcpy session stopped before the device answered");
  dropClipboardHold(context);
  context.video = emptyVideoState();
  // A session still stopping from before counts too: shutdown waits for every forward.
  const stopping = Promise.allSettled([context.sessionStopping, session.stop()]).then(() => {
    if (context.sessionStopping === stopping) context.sessionStopping = null;
  });
  context.sessionStopping = stopping;
  return stopping;
}

function scheduleIdleStop(context) {
  if (context.videoClients.size || context.controlClients.size || !context.session) return;
  clearTimeout(context.idleTimer);
  context.idleTimer = setTimeout(() => {
    context.idleTimer = null;
    if (!context.videoClients.size && !context.controlClients.size) stopSession(context);
  }, SESSION_IDLE_STOP_MS);
  context.idleTimer.unref?.();
}

function openVideoClient(context, ws, socket) {
  // needs-key → live → dropping (backlog over the limit) → live at the next key frame.
  const client = {
    ws, mode: "needs-key", keyRequested: false, stateStale: false, pendingSession: null, pendingConfig: null,
  };
  context.videoClients.add(client);
  ws.on("message", () => {});
  socket.on("drain", () => catchUpState(context, client));
  ws.on("close", () => {
    context.videoClients.delete(client);
    broadcastState(context);
    scheduleIdleStop(context);
  });
  ensureSession(context);
  const video = context.video;
  if (video.sessionMessage) ws.send(video.sessionMessage);
  if (video.configMessage) ws.send(video.configMessage);
  if (video.cache.length) {
    for (const message of video.cache) ws.send(message);
    client.mode = "live";
  } else {
    client.keyRequested = true;
    context.session?.requestKeyFrame();
  }
  broadcastState(context);
}

function onVideoSession(context, size) {
  const video = context.video;
  const previous = video.size;
  video.size = size;
  video.sessionMessage = encodeVideoSession(size);
  video.configMessage = null;
  video.cache = [];
  video.cacheBytes = 0;
  for (const client of context.videoClients) {
    sendVideoHeader(client, "session", video.sessionMessage);
    client.mode = "needs-key";
    client.keyRequested = false;
  }
  if (previous && (previous.width !== size.width || previous.height !== size.height)) {
    liftForNewSize(context, previous, size);
  }
  liftOrphans(context, size);
  broadcastState(context);
}

function onVideoConfig(context, data) {
  const message = encodeVideoConfig(data);
  context.video.configMessage = message;
  for (const client of context.videoClients) sendVideoHeader(client, "config", message);
}

/**
 * Session and config messages are small, but every device reset or rotation sends one,
 * so a client that does not read must not queue them without end. A client dropping
 * until a key frame, or behind by STATE_BACKLOG_BYTES, keeps only the latest of each (a
 * session makes the config before it useless); they go out, session first, before the
 * next packet it gets.
 */
function sendVideoHeader(client, kind, message) {
  if (kind === "session") {
    client.pendingSession = message;
    client.pendingConfig = null;
  } else {
    client.pendingConfig = message;
  }
  if (client.mode !== "dropping" && client.ws.bufferedAmount <= STATE_BACKLOG_BYTES) flushVideoHeaders(client);
}

function flushVideoHeaders(client) {
  if (client.pendingSession) client.ws.send(client.pendingSession);
  if (client.pendingConfig) client.ws.send(client.pendingConfig);
  client.pendingSession = null;
  client.pendingConfig = null;
}

function onVideoPacket(context, packet) {
  const message = encodeVideoPacket(packet);
  const video = context.video;
  if (packet.keyFrame) {
    video.cache = [message];
    video.cacheBytes = message.length;
  } else if (video.cache.length) {
    video.cache.push(message);
    video.cacheBytes += message.length;
    if (video.cacheBytes > KEY_FRAME_CACHE_BYTES) {
      video.cache = [];
      video.cacheBytes = 0;
      context.session?.requestKeyFrame();
    }
  }
  for (const client of context.videoClients) deliverPacket(context, client, packet.keyFrame, message);
}

function deliverPacket(context, client, keyFrame, message) {
  const backlog = client.ws.bufferedAmount;
  if (client.mode === "live") {
    if (backlog <= CLIENT_BACKLOG_BYTES) {
      catchUpState(context, client);
      flushVideoHeaders(client);
      client.ws.send(message);
      return;
    }
    client.mode = "dropping";
    client.keyRequested = true;
    context.session?.requestKeyFrame();
    return;
  }
  // needs-key or dropping: only a key frame can restart decoding.
  if (keyFrame) {
    if (backlog <= CLIENT_BACKLOG_BYTES) {
      catchUpState(context, client);
      flushVideoHeaders(client);
      client.ws.send(message);
      client.mode = "live";
    }
    // Still behind at this key frame: ask again once the backlog has drained.
    client.keyRequested = false;
    return;
  }
  if (!client.keyRequested && backlog <= CLIENT_BACKLOG_BYTES / 2) {
    client.keyRequested = true;
    context.session?.requestKeyFrame();
  }
}

function onDeviceMessage(context, message) {
  if (message.type === "clipboard") answerClipboardRequest(context, message.text);
  else if (message.type === "ack-clipboard") clipboardAcknowledged(context, message.sequence);
}

/**
 * Device clipboard answers carry no request id, and none comes for an empty
 * clipboard. So one GET_CLIPBOARD is in flight at a time: every client that asks
 * meanwhile shares its answer, and silence until the deadline means no text.
 */
function requestClipboard(context, client) {
  if (!context.clipboardRequest) {
    deviceWrite(context, encodeGetClipboard(COPY_KEY.NONE));
    const request = { clients: new Set(), timer: null };
    request.timer = setTimeout(() => {
      if (context.clipboardRequest === request) answerClipboardRequest(context, null);
    }, CLIPBOARD_ANSWER_MS);
    request.timer.unref?.();
    context.clipboardRequest = request;
  }
  context.clipboardRequest.clients.add(client);
}

/** The text is never logged or journaled. */
function answerClipboardRequest(context, text) {
  const request = context.clipboardRequest;
  if (!request) return;
  context.clipboardRequest = null;
  clearTimeout(request.timer);
  for (const client of request.clients) {
    if (context.controlClients.has(client)) reply(context, client, { t: "clipboard", text });
  }
}

function cancelClipboardRequest(context, message) {
  const request = context.clipboardRequest;
  if (!request) return;
  context.clipboardRequest = null;
  clearTimeout(request.timer);
  for (const client of request.clients) {
    if (context.controlClients.has(client)) reply(context, client, { t: "error", message, for: "clipboard-get" });
  }
}

function openControlClient(context, ws, origin, socket) {
  const client = {
    ws, socket, origin, pointers: new Map(), lifted: new Map(), keys: new Set(), gesture: null,
    scroll: null, journalWaiting: 0, paused: false, socketPaused: false, held: [], heldNext: 0, heldBytes: 0,
    stateStale: false,
  };
  context.controlClients.add(client);
  ws.on("message", (data, isBinary) => receive(context, client, data, isBinary));
  // The client read its replies: it gets the current state, and it may send more.
  socket.on("drain", () => {
    catchUpState(context, client);
    resumeAllReading(context);
  });
  ws.on("close", () => {
    dropHeld(client);
    releaseInput(context, client);
    context.controlClients.delete(client);
    context.clipboardRequest?.clients.delete(client);
    broadcastState(context);
    scheduleIdleStop(context);
  });
  ensureSession(context);
  broadcastState(context);
}

/**
 * Backpressure instead of buffering: while a bound is passed, this connection's socket
 * is paused, so further input waits in the client and the kernel, not in Canvas memory.
 * Messages already parsed from the socket read that paused it wait in `held`, in order.
 * While only a paste's ack holds input, the socket is read on up to a small bound.
 */
function receive(context, client, data, isBinary) {
  if (client.ws.readyState !== READY_STATE.OPEN) return;
  if (!client.paused && !inputPressure(context, client, 1)) {
    handleInbound(context, client, data, isBinary);
    return;
  }
  client.paused = true;
  client.heldBytes += data.length + HELD_MESSAGE_OVERHEAD_BYTES;
  if (client.heldBytes > HELD_INPUT_BYTES) {
    refuseInput(context, client);
    return;
  }
  client.held.push({ data, isBinary, order: ++context.heldOrder });
  if (!client.socketPaused &&
    (pastBounds(context, client, 1) || client.heldBytes > CLIPBOARD_HOLD_READ_BYTES)) {
    client.socketPaused = true;
    client.socket.pause();
  }
}

/**
 * True while a bound of this connection is past `share` of its limit, or while a paste
 * waits for its clipboard ack, which holds the input of every connection.
 */
function inputPressure(context, client, share) {
  return context.clipboardHold !== null || pastBounds(context, client, share);
}

function pastBounds(context, client, share) {
  return deviceBacklog(context) > DEVICE_BACKLOG_PAUSE_BYTES * share ||
    client.ws.bufferedAmount > REPLY_BACKLOG_PAUSE_BYTES * share ||
    client.journalWaiting >= JOURNAL_CLIENT_PAUSE_RECORDS * share ||
    journalPending(context) >= JOURNAL_QUEUE_RECORDS * share;
}

/** Control bytes the device has not read, with those held behind a paste. */
function deviceBacklog(context) {
  return (context.session?.controlBacklog ?? 0) + context.heldWriteBytes;
}

/**
 * Each paused connection whose bounds are back under half has its held messages handled,
 * then its socket read again. Messages held on several connections are handled in the
 * order they arrived, so input held behind a paste reaches the device in that order
 * whatever connection sent it.
 */
function resumeAllReading(context) {
  const resuming = [];
  for (const client of context.controlClients) {
    if (!client.paused || client.ws.readyState !== READY_STATE.OPEN) continue;
    if (inputPressure(context, client, 0.5)) continue;
    client.paused = false;
    resuming.push(client);
  }
  for (;;) {
    let next = null;
    for (const client of resuming) {
      if (client.paused || client.heldNext >= client.held.length ||
        client.ws.readyState !== READY_STATE.OPEN) continue;
      if (!next || client.held[client.heldNext].order < next.held[next.heldNext].order) next = client;
    }
    if (!next) break;
    if (inputPressure(context, next, 1)) {
      next.paused = true;
      continue;
    }
    const { data, isBinary } = next.held[next.heldNext];
    next.held[next.heldNext] = null;
    next.heldNext += 1;
    next.heldBytes -= data.length + HELD_MESSAGE_OVERHEAD_BYTES;
    handleInbound(context, next, data, isBinary);
  }
  for (const client of resuming) {
    if (client.paused) {
      compactHeld(client);
      continue;
    }
    if (client.ws.readyState !== READY_STATE.OPEN) continue;
    dropHeld(client);
    if (client.socketPaused) {
      client.socketPaused = false;
      client.socket.resume();
    }
  }
}

/** Drop the handled slots in front of a held queue that still holds messages. */
function compactHeld(client) {
  if (client.heldNext >= HELD_COMPACT_SLOTS || (client.heldNext > 0 && client.heldNext * 2 >= client.held.length)) {
    client.held.splice(0, client.heldNext);
    client.heldNext = 0;
  }
}

function dropHeld(client) {
  client.held = [];
  client.heldNext = 0;
  client.heldBytes = 0;
}

/** Pausing did not stop this connection's input: lift what it holds, then ask it to come back later. */
function refuseInput(context, client) {
  const message = "Input arrives faster than the Canvas can apply it; reconnect later";
  dropHeld(client);
  reply(context, client, { t: "error", message, for: null });
  releaseInput(context, client);
  client.ws.close(CLOSE_TRY_AGAIN_LATER, message);
}

function handleInbound(context, client, data, isBinary) {
  if (isBinary) {
    reply(context, client, { t: "error", message: "Control messages must be JSON text.", for: null });
    return;
  }
  handleControlMessage(context, client, data);
}

function reply(context, client, message) {
  sendControlText(context, client, JSON.stringify(message));
}

/**
 * Replies (pongs, errors, clipboard answers of up to 256 KiB) share the per-client
 * bound of video. A client that does not read them stops being read (inputPressure),
 * so only answers to requests it made before that can reach the bound; past it the
 * client is closed and its pointers lifted, instead of growing Canvas memory.
 */
function sendControlText(context, client, text) {
  if (client.ws.readyState !== READY_STATE.OPEN) return;
  if (client.ws.bufferedAmount > CLIENT_BACKLOG_BYTES) {
    client.ws.close(CLOSE_CODE.POLICY_VIOLATION, "Control replies are not being read");
    // Its frames are ignored from now on, so nothing would lift them before the socket goes.
    releaseInput(context, client);
    return;
  }
  client.ws.send(text);
}

function refusal(context, origin) {
  if (context.state.inputPaused) return "Canvas input is paused";
  if (context.state.controlOwner !== "shared" && context.state.controlOwner !== origin) {
    return `Canvas control is owned by ${context.state.controlOwner}`;
  }
  return null;
}

class InputError extends Error {}

function handleControlMessage(context, client, raw) {
  let message;
  try {
    message = parseControlMessage(raw);
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: error.for ?? null });
    return;
  }
  try {
    dispatchControl(context, client, message);
  } catch (error) {
    // Every refusal is answered on the connection; none may take the Canvas down.
    if (!(error instanceof InputError)) context.state.lastError = error.message;
    reply(context, client, { t: "error", message: error.message, for: message.t });
  }
}

function dispatchControl(context, client, message) {
  switch (message.t) {
    case "ping":
      reply(context, client, { t: "pong", ts: message.ts });
      return;
    case "control":
      applyControl(context, message.mode, client.origin);
      journal(context, client, {
        kind: "control", mode: message.mode, owner: context.state.controlOwner,
      });
      return;
    case "clipboard-get":
      requestClipboard(context, client);
      return;
    case "system":
      if (message.op === "keyframe") {
        requireSession(context);
        context.session.requestKeyFrame();
        return;
      }
      break;
    default:
      break;
  }
  // Handoff is checked per message, so a takeover stops a gesture already in progress.
  const refused = refusal(context, client.origin);
  if (refused) throw new InputError(refused);
  requireSession(context);
  // Any other input ends a wheel burst, so the journal keeps the order of actions.
  if (message.t !== "scroll") finishScroll(context, client);
  switch (message.t) {
    case "touch":
      touch(context, client, message);
      break;
    case "scroll":
      scroll(context, client, message);
      break;
    case "key":
      keyEvent(context, client, message);
      break;
    case "text":
      typeText(context, client, message);
      break;
    case "paste":
      pasteText(context, message.text);
      journal(context, client, { kind: "paste", text_len: codePoints(message.text) });
      break;
    case "system":
      systemAction(context, client, message.op);
      break;
    default:
      throw new InputError("Unsupported control message.");
  }
}

function requireSession(context) {
  if (!context.session?.connected) {
    throw new InputError(`The scrcpy session is not ready (${context.session?.state ?? "idle"})`);
  }
}

/**
 * Every device input write goes through here. While a paste waits for its ack the write
 * waits too, in order; a write that pastes (`clipboardSequence`) starts that wait.
 * `onWritten` runs once the bytes are written to the device socket.
 */
function deviceWrite(context, buffer, clipboardSequence = null, onWritten = null) {
  requireSession(context);
  if (context.clipboardHold) {
    context.heldWrites.push({ buffer, clipboardSequence, onWritten });
    context.heldWriteBytes += buffer.length;
    return;
  }
  context.session.send(buffer, onWritten);
  if (clipboardSequence !== null) holdForClipboard(context, clipboardSequence);
}

/** Set the device clipboard to `text` and paste it, with a sequence the device acknowledges. */
function pasteText(context, text) {
  context.clipboardSequence += 1n;
  const sequence = context.clipboardSequence;
  deviceWrite(context, encodeSetClipboard({ sequence, paste: true, text }), sequence);
}

function holdForClipboard(context, sequence) {
  const hold = { sequence, acknowledged: false, timer: null };
  hold.timer = setTimeout(() => {
    context.state.lastError = `clipboard: the device did not acknowledge a paste within ` +
      `${CLIPBOARD_ACK_TIMEOUT_MS} ms, so the input after it was written without waiting longer`;
    endClipboardHold(context, hold);
  }, CLIPBOARD_ACK_TIMEOUT_MS);
  hold.timer.unref?.();
  context.clipboardHold = hold;
}

function clipboardAcknowledged(context, sequence) {
  const hold = context.clipboardHold;
  if (!hold || hold.acknowledged || hold.sequence !== sequence) return;
  hold.acknowledged = true;
  clearTimeout(hold.timer);
  // The app handles the injected paste key a moment after the ack.
  hold.timer = setTimeout(() => endClipboardHold(context, hold), CLIPBOARD_SETTLE_MS);
  hold.timer.unref?.();
}

/** Writes made during the hold go out in order until one pastes again; then held messages. */
function endClipboardHold(context, hold) {
  if (context.clipboardHold !== hold) return;
  clearTimeout(hold.timer);
  context.clipboardHold = null;
  while (!context.clipboardHold && context.heldWrites.length) {
    const { buffer, clipboardSequence, onWritten } = context.heldWrites.shift();
    context.heldWriteBytes -= buffer.length;
    if (!context.session?.connected) continue;
    context.session.send(buffer, onWritten);
    if (clipboardSequence !== null) holdForClipboard(context, clipboardSequence);
  }
  resumeAllReading(context);
}

/** The device server is gone or going: no ack will come, and held writes have no device. */
function dropClipboardHold(context) {
  clearTimeout(context.clipboardHold?.timer);
  context.clipboardHold = null;
  context.heldWrites = [];
  context.heldWriteBytes = 0;
}

function videoSize(context) {
  const size = context.video.size;
  if (!size) throw new InputError("The video size is not known yet");
  return size;
}

function toVideoPixels(x, y, size) {
  return {
    x: Math.min(size.width - 1, Math.floor(x * size.width)),
    y: Math.min(size.height - 1, Math.floor(y * size.height)),
  };
}

/** Where a pointer was, normalized, with the video size it was mapped against. */
function gesturePoint(context, x, y) {
  return { x, y, size: context.video.size };
}

/** A gesture point in display pixels for the journal, in the video's orientation. */
function devicePixels(display, { x, y, size }) {
  let width = display?.width ?? size?.width ?? 0;
  let height = display?.height ?? size?.height ?? 0;
  if (size && display && (size.width > size.height) !== (width > height)) {
    [width, height] = [height, width];
  }
  return [Math.round(x * width), Math.round(y * height)];
}

function allocateDevicePointer(context) {
  // Ids are unique across all control connections: the device has one pointer state.
  for (let id = 0; id < MAX_POINTERS; id += 1) {
    if (!context.devicePointers.has(id)) {
      context.devicePointers.add(id);
      return id;
    }
  }
  return null;
}

function touchMessage(action, deviceId, point, size, pressure) {
  return encodeTouch({
    action,
    pointerId: deviceId,
    x: point.x,
    y: point.y,
    width: size.width,
    height: size.height,
    pressure,
  });
}

function touch(context, client, message) {
  // Mapping uses the size current now, so a rotation never sends a stale size the
  // server would ignore.
  const size = videoSize(context);
  const point = toVideoPixels(message.x, message.y, size);
  if (message.a === "down") {
    client.lifted.delete(message.id);
    if (client.pointers.has(message.id)) throw new InputError(`Pointer ${message.id} is already down`);
    const deviceId = allocateDevicePointer(context);
    if (deviceId === null) throw new InputError(`At most ${MAX_POINTERS} pointers can be down`);
    client.pointers.set(message.id, { deviceId, x: message.x, y: message.y });
    client.gesture ??= {
      startedAt: performance.now(), pointers: 0, moves: 0,
      start: gesturePoint(context, message.x, message.y), end: null,
    };
    client.gesture.pointers += 1;
    deviceWrite(context, touchMessage(MOTION_ACTION.DOWN, deviceId, point, size, message.p ?? 1));
    return;
  }
  const pointer = client.pointers.get(message.id);
  if (!pointer) {
    const lifted = client.lifted.get(message.id);
    if (!lifted) throw new InputError(`Pointer ${message.id} is not down`);
    if (message.a === "move") {
      throw new InputError(`Pointer ${message.id} was lifted because ${lifted}; put it down again`);
    }
    // The up or cancel of a pointer the Canvas lifted already: nothing is left to do.
    client.lifted.delete(message.id);
    return;
  }
  pointer.x = message.x;
  pointer.y = message.y;
  if (message.a === "move") {
    client.gesture.moves += 1;
    deviceWrite(context, touchMessage(MOTION_ACTION.MOVE, pointer.deviceId, point, size, message.p ?? 1));
    return;
  }
  if (message.a === "cancel") {
    // scrcpy keeps a cancelled pointer in its state until it sees an up for it.
    deviceWrite(context, touchMessage(MOTION_ACTION.CANCEL, pointer.deviceId, point, size, 0));
  }
  deviceWrite(context, touchMessage(MOTION_ACTION.UP, pointer.deviceId, point, size, 0), null,
    rememberUp(context, pointer.deviceId, message.x, message.y));
  client.pointers.delete(message.id);
  context.devicePointers.delete(pointer.deviceId);
  client.gesture.end = gesturePoint(context, message.x, message.y);
  if (!client.pointers.size) finishGesture(context, client);
}

function finishGesture(context, client) {
  const gesture = client.gesture;
  client.gesture = null;
  if (!gesture) return;
  // Points stay normalized until the record is written, when the display size is known.
  journal(context, client, {
    kind: "gesture",
    pointers: gesture.pointers,
    moves: gesture.moves,
    duration_ms: performance.now() - gesture.startedAt,
    start: gesture.start,
    end: gesture.end ?? gesture.start,
  });
}

function scroll(context, client, message) {
  const size = videoSize(context);
  const point = toVideoPixels(message.x, message.y, size);
  deviceWrite(context, encodeScroll({
    x: point.x, y: point.y, width: size.width, height: size.height,
    hScroll: message.dx, vScroll: message.dy,
  }));
  // A wheel burst is one action: journal it once the wheel has been still a moment.
  const burst = client.scroll ?? (client.scroll = { events: 0, dx: 0, dy: 0, timer: null });
  burst.events += 1;
  burst.dx += message.dx;
  burst.dy += message.dy;
  clearTimeout(burst.timer);
  burst.timer = setTimeout(() => finishScroll(context, client), SCROLL_BURST_IDLE_MS);
  burst.timer.unref?.();
}

function finishScroll(context, client) {
  const burst = client.scroll;
  client.scroll = null;
  if (!burst) return;
  clearTimeout(burst.timer);
  journal(context, client, { kind: "scroll", events: burst.events, dx: burst.dx, dy: burst.dy });
}

function keyEvent(context, client, message) {
  const down = message.a === "down";
  deviceWrite(context, encodeKeycode({
    action: down ? KEY_ACTION.DOWN : KEY_ACTION.UP,
    keycode: message.code,
    repeat: message.repeat,
    metaState: message.meta,
  }), null, down ? null : rememberKeyUp(context, message.code));
  if (down) {
    client.keys.add(message.code);
  } else if (client.keys.delete(message.code)) {
    journal(context, client, { kind: "key", key: message.code });
  }
}

function typeText(context, client, message) {
  if (INJECTABLE_TEXT.test(message.text)) {
    deviceWrite(context, encodeText(message.text));
  } else {
    // INJECT_TEXT only types what the virtual keyboard map can; the clipboard takes any text.
    pasteText(context, message.text);
  }
  const entry = { kind: "text", sensitive: message.sensitive, text_len: codePoints(message.text) };
  if (!message.sensitive) entry.text = message.text;
  journal(context, client, entry);
}

function systemAction(context, client, op) {
  if (op === "back") {
    deviceWrite(context, encodeBackOrScreenOn(KEY_ACTION.DOWN));
    deviceWrite(context, encodeBackOrScreenOn(KEY_ACTION.UP));
  } else if (op in SYSTEM_KEYS) {
    deviceWrite(context, encodeKeycode({ action: KEY_ACTION.DOWN, keycode: SYSTEM_KEYS[op] }));
    deviceWrite(context, encodeKeycode({ action: KEY_ACTION.UP, keycode: SYSTEM_KEYS[op] }));
  } else if (op in SYSTEM_PANELS) {
    deviceWrite(context, encodeEmpty(SYSTEM_PANELS[op]));
  } else {
    throw new InputError(`Unsupported system action ${op}`);
  }
  journal(context, client, { kind: "system", op });
}

/** Lift every pointer and key of one connection on the device and journal what it did. */
function releaseInput(context, client) {
  const session = context.session;
  const size = context.video.size;
  for (const pointer of client.pointers.values()) {
    if (session?.connected && size) {
      const point = toVideoPixels(pointer.x, pointer.y, size);
      deviceWrite(context, touchMessage(MOTION_ACTION.UP, pointer.deviceId, point, size, 0), null,
        rememberUp(context, pointer.deviceId, pointer.x, pointer.y));
    }
    context.devicePointers.delete(pointer.deviceId);
    if (client.gesture) client.gesture.end = gesturePoint(context, pointer.x, pointer.y);
  }
  client.pointers.clear();
  for (const code of client.keys) {
    if (session?.connected) {
      deviceWrite(context, encodeKeycode({ action: KEY_ACTION.UP, keycode: code }), null, rememberKeyUp(context, code));
    }
  }
  finishGesture(context, client);
  finishKeys(context, client);
  finishScroll(context, client);
}

/** The device server went away with its pointer state: drop ours without device writes. */
function forgetInput(context, client) {
  for (const pointer of client.pointers.values()) context.devicePointers.delete(pointer.deviceId);
  client.pointers.clear();
  finishGesture(context, client);
  finishKeys(context, client);
  finishScroll(context, client);
}

/**
 * The device server died with this connection's pointers and keys down. Android keeps what
 * the dead server injected down, so they are lifted through the next server (liftOrphans);
 * their device pointer ids stay taken until then. The connection's gesture and keys end
 * here, journaled like other interruptions, and its later moves for them are refused.
 */
function orphanInput(context, client) {
  for (const [id, pointer] of client.pointers) {
    context.orphans.pointers.set(pointer.deviceId, { x: pointer.x, y: pointer.y });
    liftedByCanvas(client, id, "the device server restarted");
    if (client.gesture) client.gesture.end = gesturePoint(context, pointer.x, pointer.y);
  }
  client.pointers.clear();
  for (const code of client.keys) context.orphans.keys.add(code);
  finishGesture(context, client);
  finishKeys(context, client);
  finishScroll(context, client);
}

/**
 * Ups and key-ups not written yet (behind a paste's ack, which the restart drops) or
 * written within RECENT_UP_MS (maybe still unread in the dead server's backlog) may never
 * have reached Android, so they are lifted through the next server too, their device
 * pointer ids taken until then. An up for a finger or key already up does nothing.
 */
function orphanRecent(context) {
  const now = performance.now();
  const recent = ({ at }) => at === null || now - at <= RECENT_UP_MS;
  for (const [deviceId, up] of context.recentUps) {
    if (!recent(up) || context.orphans.pointers.has(deviceId)) continue;
    context.orphans.pointers.set(deviceId, { x: up.x, y: up.y });
    context.devicePointers.add(deviceId);
  }
  for (const [code, up] of context.recentKeyUps) {
    if (recent(up)) context.orphans.keys.add(code);
  }
  context.recentUps.clear();
  context.recentKeyUps.clear();
}

/** Once the new server's video size is known: an up for each orphaned pointer, then each key. */
function liftOrphans(context, size) {
  const { pointers, keys } = context.orphans;
  if ((!pointers.size && !keys.size) || !context.session?.connected) return;
  for (const [deviceId, { x, y }] of pointers) {
    deviceWrite(context, touchMessage(MOTION_ACTION.UP, deviceId, toVideoPixels(x, y, size), size, 0), null,
      rememberUp(context, deviceId, x, y));
    context.devicePointers.delete(deviceId);
  }
  for (const code of keys) {
    deviceWrite(context, encodeKeycode({ action: KEY_ACTION.UP, keycode: code }), null, rememberKeyUp(context, code));
  }
  pointers.clear();
  keys.clear();
}

/**
 * Remember an up about to be written, as not written yet; the callback returned stamps it
 * when it reaches the device socket, which can be long after, behind a paste's ack or a
 * device backlog.
 */
function rememberUp(context, deviceId, x, y) {
  const up = { x, y, at: null };
  context.recentUps.set(deviceId, up);
  return () => {
    if (context.recentUps.get(deviceId) === up) up.at = performance.now();
  };
}

/** The same for a key-up: a restart lifts a recent or unwritten one again (orphanRecent). */
function rememberKeyUp(context, code) {
  const up = { at: null };
  context.recentKeyUps.set(code, up);
  return () => {
    if (context.recentKeyUps.get(code) === up) up.at = performance.now();
  };
}

/**
 * The video size changed (a rotation). scrcpy-server ignores positional events made for
 * the old size, so an up sent shortly before may have been ignored and a finger left down
 * on Android: those ups are sent again with the new size. Pointers still down are lifted
 * with the new size and their gestures end; later moves for them are refused.
 */
function liftForNewSize(context, previous, size) {
  const connected = Boolean(context.session?.connected);
  const lift = (deviceId, x, y) => {
    if (connected) deviceWrite(context, touchMessage(MOTION_ACTION.UP, deviceId, toVideoPixels(x, y, size), size, 0));
  };
  const now = performance.now();
  for (const [deviceId, up] of context.recentUps) {
    // An up still waiting to be written carries the old size too: it is sent again after it.
    const recent = up.at === null || now - up.at <= RECENT_UP_MS;
    if (recent && !context.devicePointers.has(deviceId)) lift(deviceId, up.x, up.y);
  }
  context.recentUps.clear();
  for (const client of context.controlClients) {
    if (!client.pointers.size) continue;
    for (const [id, pointer] of client.pointers) {
      lift(pointer.deviceId, pointer.x, pointer.y);
      context.devicePointers.delete(pointer.deviceId);
      liftedByCanvas(client, id, "the video size changed");
      // The point is where the finger was, in the orientation it was in.
      if (client.gesture) client.gesture.end = { x: pointer.x, y: pointer.y, size: previous };
    }
    client.pointers.clear();
    finishGesture(context, client);
  }
}

/** Remember why the Canvas lifted a connection's pointer, so its later moves get a clear refusal. */
function liftedByCanvas(client, id, reason) {
  client.lifted.delete(id);
  client.lifted.set(id, reason);
  if (client.lifted.size > LIFTED_POINTERS_MAX) client.lifted.delete(client.lifted.keys().next().value);
}

/** A held key the Canvas lifts is a completed key press, journaled like one released by its client. */
function finishKeys(context, client) {
  for (const code of client.keys) journal(context, client, { kind: "key", key: code });
  client.keys.clear();
}

/** Pause/resume/takeover/release for HTTP and WebSocket alike. */
function applyControl(context, mode, origin) {
  if (mode === "pause") context.state.inputPaused = true;
  else if (mode === "resume") context.state.inputPaused = false;
  else if (mode === "takeover") context.state.controlOwner = origin;
  else if (mode === "release") context.state.controlOwner = "shared";
  else throw httpError(400, "Control mode must be pause, resume, takeover, or release");
  for (const client of context.controlClients) {
    if (refusal(context, client.origin)) releaseInput(context, client);
  }
  broadcastState(context);
}

function codePoints(value) {
  let count = 0;
  for (const _ of value) count += 1;
  return count;
}

/**
 * One journal record per completed action, through the bridge; never actuates.
 * Each connection counts its records until the bridge answers them, for inputPressure,
 * which stops reading input before the journal fills. A record that finds
 * JOURNAL_QUEUE_RECORDS already pending is dropped and counted.
 */
function journal(context, client, record) {
  if (journalPending(context) >= JOURNAL_QUEUE_RECORDS) {
    context.journalDropped += 1;
    context.state.lastError = `journal: ${context.journalDropped} actions were not journaled ` +
      `because ${JOURNAL_QUEUE_RECORDS} records were waiting for the bridge`;
    return;
  }
  context.journalQueue.push({ client, origin: client.origin, record });
  client.journalWaiting += 1;
  if (!context.journalDraining) drainJournal(context);
}

function journalPending(context) {
  return context.journalQueue.length + context.journalInFlight;
}

/** Records go to the bridge in queue order, at most JOURNAL_IN_FLIGHT before their answers. */
async function drainJournal(context) {
  context.journalDraining = true;
  while (context.journalQueue.length) {
    if (context.journalInFlight >= JOURNAL_IN_FLIGHT) {
      await new Promise((resolvePromise) => { context.journalSlot = resolvePromise; });
      continue;
    }
    const entry = context.journalQueue.shift();
    context.journalInFlight += 1;
    // Awaited before the next record is written, so a gesture waiting for the display
    // size keeps its place.
    const payload = await journalPayload(context, entry.record);
    context.actionBridge.call("record", payload, entry.origin).catch((error) => {
      context.state.lastError = `journal: ${error.message}`;
    }).finally(() => journalAnswered(context, entry.client));
  }
  context.journalDraining = false;
}

/** The bridge answered a record: the next may go, and paused connections may read again. */
function journalAnswered(context, client) {
  context.journalInFlight -= 1;
  client.journalWaiting -= 1;
  const slot = context.journalSlot;
  context.journalSlot = null;
  slot?.();
  resumeAllReading(context);
}

/** The bridge payload, with gesture points in display pixels. */
async function journalPayload(context, record) {
  const payload = { transport: "scrcpy", ...record };
  if (record.kind === "gesture") {
    const display = context.display ?? await refreshDisplay(context);
    payload.start = devicePixels(display, record.start);
    payload.end = devicePixels(display, record.end);
    payload.pointers = Math.min(record.pointers, RECORD_MAX_POINTERS);
    payload.moves = Math.min(record.moves, RECORD_MAX_COUNT);
    payload.duration_ms = Math.max(0, Math.min(record.duration_ms, RECORD_MAX_DURATION_MS));
  } else if (record.kind === "scroll") {
    const clamp = (value) => Math.max(-RECORD_MAX_SCROLL, Math.min(RECORD_MAX_SCROLL, value));
    payload.events = Math.min(record.events, RECORD_MAX_COUNT);
    payload.dx = clamp(record.dx);
    payload.dy = clamp(record.dy);
  }
  return payload;
}

function assertControl(context, origin) {
  const refused = refusal(context, origin);
  if (refused) throw httpError(409, refused);
}

async function tap(context, response, body, origin) {
  assertControl(context, origin);
  const x = normalizeCoordinate(body.x, "x");
  const y = normalizeCoordinate(body.y, "y");
  const result = await context.enqueueInput(
    () => context.actionBridge.call("tap", { x, y }, origin));
  sendJson(response, 200, result);
}

async function swipe(context, response, body, origin) {
  assertControl(context, origin);
  const x1 = normalizeCoordinate(body.x1, "x1");
  const y1 = normalizeCoordinate(body.y1, "y1");
  const x2 = normalizeCoordinate(body.x2, "x2");
  const y2 = normalizeCoordinate(body.y2, "y2");
  const duration = Math.min(5000, Math.max(1, normalizeCoordinate(body.duration ?? 250, "duration")));
  const result = await context.enqueueInput(() => context.actionBridge.call(
    "swipe", { x1, y1, x2, y2, duration }, origin));
  sendJson(response, 200, result);
}

async function key(context, response, body, origin) {
  assertControl(context, origin);
  const value = String(body.key ?? "");
  if (!isSafeKeyCode(value)) throw httpError(400, "Unsupported key code");
  const result = await context.enqueueInput(
    () => context.actionBridge.call("key", { key: value }, origin));
  sendJson(response, 200, result);
}

async function text(context, response, body, origin) {
  assertControl(context, origin);
  try {
    encodeAdbText(String(body.text ?? ""));
  } catch (error) {
    throw httpError(400, error.message);
  }
  const result = await context.enqueueInput(() => context.actionBridge.call(
    "text", { text: String(body.text ?? ""), sensitive: Boolean(body.sensitive) }, origin));
  sendJson(response, 200, result);
}

async function control(context, response, body, origin) {
  applyControl(context, String(body.mode ?? ""), origin);
  sendJson(response, 200, { ok: true, control_owner: context.state.controlOwner,
    input_paused: context.state.inputPaused });
}

function createActionBridge(options, adbPath, serial) {
  const python = options.python ?? env.PYTHON ?? "python3";
  const bridgePath = options.bridge ?? resolve(
    import.meta.dirname, "../../../../../scripts/autonom_canvas_bridge.py");
  const childEnv = { ...env };
  if (options.idb) childEnv.AUTONOM_IDB = options.idb;
  const child = spawn(python, [bridgePath, "--platform", options.platform, "--target", serial,
    "--tool", adbPath], { stdio: ["pipe", "pipe", "pipe"], env: childEnv });
  const pending = new Map();
  let nextId = 0;
  let stderr = "";
  child.stdin.on("error", () => {});
  child.stderr.on("data", (chunk) => { stderr = (stderr + chunk.toString()).slice(-4000); });
  createInterface({ input: child.stdout }).on("line", (line) => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.ok) waiter.resolve(message.result);
    else waiter.reject(httpError(502, message.error ?? "Canvas action failed"));
  });
  child.on("exit", (code) => {
    for (const waiter of pending.values()) {
      waiter.reject(httpError(502, `Canvas action bridge exited ${code}: ${stderr}`));
    }
    pending.clear();
  });
  return {
    call(op, payload, origin) {
      // A bridge that is gone never answers: its waiter would stay pending, and the
      // journal, which waits for each record, would stop for good.
      if (child.exitCode !== null || child.signalCode !== null) {
        return Promise.reject(httpError(502, `Canvas action bridge exited ${child.exitCode}: ${stderr}`));
      }
      const id = ++nextId;
      return new Promise((resolvePromise, reject) => {
        pending.set(id, { resolve: resolvePromise, reject });
        child.stdin.write(`${JSON.stringify({ id, op, payload, origin })}\n`);
      });
    },
    close() { child.kill("SIGTERM"); },
  };
}

async function readJsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw httpError(413, "Request body is too large");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw httpError(400, "Invalid JSON body");
  }
}

async function runAdb(context, args, options = {}) {
  const encoding = options.encoding === "buffer" ? "buffer" : "utf8";
  const result = await execFileAsync(context.adbPath, ["-s", context.serial, ...args], {
    timeout: options.timeout ?? 10_000,
    maxBuffer: 32 * 1024 * 1024,
    encoding: encoding === "buffer" ? null : "utf8",
  });
  return { stdout: result.stdout, stderr: result.stderr };
}

/** True once `bytes` holds an Annex B start code followed by an SPS NAL unit. */
function hasSps(bytes) {
  for (let i = 0; i + 3 < bytes.length; i += 1) {
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1 && (bytes[i + 3] & 0x1f) === 7) {
      return true;
    }
  }
  return false;
}

/**
 * Android 16 (API 36) no longer lists the hidden --output-format option in
 * `screenrecord --help` although `--output-format=h264` still works, so run it
 * briefly and look for an H.264 SPS instead of reading the help text.
 */
function supportsScreenrecord(adbPath, serial) {
  return new Promise((resolvePromise) => {
    let child;
    try {
      child = spawn(adbPath, [
        "-s", serial, "exec-out", "screenrecord", "--output-format=h264", "--time-limit", "1", "-",
      ], { stdio: ["ignore", "pipe", "ignore"] });
    } catch {
      resolvePromise(false);
      return;
    }
    let tail = Buffer.alloc(0);
    let done = false;
    const finish = (supported) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.kill("SIGTERM");
      resolvePromise(supported);
    };
    const timer = setTimeout(() => finish(false), SCREENRECORD_PROBE_MS);
    child.stdout.on("data", (chunk) => {
      // Keep three bytes so a start code split across chunks is still found.
      const bytes = Buffer.concat([tail, chunk]);
      if (hasSps(bytes)) finish(true);
      tail = bytes.subarray(Math.max(0, bytes.length - 3));
    });
    child.once("error", () => finish(false));
    child.once("close", () => finish(false));
  });
}

async function assertDevice(adbPath, serial) {
  const { stdout } = await execFileAsync(adbPath, ["-s", serial, "get-state"], { timeout: 5000, encoding: "utf8" });
  if (stdout.trim() !== "device") throw new Error(`adb target is not ready: ${serial}`);
}

async function assertSimulator(xcrunPath, udid) {
  const { stdout } = await execFileAsync(
    xcrunPath, ["simctl", "list", "devices", "--json"],
    { timeout: 5000, encoding: "utf8" });
  const devices = Object.values(JSON.parse(stdout).devices ?? {}).flat();
  const match = devices.find((device) => device.udid === udid);
  if (!match || match.state !== "Booted" || match.isAvailable === false) {
    throw new Error(`iOS Simulator target is not booted and available: ${udid}`);
  }
}

async function inferSingleSimulator(xcrunPath) {
  const { stdout } = await execFileAsync(
    xcrunPath, ["simctl", "list", "devices", "--json"],
    { timeout: 5000, encoding: "utf8" });
  const devices = Object.values(JSON.parse(stdout).devices ?? {}).flat()
    .filter((device) => device.state === "Booted" && device.isAvailable !== false);
  if (devices.length === 1) return devices[0].udid;
  if (!devices.length) throw new Error("No booted iOS Simulator is available. Pass --target after booting one.");
  throw new Error(`Multiple iOS Simulators are booted (${devices.map((item) => item.udid).join(", ")}). Pass --target.`);
}

async function inferSingleDevice(adbPath) {
  const { stdout } = await execFileAsync(adbPath, ["devices"], { timeout: 5000, encoding: "utf8" });
  const devices = stdout.split(/\r?\n/).slice(1)
    .map((line) => line.trim().split(/\s+/))
    .filter((parts) => parts.length >= 2 && parts[1] === "device")
    .map((parts) => parts[0]);
  if (devices.length === 1) return devices[0];
  if (!devices.length) throw new Error("No authorized adb device is connected. Pass --serial after starting one.");
  throw new Error(`Multiple adb devices are connected (${devices.join(", ")}). Pass --serial.`);
}

async function findAdb() {
  const candidates = [];
  const found = await findExecutable("adb").catch(() => null);
  if (found) candidates.push(found);
  for (const root of [env.ANDROID_SDK_ROOT, env.ANDROID_HOME, join(homedir(), "Library/Android/sdk")]) {
    if (root) candidates.push(join(root, "platform-tools", platform === "win32" ? "adb.exe" : "adb"));
  }
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {}
  }
  throw new Error("adb not found on PATH, ANDROID_SDK_ROOT, ANDROID_HOME, or ~/Library/Android/sdk");
}

async function findExecutable(name) {
  const extensions = platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    for (const extension of extensions) {
      const candidate = join(directory, name + extension);
      try {
        await access(candidate, constants.X_OK);
        return candidate;
      } catch {}
    }
  }
  throw new Error(`${name} not found on PATH`);
}

function renderPage(context) {
  const serial = escapeHtml(context.serial);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonom</title>
<link rel="icon" href="data:,">
<style>
:root{color-scheme:dark;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;background:#111418;color:#f5f7f7}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;grid-template-columns:minmax(280px,1fr) 290px;gap:20px;padding:20px}
main{display:flex;align-items:center;justify-content:center;min-width:0}.device{height:calc(100vh - 40px);max-width:100%;aspect-ratio:9/19.5;background:#030506;border:9px solid #030506;border-radius:30px;overflow:hidden;box-shadow:0 24px 70px #0009;display:flex;align-items:center;justify-content:center;touch-action:none}
img,canvas{width:100%;height:100%;object-fit:contain;background:#000;user-select:none;-webkit-user-drag:none;touch-action:none;cursor:crosshair;outline:none}[hidden]{display:none!important}
aside{display:flex;flex-direction:column;gap:12px;min-width:0}h1{font-size:20px;margin:0}.meta,.status{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#b8c3c0;white-space:pre-wrap;overflow-wrap:anywhere}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}button,input{min-height:38px;border-radius:8px;border:1px solid #3b4643;background:#202724;color:#f5f7f7;padding:8px;font:inherit}button{cursor:pointer}button:hover{background:#2a3531}input{grid-column:1/-1}.wide{grid-column:1/-1}.accent{border-color:#00C2A8}
@media(max-width:780px){body{grid-template-columns:1fr;padding:12px}.device{height:auto;width:min(100%,420px)}}
</style>
</head>
<body>
<main><div class="device" id="device"><canvas id="video" tabindex="0" aria-label="Android device screen" hidden></canvas><img id="screen" tabindex="0" alt="Android device screen"></div></main>
<aside>
  <div><h1>Autonom</h1><div class="meta">adb: ${serial}</div></div>
  <div class="controls">
    <button data-key="KEYCODE_BACK" data-system="back">Back</button><button data-key="KEYCODE_HOME" data-system="home">Home</button><button data-key="KEYCODE_APP_SWITCH" data-system="app-switch">Apps</button>
    <button data-key="KEYCODE_DPAD_UP" data-code="19">↑</button><button data-key="KEYCODE_ENTER" data-code="66">Enter</button><button data-key="KEYCODE_DPAD_DOWN" data-code="20">↓</button>
    <button data-key="KEYCODE_DPAD_LEFT" data-code="21">←</button><button data-key="KEYCODE_DEL" data-code="67">Delete</button><button data-key="KEYCODE_DPAD_RIGHT" data-code="22">→</button>
    <button data-key="KEYCODE_WAKEUP" data-system="wake">Wake</button><button data-key="KEYCODE_POWER" data-system="power">Power</button><button data-system="rotate" data-scrcpy>Rotate</button>
    <button data-system="volume-down" data-scrcpy>Vol −</button><button data-system="volume-up" data-scrcpy>Vol +</button><button data-system="notifications" data-scrcpy>Alerts</button>
    <button data-system="quick-settings" data-scrcpy>Quick</button><button data-system="collapse" data-scrcpy>Collapse</button><button id="clipboard" data-scrcpy>Clipboard</button>
    <button id="refresh" class="wide">Reconnect stream</button>
    <input id="text" autocomplete="off" placeholder="Safe ASCII text">
    <button id="sendText" class="wide accent">Type text</button>
  </div>
  <div class="status" id="status">Connecting…</div>
</aside>
<script>
${pageScript()}
</script>
</body>
</html>`;
}

/**
 * The page's own code. It is plain JavaScript inside this template literal, so it
 * avoids backticks, and escapes such as newlines are written with a doubled backslash.
 */
function pageScript() {
  return `"use strict";
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
const image=$("screen"),video=$("video"),device=$("device"),statusEl=$("status"),textInput=$("text");
const ctx=video.getContext("2d");
let csrf=null,logicalDisplay=null,reconnectTimer=null,pointer=null;
const view={transport:null,mode:null,reason:"",status:null,owner:"shared",paused:false,session:"idle",clients:null,width:0,height:0,note:"",rtt:null};
const stats={decoder:"none",codec:null,framesDecoded:0,framesRendered:0,framesDropped:0,packets:0,bytes:0,errors:0,renderTimes:[],lastFrameAt:0};
let videoSocket=null,controlSocket=null,decoder=null,config=null,waitingKey=true,pendingFrame=null,drawScheduled=false,configuring=false,pendingChunks=[];
let videoRetry=500,controlRetry=500,videoRefusals=0;
const supportedCodecs=new Map(),activePointers=new Map(),heldKeys=new Map();
function setStatus(value){statusEl.textContent=value}
function url(path,cacheBust=false){const params=new URLSearchParams();if(cacheBust)params.set("ts",String(Date.now()));const query=params.toString();return path+(query?"?"+query:"")}
function wsUrl(path){const params=new URLSearchParams();if(csrf)params.set("csrf",csrf);const query=params.toString();return(location.protocol==="https:"?"wss://":"ws://")+location.host+path+(query?"?"+query:"")}
async function post(path,body){const headers={"Content-Type":"application/json","X-Autonom-Origin":"human"};if(csrf)headers["X-Autonom-CSRF"]=csrf;const response=await fetch(url(path),{method:"POST",headers,body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.error||response.statusText);return payload}
function scrcpyActive(){return view.transport==="scrcpy"}
function surface(){return view.mode==="webcodecs"?video:image}
function restart(){clearTimeout(reconnectTimer);image.src=url("/stream.mjpeg",true)}
function note(message){view.note=message}

// Multipart path (and HTTP input when the transport is not scrcpy).
function point(event){const rect=image.getBoundingClientRect(),nw=image.naturalWidth,nh=image.naturalHeight;if(!nw||!nh)return null;const ratio=Math.min(rect.width/nw,rect.height/nh),rw=nw*ratio,rh=nh*ratio,xoff=(rect.width-rw)/2,yoff=(rect.height-rh)/2,px=(event.clientX-rect.left-xoff)/ratio,py=(event.clientY-rect.top-yoff)/ratio,dw=logicalDisplay?.width||nw,dh=logicalDisplay?.height||nh,x=Math.round(px*dw/nw),y=Math.round(py*dh/nh);if(x<0||y<0||x>dw||y>dh)return null;return{x,y}}
image.addEventListener("load",()=>note("video connected"));image.addEventListener("error",()=>{if(view.mode!=="multipart")return;note("stream reconnecting");reconnectTimer=setTimeout(restart,600)});
function startMultipart(reason){view.mode="multipart";view.reason=reason||"";stats.decoder="multipart";closeVideoSocket();if(decoder&&decoder.state!=="closed")decoder.close();decoder=null;video.hidden=true;image.hidden=false;restart()}

// scrcpy video: WebSocket packets decoded by WebCodecs, newest frame drawn per animation frame.
function startWebCodecs(){view.mode="webcodecs";stats.decoder="webcodecs";clearTimeout(reconnectTimer);image.removeAttribute("src");image.hidden=true;video.hidden=false;connectVideo()}
function closeVideoSocket(){const socket=videoSocket;videoSocket=null;if(socket)socket.close()}
function connectVideo(){if(videoSocket||view.mode!=="webcodecs")return;const socket=new WebSocket(wsUrl("/ws/video"));let opened=false;socket.binaryType="arraybuffer";videoSocket=socket;socket.onopen=()=>{opened=true;videoRetry=500;videoRefusals=0};socket.onmessage=event=>onVideoMessage(event.data);socket.onclose=()=>{if(videoSocket!==socket)return;videoSocket=null;waitingKey=true;if(!opened&&++videoRefusals>=4){fallback("the video WebSocket was refused");return}if(view.mode==="webcodecs"&&scrcpyActive()){setTimeout(connectVideo,videoRetry);videoRetry=Math.min(5000,videoRetry*2)}}}
function onVideoMessage(data){if(typeof data==="string"){try{applyState(JSON.parse(data))}catch{}return}const bytes=new Uint8Array(data);if(!bytes.length)return;const view8=new DataView(data);if(bytes[0]===1&&bytes.length>=9){setVideoSize(view8.getUint32(1),view8.getUint32(5));waitingKey=true}else if(bytes[0]===2){configure(bytes.slice(1))}else if(bytes[0]===3&&bytes.length>=10){stats.packets+=1;stats.bytes+=bytes.length-10;decode((bytes[1]&1)===1,Number(view8.getBigUint64(2)),bytes.subarray(10))}}
function setVideoSize(width,height){if(!width||!height)return;view.width=width;view.height=height;if(video.width!==width||video.height!==height){video.width=width;video.height=height}device.style.aspectRatio=width+" / "+height}
function codecFromSps(data){for(let i=0;i+3<data.length;i++){if(data[i]===0&&data[i+1]===0&&data[i+2]===1&&(data[i+3]&31)===7){const out=[];let zeros=0;for(let j=i+3;j<data.length&&out.length<4;j++){const b=data[j];if(zeros>=2&&b===3){zeros=0;continue}zeros=b===0?zeros+1:0;out.push(b)}if(out.length<4)return null;return"avc1."+[out[1],out[2],out[3]].map(b=>b.toString(16).toUpperCase().padStart(2,"0")).join("")}}return null}
function fallback(reason){note(reason);startMultipart(reason)}
function configure(data){config=data;waitingKey=true;const codec=codecFromSps(data);if(!codec){fallback("the stream has no H.264 SPS");return}stats.codec=codec;const known=supportedCodecs.get(codec);if(known===true){applyDecoderConfig(codec);return}if(known===false){fallback("VideoDecoder rejects "+codec);return}configuring=true;VideoDecoder.isConfigSupported({codec,optimizeForLatency:true}).then(result=>{configuring=false;supportedCodecs.set(codec,Boolean(result.supported));if(!result.supported){pendingChunks=[];fallback("VideoDecoder rejects "+codec);return}applyDecoderConfig(codec);const queued=pendingChunks;pendingChunks=[];for(const item of queued)decode(item[0],item[1],item[2])}).catch(error=>{configuring=false;pendingChunks=[];fallback("VideoDecoder check failed: "+error.message)})}
function applyDecoderConfig(codec){if(view.mode!=="webcodecs")return;if(!decoder||decoder.state==="closed")decoder=new VideoDecoder({output:onFrame,error:onDecoderError});decoder.configure({codec,optimizeForLatency:true});waitingKey=true}
function decode(key,pts,data){if(configuring){if(pendingChunks.length<300)pendingChunks.push([key,pts,data]);else{pendingChunks=[];waitingKey=true}return}if(!decoder||decoder.state!=="configured"){stats.framesDropped+=1;waitingKey=true;return}if(waitingKey&&!key){stats.framesDropped+=1;return}if(!key&&decoder.decodeQueueSize>30){waitingKey=true;stats.framesDropped+=1;sendControl({t:"system",op:"keyframe"});return}let chunk=data;if(key&&config){chunk=new Uint8Array(config.length+data.length);chunk.set(config);chunk.set(data,config.length)}try{decoder.decode(new EncodedVideoChunk({type:key?"key":"delta",timestamp:pts,data:chunk}));waitingKey=false}catch(error){onDecoderError(error)}}
function onFrame(frame){stats.framesDecoded+=1;if(pendingFrame){pendingFrame.close();stats.framesDropped+=1}pendingFrame=frame;if(!drawScheduled){drawScheduled=true;requestAnimationFrame(draw)}}
function draw(){drawScheduled=false;const frame=pendingFrame;if(!frame)return;pendingFrame=null;if(video.width!==frame.displayWidth||video.height!==frame.displayHeight){video.width=frame.displayWidth;video.height=frame.displayHeight}ctx.drawImage(frame,0,0,video.width,video.height);frame.close();const now=performance.now();stats.framesRendered+=1;stats.lastFrameAt=now;stats.renderTimes.push(now);while(stats.renderTimes.length&&now-stats.renderTimes[0]>1000)stats.renderTimes.shift()}
function onDecoderError(error){stats.errors+=1;note("decoder: "+(error&&error.message||error));try{if(decoder&&decoder.state!=="closed")decoder.close()}catch{}decoder=null;waitingKey=true;if(stats.codec&&supportedCodecs.get(stats.codec))applyDecoderConfig(stats.codec);sendControl({t:"system",op:"keyframe"})}

// scrcpy control: one WebSocket carrying touch, wheel, keys, text and system actions.
function connectControl(){if(controlSocket||!scrcpyActive())return;const socket=new WebSocket(wsUrl("/ws/control"));controlSocket=socket;socket.onopen=()=>{controlRetry=500};socket.onmessage=event=>{let message;try{message=JSON.parse(event.data)}catch{return}onControlMessage(message)};socket.onclose=()=>{if(controlSocket!==socket)return;controlSocket=null;activePointers.clear();if(scrcpyActive()){setTimeout(connectControl,controlRetry);controlRetry=Math.min(5000,controlRetry*2)}}}
function closeControlSocket(){const socket=controlSocket;controlSocket=null;if(socket)socket.close()}
function onControlMessage(message){if(message.t==="state")applyState(message);else if(message.t==="error"){stats.errors+=1;note(message.message)}else if(message.t==="clipboard"){if(typeof message.text!=="string"){note("the device clipboard has no text");return}textInput.value=message.text;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(message.text).catch(()=>{});note("device clipboard copied")}else if(message.t==="pong"){view.rtt=Math.round(performance.now()-message.ts)}}
function sendControl(message){if(!controlSocket||controlSocket.readyState!==1)return false;controlSocket.send(JSON.stringify(message));return true}
function applyState(message){view.owner=message.owner;view.paused=message.paused;view.session=message.session;view.clients=message.clients;if(view.mode==="webcodecs"&&message.width&&message.height)setVideoSize(message.width,message.height);if(message.transport&&message.transport!==view.transport){view.transport=message.transport;applyTransport()}}
function applyTransport(){textInput.placeholder=scrcpyActive()?"Text (Unicode)":"Safe ASCII text";for(const button of document.querySelectorAll("[data-scrcpy]"))button.hidden=!scrcpyActive();if(scrcpyActive()){connectControl();if(!view.mode){if(typeof VideoDecoder==="function"&&typeof EncodedVideoChunk==="function")startWebCodecs();else startMultipart("VideoDecoder is not available in this browser")}}else{closeControlSocket();if(view.mode!=="multipart")startMultipart("")}}
function clamp01(value){return Math.min(1,Math.max(0,value))}
function normalized(event,clampInside){const element=surface(),rect=element.getBoundingClientRect(),width=view.mode==="webcodecs"?video.width:image.naturalWidth,height=view.mode==="webcodecs"?video.height:image.naturalHeight;if(!width||!height||!rect.width||!rect.height)return null;const ratio=Math.min(rect.width/width,rect.height/height),rw=width*ratio,rh=height*ratio,x=(event.clientX-rect.left-(rect.width-rw)/2)/rw,y=(event.clientY-rect.top-(rect.height-rh)/2)/rh;if(clampInside)return{x:clamp01(x),y:clamp01(y)};if(x<0||y<0||x>1||y>1)return null;return{x,y}}
function pressure(event){return event.pointerType==="mouse"||!event.pressure?1:Math.min(1,event.pressure)}
// Ctrl- or Alt-drag adds a second finger mirrored around the centre: a pinch.
function mirrorId(id){return -1000-id}
function sendTouch(action,id,at,pinch,p){sendControl({t:"touch",a:action,id,x:at.x,y:at.y,p});if(pinch)sendControl({t:"touch",a:action,id:mirrorId(id),x:1-at.x,y:1-at.y,p})}
function onPointerDown(event){if(!scrcpyActive()){legacyPointerDown(event);return}if(event.pointerType==="mouse"&&event.button!==0)return;const at=normalized(event,false);if(!at)return;event.preventDefault();surface().focus();surface().setPointerCapture(event.pointerId);const pinch=event.pointerType==="mouse"&&(event.ctrlKey||event.altKey);activePointers.set(event.pointerId,{pinch});sendTouch("down",event.pointerId,at,pinch,pressure(event))}
function onPointerMove(event){const active=activePointers.get(event.pointerId);if(!active)return;const at=normalized(event,true);if(at)sendTouch("move",event.pointerId,at,active.pinch,pressure(event))}
function onPointerEnd(event,action){const active=activePointers.get(event.pointerId);if(!active)return;activePointers.delete(event.pointerId);const at=normalized(event,true)||{x:0.5,y:0.5};sendTouch(action,event.pointerId,at,active.pinch,0)}
function legacyPointerDown(event){const p=point(event);if(!p)return;image.setPointerCapture(event.pointerId);pointer={...p,time:performance.now(),id:event.pointerId}}
async function legacyPointerUp(event){if(!pointer)return;const end=point(event)||pointer,start=pointer;pointer=null;const duration=Math.max(1,Math.round(performance.now()-start.time)),distance=Math.hypot(end.x-start.x,end.y-start.y);try{if(distance<12&&duration<500)await post("/tap",{x:end.x,y:end.y});else await post("/swipe",{x1:start.x,y1:start.y,x2:end.x,y2:end.y,duration:Math.min(5000,duration)})}catch(error){note(error.message)}}
for(const element of [image,video]){
  element.addEventListener("pointerdown",onPointerDown);
  element.addEventListener("pointermove",onPointerMove);
  element.addEventListener("pointerup",event=>{if(scrcpyActive())onPointerEnd(event,"up");else legacyPointerUp(event)});
  element.addEventListener("pointercancel",event=>{if(scrcpyActive())onPointerEnd(event,"cancel");else pointer=null});
  element.addEventListener("contextmenu",event=>{if(scrcpyActive())event.preventDefault()});
  element.addEventListener("wheel",onWheel,{passive:false});
  element.addEventListener("keydown",onKeyDown);
  element.addEventListener("keyup",onKeyUp);
  element.addEventListener("blur",releaseKeys);
  element.addEventListener("paste",onPaste);
}
async function onWheel(event){event.preventDefault();if(scrcpyActive()){const at=normalized(event,false);if(!at)return;const scale=event.deltaMode===1?1/3:event.deltaMode===2?1:1/100;const dx=Math.max(-16,Math.min(16,event.deltaX*scale)),dy=Math.max(-16,Math.min(16,-event.deltaY*scale));if(dx||dy)sendControl({t:"scroll",x:at.x,y:at.y,dx,dy});return}if(!image.naturalWidth)return;const width=logicalDisplay?.width||image.naturalWidth,height=logicalDisplay?.height||image.naturalHeight,x=Math.round(width/2),y1=Math.round(height*.55),y2=Math.round(height*(event.deltaY>0?.25:.78));try{await post("/swipe",{x1:x,y1,x2:x,y2,duration:220})}catch(error){note(error.message)}}
function metaFor(event){return metaStateFor({shiftKey:event.shiftKey,ctrlKey:event.ctrlKey,altKey:event.altKey,metaKey:event.metaKey})}
function onKeyDown(event){if(!scrcpyActive())return;const input=keyInputFor(event,KEYCODES,US_KEY_CHARS);if(!input)return;event.preventDefault();if(input.text!==undefined){sendControl({t:"text",text:input.text});return}const repeat=event.repeat?(heldKeys.get(event.code)||0)+1:0;heldKeys.set(event.code,repeat);sendControl({t:"key",a:"down",code:input.code,meta:metaFor(event),repeat})}
function onKeyUp(event){if(!scrcpyActive()||!heldKeys.has(event.code))return;event.preventDefault();heldKeys.delete(event.code);sendControl({t:"key",a:"up",code:KEYCODES[event.code],meta:metaFor(event),repeat:0})}
function releaseKeys(){for(const name of heldKeys.keys()){const code=KEYCODES[name];if(code!==undefined)sendControl({t:"key",a:"up",code,meta:0,repeat:0})}heldKeys.clear()}
function onPaste(event){if(!scrcpyActive())return;const text=event.clipboardData&&event.clipboardData.getData("text/plain");if(!text)return;event.preventDefault();const message={t:"paste",text};if(!fitsControl(message,"clipboard text"))return;sendControl(message)}
function utf8Length(text){return new TextEncoder().encode(text).length}
function fitsControl(message,what){const size=utf8Length(JSON.stringify(message));if(size<=MAX_CONTROL_BYTES)return true;note(what+" is too large: "+size+" bytes as a control message after JSON escaping, at most "+MAX_CONTROL_BYTES+"; nothing was sent");return false}
for(const button of document.querySelectorAll("[data-system],[data-code],[data-key]")){button.addEventListener("click",()=>{if(scrcpyActive()){if(button.dataset.system)sendControl({t:"system",op:button.dataset.system});else if(button.dataset.code){const code=Number(button.dataset.code);sendControl({t:"key",a:"down",code,meta:0,repeat:0});sendControl({t:"key",a:"up",code,meta:0,repeat:0})}return}if(button.dataset.key)post("/key",{key:button.dataset.key}).catch(error=>note(error.message))})}
$("clipboard").onclick=()=>sendControl({t:"clipboard-get"});
$("refresh").onclick=()=>{if(view.mode==="webcodecs"){closeVideoSocket();waitingKey=true;videoRetry=500;connectVideo()}else restart()};
async function sendText(){const value=textInput.value;if(!value)return;if(scrcpyActive()){const message=utf8Length(value)<=300?{t:"text",text:value}:{t:"paste",text:value};if(!fitsControl(message,"the text"))return;const ok=sendControl(message);if(ok)textInput.value="";return}try{await post("/text",{text:value});textInput.value=""}catch(error){note(error.message)}}
$("sendText").onclick=sendText;textInput.addEventListener("keydown",event=>{if(event.key==="Enter")sendText()});
function transportLabel(){if(!view.transport)return"connecting";if(view.transport!=="scrcpy")return view.transport;if(view.mode==="webcodecs")return"scrcpy (webcodecs)";return"scrcpy (multipart"+(view.reason?": "+view.reason:"")+")"}
function render(){const data=view.status||{},lines=["platform: "+(data.platform||"unknown"),"transport: "+transportLabel()];if(data.fallback_reason)lines.push("fallback: "+data.fallback_reason);if(view.mode==="webcodecs"){lines.push("decoder: "+(stats.codec||"waiting for config"));lines.push("fps: "+stats.renderTimes.length+" rendered, "+stats.framesDecoded+" decoded, "+stats.framesDropped+" dropped, queue "+(decoder?decoder.decodeQueueSize:0));lines.push("video: "+(view.width?view.width+"x"+view.height:"waiting")+", session "+view.session)}else{lines.push("frames: "+(data.frames_sent??0)+"\\nstream clients: "+(data.stream_clients??0))}if(view.clients)lines.push("clients: video "+view.clients.video+", control "+view.clients.control);lines.push("display: "+(data.display?data.display.width+"x"+data.display.height:"unknown"));lines.push("control: "+view.owner+(view.paused?" (paused)":""));if(data.last_error)lines.push("last error: "+data.last_error);if(view.note)lines.push("note: "+view.note);setStatus(lines.join("\\n"))}
async function poll(){try{const response=await fetch(url("/status")),data=await response.json();if(!response.ok)throw new Error(data.error||response.statusText);view.status=data;logicalDisplay=data.display;view.owner=data.control_owner;view.paused=data.input_paused;if(data.transport!==view.transport){view.transport=data.transport;applyTransport()}}catch(error){note(error.message);if(!view.mode)startMultipart("status unavailable")}finally{setTimeout(poll,1200)}}
function snapshot(){return{transport:view.transport,mode:view.mode,decoder:stats.decoder,fallbackReason:view.reason||null,codec:stats.codec,framesDecoded:stats.framesDecoded,framesRendered:stats.framesRendered,framesDropped:stats.framesDropped,decodeQueueSize:decoder?decoder.decodeQueueSize:0,fps:stats.renderTimes.length,packets:stats.packets,bytes:stats.bytes,errors:stats.errors,lastFrameAt:stats.lastFrameAt,width:view.width,height:view.height,session:view.session,owner:view.owner,paused:view.paused,clients:view.clients,control:controlSocket?controlSocket.readyState:-1,video:videoSocket?videoSocket.readyState:-1,rtt:view.rtt,note:view.note,status:statusEl.textContent}}
window.autonomCanvas=Object.freeze({stats:snapshot,send:sendControl});
async function authenticate(){const body=bootstrapToken?{token:bootstrapToken}:{};const response=await fetch("/auth",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(response.ok){csrf=payload.csrf;return}if(bootstrapToken)throw new Error(payload.error||"Authentication failed");note("open the Canvas URL with its #token to sign in")}
async function bootstrap(){await authenticate();setInterval(render,500);await poll()}
bootstrap().catch(error=>setStatus(error.message));`;
}

function sendJson(response, status, value, headers = {}) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": body.length,
    ...headers,
  });
  response.end(body);
}

function sendHtml(response, html) {
  const body = Buffer.from(html);
  response.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    // Another local port is the same site, so its pages share the cookie: a framed
    // Canvas would work for them and could be clickjacked.
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Length": body.length,
  });
  response.end(body);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function isPng(buffer) {
  return Buffer.isBuffer(buffer) && buffer.length > 8 && buffer.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
}

function httpError(statusCode, message) {
  const error = new Error(message);
  error.statusCode = statusCode;
  return error;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
