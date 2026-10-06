#!/usr/bin/env node
import { mkdtempSync, rmSync } from "node:fs";
import { access, constants, readFile, rm } from "node:fs/promises";
import { ServerResponse, createServer } from "node:http";
import { homedir, tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { env, exit, platform } from "node:process";
import { execFile, spawn } from "node:child_process";
import { EventEmitter } from "node:events";
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
  screenrecordMaxSize,
  streamMaxSize,
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
import {
  DEFAULT_VIDEO_OPTIONS,
  ERROR_CODE,
  IDB_CAPABILITY,
  IdbCompanion,
  StructuredError,
  idbUnavailableError,
  resolveIdbCompanionBinary,
} from "./ios-idb-companion.mjs";

const execFileAsync = promisify(execFile);
const BOUNDARY = "autonom-frame";

// The most unsent bytes the Canvas holds per client: past it a video client drops
// packets until a key frame, and a control client that does not read its replies is closed.
const CLIENT_BACKLOG_BYTES = 4 * 1024 * 1024;
// Any control message can change the state that goes to every client, and a state
// message is a snapshot: a client with this much unsent data gets no new one, only the
// current state once its socket drains or ahead of the next packet or reply it is sent
// (catchUpState). Many small messages cost far more memory than their bytes, so this
// bound is small.
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
// Android display presets (DISPLAY-001), from the SDK device profiles small_phone,
// pixel_fold and pixel_tablet and the published Pixel 11 display. `default` resets the
// target to its own size and density. The order is the order pages and /status show.
const DISPLAY_PRESETS = Object.freeze([
  { id: "small", label: "Small phone", width: 720, height: 1280, density: 320 },
  { id: "pixel-11", label: "Pixel 11", width: 1080, height: 2424, density: 420 },
  { id: "pixel-fold", label: "Pixel Fold (open)", width: 2208, height: 1840, density: 420 },
  { id: "tablet", label: "Tablet", width: 2560, height: 1600, density: 320 },
  { id: "default", label: "Device default" },
].map((preset) => Object.freeze(preset)));
const DISPLAY_PRESET_IDS = Object.freeze(DISPLAY_PRESETS.map((preset) => preset.id));
// The presets as every state message carries them, serialized once: a flood of handoff
// changes sends one state message per change to every client.
const DISPLAY_PRESETS_JSON = JSON.stringify(DISPLAY_PRESETS);
// Each `wm` call; the stop-time restore runs inside SHUTDOWN_STOP_MS as a whole.
const WM_TIMEOUT_MS = 5000;
// /status and the state message show a `wm` reading at most this old (DISPLAY-001). Pages
// poll /status about every second, so readings are shared instead of made per request.
const DISPLAY_READ_MAX_AGE_MS = 2000;
// State messages go to WebSocket clients, which need not ask /status: while one is
// connected the reading is renewed once it is this old, so it stays within the age above
// as long as adb answers a reading within the difference.
const DISPLAY_WATCH_AGE_MS = 1500;
// The largest display side `dw` and `dh` of POST /tap and /swipe may name, as the journal's.
const MAX_SENT_DISPLAY_SIDE = 10_000;
// Transports whose video and input go over /ws/video and /ws/control: scrcpy on Android,
// idb (one idb_companion H.264 stream and its HID stream) on the iOS Simulator.
const FAST_TRANSPORTS = new Set(["scrcpy", "idb"]);
// A fast stream that sends no frame this long after it opened is ended and opened again.
const IDB_FIRST_FRAME_MS = 10_000;
// The companion has no key frame on demand; it sends one every DEFAULT_VIDEO_OPTIONS
// .keyFrameRate seconds (DEC-004). A key frame asked for and not seen within this wait is
// forced with Stop and Start on the same stream, at most once per IDB_FORCED_KEY_FRAME_MS.
const IDB_KEY_FRAME_WAIT_MS = 2500;
const IDB_FORCED_KEY_FRAME_MS = 5000;
// The Simulator state is read this often while the fast stream runs, so a reboot restarts
// the frozen stream (IOSF-006); the orientation is read this often for rotated pictures.
const IDB_SIMULATOR_WATCH_MS = 2000;
const IDB_ORIENTATION_MS = 1000;
const IDB_RETRY_MIN_MS = 500;
const IDB_RETRY_MAX_MS = 5000;
// A stream that ran this long resets the restart backoff.
const IDB_STABLE_MS = 10_000;
// The page's system buttons the Simulator has a hardware button for.
const IOS_BUTTONS = Object.freeze({
  home: "HOME", power: "LOCK", "volume-up": "VOLUME_UP", "volume-down": "VOLUME_DOWN",
});
// Clockwise degrees that turn the Simulator's portrait picture upright for each
// orientation; the picture itself stays portrait when the Simulator rotates.
const IOS_ROTATION = Object.freeze({ LANDSCAPE_LEFT: 270, LANDSCAPE_RIGHT: 90, PORTRAIT_UPSIDE_DOWN: 180 });
// A wheel burst is one synthetic drag: the finger moves this many points per wheel step and
// lifts once the wheel has been still IOS_WHEEL_IDLE_MS, so iOS sees no fling at the end.
// It never taps: every drag goes down at an anchor at least IOS_WHEEL_MARGIN_POINTS inside
// each edge and its first move goes at least IOS_WHEEL_MIN_POINTS in the wheel's direction,
// past the iOS tap slop (about 10 points); its points stay IOS_WHEEL_EDGE_POINTS inside the screen.
const IOS_WHEEL_POINTS = 24;
const IOS_WHEEL_MIN_POINTS = 16;
const IOS_WHEEL_MARGIN_POINTS = 32;
const IOS_WHEEL_EDGE_POINTS = 2;
const IOS_WHEEL_IDLE_MS = 150;
// A HID write the companion has not taken within this long no longer holds the input queue;
// the HID stream still keeps its order, and the companion is restarted when it is gone.
const IOS_HID_WRITE_MS = 5000;
const IOS_ONE_FINGER = "The iOS Simulator takes one finger at a time: this pointer was refused while another is down";
// Two pointers of one connection are one pinch: idb has no live second finger, only a canned
// HIDPinch sent when either pointer lifts. The page's mirrored pinch finger has the id
// -1000 - id and comes before its partner, so nothing goes down before the pair is complete.
const IOS_MIRROR_ID_MAX = -1000;
// A pinch replays at most this long on the Simulator, so a long hold cannot stall the HID
// stream; its fingers stay at least IOS_PINCH_MIN_RADIUS points from its center.
const IOS_PINCH_MAX_S = 2;
const IOS_PINCH_MIN_RADIUS = 8;
// The Simulator clipboard is read and set with `xcrun simctl pbpaste|pbcopy UDID`. A paste
// that fits the HID text path is also typed: at most IOS_TEXT_MAX_BYTES and only characters
// idb's `ui text` has a key for (IOS_TYPEABLE_TEXT: printable ASCII and newline; any other
// character fails with "No keycode found"). Other text is only set, and long-press Paste
// inserts it (idb Cmd+V does not paste text set on the Mac).
const IOS_TEXT_MAX_BYTES = 300;
const IOS_TYPEABLE_TEXT = /^[\n\x20-\x7e]*$/;
const IOS_SIMCTL_MS = 5000;
const IOS_CLIPBOARD_MAX_BYTES = 256 * 1024;
const IOS_EMPTY_PASTEBOARD = "There are no items on the device's pasteboard.";
const IOS_PASTE_INSERT = "touch and hold a text field and choose Paste to insert it";
const IOS_PASTE_TOO_LONG = "The Simulator clipboard was set but the text is longer than " +
  `${IOS_TEXT_MAX_BYTES} bytes, so it was not typed: ${IOS_PASTE_INSERT}`;
const IOS_PASTE_NOT_TYPEABLE = "The Simulator clipboard was set but the text has characters " +
  `other than plain ASCII, which the Simulator cannot type, so it was not typed: ${IOS_PASTE_INSERT}`;

main().catch((error) => {
  // A refusal with a code is the CLI's JSON error object on stderr and exit code 2, as
  // `autonom canvas serve` prints it when it refuses first.
  if (error instanceof StructuredError) {
    console.error(JSON.stringify(error));
    exit(2);
  }
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
  if (!isIos && (options.transport === "idb" || options.idbCompanion !== undefined)) {
    throw new StructuredError(ERROR_CODE.UNSUPPORTED_ON_PLATFORM, "the idb transport mirrors iOS Simulators only", {
      hint: "On Android use --transport auto or scrcpy.", capability: IDB_CAPABILITY,
    });
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
  const [scrcpy, screenrecordSupported, idb] = await Promise.all([
    resolveScrcpy(options, isIos),
    isIos ? false : supportsScreenrecord(adbPath, serial),
    resolveIdbCompanion(options, isIos),
  ]);
  if (options.transport === "scrcpy" && !scrcpy.available) throw scrcpyError(scrcpy.reason);
  if (options.transport === "idb" && !idb.available) throw idbUnavailableError(idb.reason);
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
    idbFailed: null,
    // The scrcpy stream size and how it was chosen (streamMaxSize): known once a session
    // has probed the encoders, or from the start with --max-size.
    streamSize: options.maxSizeExplicit ? streamMaxSize(options) : null,
    streamSizeLogged: null,
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
    idb,
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
    // Waiting until no device write is held behind a paste (heldWritesSent).
    heldWritesWaiters: [],
    clipboardSequence: 0n,
    // Arrival order of held control messages, across connections.
    heldOrder: 0,
    // iOS: the one finger down on the Simulator as its connection sees it,
    // {client, id, x, y, point, wheel, pressed}.
    iosFinger: null,
    // iOS input in message order (iosEnqueue): HID events, text for the bridge and records,
    // each run once the one before it is done, and whether the queue is running.
    iosInput: [],
    iosInputRunning: false,
    iosResumeQueued: false,
    // Counts companion losses: HID events queued before one never reach the next companion.
    iosEpoch: 0,
    // What the HID stream of the current session and epoch holds down, by the events written
    // to it: {session, epoch, touch: point or null, buttons}. When the companion or stream is
    // lost (or the session stops) it becomes the orphan, {touch, buttons}, lifted with UPs
    // through the next companion that streams, whatever the queue held then.
    iosHeld: null,
    iosOrphan: null,
    // Device pointer id → the last up for it: {x, y, at}, `at` null until it is written.
    recentUps: new Map(),
    // Android keycode → the last key-up for it: {at}, `at` null until it is written.
    recentKeyUps: new Map(),
    // Pointers (device id → {x, y}) and keys down when the device server died, lifted
    // through the next server once its video size is known.
    orphans: { pointers: new Map(), keys: new Set() },
    display: null,
    // The newest `wm` reading (readWm, null when it failed), when it started, the reading
    // in flight, and whether the last one failed (last_error says so once). The preset it
    // stands for (presetInEffect), from the last preset whose commands succeeded and whose
    // read-back matched it.
    displayWm: null,
    displayWmAt: -Infinity,
    displayReading: null,
    displayReadFailed: false,
    displayPreset: null,
    displayApplied: null,
    // The timer renewing the reading while WebSocket clients are connected (watchDisplay),
    // and whether a state message went out with a reading older than allowed, so the next
    // reading is sent to every client even when it shows the same.
    displayWatch: null,
    displayStaleSent: false,
    // The size and density overrides found before the first change, and whether a `wm`
    // command may have changed the display since, so the stop puts them back.
    displayOriginal: null,
    displayWritten: false,
    // The change waiting in the input queue, at most one, and the `wm` commands of the one
    // running, which are all the restore at stop waits for.
    displayWaiting: null,
    displayRunning: null,
    // The bridge of its own that writes the restore record while the shared one is busy.
    restoreBridge: null,
    // Tasks ever queued: a waiting change with nothing queued after it is the last one.
    inputEnqueued: 0,
    journalQueue: [],
    journalDraining: false,
    journalInFlight: 0,
    journalSlot: null,
    journalDropped: 0,
    broadcaster: null,
    shuttingDown: false,
    enqueueInput(task) {
      inputQueue = inputQueue.catch(() => {}).then(() => {
        // Once the Canvas stops, queued input is dropped instead of delaying the restore.
        if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
        return task();
      });
      context.inputEnqueued += 1;
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
    clearTimeout(context.displayWatch);
    // The device server and the adb forward must be gone before the process is, and the
    // display back as it was, within the 5 s the supervisor allows after SIGTERM.
    await Promise.race([
      Promise.all([stopSession(context), restoreDisplay(context)]),
      sleep(SHUTDOWN_STOP_MS),
    ]);
    actionBridge.close();
    context.restoreBridge?.close();
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
  --idb-companion PATH        idb_companion for the iOS idb transport (default:
                              AUTONOM_IDB_COMPANION_BIN, then PATH).
  --ffmpeg PATH               ffmpeg executable path.
  --port, -p PORT             Localhost port (default: 3277; 0 chooses a free port).
  --transport MODE            auto, scrcpy, idb, screenrecord, or screencap (default: auto).
                              auto prefers scrcpy on Android when a ${SCRCPY_PROTOCOL_VERSION} server is found,
                              and idb on the iOS Simulator when idb_companion is found.
  --fps FPS                   Frame rate cap (default: 15; ${DEFAULT_SCRCPY_MAX_FPS} on scrcpy and idb).
  --max-size PX               Longest video side on scrcpy, video width on screenrecord;
                              0 is native size. Default on scrcpy: follows the device's
                              H.264 encoder (2048 when software only, native with a
                              hardware one, 1280 when it cannot tell); otherwise 1280.
  --bit-rate BPS              H.264 bitrate (default: 12000000).
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

/**
 * The idb_companion of the iOS fast transport (resolveIdbCompanionBinary): --idb-companion,
 * else AUTONOM_IDB_COMPANION_BIN, else idb_companion on PATH. AUTONOM_IDB_COMPANION is the
 * remote companion of the CLI's idb calls and is never read here.
 */
async function resolveIdbCompanion(options, isIos) {
  if (!isIos || options.transport === "screencap" || options.transport === "screenrecord") {
    return { available: false, path: null, source: null, reason: null };
  }
  return resolveIdbCompanionBinary({ flag: options.idbCompanion, env });
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
  } else if (request.method === "POST" && url.pathname === "/display") {
    await setDisplay(context, response, await readJsonBody(request), origin);
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

/**
 * A WebSocket handshake for /ws/video or /ws/control: the Upgrade tokens include `websocket`.
 * It never throws, since it also runs as the server's shouldUpgradeCallback.
 */
function isCanvasWebSocket(request) {
  try {
    const tokens = String(request.headers.upgrade ?? "").split(",").map((token) => token.trim().toLowerCase());
    if (!tokens.includes("websocket")) return false;
    const url = requestUrl(request);
    return url !== null && (url.pathname === "/ws/video" || url.pathname === "/ws/control");
  } catch {
    return false;
  }
}

/**
 * Whether an upgrade request says it carries a body: any Transfer-Encoding, or a
 * Content-Length that is not digits only (signed, empty, not a number) or not zero.
 * Any run of zeros, as in `Content-Length: 00`, is no body.
 */
function upgradeRequestHasBody(headers) {
  if (headers["transfer-encoding"] !== undefined) return true;
  if (headers["content-length"] === undefined) return false;
  const length = String(headers["content-length"]).trim();
  return !/^\d+$/.test(length) || Number(length) !== 0;
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
  if (upgradeRequestHasBody(request.headers)) {
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
  if (!FAST_TRANSPORTS.has(chooseTransport(context))) {
    rejectUpgrade(socket, 409, `The ${isIosCanvas(context) ? "idb" : "scrcpy"} transport is not active on this Canvas`);
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
  const { options, scrcpy, idb, state } = context;
  if (options.transport === "scrcpy") return "scrcpy";
  if (options.transport === "idb") return "idb";
  if (options.transport === "auto" && scrcpy.available && !state.scrcpyFailed) return "scrcpy";
  if (options.transport === "auto" && idb.available && !state.idbFailed) return "idb";
  return multipartTransport(context);
}

/** An iOS Canvas: its only WebSocket transport is idb. */
function isIosCanvas(context) {
  return context.options.platform === "ios";
}

/** The transport behind /stream.mjpeg, also used when a page cannot decode scrcpy video. */
function multipartTransport(context) {
  const { options, ffmpegPath, screenrecordSupported, state } = context;
  if (options.transport === "screencap") return "screencap";
  if (options.transport === "screenrecord") return "screenrecord";
  if (ffmpegPath && screenrecordSupported && !state.acceleratedFailed) return "screenrecord";
  return "screencap";
}

/** Why auto mode is not on its first choice, or null. */
function fallbackReason(context) {
  const { options, scrcpy, idb, state, ffmpegPath, screenrecordSupported } = context;
  if (options.transport === "auto" && isIosCanvas(context)) {
    if (chooseTransport(context) === "idb") return null;
    return state.idbFailed ? `idb failed to start: ${state.idbFailed}` : idb.reason;
  }
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
  const [measured] = await Promise.all([
    iosStreamDisplay(context) ?? context.actionBridge.call("screen-size", {}, "system"),
    freshDisplay(context),
  ]);
  if (measured.display) context.display = measured.display;
  const transport = chooseTransport(context);
  sendJson(response, 200, {
    platform: context.options.platform,
    serial: context.serial,
    display: displayStatus(context, measured.display),
    transport,
    requested_transport: context.options.transport,
    fallback_reason: fallbackReason(context),
    ffmpeg: Boolean(context.ffmpegPath),
    screenrecord_h264: context.screenrecordSupported,
    // The width the multipart screenrecord stream is scaled to, while it is the transport.
    screenrecord_max_size: transport === "screenrecord" ? screenrecordMaxSize(context.options) : null,
    direct_h264_url: context.screenrecordSupported ? "/stream.h264" : null,
    frames_sent: context.state.framesSent,
    last_frame_at: context.state.lastFrameAt,
    stream_clients: context.state.streamClients,
    uptime_seconds: Math.round((performance.now() - context.state.startedAt) / 1000),
    last_error: context.state.lastError,
    control_owner: context.state.controlOwner,
    input_paused: context.state.inputPaused,
    scrcpy: scrcpyStatus(context),
    idb: idbStatus(context),
  });
}

/**
 * The stream size of the transport in use, as `state` and `/status` report it: what scrcpy
 * was asked for (null until a session chose), the width screenrecord is scaled to, or null
 * on screencap and idb. A size chosen for scrcpy is not reported once the Canvas fell back.
 */
function streamSizeInUse(context) {
  const transport = chooseTransport(context);
  if (transport === "scrcpy") return context.state.streamSize;
  if (transport !== "screenrecord") return null;
  const source = context.options.maxSizeExplicit ? "explicit" : "default";
  return { maxSize: screenrecordMaxSize(context.options), source, encoder: null, encoderName: null };
}

/** The iOS fast transport in /status: where the companion came from and what the stream did. */
function idbStatus(context) {
  if (!context.idb.available) return null;
  const session = context.session instanceof IosFastSession ? context.session : null;
  const stats = session?.stats;
  return {
    companion_path: context.idb.path,
    source: context.idb.source,
    session_state: session?.state ?? "idle",
    companion_pid: stats?.companionPid ?? null,
    width: context.video.size?.width ?? null,
    height: context.video.size?.height ?? null,
    points: stats?.points ?? null,
    orientation: session?.orientation ?? null,
    rotation: iosRotation(context),
    packets: stats?.packets ?? 0,
    bytes: stats?.bytes ?? 0,
    key_frames: stats?.keyFrames ?? 0,
    restarts: stats?.restarts ?? 0,
    companion_starts: stats?.companionStarts ?? 0,
    streams_opened: stats?.streamsOpened ?? 0,
    stream_restarts: stats?.streamRestarts ?? 0,
    forced_key_frames: stats?.forcedKeyFrames ?? 0,
    hid_events: stats?.hidEvents ?? 0,
    video_clients: context.videoClients.size,
    control_clients: context.controlClients.size,
    journal_pending: journalPending(context),
    journal_dropped: context.journalDropped,
  };
}

function scrcpyStatus(context) {
  if (!context.scrcpy.available) return null;
  const stats = context.session?.stats;
  const scrcpySize = chooseTransport(context) === "scrcpy" ? context.state.streamSize : null;
  return {
    version: context.scrcpy.version,
    server_path: context.scrcpy.serverPath,
    source: context.scrcpy.source,
    session_state: context.session?.state ?? "idle",
    // The size asked of scrcpy-server (0 is native) and why: "explicit" (--max-size),
    // "encoder" (the encoder named) or "default" (the encoders could not be read); all
    // null until the first session has chosen, and while scrcpy is not the transport.
    max_size: scrcpySize?.maxSize ?? null,
    max_size_source: scrcpySize?.source ?? null,
    encoder: scrcpySize?.encoder ?? null,
    encoder_name: scrcpySize?.encoderName ?? null,
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
  // "screenrecord" or "screencap" while a capture of that kind runs.
  #capture = null;

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

  /**
   * The display size changed (DISPLAY-008). screenrecord keeps the size it started with,
   * so its capture ends and the loop starts a new one; each screencap has the new size.
   */
  restartCapture() {
    if (this.#capture === "screenrecord") this.#stopCapture?.();
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
      this.#capture = null;
    }
    if (this.#clients.size) {
      setTimeout(() => {
        if (!this.#running && this.#clients.size) this.#run();
      }, 500);
    }
  }

  async #screencap() {
    const context = this.#context;
    this.#capture = "screencap";
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
    const filter = `fps=${options.fps},scale=min(${screenrecordMaxSize(options)}\\,iw):-2`;
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
    let stopped = false;
    let stderr = "";
    const keepStderr = (chunk) => { stderr = (stderr + chunk.toString()).slice(-4000); };
    adb.stderr.on("data", keepStderr);
    ffmpeg.stderr.on("data", keepStderr);
    this.#capture = "screenrecord";
    this.#stopCapture = () => {
      stopped = true;
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
        // A capture ended on purpose, before its first frame, has not failed.
        if (!receivedFrame && this.#clients.size && !stopped) {
          state.acceleratedFailed = true;
          state.lastError = `accelerated stream failed${code === null ? "" : ` (exit ${code})`}: ${stderr.trim()}`;
        }
        resolvePromise();
      });
    });
  }
}

/**
 * The iOS Simulator fast transport (IOSF-001..006): one idb_companion owned by this Canvas,
 * one H.264 video stream on it that every tab shares through the scrcpy /ws/video messages,
 * and the companion's HID stream for input. States: idle → starting → streaming ↔
 * restarting → stopped. Events: state(name), session({width, height}), config(Buffer),
 * packet({keyFrame, pts, data}), orientation(name), error(Error).
 *
 * Every stream ends with an explicit Stop (the client sends it); a stream that ended without
 * one may leave its encoder running in the companion, so the companion is restarted then.
 * There is never a second stream: a late joiner waits for the periodic key frame, and a key
 * frame that does not come is forced with Stop and Start on the same stream (DEC-004).
 */
class IosFastSession extends EventEmitter {
  #udid;
  #xcrun;
  #binary;
  #video;
  #companion = null;
  #stream = null;
  #state = "idle";
  #stopped = false;
  #stopPromise = null;
  #loop = null;
  #everStreamed = false;
  #first = null;
  #wake = null;
  #t0 = performance.now();
  #config = null;
  #size = null;
  #points = null;
  #orientation = "PORTRAIT";
  #booted = true;
  #watchTimer = null;
  #orientationTimer = null;
  #keyFrameWantedAt = null;
  #keyFrameTimer = null;
  #forcedKeyFrameAt = -Infinity;
  #streamRestarting = false;
  #stats = {
    packets: 0, bytes: 0, keyFrames: 0, restarts: 0, companionStarts: 0, streamsOpened: 0,
    streamRestarts: 0, forcedKeyFrames: 0, hidEvents: 0,
  };

  constructor({ udid, xcrunPath, binary, video }) {
    super();
    this.#udid = udid;
    this.#xcrun = xcrunPath;
    this.#binary = binary;
    this.#video = video;
  }

  get state() {
    return this.#state;
  }

  get everStreamed() {
    return this.#everStreamed;
  }

  /** Input can go out: the stream runs, the Simulator is booted and the companion answers. */
  get connected() {
    const client = this.#companion?.client;
    return this.#state === "streaming" && this.#booted && Boolean(client) && !client.closed;
  }

  /** HID events are a few bytes written straight to the companion; nothing waits in Canvas memory. */
  get controlBacklog() {
    return 0;
  }

  /** The screen in logical points, portrait. */
  get points() {
    return this.#points;
  }

  get orientation() {
    return this.#orientation;
  }

  get stats() {
    return { ...this.#stats, state: this.#state, points: this.#points, companionPid: this.#companion?.pid ?? null };
  }

  /** Resolves with the first frame; rejects when the first attempt fails. Later restarts only emit. */
  start() {
    if (this.#stopped) return Promise.reject(new Error("idb session is stopped"));
    if (this.#loop) return Promise.reject(new Error("idb session already started"));
    this.#setState("starting");
    const first = new Promise((resolvePromise, reject) => {
      this.#first = { resolve: resolvePromise, reject };
    });
    this.#loop = this.#run();
    this.#watchSimulator();
    this.#watchOrientation();
    return first;
  }

  /** One HID event on the companion's HID stream, in call order. */
  hid(event) {
    try {
      const client = this.#companion?.client;
      if (!client || client.closed) throw new Error("the idb companion is not running");
      this.#stats.hidEvents += 1;
      return client.hid().send(event);
    } catch (error) {
      return Promise.reject(error);
    }
  }

  /**
   * A tab needs a key frame. The companion sends one every few seconds; only one that does
   * not arrive within IDB_KEY_FRAME_WAIT_MS is forced, by Stop and Start on the same stream.
   */
  requestKeyFrame() {
    if (!this.connected) return;
    this.#keyFrameWantedAt ??= performance.now();
    if (!this.#keyFrameTimer) this.#armKeyFrameTimer(IDB_KEY_FRAME_WAIT_MS);
  }

  /** Stop for good: Stop on the stream, then the companion and its process group. Idempotent. */
  stop() {
    if (this.#stopPromise) return this.#stopPromise;
    this.#stopped = true;
    clearTimeout(this.#watchTimer);
    clearTimeout(this.#orientationTimer);
    clearTimeout(this.#keyFrameTimer);
    this.#wake?.();
    this.#setState("stopped");
    this.#settleFirst(new Error("the idb session stopped"));
    this.#stopPromise = (async () => {
      await this.#stream?.stop().catch(() => {});
      await this.#stopCompanion();
    })();
    return this.#stopPromise;
  }

  #setState(state) {
    if (this.#state === state) return;
    this.#state = state;
    this.emit("state", state);
  }

  #settleFirst(error) {
    const first = this.#first;
    if (!first) return;
    this.#first = null;
    if (error) first.reject(error);
    else first.resolve(true);
  }

  #sleep(milliseconds) {
    return new Promise((resolvePromise) => {
      const done = () => {
        clearTimeout(timer);
        if (this.#wake === done) this.#wake = null;
        resolvePromise();
      };
      const timer = setTimeout(done, milliseconds);
      this.#wake = done;
    });
  }

  async #run() {
    let backoff = IDB_RETRY_MIN_MS;
    while (!this.#stopped) {
      const result = await this.#streamOnce().catch((error) => ({ error, abnormal: true, ranMs: 0 }));
      if (this.#stopped) break;
      const error = result.error ?? new Error("the idb companion ended the video stream");
      this.#settleFirst(error);
      if (this.listenerCount("error")) this.emit("error", error);
      this.#setState("restarting");
      // No Stop reached the companion, so its encoder may still run: start a new one.
      if (result.abnormal) await this.#stopCompanion();
      if (result.ranMs >= IDB_STABLE_MS) backoff = IDB_RETRY_MIN_MS;
      await this.#sleep(backoff);
      backoff = Math.min(IDB_RETRY_MAX_MS, backoff * 2);
      if (!this.#stopped) this.#stats.restarts += 1;
    }
  }

  async #stopCompanion() {
    const companion = this.#companion;
    this.#companion = null;
    await companion?.stop().catch(() => {});
  }

  /** One stream on the current companion (started first when needed), until it ends. */
  async #streamOnce() {
    // A companion whose process is gone is replaced, even after a stream that ended with a
    // status: each process is one companion start.
    if (this.#companion && !this.#companion.running) await this.#stopCompanion();
    if (!this.#companion) {
      const companion = new IdbCompanion({ udid: this.#udid, binary: this.#binary });
      companion.on("exit", ({ code, signal, expected }) => {
        if (expected || !this.listenerCount("error")) return;
        const tail = companion.logTail().slice(-3).join(" | ");
        this.emit("error", new Error(`idb_companion exited (${signal ?? `code ${code}`})${tail ? `: ${tail}` : ""}`));
      });
      this.#companion = companion;
      this.#stats.companionStarts += 1;
    }
    const client = await this.#companion.start();
    if (this.#stopped) return { abnormal: false, ranMs: 0 };
    await this.#describe(client);
    await this.#readOrientation(client);
    if (this.#stopped) return { abnormal: false, ranMs: 0 };
    const stream = await client.openVideoStream(this.#video);
    this.#stream = stream;
    this.#stats.streamsOpened += 1;
    const openedAt = performance.now();
    let framed = false;
    let error = null;
    const timer = setTimeout(() => {
      if (framed) return;
      error = new Error(`the idb video stream sent no frame within ${IDB_FIRST_FRAME_MS / 1000} s`);
      stream.stop().catch(() => {});
    }, IDB_FIRST_FRAME_MS);
    timer.unref?.();
    try {
      for await (const frame of stream) {
        framed = true;
        this.#onFrame(frame);
      }
    } catch (thrown) {
      error ??= thrown;
    } finally {
      clearTimeout(timer);
      if (this.#stream === stream) this.#stream = null;
    }
    const closed = await stream.closed;
    return { abnormal: closed.abnormal, error: error ?? closed.error ?? null, ranMs: performance.now() - openedAt };
  }

  /** The screen size in points, for HID coordinates; the last known one is kept on failure. */
  async #describe(client) {
    try {
      const { screen } = await client.describe({ timeoutMs: 3000 });
      if (screen?.widthPoints && screen?.heightPoints) {
        this.#points = { width: screen.widthPoints, height: screen.heightPoints };
      } else if (screen?.width && screen?.height && screen?.density) {
        this.#points = { width: Math.round(screen.width / screen.density), height: Math.round(screen.height / screen.density) };
      }
    } catch (error) {
      if (this.listenerCount("error")) this.emit("error", new Error(`idb describe failed: ${error.message}`));
    }
  }

  async #readOrientation(client) {
    let name;
    try {
      name = await client.getOrientation({ timeoutMs: 2000 });
    } catch {
      return;
    }
    // Face up or down and unknown keep the orientation the screen is drawn in.
    if (!["PORTRAIT", "PORTRAIT_UPSIDE_DOWN", "LANDSCAPE_LEFT", "LANDSCAPE_RIGHT"].includes(name)) return;
    if (name === this.#orientation) return;
    this.#orientation = name;
    this.emit("orientation", name);
  }

  #onFrame(frame) {
    if (frame.config && (!this.#config || !frame.config.equals(this.#config))) {
      this.#config = Buffer.from(frame.config);
      if (frame.width && frame.height &&
        (frame.width !== this.#size?.width || frame.height !== this.#size?.height)) {
        this.#size = { width: frame.width, height: frame.height };
        // Without a describe answer, an iPhone's picture is three pixels per point.
        this.#points ??= { width: Math.round(frame.width / 3), height: Math.round(frame.height / 3) };
        this.emit("session", { ...this.#size });
      }
      this.emit("config", this.#config);
    }
    // A stream frozen while the Simulator is down is not streaming yet.
    if (this.#state !== "streaming" && this.#booted) {
      this.#everStreamed = true;
      this.#setState("streaming");
      this.#settleFirst(null);
    }
    // The CONFIG message carries SPS and PPS, which the page puts before each key frame.
    const data = frame.key && frame.config ? withoutParameterSets(frame.data) : frame.data;
    if (frame.key) {
      this.#stats.keyFrames += 1;
      this.#keyFrameWantedAt = null;
      clearTimeout(this.#keyFrameTimer);
      this.#keyFrameTimer = null;
    }
    this.#stats.packets += 1;
    this.#stats.bytes += frame.data.length;
    const pts = Math.max(0, Math.round((frame.receivedAt - this.#t0) * 1000));
    this.emit("packet", { keyFrame: frame.key, pts, data });
  }

  #armKeyFrameTimer(milliseconds) {
    this.#keyFrameTimer = setTimeout(() => {
      this.#keyFrameTimer = null;
      if (this.#keyFrameWantedAt === null || this.#stopped) return;
      const wait = this.#forcedKeyFrameAt + IDB_FORCED_KEY_FRAME_MS - performance.now();
      if (wait > 0) {
        this.#armKeyFrameTimer(wait);
        return;
      }
      this.#forcedKeyFrameAt = performance.now();
      this.#stats.forcedKeyFrames += 1;
      this.#restartStream("a key frame was asked for");
    }, milliseconds);
    this.#keyFrameTimer.unref?.();
  }

  /**
   * Stop, then Start on the same stream: SPS, PPS and an IDR follow within about 0.15 s.
   * A Stop the companion did not confirm ends the stream as abnormal instead, so the run
   * loop replaces the companion before the next stream opens: never two encoders.
   */
  #restartStream(reason) {
    const stream = this.#stream;
    if (!stream || stream.done || this.#streamRestarting) return;
    this.#streamRestarting = true;
    this.#stats.streamRestarts += 1;
    stream.restart().then(() => {
      this.#stats.streamsOpened += 1;
    }, (error) => {
      // An abnormal end is reported by the run loop, with this same error.
      if (stream.abnormal || !this.listenerCount("error")) return;
      this.emit("error", new Error(`idb stream restart (${reason}) failed: ${error.message}`));
    }).finally(() => {
      this.#streamRestarting = false;
    });
  }

  /** A Simulator that shuts down freezes the stream; once it is Booted again the stream restarts. */
  #watchSimulator() {
    const tick = async () => {
      this.#watchTimer = null;
      if (this.#stopped) return;
      const state = await simulatorState(this.#xcrun, this.#udid).catch(() => null);
      if (this.#stopped) return;
      try {
        if (state !== null) {
          const booted = state === "Booted";
          if (!booted && this.#booted) {
            this.#booted = false;
            if (this.#state === "streaming") this.#setState("restarting");
          } else if (booted && !this.#booted) {
            this.#booted = true;
            this.#restartStream("the Simulator booted again");
          }
        }
      } catch (error) {
        // A listener that throws must not end the watch, nor the Canvas.
        if (this.listenerCount("error")) this.emit("error", error);
      }
      this.#watchTimer = setTimeout(tick, IDB_SIMULATOR_WATCH_MS);
      this.#watchTimer.unref?.();
    };
    this.#watchTimer = setTimeout(tick, IDB_SIMULATOR_WATCH_MS);
    this.#watchTimer.unref?.();
  }

  /** The picture stays portrait when the Simulator rotates; the page turns it (IOSF-006). */
  #watchOrientation() {
    const tick = async () => {
      this.#orientationTimer = null;
      if (this.#stopped) return;
      const client = this.#companion?.client;
      try {
        if (client && !client.closed && this.#state === "streaming" && this.#booted) await this.#readOrientation(client);
      } catch (error) {
        if (this.listenerCount("error")) this.emit("error", error);
      }
      if (this.#stopped) return;
      this.#orientationTimer = setTimeout(tick, IDB_ORIENTATION_MS);
      this.#orientationTimer.unref?.();
    };
    this.#orientationTimer = setTimeout(tick, IDB_ORIENTATION_MS);
    this.#orientationTimer.unref?.();
  }
}

/** The state `simctl` reports for one Simulator ("Booted", "Shutdown", ...), "Unknown" when it is gone. */
async function simulatorState(xcrunPath, udid) {
  const { stdout } = await execFileAsync(xcrunPath, ["simctl", "list", "devices", "--json"],
    { timeout: 5000, encoding: "utf8" });
  const devices = Object.values(JSON.parse(stdout).devices ?? {}).flat();
  return devices.find((device) => device.udid === udid)?.state ?? "Unknown";
}

const ANNEX_B_START = Buffer.from([0, 0, 0, 1]);

/**
 * A key frame access unit without its SPS and PPS NAL units, which go to pages in the
 * CONFIG message; any other NAL unit before the first slice is kept, the slices untouched.
 */
function withoutParameterSets(data) {
  const units = [];
  for (let i = 0; i + 3 < data.length;) {
    if (data[i] !== 0 || data[i + 1] !== 0 || data[i + 2] !== 1) {
      i += 1;
      continue;
    }
    const type = data[i + 3] & 0x1f;
    units.push({ codeAt: i > 0 && data[i - 1] === 0 ? i - 1 : i, nalAt: i + 3, type });
    if (type >= 1 && type <= 5) break;
    i += 3;
  }
  const slice = units.at(-1);
  if (!slice || slice.type < 1 || slice.type > 5 || !units.some(({ type }) => type === 7 || type === 8)) return data;
  const parts = [];
  for (let index = 0; index + 1 < units.length; index += 1) {
    const unit = units[index];
    if (unit.type === 7 || unit.type === 8) continue;
    parts.push(ANNEX_B_START, data.subarray(unit.nalAt, units[index + 1].codeAt));
  }
  parts.push(data.subarray(slice.codeAt));
  return Buffer.concat(parts);
}

// One private folder per Canvas for iOS screenshots, removed when the process exits, so a
// capture still running at shutdown leaves nothing behind.
let screenshotFolder = null;
let screenshotCount = 0;

function screenshotPath() {
  if (!screenshotFolder) {
    screenshotFolder = mkdtempSync(join(tmpdir(), "autonom-canvas-"));
    process.once("exit", () => rmSync(screenshotFolder, { recursive: true, force: true }));
  }
  screenshotCount += 1;
  return join(screenshotFolder, `frame-${screenshotCount}.png`);
}

async function capturePng(context) {
  if (context.options.platform === "ios") {
    // A file, never "-": since Xcode 27 `simctl io screenshot -` writes a file named "-"
    // in the working directory instead of PNG on stdout.
    const frame = screenshotPath();
    try {
      await execFileAsync(context.adbPath, [
        "simctl", "io", context.serial, "screenshot", "--type=png", frame,
      ], { timeout: 8000, maxBuffer: 1024 * 1024 });
      return await readFile(frame);
    } finally {
      await rm(frame, { force: true });
    }
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
  const fields = JSON.stringify({
    t: "state",
    owner: context.state.controlOwner,
    paused: context.state.inputPaused,
    transport: chooseTransport(context),
    width: context.video.size?.width ?? null,
    height: context.video.size?.height ?? null,
    clients: { video: context.videoClients.size, control: context.controlClients.size },
    session: context.session?.state ?? "idle",
    // iOS: the Simulator orientation, and how far the page turns the portrait picture.
    ...(isIosCanvas(context)
      ? {
        orientation: context.session?.orientation ?? null, rotation: iosRotation(context),
        // Whether paste and clipboard-get reach the Simulator clipboard (simctl on this Mac).
        clipboard: iosClipboardRefusal(context) === null,
      }
      // Android: the stream size of the transport in use (scrcpy's, 0 is native, with the
      // encoder behind it; screenrecord's width; null on screencap).
      : {
        max_size: streamSizeInUse(context)?.maxSize ?? null,
        encoder: streamSizeInUse(context)?.encoder ?? null,
      }),
    // Only Android has this transport, so state messages show the display preset.
    ...stateDisplay(context),
  });
  // `presets` stays the last field, so the text is what serializing it in place gives.
  return `${fields.slice(0, -1)},"presets":${DISPLAY_PRESETS_JSON}}`;
}

/**
 * The preset and density a state message shows, from a reading begun at most
 * DISPLAY_READ_MAX_AGE_MS before (DISPLAY-001). The reading is renewed while clients are
 * connected (watchDisplay); while it is older even so (the first client just connected,
 * or adb is slow) both are left out, pages keep what they show, and the next reading
 * then goes to every client.
 */
function stateDisplay(context) {
  if (performance.now() - context.displayWmAt > DISPLAY_READ_MAX_AGE_MS) {
    context.displayStaleSent = true;
    return {};
  }
  return { preset: context.displayPreset, density: context.displayWm?.density ?? null };
}

function broadcastState(context) {
  const message = stateMessage(context);
  for (const client of context.videoClients) sendState(client, message);
  for (const client of context.controlClients) sendState(client, message);
}

/**
 * A client behind by STATE_BACKLOG_BYTES skips state messages, so a flood of handoff
 * changes leaves at most one bound of them per client. It skips them only while its
 * socket owes a "drain" event (past the socket's high-water mark, which may be above
 * this bound), so that event always comes once the backlog has gone.
 */
function sendState(client, message) {
  if (client.ws.bufferedAmount > STATE_BACKLOG_BYTES && client.ws.writableNeedDrain) {
    client.stateStale = true;
    return;
  }
  client.stateStale = false;
  client.ws.send(message);
}

/**
 * A client that skipped a state gets the current one when its socket drains, and ahead
 * of the next video packet or control reply it is sent anyway, whatever its backlog: a
 * client on a slow link whose backlog never empties gets no "drain". That is at most one
 * state for each message it gets, and those have their own bounds.
 */
function catchUpState(context, client) {
  if (!client.stateStale) return;
  client.stateStale = false;
  client.ws.send(stateMessage(context));
}

/** Start the device server (or companion) for the first WebSocket client; at most one per Canvas. */
function ensureSession(context) {
  clearTimeout(context.idleTimer);
  context.idleTimer = null;
  const transport = chooseTransport(context);
  if (context.shuttingDown || context.session || !FAST_TRANSPORTS.has(transport)) return;
  const { options, adbPath, serial, scrcpy } = context;
  const maxFps = options.fpsExplicit ? options.fps : DEFAULT_SCRCPY_MAX_FPS;
  const session = transport === "idb"
    ? new IosFastSession({
      udid: serial,
      xcrunPath: adbPath,
      binary: context.idb.path,
      video: { ...DEFAULT_VIDEO_OPTIONS, fps: maxFps, avgBitrate: options.bitRate },
    })
    : new ScrcpySession({
      adbPath,
      serial,
      serverPath: scrcpy.serverPath,
      version: scrcpy.version,
      maxSize: options.maxSize,
      // Without --max-size the size follows the device's encoder (DEC-005).
      chooseMaxSize: options.maxSizeExplicit ? null : (probe) => {
        context.state.streamSize = streamMaxSize(options, probe);
        logStreamSize(context);
        return context.state.streamSize.maxSize;
      },
      bitRate: options.bitRate,
      maxFps,
    });
  if (transport === "scrcpy") logStreamSize(context);
  context.session = session;
  session.on("orientation", () => {
    // A rotation ends a finger or wheel drag: its points belong to the old orientation.
    for (const client of context.controlClients) iosReleaseInput(context, client, "the Simulator rotated");
    const streamed = iosStreamDisplay(context);
    if (streamed) context.display = streamed.display;
    broadcastState(context);
  });
  session.on("state", (name) => {
    if (name === "restarting") {
      // What the lost companion's HID stream held down is lifted through the next one,
      // even an UP still waiting in the queue, which the new epoch drops.
      iosOrphanHeld(context);
      context.iosEpoch += 1;
      // The server is gone, but what it injected is still down on Android: lifted through
      // the next server. The old server will not answer old requests either.
      for (const client of context.controlClients) orphanInput(context, client);
      orphanRecent(context);
      cancelClipboardRequest(context, "The scrcpy session restarted before the device answered");
      // No ack comes for a paste the old server took along: held input is refused below.
      dropClipboardHold(context);
    }
    if (name === "streaming") {
      refreshDisplay(context);
      liftIosOrphan(context);
    }
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
      if (transport === "idb") context.state.idbFailed = error.message;
      else context.state.scrcpyFailed = error.message;
      stopSession(context);
      broadcastState(context);
      for (const client of [...context.videoClients, ...context.controlClients]) {
        client.ws.close(CLOSE_CODE.NORMAL, `${transport} is unavailable; the Canvas falls back`);
      }
    });
  });
}

/** Print the scrcpy stream size once it is known, and again only when it changes. */
function logStreamSize(context) {
  const chosen = context.state.streamSize;
  if (!chosen) return;
  const size = chosen.maxSize === 0 ? "native" : `${chosen.maxSize} on the long side`;
  const why = chosen.source === "explicit" ? "--max-size"
    : chosen.source === "encoder" ? `${chosen.encoder} H.264 encoder ${chosen.encoderName}`
      : "the device's encoders could not be read";
  const line = `Stream size: ${size} (${why})`;
  if (context.state.streamSizeLogged === line) return;
  context.state.streamSizeLogged = line;
  console.log(line);
}

/** The display size puts journaled gestures in device pixels, like HTTP input. */
/**
 * On the idb transport the companion already reported the screen in points, as the bridge
 * would read it from the accessibility tree; asking the bridge on every /status would run an
 * accessibility dump per page poll and hold journal records behind it. Null otherwise.
 */
function iosStreamDisplay(context) {
  if (!isIosCanvas(context) || !(context.session instanceof IosFastSession) || !context.session.points) return null;
  return { display: iosLogicalSize(context) };
}

function refreshDisplay(context) {
  const streamed = iosStreamDisplay(context);
  if (streamed) {
    context.display = streamed.display;
    return Promise.resolve(context.display);
  }
  return context.actionBridge.call("screen-size", {}, "system").then((measured) => {
    if (measured?.display) context.display = measured.display;
    return context.display;
  }, () => context.display);
}

function stopSession(context) {
  const session = context.session;
  if (!session) return context.sessionStopping ?? Promise.resolve();
  context.session = null;
  if (isIosCanvas(context)) iosOrphanHeld(context);
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
    watchDisplay(context);
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
  watchDisplay(context);
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
    // iOS: pointers refused while another finger was down (their moves and ups are ignored),
    // and this connection's input waiting in the iOS input queue.
    refused: new Set(), iosWaiting: 0,
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
    watchDisplay(context);
  });
  ensureSession(context);
  broadcastState(context);
  watchDisplay(context);
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
    (client.iosWaiting ?? 0) >= JOURNAL_CLIENT_PAUSE_RECORDS * share ||
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
 * client is closed and its pointers lifted, instead of growing Canvas memory. A client
 * that skipped a state gets the current one first (catchUpState).
 */
function sendControlText(context, client, text) {
  if (client.ws.readyState !== READY_STATE.OPEN) return;
  if (client.ws.bufferedAmount > CLIENT_BACKLOG_BYTES) {
    client.ws.close(CLOSE_CODE.POLICY_VIOLATION, "Control replies are not being read");
    // Its frames are ignored from now on, so nothing would lift them before the socket goes.
    releaseInput(context, client);
    return;
  }
  catchUpState(context, client);
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

/**
 * The shared parser knows device input only; a display change is this server's own
 * message, and its preset is checked once its sender may change the display.
 */
function parseCanvasMessage(raw) {
  try {
    return parseControlMessage(raw);
  } catch (error) {
    if (error.for !== "display") throw error;
    return { t: "display", preset: JSON.parse(raw).preset };
  }
}

function handleControlMessage(context, client, raw) {
  let message;
  try {
    message = parseCanvasMessage(raw);
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
      if (isIosCanvas(context)) {
        iosClipboardGet(context, client);
        return;
      }
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
  if (message.t === "display") {
    // `wm` goes through adb, not the device server, so no session is needed.
    finishScroll(context, client);
    assertDisplayPlatform(context);
    requestDisplay(context, displayPreset(message.preset), client.origin, client).then(
      (display) => reply(context, client, { t: "display", ok: true, display }),
      (error) => replyDisplayError(context, client, error));
    return;
  }
  requireSession(context);
  // Any other input ends a wheel burst, so the journal keeps the order of actions.
  if (message.t !== "scroll") finishScroll(context, client);
  if (isIosCanvas(context)) {
    dispatchIosInput(context, client, message);
    return;
  }
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
    const name = isIosCanvas(context) ? "idb" : "scrcpy";
    throw new InputError(`The ${name} session is not ready (${context.session?.state ?? "idle"})`);
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
  if (!context.heldWrites.length) wakeHeldWritesWaiters(context);
  resumeAllReading(context);
}

/** The device server is gone or going: no ack will come, and held writes have no device. */
function dropClipboardHold(context) {
  clearTimeout(context.clipboardHold?.timer);
  context.clipboardHold = null;
  context.heldWrites = [];
  context.heldWriteBytes = 0;
  wakeHeldWritesWaiters(context);
}

/**
 * Resolves once no device write waits behind a paste: every write made so far is on the
 * device socket, or went with a device server that is gone.
 */
function heldWritesSent(context) {
  if (!context.heldWrites.length) return Promise.resolve();
  return new Promise((resolvePromise) => context.heldWritesWaiters.push(resolvePromise));
}

function wakeHeldWritesWaiters(context) {
  const waiters = context.heldWritesWaiters;
  context.heldWritesWaiters = [];
  for (const wake of waiters) wake();
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
  }, null, gesture.action ?? null);
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
  if (burst.wheel) iosWheelUp(context, client, burst);
  journal(context, client, { kind: "scroll", events: burst.events, dx: burst.dx, dy: burst.dy }, null,
    burst.action ?? null);
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
  if (isIosCanvas(context)) {
    iosReleaseInput(context, client);
    return;
  }
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
  if (isIosCanvas(context)) {
    iosForgetInput(context, client);
    return;
  }
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
  if (isIosCanvas(context)) {
    iosOrphanInput(context, client);
    return;
  }
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
  // The Simulator picture keeps its size when it rotates; HID points do not depend on it.
  if (isIosCanvas(context)) return;
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

// ---------------------------------------------------------------------------
// iOS input on the idb transport (IOSF-004): one finger over the companion's HID stream.
// ---------------------------------------------------------------------------

/** Clockwise degrees the page turns the portrait picture to show it upright. */
function iosRotation(context) {
  if (!isIosCanvas(context)) return 0;
  return IOS_ROTATION[context.session?.orientation] ?? 0;
}

/** The Simulator screen in logical points as the user sees it now: landscape is turned. */
function iosLogicalSize(context) {
  const points = context.session?.points;
  if (!points) throw new InputError("The Simulator screen size is not known yet");
  return iosRotation(context) % 180 ? { width: points.height, height: points.width } : points;
}

/** 0..1 coordinates of the upright picture → HID logical points in the current orientation. */
function iosPoint(context, x, y) {
  const size = iosLogicalSize(context);
  const round = (value) => Math.round(value * 100) / 100;
  return {
    x: round(Math.min(size.width, Math.max(0, x * size.width))),
    y: round(Math.min(size.height, Math.max(0, y * size.height))),
  };
}

/** A gesture point for the journal, with the upright picture size it was taken against. */
function iosGesturePoint(context, x, y) {
  const size = context.video.size;
  const upright = size && iosRotation(context) % 180 ? { width: size.height, height: size.width } : size;
  return { x, y, size: upright };
}

/**
 * iOS input runs in one queue, in message order across connections, as one device socket
 * keeps it on Android: HID events (each written before the next item runs), text (typed
 * by the bridge before the next item runs) and the records of the actions before them.
 * Pause and takeover are checked again when each item's turn comes: the input of a
 * connection refused by then is dropped, except an UP that lifts what the HID stream holds
 * down. Until it has run an item counts against its connection's bound, so a flood behind
 * slow text pauses reading instead of queueing without end.
 */
function iosEnqueue(context, item) {
  item.session = context.session;
  item.epoch = context.iosEpoch;
  if (item.client) item.client.iosWaiting = (item.client.iosWaiting ?? 0) + 1;
  if (item.first) context.iosInput.unshift(item);
  else context.iosInput.push(item);
  if (!context.iosInputRunning) drainIosInput(context);
}

async function drainIosInput(context) {
  context.iosInputRunning = true;
  try {
    while (context.iosInput.length) {
      const item = context.iosInput.shift();
      try {
        await runIosItem(context, item);
      } catch (error) {
        if (!(error instanceof InputError)) context.state.lastError = error.message;
      } finally {
        if (item.client) {
          item.client.iosWaiting -= 1;
          iosResumeReading(context);
        }
      }
    }
  } finally {
    context.iosInputRunning = false;
  }
}

/**
 * Paused connections may read again once their input ran. An item can finish while a
 * message is still being handled (a record, a dropped event), so this waits until that
 * handling is done instead of handling held messages in the middle of it.
 */
function iosResumeReading(context) {
  if (context.iosResumeQueued) return;
  context.iosResumeQueued = true;
  queueMicrotask(() => {
    context.iosResumeQueued = false;
    resumeAllReading(context);
  });
}

function runIosItem(context, item) {
  if (item.kind === "hid") return runIosHid(context, item);
  if (item.kind === "text") return runIosText(context, item);
  if (item.kind === "pbpaste") return runIosClipboardGet(context, item);
  if (item.kind === "paste") return runIosPaste(context, item);
  runIosRecord(context, item);
  return undefined;
}

/** True while iOS input waits or runs, so what comes next must wait behind it. */
function iosBusy(context) {
  return context.iosInputRunning || context.iosInput.length > 0;
}

/**
 * One HID event for the queue. `action` ({sent, written, broken}) counts the events of one
 * action and those a live companion took: an action with events is journaled only when one
 * of them was taken. `start` marks the DOWN that puts a finger on the screen: once it is
 * refused or dropped, the moves and the UP of that contact are dropped too (`broken`), until
 * the next start of the action, instead of pressing where the user never pressed.
 */
function hidSend(context, event, client = null, action = null, start = false) {
  if (action) action.sent += 1;
  if (!(context.session instanceof IosFastSession)) return;
  iosEnqueue(context, { kind: "hid", event, client, action, start });
}

/** What the HID stream of the current session and epoch holds down. */
function hidHeld(context) {
  const held = context.iosHeld;
  if (held && held.session === context.session && held.epoch === context.iosEpoch) return held;
  context.iosHeld = { session: context.session, epoch: context.iosEpoch, touch: null, buttons: new Set() };
  return context.iosHeld;
}

async function runIosHid(context, item) {
  const { session, event, client, action } = item;
  if (event.pinch) {
    // A pinch holds nothing down: it is sent, by its turn, only for a live companion and a
    // connection still allowed to send input.
    const current = context.session === session && context.iosEpoch === item.epoch;
    if (!current || !session.connected || (client && refusal(context, client.origin))) return;
    await iosHidWrite(context, session, event, action);
    return;
  }
  const contact = Boolean(action && !event.button);
  // A new contact of the action starts clean; the rest of a contact whose DOWN was refused
  // or dropped is dropped with it.
  if (contact && item.start) action.broken = false;
  else if (contact && action.broken) return;
  const drop = () => {
    if (contact && item.start) action.broken = true;
  };
  const current = context.session === session && context.iosEpoch === item.epoch;
  if (!current || !session.connected) {
    // An orphan UP that found no companion streaming waits for the next one.
    if (item.orphan) iosAddOrphan(context, event);
    drop();
    return;
  }
  const held = hidHeld(context);
  const down = event.direction === "down";
  const isHeld = event.button ? held.buttons.has(event.button) : held.touch !== null;
  // An UP for nothing held down is not sent (its DOWN was dropped), unless it is an orphan
  // UP for what a lost companion held.
  if (!down && !isHeld && !item.orphan) return;
  let sent = event;
  if (client && refusal(context, client.origin)) {
    if (down) {
      drop();
      return;
    }
    // A refused connection's UP only lets go of what is held, where it is held: its moves
    // since were dropped.
    if (!event.button) sent = { ...event, touch: held.touch };
  }
  if (event.button) {
    if (down) held.buttons.add(event.button);
    else held.buttons.delete(event.button);
  } else {
    held.touch = down ? event.touch : null;
  }
  await iosHidWrite(context, session, sent, action);
}

/** Write one event to the session's HID stream; a write not taken in time stops holding the queue. */
async function iosHidWrite(context, session, sent, action) {
  let timer = null;
  const timeout = new Promise((resolvePromise) => {
    timer = setTimeout(() => resolvePromise("timeout"), IOS_HID_WRITE_MS);
    timer.unref?.();
  });
  // Only an event the companion took counts for its action's record.
  const written = session.hid(sent).then(() => {
    if (action) action.written += 1;
  });
  try {
    const result = await Promise.race([written, timeout]);
    if (result === "timeout") context.state.lastError = `idb hid: a write took more than ${IOS_HID_WRITE_MS} ms`;
  } catch (error) {
    context.state.lastError = `idb hid: ${error.message}`;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The companion or stream was lost, or the session stops: what its HID stream held down
 * becomes the orphan, lifted through the next companion that streams (liftIosOrphan).
 */
function iosOrphanHeld(context) {
  const held = context.iosHeld;
  context.iosHeld = null;
  if (!held) return;
  if (held.touch) iosAddOrphan(context, { touch: held.touch, direction: "up" });
  for (const button of held.buttons) iosAddOrphan(context, { button, direction: "up" });
}

function iosAddOrphan(context, event) {
  const orphan = context.iosOrphan ?? (context.iosOrphan = { touch: null, buttons: new Set() });
  if (event.button) orphan.buttons.add(event.button);
  else orphan.touch = event.touch;
}

/** A companion streams: an UP for each orphan goes first, before any input queued after. */
function liftIosOrphan(context) {
  const orphan = context.iosOrphan;
  if (!orphan || !isIosCanvas(context) || !(context.session instanceof IosFastSession)) return;
  context.iosOrphan = null;
  const events = [
    ...(orphan.touch ? [{ touch: orphan.touch, direction: "up" }] : []),
    ...[...orphan.buttons].map((button) => ({ button, direction: "up" })),
  ];
  for (const event of events.reverse()) iosEnqueue(context, { kind: "hid", event, client: null, orphan: true, first: true });
}

/** Text is typed by the bridge, behind HTTP input too; refused by its turn, it is not typed. */
async function runIosText(context, item) {
  const { client, payload } = item;
  try {
    const refused = refusal(context, client.origin);
    if (refused) throw new InputError(refused);
    await context.enqueueInput(() => {
      const late = refusal(context, client.origin);
      if (late) throw new InputError(late);
      return context.actionBridge.call("text", payload, client.origin);
    });
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: "text" });
  }
}

/**
 * A record waits behind the input before it. An action none of whose events a live
 * companion took (refused by their turn, or dropped with a lost companion or a stopped
 * session) did not happen: its record is dropped. An action without events (a wheel burst
 * that did not move) is journaled unless refused by its turn.
 */
function runIosRecord(context, item) {
  const { client, record, onAnswered, action } = item;
  if (action && !action.written && (action.sent || refusal(context, client.origin))) {
    onAnswered?.();
    return;
  }
  journalNow(context, client, record, onAnswered);
}

function dispatchIosInput(context, client, message) {
  switch (message.t) {
    case "touch":
      iosTouch(context, client, message);
      break;
    case "scroll":
      iosScroll(context, client, message);
      break;
    case "text":
      iosText(context, client, message);
      break;
    case "system":
      iosSystem(context, client, message.op);
      break;
    case "key":
      throw new InputError("Android keycodes have no iOS Simulator equivalent; send text instead");
    case "paste":
      iosPaste(context, client, message);
      break;
    default:
      throw new InputError("Unsupported control message.");
  }
}

/**
 * Touch down/move/up live: a held finger moves by repeating DOWN at its new point, then UP.
 * The HID touch has no finger id, so a pointer from another connection, or a third one, is
 * refused; a second pointer of the same connection makes the pair a pinch (iosStartPinch).
 */
function iosTouch(context, client, message) {
  const { id } = message;
  if (message.a === "down") {
    client.lifted.delete(id);
    client.refused.delete(id);
    const finger = context.iosFinger;
    if (finger) {
      if (finger.client === client && iosFingerIds(finger).includes(id)) throw new InputError(`Pointer ${id} is already down`);
      if (finger.client === client && !finger.wheel && !finger.pinch) {
        iosStartPinch(context, client, finger, message);
        return;
      }
      iosRefusePointer(client, id);
      throw new InputError(IOS_ONE_FINGER);
    }
    const point = iosPoint(context, message.x, message.y);
    // A mirrored pinch finger waits for its partner; alone, it goes down at its first move.
    const deferred = id <= IOS_MIRROR_ID_MAX;
    context.iosFinger = { client, id, x: message.x, y: message.y, point, wheel: false, deferred, pinch: null };
    client.gesture = {
      startedAt: performance.now(), pointers: 1, moves: 0,
      start: iosGesturePoint(context, message.x, message.y), end: null, action: { sent: 0, written: 0, broken: false },
    };
    if (!deferred) hidSend(context, { touch: point, direction: "down" }, client, client.gesture.action, true);
    return;
  }
  const finger = context.iosFinger;
  if (!finger || finger.client !== client || finger.wheel || !iosFingerIds(finger).includes(id)) {
    // The refusal went with the down; its moves and up need no answer.
    if (client.refused.has(id)) {
      if (message.a !== "move") client.refused.delete(id);
      return;
    }
    const lifted = client.lifted.get(id);
    if (!lifted) throw new InputError(`Pointer ${id} is not down`);
    if (message.a === "move") {
      throw new InputError(`Pointer ${id} was lifted because ${lifted}; put it down again`);
    }
    client.lifted.delete(id);
    return;
  }
  if (finger.pinch) {
    iosPinchPointer(context, client, finger, message);
    return;
  }
  const action = client.gesture?.action ?? null;
  if (finger.deferred) {
    // A mirrored finger whose partner never came is one finger after all.
    finger.deferred = false;
    hidSend(context, { touch: finger.point, direction: "down" }, client, action, true);
  }
  finger.x = message.x;
  finger.y = message.y;
  finger.point = iosPoint(context, message.x, message.y);
  if (message.a === "move") {
    if (client.gesture) client.gesture.moves += 1;
    hidSend(context, { touch: finger.point, direction: "down" }, client, action);
    return;
  }
  // up, or cancel: the HID touch has no cancel, so both lift the finger where it is.
  hidSend(context, { touch: finger.point, direction: "up" }, client, action);
  context.iosFinger = null;
  if (client.gesture) client.gesture.end = iosGesturePoint(context, message.x, message.y);
  finishGesture(context, client);
}

/** The pointer ids of the finger state: one, or the two of a pinch. */
function iosFingerIds(finger) {
  return finger.pinch ? [...finger.pinch.pointers.keys()] : [finger.id];
}

/** A refused pointer: its moves and up are ignored without an answer. */
function iosRefusePointer(client, id) {
  client.refused.add(id);
  if (client.refused.size > LIFTED_POINTERS_MAX) client.refused.delete(client.refused.values().next().value);
}

/**
 * A second pointer of the finger's connection: the pair becomes a pending pinch. A live
 * finger lifts where it is first, so the HID stream holds nothing down; the pinch itself
 * is sent when either pointer lifts (iosPinchPointer), and nothing moves before that.
 */
function iosStartPinch(context, client, finger, message) {
  const point = iosPoint(context, message.x, message.y);
  if (!finger.deferred) {
    hidSend(context, { touch: finger.point, direction: "up" }, client, client.gesture?.action ?? null);
  }
  finger.deferred = false;
  finger.pinch = {
    startedAt: performance.now(),
    pointers: new Map([
      [finger.id, { start: finger.point, now: finger.point }],
      [message.id, { start: point, now: point }],
    ]),
  };
}

/** A move updates its pointer; the first up (or cancel) sends the pair as one HIDPinch. */
function iosPinchPointer(context, client, finger, message) {
  const pointer = finger.pinch.pointers.get(message.id);
  pointer.now = iosPoint(context, message.x, message.y);
  if (message.id === finger.id) {
    finger.x = message.x;
    finger.y = message.y;
    finger.point = pointer.now;
  }
  if (message.a === "move") {
    if (client.gesture) client.gesture.moves += 1;
    return;
  }
  context.iosFinger = null;
  // The other pointer is done with the pinch: its moves and up need no answer.
  for (const other of finger.pinch.pointers.keys()) if (other !== message.id) iosRefusePointer(client, other);
  let pinch;
  try {
    pinch = iosPinchEvent(context, finger.pinch);
  } catch (error) {
    iosLetGoUnsent(client, finger, null);
    finishGesture(context, client);
    throw error;
  }
  hidSend(context, { pinch }, client, client.gesture?.action ?? null);
  if (client.gesture) {
    client.gesture.pointers = 2;
    client.gesture.end = iosGesturePoint(context, finger.x, finger.y);
  }
  finishGesture(context, client);
}

/**
 * The HIDPinch for two pointers in points: center is the midpoint where they started, radius
 * half the start distance and scale end distance / start distance, with the duration they
 * took (at most IOS_PINCH_MAX_S). The companion moves both fingers on a horizontal line
 * through the center, so center and radii are kept where those fingers stay on the screen.
 */
function iosPinchEvent(context, pinch) {
  const size = iosLogicalSize(context);
  const [a, b] = [...pinch.pointers.values()];
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const half = (p, q) => Math.hypot(p.x - q.x, p.y - q.y) / 2;
  const minRadius = Math.min(IOS_PINCH_MIN_RADIUS, size.width / 4);
  const center = {
    x: roundPoints(clamp((a.start.x + b.start.x) / 2, minRadius + 1, size.width - minRadius - 1)),
    y: roundPoints(clamp((a.start.y + b.start.y) / 2, 0, size.height)),
  };
  const maxRadius = Math.max(minRadius, Math.min(center.x, size.width - center.x) - 1);
  const radius = roundPoints(clamp(half(a.start, b.start), minRadius, maxRadius));
  const end = clamp(half(a.now, b.now), minRadius, maxRadius);
  const seconds = clamp((performance.now() - pinch.startedAt) / 1000, 0, IOS_PINCH_MAX_S);
  return {
    center, radius,
    scale: Math.round((end / radius) * 10_000) / 10_000,
    duration: Math.round(seconds * 1000) / 1000,
  };
}

/**
 * A finger that never reached the HID stream (a mirrored finger without its partner, or a
 * pinch not sent yet) is let go without sending anything; a gesture that sent nothing at all
 * did not happen and is not journaled.
 */
function iosLetGoUnsent(client, finger, reason) {
  if (reason) for (const id of iosFingerIds(finger)) liftedByCanvas(client, id, reason);
  if (client.gesture && !client.gesture.action?.sent) client.gesture = null;
}

const roundPoints = (value) => Math.round(value * 100) / 100;

/** `point` moved inside the screen, at least `margin` points from each edge. */
function iosInside(size, point, margin) {
  return {
    x: roundPoints(Math.min(size.width - margin, Math.max(margin, point.x))),
    y: roundPoints(Math.min(size.height - margin, Math.max(margin, point.y))),
  };
}

/**
 * A wheel burst is one short synthetic drag that never taps: the finger goes down at an
 * anchor (where the wheel turns, kept IOS_WHEEL_MARGIN_POINTS inside every edge) with its
 * first move, which goes at least IOS_WHEEL_MIN_POINTS in the wheel's direction, past the
 * tap slop; later steps move it by the wheel's delta. At the screen edge it lifts there and
 * starts over from the anchor. It lifts where it is once the wheel is still (finishScroll)
 * and is journaled as one scroll record.
 */
function iosScroll(context, client, message) {
  const finger = context.iosFinger;
  if (finger && !(finger.wheel && finger.client === client)) {
    throw new InputError("A finger is down on the Simulator; the wheel scrolls once it is lifted");
  }
  const size = iosLogicalSize(context);
  let burst = client.scroll;
  if (!burst) {
    const margin = Math.min(IOS_WHEEL_MARGIN_POINTS, size.width / 4, size.height / 4);
    const anchor = iosInside(size, iosPoint(context, message.x, message.y), margin);
    burst = client.scroll = { events: 0, dx: 0, dy: 0, timer: null, wheel: { anchor }, action: { sent: 0, written: 0, broken: false } };
    context.iosFinger = { client, id: null, x: message.x, y: message.y, point: anchor, wheel: true, pressed: false };
  }
  burst.events += 1;
  burst.dx += message.dx;
  burst.dy += message.dy;
  // Wheel down (dy < 0) shows what is below: the finger moves up; dx > 0 moves it left.
  const move = { x: -message.dx * IOS_WHEEL_POINTS, y: message.dy * IOS_WHEEL_POINTS };
  if (move.x || move.y) iosWheelMove(context, client, burst, context.iosFinger, move, size);
  clearTimeout(burst.timer);
  burst.timer = setTimeout(() => finishScroll(context, client), IOS_WHEEL_IDLE_MS);
  burst.timer.unref?.();
}

function iosWheelMove(context, client, burst, wheel, move, size) {
  const send = (point, direction, start = false) => hidSend(context, { touch: point, direction }, client, burst.action, start);
  const same = (a, b) => a.x === b.x && a.y === b.y;
  if (wheel.pressed) {
    const wanted = { x: wheel.point.x + move.x, y: wheel.point.y + move.y };
    const next = iosInside(size, wanted, IOS_WHEEL_EDGE_POINTS);
    if (next.x === roundPoints(wanted.x) && next.y === roundPoints(wanted.y)) {
      if (!same(next, wheel.point)) send(next, "down");
      wheel.point = next;
      return;
    }
    // The edge: the drag, already past the slop, goes to the edge and lifts there; a new one
    // starts from the anchor below.
    if (!same(next, wheel.point)) send(next, "down");
    send(next, "up");
    wheel.point = next;
    wheel.pressed = false;
  }
  const { anchor } = burst.wheel;
  const scale = Math.max(1, IOS_WHEEL_MIN_POINTS / Math.hypot(move.x, move.y));
  const target = iosInside(size, { x: anchor.x + move.x * scale, y: anchor.y + move.y * scale }, IOS_WHEEL_EDGE_POINTS);
  // A screen too small to move past the slop from the anchor gets no drag at all.
  if (Math.hypot(target.x - anchor.x, target.y - anchor.y) < IOS_WHEEL_MIN_POINTS) {
    wheel.point = anchor;
    return;
  }
  send(anchor, "down", true);
  send(target, "down");
  wheel.point = target;
  wheel.pressed = true;
}

/** The wheel's finger lifts where it is (no fling: it has been still a moment). */
function iosWheelUp(context, client, burst) {
  const finger = context.iosFinger;
  if (!finger?.wheel || finger.client !== client) return;
  context.iosFinger = null;
  if (finger.pressed) hidSend(context, { touch: finger.point, direction: "up" }, client, burst.action);
}

/** Home and Power press the Simulator's HOME and LOCK buttons; nothing else has one. */
function iosSystem(context, client, op) {
  const button = IOS_BUTTONS[op];
  if (!button) throw new InputError(`${op} is not available on the iOS Simulator`);
  const action = { sent: 0, written: 0, broken: false };
  hidSend(context, { button, direction: "down" }, client, action);
  hidSend(context, { button, direction: "up" }, client, action);
  journal(context, client, { kind: "system", op }, null, action);
}

/**
 * Text goes through the bridge, like the HTTP text of every transport (the bridge journals
 * it), at its place in the iOS input queue: input sent after it waits until it is typed.
 * Pause and takeover are checked again when its turn comes: text refused by then is not
 * typed, and the connection is told why.
 */
function iosText(context, client, message) {
  const payload = { text: message.text, sensitive: message.sensitive, transport: "idb" };
  iosEnqueue(context, { kind: "text", client, payload });
}

/**
 * Why paste and clipboard-get cannot reach the Simulator clipboard, or null when they can.
 * `simctl pbcopy`/`pbpaste` act on the Simulators of the Mac they run on, so a Canvas that
 * does not run on macOS (its Simulator, behind a remote companion, is on another Mac) has
 * none and runs no simctl command for them.
 */
function iosClipboardRefusal(context) {
  if (platform !== "darwin") {
    return "The iOS Simulator clipboard needs xcrun simctl on the Mac that runs the Simulator; " +
      "this Canvas does not run on macOS";
  }
  return null;
}

/**
 * `xcrun simctl <command> <udid>` with `input` on stdin, no shell; resolves with stdout as
 * text. Neither stdin nor stdout is ever logged: they are clipboard text.
 */
function runSimctlClipboard(context, command, input = null) {
  return new Promise((resolvePromise, reject) => {
    let child;
    try {
      child = spawn(context.adbPath, ["simctl", command, context.serial], { stdio: ["pipe", "pipe", "pipe"] });
    } catch (error) {
      reject(error);
      return;
    }
    const out = [];
    let outBytes = 0;
    let errText = "";
    let failure = null;
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolvePromise(value);
    };
    const timer = setTimeout(() => {
      failure = new Error(`simctl ${command} took more than ${IOS_SIMCTL_MS} ms`);
      child.kill("SIGKILL");
    }, IOS_SIMCTL_MS);
    timer.unref?.();
    child.stdout.on("data", (chunk) => {
      outBytes += chunk.length;
      if (outBytes > IOS_CLIPBOARD_MAX_BYTES) {
        failure ??= new Error(`the Simulator clipboard holds more than ${IOS_CLIPBOARD_MAX_BYTES / 1024} KiB of text`);
        child.kill("SIGKILL");
        return;
      }
      out.push(chunk);
    });
    // simctl's own error message, never clipboard text: a short line for the reply.
    child.stderr.on("data", (chunk) => {
      if (errText.length < 400) errText += chunk.toString("utf8");
    });
    child.stdin.on("error", () => {});
    child.on("error", (error) => {
      failure ??= error;
      // A child that never started has no close to wait for.
      if (child.pid === undefined) finish(failure);
    });
    child.on("close", (code, signal) => {
      if (!failure && code !== 0) {
        const detail = errText.trim().split("\n")[0].slice(0, 200);
        failure = new Error(`simctl ${command} exited with ${code ?? signal}${detail ? `: ${detail}` : ""}`);
      }
      finish(failure, failure ? undefined : Buffer.concat(out).toString("utf8"));
    });
    child.stdin.end(input ?? undefined);
  });
}

/**
 * clipboard-get on iOS reads the Simulator clipboard with simctl pbpaste at its place in the
 * input queue, so it sees a paste sent before it. Like Android it acts for anyone; empty
 * reads as no text. The text is never logged or journaled.
 */
function iosClipboardGet(context, client) {
  const refused = iosClipboardRefusal(context);
  if (refused) throw new InputError(refused);
  iosEnqueue(context, { kind: "pbpaste", client });
}

async function runIosClipboardGet(context, item) {
  const { client } = item;
  if (!context.controlClients.has(client)) return;
  let text;
  try {
    text = await runSimctlClipboard(context, "pbpaste");
  } catch (error) {
    context.state.lastError = `clipboard: ${error.message}`;
    reply(context, client, { t: "error", message: `The Simulator clipboard could not be read: ${error.message}`, for: "clipboard-get" });
    return;
  }
  // A fresh Simulator says it has no items, with exit code 0.
  const empty = text === "" || text.replace(/\r?\n$/, "") === IOS_EMPTY_PASTEBOARD;
  if (context.controlClients.has(client)) reply(context, client, { t: "clipboard", text: empty ? null : text });
}

/**
 * paste on iOS sets the Simulator clipboard with simctl pbcopy at its place in the input
 * queue, behind HTTP input too, and types the text when it fits the HID text path; longer
 * text is only set, and the reply says how to insert it. Pause and takeover are checked when
 * its turn comes. One `paste` record with the text length only; the text is never logged.
 */
function iosPaste(context, client, message) {
  const refused = iosClipboardRefusal(context);
  if (refused) throw new InputError(refused);
  iosEnqueue(context, { kind: "paste", client, text: message.text, setOnly: iosPasteSetOnly(message.text) });
}

/** Why a paste is only set and not typed (null when it fits the HID text path). */
function iosPasteSetOnly(text) {
  if (Buffer.byteLength(text, "utf8") > IOS_TEXT_MAX_BYTES) return IOS_PASTE_TOO_LONG;
  if (!IOS_TYPEABLE_TEXT.test(text)) return IOS_PASTE_NOT_TYPEABLE;
  return null;
}

async function runIosPaste(context, item) {
  const { client, text, setOnly } = item;
  const typed = !setOnly;
  try {
    const refused = refusal(context, client.origin);
    if (refused) throw new InputError(refused);
    await context.enqueueInput(async () => {
      const late = refusal(context, client.origin);
      if (late) throw new InputError(late);
      await runSimctlClipboard(context, "pbcopy", text).catch((error) => {
        context.state.lastError = `clipboard: ${error.message}`;
        throw new InputError(`The Simulator clipboard could not be set: ${error.message}`);
      });
      if (!typed) return;
      const lateType = refusal(context, client.origin);
      if (lateType) throw new InputError(`The Simulator clipboard was set, but the text was not typed: ${lateType}`);
      // The bridge types it and journals it as this paste, with its length only.
      await context.actionBridge.call("text", { text, sensitive: true, transport: "idb", paste: true }, client.origin);
    });
    if (!typed) journalNow(context, client, { kind: "paste", text_len: codePoints(text) });
    reply(context, client, typed ? { t: "paste", typed: true } : { t: "paste", typed: false, message: setOnly });
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: "paste" });
  }
}

/**
 * Lift this connection's finger or wheel on the Simulator and journal what it did (pause,
 * takeover, disconnect, rotation). With a reason, later moves of the finger are refused.
 */
function iosReleaseInput(context, client, reason = null) {
  if (!isIosCanvas(context)) return;
  finishScroll(context, client);
  const finger = context.iosFinger;
  if (finger && finger.client === client && !finger.wheel && (finger.pinch || finger.deferred)) {
    // A pending pinch is dropped, not sent: nothing of it is down on the HID stream.
    context.iosFinger = null;
    iosLetGoUnsent(client, finger, reason);
  } else if (finger && finger.client === client && !finger.wheel) {
    context.iosFinger = null;
    // Queued behind the finger's DOWN, the UP lifts it even when the connection is refused;
    // it is not sent when that DOWN was dropped.
    hidSend(context, { touch: finger.point, direction: "up" }, client, client.gesture?.action ?? null);
    if (reason) liftedByCanvas(client, finger.id, reason);
    if (client.gesture) client.gesture.end = iosGesturePoint(context, finger.x, finger.y);
  }
  finishGesture(context, client);
}

/** The session stopped with the companion (its held input is the orphan): drop the finger. */
function iosForgetInput(context, client) {
  const finger = context.iosFinger;
  if (finger?.client === client) context.iosFinger = null;
  if (finger?.client === client && (finger.pinch || finger.deferred)) iosLetGoUnsent(client, finger, null);
  finishScroll(context, client);
  if (finger?.client === client && !finger.wheel && client.gesture) {
    client.gesture.end = iosGesturePoint(context, finger.x, finger.y);
  }
  finishGesture(context, client);
}

/**
 * The companion or the Simulator went away with a finger down: its gesture ends here and its
 * later moves are refused. What the HID stream held is lifted through the next companion
 * (iosOrphanHeld, liftIosOrphan), whether or not its UP was still queued.
 */
function iosOrphanInput(context, client) {
  const finger = context.iosFinger;
  if (finger?.client === client) {
    context.iosFinger = null;
    if (finger.pinch || finger.deferred) {
      iosLetGoUnsent(client, finger, "the idb companion restarted");
    } else if (!finger.wheel) {
      liftedByCanvas(client, finger.id, "the idb companion restarted");
      if (client.gesture) client.gesture.end = iosGesturePoint(context, finger.x, finger.y);
    }
  }
  // The wheel's finger is no longer ours to lift: finishScroll only journals the burst.
  finishScroll(context, client);
  finishGesture(context, client);
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

function inputError(statusCode, message) {
  const error = new InputError(message);
  error.statusCode = statusCode;
  return error;
}

function assertDisplayPlatform(context) {
  if (context.options.platform !== "android") throw inputError(400, "Display presets are Android-only");
}

/** The preset named `id`; anything else is refused naming the valid ids (DISPLAY-005). */
function displayPreset(id) {
  const preset = DISPLAY_PRESETS.find((item) => item.id === id);
  if (!preset) {
    throw inputError(400, `Unknown display preset; expected one of: ${DISPLAY_PRESET_IDS.join(", ")}`);
  }
  return preset;
}

function supersededError(preset) {
  const error = inputError(409, `The display change to ${preset.id} was superseded by a later one`);
  error.superseded = true;
  return error;
}

function replyDisplayError(context, client, error) {
  if (!(error instanceof InputError)) context.state.lastError = error.message;
  const message = { t: "error", message: error.message, for: "display" };
  if (error.superseded) message.superseded = true;
  reply(context, client, message);
}

/**
 * Ask for `preset` on the Canvas target; resolves with the display in effect once it is
 * applied (DISPLAY-002). Changes run one at a time in the queue of HTTP input, in arrival
 * order, and at most one waits (DISPLAY-007): a newer request answers the waiting one
 * `superseded`, which then runs no `wm` command, and goes to the end of the queue. With
 * nothing queued after the waiting change, that place is the end already and the newer
 * request takes it over, so a flood of changes never grows the queue.
 */
function requestDisplay(context, preset, origin, client = null) {
  return new Promise((resolvePromise, reject) => {
    if (context.shuttingDown) {
      reject(inputError(503, "Canvas is stopping"));
      return;
    }
    const request = { preset, origin, client, resolve: resolvePromise, reject };
    const waiting = context.displayWaiting;
    if (waiting) {
      waiting.request.reject(supersededError(waiting.request.preset));
      if (waiting.ticket === context.inputEnqueued) {
        waiting.request = request;
        return;
      }
      waiting.request = null;
    }
    const entry = { request, ticket: 0 };
    context.displayWaiting = entry;
    context.enqueueInput(() => startDisplayChange(context, entry)).then(
      (display) => entry.request?.resolve(display),
      (error) => entry.request?.reject(error));
    entry.ticket = context.inputEnqueued;
  });
}

/** The change at the head of the queue; a superseded one was answered already and runs nothing. */
function startDisplayChange(context, entry) {
  if (context.displayWaiting === entry) context.displayWaiting = null;
  const { request } = entry;
  if (!request) return null;
  return changeDisplay(context, request.preset, request.origin, request.client);
}

/**
 * Held fingers and keys are lifted first, and those ups reach the device socket before any
 * `wm` command (DISPLAY-007). The overrides in effect before the first change are kept for
 * the restore at stop. A stop, a handoff or the asking connection closing ends a change
 * before its first `wm` command that changes the display; once started, only a stop ends
 * it, after the command running. The preset counts as in effect only once its commands
 * succeeded and the display reads back as it (DISPLAY-001).
 */
async function changeDisplay(context, preset, origin, client) {
  assertDisplayGoesOn(context, origin, client);
  liftForDisplayChange(context);
  await heldWritesSent(context);
  assertDisplayGoesOn(context, origin, client);
  if (!context.displayWritten) {
    const original = await readWm(context).catch((error) => {
      throw httpError(502, `wm could not read the display before changing it: ${adbFailure(error)}`);
    });
    context.displayOriginal = { size: original.sizeOverride, density: original.densityOverride };
    assertDisplayGoesOn(context, origin, client);
  }
  // From here on the stop has something to put back.
  context.displayWritten = true;
  const commands = runWm(context, presetCommands(preset), { change: true });
  context.displayRunning = commands;
  const [failure] = await commands;
  if (context.displayRunning === commands) context.displayRunning = null;
  // The restore at stop runs now and reads the display itself.
  if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
  const readAt = performance.now();
  const wm = await readWm(context).catch(() => null);
  if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
  const applied = !failure && matchesPreset(wm, preset);
  if (applied) context.displayApplied = preset;
  keepDisplayReading(context, wm, readAt);
  // screenrecord keeps the size it started with (DISPLAY-008).
  context.broadcaster.restartCapture();
  if (failure) throw httpError(502, `${failure}; in effect: ${describeDisplay(wm)}`);
  if (!wm) throw httpError(502, `${preset.id} was applied, but wm could not read the display back`);
  if (!applied) throw httpError(502, `${preset.id} was applied, but wm reads ${describeDisplay(wm)}`);
  console.log(`Display: ${preset.id} ${describeDisplay(wm)}`);
  journal(context, client ?? { origin, journalWaiting: 0 }, {
    kind: "display", transport: chooseTransport(context), preset: preset.id,
    width: wm.width, height: wm.height, density: wm.density,
  });
  return displayInfo(context);
}

/** A change goes on only while the Canvas runs and its sender is connected and may send input. */
function assertDisplayGoesOn(context, origin, client) {
  if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
  if (client && !context.controlClients.has(client)) {
    throw inputError(409, "The connection that asked for this display change closed");
  }
  // Control may have changed hands while this change waited for the ones before it.
  const refused = refusal(context, origin);
  if (refused) throw inputError(409, refused);
}

/** Lift every held finger and key before the display changes, as a takeover does. */
function liftForDisplayChange(context) {
  for (const client of context.controlClients) {
    for (const id of client.pointers.keys()) liftedByCanvas(client, id, "the display size changed");
    releaseInput(context, client);
  }
}

function presetCommands(preset) {
  if (preset.id === "default") return [["size", "reset"], ["density", "reset"]];
  return [["size", `${preset.width}x${preset.height}`], ["density", String(preset.density)]];
}

/** Put back the overrides found before the first change, or reset a value that had none. */
function restoreCommands(original) {
  return [
    ["size", original.size ? `${original.size.width}x${original.size.height}` : "reset"],
    ["density", original.density ? String(original.density) : "reset"],
  ];
}

/**
 * Run `wm` commands on the Canvas target in order; returns what failed, as text. A change
 * stops at its first failure, and before its next command once the Canvas stops, since
 * the restore puts back both values; the restore runs every command.
 */
async function runWm(context, commands, { change }) {
  const failures = [];
  for (const command of commands) {
    if (change && context.shuttingDown) break;
    try {
      await runAdb(context, ["shell", "wm", ...command], { timeout: WM_TIMEOUT_MS });
    } catch (error) {
      failures.push(`wm ${command.join(" ")} failed: ${adbFailure(error)}`);
      if (change) break;
    }
  }
  return failures;
}

function adbFailure(error) {
  if (error.killed) return `no answer within ${WM_TIMEOUT_MS} ms`;
  const output = `${error.stderr ?? ""}`.trim() || `${error.stdout ?? ""}`.trim() || error.message;
  return output.split("\n")[0].slice(0, 200);
}

/**
 * The target's size and density, with any overrides, from `wm size` and `wm density`.
 * With `untilStop`, a reading nobody needs once the Canvas stops runs no second command.
 */
async function readWm(context, { untilStop = false } = {}) {
  const size = await runAdb(context, ["shell", "wm", "size"], { timeout: WM_TIMEOUT_MS });
  if (untilStop && context.shuttingDown) throw new Error("Canvas is stopping");
  const density = await runAdb(context, ["shell", "wm", "density"], { timeout: WM_TIMEOUT_MS });
  return parseWm(size.stdout, density.stdout);
}

function parseWm(sizeText, densityText) {
  const sizes = {};
  for (const [, kind, width, height] of sizeText.matchAll(/(Physical|Override) size: (\d+)x(\d+)/g)) {
    sizes[kind] = { width: Number(width), height: Number(height) };
  }
  const densities = {};
  for (const [, kind, value] of densityText.matchAll(/(Physical|Override) density: (\d+)/g)) {
    densities[kind] = Number(value);
  }
  if (!sizes.Physical || !densities.Physical) {
    throw new Error("wm did not report the physical display size and density");
  }
  const size = sizes.Override ?? sizes.Physical;
  return {
    sizeOverride: sizes.Override ?? null,
    densityOverride: densities.Override ?? null,
    width: size.width,
    height: size.height,
    density: densities.Override ?? densities.Physical,
  };
}

/**
 * Whether a reading shows `preset`: its values, or no override for `default`. Values are
 * compared, not overrides, because Android drops an override equal to the physical value.
 */
function matchesPreset(wm, preset) {
  if (!wm) return false;
  if (!preset.width) return !wm.sizeOverride && !wm.densityOverride;
  return wm.width === preset.width && wm.height === preset.height && wm.density === preset.density;
}

/**
 * The preset a reading stands for: the last one this Canvas applied while the values still
 * match it, else `default` without overrides, else null, an override this Canvas did not make.
 */
function presetInEffect(wm, applied) {
  if (!wm) return null;
  if (applied && matchesPreset(wm, applied)) return applied.id;
  return wm.sizeOverride || wm.densityOverride ? null : "default";
}

function describeDisplay(wm) {
  return wm ? `${wm.width}x${wm.height} @ ${wm.density}` : "unknown (wm could not read the display)";
}

function displayInfo(context) {
  const wm = context.displayWm;
  return {
    preset: context.displayPreset,
    width: wm?.width ?? null,
    height: wm?.height ?? null,
    density: wm?.density ?? null,
  };
}

/**
 * Keep a `wm` reading begun at `at` (null when it failed) unless one begun later is kept
 * already, whichever finished first, and tell every page of a new preset or density, or
 * of the same ones when a state message showed a reading too old.
 */
function keepDisplayReading(context, wm, at) {
  const before = { preset: context.displayPreset, density: context.displayWm?.density ?? null };
  if (at >= context.displayWmAt) {
    context.displayWm = wm;
    context.displayWmAt = at;
    // Journaled gestures and HTTP input use the size read back.
    if (wm) context.display = { width: wm.width, height: wm.height };
  }
  context.displayPreset = presetInEffect(context.displayWm, context.displayApplied);
  const density = context.displayWm?.density ?? null;
  const changed = context.displayPreset !== before.preset || density !== before.density;
  if (changed || context.displayStaleSent) {
    context.displayStaleSent = false;
    broadcastState(context);
  }
}

/**
 * The display as /status and the state message show it (DISPLAY-001): a reading begun at
 * most `maxAge` ago, else a new one, which requests made meanwhile share, so a change made
 * outside this Canvas shows within that time. Nothing is read once the Canvas stops.
 * Never rejects.
 */
async function freshDisplay(context, maxAge = DISPLAY_READ_MAX_AGE_MS) {
  if (context.options.platform !== "android" || context.shuttingDown) return;
  if (performance.now() - context.displayWmAt <= maxAge) return;
  context.displayReading ??= readDisplayNow(context).finally(() => {
    context.displayReading = null;
  });
  await context.displayReading;
}

async function readDisplayNow(context) {
  const at = performance.now();
  let wm = null;
  try {
    wm = await readWm(context, { untilStop: true });
    context.displayReadFailed = false;
  } catch (error) {
    if (context.shuttingDown) return;
    // Pages poll /status every second or so: a reading that keeps failing is reported once.
    if (!context.displayReadFailed) {
      context.state.lastError = `display: wm could not read the display: ${adbFailure(error)}`;
    }
    context.displayReadFailed = true;
  }
  keepDisplayReading(context, wm, at);
}

/**
 * State messages go to WebSocket clients and show the display read at most
 * DISPLAY_READ_MAX_AGE_MS before (DISPLAY-001), whether or not a client asks /status.
 * So while a client is connected the reading is renewed once it is DISPLAY_WATCH_AGE_MS
 * old; with none, and once the Canvas stops, nothing reads the display unasked. Called
 * whenever a client comes or goes.
 */
function watchDisplay(context) {
  const watched = context.options.platform === "android" && !context.shuttingDown &&
    (context.videoClients.size > 0 || context.controlClients.size > 0);
  if (!watched) {
    clearTimeout(context.displayWatch);
    context.displayWatch = null;
    return;
  }
  if (context.displayWatch) return;
  const due = Math.max(0, context.displayWmAt + DISPLAY_WATCH_AGE_MS - performance.now());
  const watch = setTimeout(() => {
    freshDisplay(context, DISPLAY_WATCH_AGE_MS).then(() => {
      // A watch ended meanwhile, by the last client leaving, leaves any newer one alone.
      if (context.displayWatch !== watch) return;
      context.displayWatch = null;
      watchDisplay(context);
    });
  }, due);
  context.displayWatch = watch;
}

/**
 * /status `display`: on Android the size and density of one reading, the preset in effect
 * and every preset (DISPLAY-001); the bridge's size when wm could not be read.
 */
function displayStatus(context, measured) {
  if (context.options.platform !== "android") return measured;
  const wm = context.displayWm;
  return {
    width: wm?.width ?? measured?.width ?? null,
    height: wm?.height ?? measured?.height ?? null,
    density: wm?.density ?? null,
    preset: context.displayPreset,
    presets: DISPLAY_PRESETS,
  };
}

/**
 * At stop, put back the overrides found before the first change (DISPLAY-003). Only the
 * `wm` commands of a change already running are waited for, and that change runs no other;
 * queued input and queued changes are dropped by the stop, never waited for. A Canvas whose
 * `wm` commands never changed the display runs none. Written to the journal as preset
 * `restore` by `system` (DISPLAY-006), and on stdout either way.
 */
async function restoreDisplay(context) {
  await context.displayRunning;
  const original = context.displayOriginal;
  if (!context.displayWritten || !original) return;
  try {
    const failures = await runWm(context, restoreCommands(original), { change: false });
    const wm = await readWm(context).catch(() => null);
    console.log(failures.length
      ? `Display restore failed: ${failures.join("; ")}; in effect: ${describeDisplay(wm)}`
      : `Display restored: ${describeDisplay(wm)}`);
    const record = { kind: "display", transport: chooseTransport(context), preset: "restore" };
    if (wm) Object.assign(record, { width: wm.width, height: wm.height, density: wm.density });
    await journalRestore(context, record);
  } catch (error) {
    console.log(`Display restore failed: ${error.message}`);
  }
}

/**
 * The restore record goes through the bridge like every other (DISPLAY-006). The bridge
 * answers one line at a time, so behind a swipe or text still running it would wait past
 * the shutdown budget and be lost with the bridge; then a bridge of its own, on the same
 * target and with the same allowlist, writes it.
 */
async function journalRestore(context, record) {
  if (!context.actionBridge.busy()) {
    await new Promise((resolvePromise) => {
      journal(context, { origin: "system", journalWaiting: 0 }, record, resolvePromise);
    });
    return;
  }
  let bridge = null;
  try {
    bridge = createActionBridge(context.options, context.adbPath, context.serial);
    context.restoreBridge = bridge;
    await new Promise((resolvePromise, reject) => {
      // A bridge that cannot start answers nothing; its error must not end the stop.
      bridge.child.on("error", reject);
      journalPayload(context, record)
        .then((payload) => bridge.call("record", payload, "system"))
        .then(resolvePromise, reject);
    });
  } catch (error) {
    context.state.lastError = `journal: ${error.message}`;
  } finally {
    bridge?.close();
  }
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
 * JOURNAL_QUEUE_RECORDS already pending is dropped and counted. `onAnswered` runs once
 * the bridge answered the record, or at once for a dropped one.
 */
function journal(context, client, record, onAnswered = null, action = null) {
  if (record.kind !== "control" && isIosCanvas(context) && client.ws) {
    // iOS: the record of an action goes through the input queue behind its HID events and
    // any text before it, so the journal keeps the order of actions. Refused by its turn
    // with none of its own events (`action`) written, the action did not happen and is not
    // journaled.
    iosEnqueue(context, { kind: "record", client, record, onAnswered, action });
    return;
  }
  journalNow(context, client, record, onAnswered);
}

function journalNow(context, client, record, onAnswered) {
  if (journalPending(context) >= JOURNAL_QUEUE_RECORDS) {
    context.journalDropped += 1;
    context.state.lastError = `journal: ${context.journalDropped} actions were not journaled ` +
      `because ${JOURNAL_QUEUE_RECORDS} records were waiting for the bridge`;
    onAnswered?.();
    return;
  }
  context.journalQueue.push({ client, origin: client.origin, record, onAnswered });
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
    }).finally(() => {
      journalAnswered(context, entry.client);
      entry.onAnswered?.();
    });
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
  const payload = { transport: isIosCanvas(context) ? "idb" : "scrcpy", ...record };
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

/**
 * The display size a page computed its points for, from the optional `dw` and `dh` of
 * POST /tap and /swipe, or null for a request without them, as an agent's. Both or
 * neither, each an integer from 1 to MAX_SENT_DISPLAY_SIDE.
 */
function sentDisplaySize(body) {
  if (body.dw === undefined && body.dh === undefined) return null;
  const valid = (value) => Number.isInteger(value) && value >= 1 && value <= MAX_SENT_DISPLAY_SIDE;
  if (!valid(body.dw) || !valid(body.dh)) {
    throw httpError(400, `dw and dh must both be integers from 1 to ${MAX_SENT_DISPLAY_SIDE}`);
  }
  return { width: body.dw, height: body.dh };
}

/**
 * Points a page computed for the display size `sent`, on the display read back now. A page
 * on a multipart transport learns of a display change made by an agent or another page
 * only at its next status poll, and its taps carry the old size until then (DISPLAY-007).
 * Points without a size, or for the size in effect, are left as they are.
 */
function onCurrentDisplay(context, sent, points) {
  const current = context.displayWm ?? context.display;
  if (!sent || !current?.width || !current?.height) return points;
  if (sent.width === current.width && sent.height === current.height) return points;
  return points.map(([x, y]) => [
    normalizeCoordinate(x * current.width / sent.width, "x"),
    normalizeCoordinate(y * current.height / sent.height, "y"),
  ]);
}

async function tap(context, response, body, origin) {
  assertControl(context, origin);
  const x = normalizeCoordinate(body.x, "x");
  const y = normalizeCoordinate(body.y, "y");
  const sent = sentDisplaySize(body);
  // Scaled when its turn comes, so after any display change queued before it.
  const result = await context.enqueueInput(() => {
    const [[tx, ty]] = onCurrentDisplay(context, sent, [[x, y]]);
    return context.actionBridge.call("tap", { x: tx, y: ty }, origin);
  });
  sendJson(response, 200, result);
}

async function swipe(context, response, body, origin) {
  assertControl(context, origin);
  const x1 = normalizeCoordinate(body.x1, "x1");
  const y1 = normalizeCoordinate(body.y1, "y1");
  const x2 = normalizeCoordinate(body.x2, "x2");
  const y2 = normalizeCoordinate(body.y2, "y2");
  const duration = Math.min(5000, Math.max(1, normalizeCoordinate(body.duration ?? 250, "duration")));
  const sent = sentDisplaySize(body);
  const result = await context.enqueueInput(() => {
    const [[sx1, sy1], [sx2, sy2]] = onCurrentDisplay(context, sent, [[x1, y1], [x2, y2]]);
    return context.actionBridge.call("swipe", { x1: sx1, y1: sy1, x2: sx2, y2: sy2, duration }, origin);
  });
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

async function setDisplay(context, response, body, origin) {
  assertDisplayPlatform(context);
  assertControl(context, origin);
  const preset = displayPreset(body?.preset);
  let display;
  try {
    display = await requestDisplay(context, preset, origin);
  } catch (error) {
    if (!error.superseded) throw error;
    sendJson(response, 409, { error: error.message, superseded: true });
    return;
  }
  sendJson(response, 200, { ok: true, display });
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
        pending.set(id, { op, resolve: resolvePromise, reject });
        child.stdin.write(`${JSON.stringify({ id, op, payload, origin })}\n`);
      });
    },
    /** Whether a call other than a journal record waits for its answer: input can take seconds. */
    busy() {
      for (const waiter of pending.values()) if (waiter.op !== "record") return true;
      return false;
    },
    child,
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

// Inline SVG paths of the page's icons, on a 24 px grid; stroke and size come from the CSS.
const PAGE_ICONS = Object.freeze({
  mark: '<path d="M12 3 4 20h4l4-9 4 9h4z"/>',
  phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  phoneLandscape: '<rect x="2.5" y="7" width="19" height="10" rx="2.5"/><path d="M18.5 11v2"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  people: '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.5a3 3 0 0 1 0 5.5M18 19a5.5 5.5 0 0 0-2.5-4.6"/>',
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
  return `<button type="button" class="popup" id="display-preset" aria-haspopup="listbox" aria-expanded="false" ` +
    `aria-controls="display-list" title="Display size" disabled>${icon("phone", "portrait")}${icon("phoneLandscape", "landscape")}<span class="sr">Display size: </span>` +
    `<span id="display-label">Size</span>${icon("chevron", "chev")}</button>` +
    `<div class="menu" id="display-menu"><div role="listbox" id="display-list" aria-label="Display size">${options}</div>` +
    `<p class="note">Changes the device's screen size and density. Restored when the Canvas stops.</p></div>`;
}

/**
 * The Canvas page (PAGE-001..004): toolbar, the device in a frame that follows the video's
 * aspect, a floating pill of device buttons and an inspector. Styles, icons and code are
 * inline (the CSP loads nothing else); device and status strings reach the DOM as text only.
 */
function renderPage(context) {
  const serial = escapeHtml(context.serial);
  const android = context.options.platform === "android";
  const platform = android ? "Android" : "iOS";
  // iOS refuses the Android key buttons, so its page keeps them out of sight (PAGE-003);
  // Home and Power stay, as the bridge presses the Simulator's Home and Lock for them.
  const androidOnly = android ? "" : " hidden";
  // Volume buttons: scrcpy on Android; on iOS only the idb transport presses them, so they
  // show there (data-idb) and stay out of sight on screencap.
  const volume = android ? " data-scrcpy" : " data-idb hidden";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Autonom Canvas · ${serial}</title>
<link rel="icon" href="data:,">
<style>
:root{
  color-scheme:light dark;
  --bg:#f5f5f7;--surface:#fff;--raised:#fff;--text:#1d1d1f;--text-2:#6e6e73;--line:rgba(0,0,0,.09);--line-2:rgba(0,0,0,.14);
  --fill:rgba(0,0,0,.045);--fill-2:rgba(0,0,0,.08);--accent:#0071e3;--accent-fill:#0071e3;--on-accent:#fff;--live:#1f9d55;--warn:#b25000;--idle:#8e8e93;
  --bezel:#1d1d1f;--shadow:0 1px 1px rgba(0,0,0,.04),0 10px 30px rgba(0,0,0,.08);--device-shadow:0 2px 6px rgba(0,0,0,.08),0 24px 60px rgba(0,0,0,.16);
  --material:rgba(255,255,255,.96);--r-sm:8px;--video-ratio:.4615;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0b0c;--surface:#161617;--raised:#1f1f21;--text:#f5f5f7;--text-2:#a1a1a6;--line:rgba(255,255,255,.09);--line-2:rgba(255,255,255,.16);
  --fill:rgba(255,255,255,.06);--fill-2:rgba(255,255,255,.11);--accent:#0a84ff;--accent-fill:#0068d6;--live:#32d74b;--warn:#ff9f0a;
  --bezel:#1c1c1e;--shadow:0 1px 1px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.45);--device-shadow:0 0 0 1px rgba(255,255,255,.16),0 24px 70px rgba(0,0,0,.7);
  --material:rgba(36,36,38,.95);
}}
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
.bar{grid-area:bar;position:relative;z-index:4;display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:12px;padding:0 14px;border-bottom:1px solid var(--line);background:var(--surface)}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.mark{width:22px;height:22px;border-radius:6px;background:var(--text);color:var(--bg);display:grid;place-items:center;flex:none}
.mark svg{width:14px;height:14px;stroke-width:2}
.brand b{font-weight:600;font-size:14px}
.target{display:flex;align-items:center;gap:6px;min-width:0;color:var(--text-2);white-space:nowrap;overflow:hidden}
.target strong{color:var(--text);font-weight:500;overflow:hidden;text-overflow:ellipsis}
.dot{width:7px;height:7px;border-radius:50%;background:var(--idle);flex:none}
.dot[data-state=live]{background:var(--live);box-shadow:0 0 0 3px color-mix(in srgb,var(--live) 18%,transparent)}
.dot[data-state=warn]{background:var(--warn)}
.center{position:relative;display:flex;align-items:center;gap:10px;min-width:0}
.popup{display:flex;align-items:center;gap:7px;height:30px;padding:0 10px 0 9px;border-radius:var(--r-sm);border:1px solid var(--line-2);background:var(--raised);font-weight:500;white-space:nowrap;box-shadow:0 1px 1px rgba(0,0,0,.04)}
.popup .chev{width:12px;height:12px;color:var(--text-2)}
.popup .landscape,.popup.wide .portrait{display:none}
.popup.wide .landscape{display:block}
.dims{color:var(--text-2);font-variant-numeric:tabular-nums;white-space:nowrap}
.actions{display:flex;justify-content:flex-end;align-items:center;gap:6px;min-width:0}
.chip{display:flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:999px;background:var(--fill);color:var(--text-2);font-weight:500;white-space:nowrap}
.chip svg{width:14px;height:14px}
.icon{width:32px;height:32px;display:grid;place-items:center;border-radius:var(--r-sm);border:0;background:transparent;color:var(--text-2)}
.icon[aria-pressed=true]{color:var(--accent)}

/* size menu */
.menu{position:absolute;top:calc(100% + 5px);left:50%;z-index:5;width:288px;padding:6px;border-radius:12px;background:var(--material);
  -webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);border:1px solid var(--line-2);box-shadow:var(--shadow);
  opacity:0;visibility:hidden;transform:translateX(-50%) scale(.98);transform-origin:50% 0;transition:opacity .12s ease-out,transform .12s ease-out,visibility 0s linear .12s}
.menu.open{opacity:1;visibility:visible;transform:translateX(-50%);transition-delay:0s}
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

/* stage */
.stage{grid-area:stage;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-width:0;min-height:0;padding:28px 24px 96px;overflow:hidden}
.device{position:relative;flex:none;box-sizing:content-box;aspect-ratio:9/19.5;padding:9px;border-radius:44px;background:var(--bezel);box-shadow:var(--device-shadow);touch-action:none;
  width:min(calc(100% - 18px),calc((100vh - 224px) * var(--video-ratio)));width:min(calc(100% - 18px),calc((100dvh - 224px) * var(--video-ratio)))}
.surface{position:absolute;inset:9px;display:block;width:calc(100% - 18px);height:calc(100% - 18px);object-fit:contain;border-radius:35px;background:#000;
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
.side{grid-area:side;border-left:1px solid var(--line);background:var(--surface);overflow:auto;padding:6px 0 24px}
.group{padding:14px 16px;border-bottom:1px solid var(--line)}
.group:last-child{border-bottom:0}
.group h2{margin:0 0 10px;font-size:12px;font-weight:600;color:var(--text-2);letter-spacing:.01em}
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
.owner{display:flex;align-items:flex-start;gap:8px;margin:0;color:var(--text-2)}
.owner .dot{margin-top:5px}
.owner strong{color:var(--text);font-weight:600}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
dl{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:7px 12px}
dt{color:var(--text-2)}
dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dd code{font:12px var(--mono)}
.linkbtn{margin-top:12px;padding:0;border:0;background:none;color:var(--accent);font-weight:500}
.diag{margin-top:12px}
.diag summary{width:max-content;cursor:pointer;color:var(--text-2);font-size:12px}
.status{margin:8px 0 0;font:11.5px/1.45 var(--mono);color:var(--text-2);white-space:pre-wrap;overflow-wrap:anywhere}

@media (hover:hover){
  .option:hover{background:var(--accent-fill);color:var(--on-accent)}
  .option:hover small{color:inherit}
  .popup:not(:disabled):hover,.icon:hover,.btn:not(.primary):not(:disabled):hover,.row:not(:disabled):hover{background:var(--fill)}
  .icon:hover{color:var(--text)}
  .key:not(:disabled):hover,.dock button:not(:disabled):hover{background:var(--fill-2)}
}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){:root{--material:var(--raised)}}
@media (forced-colors:active){.option:focus-visible{outline:2px solid CanvasText}}
@media (max-width:900px){.dims{display:none}}

/* phone: the inspector moves under the device, the pill stays above the safe area, 44 px targets */
@media (max-width:760px){
  body{display:block;height:auto;min-height:100%}
  .bar{position:sticky;top:0;grid-template-columns:auto minmax(0,1fr) auto;min-height:calc(52px + env(safe-area-inset-top));padding:env(safe-area-inset-top) 12px 0}
  .target,.chip,.brand b,.actions .icon{display:none}
  .center{justify-content:center}
  .menu{position:fixed;top:calc(56px + env(safe-area-inset-top));width:min(320px,calc(100vw - 24px))}
  .stage{padding:16px 16px 92px;overflow:visible}
  .device{padding:7px;border-radius:36px;width:min(calc(100% - 14px),calc((100svh - 208px) * var(--video-ratio)))}
  .surface{inset:7px;width:calc(100% - 14px);height:calc(100% - 14px);border-radius:29px}
  .notice{position:fixed;top:calc(60px + env(safe-area-inset-top))}
  .dock{position:fixed;bottom:calc(14px + env(safe-area-inset-bottom));z-index:4}
  .dock button{width:44px;height:44px}
  .side{border-left:0;border-top:1px solid var(--line);padding-bottom:calc(110px + env(safe-area-inset-bottom))}
  .popup,.option,.key,.btn,.field input,.row{height:44px}
  body.no-inspector .side{display:block}
  .linkbtn{min-height:44px}
  .diag summary{line-height:44px}
  .field input{font-size:16px}
}
@media (max-width:360px){.dock{gap:0;padding:2px}.dock .div{display:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}button:active{transform:none!important}}
</style>
</head>
<body>
<header class="bar">
  <div class="brand">
    <span class="mark" aria-hidden="true">${icon("mark")}</span>
    <b>Autonom</b>
    <span class="target"><i class="dot" id="live-dot" aria-hidden="true"></i><strong>${serial}</strong><span id="target-detail">${platform} · connecting</span></span>
  </div>
  <div class="center">
    ${displayPicker(context)}
    <span class="dims" id="display-dims"></span>
  </div>
  <div class="actions">
    <span class="chip" title="Who controls the device">${icon("people")}<span id="control-chip">Shared</span></span>
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
    ${iconButton("rotate", "Rotate", `data-system="rotate" data-scrcpy${androidOnly}`)}
    ${iconButton("volumeDown", "Volume down", `data-system="volume-down"${volume}`)}
    ${iconButton("volumeUp", "Volume up", `data-system="volume-up"${volume}`)}
    ${iconButton("power", "Power", 'data-key="KEYCODE_POWER" data-system="power"')}
  </nav>
</main>
<aside class="side" id="inspector" aria-label="Inspector">
  <section class="group">
    <h2>Type</h2>
    <div class="field"><input id="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Text to type" aria-label="Text to type"><button type="button" id="sendText" class="btn primary">Send</button></div>
    <p class="hint" id="text-hint">Any language. Longer text is pasted through the device clipboard.</p>
  </section>
  <section class="group"${androidOnly}>
    <h2>Keys</h2>
    <div class="keys">
      <span></span>${iconButton("up", "Up", 'data-key="KEYCODE_DPAD_UP" data-code="19"', "key")}<span></span>
      ${iconButton("left", "Left", 'data-key="KEYCODE_DPAD_LEFT" data-code="21"', "key")}<button type="button" class="key" data-key="KEYCODE_ENTER" data-code="66">Enter</button>${iconButton("right", "Right", 'data-key="KEYCODE_DPAD_RIGHT" data-code="22"', "key")}
      <span></span>${iconButton("down", "Down", 'data-key="KEYCODE_DPAD_DOWN" data-code="20"', "key")}${iconButton("delete", "Delete", 'data-key="KEYCODE_DEL" data-code="67"', "key")}
    </div>
  </section>
  <section class="group"${androidOnly}>
    <h2>Device</h2>
    <div class="list">
      <button type="button" class="row" data-key="KEYCODE_WAKEUP" data-system="wake">${icon("wake")}Wake screen</button>
      <button type="button" class="row" data-system="notifications" data-scrcpy>${icon("notifications")}Notifications</button>
      <button type="button" class="row" data-system="quick-settings" data-scrcpy>${icon("quickSettings")}Quick settings</button>
      <button type="button" class="row" data-system="collapse" data-scrcpy>${icon("collapse")}Collapse panels</button>
      <button type="button" class="row" id="clipboard" data-scrcpy>${icon("clipboard")}Copy device clipboard</button>
    </div>
  </section>
  <section class="group">
    <h2>Control</h2>
    <p class="owner"><i class="dot" id="owner-dot" aria-hidden="true"></i><span><strong id="owner-name">Shared</strong> <span id="owner-detail">· people and agents can send input</span></span></p>
    <div class="pair"><button type="button" class="btn" id="control-take" disabled>Take control</button><button type="button" class="btn" id="control-pause" disabled>Pause input</button></div>
  </section>
  <section class="group">
    <h2>Stream</h2>
    <dl>
      <dt>Transport</dt><dd id="stream-transport">connecting</dd>
      <dt>Codec</dt><dd><code id="stream-codec">—</code></dd>
      <dt>Frames</dt><dd id="stream-frames">—</dd>
      <dt>Video</dt><dd id="stream-video">—</dd>
      <dt>Display</dt><dd id="stream-display">—</dd>
      <dt>Clients</dt><dd id="stream-clients">—</dd>
    </dl>
    <button type="button" class="linkbtn" id="refresh">Reconnect stream</button>
    <details class="diag"><summary>Diagnostics</summary><pre class="status" id="status">Connecting…</pre></details>
  </section>
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
const image=$("screen"),video=$("video"),device=$("device"),statusEl=$("status"),textInput=$("text"),sizePicker=$("display-preset");
const sizeMenu=$("display-menu"),sizeLabel=$("display-label"),defaultSize=$("display-default-size"),sendButton=$("sendText");
const ui={liveDot:$("live-dot"),target:$("target-detail"),dims:$("display-dims"),chip:$("control-chip"),inspector:$("inspector-toggle"),caption:$("caption"),notice:$("notice"),hint:$("text-hint"),ownerDot:$("owner-dot"),ownerName:$("owner-name"),ownerDetail:$("owner-detail"),take:$("control-take"),pause:$("control-pause"),transport:$("stream-transport"),codec:$("stream-codec"),frames:$("stream-frames"),videoSize:$("stream-video"),display:$("stream-display"),clients:$("stream-clients")};
const PRESET_LABELS=${JSON.stringify(Object.fromEntries(DISPLAY_PRESETS.map(({ id, label }) => [id, label])))};
const WIDE_PRESETS=${JSON.stringify(DISPLAY_PRESETS.filter(({ width, height }) => width > height).map(({ id }) => id))};
const OWNERS={shared:["Shared","people and agents can send input"],human:["People","agents cannot send input"],agent:["Agent","this page cannot send input"]};
const NOTICE_MS=8000;
const sizeOptions=[...document.querySelectorAll("[data-preset]")];
// Every device input control; the clipboard read, handoff and Reconnect act for anyone.
const inputControls=[...document.querySelectorAll("[data-system],[data-code],[data-key]"),textInput,sendButton];
const ctx=video.getContext("2d");
let csrf=null,logicalDisplay=null,reconnectTimer=null,pointer=null,displayBusy=false,refocusPicker=false,menuOpen=false,noteAt=0,frameRatio=0,rotation=0;
const view={transport:null,mode:null,reason:"",status:null,owner:"shared",paused:false,session:"idle",clients:null,width:0,height:0,note:"",rtt:null,preset:null,density:null,clipboard:false};
const stats={decoder:"none",codec:null,framesDecoded:0,framesRendered:0,framesDropped:0,packets:0,bytes:0,errors:0,renderTimes:[],lastFrameAt:0};
let videoSocket=null,controlSocket=null,decoder=null,config=null,waitingKey=true,drawScheduled=false,configuring=false,pendingChunks=[];
const MAX_QUEUED_FRAMES=2,frameQueue=[];
let videoRetry=500,controlRetry=500,videoRefusals=0;
const supportedCodecs=new Map(),activePointers=new Map(),heldKeys=new Map();
function setStatus(value){statusEl.textContent=value}
function url(path,cacheBust=false){const params=new URLSearchParams();if(cacheBust)params.set("ts",String(Date.now()));const query=params.toString();return path+(query?"?"+query:"")}
function wsUrl(path){const params=new URLSearchParams();if(csrf)params.set("csrf",csrf);const query=params.toString();return(location.protocol==="https:"?"wss://":"ws://")+location.host+path+(query?"?"+query:"")}
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
function startMultipart(reason){view.mode="multipart";view.reason=reason||"";stats.decoder="multipart";closeVideoSocket();if(decoder&&decoder.state!=="closed")decoder.close();decoder=null;dropQueuedFrames();video.hidden=true;image.hidden=false;restart()}

// Fast video (scrcpy, idb): WebSocket packets decoded by WebCodecs and drawn on animation frames.
function startWebCodecs(){view.mode="webcodecs";stats.decoder="webcodecs";clearTimeout(reconnectTimer);image.removeAttribute("src");image.hidden=true;video.hidden=false;connectVideo()}
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
function fallback(reason){note(reason);startMultipart(reason)}
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
function connectControl(){if(controlSocket||!fastActive())return;const socket=new WebSocket(wsUrl("/ws/control"));controlSocket=socket;socket.onopen=()=>{controlRetry=500};socket.onmessage=event=>{let message;try{message=JSON.parse(event.data)}catch{return}onControlMessage(message)};socket.onclose=()=>{if(controlSocket!==socket)return;controlSocket=null;activePointers.clear();displayBusy=false;if(fastActive()){setTimeout(connectControl,controlRetry);controlRetry=Math.min(5000,controlRetry*2)}}}
function closeControlSocket(){const socket=controlSocket;controlSocket=null;if(socket)socket.close()}
function onControlMessage(message){if(message.t==="state")applyState(message);else if(message.t==="display")onDisplay(message.display);else if(message.t==="error"){stats.errors+=1;note(message.message);if(message.for==="display"){displayBusy=false;syncPicker()}}else if(message.t==="clipboard"){if(typeof message.text!=="string"){note("the device clipboard has no text");return}textInput.value=message.text;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(message.text).catch(()=>{});note("device clipboard copied")}else if(message.t==="paste"){if(message.message)note(message.message)}else if(message.t==="pong"){view.rtt=Math.round(performance.now()-message.ts)}}
function sendControl(message){if(!controlSocket||controlSocket.readyState!==1)return false;controlSocket.send(JSON.stringify(message));return true}
function applyState(message){view.owner=message.owner;view.paused=message.paused;view.session=message.session;view.clients=message.clients;if("clipboard" in message){view.clipboard=message.clipboard===true;if(iosFast())applyHint()}if("preset" in message){view.preset=message.preset;view.density=message.density}syncPicker();if("rotation" in message)setRotation(message.rotation);if(view.mode==="webcodecs"&&message.width&&message.height)setVideoSize(message.width,message.height);if(message.transport&&message.transport!==view.transport){view.transport=message.transport;applyTransport()}}
function applyHint(){setText(ui.hint,iosFast()?(view.clipboard?"Plain ASCII up to 300 bytes is typed into the Simulator; longer or other text is set on its clipboard: touch and hold, then Paste.":"Typed into the Simulator, up to 300 bytes at a time."):scrcpyActive()?"Any language. Longer text is pasted through the device clipboard.":"Letters, digits and punctuation of plain ASCII on this transport.")}
function applyTransport(){textInput.placeholder=fastActive()?"Text to type":"ASCII text to type";applyHint();for(const button of document.querySelectorAll("[data-scrcpy]"))button.hidden=!scrcpyActive();for(const button of document.querySelectorAll("[data-idb]"))button.hidden=!iosFast();if(fastActive()){connectControl();if(!view.mode){if(typeof VideoDecoder==="function"&&typeof EncodedVideoChunk==="function")startWebCodecs();else startMultipart("VideoDecoder is not available in this browser")}}else{closeControlSocket();if(view.mode!=="multipart")startMultipart("")}}
// Size picker (Android): the preset in effect is selected; a choice goes the way other input goes.
function canSendInput(){return !view.paused&&(view.owner==="shared"||view.owner==="human")}
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
function syncControls(){const ready=Boolean(view.status)&&canSendInput(),owner=OWNERS[view.owner]||[String(view.owner),"holds control"];for(const control of inputControls)if(control.disabled===ready)control.disabled=!ready;setText(ui.ownerName,owner[0]);setText(ui.ownerDetail,"· "+(view.paused?"input is paused":owner[1]));setText(ui.chip,owner[0]+(view.paused?" · Paused":""));setState(ui.ownerDot,ready?"live":"warn");setText(ui.take,view.owner==="human"?"Release":"Take control");setText(ui.pause,view.paused?"Resume input":"Pause input");for(const button of [ui.take,ui.pause])if(button.disabled===Boolean(view.status))button.disabled=!view.status}
function setControl(mode){if(fastActive()&&sendControl({t:"control",mode}))return;post("/control",{mode}).then(result=>{view.owner=result.control_owner;view.paused=result.input_paused;syncPicker()}).catch(error=>note(error.message))}
ui.take.addEventListener("click",()=>setControl(view.owner==="human"?"release":"takeover"));
ui.pause.addEventListener("click",()=>setControl(view.paused?"resume":"pause"));
ui.inspector.addEventListener("click",()=>{const hidden=document.body.classList.toggle("no-inspector");ui.inspector.setAttribute("aria-pressed",String(!hidden));ui.inspector.title=hidden?"Show inspector":"Hide inspector"});
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
function onPointerDown(event){if(!fastActive()){legacyPointerDown(event);return}if(event.pointerType==="mouse"&&event.button!==0)return;const at=normalized(event,false);if(!at)return;event.preventDefault();const pinch=event.pointerType==="mouse"&&(event.ctrlKey||event.altKey);if(iosFast()&&iosFingers()+(pinch?2:1)>2){note("the iOS Simulator takes two fingers at most, sent as one pinch when one lifts");return}surface().focus();surface().setPointerCapture(event.pointerId);activePointers.set(event.pointerId,{pinch});sendTouch("down",event.pointerId,at,pinch,pressure(event))}
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
function onKeyDown(event){if(iosFast()){onIosKey(event);return}if(!scrcpyActive())return;const input=keyInputFor(event,KEYCODES,US_KEY_CHARS);if(!input)return;event.preventDefault();if(input.text!==undefined){sendControl({t:"text",text:input.text});return}const repeat=event.repeat?(heldKeys.get(event.code)||0)+1:0;heldKeys.set(event.code,repeat);sendControl({t:"key",a:"down",code:input.code,meta:metaFor(event),repeat})}
function onKeyUp(event){if(!scrcpyActive()||!heldKeys.has(event.code))return;event.preventDefault();heldKeys.delete(event.code);sendControl({t:"key",a:"up",code:KEYCODES[event.code],meta:metaFor(event),repeat:0})}
function releaseKeys(){for(const name of heldKeys.keys()){const code=KEYCODES[name];if(code!==undefined)sendControl({t:"key",a:"up",code,meta:0,repeat:0})}heldKeys.clear()}
function onPaste(event){if(!fastActive())return;const text=event.clipboardData&&event.clipboardData.getData("text/plain");if(!text)return;event.preventDefault();if(iosFast()&&!view.clipboard){if(utf8Length(text)>300){note("the iOS Simulator takes at most 300 bytes of text at a time; nothing was sent");return}sendControl({t:"text",text});return}const message={t:"paste",text};if(!fitsControl(message,"clipboard text"))return;sendControl(message)}
function utf8Length(text){return new TextEncoder().encode(text).length}
function iosTypeable(text){return /^[\\n\\x20-\\x7e]*$/.test(text)}
function fitsControl(message,what){const size=utf8Length(JSON.stringify(message));if(size<=MAX_CONTROL_BYTES)return true;note(what+" is too large: "+size+" bytes as a control message after JSON escaping, at most "+MAX_CONTROL_BYTES+"; nothing was sent");return false}
for(const button of document.querySelectorAll("[data-system],[data-code],[data-key]")){button.addEventListener("click",()=>{if(fastActive()){if(button.dataset.system)sendControl({t:"system",op:button.dataset.system});else if(button.dataset.code){const code=Number(button.dataset.code);sendControl({t:"key",a:"down",code,meta:0,repeat:0});sendControl({t:"key",a:"up",code,meta:0,repeat:0})}return}if(button.dataset.key)post("/key",{key:button.dataset.key}).catch(error=>note(error.message))})}
$("clipboard").onclick=()=>sendControl({t:"clipboard-get"});
$("refresh").onclick=()=>{if(view.mode==="webcodecs"){closeVideoSocket();waitingKey=true;videoRetry=500;connectVideo()}else restart()};
async function sendText(){const value=textInput.value;if(!value)return;if(iosFast()&&!view.clipboard){if(utf8Length(value)>300){note("the iOS Simulator takes at most 300 bytes of text at a time; nothing was sent");return}if(sendControl({t:"text",text:value}))textInput.value="";return}if(fastActive()){const message=utf8Length(value)<=300&&(!iosFast()||iosTypeable(value))?{t:"text",text:value}:{t:"paste",text:value};if(!fitsControl(message,"the text"))return;const ok=sendControl(message);if(ok)textInput.value="";return}try{await post("/text",{text:value});textInput.value=""}catch(error){note(error.message)}}
$("sendText").onclick=sendText;textInput.addEventListener("keydown",event=>{if(event.key==="Enter")sendText()});
function transportLabel(){if(!view.transport)return"connecting";if(!fastActive())return view.transport;if(view.mode==="webcodecs")return scrcpyActive()?"scrcpy (webcodecs)":view.transport+" (webcodecs)";return view.transport+" (multipart"+(view.reason?": "+view.reason:"")+")"}
function render(){const data=view.status||{},lines=["platform: "+(data.platform||"unknown"),"transport: "+transportLabel()];if(data.fallback_reason)lines.push("fallback: "+data.fallback_reason);if(view.mode==="webcodecs"){lines.push("decoder: "+(stats.codec||"waiting for config"));lines.push("fps: "+stats.renderTimes.length+" rendered, "+stats.framesDecoded+" decoded, "+stats.framesDropped+" dropped, queue "+(decoder?decoder.decodeQueueSize:0));lines.push("video: "+(view.width?view.width+"x"+view.height:"waiting")+", session "+view.session)}else{lines.push("frames: "+(data.frames_sent??0)+"\\nstream clients: "+(data.stream_clients??0))}if(view.clients)lines.push("clients: video "+view.clients.video+", control "+view.clients.control);lines.push("display: "+(data.display&&data.display.width?data.display.width+"x"+data.display.height+(view.density?" @ "+view.density:"")+(sizePicker?" ("+(view.preset||"other")+")":""):"unknown"));lines.push("control: "+view.owner+(view.paused?" (paused)":""));if(data.last_error)lines.push("last error: "+data.last_error);if(view.note)lines.push("note: "+view.note);setStatus(lines.join("\\n"));syncPicker();renderChrome()}
// The toolbar, caption, notice and Stream details; every string reaches the page as text.
function sizeText(width,height){return width&&height?width+" × "+height:""}
function displayText(){const display=logicalDisplay||{},size=sizeText(display.width,display.height);return size&&view.density?size+" · "+view.density+" dpi":size}
function liveState(){if(!view.status)return"idle";if(view.paused)return"warn";if(view.mode==="webcodecs")return view.session==="streaming"&&stats.framesRendered?"live":"idle";return view.mode==="multipart"&&image.naturalWidth?"live":"idle"}
function captionText(){if(view.mode==="webcodecs")return stats.renderTimes.length+" fps"+(view.rtt!=null?" · "+view.rtt+" ms":"")+" · WebCodecs";if(view.mode==="multipart")return(view.transport?view.transport+" · ":"")+"multipart stream";return"Connecting…"}
function renderChrome(){const data=view.status||{},webcodecs=view.mode==="webcodecs";if(view.status)setText(ui.target,(data.platform==="ios"?"iOS":"Android")+" · "+(view.transport||"connecting"));setState(ui.liveDot,liveState());setText(ui.dims,displayText());setText(ui.caption,captionText());setText(ui.notice,view.note&&Date.now()-noteAt<NOTICE_MS?view.note:"");setText(ui.transport,fastActive()?view.transport+" · "+(webcodecs?"WebCodecs":"multipart"):view.transport||"connecting");setText(ui.codec,webcodecs?stats.codec||"waiting":"—");setText(ui.frames,webcodecs?stats.renderTimes.length+" fps · "+stats.framesDropped+" dropped":(data.frames_sent??0)+" sent");setText(ui.videoSize,(webcodecs?sizeText(view.width,view.height):sizeText(image.naturalWidth,image.naturalHeight))||"waiting");setText(ui.display,displayText()||"unknown");setText(ui.clients,view.clients?view.clients.video+" video · "+view.clients.control+" control":(data.stream_clients??0)+" stream");if(view.mode==="multipart"&&image.naturalWidth)fitFrame(image.naturalWidth,image.naturalHeight)}
async function poll(){try{const response=await fetch(url("/status")),data=await response.json();if(!response.ok)throw new Error(data.error||response.statusText);view.status=data;logicalDisplay=data.display;view.owner=data.control_owner;view.paused=data.input_paused;if(data.display&&"preset" in data.display){view.preset=data.display.preset;view.density=data.display.density}rememberDefaultSize(data.display);syncPicker();if(data.transport!==view.transport){view.transport=data.transport;applyTransport()}}catch(error){note(error.message);if(!view.mode)startMultipart("status unavailable")}finally{setTimeout(poll,1200)}}
function snapshot(){return{transport:view.transport,mode:view.mode,decoder:stats.decoder,fallbackReason:view.reason||null,codec:stats.codec,framesDecoded:stats.framesDecoded,framesRendered:stats.framesRendered,framesDropped:stats.framesDropped,decodeQueueSize:decoder?decoder.decodeQueueSize:0,fps:stats.renderTimes.length,packets:stats.packets,bytes:stats.bytes,errors:stats.errors,lastFrameAt:stats.lastFrameAt,width:view.width,height:view.height,rotation,session:view.session,owner:view.owner,paused:view.paused,clients:view.clients,control:controlSocket?controlSocket.readyState:-1,video:videoSocket?videoSocket.readyState:-1,rtt:view.rtt,note:view.note,display:{preset:view.preset,density:view.density,picker:sizePicker?sizePicker.value:null,pickerDisabled:sizePicker?sizePicker.disabled:null},status:statusEl.textContent}}
window.autonomCanvas=Object.freeze({stats:snapshot,send:sendControl});
async function authenticate(){const body=bootstrapToken?{token:bootstrapToken}:{};const response=await fetch("/auth",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(response.ok){csrf=payload.csrf;return}if(bootstrapToken)throw new Error(payload.error||"Authentication failed");note("open the Canvas URL with its #token to sign in")}
async function bootstrap(){await authenticate();setInterval(render,500);await poll()}
bootstrap().catch(error=>{setStatus(error.message);setText(ui.notice,error.message)});`;
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
