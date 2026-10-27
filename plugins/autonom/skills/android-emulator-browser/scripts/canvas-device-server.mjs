import { prepareHostVideo, CaptureDecorations } from "./host-video.mjs";
/** One device's transports, input, authentication and lifecycle inside the Canvas. */
import { mkdtempSync, rmSync } from "node:fs";
import {
  access,
  constants,
  readFile,
  rm,
} from "node:fs/promises";
import { ServerResponse } from "node:http";
import { homedir, tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { env, platform } from "node:process";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { createInterface } from "node:readline";
import {
  DEFAULT_SCRCPY_MAX_FPS,
  SOFTWARE_ENCODER_MAX_FPS,
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
  parseControlMessage,
  parseCookies,
  screenrecordMaxSize,
  streamMaxSize,
  streamMaxFps,
} from "./browser-lib.mjs";
import {
  COPY_KEY,
  KEY_ACTION,
  MOTION_ACTION,
  SCRCPY_PROTOCOL_VERSION,
  encodeBackOrScreenOn,
  encodeEmpty,
  encodeGetClipboard,
  encodeKeycode,
  encodeScroll,
  encodeSetClipboard,
  encodeText,
  encodeTouch,
} from "./scrcpy-lib.mjs";
import {
  SCRCPY_CAPABILITY,
  SCRCPY_INSTALL_HINT,
  ScrcpySession,
  resolveScrcpyServer,
} from "./scrcpy-session.mjs";
import {
  CLOSE_CODE,
  READY_STATE,
  acceptWebSocket,
  rejectUpgrade,
} from "./ws.mjs";
import { DisplayChrome } from "./canvas-display-chrome.mjs";
import { createCanvasTools, handleToolsRoute } from "./canvas-tools-server.mjs";
import { profileVideo, splitDevicePath } from "./canvas-devices.mjs";
import {
  AUTO_ACQUIRE_WAIT_MS,
  CONTROL_GENERATION_STALE,
  CONTROL_HELD,
  CONTROL_NOT_HELD,
  DEFAULT_CONTROL_IDLE_S,
  HUMAN_LEASE_TTL_S,
  LEASE_ERROR_CODES,
  LEASE_POLL_MS,
  LEASE_RENEW_MS,
  createFileCache,
  describeHolder,
  leaseFor,
  leasePath,
  leaseState,
  sessionRecordPath,
  sessionStatus,
} from "./canvas-control.mjs";
import {
  DEFAULT_VIDEO_OPTIONS,
  ERROR_CODE,
  IDB_CAPABILITY,
  IdbCompanion,
  StructuredError,
  idbUnavailableError,
  resolveIdbCompanionBinary,
} from "./ios-idb-companion.mjs";
import {
  BOUNDARY,
  BRIDGE_MAX_RESTARTS,
  BRIDGE_RESTART_MAX_MS,
  BRIDGE_RESTART_MIN_MS,
  BRIDGE_STABLE_MS,
  CLIENT_BACKLOG_BYTES,
  CLIPBOARD_ACK_TIMEOUT_MS,
  CLIPBOARD_ANSWER_MS,
  CLIPBOARD_HOLD_READ_BYTES,
  CLIPBOARD_SETTLE_MS,
  CLOSE_TRY_AGAIN_LATER,
  DEVICE_BACKLOG_PAUSE_BYTES,
  DISPLAY_PRESETS,
  DISPLAY_PRESETS_JSON,
  DISPLAY_PRESET_IDS,
  DISPLAY_READ_MAX_AGE_MS,
  DISPLAY_WATCH_AGE_MS,
  FAST_TRANSPORTS,
  FRAME_DESTINATIONS,
  HELD_COMPACT_SLOTS,
  HELD_INPUT_BYTES,
  HELD_MESSAGE_OVERHEAD_BYTES,
  IDB_FIRST_FRAME_MS,
  IDB_FORCED_KEY_FRAME_MS,
  IDB_KEY_FRAME_WAIT_MS,
  IDB_ORIENTATION_MS,
  IDB_RETRY_MAX_MS,
  IDB_RETRY_MIN_MS,
  IDB_SIMULATOR_WATCH_MS,
  IDB_STABLE_MS,
  INJECTABLE_TEXT,
  IOS_BUTTONS,
  IOS_CLIPBOARD_MAX_BYTES,
  IOS_EMPTY_PASTEBOARD,
  IOS_HID_WRITE_MS,
  IOS_MIRROR_ID_MAX,
  IOS_ONE_FINGER,
  IOS_PASTE_NOT_TYPEABLE,
  IOS_PASTE_TOO_LONG,
  IOS_PINCH_MAX_S,
  IOS_PINCH_MIN_RADIUS,
  IOS_ROTATION,
  IOS_SIMCTL_MS,
  IOS_TEXT_MAX_BYTES,
  IOS_TYPEABLE_TEXT,
  IOS_WHEEL_EDGE_POINTS,
  IOS_WHEEL_IDLE_MS,
  IOS_WHEEL_MARGIN_POINTS,
  IOS_WHEEL_MIN_POINTS,
  IOS_WHEEL_POINTS,
  JOURNAL_CLIENT_PAUSE_RECORDS,
  JOURNAL_IN_FLIGHT,
  JOURNAL_QUEUE_RECORDS,
  KEY_FRAME_CACHE_BYTES,
  LIFTED_POINTERS_MAX,
  MAX_SENT_DISPLAY_SIDE,
  MULTIPART_BACKLOG_BYTES,
  RECENT_UP_MS,
  RECORD_MAX_COUNT,
  RECORD_MAX_DURATION_MS,
  RECORD_MAX_POINTERS,
  RECORD_MAX_SCROLL,
  REPLY_BACKLOG_PAUSE_BYTES,
  ROTATION_STOP_WAIT_MS,
  SCREENRECORD_PROBE_MS,
  SCROLL_BURST_IDLE_MS,
  SESSION_IDLE_STOP_MS,
  SHUTDOWN_STOP_MS,
  STATE_BACKLOG_BYTES,
  SYSTEM_KEYS,
  SYSTEM_PANELS,
  VIDEO_CLIENT_MAX_MESSAGE_BYTES,
  VIDEO_MESSAGE,
  WM_TIMEOUT_MS,
  deviceClosings,
  execFileAsync,
  httpError,
  isPng,
  logEvent,
  logged,
  sendHtml,
  sendJson,
  sleep,
} from "./canvas-runtime.mjs";
import { renderPage } from "./canvas-device-page.mjs";

/**
 * What the Canvas holds once, whatever devices it shows: its options, access token, cookie
 * sessions, port and HTTP server, and whether it is stopping.
 */
export function createServerContext(options) {
  return {
    options,
    token: options.noAuth ? "" : (options.token ?? generateToken()),
    sessions: new Map(),
    port: null,
    httpServer: null,
    shuttingDown: false,
  };
}

/**
 * Checks one device and finds the tools that show it: adb (or xcrun on iOS), its serial
 * (inferred when `target` is undefined), ffmpeg, scrcpy-server, screenrecord support and
 * idb_companion. Refuses a transport the device cannot use.
 */
export async function resolveDeviceOptions(serverOptions, { platform, target }) {
  const options = { ...serverOptions, platform: platform ?? serverOptions.platform };
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
  const serial = target ?? (isIos
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
  return {
    platform: options.platform, target: serial, adbPath, ffmpegPath, screenrecordSupported, scrcpy, idb,
  };
}

/**
 * Everything the Canvas holds for one device (resolveDeviceOptions): its options, state,
 * input queue, action bridge, panel tools, stream and input state. The token, cookie
 * sessions and port are the server's.
 */
export function createDeviceContext(server, device) {
  const { platform, target: serial, adbPath, ffmpegPath, screenrecordSupported, scrcpy, idb } = device;
  const options = { ...server.options, platform, target: serial, serial };
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
  const id = `${platform}~${serial}`;
  const workspace = server.mode === "workspace";
  // Every bridge and tools request carries the device's bound session and this Canvas's
  // lease claim (contract 6.5); `context` is read when a request is written.
  const binding = () => requestBinding(context);
  // A workspace Canvas restarts a bridge that exits (contract 3.8); the single-device one
  // keeps the bridge it started with.
  const actionBridge = workspace
    ? createRestartingBridge(options, adbPath, serial, { onFailed: () => server.bridgeFailed?.(id), binding })
    : createActionBridge(options, adbPath, serial, { binding });
  // The panel tools: their own process and the device log feed, apart from input.
  const tools = createToolsFor(options, adbPath, serial, {
    deviceId: id, deviceName: device.name ?? null, commandLog: server.commandLog ?? null, binding,
  });

  const context = {
    options,
    adbPath,
    serial,
    ffmpegPath,
    screenrecordSupported,
    scrcpy,
    idb,
    get token() { return server.token; },
    state,
    get port() { return server.port; },
    get sessions() { return server.sessions; },
    server,
    id,
    name: device.name ?? serial,
    // The workspace stream profile (profileVideo), null on the single-device Canvas until a
    // Display menu choice is made there.
    profile: device.profile ?? null,
    // The Display menu choice ({fps, size}, QW-04) the stream should use, null for none, and
    // the one its newest finished restart applied; displaySeq counts restarts and
    // displayApplying is true while the newest one runs.
    displayChoice: null,
    displayApplied: null,
    displaySeq: 0,
    displayApplying: false,
    actionBridge,
    tools,
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
    displayChrome: null,
    displayWritten: false,
    // The change waiting in the input queue, at most one, and the `wm` commands of the one
    // running, which are all the restore at stop waits for.
    displayWaiting: null,
    displayRunning: null,
    // Manual rotation is opt-in on the first Rotate; restored when this Canvas stops.
    rotationOriginal: null,
    rotationWritten: false,
    rotationPending: false,
    rotationRunning: null,
    // Cancels the adb children of the Rotate's own `wm` writes, so none lands after a restore.
    rotationAbort: null,
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
    // The host control lease of this device (contract 6.2).
    lease: null,
    enqueueInput(task, { release = false } = {}) {
      // Stamped now: input queued before a control change does not run after it (6.4),
      // except a release, which only lets go of what the device holds.
      const epoch = context.lease.epoch;
      inputQueue = inputQueue.catch(() => {}).then(() => {
        // Once the Canvas stops, queued input is dropped instead of delaying the restore.
        if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
        if (!release && context.lease.epoch !== epoch) {
          throw inputError(409, "Input queued before a control change was not sent", CONTROL_GENERATION_STALE);
        }
        return task();
      });
      context.inputEnqueued += 1;
      return inputQueue;
    },
  };
  context.lease = createLeaseState(context);
  context.broadcaster = new FrameBroadcaster(context);
  return context;
}

/**
 * Stops one device's part of the Canvas: lifts its input, closes its clients, and stops
 * its device server, display changes, panel tools and bridges. Safe to call again.
 */
export async function closeDeviceContext(context) {
  let closing = deviceClosings.get(context);
  if (!closing) {
    closing = closeDevice(context);
    deviceClosings.set(context, closing);
  }
  return closing;
}

async function closeDevice(context) {
  // From here on no input for this device is queued or served.
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
  clearInterval(context.lease.pollTimer);
  // The device server and the adb forward must be gone before the process is, and the
  // display back as it was, within the 5 s the supervisor allows after SIGTERM.
  // The tools process and the log feed go with it: no orphans. A held control lease is
  // handed back first, through the bridge, while it still runs (bounded, 6.4).
  await Promise.race([
    Promise.all([releaseLeaseOnClose(context),
      stopSession(context).then(() => context.hostDecorations?.restore()), restoreDisplay(context),
      restoreDeviceRotation(context), context.tools.close()]),
    sleep(SHUTDOWN_STOP_MS),
  ]);
  context.actionBridge.close();
  context.restoreBridge?.close();
}

function printHelp() {
  console.log(`android-emulator-browser

Token-protected Android/iOS device preview and input bridge for an agent's side-panel browser.

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
  --fps FPS                   Frame rate cap (default: 15; ${SOFTWARE_ENCODER_MAX_FPS} on software scrcpy, ${DEFAULT_SCRCPY_MAX_FPS} on hardware/idb).
  --max-size PX               Longest video side on scrcpy, video width on screenrecord;
                              0 is native size. Default on scrcpy: follows the device's
                              H.264 encoder (1280 when software only, native with a
                              hardware one, 1280 when it cannot tell); otherwise 1280.
  --bit-rate BPS              H.264 bitrate (default: 12000000).
  --scrcpy-server PATH        scrcpy-server file to push (default: AUTONOM_SCRCPY_SERVER,
                              SCRCPY_SERVER_PATH, then the server of scrcpy on PATH).
  --scrcpy-version X.Y        Version of --scrcpy-server when its file name does not say.
  --token TOKEN               Use a supplied access token.
  --python PATH               Python executable for the persistent action bridge.
  --bridge PATH               Override autonom_canvas_bridge.py.
  --tools PATH                Override autonom_canvas_tools.py (the panel tools process).
  --autonom PATH              Override scripts/autonom.py for the device log feed.
  --no-auth                   Disable token protection (isolated local use only).
  --control-idle SECONDS      Hand the device's control lease back after this long without
                              human input (default: ${DEFAULT_CONTROL_IDLE_S}; 15 to 3600).

Workspace (tabs of 1-4 tiles, devices attached and detached while it runs):
  --workspace NAME            Open the named workspace (default: default).
  --split                     Open the default workspace.
  --device PLATFORM:ID        Place a device (android:<serial> or ios:<udid>); repeatable,
                              at most 8. One --device alone is the single-device Canvas.
  --ephemeral                 Do not read or save the workspace file.
  --bootable SPEC             avd:NAME[@PORT] or simulator:UDID the page may boot; repeatable.
  --shutdown-booted           Shut down the devices this Canvas booted when it stops.
  --install-root DIR          Folder the Actions drawer may install builds from; repeatable.
  --captures-dir DIR          Captures folder (default: AUTONOM_CAPTURES_DIR, then ~/Downloads/Autonom).
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
  if (context.server?.shuttingDown || context.shuttingDown) {
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
    await logged(context, "input", "tap", origin, response,
      async () => tap(context, response, await readJsonBody(request), origin));
  } else if (request.method === "POST" && url.pathname === "/swipe") {
    await logged(context, "input", "swipe", origin, response,
      async () => swipe(context, response, await readJsonBody(request), origin));
  } else if (request.method === "POST" && url.pathname === "/key") {
    await logged(context, "input", "key", origin, response,
      async () => key(context, response, await readJsonBody(request), origin));
  } else if (request.method === "POST" && url.pathname === "/text") {
    await logged(context, "input", "text", origin, response,
      async () => text(context, response, await readJsonBody(request), origin));
  } else if (request.method === "POST" && url.pathname === "/control") {
    await logged(context, "control", "control", origin, response,
      async () => control(context, response, await readJsonBody(request), origin));
  } else if (request.method === "POST" && url.pathname === "/display") {
    await logged(context, "display", "display", origin, response,
      async () => setDisplay(context, response, await readJsonBody(request), origin));
  } else if (url.pathname.startsWith("/tools/")) {
    const answer = await handleToolsRoute(context.tools, {
      method: request.method, url, origin,
      readBody: () => readJsonBody(request),
      refusal: (from, rule = {}) => refusal(context, from, rule),
      // A person's leased Tools/Actions op takes the lease first, like other input (6.4).
      acquire: () => ensureHumanLease(context).then((failure) => {
        if (!failure) context.lease.lastHumanHttpAt = performance.now();
        return failure;
      }),
      request, response, deviceId: context.id,
    });
    if (answer?.handled) return;
    sendJson(response, answer.status, answer.body);
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

/**
 * The origin a request names in X-Autonom-Origin. A client that names none, or names a value
 * that is not exactly one of ORIGINS ("Agent", "bogus"), is an agent (RISK-018 / DEC-003:
 * fail closed, so its input is counted and guarded); a person's input is exempt only when it
 * says `human` exactly, as the Canvas pages always do.
 */
function normalizeOrigin(value) {
  if (value === undefined || value === null || value === "") return "agent";
  return ORIGINS.includes(value) ? value : "agent";
}

/**
 * WebSocket upgrades: browsers do not apply CORS to them, so a cookie alone is never
 * enough — the page must also prove it is ours with the CSRF value and its Origin.
 * API clients use the token and may name their origin with ?origin=.
 */
function authorizeUpgrade(context, request, url) {
  const originParam = url.searchParams.get("origin");
  const apiClient = () => {
    // No ?origin= is an agent, as for HTTP (RISK-018); the page names `human` itself.
    if (originParam === null) return { ok: true, origin: "agent" };
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
    if (url === null) return false;
    if (url.pathname === "/ws/video" || url.pathname === "/ws/control") return true;
    const split = splitDevicePath(url.pathname);
    return split !== null && (split.rest === "/ws/video" || split.rest === "/ws/control");
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
  if (context.server?.shuttingDown || context.shuttingDown) {
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
  // Android already shares a bounded-age wm reading between page polls and the
  // display watcher. Asking the bridge as well runs another wm size + dumpsys
  // input for every client poll, only to discard that result in displayStatus().
  // Keep the bridge fallback when wm failed, and the existing iOS fast path.
  // The bound session is independently refreshed below for session_stopped.
  await freshDisplay(context);
  // wm reports natural orientation. Keep the bridge's rotated input dimensions
  // when the video is rotated (or no stream metadata can confirm orientation).
  const wm = context.displayWm, videoSize = context.video.size;
  const reuseWm = context.options.platform === "android" && wm && videoSize &&
    (wm.width > wm.height) === (videoSize.width > videoSize.height);
  const measured = reuseWm
    ? { display: { width: wm.width, height: wm.height } }
    : iosStreamDisplay(context) ?? await context.actionBridge.call("screen-size", {}, "system").catch((error) => {
      if (!error.errorCode) throw error;
      return { display: null };
    });
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
    stream_settings: streamSettings(context),
    tools: context.tools.status(),
    lease: leaseStatus(context),
    session_stopped: (refreshSession(context), sessionStoppedOf(context)),
    ...(context.server?.mode === "workspace" ? { device: context.server.deviceStatus(context) } : {}),
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
    max_size: stats?.maxSize ?? scrcpySize?.maxSize ?? null,
    max_fps: stats?.maxFps ?? null,
    video_backend: stats?.videoBackend ?? "android",
    host_fallback: stats?.hostFallback ?? null,
    host_capture: stats?.hostCapture ?? null,
    max_size_source: scrcpySize?.source ?? null,
    encoder: stats?.videoBackend === "videotoolbox" ? "hardware" : scrcpySize?.encoder ?? null,
    encoder_name: stats?.videoBackend === "videotoolbox" ? "VideoToolbox" : scrcpySize?.encoderName ?? null,
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
    while (!stopped && this.#clients.size && multipartTransport(context) === "screencap") {
      // Read each round: a focus change lowers or raises the cap of a running capture.
      const fps = Math.min(10, multipartFps(context));
      const interval = Math.round(1000 / fps);
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
    const filter = `fps=${multipartFps(this.#context)},scale=min(${screenrecordMaxSize(options)}\\,iw):-2`;
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
  // The first-frame watchdog of the current stream: { stream, timer, since }, and how the stream
  // that runs reports the watchdog's error to its run loop.
  #frameWatch = null;
  #streamFail = null;
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

  /**
   * New frame rate and scale for the running stream (a workspace focus change): the stream
   * is restarted on the same companion with them, and later streams open with them too.
   */
  setVideo({ fps, scaleFactor } = {}) {
    this.#video = {
      ...this.#video,
      ...(fps !== undefined ? { fps } : {}),
      ...(scaleFactor !== undefined ? { scaleFactor } : {}),
    };
    const stream = this.#stream;
    if (this.#stopped || !stream || stream.done || this.#streamRestarting) return Promise.resolve(false);
    this.#streamRestarting = true;
    this.#stats.streamRestarts += 1;
    return stream.restart({ options: this.#video }).then(() => {
      this.#stats.streamsOpened += 1;
      if (this.#stream === stream && !stream.done && !this.#stopped) this.#watchFirstFrame(stream);
      return true;
    }, (error) => {
      if (!stream.abnormal && this.listenerCount("error")) {
        this.emit("error", new Error(`idb stream restart (profile change) failed: ${error.message}`));
      }
      return false;
    }).finally(() => {
      this.#streamRestarting = false;
    });
  }

  /** Stop for good: Stop on the stream, then the companion and its process group. Idempotent. */
  stop() {
    if (this.#stopPromise) return this.#stopPromise;
    this.#stopped = true;
    clearTimeout(this.#watchTimer);
    clearTimeout(this.#orientationTimer);
    clearTimeout(this.#keyFrameTimer);
    this.#clearFrameWatch();
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
    let error = null;
    this.#streamFail = (watchdog) => { error ??= watchdog; };
    this.#watchFirstFrame(stream);
    try {
      for await (const frame of stream) this.#onFrame(frame);
    } catch (thrown) {
      error ??= thrown;
    } finally {
      this.#clearFrameWatch();
      this.#streamFail = null;
      if (this.#stream === stream) this.#stream = null;
    }
    const closed = await stream.closed;
    return { abnormal: closed.abnormal, error: error ?? closed.error ?? null, ranMs: performance.now() - openedAt };
  }

  /**
   * Ends `stream` when it sends no frame within IDB_FIRST_FRAME_MS: armed when it opens and
   * again after each restart, so a call reopened by Stop and Start that stays silent is
   * ended too and the run loop opens a new stream (replacing the companion when the Stop
   * goes unanswered).
   */
  #watchFirstFrame(stream) {
    this.#clearFrameWatch();
    // Frames the old call queued before a restart arrived before this: they do not count.
    const watch = { stream, timer: null, since: performance.now() };
    watch.timer = setTimeout(() => {
      if (this.#frameWatch !== watch) return;
      this.#frameWatch = null;
      if (this.#stopped || stream.done || this.#stream !== stream) return;
      this.#streamFail?.(new Error(`the idb video stream sent no frame within ${IDB_FIRST_FRAME_MS / 1000} s`));
      stream.stop().catch(() => {});
    }, IDB_FIRST_FRAME_MS);
    watch.timer.unref?.();
    this.#frameWatch = watch;
  }

  #clearFrameWatch() {
    clearTimeout(this.#frameWatch?.timer);
    this.#frameWatch = null;
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
    if (this.#frameWatch && !(frame.receivedAt < this.#frameWatch.since)) this.#clearFrameWatch();
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
      if (this.#stream === stream && !stream.done && !this.#stopped) this.#watchFirstFrame(stream);
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
    ...(context.server?.mode === "workspace"
      ? { profile: context.server.profileName(context), focused: context.server.focus?.focus === context.id }
      : {}),
    // The host control lease and the bound session (contract 6.7).
    ...(context.lease ? { lease: leaseStatus(context), session_stopped: sessionStoppedOf(context) } : {}),
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

/**
 * The multipart frame rate: --fps, capped by the stream profile (a workspace focus profile
 * or a Display menu choice); 0, frames on change, is a companion mode and caps nothing here.
 */
function multipartFps(context) {
  const cap = context.profile?.fps;
  return Number.isFinite(cap) && cap > 0 ? Math.min(context.options.fps, cap) : context.options.fps;
}

/**
 * The long side, in px, an Android stream of this device uses at full size: the base the
 * Display menu's Half halves. --max-size when set, else the size the encoders chose, else
 * the device's screen; null while none is known (a native size before the screen is read).
 */
export function halfSizeBase(context) {
  const { options } = context;
  if (options.maxSizeExplicit && options.maxSize > 0) return options.maxSize;
  const chosen = context.state.fullStreamSize?.maxSize;
  if (chosen > 0) return chosen;
  const display = context.display;
  if (display?.width > 0 && display?.height > 0) return Math.max(display.width, display.height);
  return null;
}

/**
 * The stream settings /status reports (QW-04): the Display choice (null for none) and the
 * frame rate, Android size (0 is native, null until known) or iOS scale the stream uses.
 */
function streamSettings(context) {
  const ios = isIosCanvas(context);
  const profile = context.profile ?? null;
  const fast = FAST_TRANSPORTS.has(chooseTransport(context));
  const fastFps = profile ? profile.fps : context.options.fpsExplicit ? context.options.fps : DEFAULT_SCRCPY_MAX_FPS;
  return {
    choice: context.displayChoice ? { ...context.displayChoice } : null,
    fps: fast ? fastFps : multipartFps(context),
    max_size: ios ? null : Number.isFinite(profile?.maxSize) ? profile.maxSize : streamSizeInUse(context)?.maxSize ?? null,
    scale_factor: ios ? profile?.scaleFactor ?? 1 : null,
  };
}

/** Start the device server (or companion) for the first WebSocket client; at most one per Canvas. */
function ensureSession(context) {
  clearTimeout(context.idleTimer);
  context.idleTimer = null;
  const transport = chooseTransport(context);
  if (context.shuttingDown || context.session || !FAST_TRANSPORTS.has(transport)) return;
  const { options, adbPath, serial, scrcpy } = context;
  // A workspace device streams with its focus profile (contract 3.7): its frame rate, and
  // in the background a smaller Android size or iOS scale.
  const profile = context.profile ?? null;
  const maxFps = profile ? profile.fps : options.fpsExplicit ? options.fps : DEFAULT_SCRCPY_MAX_FPS;
  const profileSize = Number.isFinite(profile?.maxSize) ? profile.maxSize : null;
  if (profileSize !== null && transport === "scrcpy") {
    context.state.streamSize = { maxSize: profileSize, source: "profile", encoder: null, encoderName: null };
  } else if (transport === "scrcpy" && context.state.streamSize?.source === "profile") {
    // Back at full size: the size before the profile's, until the encoders are read again.
    context.state.streamSize = options.maxSizeExplicit ? streamMaxSize(options) : context.state.fullStreamSize ?? null;
  }
  const session = transport === "idb"
    ? new IosFastSession({
      udid: serial,
      xcrunPath: adbPath,
      binary: context.idb.path,
      video: {
        ...DEFAULT_VIDEO_OPTIONS, fps: maxFps, avgBitrate: options.bitRate,
        ...(profile && profile.scaleFactor !== 1 ? { scaleFactor: profile.scaleFactor } : {}),
      },
    })
    : new ScrcpySession({
      hostFactory: opts => prepareHostVideo({ ...opts,
        decorations: context.hostDecorations ??= new CaptureDecorations(),
        orientation: async () => { context.hostRotation = await currentDeviceRotation(context); },
        geometry: () => context.displayWm ? { ...context.displayWm, rotation: context.hostRotation } : null }),
      adbPath,
      serial,
      serverPath: scrcpy.serverPath,
      version: scrcpy.version,
      maxSize: profileSize ?? options.maxSize,
      // Without --max-size the size follows the device's encoder (DEC-005).
      chooseMaxSize: options.maxSizeExplicit || profileSize !== null ? null : (probe) => {
        context.state.streamSize = streamMaxSize(options, probe);
        context.state.fullStreamSize = context.state.streamSize;
        logStreamSize(context);
        return context.state.streamSize.maxSize;
      },
      chooseMaxFps: options.fpsExplicit || maxFps <= SOFTWARE_ENCODER_MAX_FPS ? null
        : (probe) => streamMaxFps(options, probe, maxFps),
      bitRate: options.bitRate,
      maxFps,
    });
  if (transport === "scrcpy") logStreamSize(context);
  if (context.server?.mode === "workspace") {
    logEvent(context.server, { device: context.id, kind: "stream", name: "start", origin: "system",
      summary: `${transport} up to ${maxFps} fps` }, { ok: true });
  }
  context.session = session;
  session.on("orientation", () => {
    // A rotation ends a finger or wheel drag: its points belong to the old orientation.
    for (const client of context.controlClients) iosReleaseInput(context, client, "the Simulator rotated");
    const streamed = iosStreamDisplay(context);
    if (streamed) context.display = streamed.display;
    broadcastState(context);
  });
  session.on("state", (name) => {
    if (context.session !== session) return;
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
      if (context.server?.mode === "workspace") {
        logEvent(context.server, { device: context.id, kind: "stream", name: "fallback", origin: "system",
          summary: `${transport} failed` }, { ok: false, errorCode: "backend_failed", error: error.message });
      }
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

/**
 * On the idb transport the companion already reported the screen in points, as the bridge
 * would read it from the accessibility tree; asking the bridge on every /status would run an
 * accessibility dump per page poll and hold journal records behind it. Null otherwise.
 */
function iosStreamDisplay(context) {
  if (!isIosCanvas(context) || !(context.session instanceof IosFastSession) || !context.session.points) return null;
  return { display: iosLogicalSize(context) };
}

/** The display size puts journaled gestures in device pixels, like HTTP input. */
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
  // A long cached GOP overwhelms a newly created tab's decoder. It may drop a
  // delta and wait forever on a static screen. Ask for a fresh frame instead.
  if (video.cache.length && video.cache.length <= 30) {
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
    // Pointers whose down was refused (their moves and ups are ignored): on iOS while another
    // finger was down, on every platform when the lease or the device refused the down; and
    // this connection's input waiting in the iOS input queue.
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
  return context.rotationPending || context.clipboardHold !== null || context.lease?.busy != null ||
    pastBounds(context, client, share);
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

class InputError extends Error {}

// ---------------------------------------------------------------------------------------
// The host control lease (contract 6). The lease file is written only by the bridge's
// control ops; this Canvas reads it on every admission. A person's first input on a device
// nobody holds takes the human lease (DEC-013); Take over replaces a live holder; Hand back
// and the idle timeout release it; a lease lost to another holder or to expiry ends this
// Canvas's control. Without a lease file every route behaves as before.

// A human page that sent input over HTTP counts as present this long (renewing goes on).
const LEASE_HTTP_PRESENCE_MS = 30_000;
// A held lease is released at stop within this long, or left to expire.
const LEASE_RELEASE_WAIT_MS = 500;
const LEASE_ERROR_SET = new Set(LEASE_ERROR_CODES);

/** The human lease TTL: 30 s; a test seam (AUTONOM_TEST_MODE=1) may shorten it to 5 s. */
function humanLeaseTtl() {
  const raw = env.AUTONOM_CANVAS_HUMAN_TTL_S ?? "";
  if (env.AUTONOM_TEST_MODE === "1" && /^\d{1,4}$/.test(raw)) {
    const value = Number(raw);
    if (value >= 5 && value <= HUMAN_LEASE_TTL_S) return value;
  }
  return HUMAN_LEASE_TTL_S;
}

function createLeaseState(context) {
  const lease = {
    cache: createFileCache(leasePath(env, context.options.platform, context.serial)),
    // The bound session's record, watched while its id is known (6.6).
    session: null,
    sessionStatus: "none",
    view: { state: "none", holder: null, generation: null, expires_at: null, expires_at_ms: null },
    key: null,
    held: null,
    epoch: 0,
    pollTimer: null,
    renewing: false,
    renewedAt: -Infinity,
    lastHumanInputAt: -Infinity,
    lastHumanHttpAt: -Infinity,
    acquiring: null,
    // A lease op the WebSocket input waits for (an acquire, Take over, Hand back), and the
    // messages that wait with it, in arrival order.
    busy: null,
    waiting: [],
    releasing: null,
    changeQueued: false,
    tookOverFrom: null,
    endedBy: null,
  };
  context.lease = lease;
  refreshLease(context);
  lease.pollTimer = setInterval(() => leaseTick(context), LEASE_POLL_MS);
  lease.pollTimer.unref?.();
  return lease;
}

/** This Canvas as a lease holder: `canvas:<port>:<pid>`, label `Canvas <port>`. */
function holderOf(context) {
  const port = context.port ?? 0;
  return { id: `canvas:${port}:${process.pid}`, label: `Canvas ${port}` };
}

/** The session id this device is bound to: the workspace's link, null on the single-device Canvas. */
function boundSessionId(context) {
  if (context.server?.mode !== "workspace") return null;
  return context.server.registry?.get(context.id)?.session?.session_id ?? null;
}

/** The extra fields of a bridge or tools request: only those known, so other requests stay as they were. */
function requestBinding(context) {
  if (!context?.lease) return {};
  const extra = {};
  const sessionId = boundSessionId(context);
  if (sessionId) extra.session_id = sessionId;
  const { held } = context.lease;
  if (held) extra.control = { holder_id: held.holder_id, generation: held.generation };
  return extra;
}

function controlIdleMs(context) {
  const seconds = context.options.controlIdle ?? DEFAULT_CONTROL_IDLE_S;
  return seconds * 1000;
}

/**
 * Read the lease (a stat; the file only when it changed) and update the view. A change of
 * holder, generation or state bumps the epoch and runs onLeaseChange soon, except a grant
 * to this Canvas (its own acquire adds rights to input it already admitted).
 */
function refreshLease(context) {
  const lease = context.lease;
  const { value } = lease.cache.get();
  const file = leaseFor(value, { platform: context.options.platform, target: context.serial });
  const state = leaseState(file, Date.now());
  const holder = state === "held" || state === "expired" ? { ...file.holder } : null;
  lease.view = {
    state, holder, generation: file ? file.generation : null,
    expires_at: holder ? (file.expires_at ?? null) : null,
    expires_at_ms: holder ? file.expires_at_ms : null,
    previous_holder: file?.previous_holder ?? null,
  };
  const key = `${state}|${holder?.id ?? ""}|${lease.view.generation ?? ""}`;
  if (key !== lease.key) {
    const first = lease.key === null;
    lease.key = key;
    const own = state === "held" && holder.id === holderOf(context).id;
    if (!first && !own) {
      lease.epoch += 1;
      scheduleLeaseChange(context);
    }
  }
  return lease.view;
}

/** The bound session's status: "none" (no binding), "live", "stopped", "mismatch" or "missing". */
function refreshSession(context) {
  const lease = context.lease;
  const id = boundSessionId(context);
  if (!id) {
    lease.session = null;
    lease.sessionStatus = "none";
    return lease.sessionStatus;
  }
  if (lease.session?.id !== id) {
    const path = sessionRecordPath(env, id);
    lease.session = { id, cache: path ? createFileCache(path) : null };
  }
  const record = lease.session.cache ? lease.session.cache.get().value : null;
  lease.sessionStatus = sessionStatus(record, { platform: context.options.platform, target: context.serial });
  return lease.sessionStatus;
}

function sessionStoppedOf(context) {
  const status = context.lease.sessionStatus;
  return status === "stopped" || status === "mismatch";
}

/** Whether this Canvas holds the lease the file names now. */
function holdsLease(context) {
  const { held, view } = context.lease;
  return Boolean(held) && view.state === "held" && view.holder?.id === held.holder_id &&
    view.generation === held.generation;
}

/** The `lease` object of /status, state messages, POST /control and /api/devices (6.7). */
function leaseStatus(context) {
  const lease = context.lease;
  const view = refreshLease(context);
  return {
    state: view.state,
    holder: view.holder ? { ...view.holder } : null,
    generation: view.generation,
    expires_at: view.expires_at,
    mine: view.state === "held" && view.holder?.id === holderOf(context).id,
    took_over_from: lease.tookOverFrom ? { ...lease.tookOverFrom } : null,
    ended_by: lease.endedBy,
    idle_s: controlIdleMs(context) / 1000,
  };
}

function scheduleLeaseChange(context) {
  const lease = context.lease;
  if (lease.changeQueued) return;
  lease.changeQueued = true;
  queueMicrotask(() => {
    lease.changeQueued = false;
    onLeaseChange(context);
  });
}

/**
 * Another holder, a takeover by someone else, a release or an expiry: a lease this Canvas
 * held and no longer holds is lost (journaled, every client's held input lifted); clients
 * refused now let go of what they hold; every client gets the new state.
 */
function onLeaseChange(context) {
  if (context.shuttingDown) return;
  const lease = context.lease;
  if (lease.held && !holdsLease(context)) {
    endHeld(context, "lost");
    journalLeaseEnd(context, "lost");
    for (const client of context.controlClients) releaseInput(context, client);
  } else {
    for (const client of context.controlClients) {
      if (refusal(context, client.origin)) releaseInput(context, client);
    }
  }
  broadcastState(context);
}

/** This Canvas stops holding the lease (lost, released or idle): the local owner is shared again. */
function endHeld(context, reason) {
  const lease = context.lease;
  lease.held = null;
  lease.tookOverFrom = null;
  lease.endedBy = reason;
  lease.renewedAt = -Infinity;
  context.state.controlOwner = "shared";
}

/** The journal record of a lease this Canvas lost or released after its idle timeout. */
function journalLeaseEnd(context, mode) {
  journal(context, { origin: "human", journalWaiting: 0 }, { kind: "control", mode, owner: "human" });
}

/** A lease op through the bridge; answers the result or rejects with the bridge error. */
function leaseCall(context, op, payload) {
  return context.actionBridge.call(op, payload, "human");
}

function validGrant(result) {
  return Boolean(result) && result.acquired === true && Number.isSafeInteger(result.generation);
}

/** An acquire (or Take over) through the bridge; resolves null on a grant, else the failure. */
async function acquireLease(context, { takeover }) {
  const lease = context.lease;
  const holder = holderOf(context);
  const payload = { holder_id: holder.id, label: holder.label, kind: "human", ttl_s: humanLeaseTtl(), takeover };
  const sessionId = boundSessionId(context);
  if (sessionId) payload.session_id = sessionId;
  let result;
  try {
    result = await leaseCall(context, "control-acquire", payload);
  } catch (error) {
    const failure = {
      status: error.errorCode ? 409 : (error.statusCode ?? 502),
      code: error.errorCode ?? null,
      message: `Device control could not be taken: ${error.message}`,
    };
    context.state.lastError = `control: ${failure.message}`;
    return failure;
  }
  if (!validGrant(result)) {
    const failure = { status: 409, code: CONTROL_NOT_HELD, message: "Device control could not be taken: the bridge did not grant the lease" };
    context.state.lastError = `control: ${failure.message}`;
    return failure;
  }
  lease.held = { holder_id: holder.id, generation: result.generation };
  lease.endedBy = null;
  lease.renewedAt = performance.now();
  if (result.taken_over) lease.tookOverFrom = result.lease?.previous_holder ?? null;
  else if (!takeover) lease.tookOverFrom = null;
  lease.cache.reset();
  refreshLease(context);
  return null;
}

/**
 * The lease before a human input reaches the device (DEC-013): nothing to do while this
 * Canvas holds it; in Shared state one acquire, which concurrent inputs share and wait for
 * at most AUTO_ACQUIRE_WAIT_MS; never over a live holder. Resolves null when the input may
 * go on, else the failure {status, code, message}.
 */
async function ensureHumanLease(context) {
  const lease = context.lease;
  lease.lastHumanInputAt = performance.now();
  const view = refreshLease(context);
  if (holdsLease(context)) return null;
  const ours = holderOf(context).id;
  if (view.state === "held" && view.holder?.id !== ours) {
    return { status: 409, code: CONTROL_HELD, message: `Device control is held by ${describeHolder(view.holder, view.generation)}` };
  }
  if (!lease.acquiring) {
    const acquiring = acquireLease(context, { takeover: false }).then((failure) => {
      if (!failure) broadcastState(context);
      return failure;
    });
    lease.acquiring = acquiring;
    acquiring.finally(() => {
      if (lease.acquiring === acquiring) lease.acquiring = null;
    }).catch(() => {});
  }
  let timer = null;
  const timedOut = new Promise((resolvePromise) => {
    timer = setTimeout(() => resolvePromise({
      status: 409, code: CONTROL_NOT_HELD,
      message: `Device control could not be taken within ${AUTO_ACQUIRE_WAIT_MS} ms; the input was not sent`,
    }), AUTO_ACQUIRE_WAIT_MS);
    timer.unref?.();
  });
  const failure = await Promise.race([lease.acquiring, timedOut]);
  clearTimeout(timer);
  if (failure) {
    if (failure.code === CONTROL_NOT_HELD) context.state.lastError = `control: ${failure.message}`;
    return failure;
  }
  return null;
}

/** The HTTP error of a lease failure: 409 with its code, or the bridge's own status. */
function leaseError(failure) {
  const error = httpError(failure.status ?? 409, failure.message);
  if (failure.code && LEASE_ERROR_SET.has(failure.code)) error.errorCode = failure.code;
  return error;
}

/** A human HTTP input takes the lease first (6.4); other origins never do. */
async function admitHttpInput(context, origin) {
  if (origin !== "human") return;
  const failure = await ensureHumanLease(context);
  if (failure) throw leaseError(failure);
  context.lease.lastHumanHttpAt = performance.now();
}

/** The explicit Take over (human): a grant over any holder, or nothing changes. */
async function takeOverLease(context) {
  const failure = await acquireLease(context, { takeover: true });
  if (failure) throw failure.code ? leaseError(failure) : httpError(failure.status ?? 502, failure.message);
  const lease = context.lease;
  lease.epoch += 1;
  lease.lastHumanInputAt = performance.now();
}

/** Hand back (or the idle release): the lease is released; a failed release lets it expire. */
async function handBackLease(context, reason) {
  const lease = context.lease;
  const held = lease.held;
  endHeld(context, reason);
  lease.epoch += 1;
  if (!held) return;
  try {
    await leaseCall(context, "control-release", { holder_id: held.holder_id, generation: held.generation });
  } catch (error) {
    context.state.lastError = `control: the lease was not released (${error.message}); it expires on its own`;
  }
  lease.cache.reset();
  refreshLease(context);
}

/** At stop: a held lease is released unless the bridge is busy, waiting at most 500 ms. */
async function releaseLeaseOnClose(context) {
  const lease = context.lease;
  const held = lease.held;
  if (!held) return;
  lease.held = null;
  if (context.actionBridge.busy()) return;
  const releasing = leaseCall(context, "control-release", { holder_id: held.holder_id, generation: held.generation })
    .catch(() => {});
  await Promise.race([releasing, sleep(LEASE_RELEASE_WAIT_MS)]);
}

function humanPresent(context) {
  for (const client of context.controlClients) if (client.origin === "human") return true;
  return performance.now() - context.lease.lastHumanHttpAt < LEASE_HTTP_PRESENCE_MS;
}

/** Renew while a person is here and not idle; a refused renew means the lease is lost. */
async function renewLease(context) {
  const lease = context.lease;
  const held = lease.held;
  lease.renewing = true;
  lease.renewedAt = performance.now();
  try {
    await leaseCall(context, "control-renew", { holder_id: held.holder_id, generation: held.generation, ttl_s: humanLeaseTtl() });
  } catch (error) {
    if (lease.held === held && LEASE_ERROR_SET.has(error.errorCode)) {
      lease.cache.reset();
      refreshLease(context);
      if (lease.held === held) {
        endHeld(context, "lost");
        lease.epoch += 1;
        journalLeaseEnd(context, "lost");
        for (const client of context.controlClients) releaseInput(context, client);
        broadcastState(context);
      }
    } else {
      context.state.lastError = `control: the lease was not renewed (${error.message})`;
    }
  } finally {
    lease.renewing = false;
  }
}

/** The idle release (DEC-014): the lease goes back with a `control` record of mode idle. */
async function idleRelease(context) {
  const lease = context.lease;
  lease.releasing = handBackLease(context, "idle");
  journalLeaseEnd(context, "idle");
  for (const client of context.controlClients) {
    if (refusal(context, client.origin)) releaseInput(context, client);
  }
  broadcastState(context);
  try {
    await lease.releasing;
  } finally {
    lease.releasing = null;
    broadcastState(context);
  }
}

/** Every LEASE_POLL_MS while the device has clients or a held lease: changes, renew, idle. */
function leaseTick(context) {
  if (context.shuttingDown) return;
  const lease = context.lease;
  if (!lease.held && !context.videoClients.size && !context.controlClients.size) return;
  const before = context.lease.sessionStatus;
  refreshLease(context);
  if (refreshSession(context) !== before) broadcastState(context);
  if (!lease.held || lease.renewing || lease.busy || lease.acquiring || lease.releasing) return;
  const now = performance.now();
  if (now - lease.lastHumanInputAt >= controlIdleMs(context)) {
    idleRelease(context).catch(() => {});
    return;
  }
  const every = Math.min(LEASE_RENEW_MS, (humanLeaseTtl() * 1000) / 3);
  if (humanPresent(context) && now - lease.renewedAt >= every) renewLease(context).catch(() => {});
}

/**
 * The Canvas rule for one input (6.3): null when it may go on, else {message, code}. Paused
 * input and the local owner answer as before (no code); a live lease of another holder
 * (only for leased ops) and a bound session that stopped answer with their code.
 */
function refusal(context, origin, { leased = true } = {}) {
  if (context.state.inputPaused) return { message: "Canvas input is paused", code: null };
  if (context.lease) {
    const view = refreshLease(context);
    if (leased && view.state === "held" && view.holder?.id !== holderOf(context).id) {
      return { message: `Device control is held by ${describeHolder(view.holder, view.generation)}`, code: CONTROL_HELD };
    }
    const session = refreshSession(context);
    if (session === "stopped" || session === "mismatch") {
      const id = context.lease.session?.id;
      return session === "stopped"
        ? { message: `The Autonom session ${id} of this device has stopped`, code: "session_stopped" }
        : { message: `The Autonom session ${id} belongs to another device`, code: "session_target_mismatch" };
    }
  }
  if (context.state.controlOwner !== "shared" && context.state.controlOwner !== origin) {
    return { message: `Canvas control is owned by ${context.state.controlOwner}`, code: null };
  }
  return null;
}

/** An InputError for a refusal, carrying its code when it has one. */
function refusalError(refused, statusCode = null) {
  const error = statusCode === null ? new InputError(refused.message) : inputError(statusCode, refused.message);
  if (refused.code) error.errorCode = refused.code;
  return error;
}

/**
 * Whether the device path is ready for `message`: a display change goes through adb, needs
 * no session and exists on Android only; every other input needs the connected scrcpy or
 * idb session.
 */
function inputReachesDevice(context, message) {
  if (message.t === "display") return context.options.platform === "android";
  return Boolean(context.session?.connected);
}

/** Whether a WebSocket message starts human input that needs the lease (6.4). */
function startsInput(message) {
  switch (message.t) {
    case "touch":
    case "key":
      return message.a === "down";
    case "scroll":
    case "text":
    case "paste":
    case "display":
      return true;
    case "system":
      return message.op !== "keyframe";
    default:
      return false;
  }
}

function isInputMessage(message) {
  return ["touch", "key", "scroll", "text", "paste", "display", "system"].includes(message.t) &&
    !(message.t === "system" && message.op === "keyframe");
}

/**
 * A WebSocket message waits for a lease op (`work`, resolving null or a failure): it and the
 * messages read after it (held by inputPressure in arrival order) run once the op settled.
 */
function waitForLease(context, client, message, work) {
  const lease = context.lease;
  lease.waiting.push({ client, message, trigger: true });
  const busy = Promise.resolve().then(work).then(
    (failure) => failure ?? null,
    (error) => ({ status: error.statusCode ?? 502, code: error.errorCode ?? null, message: error.message }));
  lease.busy = busy;
  busy.then((failure) => {
    if (lease.busy === busy) lease.busy = null;
    const waiting = lease.waiting;
    lease.waiting = [];
    for (const item of waiting) {
      if (item.client.ws.readyState !== READY_STATE.OPEN) continue;
      if (!item.trigger) {
        // Read while the op ran by a path that does not wait: handled now, in order.
        runControlMessage(context, item.client, item.message);
        continue;
      }
      if (failure) {
        refuseDown(item.client, item.message);
        const forType = item.message.t === "system" && item.message.op === "rotate" ? "rotate" : item.message.t;
        const answer = { t: "error", message: failure.message, for: forType };
        if (failure.code) answer.code = failure.code;
        reply(context, item.client, answer);
        continue;
      }
      runControlMessage(context, item.client, item.message, { leaseDone: true });
    }
    resumeAllReading(context);
  });
}

/** A refused touch down answers once: the moves and the up of that pointer are dropped. */
function refuseDown(client, message) {
  if (message.t === "touch" && message.a === "down" && !client.pointers.has(message.id)) refusePointer(client, message.id);
}

/** A move or up of a pointer whose down was refused, and that is not down now: dropped. */
function dropsRefusedPointer(context, client, message) {
  if (message.t !== "touch" || message.a === "down" || !client.refused.has(message.id)) return false;
  const finger = context.iosFinger;
  const iosLive = Boolean(finger) && finger.client === client && !finger.wheel && iosFingerIds(finger).includes(message.id);
  if (client.pointers.has(message.id) || iosLive) return false;
  if (message.a !== "move") client.refused.delete(message.id);
  return true;
}


/**
 * The shared parser knows device input only; a display change is this server's own
 * message, and its preset is checked once its sender may change the display.
 */
function parseCanvasMessage(context, raw) {
  try {
    return parseControlMessage(raw, { platform: context.options.platform });
  } catch (error) {
    if (error.for !== "display") throw error;
    return { t: "display", preset: JSON.parse(raw).preset };
  }
}

function handleControlMessage(context, client, raw) {
  let message;
  try {
    message = parseCanvasMessage(context, raw);
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: error.for ?? null });
    return;
  }
  runControlMessage(context, client, message);
}

/**
 * One parsed control message. A person's first input on a device nobody holds, and a
 * person's Take over or Hand back, first wait for their lease op (waitForLease); the
 * message runs once it settled, `leaseDone`.
 */
function runControlMessage(context, client, message, { leaseDone = false } = {}) {
  const lease = context.lease;
  if (client.origin === "human" && isInputMessage(message)) lease.lastHumanInputAt = performance.now();
  if (!leaseDone && lease.busy) {
    lease.waiting.push({ client, message });
    return;
  }
  if (!leaseDone && client.origin === "human") {
    // Only input the device path would admit takes the lease: one refused because the stream
    // is not ready yet (requireSession) is answered below and leaves the lease alone (DEC-013).
    if (startsInput(message) && !holdsLease(context) && !refusal(context, client.origin) &&
      inputReachesDevice(context, message)) {
      waitForLease(context, client, message, () => ensureHumanLease(context));
      return;
    }
    if (message.t === "control" && (message.mode === "takeover" || (message.mode === "release" && lease.held))) {
      waitForLease(context, client, message, async () => {
        await applyControl(context, message.mode, client.origin);
        return null;
      });
      return;
    }
  }
  try {
    dispatchControl(context, client, message, { applied: leaseDone && message.t === "control" });
  } catch (error) {
    // Every refusal is answered on the connection; none may take the Canvas down.
    if (!(error instanceof InputError)) context.state.lastError = error.message;
    refuseDown(client, message);
    // A refused Rotate answers as one, so the page's Rotate button is enabled again.
    const answersFor = message.t === "system" && message.op === "rotate" ? "rotate" : message.t;
    const answer = { t: "error", message: error.message, for: answersFor };
    if (error.errorCode) answer.code = error.errorCode;
    reply(context, client, answer);
  }
}

function dispatchControl(context, client, message, { applied = false } = {}) {
  switch (message.t) {
    case "ping":
      reply(context, client, { t: "pong", ts: message.ts });
      return;
    case "control":
      // A person's Take over or Hand back was applied before (waitForLease).
      if (!applied) applyLocalControl(context, message.mode, client.origin);
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
    case "stream":
      // A viewing setting, not device input: no lease or handoff applies.
      setStreamChoice(context, client, message);
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
  // The refusal went with the down; that pointer's moves and up need no answer.
  if (dropsRefusedPointer(context, client, message)) return;
  // Handoff is checked per message, so a takeover stops a gesture already in progress.
  const refused = refusal(context, client.origin);
  if (refused) throw refusalError(refused);
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
  if (!isIosCanvas(context) && message.t === "system" && message.op === "rotate") {
    finishScroll(context, client);
    requestDeviceRotation(context, client).then(
      (result) => reply(context, client, { t: "rotation", ...result }),
      (error) => {
        // A refusal is answered on the connection only, as the synchronous ones are.
        if (!(error instanceof InputError)) context.state.lastError = error.message;
        reply(context, client, { t: "error", message: error.message, for: "rotate" });
      });
    return;
  }
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

/**
 * A Display menu choice (QW-04): kept on the context and applied by the server's stream
 * planner (Android: a scrcpy restart, at most one per RESTART_MIN_INTERVAL_MS; iOS: the
 * running companion stream in place). Answered with the choice and the settings the device
 * streams with once it is applied.
 */
function setStreamChoice(context, client, message) {
  const plan = context.server?.setStreamChoice;
  if (typeof plan !== "function") throw new InputError("This Canvas cannot change the stream settings.");
  const choice = { fps: message.fps, size: message.size };
  const applied = plan(context, choice);
  reply(context, client, { t: "stream", ok: true, choice, applied });
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
  deviceWrite(context, encodeSetClipboard({ sequence, paste: false, text }), sequence);
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
  // KEYCODE_PASTE is ignored by Flutter and other shortcut-based editors.
  // Send Ctrl+V only after SET_CLIPBOARD is acknowledged. Keep the chord in
  // one write, with both releases, before any input waiting behind this paste.
  if (context.session?.connected) context.session.send(Buffer.concat([
    encodeKeycode({ action: KEY_ACTION.DOWN, keycode: 113, metaState: 0x3000 }),
    encodeKeycode({ action: KEY_ACTION.DOWN, keycode: 50, metaState: 0x3000 }),
    encodeKeycode({ action: KEY_ACTION.UP, keycode: 50, metaState: 0x3000 }),
    encodeKeycode({ action: KEY_ACTION.UP, keycode: 113, metaState: 0 }),
  ]));
  // The app reads the clipboard asynchronously when it handles the shortcut.
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
    client.refused.delete(message.id);
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

/**
 * One manual rotation at a time, behind HTTP input/display changes. While it waits or
 * runs, inputPressure stops reading input, so a later Rotate runs after it; one already
 * read in the same burst is answered as busy, so no flood can grow the queue.
 */
function requestDeviceRotation(context, client) {
  if (context.rotationPending) return Promise.reject(inputError(409, "A device rotation is already in progress"));
  context.rotationPending = true;
  return context.enqueueInput(() => {
    const running = changeDeviceRotation(context, client);
    context.rotationRunning = running;
    return running.finally(() => {
      if (context.rotationRunning === running) context.rotationRunning = null;
    });
  }).finally(() => { context.rotationPending = false; resumeAllReading(context); });
}

/** Only the default display counts; dumpsys can list scrcpy's virtual display first. */
async function currentDeviceRotation(context) {
  const { stdout } = await runAdb(context, ["shell", "dumpsys", "window", "displays"], { timeout: WM_TIMEOUT_MS });
  const block = stdout.match(/(?:^|\n)\s*Display: mDisplayId=0\b([\s\S]*?)(?=\n\s*Display: mDisplayId=|$)/);
  const found = block?.[1].match(/^\s*mRotation=(?:ROTATION_)?(270|180|90|[0-3])\b/m);
  if (!found) throw new Error("WindowManager did not report the default display's rotation");
  const value = Number(found[1]);
  return value > 3 ? value / 90 : value;
}

/**
 * `wm user-rotation` and `wm fixed-to-user-rotation` exist from Android 12. An older `wm`
 * reports an unknown command: exit 255 with "Unknown command" on Android 10 and 11, exit 0
 * with only stderr on Android 9. enabled_if_no_auto_rotation exists on newer releases only.
 */
const UNKNOWN_WM_COMMAND = /\bunknown command\b/i;
const FIXED_TO_USER_ROTATION = Object.freeze(["default", "enabled", "disabled", "enabled_if_no_auto_rotation"]);

async function originalDeviceRotation(context) {
  let fixed;
  try {
    const answer = await runAdb(context, ["shell", "wm", "fixed-to-user-rotation"], { timeout: WM_TIMEOUT_MS });
    // Older Android has no per-display override; preserve its scrcpy control path.
    if (UNKNOWN_WM_COMMAND.test(String(answer.stderr) + String(answer.stdout))) return null;
    fixed = answer.stdout.trim();
  } catch (error) {
    if (UNKNOWN_WM_COMMAND.test(String(error.stderr) + String(error.stdout))) return null;
    throw error;
  }
  if (!FIXED_TO_USER_ROTATION.includes(fixed)) throw new Error("WindowManager returned an unknown rotation policy");
  const mode = (await runAdb(context, ["shell", "wm", "user-rotation"], { timeout: WM_TIMEOUT_MS })).stdout.trim();
  const locked = mode.match(/^lock ([0-3])$/);
  if (!locked && mode !== "free") throw new Error("WindowManager returned an unknown user rotation mode");
  const stored = (await runAdb(context,
    ["shell", "settings", "get", "system", "user_rotation"], { timeout: WM_TIMEOUT_MS })).stdout.trim();
  if (stored !== "null" && !/^[0-3]$/.test(stored)) throw new Error("Android did not report the stored user rotation");
  // A fresh Android uses rotation 0 when this setting has never been stored.
  const rotation = locked ? locked[1] : stored === "null" ? "0" : stored;
  return { fixed, mode: locked ? "lock" : "free", rotation, unset: stored === "null" };
}

/**
 * Keep manual rotation instead of scrcpy's freeze-then-thaw, which lets the sensor
 * snap it back. fixed-to-user-rotation also rotates displays held by a launcher/app.
 * Success is reported and journaled only when the default display reads back changed.
 */
async function changeDeviceRotation(context, client) {
  assertDisplayGoesOn(context, client.origin, client);
  liftForDisplayChange(context);
  await heldWritesSent(context);
  assertDisplayGoesOn(context, client.origin, client);
  // Only an attempt that wrote rolls back: a refused Rotate never changes the device.
  let wrote = false;
  try {
    if (!context.rotationWritten) {
      const original = await originalDeviceRotation(context);
      assertDisplayGoesOn(context, client.origin, client);
      if (!original) {
        systemAction(context, client, "rotate");
        return { legacy: true };
      }
      context.rotationOriginal = original;
    }
    const rotation = ((await currentDeviceRotation(context)) & 1) ^ 1;
    assertDisplayGoesOn(context, client.origin, client);
    context.rotationWritten = true;
    wrote = true;
    const abort = new AbortController();
    context.rotationAbort = abort;
    let failure;
    try {
      [failure] = await runWm(context, [
        ["user-rotation", "lock", String(rotation)],
        ["fixed-to-user-rotation", "enabled"],
      ], { change: true, signal: abort.signal });
    } finally {
      if (context.rotationAbort === abort) context.rotationAbort = null;
    }
    if (failure) throw new Error(failure);
    const deadline = performance.now() + WM_TIMEOUT_MS;
    do {
      if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
      if (await currentDeviceRotation(context) === rotation) {
        if (context.shuttingDown) throw inputError(503, "Canvas is stopping");
        context.hostRotation = rotation;
        journal(context, client, { kind: "system", op: "rotate" });
        return { rotation };
      }
      await sleep(100);
    } while (performance.now() < deadline);
    throw new Error("WindowManager accepted Rotate, but the default display did not rotate");
  } catch (error) {
    // A partial change cannot leave a failed Rotate's overrides behind; the original
    // settings found before the first Rotate are put back.
    if (wrote && !context.shuttingDown) await restoreRotationSettings(context);
    throw error;
  }
}

function rotationRestoreCommands(original) {
  return [
    ["user-rotation", "lock", original.rotation],
    ["fixed-to-user-rotation", original.fixed],
    ...(original.mode === "free" ? [["user-rotation", "free"]] : []),
  ];
}

/** Returns the settings it restored, or null when nothing was written or a command failed. */
async function restoreRotationSettings(context) {
  const original = context.rotationOriginal;
  if (!context.rotationWritten || !original) return null;
  const failures = await runWm(context, rotationRestoreCommands(original), { change: false });
  if (original.unset) {
    try {
      await runAdb(context, ["shell", "settings", "delete", "system", "user_rotation"], { timeout: WM_TIMEOUT_MS });
    } catch (error) { failures.push("restoring unset user_rotation failed: " + adbFailure(error)); }
  }
  if (failures.length) {
    context.state.lastError = "Rotation restore failed: " + failures.join("; ");
    console.log(context.state.lastError);
  } else {
    console.log(`Rotation restored: ${original.mode === "free" ? "free" : "lock " + original.rotation}, ` +
      `user_rotation ${original.unset ? "unset" : original.rotation}, fixed-to-user-rotation ${original.fixed}`);
    context.rotationWritten = false;
    context.rotationOriginal = null;
    return original;
  }
  return null;
}

/**
 * The running Rotate sees the stop at its next step and its pending `wm` write is killed;
 * the wait for it is bounded so the restore still fits the stop budget behind an adb call
 * that hangs. Remaining window: a killed write that the device applies only after the
 * read-back below is not put back.
 */
async function restoreDeviceRotation(context) {
  // A Rotate write still waiting on adb is killed first: it must not land after the restore.
  context.rotationAbort?.abort();
  const running = context.rotationRunning;
  if (running) await Promise.race([running.catch(() => {}), sleep(ROTATION_STOP_WAIT_MS)]);
  const original = await restoreRotationSettings(context);
  if (!original) return;
  // A killed adb client whose `wm` had already reached the device can still apply it: read
  // the settings back once and put them back again if they differ.
  try {
    const mode = (await runAdb(context, ["shell", "wm", "user-rotation"], { timeout: WM_TIMEOUT_MS })).stdout.trim();
    const fixed = (await runAdb(context, ["shell", "wm", "fixed-to-user-rotation"], { timeout: WM_TIMEOUT_MS })).stdout.trim();
    const expected = original.mode === "free" ? "free" : `lock ${original.rotation}`;
    if (mode === expected && fixed === original.fixed) return;
    console.log(`Rotation changed after its restore (${mode}, ${fixed}); restoring again`);
    const failures = await runWm(context, rotationRestoreCommands(original), { change: false });
    if (failures.length) {
      context.state.lastError = "Rotation restore failed: " + failures.join("; ");
      console.log(context.state.lastError);
    }
  } catch (error) {
    console.log(`Rotation restore read-back failed: ${adbFailure(error)}`);
  }
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

/**
 * Write one event to the session's HID stream; a write not taken in time stops holding the
 * queue. Such a late write still counts once the companion takes it: it happened on the
 * Simulator, so its action is journaled then (iosLateWrite), even after its record's turn.
 */
async function iosHidWrite(context, session, sent, action) {
  let timer = null;
  let late = false;
  const startedAt = performance.now();
  // Marked late in the timer itself, not after the race returns: a write that fails in the
  // same turn the timer fires must still see it, or its action would be held for good.
  const timeout = new Promise((resolvePromise) => {
    timer = setTimeout(() => {
      late = true;
      if (action) action.late = (action.late ?? 0) + 1;
      context.state.lastError = `idb hid: a write took more than ${IOS_HID_WRITE_MS} ms`;
      resolvePromise("timeout");
    }, IOS_HID_WRITE_MS);
    timer.unref?.();
  });
  // Only an event the companion took counts for its action's record.
  const written = session.hid(sent).then(() => {
    if (action) action.written += 1;
    if (!late) return;
    context.state.lastError = `idb hid: a write took ${Math.round(performance.now() - startedAt)} ms ` +
      `(more than ${IOS_HID_WRITE_MS} ms), then the companion took it`;
    iosLateWrite(context, action, true);
  }, (error) => {
    if (!late) throw error;
    context.state.lastError = `idb hid: ${error.message}`;
    iosLateWrite(context, action, false);
  });
  try {
    await Promise.race([written, timeout]);
  } catch (error) {
    context.state.lastError = `idb hid: ${error.message}`;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * A write that outlived IOS_HID_WRITE_MS settled. Its action's record, held back because no
 * event of the action was taken by its turn (runIosRecord), is journaled once one is taken,
 * and dropped once every late write failed.
 */
function iosLateWrite(context, action, taken) {
  if (!action) return;
  action.late -= 1;
  const deferred = action.deferred;
  if (!deferred || (!taken && action.late > 0)) return;
  action.deferred = null;
  if (taken) journalNow(context, deferred.client, deferred.record, deferred.onAnswered);
  else deferred.onAnswered?.();
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
    if (refused) throw refusalError(refused);
    await context.enqueueInput(() => {
      const late = refusal(context, client.origin);
      if (late) throw refusalError(late);
      return context.actionBridge.call("text", payload, client.origin);
    });
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: "text",
      ...(error.errorCode ? { code: error.errorCode } : {}) });
  }
}

/**
 * A record waits behind the input before it. An action none of whose events a live
 * companion took (refused by their turn, or dropped with a lost companion or a stopped
 * session) did not happen: its record is dropped. An action without events (a wheel burst
 * that did not move) is journaled unless refused by its turn. One whose writes are still
 * pending past IOS_HID_WRITE_MS waits for them (iosLateWrite).
 */
function runIosRecord(context, item) {
  const { client, record, onAnswered, action } = item;
  if (action && !action.written && action.late > 0) {
    // A write still pending past IOS_HID_WRITE_MS may yet reach the Simulator.
    action.deferred = { client, record, onAnswered };
    return;
  }
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
      refusePointer(client, id);
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
function refusePointer(client, id) {
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
  for (const other of finger.pinch.pointers.keys()) if (other !== message.id) refusePointer(client, other);
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
    if (refused) throw refusalError(refused);
    await context.enqueueInput(async () => {
      const late = refusal(context, client.origin);
      if (late) throw refusalError(late);
      await runSimctlClipboard(context, "pbcopy", text).catch((error) => {
        context.state.lastError = `clipboard: ${error.message}`;
        throw new InputError(`The Simulator clipboard could not be set: ${error.message}`);
      });
      if (!typed) return;
      const lateType = refusal(context, client.origin);
      if (lateType) {
        throw refusalError({ ...lateType,
          message: `The Simulator clipboard was set, but the text was not typed: ${lateType.message}` });
      }
      // The bridge types it and journals it as this paste, with its length only.
      await context.actionBridge.call("text", { text, sensitive: true, transport: "idb", paste: true }, client.origin);
    });
    if (!typed) journalNow(context, client, { kind: "paste", text_len: codePoints(text) });
    reply(context, client, typed ? { t: "paste", typed: true } : { t: "paste", typed: false, message: setOnly });
  } catch (error) {
    reply(context, client, { t: "error", message: error.message, for: "paste",
      ...(error.errorCode ? { code: error.errorCode } : {}) });
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

/**
 * Pause/resume/takeover/release for HTTP and WebSocket alike. A person's Take over first
 * takes the host lease over any holder (nothing changes when that fails); a person's
 * release while this Canvas holds the lease hands it back (6.4).
 */
async function applyControl(context, mode, origin) {
  if (origin === "human" && mode === "takeover") await takeOverLease(context);
  else if (origin === "human" && mode === "release" && context.lease.held) await handBackLease(context, "released");
  applyLocalControl(context, mode, origin);
}

/** The local owner rule of before (6.3 rule 5), and the lifting of input it refuses now. */
function applyLocalControl(context, mode, origin) {
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

function inputError(statusCode, message, code = null) {
  const error = new InputError(message);
  error.statusCode = statusCode;
  if (code) error.errorCode = code;
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
  if (error.errorCode) message.code = error.errorCode;
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
  const commands = (async () => {
    context.displayChrome ??= new DisplayChrome(
      (argv) => runAdb(context, argv, { timeout: WM_TIMEOUT_MS }),
      /^emulator-\d+$/.test(context.serial));
    try {
      await context.displayChrome.apply(preset.id, () => {
        if (context.shuttingDown) throw new Error("Canvas is stopping");
      });
      await context.hostDecorations?.refresh();
    } catch (error) {
      const failures = [`Display artwork: ${adbFailure(error)}`];
      try { await context.displayChrome.restore(); }
      catch (restoreError) { failures.push(restoreError.message); }
      return failures;
    }
    return runWm(context, presetCommands(preset), { change: true });
  })();
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
  if (refused) throw refusalError(refused, 409);
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
async function runWm(context, commands, { change, signal }) {
  const failures = [];
  for (const command of commands) {
    if (change && context.shuttingDown) break;
    try {
      await runAdb(context, ["shell", "wm", ...command], { timeout: WM_TIMEOUT_MS, signal });
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
    try { await context.displayChrome?.restore(); }
    catch (error) { failures.push(error.message); }
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
    bridge = createActionBridge(context.options, context.adbPath, context.serial,
      { binding: () => requestBinding(context) });
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
  if (!refused) return;
  // Paused input and the local owner answer as before; the lease and the session with a code.
  const error = httpError(409, refused.message);
  if (refused.code) error.errorCode = refused.code;
  throw error;
}

/** An HTTP input answer keeps its body of before: the bridge's lease attribution is left out (6.5). */
function inputAnswer(result) {
  if (!result || typeof result !== "object" || !("control" in result)) return result;
  const { control: _control, ...rest } = result;
  return rest;
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
  await admitHttpInput(context, origin);
  // Scaled when its turn comes, so after any display change queued before it.
  const result = await context.enqueueInput(() => {
    const [[tx, ty]] = onCurrentDisplay(context, sent, [[x, y]]);
    return context.actionBridge.call("tap", { x: tx, y: ty }, origin);
  });
  sendJson(response, 200, inputAnswer(result));
}

async function swipe(context, response, body, origin) {
  assertControl(context, origin);
  const x1 = normalizeCoordinate(body.x1, "x1");
  const y1 = normalizeCoordinate(body.y1, "y1");
  const x2 = normalizeCoordinate(body.x2, "x2");
  const y2 = normalizeCoordinate(body.y2, "y2");
  const duration = Math.min(5000, Math.max(1, normalizeCoordinate(body.duration ?? 250, "duration")));
  const sent = sentDisplaySize(body);
  await admitHttpInput(context, origin);
  const result = await context.enqueueInput(() => {
    const [[sx1, sy1], [sx2, sy2]] = onCurrentDisplay(context, sent, [[x1, y1], [x2, y2]]);
    return context.actionBridge.call("swipe", { x1: sx1, y1: sy1, x2: sx2, y2: sy2, duration }, origin);
  });
  sendJson(response, 200, inputAnswer(result));
}

async function key(context, response, body, origin) {
  assertControl(context, origin);
  const value = String(body.key ?? "");
  if (!isSafeKeyCode(value)) throw httpError(400, "Unsupported key code");
  await admitHttpInput(context, origin);
  const result = await context.enqueueInput(
    () => context.actionBridge.call("key", { key: value }, origin));
  sendJson(response, 200, inputAnswer(result));
}

async function text(context, response, body, origin) {
  assertControl(context, origin);
  try {
    encodeAdbText(String(body.text ?? ""));
  } catch (error) {
    throw httpError(400, error.message);
  }
  await admitHttpInput(context, origin);
  const result = await context.enqueueInput(() => context.actionBridge.call(
    "text", { text: String(body.text ?? ""), sensitive: Boolean(body.sensitive) }, origin));
  sendJson(response, 200, inputAnswer(result));
}

async function control(context, response, body, origin) {
  await applyControl(context, String(body.mode ?? ""), origin);
  sendJson(response, 200, { ok: true, control_owner: context.state.controlOwner,
    input_paused: context.state.inputPaused, lease: leaseStatus(context),
    session_stopped: sessionStoppedOf(context) });
}

async function setDisplay(context, response, body, origin) {
  assertDisplayPlatform(context);
  assertControl(context, origin);
  const preset = displayPreset(body?.preset);
  await admitHttpInput(context, origin);
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

/** The panel tools of this Canvas (canvas-tools-server.mjs), started with the Canvas. */
function createToolsFor(options, adbPath, serial, {
  deviceId = null, deviceName = null, commandLog = null, binding = null,
} = {}) {
  const childEnv = { ...env };
  if (options.idb) childEnv.AUTONOM_IDB = options.idb;
  return createCanvasTools({
    python: options.python ?? env.PYTHON ?? "python3",
    toolsPath: options.tools ?? null,
    autonomPath: options.autonom ?? null,
    platform: options.platform,
    target: serial,
    tool: adbPath,
    env: childEnv,
    installRoots: options.installRoots ?? [],
    capturesDir: options.capturesDir ?? null,
    deviceId,
    deviceName,
    commandLog,
    binding,
  });
}

/**
 * The action bridge of a workspace device: one that exits unexpectedly is started again
 * after 1 s, doubling to 30 s, reset once one ran 60 s; calls meanwhile reject 502 as for a
 * bridge that is gone. After 10 failed restarts `onFailed` marks the device failed.
 */
function createRestartingBridge(options, adbPath, serial, { onFailed = () => {}, binding = null } = {}) {
  let current = createActionBridge(options, adbPath, serial, { binding });
  let closed = false;
  let delay = BRIDGE_RESTART_MIN_MS;
  let failures = 0;
  let startedAt = performance.now();
  let timer = null;
  const watch = (bridge) => {
    bridge.child.once("exit", () => {
      if (closed || bridge !== current) return;
      if (performance.now() - startedAt >= BRIDGE_STABLE_MS) {
        delay = BRIDGE_RESTART_MIN_MS;
        failures = 0;
      }
      failures += 1;
      if (failures > BRIDGE_MAX_RESTARTS) {
        onFailed();
        return;
      }
      timer = setTimeout(() => {
        timer = null;
        if (closed) return;
        current = createActionBridge(options, adbPath, serial, { binding });
        startedAt = performance.now();
        watch(current);
      }, delay);
      timer.unref?.();
      delay = Math.min(delay * 2, BRIDGE_RESTART_MAX_MS);
    });
  };
  watch(current);
  return {
    call: (...args) => current.call(...args),
    busy: () => current.busy(),
    get child() { return current.child; },
    get restarts() { return failures; },
    close() {
      closed = true;
      clearTimeout(timer);
      current.close();
    },
  };
}

/**
 * The persistent action bridge of one device. `binding()` adds the request fields of the
 * device's session binding and lease claim (contract 6.5); a lease or session refusal
 * rejects with HTTP 409 and its `errorCode`, any other failure with 502 as before.
 */
function createActionBridge(options, adbPath, serial, { binding = null } = {}) {
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
    if (message.ok) {
      waiter.resolve(message.result);
      return;
    }
    const leaseRefusal = LEASE_ERROR_SET.has(message.error_code);
    const error = httpError(leaseRefusal ? 409 : 502, message.error ?? "Canvas action failed");
    if (leaseRefusal) error.errorCode = message.error_code;
    waiter.reject(error);
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
      const extra = binding ? binding() : {};
      return new Promise((resolvePromise, reject) => {
        pending.set(id, { op, resolve: resolvePromise, reject });
        child.stdin.write(`${JSON.stringify({ id, op, payload, origin, ...extra })}\n`);
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
    ...(options.signal ? { signal: options.signal } : {}),
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


export { IosFastSession, authorize, broadcastState, chooseTransport, control, ensureSession, exchangeToken, fallbackReason, findAdb, findExecutable, handleRequest, handleUpgrade, iosHidWrite, isCanvasWebSocket, key, leaseStatus, normalizeOrigin, printHelp, readJsonBody, refreshSession, refusal, requestUrl, resolveIdbCompanion, resolveScrcpy, serveUpgradeAsRequest, sessionStoppedOf, stopSession, swipe, tap, text };
