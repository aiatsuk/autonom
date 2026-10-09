// Unit tests of canvas-sessions.mjs against a fake CLI: session start, reuse of a live
// session, a session whose Canvas died stopped and started afresh, failures that keep the
// device, release of only the Canvas's own sessions, and the detached release at stop.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  createSessionManager,
  parseCliJson,
  runCliJson,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-sessions.mjs";

/** A fake `run`: answers by the subcommand, records every argv. */
function fakeRun(answers) {
  const calls = [];
  const run = async (argv, options) => {
    calls.push({ argv, options });
    const verb = argv.slice(argv.indexOf("session"), argv.indexOf("session") + 2).join(" ");
    const answer = typeof answers[verb] === "function" ? answers[verb](argv) : answers[verb];
    if (answer instanceof Error) throw answer;
    return answer ?? { code: 2, json: { ok: false, error_code: "usage_error", error: "unexpected" } };
  };
  return { run, calls };
}

const manager = (run, extra = {}) => createSessionManager({
  python: "python3", autonomPath: "/x/autonom.py", port: () => 4321, pid: 999, run, ...extra,
});

test("ensure starts a session alongside the primary, with the Canvas as its starter", async () => {
  const { run, calls } = fakeRun({ "session start": { code: 0, json: { ok: true, session: { session_id: "s_new" } } } });
  const link = await manager(run).ensure({ platform: "android", target: "emulator-5580", tool: "/sdk/adb" });
  assert.deepEqual(link, { session_id: "s_new", started_by_canvas: true, reused: false, error: null });
  assert.deepEqual(calls[0].argv, ["python3", "/x/autonom.py", "--platform", "android", "--serial", "emulator-5580",
    "--adb", "/sdk/adb", "session", "start", "--alongside", "--started-by", "canvas:4321:999"]);
  assert.equal(calls[0].options.timeoutMs, 60_000);
});

test("ensure on iOS names the Simulator, simctl and idb and starts the log stream", async () => {
  const { run, calls } = fakeRun({ "session start": { code: 0, json: { ok: true, session: { session_id: "s_ios" } } } });
  await manager(run).ensure({ platform: "ios", target: "UDID-1", tool: "/usr/bin/xcrun", idb: "/opt/idb" });
  assert.deepEqual(calls[0].argv.slice(2), ["--platform", "ios", "--udid", "UDID-1", "--simctl", "/usr/bin/xcrun",
    "--idb", "/opt/idb", "session", "start", "--alongside", "--started-by", "canvas:4321:999", "--log-stream"]);
});

test("ensure reuses a live session, and adopts one this Canvas started", async () => {
  const refusal = (startedBy) => ({ code: 2, json: {
    ok: false, error_code: "session_already_active", error: "busy", session_id: "s_live",
    target_id: "emulator-5580", started_by: startedBy,
  } });
  const alive = new Set([1234]);
  const isAlive = (pid) => alive.has(pid);
  const cases = [
    [null, { started_by_canvas: false, reused: true }],
    [{ kind: "canvas", port: 1, pid: 1234 }, { started_by_canvas: false, reused: true }],
    [{ kind: "canvas", port: 4321, pid: 999 }, { started_by_canvas: true, reused: true }],
  ];
  for (const [startedBy, expected] of cases) {
    const { run } = fakeRun({ "session start": refusal(startedBy) });
    const link = await manager(run, { isAlive }).ensure({ platform: "android", target: "emulator-5580", tool: "adb" });
    assert.deepEqual(link, { session_id: "s_live", error: null, ...expected }, JSON.stringify(startedBy));
  }
});

test("a session left by a Canvas that is gone is stopped first, then a fresh one starts", async () => {
  // The old Canvas started a detached stop for it at its own stop; adopting it as is would
  // let that stop end the session after this Canvas took it over.
  const order = [];
  let starts = 0;
  const { run, calls } = fakeRun({
    "session start": () => {
      starts += 1;
      order.push(`start ${starts}`);
      return starts === 1
        ? { code: 2, json: { ok: false, error_code: "session_already_active", error: "busy", session_id: "s_old",
          target_id: "emulator-5580", started_by: { kind: "canvas", port: 4321, pid: 5678 } } }
        : { code: 0, json: { ok: true, session: { session_id: "s_fresh" } } };
    },
    "session stop": (argv) => { order.push(`stop ${argv.at(-1)}`); return { code: 0, json: { ok: true } }; },
  });
  const link = await manager(run, { isAlive: () => false })
    .ensure({ platform: "android", target: "emulator-5580", tool: "adb" });
  assert.deepEqual(link, { session_id: "s_fresh", started_by_canvas: true, reused: false, error: null });
  assert.deepEqual(order, ["start 1", "stop s_old", "start 2"]);
  assert.deepEqual(calls[1].argv, ["python3", "/x/autonom.py", "session", "stop", "--session-id", "s_old"]);
  // The old Canvas's stop finished first: this stop is refused, the fresh start still runs.
  starts = 0;
  const late = fakeRun({
    "session start": () => (++starts === 1
      ? { code: 2, json: { ok: false, error_code: "session_already_active", session_id: "s_old",
        started_by: { kind: "canvas", port: 1, pid: 5678 } } }
      : { code: 0, json: { ok: true, session: { session_id: "s_fresh" } } }),
    "session stop": { code: 2, json: { ok: false, error_code: "session_stopped", error: "stopped" } },
  });
  assert.equal((await manager(late.run, { isAlive: () => false })
    .ensure({ platform: "android", target: "emulator-5580", tool: "adb" })).session_id, "s_fresh");
  // Still live after the stop: no session is handed out, the device keeps the reason.
  const stuck = fakeRun({
    "session start": { code: 2, json: { ok: false, error_code: "session_already_active", session_id: "s_old",
      started_by: { kind: "canvas", port: 1, pid: 5678 } } },
    "session stop": { code: null, json: null, error: "timed out after 60 s" },
  });
  const refused = await manager(stuck.run, { isAlive: () => false })
    .ensure({ platform: "android", target: "emulator-5580", tool: "adb" });
  assert.equal(refused.session_id, null);
  assert.equal(refused.started_by_canvas, false);
  assert.deepEqual(refused.error, { error_code: "backend_failed", error: "timed out after 60 s" });
  assert.equal(stuck.calls.filter((call) => call.argv.includes("start")).length, 2);
});

test("an ensure meeting this Canvas's own release still running waits for it", async () => {
  let finishStop;
  const stopDone = new Promise((resolve) => { finishStop = resolve; });
  let stopped = false;
  const { run, calls } = fakeRun({
    "session start": () => (stopped
      ? { code: 0, json: { ok: true, session: { session_id: "s_next" } } }
      : { code: 2, json: { ok: false, error_code: "session_already_active", session_id: "s_mine",
        started_by: { kind: "canvas", port: 4321, pid: 999 } } }),
    "session stop": async () => { await stopDone; stopped = true; return { code: 0, json: { ok: true } }; },
  });
  const sessions = manager(run);
  const releasing = sessions.release({ session_id: "s_mine", started_by_canvas: true, reused: false });
  const ensuring = sessions.ensure({ platform: "android", target: "emulator-5580", tool: "adb" });
  await new Promise((r) => setTimeout(r, 20));
  finishStop();
  assert.deepEqual(await releasing, { stopped: true, error: null });
  assert.deepEqual(await ensuring, { session_id: "s_next", started_by_canvas: true, reused: false, error: null });
  assert.equal(calls.filter((call) => call.argv.includes("stop")).length, 1);
});

test("ensure never rejects: a failure keeps the device and says why", async () => {
  for (const answer of [
    { code: 2, json: { ok: false, error_code: "tool_missing", error: "adb was not found" } },
    { code: null, json: null, error: "timed out after 60 s" },
    new Error("spawn python3 ENOENT"),
    { code: 0, json: { ok: true } },
  ]) {
    const { run } = fakeRun({ "session start": answer });
    const link = await manager(run).ensure({ platform: "android", target: "x", tool: "adb" });
    assert.equal(link.session_id, null);
    assert.equal(link.started_by_canvas, false);
    assert.equal(typeof link.error.error_code, "string");
    assert.equal(typeof link.error.error, "string");
  }
});

test("release stops only sessions the Canvas started", async () => {
  const { run, calls } = fakeRun({ "session stop": { code: 0, json: { ok: true } } });
  const sessions = manager(run);
  assert.deepEqual(await sessions.release({ session_id: "s_other", started_by_canvas: false, reused: true }),
    { stopped: false, error: null });
  assert.deepEqual(await sessions.release(null), { stopped: false, error: null });
  assert.deepEqual(calls, []);
  assert.deepEqual(await sessions.release({ session_id: "s_mine", started_by_canvas: true, reused: false }),
    { stopped: true, error: null });
  assert.deepEqual(calls[0].argv, ["python3", "/x/autonom.py", "session", "stop", "--session-id", "s_mine"]);
  const failing = manager(fakeRun({ "session stop": { code: 2, json: { ok: false, error_code: "x", error: "no" } } }).run);
  assert.deepEqual(await failing.release({ session_id: "s", started_by_canvas: true }),
    { stopped: false, error: { error_code: "x", error: "no" } });
});

test("releaseDetached starts the stops of own sessions without waiting", () => {
  const spawned = [];
  const sessions = createSessionManager({
    python: "py", autonomPath: "a.py", port: () => 1, run: () => { throw new Error("not used"); },
    spawnDetached: (argv) => { spawned.push(argv); return true; },
  });
  const started = sessions.releaseDetached([
    { session_id: "s_1", started_by_canvas: true }, { session_id: "s_2", started_by_canvas: false },
    null, { session_id: null, started_by_canvas: true, error: {} }, { session_id: "s_3", started_by_canvas: true },
  ]);
  assert.deepEqual(started, ["s_1", "s_3"]);
  assert.deepEqual(spawned, [["py", "a.py", "session", "stop", "--session-id", "s_1"],
    ["py", "a.py", "session", "stop", "--session-id", "s_3"]]);
});

test("runCliJson reads stdout on success and the stderr error object on failure", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "autonom-sessions-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const script = join(directory, "fake-cli.mjs");
  await writeFile(script, String.raw`
const args = process.argv.slice(2);
if (args[0] === "ok") console.log(JSON.stringify({ ok: true, session: { session_id: "s_1" } }, null, 2));
else if (args[0] === "fail") {
  console.error("a warning line");
  console.error(JSON.stringify({ ok: false, error_code: "session_already_active", error: "busy", session_id: "s_9" }));
  process.exit(2);
} else setInterval(() => {}, 1000);
`);
  const ok = await runCliJson([process.execPath, script, "ok"]);
  assert.equal(ok.code, 0);
  assert.equal(ok.json.session.session_id, "s_1");
  const fail = await runCliJson([process.execPath, script, "fail"]);
  assert.equal(fail.code, 2);
  assert.equal(fail.json.error_code, "session_already_active");
  const slow = await runCliJson([process.execPath, script, "hang"], { timeoutMs: 200 });
  assert.equal(slow.code, null);
  assert.match(slow.error, /timed out/);
  const missing = await runCliJson([join(directory, "nope")]);
  assert.equal(missing.code, null);
  assert.deepEqual(parseCliJson("noise\n{\"a\":1}"), { a: 1 });
  assert.equal(parseCliJson(""), null);
});

test("the default detached release outlives its caller and runs the stop", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "autonom-sessions-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const script = join(directory, "fake-cli.mjs");
  const log = join(directory, "stopped.log");
  await writeFile(script, `import { writeFileSync } from "node:fs";
writeFileSync(${JSON.stringify(log)}, process.argv.slice(2).join(" "));`);
  const sessions = createSessionManager({ python: process.execPath, autonomPath: script, port: () => 1 });
  assert.deepEqual(sessions.releaseDetached([{ session_id: "s_77", started_by_canvas: true }]), ["s_77"]);
  const deadline = Date.now() + 5000;
  while (!existsSync(log) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 20));
  assert.equal(await readFile(log, "utf8"), "session stop --session-id s_77");
});
