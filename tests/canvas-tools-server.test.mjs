// Tests of the Canvas tools server (canvas-tools-server.mjs and its /tools/* routes) against
// fakes only: a fake adb or xcrun, a fake input bridge, a fake tools process and a fake
// `autonom logs follow`. No device, emulator, Simulator or browser is involved.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  LOG_MAX_ENTRIES,
  LogFeed,
  TOOLS_MUTATING_OPS,
  TOOLS_OPS,
  TOOLS_READ_OPS,
  ToolsClient,
  handleToolsRoute,
  literalPattern,
  toolsErrorStatus,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-server.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const TOKEN = "tools-test-token";
const SERIAL = "fake-device-1";
const UDID = "00000000-0000-4000-8000-000000000001";
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Wl8sAAAAASUVORK5CYII=";

const FAKE_ADB = String.raw`
const args = process.argv.slice(2);
const rest = args[0] === "-s" ? args.slice(2) : args;
const line = rest.join(" ");
if (rest[0] === "get-state") console.log("device");
else if (line === "exec-out screencap -p") process.stdout.write(Buffer.from(process.env.FAKE_PNG, "base64"));
else if (line === "shell wm size") console.log("Physical size: 1080x2400");
else if (line === "shell wm density") console.log("Physical density: 420");
else process.exitCode = 1;
`;

const FAKE_XCRUN = String.raw`
const args = process.argv.slice(2);
if (args.join(" ") === "simctl list devices --json") {
  console.log(JSON.stringify({ devices: { "iOS 26": [
    { udid: process.env.FAKE_UDID, state: "Booted", isAvailable: true, name: "iPhone" },
  ] } }));
} else {
  process.exitCode = 1;
}
`;

// The input bridge: every call succeeds, and each is logged.
const FAKE_BRIDGE = String.raw`
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  appendFileSync(process.env.FAKE_BRIDGE_LOG, line + "\n");
  const result = { ok: true, display: { width: 1080, height: 2400 } };
  process.stdout.write(JSON.stringify({ id: message.id, ok: true, result }) + "\n");
});
`;

// The tools process of the contract. payload.fake picks a behaviour: hang, die, error,
// late (answered after payload.ms) or noise (a non-protocol line first).
const FAKE_TOOLS = String.raw`
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
const LOG = process.env.FAKE_TOOLS_LOG;
appendFileSync(LOG, JSON.stringify({ start: process.pid, argv: process.argv.slice(2) }) + "\n");
const send = (message) => process.stdout.write(JSON.stringify(message) + "\n");
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  appendFileSync(LOG, JSON.stringify({ request: message }) + "\n");
  const { id, op, payload = {}, origin } = message;
  const fake = payload.fake;
  if (fake === "hang") return;
  if (fake === "die") process.exit(3);
  if (fake === "error") {
    const reply = { id, ok: false, error: "fake failure", hint: payload.hint ?? null, capability: payload.capability ?? null };
    if (payload.code !== undefined) reply.error_code = payload.code;
    return send(reply);
  }
  if (fake === "late") return setTimeout(() => send({ id, ok: true, result: { late: true } }), payload.ms);
  if (fake === "noise") process.stdout.write("library output that is not protocol\n");
  if (op === "context") {
    const session = process.env.FAKE_SESSION ? { id: "session-1", app_id: "com.example" } : null;
    return send({ id, ok: true, result: { platform: process.argv[3], session } });
  }
  send({ id, ok: true, result: { op, payload, origin } });
}).on("close", () => process.exit(0));
`;

// autonom.py for `logs follow`: FAKE_LOG_LINES entries, then an eof line when FAKE_LOG_EOF
// names a reason, else it keeps running. FAKE_LOG_GRANDCHILD starts a process of its own
// (the device log reader of the real CLI).
const FAKE_AUTONOM = String.raw`
import { appendFileSync } from "node:fs";
import { spawn } from "node:child_process";
const LOG = process.env.FAKE_LOG_ARGS;
const args = process.argv.slice(2);
appendFileSync(LOG, JSON.stringify({ pid: process.pid, args }) + "\n");
if (process.env.FAKE_LOG_GRANDCHILD) {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  appendFileSync(LOG, JSON.stringify({ grandchild: child.pid }) + "\n");
}
const at = args.indexOf("--package");
const pkg = at >= 0 ? args[at + 1] : null;
const count = Number(process.env.FAKE_LOG_LINES ?? 0);
let out = "not json\n";
for (let i = 1; i <= count; i += 1) {
  out += JSON.stringify({ kind: "line", source: "device", ts: "2026-10-07T00:00:00Z", text: "line " + i, package: pkg }) + "\n";
}
process.stdout.write(out);
if (process.env.FAKE_LOG_EOF) {
  process.stdout.write(JSON.stringify({ kind: "eof", reason: process.env.FAKE_LOG_EOF, lines: count, source: "device" }) + "\n");
} else {
  // A blank line now and then: ignored by the feed, and a closed pipe ends this process.
  setInterval(() => process.stdout.write("\n"), 200);
}
`;

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

async function waitFor(predicate, timeoutMs = 5000, what = "condition") {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(20);
  }
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function makeWorld(t, env = {}) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-tools-"));
  const bin = join(directory, "bin");
  await mkdir(bin);
  const files = {
    fakeAdb: join(directory, "fake-adb.mjs"),
    fakeXcrun: join(directory, "fake-xcrun.mjs"),
    fakeBridge: join(directory, "fake-bridge.mjs"),
    fakeTools: join(directory, "fake-tools.mjs"),
    fakeAutonom: join(directory, "fake-autonom.mjs"),
    bridgeLog: join(directory, "bridge.log"),
    toolsLog: join(directory, "tools.log"),
    logArgs: join(directory, "log-args.log"),
  };
  await writeFile(files.fakeAdb, `#!${process.execPath}\n${FAKE_ADB}`);
  await writeFile(files.fakeXcrun, `#!${process.execPath}\n${FAKE_XCRUN}`);
  await chmod(files.fakeAdb, 0o755);
  await chmod(files.fakeXcrun, 0o755);
  await writeFile(files.fakeBridge, FAKE_BRIDGE);
  await writeFile(files.fakeTools, FAKE_TOOLS);
  await writeFile(files.fakeAutonom, FAKE_AUTONOM);
  for (const log of [files.bridgeLog, files.toolsLog, files.logArgs]) await writeFile(log, "");
  const world = {
    directory, ...files, children: [],
    env: {
      PATH: bin,
      HOME: directory,
      AUTONOM_HOME: join(directory, "autonom-home"),
      FAKE_PNG: PNG_BASE64,
      FAKE_UDID: UDID,
      FAKE_BRIDGE_LOG: files.bridgeLog,
      FAKE_TOOLS_LOG: files.toolsLog,
      FAKE_LOG_ARGS: files.logArgs,
      ...env,
    },
  };
  t.after(async () => {
    for (const child of world.children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    // Nothing a fake started may outlive the test.
    for (const pid of [...await toolsPids(world), ...await logPids(world), ...await grandchildPids(world)]) {
      try { process.kill(pid, "SIGKILL"); } catch { /* gone */ }
    }
    await rm(directory, { recursive: true, force: true });
  });
  return world;
}

async function lines(path) {
  return (await readFile(path, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function toolsPids(world) {
  return (await lines(world.toolsLog)).filter((entry) => entry.start).map((entry) => entry.start);
}

async function toolsRequests(world) {
  return (await lines(world.toolsLog)).filter((entry) => entry.request).map((entry) => entry.request);
}

async function logPids(world) {
  return (await lines(world.logArgs)).filter((entry) => entry.pid).map((entry) => entry.pid);
}

/** The runs of the fake log follow, once at least `count` have logged their start. */
async function logRuns(world, count) {
  return await waitFor(async () => {
    const runs = (await lines(world.logArgs)).filter((entry) => entry.pid);
    return runs.length >= count ? runs : null;
  }, 5000, `${count} log feed starts`);
}

async function grandchildPids(world) {
  return (await lines(world.logArgs)).filter((entry) => entry.grandchild).map((entry) => entry.grandchild);
}

/** Start the Canvas; resolves once it prints its URL. */
function startCanvas(world, { platform = "android", args = [] } = {}) {
  const targetArgs = platform === "ios"
    ? ["--platform", "ios", "--simctl", world.fakeXcrun, "--target", UDID]
    : ["--adb", world.fakeAdb, "--serial", SERIAL];
  const child = spawn(process.execPath, [
    CANVAS, ...targetArgs, "--port", "0", "--token", TOKEN, "--transport", "screencap",
    "--python", process.execPath, "--bridge", world.fakeBridge,
    "--tools", world.fakeTools, "--autonom", world.fakeAutonom, ...args,
  ], { cwd: ROOT, env: world.env, stdio: ["ignore", "pipe", "pipe"] });
  world.children.push(child);
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => {
    child.once("exit", (code, signal) => resolvePromise({ code, signal, output }));
  });
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${output}`)), 15_000);
    child.stdout.on("data", () => {
      const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:(\d+)\/)/);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise({ child, origin: match[1].slice(0, -1), port: Number(match[2]), exited });
    });
    exited.then(({ code }) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${output}`));
    });
  });
}

async function login(canvas) {
  const response = await fetch(`${canvas.origin}/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: TOKEN }),
  });
  assert.equal(response.status, 200);
  const { csrf } = await response.json();
  return { csrf, cookie: response.headers.get("set-cookie").split(";", 1)[0] };
}

/** A request as the page sends it: cookie, CSRF and origin. */
async function api(canvas, auth, method, path, body = undefined, { origin = "human", headers = {}, signal } = {}) {
  const response = await fetch(`${canvas.origin}${path}`, {
    method,
    headers: {
      Cookie: auth.cookie,
      "X-Autonom-Csrf": auth.csrf,
      "X-Autonom-Origin": origin,
      "Content-Type": "application/json",
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  return { status: response.status, body: await response.json() };
}

async function status(canvas) {
  const response = await fetch(`${canvas.origin}/status`, { headers: { Authorization: `Bearer ${TOKEN}` } });
  assert.equal(response.status, 200);
  return await response.json();
}

async function stopCanvas(canvas) {
  canvas.child.kill("SIGTERM");
  return await canvas.exited;
}

async function canvasWorld(t, { env = {}, platform = "android" } = {}) {
  const world = await makeWorld(t, env);
  const canvas = await startCanvas(world, { platform });
  const auth = await login(canvas);
  return { world, canvas, auth };
}

// --- The contract tables --------------------------------------------------------------

test("ops: the allowlist is the read and mutating ops of the contract, with no overlap", () => {
  assert.deepEqual(TOOLS_READ_OPS, ["context", "permissions.list", "location.get", "network.status",
    "network.requests", "network.request", "mocks.list"]);
  assert.deepEqual(TOOLS_MUTATING_OPS, ["permissions.set", "location.set", "location.clear", "simulate",
    "network.start", "network.attach", "network.detach", "network.stop", "mocks.add", "mocks.update",
    "mocks.enable", "mocks.disable", "mocks.remove", "mocks.clear"]);
  assert.equal(new Set(TOOLS_OPS).size, TOOLS_READ_OPS.length + TOOLS_MUTATING_OPS.length);
});

test("errors: each tools error code maps to its HTTP status, anything else is 502", () => {
  for (const code of ["flow_command_invalid", "invalid_value", "invalid_coordinates", "unknown_privacy_service",
    "invalid_simulator_action", "consent_required", "consent_declined", "selector_required"]) {
    assert.equal(toolsErrorStatus(code), 400, code);
  }
  for (const code of ["mock_not_found", "flow_not_found", "app_not_installed"]) {
    assert.equal(toolsErrorStatus(code), 404, code);
  }
  for (const code of ["unsupported_on_platform", "unsupported_capability", "no_active_session",
    "session_target_mismatch", "emulator_only", "proxy_not_running", "physical_device_attach_unsupported"]) {
    assert.equal(toolsErrorStatus(code), 409, code);
  }
  for (const code of ["backend_failed", "adb_not_found", "something_new", undefined]) {
    assert.equal(toolsErrorStatus(code), 502, String(code));
  }
});

// --- The real Canvas ------------------------------------------------------------------

test("auth: /tools routes need authorization, and their POSTs the CSRF value", async (t) => {
  const { canvas, auth } = await canvasWorld(t);
  for (const [method, path] of [["GET", "/tools/logs"], ["POST", "/tools/call"], ["POST", "/tools/logs"]]) {
    const response = await fetch(`${canvas.origin}${path}`, {
      method, headers: { "Content-Type": "application/json" },
      body: method === "POST" ? JSON.stringify({ op: "context", active: false }) : undefined,
    });
    assert.equal(response.status, 401, `${method} ${path}`);
  }
  for (const path of ["/tools/call", "/tools/logs"]) {
    const wrong = await api(canvas, auth, "POST", path, { op: "context", active: false },
      { headers: { "X-Autonom-Csrf": "not-the-value" } });
    assert.equal(wrong.status, 403, path);
    assert.equal(wrong.body.error, "CSRF token rejected");
    const missing = await fetch(`${canvas.origin}${path}`, {
      method: "POST", headers: { Cookie: auth.cookie, "Content-Type": "application/json" },
      body: JSON.stringify({ op: "context", active: false }),
    });
    assert.equal(missing.status, 403, path);
  }
  const read = await fetch(`${canvas.origin}/tools/logs`, { headers: { Cookie: auth.cookie } });
  assert.equal(read.status, 200);
  const bearer = await fetch(`${canvas.origin}/tools/call`, {
    method: "POST", headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ op: "context" }),
  });
  assert.equal(bearer.status, 200);
  const unknownPath = await api(canvas, auth, "GET", "/tools/other");
  assert.equal(unknownPath.status, 404);
});

test("call: a known op reaches the tools process with its payload and origin; unknown ops and bad bodies are 400", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t);
  const answer = await api(canvas, auth, "POST", "/tools/call", { op: "mocks.list", payload: { a: 1 } },
    { origin: "agent" });
  assert.equal(answer.status, 200);
  assert.deepEqual(answer.body, { ok: true, result: { op: "mocks.list", payload: { a: 1 }, origin: "agent" } });
  // A missing payload is an empty one; an unknown origin header is human, as for input.
  const plain = await api(canvas, auth, "POST", "/tools/call", { op: "network.status" }, { origin: "nobody" });
  assert.deepEqual(plain.body.result, { op: "network.status", payload: {}, origin: "human" });
  // A non-protocol line on the tools stdout is skipped.
  const noisy = await api(canvas, auth, "POST", "/tools/call", { op: "mocks.list", payload: { fake: "noise" } });
  assert.equal(noisy.status, 200);

  const unknown = await api(canvas, auth, "POST", "/tools/call", { op: "shell", payload: {} });
  assert.equal(unknown.status, 400);
  assert.equal(unknown.body.error_code, "flow_command_invalid");
  assert.match(unknown.body.error, /mocks\.list/);
  for (const body of [[], { op: "context", payload: [] }, { op: "context", payload: "x" }]) {
    const bad = await api(canvas, auth, "POST", "/tools/call", body);
    assert.equal(bad.status, 400, JSON.stringify(body));
    assert.equal(bad.body.error_code, "invalid_value");
  }
  const tooLarge = await api(canvas, auth, "POST", "/tools/call",
    { op: "mocks.add", payload: { body: "x".repeat(70 * 1024) } });
  assert.equal(tooLarge.status, 413);
  const requests = await toolsRequests(world);
  assert.deepEqual(requests.map((request) => request.op), ["mocks.list", "network.status", "mocks.list"]);
  // The tools process got the Canvas target.
  const [start] = (await lines(world.toolsLog)).filter((entry) => entry.start);
  assert.deepEqual(start.argv, ["--platform", "android", "--target", SERIAL, "--tool", world.fakeAdb]);
  const reported = await status(canvas);
  assert.deepEqual(reported.tools, { available: true, logs: { running: false, package: null } });
});

test("control: mutating ops are refused 403 while input is paused or another origin owns control; read ops never", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t);
  const call = (op, origin = "human") => api(canvas, auth, "POST", "/tools/call", { op, payload: {} }, { origin });

  assert.equal((await api(canvas, auth, "POST", "/control", { mode: "pause" })).status, 200);
  for (const op of TOOLS_MUTATING_OPS) {
    const refused = await call(op);
    assert.equal(refused.status, 403, op);
    assert.equal(refused.body.error_code, "control_refused");
    assert.match(refused.body.error, /paused/);
  }
  for (const op of TOOLS_READ_OPS) assert.equal((await call(op)).status, 200, op);
  // The log feed is not a device mutation.
  assert.equal((await api(canvas, auth, "POST", "/tools/logs", { active: false })).status, 200);
  assert.equal((await api(canvas, auth, "POST", "/control", { mode: "resume" })).status, 200);

  assert.equal((await api(canvas, auth, "POST", "/control", { mode: "takeover" }, { origin: "agent" })).status, 200);
  const human = await call("mocks.add");
  assert.equal(human.status, 403);
  assert.equal(human.body.error_code, "control_refused");
  assert.match(human.body.error, /owned by agent/);
  assert.equal((await call("mocks.list")).status, 200);
  assert.equal((await call("mocks.add", "agent")).status, 200);
  assert.equal((await api(canvas, auth, "POST", "/control", { mode: "release" }, { origin: "agent" })).status, 200);
  assert.equal((await call("mocks.clear")).status, 200);

  // A refused op never reached the tools process.
  const sent = (await toolsRequests(world)).map((request) => `${request.origin}:${request.op}`);
  assert.deepEqual(sent, [...TOOLS_READ_OPS.map((op) => `human:${op}`), "human:mocks.list", "agent:mocks.add",
    "human:mocks.clear"]);
});

test("errors: a tools failure keeps error, hint and capability, with the status of its code", async (t) => {
  const { canvas, auth } = await canvasWorld(t);
  const cases = [
    ["invalid_value", 400], ["consent_required", 400], ["mock_not_found", 404], ["app_not_installed", 404],
    ["no_active_session", 409], ["unsupported_on_platform", 409], ["backend_failed", 502], ["brand_new_code", 502],
  ];
  for (const [code, expected] of cases) {
    const answer = await api(canvas, auth, "POST", "/tools/call", {
      op: "mocks.list", payload: { fake: "error", code, hint: "try this", capability: "network.mitm" },
    });
    assert.equal(answer.status, expected, code);
    assert.deepEqual(answer.body, { ok: false, error: "fake failure", error_code: code, hint: "try this",
      capability: "network.mitm" });
  }
  const bare = await api(canvas, auth, "POST", "/tools/call", { op: "mocks.list", payload: { fake: "error" } });
  assert.equal(bare.status, 502);
  assert.deepEqual(bare.body, { ok: false, error: "fake failure", error_code: "backend_failed", hint: null,
    capability: null });
});

test("restart: a tools process that dies answers 502 tools_unavailable, the next call restarts it once, and input is unaffected", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t);
  const call = (payload = {}) => api(canvas, auth, "POST", "/tools/call", { op: "mocks.list", payload });
  assert.equal((await call()).status, 200);

  const died = await call({ fake: "die" });
  assert.equal(died.status, 502);
  assert.equal(died.body.error_code, "tools_unavailable");
  assert.equal((await status(canvas)).tools.available, false);
  // Device input does not go through the tools process.
  assert.equal((await api(canvas, auth, "POST", "/key", { key: "KEYCODE_HOME" })).status, 200);

  const restarted = await call();
  assert.equal(restarted.status, 200);
  assert.equal((await toolsPids(world)).length, 2);
  assert.equal((await status(canvas)).tools.available, true);

  // A second death within 10 s of the restart is not restarted.
  assert.equal((await call({ fake: "die" })).body.error_code, "tools_unavailable");
  const refused = await call();
  assert.equal(refused.status, 502);
  assert.equal(refused.body.error_code, "tools_unavailable");
  assert.equal((await toolsPids(world)).length, 2);
  assert.equal((await api(canvas, auth, "POST", "/key", { key: "KEYCODE_BACK" })).status, 200);
});

test("hang: a /key input is answered while a tools call hangs", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t);
  const controller = new AbortController();
  const hanging = api(canvas, auth, "POST", "/tools/call", { op: "network.status", payload: { fake: "hang" } },
    { signal: controller.signal }).catch((error) => error);
  await waitFor(async () => (await toolsRequests(world)).length === 1, 5000, "the hanging call");
  const started = performance.now();
  const key = await api(canvas, auth, "POST", "/key", { key: "KEYCODE_HOME" });
  assert.equal(key.status, 200);
  assert.ok(performance.now() - started < 2000);
  assert.equal((await status(canvas)).tools.available, true);
  const keys = (await lines(world.bridgeLog)).filter((message) => message.op === "key");
  assert.equal(keys.length, 1);
  controller.abort();
  assert.equal((await hanging).name, "AbortError");
});

test("logs: the feed runs logs follow on this target, reads after a cursor, and eof ends it with its reason", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t, { env: { FAKE_LOG_LINES: "5", FAKE_LOG_EOF: "max_seconds" } });
  const started = await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.example.app" });
  assert.equal(started.status, 200);
  assert.equal(started.body.ok, true);
  assert.equal(started.body.package, "com.example.app");
  const [run] = await logRuns(world, 1);
  assert.deepEqual(run.args, ["--platform", "android", "--serial", SERIAL, "--adb", world.fakeAdb, "logs", "follow",
    "--source", "device", "--package", "com.example.app"]);

  const ended = await waitFor(async () => {
    const read = await api(canvas, auth, "GET", "/tools/logs?after=0");
    return read.body.running === false && read.body.lines.length === 5 ? read.body : null;
  }, 5000, "the eof");
  assert.match(ended.error, /max_seconds/);
  assert.equal(ended.package, "com.example.app");
  assert.equal(ended.dropped, 0);
  assert.deepEqual(ended.lines.map((line) => line.seq), [1, 2, 3, 4, 5]);
  assert.deepEqual(ended.lines[0], { kind: "line", source: "device", ts: "2026-10-07T00:00:00Z", text: "line 1",
    package: "com.example.app", seq: 1 });
  assert.equal(ended.next, 5);

  const after = (await api(canvas, auth, "GET", "/tools/logs?after=3")).body;
  assert.deepEqual(after.lines.map((line) => line.seq), [4, 5]);
  assert.equal(after.next, 5);
  const none = (await api(canvas, auth, "GET", "/tools/logs?after=5")).body;
  assert.deepEqual(none.lines, []);
  assert.equal(none.next, 5);
  const limited = (await api(canvas, auth, "GET", "/tools/logs?after=0&limit=2")).body;
  assert.deepEqual(limited.lines.map((line) => line.seq), [1, 2]);
  assert.equal(limited.next, 2);
  assert.deepEqual((await status(canvas)).tools.logs, { running: false, package: "com.example.app" });

  for (const query of ["limit=0", "limit=501", "limit=x", "after=-1", "after=1.5"]) {
    const bad = await api(canvas, auth, "GET", `/tools/logs?${query}`);
    assert.equal(bad.status, 400, query);
    assert.equal(bad.body.error_code, "invalid_value");
  }
  for (const body of [{ active: "yes" }, { active: true, package: "com.example; rm" }, { active: true, package: 5 },
    { active: true, package: "" }, { active: true, package: "a".repeat(256) }]) {
    const bad = await api(canvas, auth, "POST", "/tools/logs", body);
    assert.equal(bad.status, 400, JSON.stringify(body));
    assert.equal(bad.body.error_code, "invalid_value");
  }
  assert.equal((await lines(world.logArgs)).filter((entry) => entry.pid).length, 1);
});

// Regression: on a booted iOS Simulator (thousands of lines a second) a marker line left the
// ring before a reader got to it; `match` narrows the feed at its source instead.
test("logs: match narrows the feed at its source as literal text, and a new match restarts it", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t, { env: { FAKE_LOG_LINES: "1" } });
  const started = await api(canvas, auth, "POST", "/tools/logs", { active: true, match: "Marker (x).1" });
  assert.equal(started.status, 200);
  assert.equal(started.body.running, true);
  assert.equal(started.body.match, "Marker (x).1");
  const [run] = await logRuns(world, 1);
  assert.deepEqual(run.args.slice(-6), ["logs", "follow", "--source", "device", "--grep", "Marker\\ \\(x\\)\\.1"]);
  // The same match keeps the running child; another one restarts it.
  assert.equal((await api(canvas, auth, "POST", "/tools/logs", { active: true, match: "Marker (x).1" })).body.running, true);
  await sleep(200);
  assert.equal((await logPids(world)).length, 1);
  const changed = await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.example.app", match: "other" });
  assert.equal(changed.body.match, "other");
  const runs = await logRuns(world, 2);
  assert.deepEqual(runs[1].args.slice(-4), ["--package", "com.example.app", "--grep", "other"]);
  // No match: the whole log again, without --grep.
  assert.equal((await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.example.app" })).body.match, null);
  const third = (await logRuns(world, 3))[2];
  assert.equal(third.args.includes("--grep"), false);
  for (const match of ["", 5, "a\nb", "x".repeat(201), "caf\u00e9", ["a"]]) {
    const bad = await api(canvas, auth, "POST", "/tools/logs", { active: true, match });
    assert.equal(bad.status, 400, JSON.stringify(match));
    assert.equal(bad.body.error_code, "invalid_value");
  }
});

test("logs: literalPattern escapes every character but letters, digits and _", () => {
  assert.equal(literalPattern("autonom-tools-marker-ab12"), "autonom\\-tools\\-marker\\-ab12");
  assert.equal(literalPattern("a.b*c(d)[e]|f^$ g\\h"), "a\\.b\\*c\\(d\\)\\[e\\]\\|f\\^\\$\\ g\\\\h");
  const printable = Array.from({ length: 0x7f - 0x20 }, (_, index) => String.fromCharCode(0x20 + index)).join("");
  const pattern = new RegExp(literalPattern(printable), "i");
  assert.ok(pattern.test(`x${printable.toUpperCase()}y`));
  assert.equal(new RegExp(literalPattern("a.c"), "i").test("abc"), false);
});

test("logs: the ring keeps the last 2000 entries and counts the evicted ones as dropped", async (t) => {
  const { canvas, auth } = await canvasWorld(t, { env: { FAKE_LOG_LINES: "2600" } });
  await api(canvas, auth, "POST", "/tools/logs", { active: true });
  await waitFor(async () => {
    const read = await api(canvas, auth, "GET", "/tools/logs?after=2599");
    return read.body.lines.length === 1;
  }, 5000, "every line");
  const oldest = (await api(canvas, auth, "GET", "/tools/logs?after=0&limit=500")).body;
  assert.equal(oldest.running, true);
  assert.equal(oldest.package, null);
  assert.equal(oldest.dropped, 600);
  assert.equal(oldest.lines.length, 500);
  assert.equal(oldest.lines[0].seq, 601);
  assert.equal(oldest.next, 1100);
  const seen = (await api(canvas, auth, "GET", "/tools/logs?after=1000")).body;
  assert.equal(seen.dropped, 0);
  assert.equal(seen.lines[0].seq, 1001);
  assert.equal(LOG_MAX_ENTRIES, 2000);
  assert.deepEqual((await status(canvas)).tools.logs, { running: true, package: null });
});

test("stop: active false and a package change stop the old feed; Canvas stop leaves no tools, log or reader process", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t, { env: { FAKE_LOG_GRANDCHILD: "1", FAKE_LOG_LINES: "3" } });
  await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.one" });
  // The same package again keeps the running feed.
  await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.one" });
  await logRuns(world, 1);
  await sleep(100);
  assert.equal((await logPids(world)).length, 1);
  await waitFor(async () => (await api(canvas, auth, "GET", "/tools/logs")).body.lines.length === 3, 5000, "lines");

  const changed = await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.two" });
  assert.equal(changed.body.running, true);
  assert.equal(changed.body.package, "com.two");
  const [first, second] = (await logRuns(world, 2)).map((entry) => entry.pid);
  const [firstReader] = await waitFor(async () => {
    const readers = await grandchildPids(world);
    return readers.length >= 1 ? readers : null;
  }, 5000, "the first reader");
  assert.ok(second);
  await waitFor(() => !isAlive(first) && !isAlive(firstReader), 3000, "the first feed to stop");
  // The new feed starts a fresh buffer: the old package's lines are cleared, not dropped.
  const fresh = await waitFor(async () => {
    const read = (await api(canvas, auth, "GET", "/tools/logs?after=0")).body;
    return read.lines.length === 3 ? read : null;
  }, 5000, "the new feed's lines");
  assert.deepEqual(fresh.lines.map((line) => line.seq), [4, 5, 6]);
  assert.equal(fresh.lines[0].package, "com.two");
  assert.equal(fresh.dropped, 0);

  const stopped = await api(canvas, auth, "POST", "/tools/logs", { active: false });
  assert.deepEqual(stopped.body, { ok: true, running: false, package: "com.two", match: null, error: null });
  await waitFor(() => !isAlive(second), 3000, "the second feed to stop");
  const after = (await api(canvas, auth, "GET", "/tools/logs?after=0")).body;
  assert.equal(after.running, false);
  assert.equal(after.error, null);

  await api(canvas, auth, "POST", "/tools/logs", { active: true });
  await logRuns(world, 3);
  await waitFor(async () => (await grandchildPids(world)).length === 3, 5000, "the third reader");
  const running = [(await toolsPids(world))[0], (await logPids(world))[2], (await grandchildPids(world))[2]];
  assert.ok(running.every(isAlive));
  const pids = [...await toolsPids(world), ...await logPids(world), ...await grandchildPids(world)];
  assert.equal(pids.length, 1 + 3 + 3);
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0, exit.output);
  await waitFor(() => pids.every((pid) => !isAlive(pid)), 3000, "no process left");
});

test("ios: the log feed starts only with a session on this Simulator", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t, { platform: "ios", env: { FAKE_LOG_LINES: "1" } });
  const refused = await api(canvas, auth, "POST", "/tools/logs", { active: true, package: "com.example" });
  assert.equal(refused.status, 200);
  assert.equal(refused.body.running, false);
  assert.match(refused.body.error, new RegExp(`autonom session start --platform ios --udid ${UDID} --log-stream`));
  assert.equal((await logPids(world)).length, 0);
  const [context] = await toolsRequests(world);
  assert.deepEqual(context, { id: 1, op: "context", payload: {}, origin: "system" });
  const read = (await api(canvas, auth, "GET", "/tools/logs")).body;
  assert.equal(read.running, false);
  assert.match(read.error, /--log-stream/);
});

test("ios: with a session on this Simulator the feed follows it by udid", async (t) => {
  const { world, canvas, auth } = await canvasWorld(t, { platform: "ios",
    env: { FAKE_SESSION: "1", FAKE_LOG_LINES: "1" } });
  const started = await api(canvas, auth, "POST", "/tools/logs", { active: true });
  assert.equal(started.body.running, true);
  assert.equal(started.body.error, null);
  const [run] = await logRuns(world, 1);
  assert.deepEqual(run.args, ["--platform", "ios", "--udid", UDID, "--simctl", world.fakeXcrun, "logs", "follow",
    "--source", "device"]);
  await waitFor(async () => (await api(canvas, auth, "GET", "/tools/logs")).body.lines.length === 1, 5000, "a line");
});

// --- In process, with short timers --------------------------------------------------

async function fakeScripts(t) {
  const world = await makeWorld(t);
  return world;
}

function toolsClient(world, options = {}) {
  const client = new ToolsClient({
    spawnTools: () => spawn(process.execPath, [world.fakeTools, "--platform", "android"], {
      stdio: ["pipe", "pipe", "pipe"], env: world.env,
    }),
    ...options,
  });
  client.start();
  return client;
}

test("timeout: a call answers 504 after the timeout, the process is kept, and the late reply is dropped", async (t) => {
  const world = await fakeScripts(t);
  const client = toolsClient(world, { timeoutMs: 200 });
  t.after(() => client.close());
  const slow = await client.call("mocks.list", { fake: "late", ms: 500 }, "human");
  assert.equal(slow.status, 504);
  assert.equal(slow.body.error_code, "timeout");
  assert.equal(client.available(), true);
  const next = await client.call("mocks.list", {}, "human");
  assert.deepEqual(next, { status: 200, body: { ok: true, result: { op: "mocks.list", payload: {}, origin: "human" } } });
  // The late reply arrives meanwhile and answers nothing.
  await sleep(450);
  const after = await client.call("context", {}, "system");
  assert.equal(after.status, 200);
  assert.equal(after.body.result.session, null);
  assert.equal((await toolsPids(world)).length, 1);
});

test("timeout: the route answers 504 for a hung call", async (t) => {
  const world = await fakeScripts(t);
  const client = toolsClient(world, { timeoutMs: 150 });
  t.after(() => client.close());
  const answer = await handleToolsRoute({ call: (...args) => client.call(...args), feed: null }, {
    method: "POST", url: new URL("http://127.0.0.1/tools/call"), origin: "human",
    readBody: async () => ({ op: "network.attach", payload: { fake: "hang", acknowledged: true } }),
    refusal: () => null,
  });
  assert.equal(answer.status, 504);
  assert.equal(answer.body.ok, false);
  assert.equal(answer.body.error_code, "timeout");
});

test("restart: no tools script at all answers tools_unavailable instead of failing", async (t) => {
  const client = new ToolsClient({
    spawnTools: () => spawn(join(tmpdir(), "no-such-python-for-autonom-tools"), [], { stdio: ["pipe", "pipe", "pipe"] }),
  });
  client.start();
  t.after(() => client.close());
  const answer = await client.call("context", {}, "system");
  assert.equal(answer.status, 502);
  assert.equal(answer.body.error_code, "tools_unavailable");
  assert.equal(client.available(), false);
});

// A child process stand-in: its exit and stdout are driven by the test.
function scriptedChild(pid) {
  const child = new EventEmitter();
  child.pid = pid;
  child.exitCode = null;
  child.signalCode = null;
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.requests = [];
  createLineReader(child.stdin, (line) => child.requests.push(JSON.parse(line)));
  child.exit = (code = 1) => {
    if (child.exitCode !== null) return;
    child.exitCode = code;
    child.emit("exit", code, null);
  };
  child.kill = () => { child.exit(0); child.stdout.end(); child.emit("close", 0, null); };
  child.reply = (id, result) => child.stdout.write(`${JSON.stringify({ id, ok: true, result })}\n`);
  return child;
}

function createLineReader(stream, onLine) {
  let buffer = "";
  stream.on("data", (chunk) => {
    buffer += chunk.toString();
    for (let index = buffer.indexOf("\n"); index >= 0; index = buffer.indexOf("\n")) {
      onLine(buffer.slice(0, index));
      buffer = buffer.slice(index + 1);
    }
  });
}

// Regression: a restart in the drain window after an exit replaced this.child, and the old
// child's last reply was thrown away, so a call that succeeded was answered 502.
test("restart: a reply that drains from the exited child after a restart still answers its own call", async (t) => {
  const children = [];
  const client = new ToolsClient({
    spawnTools: () => { const child = scriptedChild(1000 + children.length); children.push(child); return child; },
  });
  client.start();
  t.after(() => client.close());
  const first = client.call("mocks.add", { url_glob: "*x*" }, "human");
  await waitFor(() => children[0].requests.length === 1, 2000, "the first request");
  const firstId = children[0].requests[0].id;
  children[0].exit(1);
  // The next call restarts the process before the old child's stdout has drained.
  const second = client.call("mocks.list", {}, "human");
  assert.equal(children.length, 2);
  await waitFor(() => children[1].requests.length === 1, 2000, "the second request");
  const secondId = children[1].requests[0].id;
  // The old child names the new call's id: it must not answer a call it never received.
  children[0].stdout.write(`${JSON.stringify({ id: secondId, ok: true, result: { from: "old" } })}\n`);
  children[0].reply(firstId, { id: "m1" });
  assert.deepEqual(await first, { status: 200, body: { ok: true, result: { id: "m1" } } });
  let settled = false;
  second.then(() => { settled = true; });
  await sleep(50);
  assert.equal(settled, false, "a reply from another child never answers this call");
  children[1].reply(secondId, { mocks: [] });
  assert.deepEqual(await second, { status: 200, body: { ok: true, result: { mocks: [] } } });
  // Once the old child is gone for good, nothing of it is left pending.
  children[0].stdout.end();
  children[0].emit("close", 1, null);
  assert.equal(client.pending.size, 0);
});

// Regression: `dropped` re-counted the same lost seq on every read whose cursor was below it.
test("logs: each dropped seq is counted once per cursor, however the reads are paged", () => {
  const feed = new LogFeed({ spawnFollow: () => { throw new Error("not used"); } });
  const run = { ended: false, stopping: true };
  feed.run = run;
  const line = (text) => feed.onLine(run, Buffer.from(JSON.stringify({ kind: "line", text })));
  const poll = (after, limit, rounds) => {
    let cursor = after;
    let total = 0;
    const pages = [];
    for (let i = 0; i < rounds; i += 1) {
      const read = feed.read(cursor, limit);
      pages.push({ seqs: read.lines.map((entry) => entry.seq), next: read.next, dropped: read.dropped });
      total += read.dropped;
      cursor = read.next;
    }
    return { total, pages, cursor };
  };
  try {
    // The newest seq was dropped: the cursor moves past it and it is counted once.
    line("one");
    feed.dropOne();
    const newest = poll(0, 500, 4);
    assert.deepEqual(newest.pages[0], { seqs: [1], next: 2, dropped: 1 });
    assert.deepEqual(newest.pages.slice(1).map((page) => page.dropped), [0, 0, 0]);
    assert.equal(newest.total, 1);

    // A gap between two pages is counted on the page that passes it, once.
    line("three");
    const paged = poll(0, 1, 3);
    assert.deepEqual(paged.pages, [
      { seqs: [1], next: 1, dropped: 0 },
      { seqs: [3], next: 3, dropped: 1 },
      { seqs: [], next: 3, dropped: 0 },
    ]);

    // A gap past the first page of a long backlog is counted once, not on every page.
    feed.clear();
    const base = feed.seq;
    for (let i = 1; i <= 1200; i += 1) {
      if (i === 900) feed.dropOne();
      else line(`line ${i}`);
    }
    const backlog = poll(base, 500, 4);
    assert.deepEqual(backlog.pages.map((page) => page.dropped), [0, 1, 0, 0]);
    assert.equal(backlog.total, 1);
    assert.equal(backlog.cursor, feed.seq);
  } finally {
    feed.run = null;
    clearTimeout(feed.idleTimer);
  }
});

function logFeed(world, options = {}, env = {}) {
  return new LogFeed({
    spawnFollow: (packageName) => spawn(process.execPath,
      [world.fakeAutonom, "logs", "follow", ...(packageName ? ["--package", packageName] : [])],
      { stdio: ["ignore", "pipe", "pipe"], env: { ...world.env, ...env }, detached: true }),
    ...options,
  });
}

test("logs: the feed stops when nobody read it for the idle period, and a read keeps it running", async (t) => {
  const world = await fakeScripts(t);
  const feed = logFeed(world, { idleMs: 300 });
  t.after(() => feed.close());
  await feed.set(true, null);
  assert.equal(feed.running(), true);
  for (let i = 0; i < 4; i += 1) {
    await sleep(150);
    feed.read(0, 10);
  }
  assert.equal(feed.running(), true);
  const [pid] = await logPids(world);
  await waitFor(() => !feed.running(), 2000, "the idle stop");
  assert.equal(feed.read(0, 10).error, null);
  await waitFor(() => !isAlive(pid), 3000, "the feed process to exit");
});

test("logs: the ring holds at most its byte budget, and an oversized line is dropped", async (t) => {
  const world = await fakeScripts(t);
  const feed = logFeed(world, { maxBytes: 4096, maxLineBytes: 1024 }, { FAKE_LOG_LINES: "200" });
  t.after(() => feed.close());
  await feed.set(true, "com.bytes");
  await waitFor(() => feed.read(199, 10).lines.length === 1, 5000, "every line");
  assert.ok(feed.bytes <= 4096);
  const read = feed.read(0, 500);
  assert.ok(read.lines.length > 10 && read.lines.length < 200, String(read.lines.length));
  assert.equal(read.dropped + read.lines.length, 200);
  assert.equal(read.lines.at(-1).seq, 200);

  // A line longer than the line limit takes a seq but never enters the ring.
  const run = feed.run;
  feed.onLine(run, Buffer.from(JSON.stringify({ kind: "line", text: "x".repeat(2000) })));
  assert.equal(feed.seq, 201);
  const later = feed.read(200, 10);
  assert.deepEqual(later.lines, []);
  assert.equal(later.dropped, 1);
});

test("logs: a feed that cannot start reports why and is not running", async (t) => {
  const feed = new LogFeed({
    spawnFollow: () => spawn(join(tmpdir(), "no-such-python-for-autonom-logs"), [], { stdio: ["ignore", "pipe", "pipe"] }),
  });
  t.after(() => feed.close());
  await feed.set(true, null);
  await waitFor(() => !feed.running(), 2000, "the failed start");
  assert.match(feed.read(0, 10).error, /could not start|exited/);
});

test("logs: a stop does not wait for a start still checking its precondition, and that start never runs", async (t) => {
  const world = await fakeScripts(t);
  let spawned = 0;
  const feed = new LogFeed({
    spawnFollow: () => {
      spawned += 1;
      return spawn(process.execPath, [world.fakeAutonom], { stdio: ["ignore", "pipe", "pipe"], env: world.env, detached: true });
    },
    checkStart: () => sleep(400).then(() => null),
  });
  t.after(() => feed.close());
  const starting = feed.set(true, "com.slow");
  const before = performance.now();
  const stopped = await feed.set(false);
  assert.ok(performance.now() - before < 200);
  assert.equal(stopped.running, false);
  const started = await starting;
  assert.equal(started.running, false);
  assert.equal(spawned, 0);
  // A later start runs as usual.
  assert.equal((await feed.set(true, "com.slow")).running, true);
  assert.equal(spawned, 1);
});
