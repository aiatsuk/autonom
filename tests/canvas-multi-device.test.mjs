// End-to-end tests of the workspace Canvas (contract 3) against fakes only: a fake adb that
// lists the devices named in a file, a fake input bridge, a fake tools process and a fake
// autonom.py for sessions and boots. No device, emulator, Simulator or browser is involved,
// and every file goes to a temporary AUTONOM_HOME.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { EventEmitter } from "node:events";
import { connect, createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";

import { parseArgs } from "../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs";
import {
  DEFAULT_VIDEO_OPTIONS,
  GrpcMessageParser,
  IdbCompanionClient,
  IdbVideoStream,
  decodeVideoStreamRequest,
  encodeHidEvent,
  grpcFrame,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";
import {
  IosFastSession,
  iosDaemonEnabled,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-device-server.mjs";
import {
  IOSD_SCHEMA,
  daemonPaths,
  pidAlive,
  socketPathFor,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-device-daemon.mjs";
import {
  deviceScope,
  startCanvas as startCanvasInProcess,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const TOKEN = "multi-device-test-token";
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Wl8sAAAAASUVORK5CYII=";
const A = "emulator-5580";
const B = "emulator-5582";
const ID_A = `android~${A}`;
const ID_B = `android~${B}`;

// adb: `devices` lists the serials in FAKE_DEVICES (a JSON list), `emu avd name` answers from
// FAKE_AVD_NAMES, and screencap, wm and get-state answer like a device.
const FAKE_ADB = String.raw`
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify(args) + "\n");
const serial = args[0] === "-s" ? args[1] : null;
const rest = args[0] === "-s" ? args.slice(2) : args;
const line = rest.join(" ");
const listed = () => { try { return JSON.parse(readFileSync(process.env.FAKE_DEVICES, "utf8")); } catch { return []; } };
if (line === "devices" && process.env.FAKE_SLOW_FIRST_DEVICES && !existsSync(process.env.FAKE_SLOW_FIRST_DEVICES)) {
  // The first listing (the restore's) answers late, as adb does on a loaded host.
  writeFileSync(process.env.FAKE_SLOW_FIRST_DEVICES, "");
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(process.env.FAKE_SLOW_MS ?? 1500));
}
if (line === "devices") {
  console.log("List of devices attached");
  for (const item of listed()) console.log(item + "\tdevice");
} else if (rest[0] === "get-state") {
  if (listed().includes(serial)) console.log("device");
  else { console.error("error: device '" + serial + "' not found"); process.exitCode = 1; }
} else if (line === "emu avd name") {
  const names = JSON.parse(process.env.FAKE_AVD_NAMES ?? "{}");
  if (names[serial]) console.log(names[serial] + "\nOK");
  else process.exitCode = 1;
} else if (line === "exec-out screencap -p") {
  process.stdout.write(Buffer.from(process.env.FAKE_PNG, "base64"));
} else if (line === "shell wm size") console.log("Physical size: 1080x2400");
else if (line === "shell wm density") console.log("Physical density: 420");
else process.exitCode = 1;
`;

const FAKE_BRIDGE = String.raw`
import { createInterface } from "node:readline";
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  const result = { ok: true, display: { width: 1080, height: 2400 } };
  process.stdout.write(JSON.stringify({ id: message.id, ok: true, result }) + "\n");
});
`;

const FAKE_TOOLS = String.raw`
import { createInterface } from "node:readline";
createInterface({ input: process.stdin }).on("line", (line) => {
  const { id, op } = JSON.parse(line);
  process.stdout.write(JSON.stringify({ id, ok: true, result: { op } }) + "\n");
}).on("close", () => process.exit(0));
`;

// autonom.py: session start/stop, devices boot/shutdown and logs follow, each logged.
// FAKE_BUSY names serials that already have a live session (started_by FAKE_BUSY_BY).
const FAKE_AUTONOM = String.raw`
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_AUTONOM_LOG, JSON.stringify(args) + "\n");
const flag = (name) => { const at = args.indexOf(name); return at >= 0 ? args[at + 1] : null; };
const verb = args.filter((arg) => ["session", "devices", "logs"].includes(arg))[0];
const action = verb ? args[args.indexOf(verb) + 1] : null;
const target = flag("--serial") ?? flag("--udid");
if (verb === "session" && action === "start") {
  // FAKE_START_GATE holds every start until the test creates that file (bounded at 30 s), so a
  // test sees the start in progress for as long as it needs, however loaded the host is.
  const gate = process.env.FAKE_START_GATE;
  const gateDeadline = Date.now() + 30_000;
  while (gate && !existsSync(gate) && Date.now() < gateDeadline) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
  }
  const busy = JSON.parse(process.env.FAKE_BUSY ?? "[]");
  if (busy.includes(target)) {
    console.error(JSON.stringify({ ok: false, error_code: "session_already_active", error: "busy",
      session_id: "s_primary", target_id: target, started_by: JSON.parse(process.env.FAKE_BUSY_BY ?? "null") }));
    process.exit(2);
  }
  console.log(JSON.stringify({ ok: true, session: { session_id: "s_" + target.replace(/[^a-z0-9]/gi, ""), primary: false } }, null, 2));
} else if (verb === "session" && action === "stop") {
  console.log(JSON.stringify({ ok: true, session: { session_id: flag("--session-id") } }));
} else if (verb === "devices" && action === "boot") {
  const port = flag("--port") ?? "5586";
  const serial = "emulator-" + port;
  const listed = JSON.parse(readFileSync(process.env.FAKE_DEVICES, "utf8"));
  if (!listed.includes(serial)) writeFileSync(process.env.FAKE_DEVICES, JSON.stringify([...listed, serial]));
  console.log(JSON.stringify({ ok: true, avd: flag("--avd"), serial, target_id: serial, already_running: false, port: Number(port) }));
} else if (verb === "devices" && action === "shutdown") {
  console.log(JSON.stringify({ ok: true }));
} else if (verb === "devices" && action === undefined) {
  // The machine listing --discover-bootable reads: FAKE_AVDS names the AVDs; FAKE_AVDS_BROKEN
  // prints no JSON, FAKE_AVDS_HANG_MS answers late.
  if (process.env.FAKE_AVDS_BROKEN) { console.log("not json"); process.exit(1); }
  if (process.env.FAKE_AVDS_HANG_MS) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(process.env.FAKE_AVDS_HANG_MS));
  console.log(JSON.stringify({ ok: true, devices: [], warnings: [], avds: JSON.parse(process.env.FAKE_AVDS ?? "[]") }));
} else if (verb === "logs") {
  setInterval(() => {}, 1000);
} else {
  console.error(JSON.stringify({ ok: false, error_code: "usage_error", error: "fake autonom: " + args.join(" ") }));
  process.exit(2);
}
`;

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

async function waitFor(predicate, timeoutMs = 8000, what = "condition") {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(50);
  }
}

async function makeWorld(t, { devices = [A, B], env = {} } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-multi-"));
  const bin = join(directory, "bin");
  await mkdir(bin);
  const files = {
    fakeAdb: join(directory, "fake-adb.mjs"),
    fakeBridge: join(directory, "fake-bridge.mjs"),
    fakeTools: join(directory, "fake-tools.mjs"),
    fakeAutonom: join(directory, "fake-autonom.mjs"),
    devicesFile: join(directory, "devices.json"),
    adbLog: join(directory, "adb.log"),
    autonomLog: join(directory, "autonom.log"),
    home: join(directory, "autonom-home"),
  };
  await writeFile(files.fakeAdb, `#!${process.execPath}\n${FAKE_ADB}`);
  await chmod(files.fakeAdb, 0o755);
  await writeFile(files.fakeBridge, FAKE_BRIDGE);
  await writeFile(files.fakeTools, FAKE_TOOLS);
  await writeFile(files.fakeAutonom, FAKE_AUTONOM);
  await writeFile(files.devicesFile, JSON.stringify(devices));
  await writeFile(files.adbLog, "");
  await writeFile(files.autonomLog, "");
  const world = {
    directory, ...files, children: [],
    env: {
      PATH: bin,
      HOME: directory,
      AUTONOM_HOME: files.home,
      FAKE_PNG: PNG_BASE64,
      FAKE_DEVICES: files.devicesFile,
      FAKE_ADB_LOG: files.adbLog,
      FAKE_AUTONOM_LOG: files.autonomLog,
      FAKE_AVD_NAMES: JSON.stringify({ [A]: "Autonom_Fast", [B]: "Autonom_Split" }),
      ...env,
    },
  };
  t.after(async () => {
    for (const child of world.children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    await rm(directory, { recursive: true, force: true });
  });
  return world;
}

function canvasArgs(world, args) {
  return [
    CANVAS, "--adb", world.fakeAdb, "--port", "0", "--token", TOKEN, "--transport", "screencap",
    "--python", process.execPath, "--bridge", world.fakeBridge, "--tools", world.fakeTools,
    "--autonom", world.fakeAutonom, ...args,
  ];
}

/** The final line the Canvas prints on start-up, after `Preview at`. */
const STARTED_LINE = /^Open this exact URL in your agent's visible side-panel browser: \S+\n/m;

/** Start the Canvas; resolves once it prints its last start-up line, or with its exit when `expectExit`. */
function startCanvas(world, args, { expectExit = false } = {}) {
  const child = spawn(process.execPath, canvasArgs(world, args),
    { cwd: ROOT, env: world.env, stdio: ["ignore", "pipe", "pipe"] });
  world.children.push(child);
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  const exited = new Promise((resolvePromise) => {
    child.once("exit", (code, signal) => resolvePromise({ code, signal, stdout, stderr }));
  });
  if (expectExit) return exited;
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${stdout}${stderr}`)), 15_000);
    child.stdout.on("data", () => {
      const match = stdout.match(/Preview at (http:\/\/127\.0\.0\.1:(\d+)\/)/);
      // The side-panel line is the last start-up line; wait for it whole, so callers can assert on all of them.
      if (!match || !STARTED_LINE.test(stdout)) return;
      clearTimeout(timer);
      resolvePromise({ child, origin: match[1].slice(0, -1), port: Number(match[2]), exited,
        stdout: () => stdout, stderr: () => stderr });
    });
    exited.then(({ code }) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${stdout}${stderr}`));
    });
  });
}

async function api(canvas, method, path, { body, headers = {}, auth = "bearer" } = {}) {
  const response = await fetch(`${canvas.origin}${path}`, {
    method,
    redirect: "manual",
    headers: {
      ...(auth === "bearer" ? { Authorization: `Bearer ${TOKEN}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch {}
  return { status: response.status, json, text, headers: response.headers };
}

async function liveDevices(canvas, count) {
  return await waitFor(async () => {
    const { json } = await api(canvas, "GET", "/api/devices");
    const live = json.devices.filter((device) => device.state === "live" && device.session);
    return live.length >= count ? json : null;
  }, 15_000, `${count} live devices`);
}

/** A WebSocket handshake; resolves with the status line's code. */
function upgradeStatus(canvas, path) {
  return new Promise((resolvePromise, reject) => {
    const socket = connect(canvas.port, "127.0.0.1");
    let data = "";
    socket.on("error", reject);
    socket.on("connect", () => socket.write([
      `GET ${path} HTTP/1.1`, `Host: 127.0.0.1:${canvas.port}`, "Upgrade: websocket", "Connection: Upgrade",
      "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13", `Authorization: Bearer ${TOKEN}`,
      "", "",
    ].join("\r\n")));
    socket.on("data", (chunk) => {
      data += chunk.toString("latin1");
      if (!data.includes("\r\n")) return;
      socket.destroy();
      resolvePromise({ status: Number(data.split(" ")[1]), text: data });
    });
  });
}

async function autonomCalls(world, verb) {
  const lines = (await readFile(world.autonomLog, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  return lines.filter((args) => !verb || args.join(" ").includes(verb));
}

function discoveryPath(world, port) {
  return join(world.home, "canvas", `${port}.json`);
}

test("a workspace Canvas with no device stays up, writes its files and stops on POST /api/stop", async (t) => {
  const world = await makeWorld(t, { devices: [] });
  const canvas = await startCanvas(world, ["--workspace", "empty"]);
  assert.match(canvas.stdout(), /^autonom Canvas workspace empty ready\nTransport preference: screencap\nPreview at /);
  assert.match(canvas.stdout(), /Open this exact URL in your agent's visible side-panel browser: http:\/\/127\.0\.0\.1:\d+\/#token=/);
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.mode, "workspace");
  assert.equal(workspace.name, "empty");
  assert.equal(workspace.persisted, true);
  assert.equal(workspace.tabs.length, 1);
  assert.deepEqual(workspace.devices, []);
  assert.deepEqual(workspace.limits, { max_tabs: 8, max_tiles: 4, max_devices: 8 });
  // The root aliases need a device.
  const status = await api(canvas, "GET", "/status");
  assert.equal(status.status, 409);
  assert.equal(status.json.error_code, "no_device");
  // Still serving after a few offline polls.
  await sleep(2500);
  assert.equal(canvas.child.exitCode, null);
  // The discovery file: private, complete, no secrets beyond the token it exists to share.
  const path = discoveryPath(world, canvas.port);
  const discovery = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(statSync(path).mode & 0o777, 0o600);
  assert.equal(statSync(join(world.home, "canvas")).mode & 0o777, 0o700);
  assert.deepEqual(Object.keys(discovery).sort(), ["booted", "devices", "mode", "pid", "port", "schema", "started_at",
    "tabs", "token", "url", "workspace"]);
  assert.equal(discovery.schema, "autonom-canvas/v1");
  assert.equal(discovery.pid, canvas.child.pid);
  assert.equal(discovery.url, `http://127.0.0.1:${canvas.port}/`);
  assert.equal(discovery.token, TOKEN);
  assert.equal(discovery.mode, "workspace");
  assert.equal(discovery.workspace, "empty");
  assert.equal(discovery.tabs[0].id, workspace.tabs[0].id);
  const lock = join(world.home, "canvas", "workspaces", "empty.lock");
  assert.deepEqual(JSON.parse(readFileSync(lock, "utf8")), { pid: canvas.child.pid, port: canvas.port });
  // Unauthenticated API calls are refused.
  assert.equal((await api(canvas, "GET", "/api/workspace", { auth: "none" })).status, 401);
  const stop = await api(canvas, "POST", "/api/stop", { body: {} });
  assert.equal(stop.status, 202);
  assert.deepEqual(stop.json, { ok: true, stopping: true });
  const { code } = await canvas.exited;
  assert.equal(code, 0);
  assert.equal(existsSync(path), false, "discovery file removed");
  assert.equal(existsSync(lock), false, "workspace lock removed");
  const saved = JSON.parse(readFileSync(join(world.home, "canvas", "workspaces", "empty.json"), "utf8"));
  assert.equal(saved.schema, "autonom-canvas-workspace/v1");
});

test("two --device on a fake adb: both attach with their own sessions, routes per device", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--device", `android:${A}`, "--device", `android:${B}`]);
  assert.match(canvas.stdout(), /^autonom Canvas workspace default ready/);
  const devices = await liveDevices(canvas, 2);
  assert.equal(devices.primary, ID_A);
  const [first, second] = devices.devices;
  assert.equal(first.id, ID_A);
  assert.equal(first.name, "Autonom_Fast", "the AVD name");
  assert.equal(first.primary, true);
  assert.equal(first.transport, "screencap");
  assert.equal(first.url, `/d/${ID_A}/`);
  assert.deepEqual(first.session, { id: "s_emulator5580", started_by_canvas: true, reused: false });
  assert.equal(first.session_error, null);
  // The first tab grew to two tiles: both devices sit side by side in it.
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.tabs.length, 1);
  assert.equal(workspace.tabs[0].layout, 2);
  assert.equal(first.tab, second.tab);
  // Sessions start alongside, with this Canvas as their starter.
  const starts = await autonomCalls(world, "session start");
  assert.equal(starts.length, 2);
  for (const args of starts) {
    assert.ok(args.includes("--alongside"));
    assert.equal(args[args.indexOf("--started-by") + 1], `canvas:${canvas.port}:${canvas.child.pid}`);
  }

  // Per-device routes.
  const statusA = await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}/status`);
  assert.equal(statusA.status, 200);
  assert.equal(statusA.json.serial, A);
  assert.deepEqual(Object.keys(statusA.json.device),
    ["id", "state", "tab", "primary", "focused", "profile", "fps_cap", "max_size", "scale_factor", "session",
      "profile_applied"]);
  assert.equal(statusA.json.device.id, ID_A);
  const statusB = await api(canvas, "GET", `/d/${encodeURIComponent(ID_B)}/status`);
  assert.equal(statusB.json.serial, B);
  const root = await api(canvas, "GET", "/status");
  assert.equal(root.json.serial, A, "root aliases go to the primary");
  const redirect = await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}?embed=1`, { auth: "none" });
  assert.equal(redirect.status, 308);
  assert.equal(redirect.headers.get("location"), `/d/${encodeURIComponent(ID_A)}/?embed=1`);
  const page = await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}/?embed=1`, { auth: "none" });
  assert.equal(page.status, 200);
  assert.match(page.headers.get("content-security-policy"), /frame-ancestors 'self'/);
  assert.equal(page.headers.get("x-frame-options"), "SAMEORIGIN");
  assert.match(page.text, /<html lang="en" data-embed="1">/);
  assert.ok(page.text.includes(`const BASE="/d/${encodeURIComponent(ID_A)}",EMBED=true,DEVICE_ID="${ID_A}";`));
  const framed = await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}/`, {
    auth: "none", headers: { "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site" } });
  assert.equal(framed.status, 403);
  const sameSite = await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}/`, {
    auth: "none", headers: { "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin" } });
  assert.equal(sameSite.status, 200);
  const frame = await fetch(`${canvas.origin}/d/${encodeURIComponent(ID_B)}/frame`, {
    headers: { Authorization: `Bearer ${TOKEN}` } });
  assert.equal(frame.status, 200);
  assert.equal(frame.headers.get("content-type"), "image/png");
  // WebSocket upgrades reach the device (screencap has no fast transport: 409), an unknown
  // device is 404.
  assert.equal((await upgradeStatus(canvas, `/d/${encodeURIComponent(ID_A)}/ws/video`)).status, 409);
  assert.equal((await upgradeStatus(canvas, "/d/android~nope/ws/control")).status, 404);
  assert.equal((await upgradeStatus(canvas, "/ws/video")).status, 409, "the root alias goes to the primary");
  const unknown = await api(canvas, "GET", "/d/android~nope/status");
  assert.equal(unknown.status, 404);
  assert.equal(unknown.json.error_code, "device_not_found");
  // The workspace page and tab URLs; neither may be framed.
  const shell = await api(canvas, "GET", "/", { auth: "none" });
  assert.equal(shell.status, 200);
  assert.match(shell.headers.get("content-security-policy"), /frame-ancestors 'none'/);
  assert.equal(shell.headers.get("x-frame-options"), "DENY");
  assert.ok(!shell.text.includes(A), "no device data in the markup");
  assert.equal((await api(canvas, "GET", `/c/${second.tab}`, { auth: "none" })).status, 200);
  assert.equal((await api(canvas, "GET", "/c/t_00000000", { auth: "none" })).status, 200, "a closed tab still serves");
  assert.equal((await api(canvas, "GET", "/c/bogus", { auth: "none" })).status, 404);
  assert.equal((await api(canvas, "GET", "/", { auth: "none", headers: { "Sec-Fetch-Dest": "iframe" } })).status, 403);

  // Focus: the focused device runs 60 fps, the other the background profile.
  const focus = await api(canvas, "POST", "/api/focus", { body: { id: ID_B } });
  assert.deepEqual(focus.json, { ok: true, focus: ID_B });
  const after = (await api(canvas, "GET", "/api/devices")).json;
  const byId = Object.fromEntries(after.devices.map((device) => [device.id, device]));
  assert.equal(after.focus, ID_B);
  assert.equal(byId[ID_B].focused, true);
  assert.equal(byId[ID_B].profile, "focused");
  assert.equal(byId[ID_A].profile, "background");
  assert.equal(byId[ID_A].max_size, 1024);
  assert.equal(byId[ID_A].fps_cap, 15, "multipart: --fps (15) stays below the profile cap");
  // On screencap nothing restarts: the applied profile follows at once, with its time.
  assert.equal(byId[ID_A].profile_applied.profile, "background");
  assert.equal(byId[ID_A].profile_applied.max_size, 1024);
  assert.equal(byId[ID_A].profile_applied.fps_cap, 15);
  assert.ok(Number.isFinite(Date.parse(byId[ID_A].profile_applied.applied_at)));
  assert.equal(byId[ID_B].profile_applied.profile, "focused");
  assert.equal((await api(canvas, "POST", "/api/focus", { body: { id: "android~nope" } })).status, 404);
  assert.equal((await api(canvas, "POST", "/api/focus", { body: { id: null } })).json.focus, null);

  // Detach: the slot empties, the Canvas-started session stops, the Canvas keeps serving.
  const detach = await api(canvas, "POST", `/api/devices/${encodeURIComponent(ID_B)}/detach`, { body: {} });
  assert.equal(detach.status, 200);
  assert.deepEqual(detach.json, { ok: true, detached: ID_B, session_stopped: true });
  const stops = await autonomCalls(world, "session stop");
  assert.deepEqual(stops.map((args) => args.slice(-2)), [["--session-id", "s_emulator5582"]]);
  const left = (await api(canvas, "GET", "/api/workspace")).json;
  assert.deepEqual(left.devices.map((device) => device.id), [ID_A]);
  assert.deepEqual(left.tabs.find((tab) => tab.id === second.tab).slots, [{ device: ID_A, present: true }, null]);
  assert.equal((await api(canvas, "GET", `/d/${encodeURIComponent(ID_B)}/status`)).status, 404);
  await canvas.child.kill("SIGTERM");
  assert.equal((await canvas.exited).code, 0);
});

test("API status codes of tabs, devices, boot and focus", async (t) => {
  const world = await makeWorld(t, { devices: [A, B, "emulator-5590"] });
  const canvas = await startCanvas(world, ["--workspace", "codes", "--ephemeral", "--device", `android:${A}`]);
  await liveDevices(canvas, 1);
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.persisted, false);
  const tab1 = workspace.tabs[0].id;
  const post = (path, body = {}) => api(canvas, "POST", path, { body });

  const created = await post("/api/tabs", { name: "Pair", layout: 2 });
  assert.equal(created.status, 201);
  const tab2 = created.json.tab.id;
  assert.match(tab2, /^t_[0-9a-f]{8}$/);
  assert.equal(created.json.tab.url, `/c/${tab2}`);
  assert.equal((await post("/api/tabs", { layout: 9 })).status, 400);
  assert.equal((await post("/api/tabs", { name: "" })).json.error_code, "invalid_value");
  assert.equal((await post(`/api/tabs/${tab2}`, { name: "Renamed" })).json.tab.name, "Renamed");
  assert.equal((await post("/api/tabs/t_ffffffff", { name: "x" })).json.error_code, "tab_not_found");
  assert.equal((await post(`/api/tabs/${tab2}/activate`)).json.active_tab, tab2);
  assert.equal((await post("/api/tabs/t_ffffffff/activate")).status, 404);

  // Attach errors.
  const attach = (body) => post("/api/devices", body);
  assert.equal((await attach({ platform: "android" })).status, 400);
  assert.equal((await attach({ platform: "windows", target: "x" })).json.error_code, "invalid_value");
  const missing = await attach({ platform: "android", target: "emulator-5999", tab: tab2 });
  assert.equal(missing.status, 404);
  assert.equal(missing.json.error_code, "target_not_found");
  assert.equal((await attach({ platform: "android", target: B, tab: "t_ffffffff" })).json.error_code, "tab_not_found");
  const again = await attach({ platform: "android", target: A, tab: tab1 });
  assert.equal(again.status, 200);
  assert.equal(again.json.attached, false);
  const other = await attach({ platform: "android", target: A, tab: tab2 });
  assert.equal(other.status, 409);
  assert.equal(other.json.error_code, "device_in_other_tab");
  assert.equal(other.json.device, ID_A);
  assert.equal(other.json.tab, tab1);
  assert.equal(typeof other.json.hint, "string");
  assert.equal((await attach({ platform: "android", target: B, tab: tab1 })).json.error_code, "tab_full");
  const ok = await attach({ platform: "android", target: B, tab: tab2, slot: 1 });
  assert.equal(ok.status, 201);
  assert.equal(ok.json.attached, true);
  assert.equal(ok.json.device.slot, 1);
  assert.equal((await attach({ platform: "android", target: "emulator-5590", tab: tab2, slot: 1 })).json.error_code,
    "slot_taken");
  assert.equal((await attach({ platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5" })).json.error_code,
    "platform_unavailable");

  // Layout below the devices in the tab; slot clear of a live device.
  assert.equal((await attach({ platform: "android", target: "emulator-5590", tab: tab2, slot: 0 })).status, 201);
  assert.equal((await post(`/api/tabs/${tab2}`, { layout: 1 })).json.error_code, "layout_too_small");
  const detached = await post(`/api/devices/${encodeURIComponent("android~emulator-5590")}/detach`);
  assert.deepEqual(detached.json, { ok: true, detached: "android~emulator-5590", session_stopped: true });
  assert.equal((await post(`/api/tabs/${tab2}/slots/1/clear`)).json.error_code, "slot_live");
  assert.equal((await post(`/api/tabs/${tab2}/slots/0/clear`)).status, 200);

  // Move: context and session kept.
  const before = (await api(canvas, "GET", "/api/devices")).json.devices.find((d) => d.id === ID_B);
  const move = await post(`/api/devices/${encodeURIComponent(ID_B)}/move`, { tab: tab1 });
  assert.equal(move.json.error_code, "tab_full");
  const moved = await post(`/api/devices/${encodeURIComponent(ID_B)}/move`, { tab: tab2, slot: 0 });
  assert.equal(moved.status, 200);
  assert.equal(moved.json.device.slot, 0);
  assert.deepEqual(moved.json.device.session, before.session);
  assert.equal(moved.json.device.attached_at, before.attached_at);
  assert.equal((await post("/api/devices/android~nope/move", { tab: tab2 })).status, 404);
  assert.equal((await post(`/api/devices/${encodeURIComponent(ID_B)}/move`, { tab: "t_ffffffff" })).status, 404);

  // Reconnect, detach of unknown devices, boot, focus.
  const reconnect = await post(`/api/devices/${encodeURIComponent(ID_B)}/reconnect`);
  assert.equal(reconnect.status, 202);
  await liveDevices(canvas, 2);
  assert.equal((await post("/api/devices/android~nope/reconnect")).json.error_code, "device_not_found");
  assert.equal((await post("/api/devices/android~nope/detach")).json.error_code, "device_not_found");
  const boot = await post("/api/boot", { kind: "avd", name: "Other" });
  assert.equal(boot.status, 403);
  assert.equal(boot.json.error_code, "boot_not_allowed");
  assert.equal((await post("/api/boot", { kind: "floppy" })).status, 400);
  assert.equal((await api(canvas, "GET", "/api/boot/b_00000000")).json.error_code, "job_not_found");
  assert.equal((await api(canvas, "GET", "/api/nothing")).json.error_code, "not_found");
  const targets = (await api(canvas, "GET", "/api/targets")).json;
  assert.deepEqual(targets.running.map((target) => [target.target, target.attached, target.attachable]),
    [[A, true, false], [B, true, false], ["emulator-5590", false, true]]);
  assert.deepEqual(targets.bootable, []);

  // Close a tab: its devices are detached, their sessions stopped.
  const close = await post(`/api/tabs/${tab2}/close`);
  assert.deepEqual({ ...close.json, created: null }, { ok: true, closed: tab2, detached: [ID_B], created: null });
  assert.equal((await post(`/api/tabs/${tab2}/close`)).status, 404);
  // Closing the last tab creates a fresh one; its device is detached too.
  const last = await post(`/api/tabs/${tab1}/close`);
  assert.equal(last.json.created.name, "Canvas 1");
  assert.deepEqual(last.json.detached, [ID_A]);
  assert.equal(canvas.child.exitCode, null, "closing the last tab never stops the Canvas");
  const status = await api(canvas, "GET", "/status");
  assert.equal(status.json.error_code, "no_device");

  // Cookie sessions need the CSRF value on POST.
  const login = await fetch(`${canvas.origin}/auth`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: TOKEN }) });
  const cookie = login.headers.get("set-cookie").split(";", 1)[0];
  const { csrf } = await login.json();
  const refused = await api(canvas, "POST", "/api/tabs", { auth: "none", body: {}, headers: { Cookie: cookie } });
  assert.equal(refused.status, 403);
  assert.equal(refused.json.error_code, "csrf_rejected");
  const accepted = await api(canvas, "POST", "/api/tabs", {
    auth: "none", body: {}, headers: { Cookie: cookie, "X-Autonom-CSRF": csrf } });
  assert.equal(accepted.status, 201);
  canvas.child.kill("SIGTERM");
  await canvas.exited;
  assert.equal(existsSync(join(world.home, "canvas", "workspaces", "codes.json")), false, "ephemeral writes no file");
});

test("stop ends the Canvas's own sessions; a restart restores the tabs and re-attaches", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_BUSY: JSON.stringify([A]) } });
  const canvas = await startCanvas(world, ["--device", `android:${A}`, "--device", `android:${B}`]);
  const devices = (await liveDevices(canvas, 2)).devices;
  const byId = Object.fromEntries(devices.map((device) => [device.id, device]));
  // A already had a live session (the agent's primary): reused, never stopped by the Canvas.
  assert.deepEqual(byId[ID_A].session, { id: "s_primary", started_by_canvas: false, reused: true });
  assert.deepEqual(byId[ID_B].session, { id: "s_emulator5582", started_by_canvas: true, reused: false });
  const tabs = (await api(canvas, "GET", "/api/workspace")).json.tabs;
  canvas.child.kill("SIGINT");
  assert.equal((await canvas.exited).code, 0);
  const stops = await waitFor(async () => {
    const calls = await autonomCalls(world, "session stop");
    return calls.length ? calls : null;
  }, 5000, "the detached session stop");
  assert.deepEqual(stops.map((args) => args.at(-1)), ["s_emulator5582"]);
  const saved = JSON.parse(readFileSync(join(world.home, "canvas", "workspaces", "default.json"), "utf8"));
  assert.deepEqual(saved.tabs.map((tab) => tab.slots.map((slot) => slot?.target ?? null)), [[A, B]],
    "the file holds the devices attached at stop");

  // Restart without --device: the tab comes back with its id, both devices attach again.
  const again = await startCanvas(world, ["--workspace", "default"]);
  const restored = await liveDevices(again, 2);
  const restoredTabs = (await api(again, "GET", "/api/workspace")).json.tabs;
  assert.deepEqual(restoredTabs.map((tab) => tab.id), tabs.map((tab) => tab.id));
  assert.deepEqual(restored.devices.map((device) => [device.id, device.tab]).sort(),
    devices.map((device) => [device.id, device.tab]).sort());
  // A target that disappears goes offline, and comes back live.
  await writeFile(world.devicesFile, JSON.stringify([A]));
  await waitFor(async () => (await api(again, "GET", "/api/devices")).json.devices
    .find((device) => device.id === ID_B)?.state === "offline", 8000, "offline");
  const offline = await api(again, "GET", `/d/${encodeURIComponent(ID_B)}/status`);
  assert.equal(offline.status, 200);
  assert.equal(offline.json.device.state, "offline");
  assert.equal((await api(again, "GET", `/d/${encodeURIComponent(ID_B)}/frame`)).json.error_code, "device_not_ready");
  await writeFile(world.devicesFile, JSON.stringify([A, B]));
  await waitFor(async () => (await api(again, "GET", "/api/devices")).json.devices
    .find((device) => device.id === ID_B)?.state === "live", 8000, "live again");
  again.child.kill("SIGTERM");
  await again.exited;
});

test("a stop while session starts are still running ends those sessions too", async (t) => {
  const gate = join(tmpdir(), `autonom-start-gate-${process.pid}-${Date.now()}`);
  t.after(() => rm(gate, { force: true }));
  const world = await makeWorld(t, { env: { FAKE_START_GATE: gate } });
  const canvas = await startCanvas(world, ["--ephemeral", "--device", `android:${A}`, "--device", `android:${B}`]);
  await waitFor(async () => (await autonomCalls(world, "session start")).length >= 2, 8000, "two session starts");
  const stop = await api(canvas, "POST", "/api/stop", { body: {} });
  assert.equal(stop.status, 202);
  // Both starts are still held: the stop has begun while they run. Let them answer now.
  assert.deepEqual(await autonomCalls(world, "session stop"), [], "no stop before the starts answer");
  await writeFile(gate, "");
  assert.equal((await canvas.exited).code, 0);
  const stops = await waitFor(async () => {
    const calls = await autonomCalls(world, "session stop");
    return calls.length >= 2 ? calls : null;
  }, 8000, "both session stops");
  assert.deepEqual(stops.map((args) => args.at(-1)).sort(), ["s_emulator5580", "s_emulator5582"]);
});

test("two detaches of one device at once stop its session once and answer the same", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--ephemeral", "--device", `android:${A}`, "--device", `android:${B}`]);
  await liveDevices(canvas, 2);
  const path = `/api/devices/${encodeURIComponent(ID_B)}/detach`;
  const [first, second] = await Promise.all([
    api(canvas, "POST", path, { body: {} }),
    api(canvas, "POST", path, { body: {} }),
  ]);
  for (const answer of [first, second]) {
    assert.equal(answer.status, 200);
    assert.deepEqual(answer.json, { ok: true, detached: ID_B, session_stopped: true });
  }
  const stops = await autonomCalls(world, "session stop");
  assert.deepEqual(stops.map((args) => args.at(-1)), ["s_emulator5582"]);
  assert.equal((await api(canvas, "POST", path, { body: {} })).status, 404, "a later detach finds nothing");
  canvas.child.kill("SIGTERM");
  assert.equal((await canvas.exited).code, 0);
});

test("a detach while the device's session is still starting waits for it and stops it", async (t) => {
  // A restored device turns live before its session start answers (live run 2026-10-07: the
  // detach right after a restart answered session_stopped false and the session ended later).
  // The start is held on a gate file rather than a fixed delay: with a 2.5 s delay a loaded host
  // turned B live only after the start had answered, and the test saw a session already linked.
  const gate = join(tmpdir(), `autonom-start-gate-${process.pid}-${Date.now()}`);
  t.after(() => rm(gate, { force: true }));
  const world = await makeWorld(t, { env: { FAKE_START_GATE: gate } });
  const canvas = await startCanvas(world, ["--ephemeral", "--split", "--device", `android:${B}`]);
  await waitFor(async () => (await autonomCalls(world, "session start")).length === 1, 8000, "the session start");
  const live = await waitFor(async () => (await api(canvas, "GET", "/api/devices")).json.devices
    .find((device) => device.id === ID_B && device.state === "live") ?? null, 8000, "B live");
  assert.equal(live.session, null, "the session start is still running");
  let answered = false;
  const detaching = api(canvas, "POST", `/api/devices/${encodeURIComponent(ID_B)}/detach`, { body: {} })
    .finally(() => { answered = true; });
  // The detach has begun once B is gone from the list; it must then wait for the held start.
  await waitFor(async () => !(await api(canvas, "GET", "/api/devices")).json.devices
    .some((device) => device.id === ID_B), 8000, "B detaching");
  assert.equal(answered, false, "the detach waits for the session start");
  assert.deepEqual(await autonomCalls(world, "session stop"), [], "nothing to stop before the start answers");
  await writeFile(gate, "");
  const detach = await detaching;
  assert.equal(detach.status, 200);
  assert.deepEqual(detach.json, { ok: true, detached: ID_B, session_stopped: true });
  const stops = await autonomCalls(world, "session stop");
  assert.deepEqual(stops.map((args) => args.at(-1)), ["s_emulator5582"], "stopped once, before the answer");
  canvas.child.kill("SIGTERM");
  assert.equal((await canvas.exited).code, 0);
});

test("absent devices: placed but not running, attached once their target appears", async (t) => {
  const world = await makeWorld(t, { devices: [A] });
  const canvas = await startCanvas(world, ["--device", `android:${A}`, "--device", `android:${B}`]);
  await liveDevices(canvas, 1);
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.deepEqual(workspace.absent.map((ref) => [ref.id, ref.bootable]), [[ID_B, null]]);
  const tab = workspace.tabs.find((item) => item.id === workspace.absent[0].tab);
  assert.deepEqual(tab.slots, [{ device: ID_A, present: true }, { device: ID_B, present: false }]);
  await writeFile(world.devicesFile, JSON.stringify([A, B]));
  await liveDevices(canvas, 2);
  canvas.child.kill("SIGTERM");
  await canvas.exited;
});

test("boot jobs: an allowed AVD boots, attaches as booted by the Canvas and is shut down at stop", async (t) => {
  const world = await makeWorld(t, { devices: [] });
  const canvas = await startCanvas(world, ["--split", "--bootable", "avd:Autonom_Split2@5586", "--shutdown-booted"]);
  const targets = (await api(canvas, "GET", "/api/targets")).json;
  assert.deepEqual(targets.bootable, [{ kind: "avd", name: "Autonom_Split2", port: 5586, udid: null,
    label: "Autonom_Split2", runtime: null, running: false, target: null, job: null }]);
  const started = await api(canvas, "POST", "/api/boot", { body: { kind: "avd", name: "Autonom_Split2" } });
  assert.equal(started.status, 202);
  assert.match(started.json.job.id, /^b_[0-9a-f]{8}$/);
  const repeat = await api(canvas, "POST", "/api/boot", { body: { kind: "avd", name: "Autonom_Split2" } });
  assert.ok(repeat.status === 202 || (repeat.status === 200 && repeat.json.job.id === started.json.job.id),
    "a running job of the same entry is answered, not started twice");
  const job = await waitFor(async () => {
    const { json } = await api(canvas, "GET", `/api/boot/${started.json.job.id}`);
    return json.job.finished_at ? json.job : null;
  }, 10_000, "the boot job");
  assert.equal(job.state, "done", JSON.stringify(job));
  assert.equal(job.target, "emulator-5586");
  assert.equal(job.device, "android~emulator-5586");
  assert.equal(job.already_running, false);
  const boots = await autonomCalls(world, "devices boot");
  assert.deepEqual(boots[0].slice(boots[0].indexOf("devices")), ["devices", "boot", "--avd", "Autonom_Split2",
    "--port", "5586", "--timeout", "180"]);
  const device = (await liveDevices(canvas, 1)).devices[0];
  assert.equal(device.booted_by_canvas, true);
  const discovery = JSON.parse(readFileSync(discoveryPath(world, canvas.port), "utf8"));
  assert.deepEqual(discovery.booted.map((item) => item.target), ["emulator-5586"]);
  canvas.child.kill("SIGTERM");
  const { stderr } = await canvas.exited;
  assert.match(stderr, /Shutting down android~emulator-5586 \(booted by this Canvas\)/);
  const shutdowns = await waitFor(async () => {
    const calls = await autonomCalls(world, "devices shutdown");
    return calls.length ? calls : null;
  }, 5000, "the shutdown");
  assert.deepEqual(shutdowns[0].slice(-4), ["--serial", "emulator-5586", "devices", "shutdown"]);
});

// xcrun simctl: nothing booted; one available iOS Simulator, one watchOS one (never bootable here).
const SIM_UDID = "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5";
const FAKE_XCRUN = String.raw`
const args = process.argv.slice(2).join(" ");
if (args === "simctl list devices booted -j") {
  console.log(JSON.stringify({ devices: {} }));
} else if (args === "simctl list devices available -j") {
  console.log(JSON.stringify({ devices: {
    "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
      { udid: "${SIM_UDID}", name: "iPhone 17", state: "Shutdown", isAvailable: true },
      { udid: "11111111-2222-3333-4444-555555555555", name: "Broken", state: "Shutdown", isAvailable: false }],
    "com.apple.CoreSimulator.SimRuntime.watchOS-12-0": [
      { udid: "66666666-7777-8888-9999-000000000000", name: "Watch", state: "Shutdown", isAvailable: true }],
  } }));
} else {
  process.exit(1);
}
`;

test("--discover-bootable: every AVD and iOS Simulator of the machine can be booted", async (t) => {
  const world = await makeWorld(t, { devices: [A], env: {
    FAKE_AVDS: JSON.stringify(["Autonom_Fast", "Autonom_Split2", "Pixel_9", "bad name"]) } });
  const fakeXcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(fakeXcrun, `#!${process.execPath}
${FAKE_XCRUN}`);
  await chmod(fakeXcrun, 0o755);
  const canvas = await startCanvas(world, ["--split", "--simctl", fakeXcrun, "--discover-bootable",
    "--bootable", "avd:Autonom_Split2@5586"]);
  const targets = (await api(canvas, "GET", "/api/targets")).json;
  // The --bootable entry first with its port; then the others; a running AVD shows as running.
  assert.deepEqual(targets.bootable.map((item) => [item.kind, item.label, item.port, item.runtime, item.running]), [
    ["avd", "Autonom_Split2", 5586, null, false],
    ["avd", "Autonom_Fast", null, null, true],
    ["avd", "Pixel_9", null, null, false],
    ["simulator", "iPhone 17", null, "iOS 27.0", false],
  ]);
  assert.equal(targets.bootable[3].udid, SIM_UDID);
  // A discovered AVD boots without a port: `devices boot` picks one.
  const started = await api(canvas, "POST", "/api/boot", { body: { kind: "avd", name: "Pixel_9", attach: false } });
  assert.equal(started.status, 202, JSON.stringify(started.json));
  const job = await waitFor(async () => {
    const { json } = await api(canvas, "GET", `/api/boot/${started.json.job.id}`);
    return json.job.finished_at ? json.job : null;
  }, 10_000, "the boot job");
  assert.equal(job.state, "done", JSON.stringify(job));
  const boots = await autonomCalls(world, "devices boot");
  assert.deepEqual(boots[0].slice(boots[0].indexOf("devices")), ["devices", "boot", "--avd", "Pixel_9", "--timeout", "180"]);
  // What the machine does not list stays refused.
  for (const body of [{ kind: "avd", name: "Nope" }, { kind: "simulator", udid: "66666666-7777-8888-9999-000000000000" }]) {
    const refused = await api(canvas, "POST", "/api/boot", { body });
    assert.equal(refused.status, 403);
    assert.equal(refused.json.error_code, "boot_not_allowed");
  }
  canvas.child.kill("SIGTERM");
  await canvas.exited;
});

test("an attach with no tab named grows the active tab to the next layout", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--split"]);
  for (const target of [A, B]) {
    const attached = await api(canvas, "POST", "/api/devices", { body: { platform: "android", target } });
    assert.equal(attached.status, 201, JSON.stringify(attached.json));
  }
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.tabs.length, 1);
  assert.equal(workspace.tabs[0].layout, 2);
  assert.deepEqual(workspace.tabs[0].slots.map((slot) => slot?.device ?? null), [ID_A, `android~${B}`]);
  // The same device again stays where it is.
  const again = await api(canvas, "POST", "/api/devices", { body: { platform: "android", target: A } });
  assert.equal(again.status, 200);
  assert.equal(again.json.attached, false);
  assert.equal((await api(canvas, "GET", "/api/workspace")).json.tabs[0].layout, 2);
  // A refused attach makes no room: no grown layout, no new tab.
  const missing = await api(canvas, "POST", "/api/devices", { body: { platform: "android", target: "emulator-5999" } });
  assert.equal(missing.status, 404);
  assert.equal(missing.json.error_code, "target_not_found");
  const after = (await api(canvas, "GET", "/api/workspace")).json;
  assert.deepEqual(after.tabs.map((tab) => tab.layout), [2]);
  canvas.child.kill("SIGTERM");
  await canvas.exited;
});

test("attaches at the same moment each find room: none is refused tab_full", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--split"]);
  const answers = await Promise.all([A, B].map((target) =>
    api(canvas, "POST", "/api/devices", { body: { platform: "android", target } })));
  assert.deepEqual(answers.map((answer) => answer.status), [201, 201], JSON.stringify(answers.map((a) => a.json)));
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.deepEqual(workspace.tabs.map((tab) => tab.slots.filter(Boolean).length), [2]);
  canvas.child.kill("SIGTERM");
  await canvas.exited;
});

test("--discover-bootable: a broken or slow machine listing leaves the --bootable list", async (t) => {
  for (const env of [{ FAKE_AVDS_BROKEN: "1" }, { FAKE_AVDS_HANG_MS: "8000", FAKE_AVDS: JSON.stringify(["Pixel_9"]) }]) {
    const world = await makeWorld(t, { devices: [], env });
    const canvas = await startCanvas(world, ["--split", "--discover-bootable", "--bootable", "avd:Autonom_Split2@5586"]);
    const started = Date.now();
    const targets = (await api(canvas, "GET", "/api/targets")).json;
    assert.ok(Date.now() - started < 6000, `GET /api/targets took ${Date.now() - started} ms`);
    assert.deepEqual(targets.bootable.map((item) => item.label), ["Autonom_Split2"], JSON.stringify(env));
    const refused = await api(canvas, "POST", "/api/boot", { body: { kind: "avd", name: "Pixel_9" } });
    assert.equal(refused.json.error_code, "boot_not_allowed");
    canvas.child.kill("SIGTERM");
    await canvas.exited;
  }
});

test("the single-device Canvas keeps its behaviour and answers the additive routes", async (t) => {
  const world = await makeWorld(t, { devices: [A] });
  const canvas = await startCanvas(world, ["--serial", A]);
  assert.match(canvas.stdout(), new RegExp(`^autonom Canvas ready for android:${A}\\nTransport preference: screencap\\nTransport: screencap\\n`));
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.mode, "single");
  assert.deepEqual(workspace.tabs, []);
  assert.deepEqual(workspace.devices.map((device) => [device.id, device.tab, device.session]), [[ID_A, null, null]]);
  const refused = await api(canvas, "POST", "/api/tabs", { body: {} });
  assert.equal(refused.status, 409);
  assert.equal(refused.json.error_code, "single_device_canvas");
  assert.equal((await api(canvas, "POST", "/api/devices", { body: { platform: "android", target: B } })).json.error_code,
    "single_device_canvas");
  const status = await api(canvas, "GET", "/status");
  assert.equal("device" in status.json, false, "no device key in /status");
  assert.equal((await api(canvas, "GET", `/d/${encodeURIComponent(ID_A)}/status`)).json.serial, A);
  assert.equal((await api(canvas, "GET", "/c/t_00000000")).status, 404);
  assert.deepEqual((await api(canvas, "GET", "/nothing")).json, { error: "Not found" }, "old routes keep {error}");
  const page = await api(canvas, "GET", "/", { auth: "none" });
  assert.ok(page.text.includes('const BASE="",EMBED=false,DEVICE_ID="android~emulator-5580";'));
  assert.equal(page.headers.get("x-frame-options"), "DENY");
  assert.deepEqual(await autonomCalls(world, "session"), [], "no automatic session");
  const discovery = JSON.parse(readFileSync(discoveryPath(world, canvas.port), "utf8"));
  assert.equal(discovery.mode, "single");
  assert.equal(discovery.workspace, null);
  assert.deepEqual(discovery.devices, [{ id: ID_A, platform: "android", target: A, tab: null }]);
  assert.equal((await api(canvas, "POST", "/api/stop", { body: {} })).status, 202);
  assert.equal((await canvas.exited).code, 0);
  assert.equal(existsSync(discoveryPath(world, canvas.port)), false);
});

test("a second Canvas on the same workspace and a busy port are refused with exit 2", async (t) => {
  const world = await makeWorld(t, { devices: [] });
  const canvas = await startCanvas(world, ["--workspace", "solo"]);
  const second = await startCanvas(world, ["--workspace", "solo"], { expectExit: true });
  assert.equal(second.code, 2);
  const refusal = JSON.parse(second.stderr.trim().split("\n").at(-1));
  assert.equal(refusal.error_code, "workspace_in_use");
  assert.equal(refusal.port, canvas.port);
  assert.equal(refusal.pid, canvas.child.pid);
  assert.equal(typeof refusal.hint, "string");
  // A busy port.
  const blocker = createNetServer();
  await new Promise((resolvePromise) => blocker.listen(0, "127.0.0.1", resolvePromise));
  t.after(() => blocker.close());
  const busy = await startCanvas(world, ["--workspace", "other", "--port", String(blocker.address().port)],
    { expectExit: true });
  assert.equal(busy.code, 2, busy.stderr);
  const busyRefusal = JSON.parse(busy.stderr.trim().split("\n").at(-1));
  assert.equal(busyRefusal.error_code, "port_unavailable");
  assert.equal(busyRefusal.hint, "Pass --port 0 or another port");
  assert.equal(existsSync(join(world.home, "canvas", "workspaces", "other.lock")), false, "the lock is released");
  canvas.child.kill("SIGTERM");
  await canvas.exited;
});

test("a device attached while the start-up restore lists targets stays present in its slot", async (t) => {
  // Live run 2026-10-08: the restore marked every placed device absent after its slow target
  // listing, so a device attached meanwhile showed "is not running" while it was live.
  const world = await makeWorld(t, { devices: [A] });
  world.env.FAKE_SLOW_FIRST_DEVICES = join(world.directory, "slow-devices-done");
  world.env.FAKE_SLOW_MS = "1500";
  const canvas = await startCanvas(world, ["--workspace", "race"]);
  // The restore is inside its slow listing; this attach lists, places and attaches meanwhile.
  await waitFor(async () => (existsSync(world.env.FAKE_SLOW_FIRST_DEVICES) ? true : null), 5000, "the restore listing");
  const attached = await api(canvas, "POST", "/api/devices", { body: { platform: "android", target: A } });
  assert.equal(attached.status, 201, attached.text);
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 2500));
  const { json } = await api(canvas, "GET", "/api/workspace");
  const slots = json.tabs.flatMap((tab) => tab.slots).filter(Boolean);
  assert.deepEqual(slots, [{ device: ID_A, present: true }]);
  assert.deepEqual(json.absent ?? [], []);
  const device = (await api(canvas, "GET", "/api/devices")).json.devices.find((item) => item.id === ID_A);
  assert.equal(device?.state, "live");
});

test("a crash in one device's scope fails and restores only that device", async (t) => {
  const world = await makeWorld(t);
  for (const [key, value] of Object.entries(world.env)) {
    if (key === "PATH" || key === "HOME") continue;
    const before = process.env[key];
    process.env[key] = value;
    t.after(() => {
      if (before === undefined) delete process.env[key];
      else process.env[key] = before;
    });
  }
  const options = parseArgs([
    "--adb", world.fakeAdb, "--port", "0", "--token", TOKEN, "--transport", "screencap",
    "--python", process.execPath, "--bridge", world.fakeBridge, "--tools", world.fakeTools,
    "--autonom", world.fakeAutonom, "--ephemeral", "--device", `android:${A}`, "--device", `android:${B}`,
  ]);
  const log = [];
  const commandLog = { begin: (fields) => ({ end: (end) => log.push({ ...fields, ...end }) }) };
  const lines = [];
  const originalError = console.error;
  const originalLog = console.log;
  console.error = (line) => lines.push(String(line));
  console.log = () => {};
  t.after(() => {
    console.error = originalError;
    console.log = originalLog;
  });
  const handle = await startCanvasInProcess(options, { commandLog },
    { processHandlers: false, env: { ...process.env, AUTONOM_HOME: world.home } });
  t.after(() => handle.stop());
  const canvas = { origin: `http://127.0.0.1:${handle.port}` };
  await liveDevices(canvas, 2);
  deviceScope.run({ id: ID_A }, () => handle.onUncaught(new Error("boom in A")));
  assert.ok(lines.includes(`Device ${ID_A} failed: boom in A`), lines.join("\n"));
  let devices = (await api(canvas, "GET", "/api/devices")).json.devices;
  assert.deepEqual(devices.map((device) => [device.id, device.state]), [[ID_A, "failed"], [ID_B, "live"]]);
  assert.equal(devices[0].error, "boom in A");
  assert.equal((await api(canvas, "GET", `/d/${encodeURIComponent(ID_B)}/status`)).status, 200, "B keeps serving");
  assert.ok(log.some((entry) => entry.device === ID_A && entry.errorCode === "device_failed"));
  // Restored after about a second, with its session link kept.
  devices = await waitFor(async () => {
    const { json } = await api(canvas, "GET", "/api/devices");
    return json.devices.every((device) => device.state === "live") ? json.devices : null;
  }, 8000, "the restore");
  assert.equal(devices[0].session.id, "s_emulator5580");
  assert.equal((await autonomCalls(world, "session start")).length, 2, "no new session for a restore");
  // An error outside any device scope is logged and the Canvas keeps serving.
  handle.onUncaught(new Error("stray"));
  assert.ok(lines.includes("Canvas error: stray"));
  assert.equal((await api(canvas, "GET", "/api/workspace")).status, 200);
  // Three failures within five minutes: it stays failed until a reconnect.
  for (let i = 0; i < 3; i += 1) deviceScope.run({ id: ID_B }, () => handle.onUncaught(new Error(`again ${i}`)));
  await sleep(1500);
  assert.equal((await api(canvas, "GET", "/api/devices")).json.devices.find((d) => d.id === ID_B).state, "failed");
  assert.equal((await api(canvas, "POST", `/api/devices/${encodeURIComponent(ID_B)}/reconnect`, { body: {} })).status, 202);
  await liveDevices(canvas, 2);
  await handle.stop();
});

/** A video call that answers Stop by closing, and records every Start it was sent. */
class AcknowledgingCall extends EventEmitter {
  writable = true;
  destroyed = false;
  closed = false;
  requests = [];

  write(chunk, callback) {
    const request = decodeVideoStreamRequest(new GrpcMessageParser().push(chunk)[0]);
    this.requests.push(request);
    callback?.();
    if (request.type === "stop") setImmediate(() => this.close());
    return true;
  }

  end() {
    this.writable = false;
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.destroyed = true;
    setImmediate(() => this.emit("close"));
  }

  pause() {}

  resume() {}
}

test("iOS: restart({options}) starts the next call with new fps and scale; without options, unchanged", async () => {
  const calls = [];
  const client = { openCall: () => calls.at(calls.push(new AcknowledgingCall()) - 1) };
  const stream = new IdbVideoStream(client, { fps: 60, avgBitrate: 1_000_000 });
  await stream.start();
  await stream.restart({ timeoutMs: 500, options: { fps: 30, scaleFactor: 0.5, avgBitrate: 1_000_000 } });
  assert.equal(calls.length, 2);
  const second = calls[1].requests[0];
  assert.equal(second.type, "start");
  assert.equal(second.fps, 30);
  assert.equal(second.scaleFactor, 0.5);
  assert.deepEqual({ fps: stream.options.fps, scaleFactor: stream.options.scaleFactor }, { fps: 30, scaleFactor: 0.5 });
  await stream.restart({ timeoutMs: 500 });
  assert.equal(calls[2].requests[0].fps, 30, "a plain restart keeps the options in use");
  // Bad options are refused before the running call is stopped.
  await assert.rejects(stream.restart({ options: { scaleFactor: 2 } }), RangeError);
  assert.equal(calls.length, 3);
  assert.equal(calls[2].closed, false);
  // On change (fps 0), as the Display menu's applyProfile asks: the Start carries fps 0 and
  // the stream's options say 0, also through a later plain restart.
  await stream.restart({ timeoutMs: 500, options: { fps: 0, scaleFactor: 0.5, avgBitrate: 1_000_000 } });
  assert.equal(calls[3].requests[0].fps, 0);
  assert.equal(calls[3].requests[0].scaleFactor, 0.5);
  assert.equal(stream.options.fps, 0);
  await stream.restart({ timeoutMs: 500 });
  assert.equal(calls[4].requests[0].fps, 0, "a plain restart keeps frames on change");
  await stream.stop({ timeoutMs: 500 });
});

test("display choice in a workspace (QW-05): per device, reported as display_choice, the lower of it and the background profile, kept by a restore", async (t) => {
  const world = await makeWorld(t);
  for (const [key, value] of Object.entries(world.env)) {
    if (key === "PATH" || key === "HOME") continue;
    const before = process.env[key];
    process.env[key] = value;
    t.after(() => {
      if (before === undefined) delete process.env[key];
      else process.env[key] = before;
    });
  }
  // --fps 60 so the multipart cap does not hide the profile's frame rate; --max-size 1600 is
  // the full size Half halves.
  const options = parseArgs([
    "--adb", world.fakeAdb, "--port", "0", "--token", TOKEN, "--transport", "screencap", "--fps", "60",
    "--max-size", "1600", "--python", process.execPath, "--bridge", world.fakeBridge, "--tools", world.fakeTools,
    "--autonom", world.fakeAutonom, "--ephemeral", "--device", `android:${A}`, "--device", `android:${B}`,
  ]);
  const log = [];
  const commandLog = { begin: (fields) => ({ end: (end) => log.push({ ...fields, ...end }) }) };
  const originalError = console.error;
  const originalLog = console.log;
  console.error = () => {};
  console.log = () => {};
  t.after(() => {
    console.error = originalError;
    console.log = originalLog;
  });
  const handle = await startCanvasInProcess(options, { commandLog },
    { processHandlers: false, env: { ...process.env, AUTONOM_HOME: world.home } });
  t.after(() => handle.stop());
  const canvas = { origin: `http://127.0.0.1:${handle.port}` };
  await liveDevices(canvas, 2);
  const devices = async () => Object.fromEntries((await api(canvas, "GET", "/api/devices")).json.devices
    .map((device) => [device.id, device]));
  const contextOf = (id) => handle.server.registry.get(id).context;
  // INV-3: no choice is today's profile.
  let byId = await devices();
  for (const id of [ID_A, ID_B]) {
    assert.equal(byId[id].display_choice, null);
    assert.equal(byId[id].fps_cap, 60);
    assert.equal(byId[id].max_size, null);
  }
  // A choice for A only, as the control message sets it; nothing of B changes.
  assert.deepEqual(handle.server.setStreamChoice(contextOf(ID_A), { fps: 60, size: "half" }),
    { fps: 60, max_size: 800, scale_factor: null });
  byId = await devices();
  assert.deepEqual(byId[ID_A].display_choice, { fps: 60, size: "half" });
  assert.equal(byId[ID_A].max_size, 800);
  assert.equal(byId[ID_B].display_choice, null);
  await waitFor(async () => (await devices())[ID_A].profile_applied?.max_size === 800, 3000, "A's choice applied");
  assert.ok(log.some((entry) => entry.device === ID_A && entry.kind === "stream" && entry.name === "profile" &&
    entry.summary === `${ID_A} focused 60 fps half` && entry.ok === true), JSON.stringify(log.filter((entry) => entry.kind === "stream")));
  // QW-05-S02: A loses the focus and runs 30 fps (the background cap below its 60) at its
  // half size (below the 1024 cap); focused again it returns to 60.
  await api(canvas, "POST", "/api/focus", { body: { id: ID_B } });
  byId = await devices();
  assert.equal(byId[ID_A].profile, "background");
  assert.equal(byId[ID_A].fps_cap, 30);
  assert.equal(byId[ID_A].max_size, 800);
  byId = await waitFor(async () => {
    const now = await devices();
    return now[ID_A].profile_applied?.profile === "background" ? now : null;
  }, 6000, "A's background profile applied");
  assert.equal(byId[ID_A].profile_applied.fps_cap, 30);
  assert.equal(byId[ID_A].profile_applied.max_size, 800);
  // B, focused with no choice, keeps today's focused profile.
  assert.equal(byId[ID_B].fps_cap, 60);
  assert.equal(byId[ID_B].max_size, null);
  await api(canvas, "POST", "/api/focus", { body: { id: ID_A } });
  byId = await waitFor(async () => {
    const now = await devices();
    return now[ID_A].profile_applied?.profile === "focused" ? now : null;
  }, 6000, "A focused again");
  assert.equal(byId[ID_A].profile_applied.fps_cap, 60);
  assert.equal(byId[ID_A].fps_cap, 60);
  // A lower choice stays lower in the background: B at 15 fps runs 15, not 30.
  assert.deepEqual(handle.server.setStreamChoice(contextOf(ID_B), { fps: 15, size: "full" }),
    { fps: 15, max_size: 1024, scale_factor: null });
  assert.equal((await devices())[ID_B].fps_cap, 15);
  // A restore after a crash keeps the device's choice.
  deviceScope.run({ id: ID_A }, () => handle.onUncaught(new Error("boom in A")));
  await waitFor(async () => {
    const entry = handle.server.registry.get(ID_A);
    return entry.state === "live" && entry.context !== null ? entry : null;
  }, 8000, "A restored");
  byId = await devices();
  assert.deepEqual(byId[ID_A].display_choice, { fps: 60, size: "half" });
  assert.equal(byId[ID_A].profile_applied.max_size, 800);
  assert.equal(contextOf(ID_A).profile.maxSize, 800);
  // A device the focus plan does not know refuses a change and keeps its choice.
  handle.server.focus.forget(ID_B);
  assert.throws(() => handle.server.setStreamChoice(contextOf(ID_B), { fps: 30, size: "half" }), /cannot be changed now/);
  assert.deepEqual((await devices())[ID_B].display_choice, { fps: 15, size: "full" });
  await handle.stop();
});

// ---------------------------------------------------------------------------
// ID-14: an iOS session streams on the device daemon's companion. A fake daemon in this
// process speaks the daemon's socket protocol (attach, companion, detach) and runs fake
// idb_companion processes; the session opens its own gRPC session on the one it names.
// ---------------------------------------------------------------------------

const IOS_UDID = "5D2B0D6C-7A5B-4E0B-9C51-2B1A3F4E8D90";
const IDB_MODULE_URL = new URL(
  "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs", import.meta.url).href;

// A fake idb_companion: describe, get_orientation, a video stream of tiny H.264-shaped frames
// (real simulator SPS/PPS on every key frame), HID; each spawn, Start, Stop and HID event is a
// line in __EVENTS__. `--version` prints a version, as the daemon's version check reads it.
const FAKE_COMPANION = String.raw`
import { appendFileSync } from "node:fs";
import * as http2 from "node:http2";
import { GrpcMessageParser, decodeHidEvent, decodeVideoStreamRequest, encodeVideoStreamResponse, grpcFrame } from "__MODULE__";

const EVENTS = "__EVENTS__";
const args = process.argv.slice(2);
if (args.includes("--version")) { console.log("1.6.5"); process.exit(0); }
const arg = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const record = (event) => appendFileSync(EVENTS, JSON.stringify({ pid: process.pid, ...event }) + "\n");
record({ type: "spawn", args });
process.on("SIGTERM", () => { record({ type: "term" }); process.exit(0); });
const varint = (value) => { const out = []; let v = value; while (v >= 0x80) { out.push((v & 0x7f) | 0x80); v = Math.floor(v / 128); } out.push(v); return Buffer.from(out); };
const tag = (field, wire) => varint(field * 8 + wire);
const len = (field, bytes) => Buffer.concat([tag(field, 2), varint(bytes.length), bytes]);
const uint = (field, value) => Buffer.concat([tag(field, 0), varint(value)]);
const dbl = (field, value) => { const b = Buffer.alloc(8); b.writeDoubleLE(value); return Buffer.concat([tag(field, 1), b]); };
const str = (field, text) => len(field, Buffer.from(text));
const SC = Buffer.from([0, 0, 0, 1]);
const SPS = Buffer.from("27640033ac13143c04c0149e6a9a80868083c20108f8", "hex");
const PPS = Buffer.from("28ee3cb0", "hex");
let seq = 0;
let hidActive = null;
const hidWaiting = [];
const frame = (key) => {
  const body = Buffer.alloc(5);
  body[0] = key ? 0x65 : 0x41;
  body.writeUInt32BE(seq++, 1);
  return key ? Buffer.concat([SC, SPS, SC, PPS, SC, body]) : Buffer.concat([SC, body]);
};
const server = http2.createServer();
server.on("session", (session) => session.on("error", () => {}));
server.on("stream", (stream, headers) => {
  const method = headers[":path"].split("/").pop();
  const parser = new GrpcMessageParser();
  stream.on("error", () => {});
  stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
  stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
  if (method === "video_stream") {
    let timer = null;
    let sent = 0;
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        const request = decodeVideoStreamRequest(message);
        record({ type: request.type });
        if (request.type === "start") {
          timer = setInterval(() => {
            stream.write(grpcFrame(encodeVideoStreamResponse({ data: frame(sent % 30 === 0) })));
            sent += 1;
          }, 10);
        } else if (request.type === "stop") {
          clearInterval(timer);
          stream.end();
        }
      }
    });
    stream.on("close", () => clearInterval(timer));
  } else if (method === "hid") {
    // Like idb_companion: one hid call at a time; a second call is served, and its events
    // performed, only once the open one ends.
    const serve = () => {
      hidActive = stream;
      record({ type: "hid_open" });
      stream.on("data", (chunk) => { for (const message of parser.push(chunk)) record({ type: "hid", event: decodeHidEvent(message) }); });
      stream.on("end", () => stream.end());
    };
    stream.on("close", () => {
      const waiting = hidWaiting.indexOf(serve);
      if (waiting >= 0) hidWaiting.splice(waiting, 1);
      if (hidActive !== stream) return;
      hidActive = null;
      record({ type: "hid_close" });
      hidWaiting.shift()?.();
    });
    if (hidActive) {
      record({ type: "hid_wait" });
      hidWaiting.push(serve);
    } else {
      serve();
    }
  } else {
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        if (method === "describe") {
          const screen = Buffer.concat([uint(1, 1206), uint(2, 2622), dbl(3, 3), uint(4, 402), uint(5, 874)]);
          stream.write(grpcFrame(len(1, Buffer.concat([str(1, arg("--udid")), str(2, "Autonom-Daemon-Test"), len(3, screen), str(4, "Booted")]))));
        } else if (method === "get_orientation") {
          stream.write(grpcFrame(uint(1, 1)));
        }
        stream.end();
      }
    });
  }
});
const socketPath = arg("--grpc-domain-sock");
server.listen(socketPath, () => process.stdout.write(JSON.stringify({ grpc_path: socketPath }) + "\n"));
`;

// simctl for the session's Simulator watch: the one UDID, Booted.
const FAKE_SIMCTL = String.raw`
const args = process.argv.slice(2);
if (args[0] === "simctl" && args[1] === "list") {
  console.log(JSON.stringify({ devices: { "iOS 27.0": [{ udid: "__UDID__", state: "Booted", isAvailable: true }] } }));
} else {
  process.exitCode = 2;
}
`;

/** A temporary home, fake companion and simctl for an iOS session; everything goes at the end. */
async function iosDaemonWorld(t) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-iosd-canvas-"));
  // Unix socket paths stay short: their own folder directly under the temporary directory.
  const sockets = await mkdtemp(join(tmpdir(), "aic-"));
  const bin = join(directory, "bin");
  await mkdir(bin);
  const world = {
    directory, sockets, bin,
    home: join(directory, "autonom-home"),
    events: join(directory, "companion-events.jsonl"),
    companion: join(directory, "idb_companion"),
    xcrun: join(directory, "xcrun"),
    daemons: [],
    sessions: [],
  };
  await writeFile(world.events, "");
  await writeFile(world.companion, `#!${process.execPath}\n${FAKE_COMPANION
    .replace("__MODULE__", IDB_MODULE_URL).replace("__EVENTS__", world.events)}`);
  await chmod(world.companion, 0o755);
  await writeFile(world.xcrun, `#!${process.execPath}\n${FAKE_SIMCTL.replace("__UDID__", IOS_UDID)}`);
  await chmod(world.xcrun, 0o755);
  // What a daemon this test might start gets: no idb client, no real idb state, no real home.
  world.daemonEnv = {
    PATH: bin, HOME: directory, TMPDIR: tmpdir(), AUTONOM_HOME: world.home,
    AUTONOM_IDB_COMPANION_BIN: world.companion, AUTONOM_IDB_STATE_FILE: join(directory, "idb-state"),
  };
  t.after(async () => {
    await Promise.allSettled(world.sessions.map((session) => session.stop()));
    await Promise.allSettled(world.daemons.map((daemon) => daemon.close()));
    for (const { pid } of companionEvents(world).filter((event) => event.type === "spawn")) {
      try { process.kill(pid, "SIGKILL"); } catch {}
    }
    rmSync(socketPathFor(world.home, IOS_UDID, { tmp: tmpdir() }).dir, { recursive: true, force: true });
    await rm(sockets, { recursive: true, force: true });
    await rm(directory, { recursive: true, force: true });
  });
  return world;
}

function companionEvents(world) {
  return readFileSync(world.events, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

/**
 * The device daemon's side of the socket protocol: `attach` and `companion` answer with the
 * companion's socket and pid (waiting while it restarts, as the daemon does for up to 6 s),
 * `detach` and a closed connection drop the attachment. Its registry names this process.
 */
class FakeIosDaemon {
  ops = [];
  state = "serving";
  companion = null;
  /** How many more `attach` requests are answered daemon_unreachable (Infinity: all of them). */
  refuseAttaches = 0;
  #world;
  #attachments = new Map();
  #connections = new Set();
  #waiters = [];
  #server = null;

  constructor(world) {
    this.#world = world;
    world.daemons.push(this);
  }

  get attached() {
    let count = 0;
    for (const value of this.#attachments.values()) count += value;
    return count;
  }

  async start({ companion = null } = {}) {
    this.dir = await mkdtemp(join(this.#world.sockets, "d-"));
    this.socket = join(this.dir, "d.sock");
    this.#server = createNetServer((socket) => this.#onConnection(socket));
    await new Promise((resolvePromise, reject) => {
      this.#server.once("error", reject);
      this.#server.listen(this.socket, resolvePromise);
    });
    this.companion = companion ?? await this.spawnCompanion();
    this.writeRegistry();
    return this;
  }

  /** One fake companion on the one socket path idb uses for this UDID, like `idb connect`. */
  async spawnCompanion() {
    const socketPath = join(this.#world.sockets, "companion.sock");
    rmSync(socketPath, { force: true });
    const child = spawn(this.#world.companion, ["--udid", IOS_UDID, "--grpc-domain-sock", socketPath, "--only", "simulator"],
      { stdio: ["ignore", "pipe", "ignore"] });
    await new Promise((resolvePromise, reject) => {
      let text = "";
      child.stdout.on("data", (chunk) => {
        text += chunk;
        if (text.includes("grpc_path")) resolvePromise();
      });
      child.once("exit", (code) => reject(new Error(`the fake companion exited (${code})`)));
    });
    return { child, pid: child.pid, socketPath };
  }

  /** Kills the companion and starts the next one after `delayMs`, as the daemon replaces a lost one. */
  async restartCompanion({ delayMs = 300 } = {}) {
    this.state = "companion_restarting";
    const old = this.companion;
    const exited = new Promise((resolvePromise) => old.child.once("exit", resolvePromise));
    old.child.kill("SIGKILL");
    await exited;
    await sleep(delayMs);
    this.companion = await this.spawnCompanion();
    this.state = "serving";
    this.writeRegistry();
    for (const wake of this.#waiters.splice(0)) wake();
  }

  writeRegistry() {
    const { registry } = daemonPaths(this.#world.home, IOS_UDID);
    mkdirSync(dirname(registry), { recursive: true });
    writeFileSync(registry, JSON.stringify({
      schema: IOSD_SCHEMA, udid: IOS_UDID, pid: process.pid, socket: this.socket, state: this.state,
      companion: { pid: this.companion?.pid ?? null, socket: this.companion?.socketPath ?? null, owned: true },
    }));
  }

  /** Gone: the server and every connection close; `keepCompanion` leaves the companion running. */
  async close({ keepCompanion = false } = {}) {
    if (!this.#server) return;
    const server = this.#server;
    this.#server = null;
    for (const socket of this.#connections) socket.destroy();
    await new Promise((resolvePromise) => server.close(resolvePromise));
    if (!keepCompanion && this.companion?.child.exitCode === null) this.companion.child.kill("SIGKILL");
    rmSync(this.dir, { recursive: true, force: true });
  }

  #ready() {
    if (this.state === "serving") return Promise.resolve(true);
    return new Promise((resolvePromise) => {
      const timer = setTimeout(() => resolvePromise(false), 6000);
      this.#waiters.push(() => {
        clearTimeout(timer);
        resolvePromise(true);
      });
    });
  }

  #onConnection(socket) {
    this.#connections.add(socket);
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("error", () => {});
    socket.on("data", (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (line.trim()) this.#handle(socket, JSON.parse(line));
      }
    });
    socket.on("close", () => {
      this.#connections.delete(socket);
      this.#attachments.delete(socket);
    });
  }

  async #handle(socket, request) {
    this.ops.push(request.op);
    let reply;
    const companion = () => ({ socket: this.companion.socketPath, pid: this.companion.pid, owned: true });
    if (request.op === "attach" && this.refuseAttaches > 0) {
      this.refuseAttaches -= 1;
      reply = { ok: false, error: { code: "daemon_unreachable", message: "The companion is being restarted.", stage: "before_send" } };
    } else if (request.op === "attach" || request.op === "companion") {
      if (!(await this.#ready())) {
        reply = { ok: false, error: { code: "daemon_unreachable", message: "restarting", stage: "before_send" } };
      } else {
        if (request.op === "attach") this.#attachments.set(socket, (this.#attachments.get(socket) ?? 0) + 1);
        reply = { ok: true, ...(request.op === "attach" ? { attached: this.attached } : {}), ...companion() };
      }
    } else if (request.op === "detach") {
      const count = this.#attachments.get(socket) ?? 0;
      if (count <= 1) this.#attachments.delete(socket);
      else this.#attachments.set(socket, count - 1);
      reply = { ok: true, attached: this.attached };
    } else {
      reply = { ok: false, error: { code: "invalid_request", message: `unknown op ${request.op}`, stage: "before_send" } };
    }
    if (!socket.destroyed) socket.write(`${JSON.stringify({ id: request.id, ...reply })}\n`);
  }
}

function iosSession(world, { daemon = true, daemonOptions = {} } = {}) {
  const session = new IosFastSession({
    udid: IOS_UDID,
    xcrunPath: world.xcrun,
    binary: world.companion,
    video: { ...DEFAULT_VIDEO_OPTIONS, fps: 60 },
    daemon: daemon ? { stateBase: world.home, env: world.daemonEnv, ...daemonOptions } : null,
  });
  world.sessions.push(session);
  session.packets = 0;
  session.states = [];
  session.errors = [];
  session.on("packet", () => { session.packets += 1; });
  session.on("state", (name) => session.states.push(name));
  session.on("error", (error) => session.errors.push(error.message));
  return session;
}

test("iOS daemon switch: AUTONOM_IOS_DAEMON=off (0, false, no) keeps the Canvas on its own companion; anything else uses the daemon", () => {
  for (const value of ["off", "OFF", " off ", "0", "false", "no"]) {
    assert.equal(iosDaemonEnabled({ AUTONOM_IOS_DAEMON: value }), false, value);
  }
  for (const value of [undefined, "", "on", "1"]) {
    assert.equal(iosDaemonEnabled({ AUTONOM_IOS_DAEMON: value }), true, String(value));
  }
});

test("iOS attach (ID-14): the session streams on the daemon's companion, starts none of its own, and detaches when the device leaves", async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "frames from the daemon's companion");
  assert.deepEqual(daemon.ops, ["attach"]);
  assert.equal(daemon.attached, 1);
  const stats = session.stats;
  assert.equal(stats.companionSource, "daemon");
  assert.equal(stats.daemonAttached, true);
  assert.equal(stats.daemonFallback, null);
  assert.equal(stats.companionStarts, 0, "the Canvas started no companion");
  assert.equal(stats.companionAttaches, 1);
  assert.equal(stats.companionPid, daemon.companion.pid);
  assert.deepEqual(session.points, { width: 402, height: 874 });
  const spawns = companionEvents(world).filter((event) => event.type === "spawn");
  assert.deepEqual(spawns.map((event) => event.pid), [daemon.companion.pid], "one companion process for the UDID");
  // HID goes to the same companion, on the session's own gRPC session.
  await session.hid({ touch: { x: 10, y: 20 }, direction: "down" });
  await session.hid({ touch: { x: 10, y: 20 }, direction: "up" });
  await waitFor(() => companionEvents(world).filter((event) => event.type === "hid").length >= 2, 5000, "two HID events");
  assert.ok(companionEvents(world).filter((event) => event.type === "hid").every((event) => event.pid === daemon.companion.pid));
  // The device leaves: Stop on the stream, detach, and the daemon's companion keeps running.
  await session.stop();
  assert.deepEqual(daemon.ops, ["attach", "detach"]);
  assert.equal(daemon.attached, 0);
  assert.equal(session.stats.detaches, 1);
  assert.equal(session.stats.daemonAttached, false);
  assert.ok(companionEvents(world).some((event) => event.type === "stop" && event.pid === daemon.companion.pid), "Stop reached the companion");
  assert.equal(pidAlive(daemon.companion.pid), true, "the Canvas never stops the daemon's companion");
  assert.equal(companionEvents(world).some((event) => event.type === "term"), false);
});

test("iOS attach (ID-15): the Canvas's hid call on the daemon's companion ends when idle, so the daemon's input and the Canvas's both go through", { timeout: 30_000 }, async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "frames from the daemon's companion");
  const hid = () => companionEvents(world).filter((event) => event.type === "hid").map((event) => event.event);
  await session.hid({ touch: { x: 10, y: 20 }, direction: "down" });
  await session.hid({ touch: { x: 10, y: 20 }, direction: "up" });
  await waitFor(() => hid().length >= 2, 5000, "the Canvas's touch");
  // The daemon's input: a gRPC session and hid stream of its own on the same companion.
  const daemonClient = new IdbCompanionClient({ socketPath: daemon.companion.socketPath });
  t.after(() => daemonClient.close({ timeoutMs: 200 }));
  await daemonClient.hid().tap(30, 40);
  await waitFor(() => hid().length >= 4, 3000, "the daemon's tap performed while the Canvas is attached");
  // And the other way round: the Canvas's next input after the daemon's.
  await session.hid({ key: 4, direction: "down" });
  await session.hid({ key: 4, direction: "up" });
  await waitFor(() => hid().length >= 6, 3000, "the Canvas's input after the daemon's tap");
  assert.deepEqual(hid(), [
    { touch: { x: 10, y: 20 }, direction: "down" },
    { touch: { x: 10, y: 20 }, direction: "up" },
    { touch: { x: 30, y: 40 }, direction: "down" },
    { touch: { x: 30, y: 40 }, direction: "up" },
    { key: 4, direction: "down" },
    { key: 4, direction: "up" },
  ]);
  assert.ok(companionEvents(world).filter((event) => event.type === "hid_open").length >= 3, "a call per burst");
  await session.stop();
});

test("iOS attach (REV-024): Canvas input sent while the daemon holds the companion's hid call past 2.5 s is performed once, in order, and not reported lost", { timeout: 30_000 }, async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "frames from the daemon's companion");
  const hid = () => companionEvents(world).filter((event) => event.type === "hid").map((event) => event.event);
  // The daemon (another client) takes the companion's hid call and keeps it: a long press.
  const daemonClient = new IdbCompanionClient({ socketPath: daemon.companion.socketPath });
  t.after(() => daemonClient.close({ timeoutMs: 200 }));
  const held = daemonClient.openCall("hid");
  held.on("error", () => {});
  held.on("data", () => {});
  const heldClosed = new Promise((resolve) => held.once("close", resolve));
  held.write(grpcFrame(encodeHidEvent({ touch: { x: 1, y: 1 }, direction: "down" })));
  await waitFor(() => hid().length === 1, 5000, "the daemon's press to be served");
  const heldAt = Date.now();
  await session.hid({ touch: { x: 10, y: 20 }, direction: "down" });
  await session.hid({ touch: { x: 10, y: 20 }, direction: "up" });
  await waitFor(() => companionEvents(world).some((event) => event.type === "hid_wait"), 5000, "the Canvas's call to wait");
  // Longer than the old 2 s stop timeout plus the idle window, after which the waiting call was cancelled.
  await new Promise((resolve) => setTimeout(resolve, Math.max(0, 2600 - (Date.now() - heldAt))));
  assert.equal(hid().length, 1, "nothing of the Canvas performed while the daemon holds the call");
  held.write(grpcFrame(encodeHidEvent({ touch: { x: 1, y: 1 }, direction: "up" })));
  held.end();
  await heldClosed;
  await waitFor(() => hid().length >= 4, 5000, "the Canvas's touch after the daemon's press");
  assert.deepEqual(hid(), [
    { touch: { x: 1, y: 1 }, direction: "down" },
    { touch: { x: 1, y: 1 }, direction: "up" },
    { touch: { x: 10, y: 20 }, direction: "down" },
    { touch: { x: 10, y: 20 }, direction: "up" },
  ]);
  await waitFor(() => companionEvents(world).filter((event) => event.type === "hid_close").length >= 2, 5000, "the Canvas's call to end");
  assert.equal(session.stats.hidLost, 0);
  assert.deepEqual(session.errors.filter((message) => /not confirmed/.test(message)), []);
  await session.stop();
});

test("iOS reconnect (ID-14-S02): a companion the daemon restarts is reconnected to within 10 s, never replaced by the Canvas", async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "first frames");
  const before = daemon.companion.pid;
  const killedAt = Date.now();
  await daemon.restartCompanion({ delayMs: 300 });
  const after = daemon.companion.pid;
  assert.notEqual(after, before);
  // Frames from the new companion: its Start arrives and the session streams again.
  await waitFor(() => companionEvents(world).some((event) => event.type === "start" && event.pid === after), 10_000,
    "a Start on the restarted companion");
  const packetsAtStart = session.packets;
  await waitFor(() => session.state === "streaming" && session.packets >= packetsAtStart + 10, 10_000, "frames again");
  assert.ok(Date.now() - killedAt < 10_000, "video is back within 10 s");
  assert.ok(session.states.includes("restarting"), session.states.join(","));
  // One attach for the whole session; the new companion was asked for, not attached to again.
  assert.equal(daemon.ops.filter((op) => op === "attach").length, 1, daemon.ops.join(","));
  assert.ok(daemon.ops.includes("companion"), daemon.ops.join(","));
  assert.equal(daemon.attached, 1);
  const stats = session.stats;
  assert.equal(stats.companionStarts, 0, "no companion of the Canvas's own");
  assert.equal(stats.companionAttaches, 2);
  assert.equal(stats.companionPid, after);
  assert.equal(stats.companionSource, "daemon");
  // Only the daemon ever spawned companions: the killed one and its replacement.
  assert.deepEqual(companionEvents(world).filter((event) => event.type === "spawn").map((event) => event.pid), [before, after]);
  await session.hid({ key: 4, direction: "down" });
  await waitFor(() => companionEvents(world).some((event) => event.type === "hid" && event.pid === after), 5000, "HID on the new companion");
  await session.stop();
  assert.equal(daemon.attached, 0);
  assert.equal(daemon.ops.at(-1), "detach");
  assert.equal(pidAlive(after), true);
});

test("iOS attach: a daemon that goes away is attached to again through the daemon that took over, on the same companion", async (t) => {
  const world = await iosDaemonWorld(t);
  const first = await new FakeIosDaemon(world).start();
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "first frames");
  // A new daemon adopts the running companion and takes over the registry; the old one goes.
  const second = await new FakeIosDaemon(world).start({ companion: first.companion });
  await first.close({ keepCompanion: true });
  await waitFor(() => second.attached === 1, 10_000, "an attach to the new daemon");
  assert.deepEqual(second.ops, ["attach"]);
  assert.equal(session.stats.daemonLosses, 1);
  assert.equal(session.stats.daemonAttaches, 2);
  // The same companion keeps streaming: no new connection, no stream restart.
  assert.equal(session.stats.companionAttaches, 1);
  const packets = session.packets;
  await waitFor(() => session.packets >= packets + 10, 5000, "frames keep coming");
  assert.equal(session.state, "streaming");
  await session.stop();
  assert.deepEqual(second.ops, ["attach", "detach"]);
  assert.equal(second.attached, 0);
});

/** The daemon start lock, held by another live process (the test runner): ensureDaemon waits on it. */
function holdStartLock(t, world) {
  const { startLock } = daemonPaths(world.home, IOS_UDID);
  mkdirSync(dirname(startLock), { recursive: true });
  writeFileSync(startLock, String(process.ppid));
  const release = () => rmSync(startLock, { force: true });
  t.after(release);
  return release;
}

const spawnPids = (world) => companionEvents(world).filter((event) => event.type === "spawn").map((event) => event.pid);

test("iOS attach: a daemon whose companion is restarting does not fail the first stream; the session asks again", async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  daemon.refuseAttaches = 2;
  const session = iosSession(world);
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "frames from the daemon's companion");
  assert.deepEqual(daemon.ops, ["attach", "attach", "attach"]);
  assert.equal(daemon.attached, 1);
  const stats = session.stats;
  assert.equal(stats.companionSource, "daemon");
  assert.equal(stats.daemonFallback, null);
  assert.equal(stats.companionStarts, 0, "the Canvas started no companion");
  assert.deepEqual(spawnPids(world), [daemon.companion.pid]);
  assert.ok(session.errors.some((message) => /being restarted/.test(message)), session.errors.join("\n"));
  await session.stop();
  assert.equal(daemon.attached, 0);
});

test("iOS attach: past the first wait the session streams on its own companion, then returns to the daemon's once it attaches", async (t) => {
  const world = await iosDaemonWorld(t);
  const daemon = await new FakeIosDaemon(world).start();
  daemon.refuseAttaches = Infinity;
  const session = iosSession(world, { daemonOptions: { firstWaitMs: 1200 } });
  await session.start();
  await waitFor(() => session.packets >= 10, 10_000, "frames from the Canvas's own companion");
  assert.equal(session.stats.companionSource, "private");
  assert.match(session.stats.daemonFallback, /being restarted/);
  assert.equal(session.stats.companionStarts, 1);
  const own = spawnPids(world).find((pid) => pid !== daemon.companion.pid);
  assert.ok(own, "the Canvas started its own companion");
  // The daemon serves again: the session attaches, its own companion goes, frames come from the daemon's.
  daemon.refuseAttaches = 0;
  await waitFor(() => session.stats.companionSource === "daemon" && daemon.attached === 1, 10_000, "a return to the daemon");
  await waitFor(() => companionEvents(world).some((event) => event.type === "start" && event.pid === daemon.companion.pid), 10_000,
    "a Start on the daemon's companion");
  const packets = session.packets;
  await waitFor(() => session.state === "streaming" && session.packets >= packets + 10, 10_000, "frames again");
  await waitFor(() => !pidAlive(own), 5000, "the Canvas's own companion stopped");
  const stats = session.stats;
  assert.equal(stats.daemonFallback, null);
  assert.equal(stats.daemonReturns, 1);
  assert.equal(stats.companionPid, daemon.companion.pid);
  assert.deepEqual(spawnPids(world).filter((pid) => pidAlive(pid)), [daemon.companion.pid], "one companion runs for the UDID");
  await session.stop();
  assert.equal(daemon.attached, 0);
  assert.equal(pidAlive(daemon.companion.pid), true);
});

test("iOS attach: a daemon start that fails for a passing reason (start lock held) is tried again; the session returns to the daemon's companion", async (t) => {
  const world = await iosDaemonWorld(t);
  const release = holdStartLock(t, world);
  const session = iosSession(world, { daemonOptions: { startTimeoutMs: 300, retryMinMs: 200, retryMaxMs: 400 } });
  await session.start();
  await waitFor(() => session.packets >= 10, 10_000, "frames from the Canvas's own companion");
  assert.equal(session.stats.companionSource, "private");
  assert.match(session.stats.daemonFallback, /start lock/);
  const [own] = spawnPids(world);
  // Tried again while the lock stays taken. Until the session's own first stream has settled a
  // try only reschedules itself on the idb schedule (0.5 s doubling: 0.5 + 1 + 2 + 4 s), so the
  // first reported failure can come several seconds late on a busy host; the bound covers that.
  await waitFor(() => session.errors.some((message) => /attach failed: .*start lock/.test(message)), 20_000, "a retry of the daemon");
  assert.equal(session.stats.companionSource, "private");
  // Another process brings the daemon up: the session attaches to it and stops its own companion.
  const daemon = await new FakeIosDaemon(world).start();
  release();
  await waitFor(() => session.stats.companionSource === "daemon" && daemon.attached === 1, 10_000, "a return to the daemon");
  const packets = session.packets;
  await waitFor(() => session.state === "streaming" && session.packets >= packets + 10, 10_000, "frames on the daemon's companion");
  await waitFor(() => !pidAlive(own), 5000, "the Canvas's own companion stopped");
  assert.equal(session.stats.companionPid, daemon.companion.pid);
  assert.equal(session.stats.daemonFallback, null);
  assert.deepEqual(spawnPids(world).filter((pid) => pidAlive(pid)), [daemon.companion.pid], "one companion runs for the UDID");
  await session.stop();
  assert.equal(daemon.attached, 0);
});

test("iOS attach: a lost daemon whose restart fails for a passing reason is attached to again while the stream keeps running", async (t) => {
  const world = await iosDaemonWorld(t);
  const first = await new FakeIosDaemon(world).start();
  const session = iosSession(world, { daemonOptions: { startTimeoutMs: 300, retryMinMs: 200, retryMaxMs: 400 } });
  await session.start();
  await waitFor(() => session.packets >= 10, 5000, "first frames");
  const streams = session.stats.streamsOpened;
  // The daemon dies, its companion keeps running, and no new daemon can start yet.
  const release = holdStartLock(t, world);
  await first.close({ keepCompanion: true });
  await waitFor(() => session.errors.filter((message) => /attach failed: .*start lock/.test(message)).length >= 2, 10_000,
    "reattach tried again after daemon_start_failed");
  assert.equal(session.stats.companionSource, "daemon", "a passing failure keeps the stream on the daemon's companion");
  assert.equal(session.stats.daemonAttached, false);
  // A daemon that adopts the companion comes up: the session attaches to it, so its idle stop counts the Canvas.
  const second = await new FakeIosDaemon(world).start({ companion: first.companion });
  release();
  await waitFor(() => second.attached === 1, 10_000, "an attach to the new daemon");
  assert.equal(session.stats.daemonAttached, true);
  assert.equal(session.stats.companionAttaches, 1, "the same companion connection");
  assert.equal(session.stats.streamsOpened, streams, "the stream never restarted");
  const packets = session.packets;
  await waitFor(() => session.packets >= packets + 10, 5000, "frames keep coming");
  assert.equal(session.stats.companionStarts, 0);
  await session.stop();
  assert.equal(second.attached, 0);
});

test("iOS attach: a daemon that cannot start (no idb client) leaves the session on a companion of its own and says why", async (t) => {
  const world = await iosDaemonWorld(t);
  const session = iosSession(world, { daemonOptions: { retryMinMs: 100, retryMaxMs: 200 } });
  await session.start();
  await waitFor(() => session.packets >= 10, 10_000, "frames from the Canvas's own companion");
  const stats = session.stats;
  assert.equal(stats.companionSource, "private");
  assert.match(stats.daemonFallback, /idb client was not found/);
  assert.equal(stats.daemonAttached, false);
  assert.equal(stats.companionStarts, 1);
  assert.ok(session.errors.some((message) => /device daemon is unavailable/.test(message)), session.errors.join("\n"));
  // The failed daemon left nothing: no registry, no socket folder.
  assert.equal(existsSync(daemonPaths(world.home, IOS_UDID).registry), false);
  assert.equal(existsSync(socketPathFor(world.home, IOS_UDID, { tmp: tmpdir() }).dir), false);
  const spawns = companionEvents(world).filter((event) => event.type === "spawn" && event.args.includes("--grpc-domain-sock"));
  assert.equal(spawns.length, 1, "one companion: the Canvas's own");
  // A lasting failure (idb_missing) is not tried again.
  await sleep(800);
  assert.equal(session.errors.some((message) => /attach failed/.test(message)), false, session.errors.join("\n"));
  assert.equal(session.stats.companionSource, "private");
  await session.stop();
  assert.equal(pidAlive(spawns[0].pid), false, "the Canvas stops a companion it started");
});
