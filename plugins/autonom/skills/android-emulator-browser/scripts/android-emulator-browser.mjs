#!/usr/bin/env node
import { realpathSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { join, resolve } from "node:path";
import { env, exit, platform } from "node:process";
import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";
import { isAllowedHost, parseArgs } from "./browser-lib.mjs";
import { rejectUpgrade } from "./ws.mjs";
import {
  CanvasError,
  DeviceRegistry,
  FocusController,
  MAX_DEVICES,
  beginDisplayApply,
  deviceIdFor,
  devicePath,
  displayChoiceNeedsRestart,
  profileVideo,
  splitDevicePath,
} from "./canvas-devices.mjs";
import {
  MAX_TABS,
  MAX_TILES,
  TAB_ID_PATTERN,
  Workspace,
  acquireWorkspaceLock,
  stateBase,
  workspaceFile,
  workspaceLockFile,
  writeJsonAtomicSync,
} from "./canvas-workspace.mjs";
import { createSessionManager, runCliJson, spawnDetachedCli } from "./canvas-sessions.mjs";
import { renderWorkspacePage } from "./canvas-workspace-page.mjs";
import {
  actionsDock,
  actionsButton,
  actionsMarkup,
  actionsScript,
  actionsStyles,
} from "./canvas-actions-page.mjs";
import { createActivityApiRoute, createCommandLog } from "./canvas-activity.mjs";
import { StructuredError } from "./ios-idb-companion.mjs";
import {
  FRAME_DESTINATIONS,
  SHUTDOWN_SESSION_START_WAIT_MS,
  SHUTDOWN_STOP_MS,
  beginLog,
  deviceClosings,
  execFileAsync,
  logEvent,
  scriptString,
  sendHtml,
  sendJson,
  sleep,
} from "./canvas-runtime.mjs";
import {
  IosFastSession,
  authorize,
  broadcastState,
  chooseTransport,
  closeDeviceContext,
  createDeviceContext,
  createServerContext,
  ensureSession,
  exchangeToken,
  fallbackReason,
  findAdb,
  findExecutable,
  halfSizeBase,
  handleRequest,
  handleUpgrade,
  iosHidWrite,
  isCanvasWebSocket,
  leaseStatus,
  normalizeOrigin,
  printHelp,
  readJsonBody,
  refreshSession,
  refusal,
  requestUrl,
  resolveDeviceOptions,
  resolveIdbCompanion,
  resolveScrcpy,
  serveUpgradeAsRequest,
  sessionStoppedOf,
  stopSession,
} from "./canvas-device-server.mjs";
import { renderPage } from "./canvas-device-page.mjs";

export { renderPage, pageScript } from "./canvas-device-page.mjs";
export { IosFastSession, closeDeviceContext, createDeviceContext, createServerContext, handleRequest, handleUpgrade, iosHidWrite, resolveDeviceOptions };

/** Whether Node was started with this file, rather than another module importing it. */
function isMainModule() {
  const entry = process.argv[1];
  if (!entry) return false;
  if (pathToFileURL(resolve(entry)).href === import.meta.url) return true;
  // A script started through a symbolic link runs from its real path.
  try {
    return pathToFileURL(realpathSync(entry)).href === import.meta.url;
  } catch {
    return false;
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }
  await startCanvas(options, canvasHooks(options));
}

/**
 * The hooks main() starts the Canvas with (contract 6.2): the workspace page, the Actions
 * drawer on the device pages of a workspace, the activity and health routes and one command log.
 */
function canvasHooks(options) {
  let facts = null;
  // The server's own scrcpy, idb_companion and ffmpeg resolution for /api/health, found
  // once (it reads the file system and PATH only, never a device).
  const canvasFacts = () => {
    facts ??= (async () => {
      const [scrcpy, idb, ffmpeg] = await Promise.all([
        resolveScrcpy({ ...options, transport: "auto" }, false),
        platform === "darwin"
          ? resolveIdbCompanion({ ...options, transport: "auto" }, true)
          : { available: false, path: null, source: null, reason: "macOS only" },
        options.ffmpeg ? Promise.resolve(options.ffmpeg) : findExecutable("ffmpeg").catch(() => null),
      ]);
      return {
        scrcpy: {
          available: Boolean(scrcpy.available), path: scrcpy.serverPath ?? null, version: scrcpy.version ?? null,
          source: scrcpy.source ?? null, reason: scrcpy.reason ?? null,
        },
        idb_companion: {
          available: Boolean(idb.available), path: idb.path ?? null, source: idb.source ?? null, reason: idb.reason ?? null,
        },
        ffmpeg: ffmpeg ?? null,
      };
    })();
    return facts;
  };
  // Captures are available on single-device and workspace Canvases.
  const actionsPage = { dock: actionsDock, button: actionsButton, markup: actionsMarkup, styles: actionsStyles, script: actionsScript };
  return {
    renderWorkspacePage,
    pageExtensions: [actionsPage],
    apiRoutes: [createActivityApiRoute({ python: options.python ?? env.PYTHON ?? "python3", canvasFacts })],
    commandLog: createCommandLog(),
  };
}

// ---------------------------------------------------------------------------------------
// The Canvas server (contract 3): the single-device Canvas of before, or a workspace of
// tabs that attaches and detaches devices while it runs.

/** The command log a Canvas without one uses: every call is accepted and forgotten. */
export const NULL_COMMAND_LOG = Object.freeze({
  begin: () => Object.freeze({ end() {} }),
  list: () => ({ entries: [], next: 0, dropped: 0, total: 0 }),
  clear: () => 0,
});
// The device a request, upgrade, timer or child callback works for (crash isolation, 3.8).
export const deviceScope = new AsyncLocalStorage();
const DISCOVERY_SCHEMA = "autonom-canvas/v1";
const OFFLINE_POLL_MS = 2000;
const TARGET_LIST_MS = 5000;
const RESTORE_DELAYS_MS = Object.freeze([1000, 5000, 15_000]);
const RESTORE_CLOSE_MS = 3000;
const FAILURE_WINDOW_MS = 5 * 60_000;
const FAILURES_TO_STAY_FAILED = 3;
const BOOT_TIMEOUT_S = 180;
const BOOT_CLI_MS = (BOOT_TIMEOUT_S + 30) * 1000;
const BOOT_RUNNING_MAX = 2;
const BOOT_KEEP_MS = 10 * 60_000;
const BOOT_KEEP_MAX = 16;
// --discover-bootable: the machine's AVDs and shut-down Simulators, listed again after 30 s.
const BOOTABLE_DISCOVERY_MS = 30_000;
const BOOTABLE_DISCOVERY_CLI_MS = 40_000;
const BOOTABLE_RETRY_MS = 5_000;
const BOOTABLE_FIRST_WAIT_MS = 3_000;
const AVD_NAME_PATTERN = /^[A-Za-z0-9._-]{1,100}$/;
const SIMULATOR_UDID_PATTERN = /^[0-9A-Fa-f-]{36}$/;
const ROOT_ALIASES = new Set([
  "/status", "/frame", "/stream.mjpeg", "/stream.h264", "/tap", "/swipe", "/key", "/text", "/control", "/display",
]);
const TAB_PATH = /^\/c\/([^/]+)$/;
const DEVICE_TOOLS_HINT = "Start `autonom canvas` for a workspace that shows several devices.";

/**
 * Start a Canvas (main() with its parsed options). `hooks` adds the workspace page, page
 * extensions, API routes and the command log (contract 3.12). With `processHandlers` (the
 * default, as main() runs it) signals stop it and the process exits once it stopped; a
 * caller that passes false stops it with the returned `stop()`.
 */
export async function startCanvas(options, hooks = {}, { processHandlers = true, env: environment = env } = {}) {
  const resolvedHooks = {
    renderWorkspacePage: hooks.renderWorkspacePage ?? renderBuiltinWorkspacePage,
    pageExtensions: hooks.pageExtensions ?? [],
    apiRoutes: hooks.apiRoutes ?? [],
    commandLog: hooks.commandLog ?? NULL_COMMAND_LOG,
  };
  const workspaceMode = options.mode === "workspace";
  let legacyDevice = null;
  if (!workspaceMode) {
    legacyDevice = await resolveDeviceOptions(options, {
      platform: options.platform,
      target: options.target ?? options.serial,
    });
  }
  const server = createServerContext(options);
  server.mode = workspaceMode ? "workspace" : "single";
  server.hooks = resolvedHooks;
  server.commandLog = resolvedHooks.commandLog;
  server.env = environment;
  server.startedAt = new Date().toISOString();
  server.booted = new Map();
  server.boots = new Map();
  server.focus = null;
  server.workspace = null;
  server.lock = null;
  server.discoveryPath = null;
  server.stopping = null;
  server.profileName = (context) => profileNameOf(server, context.id);
  server.setStreamChoice = (context, choice) => setStreamChoice(server, context, choice);
  server.deviceStatus = (context) => deviceStatusOf(server, context);
  server.bridgeFailed = (id) => {
    const entry = server.registry?.get(id);
    if (!entry || entry.state === "detaching") return;
    server.registry.setState(id, "failed", { error: "The action bridge exited 10 times; reconnect the device" });
    logEvent(server, { device: id, kind: "device", name: "bridge", origin: "system", summary: "action bridge failed" },
      { ok: false, errorCode: "device_failed", error: "action bridge exhausted its restarts" });
  };
  server.registry = new DeviceRegistry({
    max: workspaceMode ? MAX_DEVICES : 1,
    create: (spec) => createWorkspaceDevice(server, spec),
    close: (context) => closeDeviceContext(context),
    onChange: () => scheduleDiscovery(server),
  });
  if (workspaceMode) {
    server.lock = acquireWorkspaceLock(workspaceLockFile(options.workspace, environment), { port: null });
    try {
      server.workspace = await Workspace.open({
        name: options.workspace,
        file: options.ephemeral ? null : workspaceFile(options.workspace, environment),
      });
    } catch (error) {
      server.lock.release();
      throw error;
    }
    server.focus = new FocusController({
      apply: (id, profile) => applyProfile(server, id, profile),
      busy: (id) => {
        const context = server.registry.get(id)?.context;
        return Boolean(context && (context.devicePointers.size || context.iosFinger));
      },
    });
    server.sessionManager = createSessionManager({
      python: options.python ?? environment.PYTHON ?? "python3",
      autonomPath: autonomScript(options),
      env: environment,
      port: () => server.port,
    });
  } else {
    const context = createDeviceContext(server, legacyDevice);
    server.legacyContext = context;
    // The single device has no focus plan; its Display menu choices are applied by one of
    // its own, so they restart the stream as rarely as a workspace device's (QW-04).
    server.streamPlanner = new FocusController({
      apply: (id, profile) => applyProfile(server, id, profile),
      busy: () => Boolean(context.devicePointers.size || context.iosFinger),
    });
    server.streamPlanner.add(context.id);
    // The registry entry of the one device (its create returns the context made above), so
    // /api reads and the /d/<id>/ aliases see it. A serial outside the id rule has no entry.
    try {
      await server.registry.attach({ platform: context.options.platform, target: context.serial, name: context.serial });
    } catch {}
  }

  const onRequest = (request, response) => {
    dispatchRequest(server, request, response).catch((error) => {
      if (!workspaceMode && server.legacyContext) server.legacyContext.state.lastError = error.message;
      respondError(response, error, { api: isApiPath(request) });
    });
  };
  const httpServer = createServer({ shouldUpgradeCallback: isCanvasWebSocket }, onRequest);
  server.httpServer = httpServer;
  httpServer.on("upgrade", (request, socket, head) => {
    try {
      if (isCanvasWebSocket(request)) dispatchUpgrade(server, request, socket, head);
      else serveUpgradeAsRequest(request, socket, onRequest);
    } catch (error) {
      if (server.legacyContext) server.legacyContext.state.lastError = error.message;
      socket.destroy();
    }
  });

  const listening = new Promise((resolvePromise, reject) => {
    if (workspaceMode) {
      httpServer.once("error", (error) => {
        server.lock?.release();
        if (error?.code === "EADDRINUSE") {
          reject(new StructuredError("port_unavailable", `Port ${options.port} is already in use`, {
            hint: "Pass --port 0 or another port", port: options.port,
          }));
        } else {
          reject(error);
        }
      });
    }
    httpServer.listen(options.port, "127.0.0.1", () => {
      const address = httpServer.address();
      server.port = typeof address === "object" && address ? address.port : options.port;
      resolvePromise();
    });
  });
  await listening;
  const token = server.token;
  const fragment = token ? `#token=${encodeURIComponent(token)}` : "";
  const pageUrl = `http://127.0.0.1:${server.port}/${fragment}`;
  if (workspaceMode) {
    console.log(`autonom Canvas workspace ${options.workspace} ready`);
    console.log(`Transport preference: ${options.transport}`);
  } else {
    const context = server.legacyContext;
    console.log(`autonom Canvas ready for ${context.options.platform}:${context.serial}`);
    console.log(`Transport preference: ${options.transport}`);
    console.log(`Transport: ${chooseTransport(context)}`);
    const reason = fallbackReason(context);
    if (reason) console.log(`Fallback reason: ${reason}`);
  }
  console.log(`Preview at ${pageUrl}`);
  console.log(`Open this exact URL in your agent's visible side-panel browser: ${pageUrl}`);
  if (!token) console.warn("WARNING: authentication is disabled");

  server.discoveryPath = join(stateBase(environment), "canvas", `${server.port}.json`);
  server.lock?.update(server.port);
  writeDiscovery(server);

  const stop = () => {
    server.stopping ??= stopServer(server).then(() => {
      // A caller without process handlers (a test, an embedding) gets the port back too.
      if (processHandlers) return;
      httpServer.close();
      httpServer.closeAllConnections?.();
    });
    return server.stopping;
  };
  server.stop = stop;
  const onUncaught = (error) => handleUncaught(server, error);
  if (processHandlers) {
    const shutdown = () => {
      if (server.exiting) return;
      server.exiting = true;
      stop().finally(() => {
        httpServer.close(() => exit(0));
        httpServer.closeAllConnections?.();
        setTimeout(() => exit(0), 1000).unref();
      });
    };
    server.exitAfterStop = shutdown;
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signal, shutdown);
    if (workspaceMode) {
      process.on("uncaughtException", onUncaught);
      process.on("unhandledRejection", onUncaught);
    }
  }
  if (workspaceMode) {
    server.workspace.onChange = () => scheduleDiscovery(server);
    scheduleDiscovery(server);
    restoreWorkspace(server).catch((error) => console.error(`Canvas workspace restore failed: ${error.message}`));
    server.pollTimer = setInterval(() => pollTargets(server), OFFLINE_POLL_MS);
    server.pollTimer.unref?.();
  }
  return { server, port: server.port, url: pageUrl, stop, onUncaught };
}

function autonomScript(options) {
  return options.autonom ?? resolve(import.meta.dirname, "../../../../../scripts/autonom.py");
}

function pythonOf(server) {
  return server.options.python ?? server.env.PYTHON ?? "python3";
}

/** Stop the Canvas (contract 3.10): state saved, devices closed, own sessions ended, files removed. */
async function stopServer(server) {
  server.shuttingDown = true;
  clearInterval(server.pollTimer);
  if (server.mode !== "workspace") {
    removeDiscovery(server);
    await closeDeviceContext(server.legacyContext);
    return;
  }
  try {
    await server.workspace.flush();
  } catch (error) {
    console.error(`Canvas workspace file could not be saved: ${error.message}`);
  }
  server.workspace.close();
  const entries = server.registry.list();
  await Promise.race([
    Promise.all(entries.map((entry) => (entry.context ? closeDeviceContext(entry.context) : null))),
    sleep(SHUTDOWN_STOP_MS + 500),
  ]);
  // Session starts still running end their own session once they answer (ensureSessionFor
  // sees shuttingDown); wait for them, bounded, so none outlives this Canvas.
  const pending = [...(server.pendingSessionStarts ?? [])];
  if (pending.length) {
    await Promise.race([Promise.allSettled(pending), sleep(SHUTDOWN_SESSION_START_WAIT_MS)]);
  }
  server.sessionManager.releaseDetached(entries.map((entry) => entry.session));
  if (server.options.shutdownBooted) {
    for (const entry of entries) {
      if (!entry.bootedByCanvas) continue;
      console.error(`Shutting down ${entry.id} (booted by this Canvas)`);
      const targetArgs = entry.platform === "ios"
        ? ["--platform", "ios", "--udid", entry.target] : ["--serial", entry.target];
      spawnDetachedCli([pythonOf(server), autonomScript(server.options), ...targetArgs, "devices", "shutdown"],
        { env: server.env });
    }
  }
  removeDiscovery(server);
  server.lock?.release();
}

// ----- discovery file -------------------------------------------------------------------

function discoveryDocument(server) {
  const workspace = server.workspace;
  const tabs = workspace
    ? workspace.tabs().map((tab) => ({
      id: tab.id, name: tab.name, devices: tab.slots.filter(Boolean).map((slot) => slot.device),
    }))
    : [];
  return {
    schema: DISCOVERY_SCHEMA,
    pid: process.pid,
    port: server.port,
    url: `http://127.0.0.1:${server.port}/`,
    token: server.token || null,
    started_at: server.startedAt,
    mode: server.mode,
    workspace: workspace?.name ?? null,
    tabs,
    devices: server.registry.list().map((entry) => ({
      id: entry.id, platform: entry.platform, target: entry.target, tab: workspace?.locate(entry.id)?.tab ?? null,
    })),
    booted: [...server.booted.values()].map(({ platform, target, bootedAt }) => ({ platform, target, booted_at: bootedAt })),
  };
}

function writeDiscovery(server) {
  if (!server.discoveryPath || server.discoveryRemoved) return;
  try {
    writeJsonAtomicSync(server.discoveryPath, discoveryDocument(server));
  } catch (error) {
    console.error(`Canvas discovery file could not be written: ${error.message}`);
  }
}

function scheduleDiscovery(server) {
  if (!server.discoveryPath || server.discoveryQueued) return;
  server.discoveryQueued = true;
  setImmediate(() => {
    server.discoveryQueued = false;
    writeDiscovery(server);
  });
}

function removeDiscovery(server) {
  server.discoveryRemoved = true;
  if (server.discoveryPath) rmSync(server.discoveryPath, { force: true });
}

// ----- request dispatch -----------------------------------------------------------------

/** API routes answer errors as {ok, error, error_code, hint}; the routes of before as {error}. */
function isApiPath(request) {
  const path = requestUrl(request)?.pathname ?? "";
  return path === "/api" || path.startsWith("/api/");
}

function apiError(response, status, code, message, hint = null, extra = {}) {
  sendJson(response, status, { ok: false, error: message, error_code: code, hint, ...extra });
}

function respondError(response, error, { api = true } = {}) {
  if (response.headersSent) {
    response.destroy(error);
    return;
  }
  if (error instanceof CanvasError) {
    sendJson(response, error.status, error.body());
    return;
  }
  const status = error?.statusCode ?? 500;
  if (!api) {
    // A lease or session refusal names its code (contract 6.3); other answers are as before.
    if (error?.errorCode) {
      sendJson(response, status, { ok: false, error: error.message, error_code: error.errorCode });
      return;
    }
    sendJson(response, status, { error: error?.message ?? String(error) });
    return;
  }
  const code = status === 400 || status === 413 ? "invalid_value" : status === 502 ? "backend_failed" : "internal_error";
  apiError(response, status, code, error?.message ?? String(error));
}

/** Host, stop and authentication checks of a new route; answers and returns null when refused. */
function admit(server, request, response, url, { auth = true } = {}) {
  if (!isAllowedHost(request.headers.host, server.port)) {
    apiError(response, 403, "host_not_allowed", "Host not allowed");
    return null;
  }
  if (server.shuttingDown) {
    apiError(response, 503, "canvas_stopping", "Canvas is stopping");
    return null;
  }
  if (!auth) return { origin: "human" };
  const authorization = authorize(server, request, url);
  if (!authorization.ok) {
    apiError(response, 401, "unauthorized", "Unauthorized", "Open the Canvas URL with its #token, or send the token.");
    return null;
  }
  if (request.method === "POST" && authorization.csrf && request.headers["x-autonom-csrf"] !== authorization.csrf) {
    apiError(response, 403, "csrf_rejected", "CSRF token rejected");
    return null;
  }
  return { origin: normalizeOrigin(request.headers["x-autonom-origin"]) };
}

async function dispatchRequest(server, request, response) {
  const url = requestUrl(request);
  const path = url?.pathname ?? "";
  if (url && (path === "/api" || path.startsWith("/api/"))) {
    await serveApi(server, request, response, url);
    return;
  }
  if (url && path.startsWith("/d/")) {
    await serveDeviceRoute(server, request, response, url);
    return;
  }
  if (server.mode !== "workspace") {
    await handleRequest(server.legacyContext, request, response);
    return;
  }
  if (!url) {
    apiError(response, 400, "invalid_value", "Bad request target");
    return;
  }
  if (request.method === "POST" && path === "/auth") {
    if (!admit(server, request, response, url, { auth: false })) return;
    await exchangeToken(server, request, response);
    return;
  }
  if (request.method === "GET" && (path === "/" || TAB_PATH.test(path))) {
    serveWorkspacePage(server, request, response, path);
    return;
  }
  if (ROOT_ALIASES.has(path) || path.startsWith("/tools/")) {
    const entry = server.registry.primary();
    if (!entry) {
      if (!admit(server, request, response, url)) return;
      apiError(response, 409, "no_device", "No device is attached to this Canvas", "Attach a device first.");
      return;
    }
    await serveDevice(server, entry, request, response, url, path);
    return;
  }
  if (!admit(server, request, response, url)) return;
  apiError(response, 404, "not_found", "Not found");
}

function serveWorkspacePage(server, request, response, path) {
  const url = requestUrl(request);
  if (!admit(server, request, response, url, { auth: false })) return;
  if (FRAME_DESTINATIONS.has(request.headers["sec-fetch-dest"])) {
    apiError(response, 403, "framed", "The Canvas page cannot be framed");
    return;
  }
  const match = TAB_PATH.exec(path);
  let initial = server.workspace.activeTab()?.id ?? null;
  if (match) {
    let id;
    try { id = decodeURIComponent(match[1]); } catch { id = ""; }
    if (!TAB_ID_PATTERN.test(id)) {
      apiError(response, 404, "tab_not_found", "Not a Canvas tab URL");
      return;
    }
    initial = id;
  }
  sendHtml(response, server.hooks.renderWorkspacePage({
    title: "Autonom Canvas",
    workspace: server.workspace.name,
    initial_tab: initial,
    limits: { max_tabs: MAX_TABS, max_tiles: MAX_TILES, max_devices: MAX_DEVICES },
    platforms: ["android", "ios"],
  }));
}

/** A route under /d/<id>: the device page, its status, or a root route of that device. */
async function serveDeviceRoute(server, request, response, url) {
  const split = splitDevicePath(url.pathname);
  if (!split) {
    if (!admit(server, request, response, url)) return;
    apiError(response, 404, "device_not_found", "No such device");
    return;
  }
  if (!isAllowedHost(request.headers.host, server.port)) {
    apiError(response, 403, "host_not_allowed", "Host not allowed");
    return;
  }
  const entry = server.registry.get(split.id);
  if (!entry) {
    if (!admit(server, request, response, url)) return;
    apiError(response, 404, "device_not_found", `No device ${split.id} is attached`);
    return;
  }
  if (split.rest === null) {
    if (!admit(server, request, response, url, { auth: false })) return;
    response.writeHead(308, { Location: `${devicePath(entry.id)}/${url.search}`, "Content-Length": 0 });
    response.end();
    return;
  }
  if (split.rest === "/" && request.method === "GET") {
    if (!admit(server, request, response, url, { auth: false })) return;
    const site = request.headers["sec-fetch-site"];
    if (FRAME_DESTINATIONS.has(request.headers["sec-fetch-dest"]) && site !== undefined && site !== "same-origin") {
      apiError(response, 403, "framed", "A device page can be framed only by this Canvas");
      return;
    }
    if (!entry.context) {
      apiError(response, 503, "device_not_ready", `Device ${entry.id} is ${entry.state}`, null, { state: entry.state });
      return;
    }
    const embed = url.searchParams.get("embed") === "1";
    sendHtml(response, renderPage(entry.context, { base: devicePath(entry.id), embed }), { frameable: true });
    return;
  }
  await serveDevice(server, entry, request, response, url, split.rest);
}

/** One root route (status, frame, streams, input, tools) for one device. */
async function serveDevice(server, entry, request, response, url, rest) {
  if (entry.state !== "live" || !entry.context) {
    if (!admit(server, request, response, url)) return;
    if (rest === "/status" && request.method === "GET") {
      sendJson(response, 200, {
        platform: entry.platform, serial: entry.target, transport: null,
        device: deviceStatusFor(server, entry),
      });
      return;
    }
    apiError(response, 503, "device_not_ready", `Device ${entry.id} is ${entry.state}`, null, { state: entry.state });
    return;
  }
  request.url = rest + url.search;
  const context = entry.context;
  try {
    await deviceScope.run({ id: entry.id }, () => handleRequest(context, request, response));
  } catch (error) {
    context.state.lastError = error.message;
    throw error;
  }
}

function dispatchUpgrade(server, request, socket, head) {
  socket.on("error", () => {});
  const url = requestUrl(request);
  const path = url?.pathname ?? "";
  if (!isAllowedHost(request.headers.host, server.port)) {
    rejectUpgrade(socket, 403, "Host not allowed");
    return;
  }
  if (server.shuttingDown) {
    rejectUpgrade(socket, 503, "Canvas is stopping");
    return;
  }
  let entry;
  let rest = path;
  if (path.startsWith("/d/")) {
    const split = splitDevicePath(path);
    entry = split ? server.registry.get(split.id) : null;
    rest = split?.rest ?? "";
    if (!entry) {
      rejectUpgrade(socket, 404, "No such device");
      return;
    }
  } else if (server.mode !== "workspace") {
    handleUpgrade(server.legacyContext, request, socket, head);
    return;
  } else {
    entry = server.registry.primary();
    if (!entry) {
      rejectUpgrade(socket, 409, "No device is attached to this Canvas");
      return;
    }
  }
  if (entry.state !== "live" || !entry.context) {
    rejectUpgrade(socket, 503, `Device ${entry.id} is ${entry.state}`);
    return;
  }
  request.url = rest + (url?.search ?? "");
  deviceScope.run({ id: entry.id }, () => handleUpgrade(entry.context, request, socket, head));
}

// ----- API --------------------------------------------------------------------------------

async function readApiBody(request) {
  const body = await readJsonBody(request);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new CanvasError(400, "invalid_value", "The body must be a JSON object");
  }
  return body;
}

function requireWorkspace(server) {
  if (server.mode !== "workspace") {
    throw new CanvasError(409, "single_device_canvas", "This Canvas shows one device", DEVICE_TOOLS_HINT);
  }
}

function optionalString(body, key) {
  const value = body[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") throw new CanvasError(400, "invalid_value", `${key} must be a string`);
  return value;
}

function optionalSlot(body) {
  const value = body.slot;
  if (value === undefined || value === null) return undefined;
  if (!Number.isInteger(value) || value < 0 || value >= MAX_TILES) {
    throw new CanvasError(400, "invalid_value", `slot must be an integer from 0 to ${MAX_TILES - 1}`);
  }
  return value;
}

async function serveApi(server, request, response, url) {
  const admitted = admit(server, request, response, url);
  if (!admitted) return;
  const { origin } = admitted;
  const method = request.method;
  const parts = url.pathname.split("/").slice(2).map((part) => {
    try { return decodeURIComponent(part); } catch { return "\u0000"; }
  });
  const route = (verb, ...pattern) => method === verb && parts.length === pattern.length &&
    pattern.every((piece, index) => piece === "*" || piece === parts[index]);
  try {
    if (route("GET", "workspace")) return sendJson(response, 200, workspaceSummary(server));
    if (route("GET", "devices")) {
      return sendJson(response, 200, {
        ok: true, primary: server.registry.primary()?.id ?? null, focus: server.focus?.focus ?? null,
        devices: server.registry.list().map((entry) => deviceSummary(server, entry)),
      });
    }
    if (route("GET", "targets")) return sendJson(response, 200, { ok: true, ...(await targetsSummary(server)) });
    if (route("POST", "stop")) {
      sendJson(response, 202, { ok: true, stopping: true });
      setImmediate(() => (server.exitAfterStop ? server.exitAfterStop() : server.stop()));
      return;
    }
    if (route("POST", "tabs")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      const tab = server.workspace.createTab({ name: optionalString(body, "name"), layout: body.layout ?? 1 });
      logEvent(server, { kind: "workspace", name: "tab.create", origin, summary: `tab ${tab.id}` }, { ok: true });
      return sendJson(response, 201, { ok: true, tab });
    }
    if (route("POST", "tabs", "*")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      const tab = server.workspace.updateTab(parts[1], {
        name: optionalString(body, "name"), layout: body.layout ?? undefined,
      });
      logEvent(server, { kind: "workspace", name: "tab.update", origin, summary: `tab ${tab.id}` }, { ok: true });
      return sendJson(response, 200, { ok: true, tab });
    }
    if (route("POST", "tabs", "*", "activate")) {
      requireWorkspace(server);
      const tab = server.workspace.activate(parts[1]);
      return sendJson(response, 200, { ok: true, active_tab: tab.id });
    }
    if (route("POST", "tabs", "*", "close")) {
      requireWorkspace(server);
      return sendJson(response, 200, await closeTab(server, parts[1], origin));
    }
    if (route("POST", "tabs", "*", "slots", "*", "clear")) {
      requireWorkspace(server);
      const slot = /^\d$/.test(parts[3]) ? Number(parts[3]) : -1;
      const tab = server.workspace.clearSlot(parts[1], slot);
      return sendJson(response, 200, { ok: true, tab });
    }
    if (route("POST", "devices")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      const platform = optionalString(body, "platform");
      const target = optionalString(body, "target");
      if (!platform || !target) throw new CanvasError(400, "invalid_value", "platform and target are required");
      // No tab named: the active tab, grown to the next layout when full (Workspace.roomFor).
      const { entry, attached } = await attachDevice(server, {
        platform, target, tab: optionalString(body, "tab"), slot: optionalSlot(body), origin, autoPlace: true,
      });
      return sendJson(response, attached ? 201 : 200, { ok: true, device: deviceSummary(server, entry), attached });
    }
    if (route("POST", "devices", "*", "move")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      return sendJson(response, 200, moveDevice(server, parts[1], {
        tab: optionalString(body, "tab"), slot: optionalSlot(body), origin,
      }));
    }
    if (route("POST", "devices", "*", "detach")) {
      requireWorkspace(server);
      return sendJson(response, 200, await detachDevice(server, parts[1], { origin }));
    }
    if (route("POST", "devices", "*", "reconnect")) {
      requireWorkspace(server);
      return sendJson(response, 202, await reconnectDevice(server, parts[1], { origin }));
    }
    if (route("POST", "boot")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      await refreshBootable(server);
      const { status, job } = startBoot(server, body, origin);
      return sendJson(response, status, { ok: true, job });
    }
    if (route("GET", "boot", "*")) {
      requireWorkspace(server);
      const job = server.boots.get(parts[1]);
      if (!job) throw new CanvasError(404, "job_not_found", `No boot job ${parts[1]}`);
      return sendJson(response, 200, { ok: true, job: jobView(job) });
    }
    if (route("POST", "focus")) {
      requireWorkspace(server);
      const body = await readApiBody(request);
      const id = body.id ?? null;
      if (id !== null && typeof id !== "string") throw new CanvasError(400, "invalid_value", "id must be a device id or null");
      if (id !== null && !server.registry.get(id)) throw new CanvasError(404, "device_not_found", `No device ${id} is attached`);
      const focus = server.focus.setFocus(id, server.registry.list().map((entry) => entry.id));
      for (const entry of server.registry.list()) if (entry.context) broadcastState(entry.context);
      return sendJson(response, 200, { ok: true, focus });
    }
    for (const handler of server.hooks.apiRoutes) {
      const answer = await handler({
        method, url, origin, request, response, readBody: () => readJsonBody(request),
        registry: server.registry, workspace: server.workspace, server, commandLog: server.commandLog,
      });
      if (!answer) continue;
      if (answer.handled) return;
      return sendJson(response, answer.status, answer.body);
    }
    apiError(response, 404, "not_found", "Not found");
  } catch (error) {
    respondError(response, error);
  }
}

function workspaceSummary(server) {
  const limits = { max_tabs: MAX_TABS, max_tiles: MAX_TILES, max_devices: MAX_DEVICES };
  const devices = server.registry.list().map((entry) => deviceSummary(server, entry));
  if (server.mode !== "workspace") {
    return {
      ok: true, mode: "single", name: null, revision: 0, persisted: false, active_tab: null, tabs: [],
      devices, absent: [], limits,
    };
  }
  const workspace = server.workspace;
  return {
    ok: true, mode: "workspace", name: workspace.name, revision: workspace.revision, persisted: workspace.persisted,
    active_tab: workspace.activeTab()?.id ?? null, tabs: workspace.tabs(), devices,
    absent: absentRefs(server), limits,
  };
}

function absentRefs(server) {
  return server.workspace.refs().filter((ref) => !server.registry.get(ref.id)).map((ref) => ({
    id: ref.id, platform: ref.platform, target: ref.target, name: ref.name ?? ref.target, tab: ref.tab, slot: ref.slot,
    bootable: bootableFor(server, ref),
  }));
}

function profileNameOf(server, id) {
  return server.focus ? server.focus.profileOf(id) : "focused";
}

function profileOf(server, entry) {
  const focused = profileNameOf(server, entry.id) === "focused";
  return entry.context ? streamProfile(server, entry.context, focused)
    : profileVideo({ platform: entry.platform, focused, options: server.options });
}

/**
 * The stream profile of a device context: its focus profile combined with its Display menu
 * choice (QW-05); with no choice exactly the focus profile.
 */
function streamProfile(server, context, focused, choice = context.displayChoice ?? null) {
  return profileVideo({
    platform: context.options.platform, focused, options: server.options, choice,
    baseMaxSize: choice?.size === "half" ? halfSizeBase(context) : null,
  });
}

/** The context of device `id`: its registry entry's, or the single-device Canvas's own. */
function contextOf(server, id) {
  return server.registry.get(id)?.context ?? (server.legacyContext?.id === id ? server.legacyContext : null);
}

/**
 * A Display menu choice for a device (QW-04): kept on its context, and when it changed, the
 * stream is restarted with it through the focus plan (or the single device's own plan), which
 * allows one restart per RESTART_MIN_INTERVAL_MS. An unchanged choice (sent again when a page
 * reconnects) restarts nothing, unless the last restart did not apply it and none runs now:
 * then it is tried once more. Answers the settings the stream gets.
 */
function setStreamChoice(server, context, choice) {
  const previous = context.displayChoice;
  const next = { fps: choice.fps, size: choice.size };
  const restart = displayChoiceNeedsRestart(context, next);
  context.displayChoice = next;
  if (restart) {
    const planner = server.focus ?? server.streamPlanner;
    if (!planner?.refresh(context.id)) {
      context.displayChoice = previous;
      throw new Error("The device's stream cannot be changed now.");
    }
  }
  const profile = streamProfile(server, context, profileNameOf(server, context.id) === "focused");
  const ios = context.options.platform === "ios";
  return {
    fps: profile.fps,
    max_size: ios ? null : Number.isFinite(profile.maxSize) ? profile.maxSize : null,
    scale_factor: ios ? profile.scaleFactor : null,
  };
}

/**
 * The frame rate a device streams with under `profile`: the profile's (what the companion or
 * scrcpy is sent, 0 for frames on change) on a fast transport; on multipart --fps, capped by
 * the profile's when that is above 0 (frames on change caps nothing there, as multipartFps).
 */
function fpsCapOf(context, profile, multipart) {
  if (!multipart || !context) return profile.fps;
  return profile.fps > 0 ? Math.min(context.options.fps, profile.fps) : context.options.fps;
}

/**
 * The profile the device's stream really runs (`FocusController.appliedOf`), with the fps cap
 * and size it brings: null outside a workspace or before the device joined the focus plan.
 */
function appliedProfileOf(server, entry, multipart) {
  const applied = server.focus?.appliedOf(entry.id) ?? null;
  if (!applied) return null;
  const profile = entry.context
    ? streamProfile(server, entry.context, applied.profile === "focused", entry.context.displayApplied ?? null)
    : profileVideo({ platform: entry.platform, focused: applied.profile === "focused", options: server.options });
  const context = entry.context;
  return {
    profile: profile.profile,
    fps_cap: fpsCapOf(context, profile, multipart),
    max_size: entry.platform === "android" && Number.isFinite(profile.maxSize) ? profile.maxSize : null,
    scale_factor: entry.platform === "ios" ? profile.scaleFactor : null,
    applied_at: applied.applied_at,
  };
}

function deviceSummary(server, entry) {
  const where = server.workspace?.locate(entry.id) ?? null;
  const context = entry.context;
  const profile = profileOf(server, entry);
  const transport = context ? chooseTransport(context) : null;
  const multipart = transport === "screencap" || transport === "screenrecord";
  const link = entry.session;
  return {
    id: entry.id,
    platform: entry.platform,
    target: entry.target,
    name: entry.name ?? entry.target,
    state: entry.state,
    tab: where?.tab ?? null,
    slot: where?.slot ?? null,
    primary: server.registry.primary() === entry,
    focused: server.focus?.focus === entry.id,
    profile: profile.profile,
    transport,
    fps_cap: fpsCapOf(context, profile, multipart),
    max_size: entry.platform === "android" && Number.isFinite(profile.maxSize) ? profile.maxSize : null,
    scale_factor: entry.platform === "ios" ? profile.scaleFactor : null,
    control_owner: context?.state.controlOwner ?? "shared",
    input_paused: context?.state.inputPaused ?? false,
    session: link?.session_id
      ? { id: link.session_id, started_by_canvas: link.started_by_canvas, reused: link.reused } : null,
    session_error: link?.error ?? null,
    booted_by_canvas: entry.bootedByCanvas,
    error: entry.error,
    url: `${devicePath(entry.id)}/`,
    attached_at: entry.attachedAt,
    // The profile the stream runs now (the fields above are the planned one).
    profile_applied: appliedProfileOf(server, entry, multipart),
    // The Display menu choice of this device ({fps, size}, QW-05), null for none.
    display_choice: context?.displayChoice ? { ...context.displayChoice } : null,
    // The host control lease and the bound session (contract 6.7); null before the device is live.
    lease: context?.lease ? leaseStatus(context) : null,
    session_stopped: context?.lease ? (refreshSession(context), sessionStoppedOf(context)) : false,
  };
}

/** The `device` key of /status in a workspace (contract 3.9). */
function deviceStatusFor(server, entry) {
  const summary = deviceSummary(server, entry);
  return {
    id: summary.id, state: summary.state, tab: summary.tab, primary: summary.primary, focused: summary.focused,
    profile: summary.profile, fps_cap: summary.fps_cap, max_size: summary.max_size,
    scale_factor: summary.scale_factor, session: summary.session, profile_applied: summary.profile_applied,
  };
}

function deviceStatusOf(server, context) {
  const entry = server.registry.get(context.id);
  return entry ? deviceStatusFor(server, entry) : null;
}

// ----- targets ------------------------------------------------------------------------------

async function androidTool(server) {
  if (server.options.adb) return server.options.adb;
  server.adbLookup ??= findAdb().catch(() => null);
  return server.adbLookup;
}

async function iosTool(server) {
  if (server.options.simctl) return server.options.simctl;
  if (platform !== "darwin") return null;
  server.xcrunLookup ??= findExecutable("xcrun").catch(() => null);
  return server.xcrunLookup;
}

/** Running targets: `adb devices` and, on macOS, the booted Simulators. */
async function listTargets(server, { ios = true } = {}) {
  const running = [];
  const failed = {};
  const adb = await androidTool(server);
  if (adb) {
    try {
      const { stdout } = await execFileAsync(adb, ["devices"], { timeout: TARGET_LIST_MS, encoding: "utf8" });
      for (const line of stdout.split(/\r?\n/).slice(1)) {
        const [target, state] = line.trim().split(/\s+/);
        if (!target || !state || !/^[A-Za-z0-9._:-]{1,128}$/.test(target)) continue;
        running.push({ platform: "android", target, state, name: await androidName(server, adb, target, state) });
      }
    } catch (error) {
      failed.android = error.message;
    }
  } else {
    failed.android = "adb was not found";
  }
  const xcrun = ios ? await iosTool(server) : null;
  if (xcrun) {
    try {
      const { stdout } = await execFileAsync(xcrun, ["simctl", "list", "devices", "booted", "-j"],
        { timeout: TARGET_LIST_MS, encoding: "utf8" });
      for (const device of Object.values(JSON.parse(stdout).devices ?? {}).flat()) {
        if (device?.state !== "Booted" || !/^[0-9A-Fa-f-]{36}$/.test(device.udid ?? "")) continue;
        running.push({ platform: "ios", target: device.udid, state: "Booted", name: String(device.name ?? device.udid) });
      }
    } catch (error) {
      failed.ios = error.message;
    }
  } else if (ios) {
    failed.ios = "xcrun simctl is not available";
  }
  return { running, failed };
}

/** An emulator's AVD name (`adb emu avd name`, cached), else the serial. */
async function androidName(server, adb, target, state) {
  server.avdNames ??= new Map();
  if (server.avdNames.has(target)) return server.avdNames.get(target);
  let name = target;
  if (state === "device" && target.startsWith("emulator-")) {
    try {
      const { stdout } = await execFileAsync(adb, ["-s", target, "emu", "avd", "name"], { timeout: 2000, encoding: "utf8" });
      const first = stdout.split(/\r?\n/)[0]?.trim();
      if (first && /^[A-Za-z0-9._-]{1,100}$/.test(first)) name = first;
    } catch {}
    server.avdNames.set(target, name);
  }
  return name;
}

function isReady(target) {
  return target.state === "device" || target.state === "Booted";
}

async function targetsSummary(server) {
  const { running } = await listTargets(server);
  const workspace = server.workspace;
  const full = server.registry.size >= server.registry.max;
  const targets = running.map((target) => {
    const id = `${target.platform}~${target.target}`;
    const attached = Boolean(server.registry.get(id));
    const tab = workspace?.locate(id)?.tab ?? null;
    let reason = null;
    if (server.mode !== "workspace") reason = "This Canvas shows one device";
    else if (attached) reason = "Attached";
    else if (!isReady(target)) reason = `The target is ${target.state}`;
    else if (full) reason = `At most ${MAX_DEVICES} devices are attached`;
    return {
      platform: target.platform, target: target.target, name: target.name, state: target.state,
      attached, tab, attachable: reason === null, reason,
    };
  });
  await refreshBootable(server);
  return { running: targets, bootable: bootableList(server, running) };
}

function bootableList(server, running = []) {
  return bootableSpecs(server).map((spec) => bootableView(server, spec, running));
}

/**
 * What the page may boot: the --bootable list first, then (with --discover-bootable) every
 * other AVD and shut-down iOS Simulator of this machine, from the last discovery.
 */
function bootableSpecs(server) {
  const given = server.options.bootable ?? [];
  const found = server.bootableFound?.items ?? [];
  const specs = given.map((spec) => {
    if (spec.kind !== "simulator") return spec;
    const known = found.find((item) => item.kind === "simulator" && item.udid.toLowerCase() === spec.udid.toLowerCase());
    return known ? { ...spec, label: known.label, runtime: known.runtime } : spec;
  });
  for (const item of found) {
    const listed = specs.some((spec) => spec.kind === item.kind && (item.kind === "avd"
      ? spec.name === item.name : spec.udid.toLowerCase() === item.udid.toLowerCase()));
    if (!listed) specs.push(item);
  }
  return specs;
}

/**
 * Discover again when the last list is older than 30 s (5 s after a failed part). The first
 * call waits at most 3 s, then answers with the --bootable list while discovery goes on.
 */
async function refreshBootable(server) {
  if (!server.options.discoverBootable) return;
  const last = server.bootableFound;
  if (last && performance.now() - last.at < last.ttl) return;
  server.bootableDiscovery ??= discoverBootable(server, last?.items ?? [])
    .then(({ items, failed }) => {
      server.bootableFound = { at: performance.now(), items, ttl: failed ? BOOTABLE_RETRY_MS : BOOTABLE_DISCOVERY_MS };
    })
    .catch(() => {})
    .finally(() => { server.bootableDiscovery = null; });
  if (!last) {
    let timer;
    await Promise.race([server.bootableDiscovery,
      new Promise((resolvePromise) => { timer = setTimeout(resolvePromise, BOOTABLE_FIRST_WAIT_MS); })]);
    clearTimeout(timer);
  }
}

/** The machine's AVDs and iOS Simulators; a part that fails keeps that part of `previous`. */
async function discoverBootable(server, previous) {
  const items = [];
  let failed = false;
  const keep = (kind) => { failed = true; items.push(...previous.filter((item) => item.kind === kind)); };
  const adb = await androidTool(server);
  if (adb) {
    // The CLI finds the emulator binary as `devices boot` does (AUTONOM_EMULATOR, the SDK of adb, ...).
    const result = await runCliJson([pythonOf(server), autonomScript(server.options), "--adb", adb, "devices"],
      { timeoutMs: BOOTABLE_DISCOVERY_CLI_MS, env: server.env });
    if (result.code !== 0 || !Array.isArray(result.json?.avds)) {
      keep("avd");
    } else {
      for (const name of result.json.avds) {
        if (typeof name === "string" && AVD_NAME_PATTERN.test(name)) items.push({ kind: "avd", name, discovered: true });
      }
    }
  }
  const xcrun = await iosTool(server);
  if (xcrun) {
    try {
      const { stdout } = await execFileAsync(xcrun, ["simctl", "list", "devices", "available", "-j"],
        { timeout: TARGET_LIST_MS * 4, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 });
      const simulators = [];
      for (const [runtimeKey, devices] of Object.entries(JSON.parse(stdout).devices ?? {})) {
        const match = /SimRuntime\.iOS-(\d+)-(\d+)/.exec(runtimeKey);
        if (!match || !Array.isArray(devices)) continue;
        for (const device of devices) {
          if (!SIMULATOR_UDID_PATTERN.test(device?.udid ?? "") || device.isAvailable === false) continue;
          simulators.push({ kind: "simulator", udid: device.udid, label: String(device.name ?? device.udid).slice(0, 100),
            runtime: `iOS ${match[1]}.${match[2]}`, discovered: true });
        }
      }
      simulators.sort((a, b) => a.label.localeCompare(b.label) || a.runtime.localeCompare(b.runtime));
      items.push(...simulators);
    } catch {
      keep("simulator");
    }
  }
  return { items, failed };
}

function bootableView(server, spec, running = []) {
  let target = null;
  if (spec.kind === "avd") {
    const match = running.find((item) => item.platform === "android" &&
      (spec.port ? item.target === `emulator-${spec.port}` : item.name === spec.name));
    target = match?.target ?? null;
  } else {
    target = running.find((item) => item.platform === "ios" && item.target.toLowerCase() === spec.udid.toLowerCase())
      ?.target ?? null;
  }
  const job = [...server.boots.values()].find((item) => sameBootable(item, spec) && !item.finished_at);
  return {
    kind: spec.kind, name: spec.name ?? null, port: spec.port ?? null, udid: spec.udid ?? null,
    label: spec.label ?? (spec.kind === "avd" ? spec.name : spec.udid), runtime: spec.runtime ?? null,
    running: target !== null, target, job: job?.id ?? null,
  };
}

function bootableFor(server, ref) {
  const spec = bootableSpecs(server).find((item) => (ref.platform === "ios"
    ? item.kind === "simulator" && item.udid.toLowerCase() === ref.target.toLowerCase()
    : item.kind === "avd" && (item.port ? ref.target === `emulator-${item.port}` : item.name === ref.name)));
  return spec ? bootableView(server, spec) : null;
}

function sameBootable(job, spec) {
  return job.kind === spec.kind && (spec.kind === "avd" ? job.name === spec.name : job.udid === spec.udid);
}

// ----- devices ------------------------------------------------------------------------------

/** Make the context of a workspace device: resolve its tools, then the per-device context. */
async function createWorkspaceDevice(server, { id, platform: devicePlatform, target, name }) {
  if (server.mode !== "workspace") return server.legacyContext;
  return deviceScope.run({ id }, async () => {
    const tool = devicePlatform === "ios" ? await iosTool(server) : await androidTool(server);
    const options = {
      ...server.options,
      ...(devicePlatform === "ios" ? { simctl: tool ?? server.options.simctl } : { adb: tool ?? server.options.adb }),
    };
    const device = await resolveDeviceOptions(options, { platform: devicePlatform, target });
    device.name = name;
    const focused = server.focus.add(id) === "focused";
    device.profile = profileVideo({ platform: devicePlatform, focused, options: server.options });
    return createDeviceContext(server, device);
  });
}

function checkPlatform(server, devicePlatform) {
  const transport = server.options.transport;
  if (devicePlatform === "ios" && (transport === "scrcpy" || transport === "screenrecord")) {
    throw new CanvasError(409, "platform_unavailable", `--transport ${transport} cannot show an iOS Simulator`,
      "Start the Canvas with --transport auto to mix platforms.");
  }
  if (devicePlatform === "android" && transport === "idb") {
    throw new CanvasError(409, "platform_unavailable", "--transport idb cannot show an Android device",
      "Start the Canvas with --transport auto to mix platforms.");
  }
}

/**
 * Attach a running target into a tab and slot: the slot is reserved first, the context
 * made, then its Autonom session started or reused. A device already in that tab answers
 * attached false.
 */
async function attachDevice(server, { platform: devicePlatform, target, tab, slot, name = null, origin = "human",
  bootedByCanvas = false, listed = null, autoPlace = false }) {
  if (server.shuttingDown) throw new CanvasError(503, "canvas_stopping", "Canvas is stopping");
  const id = deviceIdFor(devicePlatform, target);
  const workspace = server.workspace;
  // autoPlace with no tab or slot: a placed device stays where it is; a new one gets room
  // (Workspace.roomFor) only once every check passed, in the same tick as its place.
  const auto = autoPlace && tab === undefined && slot === undefined;
  let tabId = tab ?? (auto ? (workspace.locate(id)?.tab ?? null) : workspace.activeTab().id);
  if (tabId !== null && !workspace.tab(tabId)) throw new CanvasError(404, "tab_not_found", `No tab ${tabId} in this workspace`);
  const present = server.registry.get(id);
  if (present) {
    const where = workspace.locate(id);
    if (where) tabId ??= where.tab;
    if (!where && tabId === null) tabId = workspace.roomFor().id;
    if (where && where.tab !== tabId) {
      const other = workspace.tab(where.tab);
      throw new CanvasError(409, "device_in_other_tab", `Device ${id} is in tab ${other?.name ?? where.tab}`,
        "Move it here instead.", { device: id, tab: where.tab });
    }
    if (!where) workspace.place({ id, platform: devicePlatform, target, name: present.name }, { tab: tabId, slot });
    else workspace.setPresent(id, true);
    return { entry: present, attached: false };
  }
  checkPlatform(server, devicePlatform);
  const tool = devicePlatform === "ios" ? await iosTool(server) : await androidTool(server);
  if (!tool) {
    throw new CanvasError(409, "platform_unavailable",
      devicePlatform === "ios" ? "xcrun simctl is not available on this machine" : "adb was not found",
      devicePlatform === "ios" ? "iOS Simulators need macOS with Xcode." : "Install the Android SDK platform tools.");
  }
  const running = listed ?? (await listTargets(server, { ios: devicePlatform === "ios" })).running;
  const found = running.find((item) => item.platform === devicePlatform && item.target === target);
  if (!found) throw new CanvasError(404, "target_not_found", `Target ${target} is not running`, "Boot it first.");
  if (!isReady(found)) {
    throw new CanvasError(409, "target_not_ready", `Target ${target} is ${found.state}`,
      "Wait until it is ready (adb state device, Simulator Booted).");
  }
  if (server.registry.size >= server.registry.max) {
    throw new CanvasError(409, "device_limit", `A Canvas shows at most ${MAX_DEVICES} devices`, "Detach a device first.");
  }
  const deviceName = name ?? found.name ?? target;
  const wasPlaced = workspace.locate(id);
  const room = tabId === null ? workspace.roomFor() : null;
  workspace.place({ id, platform: devicePlatform, target, name: deviceName }, { tab: room?.id ?? tabId, slot });
  workspace.setPresent(id, true);
  const log = beginLog(server, { device: id, kind: "device", name: "attach", origin, summary: `attach ${id}` });
  let result;
  try {
    result = await server.registry.attach({ platform: devicePlatform, target, name: deviceName, bootedByCanvas });
  } catch (error) {
    server.focus.forget(id);
    // A concurrent attach of the same device that holds it keeps its slot present.
    if (server.registry.get(id)) workspace.setPresent(id, true);
    else if (wasPlaced) workspace.setPresent(id, false);
    else {
      workspace.remove(id);
      room?.undo();
    }
    log.end({ ok: false, errorCode: error.code ?? "backend_failed", error: error.message });
    throw error;
  }
  log.end({ ok: true });
  const { entry } = result;
  if (bootedByCanvas) server.booted.set(id, { platform: devicePlatform, target, bootedAt: new Date().toISOString() });
  // Only the attach that made the device starts its session; one that joined it waits.
  if (result.attached) await ensureSessionFor(server, entry, origin);
  scheduleDiscovery(server);
  return result;
}

/**
 * Start (or reuse) the session of an attached device. The run is kept on the entry so a
 * detach that comes while it is still starting waits for it and reports the stop truthfully.
 */
function ensureSessionFor(server, entry, origin = "system") {
  const starting = ensureSessionRun(server, entry, origin);
  entry.sessionStarting = starting;
  starting.finally(() => {
    if (entry.sessionStarting === starting) entry.sessionStarting = null;
  }).catch(() => {});
  return starting;
}

async function ensureSessionRun(server, entry, origin) {
  const context = entry.context;
  if (!context || server.shuttingDown) return entry.session;
  const log = beginLog(server, { device: entry.id, kind: "session", name: "ensure", origin, summary: `session for ${entry.id}` });
  const starting = deviceScope.run({ id: entry.id }, () => server.sessionManager.ensure({
    platform: entry.platform, target: entry.target, tool: context.adbPath, idb: server.options.idb ?? null,
  }));
  server.pendingSessionStarts ??= new Set();
  server.pendingSessionStarts.add(starting);
  let link;
  try {
    link = await starting;
  } finally {
    server.pendingSessionStarts.delete(starting);
  }
  if (server.shuttingDown) {
    // The Canvas began stopping while the session started: end it with the Canvas.
    server.sessionManager.releaseDetached([link]);
    log.end({ ok: false, errorCode: "canvas_stopping", error: "Canvas stopped while the session started" });
    return link;
  }
  if (server.registry.get(entry.id) !== entry) {
    // Detached while the session started: end it again; the detach waits for this stop.
    log.end({ ok: false, errorCode: "device_detached", error: "detached while the session started" });
    entry.lateRelease = releaseSession(server, { id: entry.id, session: link }, origin)
      .catch((error) => ({ stopped: false, error: { error_code: "backend_failed", error: String(error?.message ?? error) } }));
    return link;
  }
  entry.session = link;
  log.end(link.error ? { ok: false, errorCode: link.error.error_code, error: link.error.error } : { ok: true });
  scheduleDiscovery(server);
  return link;
}

function moveDevice(server, id, { tab, slot, origin }) {
  if (!tab) throw new CanvasError(400, "invalid_value", "tab is required");
  const workspace = server.workspace;
  if (!workspace.locate(id)) throw new CanvasError(404, "device_not_found", `Device ${id} is not in this workspace`);
  workspace.move(id, { tab, slot });
  logEvent(server, { device: id, kind: "device", name: "move", origin, summary: `move ${id} to ${tab}` }, { ok: true });
  const entry = server.registry.get(id);
  const device = entry ? deviceSummary(server, entry)
    : absentRefs(server).find((ref) => ref.id === id) ?? null;
  return { ok: true, device };
}

async function detachDevice(server, id, { origin = "human", reason = "detach" } = {}) {
  const entry = server.registry.get(id);
  if (!entry) throw new CanvasError(404, "device_not_found", `No device ${id} is attached`);
  // One detach per entry: a second call (double click, retried POST, tab close) joins the
  // first, so the Canvas-started session is stopped once and both get the same answer.
  entry.detachResult ??= (async () => {
    const log = beginLog(server, { device: id, kind: "device", name: "detach", origin, summary: `detach ${id}` });
    server.focus.forget(id);
    server.workspace.remove(id);
    await server.registry.detach(id, { reason });
    log.end({ ok: true });
    // A session start still running ends that session itself once it answers (bounded by
    // the session CLI timeout); wait for it so the answer says whether it was stopped.
    if (entry.sessionStarting) await entry.sessionStarting.catch(() => {});
    const released = entry.lateRelease ? await entry.lateRelease : await releaseSession(server, entry, origin);
    server.booted.delete(id);
    scheduleDiscovery(server);
    return { ok: true, detached: id, session_stopped: released.stopped };
  })();
  return entry.detachResult;
}

async function releaseSession(server, entry, origin = "system") {
  const link = entry.session;
  if (!link?.started_by_canvas) return { stopped: false, error: null };
  const log = beginLog(server, { device: entry.id, kind: "session", name: "release", origin, summary: `stop ${link.session_id}` });
  const result = await server.sessionManager.release(link);
  log.end(result.error ? { ok: false, errorCode: result.error.error_code, error: result.error.error } : { ok: true });
  return result;
}

async function closeTab(server, tabId, origin) {
  const workspace = server.workspace;
  if (!workspace.tab(tabId)) throw new CanvasError(404, "tab_not_found", `No tab ${tabId} in this workspace`);
  const attached = workspace.tab(tabId).slots.filter((slot) => slot?.present).map((slot) => slot.device)
    .filter((id) => server.registry.get(id));
  const result = workspace.closeTab(tabId);
  logEvent(server, { kind: "workspace", name: "tab.close", origin, summary: `tab ${tabId}` }, { ok: true });
  const detached = [];
  await Promise.all(attached.map(async (id) => {
    try {
      await detachDevice(server, id, { origin, reason: "tab-close" });
      detached.push(id);
    } catch {}
  }));
  return { ok: true, closed: result.closed, detached, created: result.created };
}

/** Restart a device's context (bridge, tools, stream) and retry its session. */
async function reconnectDevice(server, id, { origin = "human" } = {}) {
  const entry = server.registry.get(id);
  if (!entry) throw new CanvasError(404, "device_not_found", `No device ${id} is attached`);
  const { running } = await listTargets(server, { ios: entry.platform === "ios" });
  const found = running.find((item) => item.platform === entry.platform && item.target === entry.target);
  if (!found || !isReady(found)) {
    throw new CanvasError(404, "target_not_found", `Target ${entry.target} is not running`, "Boot it first.");
  }
  entry.failures = [];
  entry.holdFailed = false;
  server.registry.setState(id, "attaching");
  restoreDevice(server, entry, { delays: [0], origin, retrySession: true }).catch(() => {});
  return { ok: true, device: deviceSummary(server, entry) };
}

/**
 * Close a device's context (bounded) and create it again, after each delay in turn until one
 * works; the session link is kept. No delay left: the device stays failed.
 */
async function restoreDevice(server, entry, { delays = RESTORE_DELAYS_MS, origin = "system", retrySession = false } = {}) {
  if (entry.restoring) return entry.restoring;
  entry.restoring = (async () => {
    const old = entry.context;
    if (old) {
      await Promise.race([closeDeviceContext(old).catch(() => {}), sleep(RESTORE_CLOSE_MS)]);
      deviceClosings.delete(old);
    }
    for (const delay of delays) {
      if (delay) await sleep(delay);
      if (server.shuttingDown || server.registry.get(entry.id) !== entry || entry.state === "detaching" ||
          entry.holdFailed) {
        return false;
      }
      const log = beginLog(server, { device: entry.id, kind: "device", name: "restore", origin, summary: `restore ${entry.id}` });
      try {
        const focused = server.focus.profileOf(entry.id) === "focused";
        // The Display menu choice outlives the context it was made on.
        const choice = old?.displayChoice ?? null;
        const context = await deviceScope.run({ id: entry.id }, async () => {
          const tool = entry.platform === "ios" ? await iosTool(server) : await androidTool(server);
          const options = {
            ...server.options,
            ...(entry.platform === "ios" ? { simctl: tool ?? server.options.simctl } : { adb: tool ?? server.options.adb }),
          };
          const device = await resolveDeviceOptions(options, { platform: entry.platform, target: entry.target });
          device.name = entry.name;
          device.profile = profileVideo({ platform: entry.platform, focused, options: server.options });
          const created = createDeviceContext(server, device);
          if (choice) {
            // Its streams open with the choice, so it counts as applied.
            created.displayChoice = choice;
            created.displayApplied = choice;
            created.profile = streamProfile(server, created, focused);
          }
          return created;
        });
        if (server.registry.get(entry.id) !== entry || entry.state === "detaching") {
          await closeDeviceContext(context);
          log.end({ ok: false, errorCode: "device_detached", error: "detached while it was restored" });
          return false;
        }
        server.registry.replaceContext(entry.id, context);
        server.registry.setState(entry.id, "live");
        log.end({ ok: true });
        if (retrySession && !entry.session?.session_id) await ensureSessionFor(server, entry, origin);
        return true;
      } catch (error) {
        log.end({ ok: false, errorCode: "backend_failed", error: error.message });
        server.registry.setState(entry.id, "failed", { error: error.message });
      }
    }
    return false;
  })().finally(() => {
    entry.restoring = null;
  });
  return entry.restoring;
}

/** An exception nobody caught: a device's own fails only that device (contract 3.8). */
function handleUncaught(server, error) {
  const message = error?.message ?? String(error);
  const store = deviceScope.getStore();
  const entry = store?.id ? server.registry.get(store.id) : null;
  if (!entry) {
    console.error(`Canvas error: ${message}`);
    logEvent(server, { kind: "device", name: "error", origin: "system", summary: "uncaught error" },
      { ok: false, errorCode: "internal_error", error: message });
    return;
  }
  console.error(`Device ${entry.id} failed: ${message}`);
  logEvent(server, { device: entry.id, kind: "device", name: "failure", origin: "system", summary: `${entry.id} failed` },
    { ok: false, errorCode: "device_failed", error: message });
  if (entry.state === "detaching") return;
  const now = performance.now();
  entry.failures = [...entry.failures.filter((at) => now - at < FAILURE_WINDOW_MS), now];
  server.registry.setState(entry.id, "failed", { error: message });
  if (entry.failures.length >= FAILURES_TO_STAY_FAILED) {
    // Too many in a row: close it and wait for an explicit reconnect.
    entry.holdFailed = true;
    if (entry.context) Promise.race([closeDeviceContext(entry.context).catch(() => {}), sleep(RESTORE_CLOSE_MS)]);
    return;
  }
  restoreDevice(server, entry).catch(() => {});
}

// ----- focus profiles -------------------------------------------------------------------------

// How long a profile restart of a scrcpy stream may take to stream again before it counts as failed.
const PROFILE_STREAM_WAIT_MS = 10_000;

/** Whether the device's current session streams within `timeoutMs`. */
async function streamingWithin(context, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  for (;;) {
    if (context.session?.state === "streaming") return true;
    if (context.shuttingDown || performance.now() >= deadline) return false;
    await sleep(100);
  }
}

/**
 * Apply a planned profile to a running device: restart its stream with the new settings.
 * Answers true once the stream runs with them (or, with no stream running, once the next
 * one will open with them) and false when the restart failed, so the FocusController
 * reports the profile as applied only after a restart that worked.
 */
async function applyProfile(server, id, profileName) {
  const context = contextOf(server, id);
  if (!context) return false;
  const choice = context.displayChoice ?? null;
  const profile = streamProfile(server, context, profileName === "focused", choice);
  // The single-device Canvas keeps its CLI settings (no profile) until a choice is made.
  context.profile = server.mode === "workspace" || choice ? profile : null;
  const rate = choice?.fps === 0 ? "on change" : choice?.fps === null ? "own fps" : `${choice?.fps} fps`;
  const summary = choice ? `${id} ${profileName} ${rate} ${choice.size}` : `${id} ${profileName}`;
  const log = beginLog(server, { device: id, kind: "stream", name: "profile", origin: "system", summary });
  // Only the newest of overlapping restarts records the choice it applied.
  const settle = beginDisplayApply(context, choice);
  try {
    const session = context.session;
    let ok = true;
    if (session instanceof IosFastSession) {
      // false: no stream ran (later streams open with the new settings) or its restart failed.
      const restarted = await session.setVideo({ fps: profile.fps, scaleFactor: profile.scaleFactor });
      ok = restarted === true || context.videoClients.size === 0;
    } else if (session && chooseTransport(context) === "scrcpy") {
      const wanted = context.videoClients.size > 0 || context.controlClients.size > 0;
      const restart = async () => {
        await stopSession(context);
        if (wanted) ensureSession(context);
      };
      // A workspace device's failure stays its own (deviceScope); the single device has no scope.
      await (server.mode === "workspace" ? deviceScope.run({ id }, restart) : restart());
      if (wanted) ok = await streamingWithin(context, PROFILE_STREAM_WAIT_MS);
    }
    broadcastState(context);
    settle(ok);
    if (ok) log.end({ ok: true });
    else log.end({ ok: false, errorCode: "backend_failed", error: "the stream did not restart with the new profile" });
    return ok;
  } catch (error) {
    settle(false);
    log.end({ ok: false, errorCode: "backend_failed", error: error.message });
    return false;
  }
}

// ----- workspace restore, offline poll -----------------------------------------------------------

/** At start: place the --device entries, then attach every placed device whose target runs. */
async function restoreWorkspace(server) {
  const workspace = server.workspace;
  const { running } = await listTargets(server);
  for (const spec of server.options.devices ?? []) {
    const id = `${spec.platform}~${spec.target}`;
    if (workspace.locate(id)) continue;
    let tab;
    try {
      tab = workspace.roomFor().id;
    } catch {
      console.error(`Canvas workspace has no room for ${id}`);
      continue;
    }
    const listed = running.find((item) => item.platform === spec.platform && item.target === spec.target);
    workspace.place({ id, platform: spec.platform, target: spec.target, name: listed?.name ?? spec.target }, { tab });
  }
  // Placed devices start absent; one attached while the listing above ran (an attach
  // request does not wait for the restore) stays present: it is live.
  for (const ref of workspace.refs()) workspace.setPresent(ref.id, Boolean(server.registry.get(ref.id)));
  await Promise.all(workspace.refs().map(async (ref) => {
    const listed = running.find((item) => item.platform === ref.platform && item.target === ref.target);
    if (!listed || !isReady(listed)) return;
    await attachDevice(server, {
      platform: ref.platform, target: ref.target, tab: ref.tab, slot: ref.slot, name: ref.name, origin: "system",
      listed: running,
    }).catch((error) => console.error(`Could not attach ${ref.id}: ${error.message}`));
  }));
  scheduleDiscovery(server);
}

/** Every 2 s: a gone target goes offline, a returning one live, a running absent one attaches. */
async function pollTargets(server) {
  if (server.polling || server.shuttingDown) return;
  const entries = server.registry.list();
  const absent = absentRefs(server);
  if (!entries.length && !absent.length) return;
  server.polling = true;
  try {
    const ios = entries.some((entry) => entry.platform === "ios") || absent.some((ref) => ref.platform === "ios");
    const { running, failed } = await listTargets(server, { ios });
    const ready = new Set(running.filter(isReady).map((item) => `${item.platform}~${item.target}`));
    for (const entry of entries) {
      // An attached device is never shown absent in its slot.
      if (server.registry.get(entry.id) === entry) server.workspace.setPresent(entry.id, true);
      if (failed[entry.platform]) continue;
      const here = ready.has(entry.id);
      if (entry.state === "live" && !here) {
        server.registry.setState(entry.id, "offline", { error: "The target is not running" });
        if (entry.context) stopSession(entry.context);
        logEvent(server, { device: entry.id, kind: "device", name: "offline", origin: "system", summary: `${entry.id} offline` },
          { ok: false, errorCode: "target_not_found", error: "target gone" });
      } else if (entry.state === "offline" && here) {
        server.registry.setState(entry.id, "live");
        const context = entry.context;
        if (context && (context.videoClients.size || context.controlClients.size)) {
          deviceScope.run({ id: entry.id }, () => ensureSession(context));
        }
        logEvent(server, { device: entry.id, kind: "device", name: "online", origin: "system", summary: `${entry.id} back` },
          { ok: true });
      }
    }
    for (const ref of absent) {
      if (failed[ref.platform] || !ready.has(ref.id) || server.registry.size >= server.registry.max) continue;
      attachDevice(server, {
        platform: ref.platform, target: ref.target, tab: ref.tab, slot: ref.slot, name: ref.name, origin: "system",
        listed: running,
      }).catch(() => {});
    }
  } catch {}
  finally {
    server.polling = false;
  }
}

// ----- boot jobs -------------------------------------------------------------------------------

function jobView(job) {
  const { spec, ...view } = job;
  void spec;
  return { ...view, elapsed_ms: Math.round((job.finishedMs ?? performance.now()) - job.startedMs) };
}

function pruneJobs(server) {
  const now = performance.now();
  const finished = [...server.boots.values()].filter((job) => job.finished_at);
  for (const job of finished) if (now - job.finishedMs > BOOT_KEEP_MS) server.boots.delete(job.id);
  const left = [...server.boots.values()].filter((job) => job.finished_at);
  while (left.length > BOOT_KEEP_MAX) server.boots.delete(left.shift().id);
}

function startBoot(server, body, origin) {
  const kind = body.kind;
  let spec;
  if (kind === "avd") {
    if (typeof body.name !== "string") throw new CanvasError(400, "invalid_value", "name must be an AVD name");
    spec = bootableSpecs(server).find((item) => item.kind === "avd" && item.name === body.name);
  } else if (kind === "simulator") {
    if (typeof body.udid !== "string") throw new CanvasError(400, "invalid_value", "udid must be a Simulator UDID");
    spec = bootableSpecs(server).find((item) => item.kind === "simulator" &&
      item.udid.toLowerCase() === body.udid.toLowerCase());
  } else {
    throw new CanvasError(400, "invalid_value", "kind must be avd or simulator");
  }
  if (!spec) {
    throw new CanvasError(403, "boot_not_allowed", "This Canvas may not boot that target",
      "Pick a device the Canvas lists, or start it with --bootable avd:NAME or --bootable simulator:UDID.");
  }
  const attach = body.attach === undefined ? true : body.attach;
  if (typeof attach !== "boolean") throw new CanvasError(400, "invalid_value", "attach must be true or false");
  const tab = optionalString(body, "tab");
  const slot = optionalSlot(body);
  pruneJobs(server);
  const same = [...server.boots.values()].find((job) => sameBootable(job, spec) && !job.finished_at);
  if (same) return { status: 200, job: jobView(same) };
  if (attach) {
    if (server.registry.size >= server.registry.max) {
      throw new CanvasError(409, "device_limit", `A Canvas shows at most ${MAX_DEVICES} devices`, "Detach a device first.");
    }
    if (tab !== undefined) {
      const view = server.workspace.tab(tab);
      if (!view) throw new CanvasError(404, "tab_not_found", `No tab ${tab} in this workspace`);
      if (!view.slots.includes(null)) {
        throw new CanvasError(409, "tab_full", `Tab ${view.name} has no empty tile`, "Pick another tab.", { tab });
      }
    }
  }
  const runningJobs = [...server.boots.values()].filter((job) => !job.finished_at).length;
  if (runningJobs >= BOOT_RUNNING_MAX) {
    throw new CanvasError(429, "boot_busy", `At most ${BOOT_RUNNING_MAX} boots run at once`, "Wait for one to finish.");
  }
  let id = `b_${randomBytes(4).toString("hex")}`;
  while (server.boots.has(id)) id = `b_${randomBytes(4).toString("hex")}`;
  const job = {
    id, kind: spec.kind, name: spec.name ?? null, udid: spec.udid ?? null, port: spec.port ?? null, state: "queued",
    attach, tab: tab ?? null, slot: slot ?? null, target: null, device: null, already_running: false,
    error_code: null, error: null, started_at: new Date().toISOString(), finished_at: null,
    startedMs: performance.now(), finishedMs: null, spec,
  };
  Object.defineProperty(job, "startedMs", { enumerable: false, writable: true, value: job.startedMs });
  Object.defineProperty(job, "finishedMs", { enumerable: false, writable: true, value: null });
  server.boots.set(id, job);
  runBoot(server, job, origin).catch(() => {});
  return { status: 202, job: jobView(job) };
}

async function runBoot(server, job, origin) {
  const log = beginLog(server, { kind: "boot", name: job.kind, origin, summary: `boot ${job.name ?? job.udid}` });
  const finish = (fields) => {
    Object.assign(job, fields, { finished_at: new Date().toISOString() });
    job.finishedMs = performance.now();
    log.end(fields.state === "done" ? { ok: true } : { ok: false, errorCode: fields.error_code, error: fields.error });
    scheduleDiscovery(server);
  };
  job.state = "booting";
  const tool = job.kind === "avd" ? await androidTool(server) : await iosTool(server);
  const argv = [pythonOf(server), autonomScript(server.options)];
  if (job.kind === "avd") {
    argv.push(...(tool ? ["--adb", tool] : []), "devices", "boot", "--avd", job.name,
      ...(job.port ? ["--port", String(job.port)] : []), "--timeout", String(BOOT_TIMEOUT_S));
  } else {
    argv.push("--platform", "ios", "--udid", job.udid, ...(tool ? ["--simctl", tool] : []),
      "devices", "boot", "--timeout", String(BOOT_TIMEOUT_S));
  }
  const result = await runCliJson(argv, { timeoutMs: BOOT_CLI_MS, env: server.env });
  const json = result.json;
  if (result.code !== 0 || !json || json.ok === false) {
    finish({ state: "failed", error_code: json?.error_code ?? "backend_failed", error: String(json?.error ?? result.error ?? "boot failed").slice(0, 300) });
    return;
  }
  const target = job.kind === "avd" ? (json.serial ?? json.target_id ?? null) : (json.target_id ?? json.udid ?? job.udid);
  job.target = target;
  job.already_running = Boolean(json.already_running);
  if (!job.attach || !target) {
    finish({ state: target ? "done" : "failed", ...(target ? {} : { error_code: "backend_failed", error: "the boot named no target" }) });
    return;
  }
  job.state = "attaching";
  const platformName = job.kind === "avd" ? "android" : "ios";
  const workspace = server.workspace;
  // The tab and slot asked for, re-checked: a taken slot falls back to the first empty one,
  // a full (or closed) tab to a new tab.
  // With neither asked, the room rule of POST /api/devices (the active tab grows first).
  const auto = !job.tab && (job.slot === null || job.slot === undefined);
  let tab = job.tab && workspace.tab(job.tab) ? job.tab : workspace.activeTab().id;
  let slot = job.slot ?? undefined;
  const view = workspace.tab(tab);
  if (slot !== undefined && (slot >= view.layout || view.slots[slot] !== null)) slot = undefined;
  if (auto) {
    tab = undefined;
  } else if (!view.slots.includes(null)) {
    try {
      tab = workspace.createTab({}).id;
    } catch (error) {
      finish({ state: "failed", error_code: error.code ?? "tab_limit", error: error.message });
      return;
    }
    slot = undefined;
  }
  try {
    const { entry } = await attachDevice(server, {
      platform: platformName, target, tab, slot, origin, bootedByCanvas: !job.already_running,
      name: job.kind === "avd" ? job.name : null, autoPlace: auto,
    });
    finish({ state: "done", device: entry.id, tab: workspace.locate(entry.id)?.tab ?? tab });
  } catch (error) {
    finish({ state: "failed", error_code: error.code ?? "backend_failed", error: error.message });
  }
}

/** The workspace page used when no page module is wired in (tests and minimal installs). */
function renderBuiltinWorkspacePage(model) {
  const data = scriptString(JSON.stringify(model));
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonom Canvas</title>
<style>:root{color-scheme:light dark}body{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:16px;background:Canvas;color:CanvasText}</style>
</head>
<body>
<h1>Autonom Canvas</h1>
<p id="workspace-status">Loading the workspace…</p>
<script>
"use strict";
const MODEL=JSON.parse(${data});
document.getElementById("workspace-status").textContent="Workspace "+MODEL.workspace+": use /api/workspace.";
</script>
</body>
</html>`;
}

// Last, so every module-level constant above is initialized before main() reads it.
if (isMainModule()) {
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
}
