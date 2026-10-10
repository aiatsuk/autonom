// End-to-end tests of the workspace Canvas (contract 3) against fakes only: a fake adb that
// lists the devices named in a file, a fake input bridge, a fake tools process and a fake
// autonom.py for sessions and boots. No device, emulator, Simulator or browser is involved,
// and every file goes to a temporary AUTONOM_HOME.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { EventEmitter } from "node:events";
import { connect, createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { parseArgs } from "../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs";
import {
  GrpcMessageParser,
  IdbVideoStream,
  decodeVideoStreamRequest,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";
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
const STARTED_LINE = /^Open this exact URL in the visible \S+ side-panel browser: \S+\n/m;

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
  assert.match(canvas.stdout(), /Open this exact URL in the visible \S+ side-panel browser: http:\/\/127\.0\.0\.1:\d+\/#token=/);
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
  // The first tab holds one tile: the second device went to a new tab.
  const workspace = (await api(canvas, "GET", "/api/workspace")).json;
  assert.equal(workspace.tabs.length, 2);
  assert.notEqual(first.tab, second.tab);
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
  assert.deepEqual(left.tabs.find((tab) => tab.id === second.tab).slots, [null]);
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
  assert.deepEqual(saved.tabs.map((tab) => tab.slots.map((slot) => slot?.target ?? null)), [[A], [B]],
    "the file holds the devices attached at stop");

  // Restart without --device: both tabs come back with their ids, both devices attach again.
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
  assert.deepEqual(tab.slots, [{ device: ID_B, present: false }]);
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
    label: "Autonom_Split2", running: false, target: null, job: null }]);
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
  await stream.stop({ timeoutMs: 500 });
});
