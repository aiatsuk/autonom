// Unit tests of the Canvas side of the host control lease (contract 6.1): the admission
// table and the lease file names against the fixture the Python suite runs too, the file
// cache, the session status, the --control-idle range, and the lease hooks of the tools
// server (LEASED_TOOLS_OPS, the 409 mapping, the refusal and acquire hooks, the request
// binding and the log feed's --session-id). No device, Canvas or bridge process is involved.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  AUTO_ACQUIRE_WAIT_MS,
  DEFAULT_CONTROL_IDLE_S,
  HUMAN_LEASE_TTL_S,
  LEASE_ERROR_CODES,
  LEASE_POLL_MS,
  LEASE_RENEW_MS,
  MAX_CONTROL_IDLE_S,
  MIN_CONTROL_IDLE_S,
  admission,
  createFileCache,
  leaseFor,
  leasePath,
  leaseState,
  parseControlIdle,
  safeTarget,
  sessionRecordPath,
  sessionStatus,
  sessionsHome,
  stateBase,
  validLease,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-control.mjs";
import { parseArgs } from "../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs";
import {
  ACTION_DEVICE_OPS,
  LEASED_TOOLS_OPS,
  TOOLS_MUTATING_OPS,
  ToolsClient,
  createCanvasTools,
  handleToolsRoute,
  isLeasedToolsOp,
  toolsErrorStatus,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-server.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const FIXTURE = JSON.parse(readFileSync(join(ROOT, "tests/fixtures/control_cases.json"), "utf8"));

function tempDir(t) {
  const directory = mkdtempSync(join(tmpdir(), "autonom-canvas-control-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function lease(fields = {}) {
  return {
    schema: 1, platform: "android", target_id: "emulator-5584", generation: 3,
    holder: { kind: "agent", id: "agent:worker-1", label: "worker" }, session_id: null,
    acquired_at: "2026-10-10T14:00:00.000Z", renewed_at: "2026-10-10T14:00:00.000Z",
    expires_at: "2026-10-10T14:00:30.000Z", expires_at_ms: 2000, ttl_ms: 30000, previous_holder: null,
    reason: "acquired", updated_at: "2026-10-10T14:00:00.000Z", updated_at_ms: 1000, ...fields,
  };
}

test("fixture parity: every admission case answers what the Python rule answers", () => {
  assert.ok(FIXTURE.admission.length >= 10);
  const rows = new Set();
  for (const item of FIXTURE.admission) {
    const refused = admission(item.lease, item.claim, item.now_ms);
    assert.equal(refused ? refused.code : "admitted", item.expect, item.name);
    rows.add(item.row);
    if (refused) {
      // The refusal carries the extras the bridge's answers carry.
      assert.equal(refused.capability, "control.lease", item.name);
      assert.ok(typeof refused.message === "string" && refused.message.length > 0, item.name);
      assert.ok(typeof refused.hint === "string" && refused.hint.length > 0, item.name);
      assert.equal(refused.generation, item.lease && validLease(item.lease) ? item.lease.generation : null, item.name);
      assert.ok("holder" in refused, item.name);
      if (item.claim) assert.equal(refused.claimed_holder, item.claim.holder_id, item.name);
    }
  }
  assert.deepEqual([...rows].sort((a, b) => a - b), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
});

test("fixture parity: lease file names match session._safe_target byte for byte", () => {
  assert.ok(FIXTURE.paths.length >= 4);
  for (const item of FIXTURE.paths) {
    const path = leasePath({ AUTONOM_HOME: "/state" }, item.platform, item.target_id);
    assert.equal(path, join("/state", "control", item.file), item.target_id);
  }
  // A code point outside the BMP is one character, as Python counts it.
  assert.match(safeTarget("a\u{1F600}b"), /^a_b-[0-9a-f]{12}$/);
  assert.equal(safeTarget("emulator-5584"), "emulator-5584");
  assert.throws(() => leasePath({}, "windows", "x"), /android' or 'ios/);
  assert.throws(() => leasePath({}, "android", ""), /target id/);
});

test("state base, session store and session record paths follow the Python rules", () => {
  assert.equal(stateBase({ AUTONOM_HOME: "/h" }), "/h");
  assert.equal(stateBase({ XDG_STATE_HOME: "/x", HOME: "/home/u" }), join("/x", "autonom"));
  assert.equal(stateBase({ HOME: "/home/u" }), join("/home/u", ".local", "state", "autonom"));
  assert.equal(sessionsHome({ AUTONOM_HOME: "/h" }), join("/h", "sessions"));
  assert.equal(sessionsHome({ HOME: "/home/u" }), join("/home/u", ".autonom", "sessions"));
  assert.equal(sessionRecordPath({ AUTONOM_HOME: "/h" }, "s_0123456789"), join("/h", "sessions", "s_0123456789", "session.json"));
  for (const bad of [null, "", "../s_1", "s_1/..", "x_1", "s_", "s_a b"]) {
    assert.equal(sessionRecordPath({ AUTONOM_HOME: "/h" }, bad), null, String(bad));
  }
});

test("leaseState: none, free, held and expired at the boundaries", () => {
  assert.equal(leaseState(null, 0), "none");
  assert.equal(leaseState(lease({ schema: 2 }), 0), "none");
  assert.equal(leaseState(lease({ generation: -1 }), 0), "none");
  assert.equal(leaseState(lease({ generation: true }), 0), "none");
  assert.equal(leaseState(lease({ holder: { kind: "robot", id: "x", label: "x" } }), 0), "none");
  assert.equal(leaseState(lease({ holder: null, generation: 0 }), 0), "none");
  assert.equal(leaseState(lease({ holder: null, generation: 4 }), 0), "free");
  const { holder: _holder, ...withoutHolder } = lease({ generation: 2 });
  assert.equal(leaseState(withoutHolder, 0), "free");
  assert.equal(leaseState(lease({ expires_at_ms: 2000 }), 1999), "held");
  assert.equal(leaseState(lease({ expires_at_ms: 2000 }), 2000), "expired");
  assert.equal(leaseState(lease({ expires_at_ms: 2000 }), 2001), "expired");
  // A lease of another target reads as none for this one.
  assert.equal(leaseFor(lease(), { platform: "android", target: "emulator-5586" }), null);
  assert.equal(leaseFor(lease(), { platform: "ios", target: "emulator-5584" }), null);
  assert.ok(leaseFor(lease(), { platform: "android", target: "emulator-5584" }));
});

test("createFileCache re-reads only when mtime, size or inode changed; missing and invalid JSON are null", () => {
  let stat = null;
  let content = "";
  let reads = 0;
  const cache = createFileCache("/lease.json", {
    statSync: () => {
      if (!stat) throw Object.assign(new Error("missing"), { code: "ENOENT" });
      return stat;
    },
    readFileSync: () => {
      reads += 1;
      return content;
    },
  });
  assert.deepEqual(cache.get(), { value: null, changed: true });
  assert.deepEqual(cache.get(), { value: null, changed: false });
  assert.equal(reads, 0);
  stat = { mtimeMs: 1, size: 7, ino: 10 };
  content = '{"a":1}';
  assert.deepEqual(cache.get(), { value: { a: 1 }, changed: true });
  assert.deepEqual(cache.get(), { value: { a: 1 }, changed: false });
  assert.equal(reads, 1);
  // The same size and inode with a new mtime is read again.
  stat = { mtimeMs: 2, size: 7, ino: 10 };
  content = '{"a":2}';
  assert.deepEqual(cache.get(), { value: { a: 2 }, changed: true });
  // A new size alone too, and a new inode alone (an atomic replace).
  stat = { mtimeMs: 2, size: 8, ino: 10 };
  content = '{"a":33}';
  assert.deepEqual(cache.get().value, { a: 33 });
  stat = { mtimeMs: 2, size: 8, ino: 11 };
  content = '{"a":44}';
  assert.deepEqual(cache.get().value, { a: 44 });
  assert.equal(reads, 4);
  // Invalid JSON is no value.
  stat = { mtimeMs: 3, size: 3, ino: 11 };
  content = "{no";
  assert.deepEqual(cache.get(), { value: null, changed: true });
  // Gone again.
  stat = null;
  assert.deepEqual(cache.get(), { value: null, changed: true });
  // reset() forgets: the next get reads again.
  stat = { mtimeMs: 4, size: 7, ino: 12 };
  content = '{"b":1}';
  cache.get();
  cache.reset();
  const before = reads;
  assert.deepEqual(cache.get().value, { b: 1 });
  assert.equal(reads, before + 1);
});

test("createFileCache on a real file sees a rewrite and a removal", (t) => {
  const directory = tempDir(t);
  const path = join(directory, "x.json");
  const cache = createFileCache(path);
  assert.equal(cache.get().value, null);
  writeFileSync(path, JSON.stringify({ generation: 1 }));
  assert.deepEqual(cache.get().value, { generation: 1 });
  writeFileSync(path, JSON.stringify({ generation: 22 }));
  utimesSync(path, new Date(), new Date(Date.now() + 5000));
  assert.deepEqual(cache.get().value, { generation: 22 });
  rmSync(path);
  assert.equal(cache.get().value, null);
});

test("sessionStatus: live, stopped, mismatch and missing", () => {
  const where = { platform: "android", target: "emulator-5584" };
  assert.equal(sessionStatus(null, where), "missing");
  assert.equal(sessionStatus([], where), "missing");
  assert.equal(sessionStatus({ session_id: "s_1", platform: "android", target_id: "emulator-5584" }, where), "live");
  // v1 records: no platform (android) and the serial only.
  assert.equal(sessionStatus({ session_id: "s_1", serial: "emulator-5584" }, where), "live");
  assert.equal(sessionStatus({ session_id: "s_1", target_id: "emulator-5584", stopped_at: "2026-10-10T00:00:00Z" }, where),
    "stopped");
  assert.equal(sessionStatus({ session_id: "s_1", platform: "android", target_id: "emulator-5586" }, where), "mismatch");
  assert.equal(sessionStatus({ session_id: "s_1", platform: "ios", target_id: "emulator-5584" }, where), "mismatch");
});

test("--control-idle: 15 to 3600 seconds, default 120; anything else is a usage error", () => {
  assert.equal(DEFAULT_CONTROL_IDLE_S, 120);
  assert.deepEqual([MIN_CONTROL_IDLE_S, MAX_CONTROL_IDLE_S], [15, 3600]);
  assert.equal(parseArgs([]).controlIdle, 120);
  assert.equal(parseArgs(["--control-idle", "15"]).controlIdle, 15);
  assert.equal(parseArgs(["--control-idle", "3600"]).controlIdle, 3600);
  for (const bad of ["14", "3601", "0", "-1", "1.5", "abc", "", "1e3", " 20"]) {
    assert.throws(() => parseControlIdle(bad), /--control-idle must be an integer from 15 to 3600/, bad);
  }
  assert.throws(() => parseArgs(["--control-idle", "14"]), /--control-idle must be an integer from 15 to 3600/);
  assert.throws(() => parseArgs(["--control-idle"]), /Pass a value after --control-idle/);
  // The timing constants of the contract.
  assert.deepEqual([LEASE_POLL_MS, HUMAN_LEASE_TTL_S, LEASE_RENEW_MS, AUTO_ACQUIRE_WAIT_MS], [250, 30, 10_000, 2000]);
});

test("LEASED_TOOLS_OPS is the list of contract 4.3 and the Python tools process's list", () => {
  const expected = ["permissions.set", "location.set", "location.clear", "simulate", "network.capture", "network.attach",
    "network.detach", "network.stop", "app.install", "app.launch", "app.open_url", "app.locale"];
  assert.deepEqual([...LEASED_TOOLS_OPS], expected);
  const python = readFileSync(join(ROOT, "scripts/autonom_canvas_tools.py"), "utf8");
  const block = /^LEASED_TOOLS_OPS = \(([^)]*)\)/m.exec(python);
  assert.ok(block, "LEASED_TOOLS_OPS in autonom_canvas_tools.py");
  assert.deepEqual([...block[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]), expected);
  // Observation and host-side ops never need a lease.
  for (const op of ["capture.screenshot", "record.start", "record.stop", "network.start", "mocks.add", "mocks.clear",
    "captures.delete", "captures.open_directory", "context"]) {
    assert.equal(isLeasedToolsOp(op), false, op);
  }
  // Every leased op is one the Canvas control rule covers.
  for (const op of LEASED_TOOLS_OPS) assert.ok([...TOOLS_MUTATING_OPS, ...ACTION_DEVICE_OPS].includes(op), op);
});

test("toolsErrorStatus: the lease and session codes answer 409, the others as before", () => {
  assert.deepEqual([...LEASE_ERROR_CODES], ["control_held", "control_generation_stale", "control_lease_expired",
    "control_not_held", "session_stopped", "session_target_mismatch", "session_not_found"]);
  for (const code of LEASE_ERROR_CODES) assert.equal(toolsErrorStatus(code), 409, code);
  assert.equal(toolsErrorStatus("invalid_value"), 400);
  assert.equal(toolsErrorStatus("backend_failed"), 502);
});

function route(op, { origin = "human", refusal = () => null, acquire = null } = {}) {
  const calls = [];
  const tools = { call: async (...args) => { calls.push(args); return { status: 200, body: { ok: true, result: {} } }; } };
  return handleToolsRoute(tools, {
    method: "POST", url: new URL("http://127.0.0.1/tools/call"), origin,
    readBody: async () => ({ op, payload: {} }), refusal, acquire,
  }).then((answer) => ({ answer, calls }));
}

test("handleToolsRoute: lease refusals answer 409 with their code, paused and owner refusals stay 403", async () => {
  const seen = [];
  const held = { message: "Device control is held by agent 'worker' (generation 3)", code: "control_held" };
  let { answer, calls } = await route("app.launch", { refusal: (origin, rule) => { seen.push([origin, rule]); return held; } });
  assert.equal(answer.status, 409);
  assert.deepEqual(answer.body, { ok: false, error: held.message, error_code: "control_held", hint: null, capability: null });
  assert.equal(calls.length, 0);
  assert.deepEqual(seen, [["human", { leased: true }]]);
  ({ answer } = await route("app.launch", { refusal: () => ({ message: "Canvas input is paused", code: null }) }));
  assert.deepEqual([answer.status, answer.body.error_code, answer.body.error], [403, "control_refused", "Canvas input is paused"]);
  // A plain message (the rule of before) is still understood.
  ({ answer } = await route("mocks.add", { refusal: () => "Canvas control is owned by agent" }));
  assert.deepEqual([answer.status, answer.body.error_code], [403, "control_refused"]);
  // Observation and host-side ops ask the rule without the lease.
  seen.length = 0;
  ({ answer, calls } = await route("capture.screenshot", { refusal: (origin, rule) => { seen.push(rule); return null; } }));
  assert.equal(answer.status, 200);
  assert.deepEqual(seen, [{ leased: false }]);
  assert.equal(calls.length, 1);
});

test("handleToolsRoute: a person's leased op takes the lease first; agents and other ops never do", async () => {
  const acquired = [];
  const acquire = async (origin) => { acquired.push(origin); return null; };
  let { answer, calls } = await route("app.launch", { acquire });
  assert.equal(answer.status, 200);
  assert.deepEqual(acquired, ["human"]);
  assert.equal(calls.length, 1);
  ({ answer } = await route("app.launch", { origin: "agent", acquire }));
  ({ answer } = await route("mocks.add", { acquire }));
  ({ answer } = await route("capture.screenshot", { acquire }));
  ({ answer } = await route("context", { acquire }));
  assert.deepEqual(acquired, ["human"]);
  // A failed acquire answers its failure and runs nothing.
  const failing = async () => ({ status: 409, code: "control_not_held", message: "Device control could not be taken" });
  ({ answer, calls } = await route("location.set", { acquire: failing }));
  assert.deepEqual([answer.status, answer.body.error_code, answer.body.error], [409, "control_not_held",
    "Device control could not be taken"]);
  assert.equal(calls.length, 0);
  const down = async () => ({ status: 502, code: null, message: "Canvas action bridge exited 1" });
  ({ answer, calls } = await route("location.set", { acquire: down }));
  assert.deepEqual([answer.status, answer.body.error_code], [502, "backend_failed"]);
  assert.equal(calls.length, 0);
  // Refused again after the acquire (the lease moved meanwhile): nothing runs.
  let turn = 0;
  ({ answer, calls } = await route("app.launch", {
    acquire, refusal: () => (turn++ === 0 ? null : { message: "held", code: "control_held" }),
  }));
  assert.deepEqual([answer.status, answer.body.error_code], [409, "control_held"]);
  assert.equal(calls.length, 0);
});

/** A tools process stand-in that answers every request with the request itself. */
const ECHO_TOOLS = `
const { createInterface } = require("node:readline");
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  process.stdout.write(JSON.stringify({ id: message.id, ok: true, result: { request: message } }) + "\\n");
}).on("close", () => process.exit(0));
`;

test("ToolsClient: requests carry session_id and control only when the binding knows them", async (t) => {
  let binding = {};
  const client = new ToolsClient({
    spawnTools: () => spawn(process.execPath, ["-e", ECHO_TOOLS], { stdio: ["pipe", "pipe", "pipe"] }),
    binding: () => binding,
  });
  client.start();
  t.after(() => client.close());
  let answer = await client.call("context", {}, "system");
  assert.deepEqual(answer.body.result.request, { id: 1, op: "context", payload: {}, origin: "system" });
  binding = { session_id: "s_abc", control: { holder_id: "canvas:1:2", generation: 4 } };
  answer = await client.call("app.launch", { app_id: "x" }, "human");
  assert.deepEqual(answer.body.result.request, { id: 2, op: "app.launch", payload: { app_id: "x" }, origin: "human",
    session_id: "s_abc", control: { holder_id: "canvas:1:2", generation: 4 } });
});

test("the log feed of a bound device follows its session: --session-id before --platform", async (t) => {
  const directory = tempDir(t);
  const argsFile = join(directory, "args.json");
  const script = join(directory, "fake-autonom.js");
  writeFileSync(script, `require("node:fs").writeFileSync(${JSON.stringify(argsFile)}, JSON.stringify(process.argv.slice(2)));`);
  const idle = join(directory, "idle-tools.js");
  writeFileSync(idle, "process.stdin.resume(); process.stdin.on('end', () => process.exit(0));");
  const make = (binding) => createCanvasTools({
    python: process.execPath, toolsPath: idle, autonomPath: script, platform: "android", target: "emulator-5584",
    tool: "/bin/adb", binding,
  });
  const waitArgs = async () => {
    for (let i = 0; i < 200; i += 1) {
      try { return JSON.parse(readFileSync(argsFile, "utf8")); } catch { await new Promise((r) => setTimeout(r, 20)); }
    }
    throw new Error("the feed did not start");
  };
  const bound = make(() => ({ session_id: "s_bound1" }));
  t.after(() => bound.close());
  rmSync(argsFile, { force: true });
  const child = bound.feed.spawnFollow("com.example", null);
  child.on("error", () => {});
  assert.deepEqual(await waitArgs(), ["--session-id", "s_bound1", "--platform", "android", "--serial", "emulator-5584",
    "--adb", "/bin/adb", "logs", "follow", "--source", "device", "--package", "com.example"]);
  const unbound = make(() => ({}));
  t.after(() => unbound.close());
  rmSync(argsFile, { force: true });
  unbound.feed.spawnFollow(null, null).on("error", () => {});
  assert.deepEqual(await waitArgs(), ["--platform", "android", "--serial", "emulator-5584", "--adb", "/bin/adb",
    "logs", "follow", "--source", "device"]);
});
