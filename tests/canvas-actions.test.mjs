// Unit tests of the Canvas Actions parts (contract sections 5.4 and 5.5): the action op
// allowlists and control classes of the tools route, per-op timeouts, the capture file route
// (headers and Range), the command log (bounds, cursors, redaction), the activity and health
// routes, and the Actions drawer page (markup, styles, and its script in node:vm against a
// small fake DOM). Fakes only: no browser, Canvas, device or network is involved, and every
// file lives in a temporary folder.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { PassThrough } from "node:stream";
import test, { describe } from "node:test";
import { Script, createContext } from "node:vm";

import {
  ACTION_DEVICE_OPS,
  ACTION_LOCAL_OPS,
  ACTION_READ_OPS,
  OP_TIMEOUT_MS,
  TOOLS_MUTATING_OPS,
  TOOLS_OPS,
  TOOLS_READ_OPS,
  ToolsClient,
  callSummary,
  contentDisposition,
  createCanvasTools,
  handleToolsRoute,
  isCallableOp,
  isControlledOp,
  parseRange,
  toolsErrorStatus,
  toolsReplyResponse,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-server.mjs";
import {
  COMMAND_LOG_MAX,
  activityApiRoute,
  createActivityApiRoute,
  createCommandLog,
  redactText,
  runToolHealth,
  urlOrigin,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-activity.mjs";
import {
  ACTIONS_TABS,
  actionsButton,
  actionsMarkup,
  actionsScript,
  actionsStyles,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-actions-page.mjs";
import {
  toolsButton,
  toolsMarkup,
  toolsScript,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-page.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const DEVICE = "android~emulator-5580";

async function tempDir(t) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-actions-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

// --- allowlists and control classes -----------------------------------------------------

describe("action ops", () => {
  test("the action tuples are the contract's and the same as the Python process's", () => {
    assert.deepEqual(ACTION_READ_OPS, ["captures.list", "record.status", "apps.candidates", "health"]);
    assert.deepEqual(ACTION_DEVICE_OPS, ["capture.screenshot", "record.start", "record.stop", "app.install",
      "app.launch", "app.open_url", "app.locale"]);
    assert.deepEqual(ACTION_LOCAL_OPS, ["captures.delete"]);
    const python = JSON.parse(execFileSync(process.env.PYTHON ?? "python3", ["-c",
      "import json, autonom_canvas_tools as t; print(json.dumps([t.ACTION_READ_OPS, t.ACTION_DEVICE_OPS, " +
      "t.ACTION_LOCAL_OPS, t.INTERNAL_OPS, t.READ_OPS, t.MUTATING_OPS]))"],
    { cwd: join(ROOT, "scripts"), env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" }, encoding: "utf8" }));
    assert.deepEqual(python, [ACTION_READ_OPS, ACTION_DEVICE_OPS, ACTION_LOCAL_OPS, ["captures.file"],
      TOOLS_READ_OPS, TOOLS_MUTATING_OPS]);
  });

  test("/tools/call takes the Tools and Actions ops, never captures.file; control covers device ops only", () => {
    for (const op of [...TOOLS_OPS, ...ACTION_READ_OPS, ...ACTION_DEVICE_OPS, ...ACTION_LOCAL_OPS]) {
      assert.equal(isCallableOp(op), true, op);
    }
    for (const op of ["captures.file", "shell", "", null, 3]) assert.equal(isCallableOp(op), false, String(op));
    for (const op of [...TOOLS_MUTATING_OPS, ...ACTION_DEVICE_OPS]) assert.equal(isControlledOp(op), true, op);
    for (const op of [...TOOLS_READ_OPS, ...ACTION_READ_OPS, ...ACTION_LOCAL_OPS, "captures.file"]) {
      assert.equal(isControlledOp(op), false, op);
    }
  });

  test("the new error codes map to their HTTP statuses, additively", () => {
    assert.equal(toolsErrorStatus("install_path_not_allowed"), 403);
    for (const code of ["capture_not_found", "install_path_not_found"]) assert.equal(toolsErrorStatus(code), 404, code);
    for (const code of ["install_not_configured", "recording_already_active", "recording_not_active"]) {
      assert.equal(toolsErrorStatus(code), 409, code);
    }
    assert.equal(toolsErrorStatus("capture_too_large"), 413);
    assert.equal(toolsErrorStatus("install_failed"), 502);
    assert.equal(toolsErrorStatus("mock_not_found"), 404);
    const answer = toolsReplyResponse({ id: 1, ok: false, error_code: "install_failed", error: "no",
      reason: "INSTALL_FAILED_VERSION_DOWNGRADE" });
    assert.equal(answer.status, 502);
    assert.equal(answer.body.reason, "INSTALL_FAILED_VERSION_DOWNGRADE");
  });

  test("the route refuses device ops under the control rule and lets reads and gallery deletes through", async () => {
    const sent = [];
    const tools = { call: async (op, payload, origin) => { sent.push(`${origin}:${op}`); return { status: 200, body: { ok: true, result: {} } }; } };
    const route = (op, refused = null) => handleToolsRoute(tools, {
      method: "POST", url: new URL("http://x/tools/call"), origin: "human",
      readBody: async () => ({ op, payload: {} }), refusal: () => refused,
    });
    for (const op of ACTION_DEVICE_OPS) {
      const answer = await route(op, "Input is paused");
      assert.equal(answer.status, 403, op);
      assert.equal(answer.body.error_code, "control_refused");
    }
    for (const op of [...ACTION_READ_OPS, ...ACTION_LOCAL_OPS]) assert.equal((await route(op, "Input is paused")).status, 200, op);
    for (const op of ACTION_DEVICE_OPS) assert.equal((await route(op)).status, 200, op);
    const internal = await route("captures.file");
    assert.equal(internal.status, 400);
    assert.equal(internal.body.error_code, "flow_command_invalid");
    assert.match(internal.body.error, /capture\.screenshot/);
    assert.ok(!sent.includes("human:captures.file"));
    assert.deepEqual(sent.filter((item) => item.endsWith("record.start")), ["human:record.start"]);
  });
});

// --- per-op timeouts -----------------------------------------------------------------------

function silentChild() {
  const child = new EventEmitter();
  child.pid = 4242;
  child.exitCode = null;
  child.signalCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.stdin = new PassThrough();
  child.stdin.on("data", () => {});
  child.kill = () => { child.exitCode = 0; child.emit("exit", 0, null); child.stdout.end(); child.emit("close", 0, null); return true; };
  return child;
}

test("timeouts: install, recording and health have their own limits; other ops keep the default", async (t) => {
  assert.deepEqual(OP_TIMEOUT_MS, { "app.install": 300000, "record.start": 180000, "record.status": 180000,
    "record.stop": 180000, health: 30000 });
  const client = new ToolsClient({ spawnTools: silentChild, timeoutMs: 20, opTimeoutsMs: { "app.install": 120 } });
  t.after(() => client.close());
  assert.equal(client.timeoutFor("app.install"), 120);
  assert.equal(client.timeoutFor("captures.list"), 20);
  const started = performance.now();
  const quick = await client.call("captures.list", {}, "human");
  const quickMs = performance.now() - started;
  assert.equal(quick.status, 504);
  const slowStart = performance.now();
  const slow = await client.call("app.install", {}, "human");
  assert.equal(slow.status, 504);
  assert.ok(performance.now() - slowStart >= 110, "the install waits its own limit");
  assert.ok(quickMs < 110, "other ops keep the default");
  const defaults = new ToolsClient({ spawnTools: silentChild });
  t.after(() => defaults.close());
  assert.equal(defaults.timeoutFor("app.install"), 300000);
  assert.equal(defaults.timeoutFor("record.stop"), 180000);
  assert.equal(defaults.timeoutFor("record.start"), 180000);
  assert.equal(defaults.timeoutFor("record.status"), 180000);
  assert.equal(defaults.timeoutFor("health"), 30000);
  assert.equal(defaults.timeoutFor("context"), 60000);
});

// --- capture file route ------------------------------------------------------------------

async function fileServer(t, lookup) {
  const calls = [];
  const tools = { call: async (op, payload, origin) => { calls.push({ op, payload, origin }); return lookup(payload); } };
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1");
    const answer = await handleToolsRoute(tools, { method: request.method, url, origin: "human",
      readBody: async () => ({}), refusal: () => null, request, response });
    if (answer.handled) return;
    response.writeHead(answer.status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(answer.body));
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  t.after(() => new Promise((done) => server.close(done)));
  return { origin: `http://127.0.0.1:${server.address().port}`, calls };
}

test("file route: streams an indexed capture with safe headers, single ranges and 416", async (t) => {
  const directory = await tempDir(t);
  const path = join(directory, "Pixel 8 2026-10-07 09.05.03.mp4");
  const data = Buffer.from("0123456789abcdef");
  await writeFile(path, data);
  const { origin, calls } = await fileServer(t, ({ name }) => (name === "Pixel 8 2026-10-07 09.05.03.mp4"
    ? { status: 200, body: { ok: true, result: { name, path, content_type: "video/mp4", size: data.length } } }
    : { status: 404, body: { ok: false, error: "no capture", error_code: "capture_not_found", hint: null, capability: null } }));
  const address = `${origin}/tools/captures/${encodeURIComponent("Pixel 8 2026-10-07 09.05.03.mp4")}`;

  const whole = await fetch(address);
  assert.equal(whole.status, 200);
  assert.deepEqual(Buffer.from(await whole.arrayBuffer()), data);
  assert.equal(whole.headers.get("content-type"), "video/mp4");
  assert.equal(whole.headers.get("content-length"), "16");
  assert.equal(whole.headers.get("cache-control"), "no-store");
  assert.equal(whole.headers.get("x-content-type-options"), "nosniff");
  assert.equal(whole.headers.get("content-security-policy"), "default-src 'none'; sandbox");
  assert.equal(whole.headers.get("accept-ranges"), "bytes");
  assert.equal(whole.headers.get("content-disposition"), 'inline; filename="Pixel 8 2026-10-07 09.05.03.mp4"');
  assert.deepEqual(calls[0], { op: "captures.file", payload: { name: "Pixel 8 2026-10-07 09.05.03.mp4" }, origin: "human" });

  const part = await fetch(address, { headers: { Range: "bytes=2-5" } });
  assert.equal(part.status, 206);
  assert.equal(await part.text(), "2345");
  assert.equal(part.headers.get("content-range"), "bytes 2-5/16");
  assert.equal(part.headers.get("content-length"), "4");
  const tail = await fetch(address, { headers: { Range: "bytes=-3" } });
  assert.equal(tail.status, 206);
  assert.equal(await tail.text(), "def");
  const open = await fetch(address, { headers: { Range: "bytes=14-" } });
  assert.equal(await open.text(), "ef");
  const past = await fetch(address, { headers: { Range: "bytes=99-" } });
  assert.equal(past.status, 416);
  assert.equal(past.headers.get("content-range"), "bytes */16");
  await past.arrayBuffer();
  const several = await fetch(address, { headers: { Range: "bytes=0-1,4-5" } });
  assert.equal(several.status, 200);
  assert.equal((await several.arrayBuffer()).byteLength, 16);

  const missing = await fetch(`${origin}/tools/captures/other.png`);
  assert.equal(missing.status, 404);
  assert.equal((await missing.json()).error_code, "capture_not_found");
  for (const name of ["%2E%2E%2Fsecret", ".autonom-captures.json", "a%2Fb.png"]) {
    const refused = await fetch(`${origin}/tools/captures/${name}`);
    assert.equal(refused.status, 404, name);
    await refused.arrayBuffer();
  }
  assert.equal(calls.length, 7, "names that cannot be captures never reach the tools process");
});

test("file route: without a response object it answers 501; a lookup that is not a file is 502", async () => {
  const tools = { call: async () => ({ status: 200, body: { ok: true, result: { path: null } } }) };
  const answer = await handleToolsRoute(tools, { method: "GET", url: new URL("http://x/tools/captures/a.png"),
    origin: "human", readBody: async () => ({}), refusal: () => null });
  assert.equal(answer.status, 501);
  assert.equal(answer.body.error_code, "unsupported_capability");
  const fake = { writeHead() { throw new Error("not reached"); } };
  const bad = await handleToolsRoute(tools, { method: "GET", url: new URL("http://x/tools/captures/a.png"),
    origin: "human", readBody: async () => ({}), refusal: () => null, response: fake });
  assert.equal(bad.status, 502);
});

test("ranges and content disposition", () => {
  assert.deepEqual(parseRange("bytes=0-0", 10), { start: 0, end: 0 });
  assert.deepEqual(parseRange("bytes=5-100", 10), { start: 5, end: 9 });
  assert.deepEqual(parseRange("bytes=-20", 10), { start: 0, end: 9 });
  assert.equal(parseRange("bytes=10-", 10), "unsatisfiable");
  assert.equal(parseRange("bytes=-0", 10), "unsatisfiable");
  assert.equal(parseRange("bytes=0-", 0), "unsatisfiable");
  assert.equal(parseRange("bytes=5-2", 10), null);
  assert.equal(parseRange("items=0-5", 10), null);
  assert.equal(parseRange(undefined, 10), null);
  assert.equal(contentDisposition('a"b.png'), 'inline; filename="a_b.png"');
  assert.equal(contentDisposition("Пиксель 1.png"),
    "inline; filename=\"_______ 1.png\"; filename*=UTF-8''%D0%9F%D0%B8%D0%BA%D1%81%D0%B5%D0%BB%D1%8C%201.png");
});

// --- createCanvasTools: process arguments and the command log ----------------------------

const FAKE_TOOLS = String.raw`
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
appendFileSync(process.env.FAKE_TOOLS_LOG, JSON.stringify({ argv: process.argv.slice(2) }) + "\n");
createInterface({ input: process.stdin }).on("line", (line) => {
  const { id, op, payload = {} } = JSON.parse(line);
  const reply = payload.fail
    ? { id, ok: false, error_code: "install_path_not_allowed", error: "outside https://secret.example.com/p?q=1" }
    : { id, ok: true, result: { op } };
  process.stdout.write(JSON.stringify(reply) + "\n");
});
`;

test("createCanvasTools passes only the given action options and logs every call", async (t) => {
  const directory = await tempDir(t);
  const fake = join(directory, "fake-tools.mjs");
  const log = join(directory, "tools.ndjson");
  await writeFile(fake, FAKE_TOOLS);
  const commandLog = createCommandLog();
  const env = { ...process.env, FAKE_TOOLS_LOG: log };
  const plain = createCanvasTools({ python: process.execPath, toolsPath: fake, platform: "android",
    target: "emulator-5580", tool: "/x/adb", env });
  t.after(() => plain.close());
  assert.equal((await plain.call("captures.list", {}, "human")).status, 200);
  const rich = createCanvasTools({ python: process.execPath, toolsPath: fake, platform: "android",
    target: "emulator-5580", tool: "/x/adb", env, installRoots: ["/r1", "/r2"], capturesDir: "/caps",
    deviceId: DEVICE, deviceName: "Pixel 8", commandLog });
  t.after(() => rich.close());
  assert.equal((await rich.call("app.open_url", { url: "https://user:pw@example.com/x?token=SECRET" }, "agent")).status, 200);
  const refused = await rich.call("app.install", { path: "/r1/app.apk", fail: true }, "human");
  assert.equal(refused.status, 403);

  const starts = (await readFile(log, "utf8")).trim().split("\n").map((line) => JSON.parse(line).argv);
  assert.deepEqual(starts[0], ["--platform", "android", "--target", "emulator-5580", "--tool", "/x/adb"]);
  assert.deepEqual(starts[1], ["--platform", "android", "--target", "emulator-5580", "--tool", "/x/adb",
    "--install-root", "/r1", "--install-root", "/r2", "--captures-dir", "/caps", "--device-name", "Pixel 8"]);

  const { entries } = commandLog.list();
  assert.equal(entries.length, 2, "only the logged Canvas's calls");
  assert.deepEqual(entries.map((entry) => [entry.device, entry.kind, entry.name, entry.origin, entry.ok]),
    [[DEVICE, "tools", "app.open_url", "agent", true], [DEVICE, "tools", "app.install", "human", false]]);
  assert.equal(entries[0].summary, "https://example.com");
  assert.equal(entries[1].summary, "app.apk");
  assert.equal(entries[1].error_code, "install_path_not_allowed");
  assert.equal(entries[1].error, "outside https://secret.example.com");
  assert.ok(entries.every((entry) => typeof entry.duration_ms === "number"));
  assert.ok(!JSON.stringify(entries).includes("SECRET"));
});

test("call summaries name things, never contents", () => {
  assert.equal(callSummary("app.open_url", { url: "myapp://profile/42?token=x" }), "myapp://profile");
  assert.equal(callSummary("app.open_url", { url: "tel:+1555" }), "tel:");
  assert.equal(callSummary("app.launch", { app_id: "com.x", fresh: true }), "com.x (fresh)");
  assert.equal(callSummary("app.locale", { app_id: "com.x", locale: "de-DE" }), "com.x de-DE");
  assert.equal(callSummary("app.install", { path: "/Users/me/src/build/app.apk" }), "app.apk");
  assert.equal(callSummary("mocks.add", { body: "secret" }), null);
  assert.equal(callSummary("simulate", { values: { payload: "secret" } }), null);
});

// --- command log ---------------------------------------------------------------------------

describe("command log", () => {
  test("entries carry the contract fields; end fills result, duration and a capped error once", () => {
    let clock = Date.parse("2026-10-07T10:00:00Z");
    const log = createCommandLog({ now: () => clock });
    const handle = log.begin({ device: DEVICE, kind: "input", name: "tap", origin: "human", summary: "tap 10,20" });
    assert.deepEqual(log.list().entries[0], { seq: 1, at: "2026-10-07T10:00:00.000Z", device: DEVICE, kind: "input",
      name: "tap", origin: "human", summary: "tap 10,20", ok: null, duration_ms: null, error_code: null, error: null,
      exit_code: null });
    clock += 42;
    handle.end({ ok: false, errorCode: "backend_failed", error: "x".repeat(400), exitCode: 2 });
    handle.end({ ok: true });
    const [entry] = log.list().entries;
    assert.equal(entry.ok, false);
    assert.equal(entry.duration_ms, 42);
    assert.equal(entry.error_code, "backend_failed");
    assert.equal(entry.error.length, 300);
    assert.equal(entry.exit_code, 2);
    const other = log.begin({ device: "not a device", kind: "nonsense", name: "a b<c>", origin: "Human!" });
    other.end({ ok: true, error: "ignored" });
    const last = log.list().entries[1];
    assert.equal(last.device, null);
    assert.equal(last.kind, "device");
    assert.equal(last.name, "a_b_c_");
    assert.equal(last.origin, null);
    assert.equal(last.error, null);
  });

  test("summaries and errors keep URLs as scheme and host, one line, at most 120 characters", () => {
    assert.equal(redactText("open https://u:p@Example.COM:8443/a/b?token=SECRET#x now"), "open https://example.com now");
    assert.equal(redactText("line\none\u0000two"), "line one two");
    assert.equal(redactText("y".repeat(200)).length, 120);
    assert.equal(redactText(""), null);
    assert.equal(urlOrigin("myapp://profile/42?x=1"), "myapp://profile");
    assert.equal(urlOrigin("mailto:a@b.c"), "mailto:");
    const log = createCommandLog();
    log.begin({ name: "app.open_url", summary: "https://example.com/reset?token=SECRET" }).end({ ok: false,
      errorCode: "backend_failed", error: "failed for https://example.com/reset?token=SECRET" });
    const text = JSON.stringify(log.list());
    assert.ok(!text.includes("SECRET"));
    assert.ok(!text.includes("/reset"));
  });

  test("the ring keeps the newest entries; cursors count what left it once", () => {
    const log = createCommandLog({ max: 3 });
    for (let i = 0; i < 5; i += 1) log.begin({ name: `op${i}` }).end({ ok: true });
    const first = log.list({ after: 0 });
    assert.deepEqual(first.entries.map((entry) => entry.seq), [3, 4, 5]);
    assert.equal(first.dropped, 2);
    assert.equal(first.next, 5);
    assert.equal(first.total, 3);
    const again = log.list({ after: first.next });
    assert.deepEqual(again, { entries: [], next: 5, dropped: 0, total: 3, seen: 5 });
    const paged = log.list({ after: 0, limit: 2 });
    assert.deepEqual(paged.entries.map((entry) => entry.seq), [3, 4]);
    assert.equal(paged.next, 4);
    assert.equal(log.list({ after: paged.next }).dropped, 0);
    assert.equal(log.list({ after: 99 }).entries.length, 3, "a cursor from an older Canvas reads from the start");
    assert.equal(COMMAND_LOG_MAX, 1000);
    const big = createCommandLog();
    for (let i = 0; i < 1005; i += 1) big.begin({ name: "x" }).end({ ok: true });
    assert.equal(big.size, 1000);
    assert.equal(big.clear(), 1000);
    assert.deepEqual(big.list(), { entries: [], next: 1005, dropped: 0, total: 0, seen: 0 });
  });

  test("filters by failures and device; a running entry holds the cursor until it ends", () => {
    const log = createCommandLog();
    log.begin({ device: DEVICE, name: "a" }).end({ ok: true });
    log.begin({ device: DEVICE, name: "b" }).end({ ok: false, errorCode: "timeout" });
    log.begin({ device: "ios~X", name: "c" }).end({ ok: false });
    const running = log.begin({ device: DEVICE, name: "d" });
    log.begin({ device: DEVICE, name: "e" }).end({ ok: true });
    assert.deepEqual(log.list({ failures: true }).entries.map((entry) => entry.name), ["b", "c"]);
    assert.deepEqual(log.list({ device: DEVICE }).entries.map((entry) => entry.name), ["a", "b", "d", "e"]);
    const page = log.list({ device: DEVICE });
    assert.equal(page.next, 3, "held before the running entry");
    running.end({ ok: true });
    const later = log.list({ device: DEVICE, after: page.next });
    assert.deepEqual(later.entries.map((entry) => [entry.name, entry.ok]), [["d", true], ["e", true]]);
    assert.equal(later.next, 5);
  });

  test("entries already shown are not counted as dropped when a running entry that held the cursor leaves the ring", () => {
    const log = createCommandLog({ max: 10 });
    const install = log.begin({ device: DEVICE, name: "app.install" });
    for (let i = 0; i < 4; i += 1) log.begin({ device: DEVICE, name: `tap${i}` }).end({ ok: true });
    const first = log.list({});
    assert.deepEqual(first.entries.map((entry) => entry.seq), [1, 2, 3, 4, 5]);
    assert.equal(first.next, 0, "held before the running install");
    assert.equal(first.seen, 5);
    // 12 more commands push the install and everything shown out of the ring (seqs 1-7 leave).
    for (let i = 0; i < 12; i += 1) log.begin({ device: DEVICE, name: `swipe${i}` }).end({ ok: true });
    install.end({ ok: true });
    const second = log.list({ after: first.next, seen: first.seen });
    assert.deepEqual(second.entries.map((entry) => entry.seq), [8, 9, 10, 11, 12, 13, 14, 15, 16, 17]);
    assert.equal(second.dropped, 2, "only 6 and 7 were never shown");
    assert.equal(second.seen, 17);
    // Without `seen` (an older page) the count is the old upper bound.
    assert.equal(log.list({ after: first.next }).dropped, 7);
    // A `seen` beyond the log (an older Canvas) is ignored.
    assert.equal(log.list({ after: 0, seen: 999 }).dropped, 7);
  });
});

// --- activity and health routes --------------------------------------------------------------

function routeContext(path, { method = "GET", commandLog = null, registry = null } = {}) {
  return { method, url: new URL(`http://127.0.0.1${path}`), origin: "human", commandLog, registry };
}

describe("activity routes", () => {
  test("GET /api/activity validates its query and pages the log", async () => {
    const commandLog = createCommandLog();
    commandLog.begin({ device: DEVICE, name: "tap" }).end({ ok: true });
    commandLog.begin({ device: DEVICE, name: "app.launch" }).end({ ok: false, errorCode: "app_not_installed" });
    const route = createActivityApiRoute({ runHealth: async () => ({ checked_at: null, tools: [] }) });
    const page = await route(routeContext(`/api/activity?device=${encodeURIComponent(DEVICE)}&failures=1`, { commandLog }));
    assert.equal(page.status, 200);
    assert.deepEqual(page.body.entries.map((entry) => entry.name), ["app.launch"]);
    assert.deepEqual(Object.keys(page.body).sort(), ["dropped", "entries", "next", "ok", "seen", "total"]);
    for (const query of ["limit=0", "limit=501", "after=-1", "after=x", "seen=-1", "seen=x", "failures=yes", "device=nope"]) {
      const bad = await route(routeContext(`/api/activity?${query}`, { commandLog }));
      assert.equal(bad.status, 400, query);
      assert.equal(bad.body.error_code, "invalid_value");
    }
    const cleared = await route(routeContext("/api/activity/clear", { method: "POST", commandLog }));
    assert.deepEqual(cleared, { status: 200, body: { ok: true, cleared: 2 } });
    assert.equal(await route(routeContext("/api/workspace", { commandLog })), null, "other routes are not answered");
    assert.equal((await route(routeContext("/api/activity", { method: "DELETE", commandLog }))).status, 405);
    const empty = await route(routeContext("/api/activity"));
    assert.deepEqual(empty.body, { ok: true, entries: [], next: 0, dropped: 0, total: 0, seen: 0 });
    assert.equal(typeof activityApiRoute, "function");
  });

  test("GET /api/health asks the primary device's tools, else a cached check that works with no device", async () => {
    let runs = 0;
    const route = createActivityApiRoute({
      runHealth: async ({ refresh }) => { runs += 1; return { checked_at: `run-${runs}`, tools: [{ name: "adb", ok: true, refresh }] }; },
      canvasFacts: async () => ({ ffmpeg: "/usr/bin/ffmpeg" }),
    });
    const none = await route(routeContext("/api/health", { registry: { primary: () => null } }));
    assert.equal(none.status, 200);
    assert.equal(none.body.ok, true);
    assert.equal(none.body.checked_at, "run-1");
    assert.equal(none.body.tools[0].name, "adb");
    assert.equal(none.body.canvas.ffmpeg, "/usr/bin/ffmpeg");
    assert.deepEqual(none.body.canvas.scrcpy, { available: false, path: null, version: null, source: null, reason: null });
    await route(routeContext("/api/health"));
    assert.equal(runs, 1, "cached");
    await route(routeContext("/api/health?refresh=1"));
    assert.equal(runs, 2, "refresh runs again");

    const asked = [];
    const tools = { call: async (op, payload, origin) => { asked.push([op, payload, origin]); return { status: 200, body: { ok: true, result: { checked_at: "dev", tools: [{ name: "xcrun_simctl", ok: true }] } } }; } };
    const withDevice = await route(routeContext("/api/health", { registry: { primary: () => ({ context: { tools } }) } }));
    assert.equal(withDevice.body.checked_at, "dev");
    assert.deepEqual(asked, [["health", { refresh: false }, "system"]]);
    assert.equal(runs, 2);

    const failing = createActivityApiRoute({ runHealth: async () => { throw new Error("python missing"); } });
    const broken = await failing(routeContext("/api/health"));
    assert.equal(broken.status, 200);
    assert.deepEqual(broken.body.tools, []);
    assert.equal(broken.body.error, "python missing");
  });

  test("the fallback check runs python -m autonom_lib.tool_health in the scripts folder", async (t) => {
    const directory = await tempDir(t);
    const python = join(directory, "python");
    await writeFile(python, `#!/bin/sh\necho "$PWD|$*" > "${join(directory, "args")}"\necho '{"checked_at":"t","tools":[{"name":"adb"}]}'\n`);
    await chmod(python, 0o755);
    const result = await runToolHealth({ python, scriptsDir: directory, refresh: true });
    assert.deepEqual(result, { checked_at: "t", tools: [{ name: "adb" }] });
    const [cwd, args] = (await readFile(join(directory, "args"), "utf8")).trim().split("|");
    assert.equal(await realpath(cwd), await realpath(directory));
    assert.equal(args, "-m autonom_lib.tool_health --refresh");
    const bad = join(directory, "bad");
    await writeFile(bad, "#!/bin/sh\necho nope\nexit 3\n");
    await chmod(bad, 0o755);
    await assert.rejects(runToolHealth({ python: bad, scriptsDir: directory }), /exit 3/);
  });
});

async function realpath(path) {
  const { realpath: real } = await import("node:fs/promises");
  return real(path);
}

// --- page --------------------------------------------------------------------------------

// --- a minimal DOM: enough of the API the drawer script uses ----------------------------------

const VOID = new Set(["input", "br", "hr", "img", "meta", "link"]);
const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const decode = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity]);

class FakeText {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
}

class FakeElement {
  constructor(document, tag) {
    this.nodeType = 1;
    this.ownerDocument = document;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.scrollTop = 0;
    this.clientHeight = 160;
    this.state = {};
  }
  get localName() { return this.tagName.toLowerCase(); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  reflectBoolean(name, value) { if (value) this.setAttribute(name, ""); else this.removeAttribute(name); }
  get id() { return this.getAttribute("id") || ""; }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { this.reflectBoolean("hidden", value); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { this.reflectBoolean("disabled", value); }
  get title() { return this.getAttribute("title") || ""; }
  set title(value) { this.setAttribute("title", value); }
  get placeholder() { return this.getAttribute("placeholder") || ""; }
  set placeholder(value) { this.setAttribute("placeholder", value); }
  get checked() { return "checked" in this.state ? this.state.checked : this.hasAttribute("checked"); }
  set checked(value) { this.state.checked = Boolean(value); }
  get value() {
    if ("value" in this.state) return this.state.value;
    if (this.localName === "textarea") return this.textContent;
    if (this.localName === "select") {
      const options = this.querySelectorAll("option");
      const chosen = options.find((option) => option.hasAttribute("selected")) || options[0];
      return chosen ? chosen.getAttribute("value") ?? chosen.textContent : "";
    }
    return this.getAttribute("value") || "";
  }
  set value(value) { this.state.value = String(value); }
  get classList() {
    const element = this;
    const read = () => element.className.split(/\s+/).filter(Boolean);
    const write = (names) => element.setAttribute("class", names.join(" "));
    return {
      contains: (name) => read().includes(name),
      add: (name) => { if (!read().includes(name)) write([...read(), name]); },
      remove: (name) => write(read().filter((item) => item !== name)),
      toggle: (name, force) => {
        const on = force === undefined ? !read().includes(name) : Boolean(force);
        if (on) { if (!read().includes(name)) write([...read(), name]); } else write(read().filter((item) => item !== name));
        return on;
      },
    };
  }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get scrollHeight() { return this.children.length * 16; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = value === "" ? [] : [new FakeText(String(value))];
    this.childNodes.forEach((node) => { node.parentNode = this; });
  }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, reference) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const index = reference ? this.childNodes.indexOf(reference) : -1;
    if (index < 0) this.childNodes.push(node); else this.childNodes.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index < 0) throw new Error("not a child");
    this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    for (const node of nodes) this.appendChild(node);
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  descendants() {
    const out = [];
    const walk = (element) => { for (const child of element.children) { out.push(child); walk(child); } };
    walk(this);
    return out;
  }
  querySelectorAll(selector) { return this.descendants().filter((element) => element.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return selector.split(",").some((part) => matchesCompound(this, part.trim())); }
  closest(selector) {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  contains(node) { for (let item = node; item; item = item.parentNode) if (item === this) return true; return false; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  focus() { this.ownerDocument.activeElement = this; }
  click() { if (!this.disabled) fire(this, "click"); }
  // innerHTML is deliberately absent: a script that relied on it would fail loudly here.
  set innerHTML(_) { throw new Error("innerHTML is not allowed in the drawers"); }
}

function matchesCompound(element, selector) {
  const pattern = /^([a-z0-9]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/gy;
  let match;
  let consumed = 0;
  pattern.lastIndex = 0;
  while (consumed < selector.length && (match = pattern.exec(selector))) {
    consumed = pattern.lastIndex;
    const [, tag, id, className, attribute, quoted, bare] = match;
    if (tag && element.localName !== tag) return false;
    if (id && element.id !== id) return false;
    if (className && !element.classList.contains(className)) return false;
    if (attribute) {
      if (!element.hasAttribute(attribute)) return false;
      const expected = quoted ?? bare;
      if (expected !== undefined && element.getAttribute(attribute) !== expected) return false;
    }
  }
  if (consumed !== selector.length) throw new Error(`unsupported selector ${selector}`);
  return true;
}

class FakeDocument {
  constructor() {
    this.activeElement = null;
    this.documentElement = new FakeElement(this, "html");
    this.body = null;
  }
  createElement(tag) { return new FakeElement(this, tag); }
  getElementById(id) { return this.documentElement.descendants().find((element) => element.id === id) || null; }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  querySelector(selector) { return this.documentElement.querySelector(selector); }
}

function parseInto(document, parent, html) {
  const token = /<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  const stack = [parent];
  let match;
  let consumed = 0;
  while ((match = token.exec(html))) {
    assert.equal(match.index, consumed, `unparsed markup near ${html.slice(consumed, consumed + 40)}`);
    consumed = token.lastIndex;
    const [, closing, tag, attributes, selfClosing, text] = match;
    const top = stack[stack.length - 1];
    if (closing) {
      assert.equal(top.localName, closing.toLowerCase(), `mismatched </${closing}>`);
      stack.pop();
    } else if (tag) {
      const element = document.createElement(tag.toLowerCase());
      for (const [, name, value] of attributes.matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) element.setAttribute(name, decode(value ?? ""));
      top.appendChild(element);
      if (!selfClosing && !VOID.has(element.localName)) stack.push(element);
    } else if (text) {
      top.appendChild(new FakeText(decode(text)));
    }
  }
  assert.equal(consumed, html.length, "markup parsed to the end");
  assert.equal(stack.length, 1, "every element closed");
}

function parseMarkup(html) {
  const document = new FakeDocument();
  parseInto(document, document.documentElement, html);
  return document;
}

function fire(target, type, properties = {}) {
  let stopped = false;
  const event = {
    type, target, defaultPrevented: false, detail: 1, ...properties,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { stopped = true; },
  };
  for (let node = target; node && !stopped; node = node.parentNode) {
    for (const listener of node.listeners?.get(type) || []) {
      event.currentTarget = node;
      listener(event);
    }
  }
  return event;
}

function click(target) {
  assert.ok(target, "click target exists");
  if (target.disabled) return null;
  return fire(target, "click");
}

const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

// --- fake timers and fetch ------------------------------------------------------------------

function fakeTimers() {
  const timers = new Map();
  let now = 0;
  let next = 1;
  const add = (fn, ms, every) => { const id = next++; timers.set(id, { fn, at: now + Math.max(0, ms || 0), every }); return id; };
  return {
    setInterval: (fn, ms) => add(fn, ms, Math.max(1, ms || 1)),
    setTimeout: (fn, ms) => add(fn, ms, 0),
    clear: (id) => { timers.delete(id); },
    count: () => timers.size,
    async tick(ms) {
      const end = now + ms;
      for (;;) {
        let due = null;
        for (const [id, timer] of timers) if (timer.at <= end && (!due || timer.at < due[1].at)) due = [id, timer];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.every) timer.at += timer.every; else timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
    },
  };
}


function ok(result) { return { status: 200, body: { ok: true, result } }; }

const SHOT = { name: "Pixel 8 2026-10-07 09.05.03.png", kind: "screenshot", platform: "android", target_id: "emulator-5580",
  device_name: "Pixel 8", size: 2048, created_at: "2026-10-07T07:05:03.000Z", duration_ms: null, width: 1080, height: 2400 };
const VIDEO = { ...SHOT, name: "Pixel 8 2026-10-07 09.06.00.mp4", kind: "video", duration_ms: 5000, width: null, height: null };

function defaultReply(op) {
  switch (op) {
    case "record.status": return ok({ recording: false, started_at: null, elapsed_ms: null, limit_s: 180, last: null });
    case "apps.candidates": return ok({ configured: true, roots: ["/src/app/build"], truncated: false,
      candidates: [{ path: "/src/app/build/app-debug.apk", kind: "apk", size: 1000, modified_at: "2026-10-07T07:00:00Z" }] });
    case "captures.list": return ok({ captures: [VIDEO, SHOT], count: 2, total_bytes: 4096,
      limits: { max_items: 200, max_bytes: 2147483648 }, dir: "/tmp/caps/Pixel 8", last_pruned: null });
    case "context": return ok({ platform: "android", target_id: "emulator-5580", emulator: true, session: null, app_id: null,
      privacy_services: null, android_permission_aliases: {}, simulate: {}, location_readable: true,
      location_clearable: false, network_available: false, permissions_readable: true });
    default: return ok({});
  }
}

function bootPage({ platform = "android", reply = () => null, canSend = () => true, deviceId = DEVICE, withTools = true } = {}) {
  const html = `<body><div class="actions">${withTools ? toolsButton() : ""}${actionsButton()}` +
    `<button type="button" class="icon" id="inspector-toggle" aria-pressed="true" aria-controls="inspector">I</button></div>` +
    `<aside class="side" id="inspector" aria-label="Inspector"></aside>${withTools ? toolsMarkup({ platform }) : ""}` +
    `${actionsMarkup({ platform })}</body>`;
  const document = parseMarkup(html);
  document.body = document.documentElement.children[0];
  const timers = fakeTimers();
  const calls = [];
  const fetch = async (path, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    const entry = { path, method: init.method || "GET", headers: init.headers || {}, body, op: body && body.op };
    calls.push(entry);
    const answer = (await reply(entry)) || (path === "/d/x/tools/call" || path === "/tools/call"
      ? defaultReply(body.op)
      : path.startsWith("/api/activity")
        ? { status: 200, body: { ok: true, entries: [], next: 0, dropped: 0, total: 0 } }
        : path.startsWith("/api/health")
          ? { status: 200, body: { ok: true, checked_at: "2026-10-07T07:00:00Z", tools: [], canvas: {} } }
          : { status: 200, body: { ok: true, running: false, lines: [], next: 0, dropped: 0, error: null } });
    return { ok: answer.status < 400, status: answer.status, statusText: "", json: async () => answer.body };
  };
  const messageListeners = [];
  const sandbox = {
    document, fetch, view: { owner: "shared", paused: false, status: {} }, csrf: "csrf-123", TextEncoder, console,
    $: (id) => document.getElementById(id),
    url: (path) => path,
    note: () => {},
    canSendInput: () => canSend(),
    setInterval: timers.setInterval, clearInterval: timers.clear, setTimeout: timers.setTimeout, clearTimeout: timers.clear,
    location: { origin: "http://127.0.0.1:3277" },
    addEventListener: (type, listener) => { if (type === "message") messageListeners.push(listener); },
    DEVICE_ID: deviceId, BASE: "", EMBED: false,
  };
  sandbox.window = sandbox;
  sandbox.parent = sandbox;
  const context = createContext(sandbox);
  if (withTools) new Script(toolsScript(), { filename: "tools-script.js" }).runInContext(context);
  new Script(actionsScript(), { filename: "actions-script.js" }).runInContext(context);
  const $ = (id) => document.getElementById(id);
  const ops = (op) => calls.filter((call) => call.op === op);
  const post = (data, origin = "http://127.0.0.1:3277") => { for (const listener of messageListeners) listener({ data, origin, source: sandbox }); };
  return { document, $, calls, ops, timers, post };
}

describe("actions page markup and styles", () => {
  test("a toggle and a drawer with Capture and Activity tabs, ids prefixed actions-", () => {
    assert.deepEqual(ACTIONS_TABS.map((tab) => tab.label), ["Capture", "Activity"]);
    assert.match(actionsButton(), /id="actions-toggle"[^>]*aria-controls="actions"/);
    const markup = actionsMarkup({ platform: "android" });
    assert.match(markup, /^<aside class="act" id="actions" hidden aria-label="Device actions"/);
    assert.match(markup, /role="tablist"/);
    for (const id of markup.matchAll(/ id="([^"]+)"/g)) assert.ok(id[1] === "actions" || id[1].startsWith("actions-"), id[1]);
    assert.match(markup, /--install-root/);
    assert.match(markup, /id="actions-downgrade"/);
    assert.doesNotMatch(actionsMarkup({ platform: "ios" }), /actions-downgrade/);
    assert.match(actionsMarkup({ options: { platform: "ios" } }), /data-platform="ios"/);
    assert.doesNotMatch(markup, /<script|\son[a-z]+=/i);
  });

  test("the script parses as inlined, has no backticks and never writes HTML", () => {
    const script = actionsScript();
    assert.doesNotMatch(script, /`/);
    assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    new Script(script);
    // eslint-disable-next-line no-new-func
    new Function(`const inlined = \`${script.replace(/\\/g, "\\\\")}\`; return inlined;`);
  });

  test("styles: page tokens, dark mode, the 760 px sheet and nothing wider than 320 px", () => {
    const styles = actionsStyles();
    assert.match(styles, /var\(--surface\)/);
    assert.match(styles, /prefers-color-scheme:dark/);
    assert.match(styles, /@media \(max-width:760px\)/);
    assert.match(styles, /\.actions \.icon\.actions-toggle\{display:grid/);
    assert.match(styles, /body\.actions-open aside\.side\{display:none\}/);
    assert.match(styles, /overflow-x:hidden/);
    assert.match(styles, /minmax\(min\(132px,100%\),1fr\)/);
    assert.doesNotMatch(styles, /min-width:\s*[3-9]\d\dpx/);
  });
});

describe("actions page script", () => {
  test("opening loads status, builds and the gallery; a screenshot saves and refreshes it", async () => {
    const page = bootPage({
      reply: ({ op }) => op === "capture.screenshot" ? ok({ capture: SHOT, pruned: ["old.png", "older.png"] }) : null,
    });
    const { $, ops } = page;
    assert.ok($("actions").hidden);
    assert.equal(page.calls.length, 0);
    $("actions-toggle").click();
    await flush();
    assert.equal($("actions").hidden, false);
    assert.ok(page.document.body.classList.contains("actions-open"));
    assert.equal($("actions-toggle").getAttribute("aria-pressed"), "true");
    assert.equal(ops("record.status").length, 1);
    assert.equal(ops("apps.candidates").length, 1);
    assert.deepEqual(ops("captures.list")[0].body.payload, { limit: 200 });
    assert.equal(ops("captures.list")[0].headers["X-Autonom-CSRF"], "csrf-123");
    const items = $("actions-gallery").children;
    assert.equal(items.length, 2);
    const image = items[1].querySelector("img");
    assert.equal(image.getAttribute("src"), `/tools/captures/${encodeURIComponent(SHOT.name)}`);
    assert.match(items[0].textContent, /Open video \(0:05\)/);
    assert.match($("actions-gallery-dir").textContent, /\/tmp\/caps\/Pixel 8/);
    assert.match($("actions-candidates").textContent, /app-debug\.apk/);

    $("actions-shot").click();
    await flush();
    assert.equal(ops("capture.screenshot").length, 1);
    assert.match($("actions-msg-capture").textContent, /Saved Pixel 8 2026-10-07 09\.05\.03\.png/);
    assert.equal($("actions-prune").hidden, false);
    assert.equal($("actions-prune").textContent, "Deleted 2 oldest captures to stay under 200 files / 2 GB");
    assert.equal(ops("captures.list").length, 2);

    items[1].querySelector("button").click();
    await flush();
    assert.deepEqual(ops("captures.delete")[0].body.payload, { name: SHOT.name });
  });

  test("a gallery whose last commit pruned shows the notice; no install roots shows the explanation", async () => {
    const page = bootPage({
      reply: ({ op }) => op === "apps.candidates" ? ok({ configured: false, roots: [], candidates: [], truncated: false,
        hint: "Start the Canvas with --install-root <folder>, for example `autonom canvas --install-root ~/src/app/build`" })
        : op === "captures.list" ? ok({ captures: [], count: 0, total_bytes: 0, dir: "/c", last_pruned: { at: "x", count: 1 } })
          : null,
    });
    page.$("actions-toggle").click();
    await flush();
    assert.equal(page.$("actions-install-off").hidden, false);
    assert.equal(page.$("actions-install-on").hidden, true);
    assert.match(page.$("actions-install-hint").textContent, /--install-root <folder>/);
    assert.equal(page.$("actions-prune").textContent, "Deleted 1 oldest capture to stay under 200 files / 2 GB");
    assert.equal(page.$("actions-gallery-empty").hidden, false);
  });

  test("record start and stop, with the elapsed time and the limit ending noticed by the status poll", async () => {
    let statusRecording = false;
    const page = bootPage({
      reply: ({ op }) => {
        if (op === "record.start") { statusRecording = true; return ok({ recording: { started_at: new Date().toISOString(), limit_s: 180 } }); }
        if (op === "record.status") return ok(statusRecording
          ? { recording: true, started_at: new Date().toISOString(), elapsed_ms: 1000, limit_s: 180, last: null }
          : { recording: false, started_at: null, elapsed_ms: null, limit_s: 180, last: { capture: VIDEO, ended_early: true } });
        if (op === "record.stop") return ok({ capture: VIDEO, pruned: [], duration_ms: 5000, ended_early: false });
        return null;
      },
    });
    const { $, ops, timers } = page;
    $("actions-toggle").click();
    await flush();
    $("actions-record").click();
    await flush();
    assert.equal(ops("record.start").length, 1);
    assert.equal($("actions-record").textContent, "Stop recording");
    assert.match($("actions-rec-time").textContent, /^Recording 0:0\d \/ 3:00$/);
    await timers.tick(2000);
    assert.ok(ops("record.status").length >= 2, "the status is re-read while recording");
    $("actions-record").click();
    await flush();
    assert.equal(ops("record.stop").length, 1);
    assert.equal($("actions-record").textContent, "Record");
    assert.match($("actions-msg-capture").textContent, /Saved Pixel 8 2026-10-07 09\.06\.00\.mp4 \(0:05\)/);

    // Started again, then the 3 minute limit ends it: the next status poll says so.
    $("actions-record").click();
    await flush();
    statusRecording = false;
    await timers.tick(2000);
    assert.equal($("actions-record").textContent, "Record");
    assert.match($("actions-msg-capture").textContent, /reached 3:00 and was saved as Pixel 8 2026-10-07 09\.06\.00\.mp4/);
  });

  test("a record.start that timed out but still ran is noticed: the status is re-read until it answers", async () => {
    let statusFails = 0;
    let started = false;
    const page = bootPage({
      reply: ({ op }) => {
        // The call timed out in Node, but the queued tools process still started the recorder.
        if (op === "record.start") { started = true; return { status: 504, body: { ok: false, error: "the tools process did not answer", error_code: "timeout", hint: null } }; }
        if (op === "record.status") {
          if (!started) return ok({ recording: false, started_at: null, elapsed_ms: null, limit_s: 180, last: null });
          if (statusFails > 0) {
            statusFails -= 1;
            return { status: 504, body: { ok: false, error: "the tools process did not answer", error_code: "timeout", hint: null } };
          }
          return ok({ recording: true, started_at: new Date().toISOString(), elapsed_ms: 4000, limit_s: 180, last: null });
        }
        return null;
      },
    });
    const { $, ops, timers } = page;
    $("actions-toggle").click();
    await flush();
    const before = ops("record.status").length;
    statusFails = 1;
    $("actions-record").click();
    await flush();
    assert.equal(ops("record.start").length, 1);
    assert.equal(ops("record.status").length, before + 1, "a failed start re-reads the status at once");
    assert.equal($("actions-record").textContent, "Record");
    await timers.tick(2000);
    assert.equal(ops("record.status").length, before + 2, "a failed status read is retried");
    assert.equal($("actions-record").textContent, "Stop recording");
    assert.match($("actions-rec-time").textContent, /^Recording 0:0\d \/ 3:00$/);
  });

  test("install, launch, open link and language send their payloads; device controls follow the control rule", async () => {
    let canSend = true;
    const page = bootPage({
      canSend: () => canSend,
      reply: ({ op }) => op === "app.install" ? ok({ installed: "/src/app/build/app-debug.apk", app_id: null, size: 1, duration_ms: 1200 })
        : op === "app.install" ? null : null,
    });
    const { $, ops, timers } = page;
    $("actions-toggle").click();
    await flush();
    $("actions-candidates").querySelector("button").click();
    assert.equal($("actions-install-path").value, "/src/app/build/app-debug.apk");
    $("actions-downgrade").checked = true;
    $("actions-install-go").click();
    await flush();
    assert.deepEqual(ops("app.install")[0].body.payload, { path: "/src/app/build/app-debug.apk", allow_downgrade: true });
    assert.match($("actions-msg-capture").textContent, /Installed app-debug\.apk/);

    $("actions-launch").click();
    await flush();
    assert.equal(ops("app.launch").length, 0, "no app id, no call");
    assert.match($("actions-msg-capture").textContent, /package or bundle id/);
    $("actions-app-id").value = "com.example.app";
    $("actions-fresh").checked = true;
    $("actions-launch").click();
    await flush();
    assert.deepEqual(ops("app.launch")[0].body.payload, { app_id: "com.example.app", fresh: true });
    $("actions-url").value = "myapp://profile/42";
    $("actions-url-go").click();
    await flush();
    assert.deepEqual(ops("app.open_url")[0].body.payload, { url: "myapp://profile/42" });
    $("actions-locale").value = "de-DE";
    $("actions-locale-go").click();
    await flush();
    assert.deepEqual(ops("app.locale")[0].body.payload, { app_id: "com.example.app", locale: "de-DE" });

    canSend = false;
    await timers.tick(600);
    for (const id of ["actions-shot", "actions-record", "actions-install-go", "actions-launch", "actions-url-go", "actions-locale-go"]) {
      assert.equal($(id).disabled, true, id);
    }
    assert.equal($("actions-lock").hidden, false);
    assert.equal($("actions-gallery-refresh").disabled, false, "the gallery is not a device action");
    canSend = true;
    await timers.tick(600);
    assert.equal($("actions-shot").disabled, false);
  });

  test("Activity polls this device's log every 2 s while visible, filters failures, shows health", async () => {
    let served = 0;
    const page = bootPage({
      reply: ({ path }) => {
        if (path.startsWith("/api/activity?")) {
          served += 1;
          return { status: 200, body: { ok: true, entries: served === 1 ? [
            { seq: 1, at: "2026-10-07T07:00:00Z", device: DEVICE, kind: "tools", name: "app.open_url", origin: "human",
              summary: "https://example.com", ok: true, duration_ms: 120, error_code: null, error: null, exit_code: null },
            { seq: 2, at: "2026-10-07T07:00:01Z", device: DEVICE, kind: "tools", name: "app.locale", origin: "human",
              summary: "<b>com.x</b> de-DE", ok: false, duration_ms: 30, error_code: "unsupported_capability", error: "needs API 33", exit_code: null },
          ] : [], next: 2, dropped: 0, total: 2, seen: 2 } };
        }
        if (path.startsWith("/api/health")) {
          return { status: 200, body: { ok: true, checked_at: "2026-10-07T07:00:00Z", canvas: {}, tools: [
            { name: "adb", found: true, path: "/x/adb", version: "1.0.41", ok: true, needed_for: "Android", error: null, hint: null },
            { name: "mitmdump", found: false, path: null, version: null, ok: false, needed_for: "network capture", error: "not found", hint: "brew install mitmproxy" },
          ] } };
        }
        return null;
      },
    });
    const { $, calls, timers } = page;
    $("actions-toggle").click();
    await flush();
    $("actions-tab-activity").click();
    await flush();
    const reads = () => calls.filter((call) => call.path.startsWith("/api/activity?"));
    assert.equal(reads().length, 1);
    assert.equal(reads()[0].path, `/api/activity?limit=200&after=0&seen=0&device=${encodeURIComponent(DEVICE)}`);
    const rows = $("actions-activity").children;
    assert.equal(rows.length, 2);
    assert.match(rows[0].textContent, /app\.locale/);
    assert.match(rows[0].textContent, /unsupported_capability/);
    assert.match(rows[0].textContent, /<b>com\.x<\/b>/, "summaries render as text");
    assert.equal(rows[0].getAttribute("data-ok"), "false");
    await timers.tick(2000);
    assert.equal(reads().length, 2);
    assert.match(reads()[1].path, /after=2&seen=2/, "the page sends back how far it has read");
    $("actions-failures").checked = true;
    fire($("actions-failures"), "change");
    await flush();
    assert.match(reads()[2].path, /after=0.*&failures=1$/);
    const health = $("actions-health").children;
    assert.equal(health.length, 2);
    assert.match(health[0].textContent, /adb.*OK/);
    assert.match(health[1].textContent, /Missing.*brew install mitmproxy/);
    $("actions-health-refresh").click();
    await flush();
    assert.ok(calls.some((call) => call.path === "/api/health?refresh=1"));
    $("actions-activity-clear").click();
    await flush();
    const clear = calls.find((call) => call.path === "/api/activity/clear");
    assert.equal(clear.method, "POST");
    assert.equal(clear.headers["X-Autonom-CSRF"], "csrf-123");

    $("actions-tab-capture").click();
    await flush();
    const before = reads().length;
    await timers.tick(6000);
    assert.equal(reads().length, before, "no polling while the tab is hidden");
  });

  test("Actions, Tools and the inspector are mutually exclusive; Escape and autonom:panel close and open", async () => {
    const page = bootPage();
    const { $, document } = page;
    $("tools-toggle").click();
    await flush();
    assert.ok(document.body.classList.contains("tools-open"));
    $("actions-toggle").click();
    await flush();
    assert.ok(document.body.classList.contains("actions-open"));
    assert.ok(!document.body.classList.contains("tools-open"), "opening Actions closes Tools");
    assert.equal($("tools").hidden, true);
    $("tools-toggle").click();
    await flush();
    assert.ok(document.body.classList.contains("tools-open"));
    assert.ok(!document.body.classList.contains("actions-open"), "opening Tools closes Actions");
    $("tools-close").click();
    $("actions-toggle").click();
    await flush();
    $("inspector-toggle").click();
    assert.ok(!document.body.classList.contains("actions-open"), "the inspector closes Actions");
    assert.equal($("inspector-toggle").getAttribute("aria-pressed"), "true");

    page.post({ type: "autonom:panel", name: "actions", open: true });
    await flush();
    assert.equal($("actions").hidden, false);
    page.post({ type: "autonom:panel", name: "actions", open: false }, "https://evil.example");
    assert.equal($("actions").hidden, false, "another origin is ignored");
    page.post({ type: "autonom:panel", name: "tools", open: false });
    assert.equal($("actions").hidden, false, "another panel is ignored");
    fire($("actions"), "keydown", { key: "Escape" });
    assert.equal($("actions").hidden, true);
    assert.equal(document.activeElement, $("actions-toggle"));

    const lone = bootPage({ withTools: false, deviceId: null });
    lone.$("actions-toggle").click();
    await flush();
    lone.$("actions-tab-activity").click();
    await flush();
    assert.equal(lone.calls.find((call) => call.path.startsWith("/api/activity?")).path, "/api/activity?limit=200&after=0&seen=0");
  });

  test("errors render their message, code and hint as text", async () => {
    const page = bootPage({
      reply: ({ op }) => op === "capture.screenshot"
        ? { status: 409, body: { ok: false, error: "<img src=x>", error_code: "recording_already_active", hint: "Stop <it>" } } : null,
    });
    page.$("actions-toggle").click();
    await flush();
    page.$("actions-shot").click();
    await flush();
    const box = page.$("actions-msg-capture");
    assert.equal(box.getAttribute("data-kind"), "error");
    assert.match(box.textContent, /<img src=x> \(recording_already_active\)Stop <it>/);
    assert.equal(box.querySelectorAll("img").length, 0);
  });
});
