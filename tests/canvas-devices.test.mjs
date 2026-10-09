// Unit tests of canvas-devices.mjs: device ids and paths, the focus profile math, the device
// registry (limits, shared attaches, failures, transitions) and the focus controller's
// timing against a fake clock. No device or process is involved.
import assert from "node:assert/strict";
import test from "node:test";

import {
  ATTACH_CONCURRENCY,
  CanvasError,
  DEVICE_ID_PATTERN,
  DeviceRegistry,
  FocusController,
  MAX_DEVICES,
  PROFILE,
  RESTART_INPUT_WAIT_MS,
  deviceIdFor,
  devicePath,
  parseDeviceId,
  profileVideo,
  splitDevicePath,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-devices.mjs";

test("device ids follow <platform>~<target> and refuse anything else", () => {
  assert.equal(MAX_DEVICES, 8);
  assert.equal(deviceIdFor("android", "emulator-5580"), "android~emulator-5580");
  assert.equal(deviceIdFor("android", "127.0.0.1:5555"), "android~127.0.0.1:5555");
  assert.equal(deviceIdFor("ios", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"), "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5");
  for (const [platform, target] of [["windows", "x"], ["android", ""], ["android", "a b"], ["ios", "x/y"],
    ["android", "x".repeat(129)], ["android", undefined]]) {
    assert.throws(() => deviceIdFor(platform, target), (error) => error instanceof CanvasError &&
      error.status === 400 && error.code === "invalid_value");
  }
  assert.ok(DEVICE_ID_PATTERN.test(`android~${"x".repeat(128)}`));
  assert.deepEqual(parseDeviceId("android~127.0.0.1:5555"), { platform: "android", target: "127.0.0.1:5555" });
  assert.equal(parseDeviceId("android~"), null);
  assert.equal(parseDeviceId("nope"), null);
});

test("device paths encode the id as one segment and split back", () => {
  assert.equal(devicePath("android~127.0.0.1:5555"), "/d/android~127.0.0.1%3A5555");
  assert.deepEqual(splitDevicePath("/d/android~127.0.0.1%3A5555/ws/video"),
    { id: "android~127.0.0.1:5555", rest: "/ws/video" });
  assert.deepEqual(splitDevicePath("/d/android~emulator-5580/"), { id: "android~emulator-5580", rest: "/" });
  assert.deepEqual(splitDevicePath("/d/android~emulator-5580"), { id: "android~emulator-5580", rest: null });
  assert.equal(splitDevicePath("/d/"), null);
  assert.equal(splitDevicePath("/status"), null);
  assert.equal(splitDevicePath("/d/%E0%A4%A/x"), null);
});

test("focus profiles: focused runs full rate, background caps fps, size and scale", () => {
  assert.deepEqual(profileVideo({ platform: "android", focused: true, options: {} }),
    { profile: "focused", fps: 60, maxSize: undefined, scaleFactor: 1 });
  assert.deepEqual(profileVideo({ platform: "android", focused: false, options: {} }),
    { profile: "background", fps: 30, maxSize: 1024, scaleFactor: 1 });
  assert.deepEqual(profileVideo({ platform: "ios", focused: false, options: {} }),
    { profile: "background", fps: 30, maxSize: undefined, scaleFactor: 0.5 });
  // An explicit --fps is the focused rate, and the background never runs faster than it.
  assert.equal(profileVideo({ platform: "android", focused: true, options: { fps: 20, fpsExplicit: true } }).fps, 20);
  assert.equal(profileVideo({ platform: "android", focused: false, options: { fps: 20, fpsExplicit: true } }).fps, 20);
  assert.equal(profileVideo({ platform: "ios", focused: false, options: { fps: 45, fpsExplicit: true } }).fps, 30);
  // An explicit --max-size below the cap wins; 0 (native) counts as unbounded.
  assert.equal(profileVideo({ platform: "android", focused: false, options: { maxSize: 800, maxSizeExplicit: true } }).maxSize, 800);
  assert.equal(profileVideo({ platform: "android", focused: false, options: { maxSize: 0, maxSizeExplicit: true } }).maxSize, 1024);
  assert.equal(profileVideo({ platform: "android", focused: false, options: { maxSize: 2048, maxSizeExplicit: true } }).maxSize, 1024);
  assert.equal(PROFILE.DEMOTE_AFTER_MS, 2000);
  assert.equal(PROFILE.RESTART_MIN_INTERVAL_MS, 3000);
});

function fakeRegistry({ max, fail = new Set(), delay = 0 } = {}) {
  const created = [];
  const closed = [];
  let changes = 0;
  let running = 0;
  let peak = 0;
  const registry = new DeviceRegistry({
    max,
    create: async ({ id }) => {
      created.push(id);
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((resolvePromise) => setTimeout(resolvePromise, delay));
      running -= 1;
      if (fail.has(id)) throw fail.get?.(id) ?? new Error(`cannot open ${id}`);
      return { id };
    },
    close: async (context, reason) => { closed.push([context.id, reason]); },
    onChange: () => { changes += 1; },
  });
  return { registry, created, closed, changes: () => changes, peak: () => peak };
}

test("registry: attach, shared attaches of one id, primary and detach", async () => {
  const { registry, created, closed } = fakeRegistry({ delay: 10 });
  const [first, second] = await Promise.all([
    registry.attach({ platform: "android", target: "emulator-5580" }),
    registry.attach({ platform: "android", target: "emulator-5580" }),
  ]);
  assert.equal(first.attached, true);
  assert.equal(second.attached, false);
  assert.equal(first.entry, second.entry);
  assert.deepEqual(created, ["android~emulator-5580"], "one create for concurrent attaches");
  assert.equal(first.entry.state, "live");
  assert.equal((await registry.attach({ platform: "android", target: "emulator-5580" })).attached, false);
  await registry.attach({ platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "iPhone" });
  assert.equal(registry.primary().id, "android~emulator-5580");
  assert.equal(registry.get("ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5").name, "iPhone");
  assert.equal(registry.size, 2);
  await registry.detach("android~emulator-5580");
  assert.deepEqual(closed, [["android~emulator-5580", "detach"]]);
  assert.equal(registry.primary().id, "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5");
  await assert.rejects(registry.detach("android~emulator-5580"), { code: "device_not_found", status: 404 });
});

test("registry: the limit, failed creates and concurrency", async () => {
  const fail = new Map([["android~bad", new Error("adb target is not ready")],
    ["android~refused", new CanvasError(409, "target_not_ready", "not ready")]]);
  fail.has = Map.prototype.has.bind(fail);
  const { registry, peak } = fakeRegistry({ max: 3, fail, delay: 20 });
  await assert.rejects(registry.attach({ platform: "android", target: "bad" }),
    (error) => error.status === 502 && error.code === "backend_failed" && /not ready/.test(error.message));
  await assert.rejects(registry.attach({ platform: "android", target: "refused" }), { code: "target_not_ready" });
  assert.equal(registry.size, 0, "a failed create leaves no entry");
  await Promise.all(["a", "b", "c"].map((target) => registry.attach({ platform: "android", target })));
  assert.ok(peak() <= ATTACH_CONCURRENCY, `at most ${ATTACH_CONCURRENCY} creates at once, saw ${peak()}`);
  await assert.rejects(registry.attach({ platform: "android", target: "d" }), { code: "device_limit", status: 409 });
});

test("registry: state transitions and detach while attaching", async () => {
  const { registry, closed } = fakeRegistry({ delay: 30 });
  const pending = registry.attach({ platform: "android", target: "slow" });
  assert.equal(registry.get("android~slow").state, "attaching");
  const detaching = registry.detach("android~slow");
  await assert.rejects(pending, { code: "device_detached" });
  await detaching;
  assert.equal(registry.get("android~slow"), null);
  assert.deepEqual(closed, [["android~slow", "detach"]], "the context made meanwhile is closed");

  const { entry } = await registry.attach({ platform: "android", target: "x" });
  registry.setState(entry.id, "offline", { error: "gone" });
  assert.equal(entry.state, "offline");
  assert.equal(entry.error, "gone");
  registry.setState(entry.id, "live");
  registry.setState(entry.id, "failed", { error: "crash" });
  assert.equal(entry.state, "failed");
  registry.replaceContext(entry.id, { id: entry.id, restored: true });
  registry.setState(entry.id, "live");
  assert.equal(entry.context.restored, true);
  assert.throws(() => registry.setState(entry.id, "dancing"), RangeError);
  assert.equal(registry.setState("android~none", "live"), null);
});

/** A fake clock whose timers run when the test advances it. */
function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimer: (fn, ms) => {
      const id = ++seq;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
      }
      now = end;
    },
  };
}

test("focus: promotion is immediate, demotion after 2 s, one restart per 3 s per device", () => {
  const clock = fakeClock();
  const applied = [];
  const focus = new FocusController({
    apply: (id, profile) => applied.push([clock.now(), id, profile]),
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
  });
  const ids = ["a", "b"];
  assert.equal(focus.add("a"), "focused");
  assert.equal(focus.add("b"), "focused");
  clock.advance(10_000);
  // Focus a: b, which ran focused with nobody focused, goes to the background at once.
  focus.setFocus("a", ids);
  assert.equal(focus.profileOf("a"), "focused");
  assert.equal(focus.profileOf("b"), "background");
  assert.deepEqual(applied, [[10_000, "b", "background"]]);
  // Focus b 1 s later: b is promoted at once only if its last restart is 3 s old, so it waits.
  clock.advance(1000);
  focus.setFocus("b", ids);
  assert.equal(focus.profileOf("b"), "focused");
  assert.deepEqual(applied.slice(1), []);
  clock.advance(999);
  assert.deepEqual(applied.slice(1), [], "a keeps its profile for 2 s and b waits for its 3 s");
  clock.advance(1000);
  clock.advance(1);
  assert.deepEqual(applied.filter(([, id]) => id === "b").slice(1), [[13_000, "b", "focused"]],
    "promoted as soon as 3 s passed");
  // a is demoted 2 s after it lost the focus (at 11 000 + 2000), not at once.
  assert.deepEqual(applied.filter(([, id]) => id === "a"), [[13_000, "a", "background"]]);
  assert.equal(focus.profileOf("a"), "background");
});

test("focus: a quick switch back skips the demotion, and the latest wish wins", () => {
  const clock = fakeClock();
  const applied = [];
  const focus = new FocusController({
    apply: (id, profile) => applied.push([id, profile]),
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
  });
  const ids = ["a", "b"];
  focus.add("a");
  focus.add("b");
  clock.advance(5000);
  focus.setFocus("a", ids);
  applied.length = 0;
  clock.advance(5000);
  focus.setFocus("b", ids);
  clock.advance(500);
  focus.setFocus("a", ids);
  clock.advance(5000);
  // b was promoted and then wanted the background again: one restart at most per 3 s.
  assert.deepEqual(applied.filter(([id]) => id === "a"), [], "a never left the focused profile");
  assert.deepEqual(applied.filter(([id]) => id === "b"), [["b", "focused"], ["b", "background"]]);
  focus.forget("a");
  assert.equal(focus.focus, null);
});

test("focus: a restart waits up to 1 s while a pointer is down", () => {
  const clock = fakeClock();
  const applied = [];
  let down = true;
  const focus = new FocusController({
    apply: (id, profile) => applied.push([clock.now(), id, profile]),
    busy: () => down,
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
  });
  focus.add("a");
  focus.add("b");
  clock.advance(10_000);
  focus.setFocus("a", ["a", "b"]);
  assert.deepEqual(applied, []);
  clock.advance(RESTART_INPUT_WAIT_MS - 100);
  assert.deepEqual(applied, []);
  clock.advance(100);
  assert.deepEqual(applied, [[11_000, "b", "background"]], "applied anyway after the wait");
  down = false;
  focus.setFocus(null, ["a", "b"]);
  clock.advance(3000);
  assert.deepEqual(applied.slice(1), [[14_000, "b", "focused"]]);
});

test("focus: appliedOf changes only after a restart that worked; a failed one keeps the old profile", async () => {
  const clock = fakeClock();
  const answers = new Map([["a", []], ["b", []]]);
  const focus = new FocusController({
    apply: (id) => answers.get(id).shift()?.(),
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
  });
  const ids = ["a", "b"];
  focus.add("a");
  focus.add("b");
  assert.equal(focus.appliedOf("a").profile, "focused", "the profile a device joins with");
  assert.ok(Number.isFinite(Date.parse(focus.appliedOf("a").applied_at)));
  assert.equal(focus.appliedOf("nope"), null);
  // b's restart rejects: wanted is background, applied stays focused.
  answers.get("b").push(() => Promise.reject(new Error("restart failed")));
  clock.advance(10_000);
  focus.setFocus("a", ids);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(focus.profileOf("b"), "background");
  assert.equal(focus.appliedOf("b").profile, "focused", "a rejected restart is not applied");
  // An answer of false and a throw are failures too.
  answers.get("a").push(() => Promise.resolve(false));
  clock.advance(5000);
  focus.setFocus("b", ids);
  clock.advance(5000);
  await Promise.resolve();
  assert.equal(focus.profileOf("a"), "background");
  assert.equal(focus.appliedOf("a").profile, "focused", "false is not applied");
  answers.get("b").push(() => { throw new Error("no session"); });
  clock.advance(5000);
  focus.setFocus("a", ids);
  clock.advance(5000);
  assert.equal(focus.appliedOf("b").profile, "focused", "a throw is not applied");
  // A restart that resolves is applied, at the time it finished, not when it was asked for.
  let finishA;
  answers.get("a").push(() => new Promise((resolve) => { finishA = resolve; }));
  assert.equal(focus.appliedOf("a").profile, "focused");
  clock.advance(5000);
  focus.setFocus("b", ids);
  clock.advance(2000);
  assert.equal(typeof finishA, "function", "a is demoted 2 s after it lost the focus");
  assert.equal(focus.appliedOf("a").profile, "focused", "not applied while the restart runs");
  const startedAt = clock.now();
  clock.advance(1500);
  finishA(undefined);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(focus.appliedOf("a").profile, "background");
  assert.equal(focus.appliedOf("a").at, startedAt + 1500, "applied when the restart finished");
});

test("focus: a restart overtaken by a newer one, or of a device that left, is not recorded", async () => {
  const clock = fakeClock();
  const pending = [];
  const focus = new FocusController({
    apply: (id, profile) => new Promise((resolve) => pending.push({ id, profile, resolve })),
    now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
  });
  const ids = ["a", "b"];
  focus.add("a");
  focus.add("b");
  clock.advance(10_000);
  focus.setFocus("a", ids);
  clock.advance(5000);
  focus.setFocus("b", ids);
  const [toBackground, toFocused] = pending.filter((item) => item.id === "b");
  assert.equal(toBackground.profile, "background");
  assert.equal(toFocused.profile, "focused");
  toFocused.resolve(true);
  await Promise.resolve();
  await Promise.resolve();
  toBackground.resolve(true);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(focus.appliedOf("b").profile, "focused", "the older restart finishing late does not win");
  clock.advance(3000);
  const late = pending.find((item) => item.id === "a");
  assert.equal(late?.profile, "background", "a is demoted after it lost the focus");
  focus.forget("a");
  late.resolve(true);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(focus.appliedOf("a"), null);
});
