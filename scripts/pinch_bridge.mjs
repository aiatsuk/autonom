/** One-shot pinch using the Canvas scrcpy and idb HID transports. */
import { access, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { isIP } from "node:net";
import { delimiter, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ScrcpySession, resolveScrcpyServer, SCRCPY_INSTALL_HINT } from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs";
import { encodeTouch } from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-lib.mjs";
import { IdbCompanion, IdbCompanionClient, encodeHidEvent, resolveIdbCompanionBinary, IDB_COMPANION_HINT } from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";

class PinchError extends Error {
  constructor(code, message, hint, extra = {}) {
    super(message);
    this.code = code;
    this.hint = hint;
    this.extra = extra;
  }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let activeCleanup = null;
let terminating = false;

export function writeResult(payload, stream) {
  if (!terminating) stream.write(JSON.stringify(payload) + "\n");
}

/** Give an interrupted one-shot transport time to remove its own resources. */
export function installTerminationHandlers() {
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    terminating = true;
    let cleaned = true;
    try { if (activeCleanup) await activeCleanup(); } catch { cleaned = false; }
    process.stderr.write(JSON.stringify({ ok: false, error_code: "ui_action_uncertain",
      error: "Pinch was interrupted; input may have been sent",
      hint: "Inspect the screen and transport before retrying.", sent: "unknown",
      cleanup_confirmed: cleaned }) + "\n", () => process.exit(2));
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
}

export function parseCompanionEndpoint(value) {
  const split = value.lastIndexOf(":");
  const rawHost = value.slice(0, split);
  const rawPort = value.slice(split + 1);
  const host = rawHost.startsWith("[") && rawHost.endsWith("]") ? rawHost.slice(1, -1) : rawHost;
  const port = Number(rawPort);
  if (split < 1 || !host || !/^\d+$/.test(rawPort) || !Number.isInteger(port) || port < 1 || port > 65535 ||
      /[\s/@?#\[\]]/.test(host) || (host.includes(":") && isIP(host) !== 6)) {
    throw new PinchError("invalid_value", "Invalid idb companion endpoint",
      "Use --idb-host HOST --idb-port PORT.", { sent: false });
  }
  return { host, port };
}

async function findExecutable(name) {
  for (const folder of (process.env.PATH ?? "").split(delimiter)) {
    if (!folder) continue;
    const path = join(folder, name);
    try {
      if (!(await stat(path)).isFile()) continue;
      await access(path, constants.X_OK);
      return path;
    } catch { /* Try the next PATH entry. */ }
  }
  return null;
}

function touch(request, action, pointerId, x) {
  return encodeTouch({ action, pointerId: BigInt(pointerId), x: Math.round(x), y: request.y,
    width: request.width, height: request.height, pressure: action === 1 ? 0 : 1 });
}

async function writeTouch(session, bytes) {
  await new Promise((resolve, reject) => {
    let settled = false;
    let timer;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      session.off("error", failed);
      if (error) reject(error); else resolve();
    };
    const failed = (error) => finish(error);
    timer = setTimeout(() => finish(new Error("control socket write timed out")), 3000);
    session.once("error", failed);
    try {
      if (!session.send(bytes, () => finish())) finish(new Error("control socket is closed"));
    } catch (error) { finish(error); }
  });
}

/** Send two downs, paired moves, then two ups. Never resume after a failure. */
export async function performAndroid(request, dependencies = {}) {
  const lookup = dependencies.resolveServer ?? (() => resolveScrcpyServer({ findExecutable }));
  const server = await lookup();
  if (!server.ok) throw new PinchError("tool_missing", `Android pinch needs scrcpy: ${server.reason}`,
                                       SCRCPY_INSTALL_HINT, { tool: "scrcpy", capability: "ui.pinch", sent: false });
  const create = dependencies.createSession ?? ((options) => new ScrcpySession(options));
  const session = create({ adbPath: request.adb, serial: request.target, serverPath: server.serverPath,
                           version: server.version, controlOnly: true });
  activeCleanup = () => session.stop();
  const sleep = dependencies.delay ?? delay;
  let sent = false;
  // An EventEmitter error between writes must not crash the process.
  let failure = null;
  session.on("error", (error) => { failure = error; });
  try {
    await session.start();
    const write = async (action, pointer, x) => {
      if (failure) throw failure;
      // Mark uncertainty immediately before handing bytes to the socket.
      sent = true;
      await writeTouch(session, touch(request, action, pointer, x));
    };
    await write(0, 1, request.x - request.radius);
    await write(0, 2, request.x + request.radius);
    const steps = Math.max(2, Math.ceil(request.duration * 60));
    for (let step = 1; step <= steps; step += 1) {
      await sleep(request.duration * 1000 / steps);
      const radius = request.radius * (1 + (request.scale - 1) * step / steps);
      await write(2, 1, request.x - radius);
      await write(2, 2, request.x + radius);
    }
    const end = request.radius * request.scale;
    await write(1, 2, request.x + end);
    await write(1, 1, request.x - end);
    // scrcpy has no acknowledgement for touch. Let its control queue drain before stopping.
    await sleep(150);
    if (failure) throw failure;
    return "scrcpy";
  } catch (error) {
    if (sent) throw new PinchError("ui_action_uncertain", `Android pinch may have been sent: ${error.message}`,
      "Inspect the screen before repeating the gesture.", { sent: true });
    throw new PinchError("backend_failed", `Could not start Android pinch: ${error.message}`,
      "Check the selected emulator and scrcpy server; no touch was sent.", { sent: false });
  } finally {
    try { await session.stop(); }
    catch (error) {
      throw new PinchError(sent ? "ui_action_uncertain" : "backend_failed",
        `Could not stop Android pinch transport: ${error.message}`,
        sent ? "Inspect the screen before repeating the gesture." : "Check the scrcpy transport; no touch was sent.",
        { sent });
    }
    activeCleanup = null;
  }
}

/** An idb HID call completes with a gRPC response after its input stream ends. */
export async function performIOS(request, dependencies = {}) {
  let owner = null;
  let client = null;
  let sent = false;
  try {
    if (dependencies.client) {
      client = dependencies.client;
    } else if (request.companion) {
      client = new IdbCompanionClient(parseCompanionEndpoint(request.companion));
    } else {
      const resolved = await resolveIdbCompanionBinary();
      if (!resolved.available) throw new PinchError("tool_missing", `iOS pinch needs idb_companion: ${resolved.reason}`,
        IDB_COMPANION_HINT, { tool: "idb_companion", capability: "ui.pinch", sent: false });
      owner = new IdbCompanion({ udid: request.target, binary: resolved.path });
      activeCleanup = () => owner.stop();
      client = await owner.start();
    }
    if (!owner && !dependencies.client) activeCleanup = () => client.close();
    const description = await client.describe({ timeoutMs: 5000 });
    if (description.udid?.toLowerCase() !== request.target.toLowerCase()) {
      throw new PinchError("session_target_mismatch", "The idb companion belongs to a different target",
        "Choose a companion for the selected simulator; no touch was sent.", { sent: false });
    }
    const event = encodeHidEvent({ pinch: { center: { x: request.x, y: request.y },
      scale: request.scale, duration: request.duration, radius: request.radius } });
    sent = true;
    await client.unary("hid", event, { timeoutMs: 10000 });
    return "idb";
  } catch (error) {
    if (error instanceof PinchError) throw error;
    if (sent) throw new PinchError("ui_action_uncertain", `iOS pinch may have been sent: ${error.message}`,
      "Inspect the screen before repeating the gesture; check that idb_companion supports HIDPinch.", { sent: true });
    throw new PinchError("backend_failed", `Could not start iOS pinch: ${error.message}`,
      "Check the selected simulator and idb_companion; no touch was sent.", { sent: false });
  } finally {
    try {
      if (owner) await owner.stop();
      else if (client && !dependencies.client) await client.close();
    } catch (error) {
      throw new PinchError(sent ? "ui_action_uncertain" : "backend_failed",
        `Could not stop iOS pinch transport: ${error.message}`,
        sent ? "Inspect the screen before repeating the gesture." : "Check the idb companion; no touch was sent.",
        { sent });
    }
    activeCleanup = null;
  }
}

async function main() {
  installTerminationHandlers();
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  try {
    const request = JSON.parse(input);
    const backend = request.platform === "android" ? await performAndroid(request) :
      request.platform === "ios" ? await performIOS(request) :
        (() => { throw new PinchError("invalid_value", "Unknown pinch platform", "Choose android or ios."); })();
    writeResult({ ok: true, backend }, process.stdout);
  } catch (error) {
    writeResult({ ok: false, error_code: error.code ?? "backend_failed",
      error: error.message, hint: error.hint ?? "Inspect the target before retrying.", ...error.extra }, process.stderr);
    process.exitCode = 1;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await main();
