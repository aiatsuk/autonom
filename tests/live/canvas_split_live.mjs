#!/usr/bin/env node
/**
 * Live end-to-end check of the Canvas workspace with several devices (run canvas-multi-device,
 * contract 6.5). Not part of `node --test tests/*.test.mjs`: it starts this checkout's real
 * `autonom canvas` and drives real test devices.
 *
 *   node tests/live/canvas_split_live.mjs --evidence-dir DIR [--ui] [--four] [--boot]
 *     [--adb PATH] [--xcrun PATH] [--python PATH] [--keep-home]
 *   node tests/live/canvas_split_live.mjs --dry-run [--ui] [--four] [--boot]   (prints the plan)
 *   node tests/live/canvas_split_live.mjs --self-test                          (pure helpers only)
 *
 * Allowed devices only: emulator-5580, the Autonom-Fast-Test Simulator, Autonom_Split_API36 on
 * emulator-5584, the Autonom-Split-Test Simulator and Autonom_Split2_API36 on emulator-5586.
 * Every target goes through allowedTarget() before a Canvas or a device command sees it; any
 * other one (emulator-5554 above all) is refused. Every run gets its own temporary AUTONOM_HOME
 * and captures folder, and every Canvas listens on --port 0.
 *
 * Base case (always): the run starts its own primary session on emulator-5580, then a
 * workspace Canvas with no device; attaches emulator-5580 (reusing that session) and
 * Autonom-Fast-Test (a new Canvas-started session) into tab 1 with layout 2; checks that
 * current.json still names the primary, both tiles stream, input reaches only the focused
 * device, a focus switch changes the applied stream profile (profile_applied: fps cap and size,
 * and the Android stream's in-use max_size from its /status) within 5 s, each device
 * journals into its own session, a network attach changes only the Android device, through the
 * Canvas API emulator-5580 takes a screenshot and a ~3 s recording into the temporary captures
 * folder, launches an app and opens a URL, the activity log holds these ops with durations and
 * results and /api/health reports adb and xcrun_simctl ok, a new
 * tab 2 takes the iOS device by "Move here", killing scrcpy-server on emulator-5580 leaves iOS
 * streaming while Android recovers, and a restart (no --ephemeral) while both devices are
 * attached restores both tabs and attaches both again. Then both are detached: the
 * Canvas-started session ends, the primary does not, the Canvas keeps serving, and
 * `autonom canvas stop` ends it.
 *
 * --ui (with the base case, while both devices are in tab 1): headless Chromium through
 * Playwright (PLAYWRIGHT_ROOT, default /Users/gmh-basket/pr/defuddle-eval) at 1440x900 and
 * 390x844, light and dark: no horizontal overflow, the tab bar, two tiles, the empty-tile
 * picker of a new tab, the Actions drawer of the focused tile, and a tap made through the
 * focused tile's page that reaches only that device. Screenshots go to the evidence dir.
 *
 * --four: boots Autonom_Split_API36 (--port 5584) and Autonom-Split-Test when they are not
 * running, puts 4 tiles in one tab, opens a video viewer for all four at the same time and
 * requires video bytes from each within IOS_STREAM_LIMIT_MS (iOS viewers ask for a key frame
 * when quiet) over a fast transport (scrcpy or idb; a screenshot fallback fails), recording
 * each device's transport and bytes and the host load; checks a 5th device for that tab is
 * refused 409 tab_full, and shuts down what it booted (emulator-5586 too, if the run started it).
 *
 * --boot: a Canvas with --bootable avd:Autonom_Split2_API36@5586 --shutdown-booted boots the
 * AVD the way the picker does (POST /api/boot), it attaches, and it is shut down at Canvas stop.
 * Before that it asks to boot Autonom_Split_API36 (not allowlisted) and checks the 403
 * boot_not_allowed refusal and that no new emulator serial appears.
 *
 * Writes one report per case so a later case never overwrites an earlier one's evidence: the
 * base case (with or without --ui) writes split-live.json and an identical split.json, a run
 * with --four writes split-four.json and one with --boot split-boot.json. Before the
 * temporary AUTONOM_HOME is deleted the report records the Canvas-started sessions per
 * platform. Evidence holds states, counts, codes and ids only, never the Canvas token. Exits 0 when every step and restore passed, 1 otherwise, 2 on bad
 * arguments.
 */
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { homedir, loadavg, tmpdir } from "node:os";
import { extname, join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "../..");
const AUTONOM = join(ROOT, "scripts/autonom.py");
const DEFAULT_PLAYWRIGHT_ROOT = "/Users/gmh-basket/pr/defuddle-eval";
const REFUSED_SERIAL = "emulator-5554";
// The only targets this script may touch (contract 6.5).
const DEVICES = Object.freeze({
  primary: { platform: "android", target: "emulator-5580", name: "emulator-5580" },
  fastIos: { platform: "ios", target: "3760A5A8-E59D-4AE6-B1AF-A626908D7B61", name: "Autonom-Fast-Test" },
  splitAndroid: { platform: "android", target: "emulator-5584", name: "Autonom_Split_API36", avd: "Autonom_Split_API36", port: 5584 },
  splitIos: { platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "Autonom-Split-Test" },
  bootAndroid: { platform: "android", target: "emulator-5586", name: "Autonom_Split2_API36", avd: "Autonom_Split2_API36", port: 5586 },
});
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const SCHEMES = ["light", "dark"];
const PROFILE_LIMIT_MS = 5000;
const RECOVER_LIMIT_MS = 30_000;
// iOS sends frames only when its screen changes, so a 3 s window can see none on a loaded
// host; the stream counts as "on" once it grows within this bounded window.
const IOS_STREAM_LIMIT_MS = 20_000;
const IOS_QUIET_MS = 5000;
const RECORD_MS = 3000;
const LAUNCH_APP = "com.android.settings";
const OPEN_URL = "https://example.com/autonom-split-live";
const PARITY_OPS = Object.freeze(["capture.screenshot", "record.start", "record.stop", "app.launch", "app.open_url"]);
const HEALTH_TOOLS = Object.freeze(["adb", "xcrun_simctl"]);
const ATTACH_WAIT_MS = 90_000;
const BOOT_WAIT_MS = 300_000;
const liveSecrets = new Set();

const USAGE = `usage: canvas_split_live.mjs --evidence-dir DIR [--ui] [--four] [--boot]
  [--adb PATH] [--xcrun PATH] [--python PATH] [--keep-home] [--dry-run]
       canvas_split_live.mjs --self-test`;

let args;
try {
  ({ values: args } = parseArgs({
    options: {
      "evidence-dir": { type: "string" },
      ui: { type: "boolean", default: false },
      four: { type: "boolean", default: false },
      boot: { type: "boolean", default: false },
      serial: { type: "string" },
      udid: { type: "string" },
      adb: { type: "string", default: "adb" },
      xcrun: { type: "string", default: "xcrun" },
      python: { type: "string", default: "python3" },
      "keep-home": { type: "boolean", default: false },
      "dry-run": { type: "boolean", default: false },
      "self-test": { type: "boolean", default: false },
      help: { type: "boolean", short: "h", default: false },
    },
  }));
} catch (error) {
  usage(error.message);
}

function usage(message) {
  console.error(`canvas_split_live: ${message}`);
  console.error(USAGE);
  process.exit(2);
}

if (args.help) {
  console.log(USAGE);
  process.exit(0);
}

// --- pure helpers (checked by --self-test) --------------------------------------------------

/** The allowed device for (platform, target), or an Error naming why it is refused. */
function allowedTarget(platform, target) {
  if (target === REFUSED_SERIAL) return new Error(`${REFUSED_SERIAL} is never used by this check`);
  const found = Object.values(DEVICES).find((device) => device.platform === platform && device.target === target);
  return found ?? new Error(`${platform}:${target ?? "(none)"} is not one of the allowed test devices`);
}

/** allowedTarget, throwing: every device command and attach goes through it. */
function guard(platform, target) {
  const found = allowedTarget(platform, target);
  if (found instanceof Error) throw found;
  return found;
}

/** The phases of this run, in order. */
function planPhases(options) {
  return ["base", ...(options.ui ? ["ui"] : []), ...(options.four ? ["four"] : []), ...(options.boot ? ["boot"] : [])];
}

/** The Canvas page URL (with its #token) from the CLI's output, or null. */
function previewUrl(output) {
  const match = String(output).match(/Preview at (http:\/\/127\.0\.0\.1:(\d+)\/(?:#token=\S+)?)/);
  return match ? { url: match[1], port: Number(match[2]) } : null;
}

/** The device id of the Canvas (`<platform>~<target>`) and its URL path segment. */
function deviceId(device) {
  return `${device.platform}~${device.target}`;
}

function devicePath(id) {
  return `/d/${encodeURIComponent(id)}`;
}

/** Command log entries of one kind and name for a device. */
function countEntries(entries, device, name, kind = "input") {
  return (entries ?? []).filter((entry) => entry?.device === device && entry?.kind === kind && entry?.name === name).length;
}

/**
 * Whether a DeviceSummary's applied profile (profile_applied, set only once the stream
 * restarted with it) is the focused or the contract's background profile, applied at or
 * after `sinceMs` (wall clock).
 */
function appliedMatches(summary, focused, sinceMs = 0) {
  const applied = summary?.profile_applied;
  if (!applied || !(Date.parse(applied.applied_at) >= sinceMs)) return false;
  if (focused) return applied.profile === "focused" && summary.focused === true;
  if (applied.profile !== "background" || summary.focused) return false;
  if (!(applied.fps_cap <= 30)) return false;
  if (summary.platform === "android") return applied.max_size !== null && applied.max_size <= 1024;
  return applied.scale_factor === 0.5;
}

/** Milliseconds from `sinceMs` to the latest applied_at of `summaries` (null when one has none). */
function appliedElapsed(summaries, sinceMs) {
  const times = summaries.map((summary) => Date.parse(summary?.profile_applied?.applied_at));
  return times.every(Number.isFinite) ? Math.max(...times) - sinceMs : null;
}

/** VIDEO_MESSAGE.PACKET of the Canvas video socket: [3, keyFrame, pts(8), data...]. */
const VIDEO_PACKET = 3;

/** One video socket message: its encoded-video bytes (PACKET only), or the other bytes it holds. */
function countVideoMessage(data) {
  if (typeof data === "string") return { bytes: 0, packet: false, keyFrame: false, other: data.length };
  const view = new Uint8Array(data);
  if (view.length > 10 && view[0] === VIDEO_PACKET) {
    return { bytes: view.length - 10, packet: true, keyFrame: view[1] === 1, other: 0 };
  }
  return { bytes: 0, packet: false, keyFrame: false, other: view.length };
}

/** The video transports that stream over the video socket; anything else is a screenshot fallback. */
const VIDEO_TRANSPORTS = Object.freeze(["scrcpy", "idb"]);

/** Every device of a four-stream reading streamed video bytes over a video transport, viewer still open. */
function fourStreamsOk(rows) {
  return rows.length === 4 && rows.every((row) => VIDEO_TRANSPORTS.includes(row.transport) && row.bytes > 0 && row.packets > 0 &&
    row.viewer_closed === false && row.fallback === null);
}

/** Canvas-started sessions per platform from session.json records: {platform: {started, stopped}}. */
function canvasSessionCounts(records) {
  const counts = {};
  for (const record of records ?? []) {
    if (record?.started_by?.kind !== "canvas") continue;
    const row = counts[record.platform ?? "unknown"] ??= { started: 0, stopped: 0 };
    row.started += 1;
    if (record.stopped_at) row.stopped += 1;
  }
  return counts;
}

/** The report files a run writes: one name per case (CR-4). */
function reportNames(options) {
  const names = [];
  if (!options.four && !options.boot) names.push("split-live.json", "split.json");
  if (options.four) names.push("split-four.json");
  if (options.boot) names.push("split-boot.json");
  return names;
}

/** The tabs of two /api/workspace answers agree: ids, names, layouts and placed devices. */
function sameTabs(before, after) {
  const shape = (workspace) => (workspace?.tabs ?? []).map((tab) => ({
    id: tab.id, name: tab.name, layout: tab.layout, devices: (tab.slots ?? []).map((slot) => slot?.device ?? null),
  }));
  try {
    assert.deepEqual(shape(after), shape(before));
    return shape(before).length > 0;
  } catch {
    return false;
  }
}

/** A restored device is settled: live, and its session start has answered (a link or an error). */
function sessionSettled(summary) {
  return summary?.state === "live" && (Boolean(summary.session?.id) || Boolean(summary.session_error));
}

/** Whether the iOS viewer asks for a key frame now: quiet IOS_QUIET_MS since the kill and since the last ask. */
function keyframeDue(sinceKillMs, sinceAskMs, asked, quietMs = IOS_QUIET_MS) {
  return sinceKillMs >= quietMs && (asked === 0 || sinceAskMs >= quietMs);
}

/** A capture file is good: inside `root`, the expected extension and not empty. */
function captureOk(file, root, ext) {
  if (!file || typeof file.path !== "string") return false;
  const inside = relative(root, file.path);
  return !inside.startsWith("..") && !inside.startsWith("/") && extname(file.path).toLowerCase() === `.${ext}` &&
    Number.isInteger(file.size) && file.size > 0;
}

/** The finished activity entries of `ops` for one device: each has a duration and a result. */
function opEntries(entries, device, ops) {
  return ops.map((op) => {
    const found = (entries ?? []).filter((entry) => entry?.device === device && entry?.kind === "tools" && entry?.name === op);
    const last = found.at(-1) ?? null;
    return { op, count: found.length, ok: last?.ok ?? null, duration_ms: last?.duration_ms ?? null,
      error_code: last?.error_code ?? null };
  });
}

/** Every op ran once at least and its latest entry ended ok with a duration. */
function opEntriesOk(rows) {
  return rows.length > 0 && rows.every((row) => row.count > 0 && row.ok === true && Number.isInteger(row.duration_ms));
}

/** The named tools of a /api/health answer, each {found, ok}; missing names answer null. */
function healthOf(body, names = HEALTH_TOOLS) {
  const tools = Array.isArray(body?.tools) ? body.tools : [];
  return Object.fromEntries(names.map((name) => {
    const tool = tools.find((item) => item?.name === name);
    return [name, tool ? { found: Boolean(tool.found), ok: Boolean(tool.ok) } : null];
  }));
}

/** The 1, 5 and 15 minute load averages, rounded, for the evidence. */
function hostLoad(values = loadavg()) {
  return values.map((value) => Math.round(value * 100) / 100);
}

/** How the 5th device for a full tab is offered: an attach of a running spare, else a boot request. */
function fifthProbe(running, attachedIds) {
  const spare = (running ?? []).find((item) => {
    const device = allowedTarget(item?.platform, item?.target);
    return !(device instanceof Error) && !attachedIds.includes(deviceId(device));
  });
  if (spare) return { kind: "attach", platform: spare.platform, target: spare.target };
  return { kind: "boot", name: DEVICES.bootAndroid.avd };
}

/** `text` with the Canvas tokens of this run, `token=` values and Bearer values taken out. */
function redactSecrets(text, secrets = liveSecrets) {
  let out = String(text ?? "");
  for (const secret of secrets) if (secret) out = out.split(secret).join("<redacted>");
  return out
    .replace(/([#?&;]token=)[^&#\s"'<>\\]*/gi, "$1<redacted>")
    .replace(/(Bearer\s+)[^\s"'\\]+/gi, "$1<redacted>");
}

function errorText(error, max = 300) {
  return redactSecrets(String(error?.message ?? error)).slice(0, max);
}

function selfTest() {
  // Targets: the allowed five pass, emulator-5554 and everything else is refused.
  assert.equal(allowedTarget("android", "emulator-5580").name, "emulator-5580");
  assert.equal(allowedTarget("ios", "3760A5A8-E59D-4AE6-B1AF-A626908D7B61").name, "Autonom-Fast-Test");
  assert.equal(allowedTarget("android", "emulator-5584").avd, "Autonom_Split_API36");
  assert.equal(allowedTarget("ios", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5").name, "Autonom-Split-Test");
  assert.equal(allowedTarget("android", "emulator-5586").port, 5586);
  assert.match(allowedTarget("android", "emulator-5554").message, /never used/);
  assert.ok(allowedTarget("ios", "emulator-5580") instanceof Error);
  assert.ok(allowedTarget("android", "emulator-5582") instanceof Error);
  assert.ok(allowedTarget("android", undefined) instanceof Error);
  assert.throws(() => guard("android", "emulator-5554"), /never used/);
  assert.throws(() => guard("android", "R5CT1234"), /not one of the allowed/);

  let draws = 0;
  const dashFirst = (n) => Buffer.alloc(n, draws++ === 0 ? 0xf8 : 0x41);
  assert.ok(Buffer.alloc(18, 0xf8).toString("base64url").startsWith("-"));
  const safe = liveToken(dashFirst);
  assert.ok(!safe.startsWith("-") && draws === 2, safe);
  for (let i = 0; i < 200; i += 1) assert.ok(!liveToken().startsWith("-"));

  assert.deepEqual(planPhases({}), ["base"]);
  assert.deepEqual(planPhases({ ui: true, four: true, boot: true }), ["base", "ui", "four", "boot"]);

  assert.deepEqual(previewUrl("autonom Canvas workspace default ready\nPreview at http://127.0.0.1:51234/#token=abc\n"),
    { url: "http://127.0.0.1:51234/#token=abc", port: 51234 });
  assert.deepEqual(previewUrl("Preview at http://127.0.0.1:4000/\n"), { url: "http://127.0.0.1:4000/", port: 4000 });
  assert.equal(previewUrl("Preview at http://example.com:1/"), null);

  assert.equal(deviceId(DEVICES.fastIos), "ios~3760A5A8-E59D-4AE6-B1AF-A626908D7B61");
  assert.equal(devicePath("android~127.0.0.1:5555"), "/d/android~127.0.0.1%3A5555");

  const entries = [{ device: "a", kind: "input", name: "tap" }, { device: "a", kind: "input", name: "tap" },
    { device: "b", kind: "input", name: "tap" }, { device: "a", kind: "tools", name: "tap" }];
  assert.equal(countEntries(entries, "a", "tap"), 2);
  assert.equal(countEntries(entries, "b", "tap"), 1);
  assert.equal(countEntries(undefined, "a", "tap"), 0);


  const workspace = { tabs: [{ id: "t_1", name: "Canvas 1", layout: 2, slots: [{ device: "a", present: true }, null] }] };
  assert.equal(sameTabs(workspace, structuredClone(workspace)), true);
  assert.equal(sameTabs(workspace, { tabs: [{ ...workspace.tabs[0], name: "Other" }] }), false);
  assert.equal(sameTabs({ tabs: [] }, { tabs: [] }), false);

  assert.deepEqual(fifthProbe([{ platform: "android", target: "emulator-5586" }], ["android~emulator-5580"]),
    { kind: "attach", platform: "android", target: "emulator-5586" });
  assert.deepEqual(fifthProbe([{ platform: "android", target: "emulator-5554" }, { platform: "android", target: "emulator-5580" }],
    ["android~emulator-5580"]), { kind: "boot", name: "Autonom_Split2_API36" });

  const token = "AbC-_123tokenValue";
  const secrets = new Set([token]);
  for (const text of [`at http://127.0.0.1:1/#token=${token}`, `Authorization: Bearer ${token}`, `the ${token} leaked`,
    `ws://127.0.0.1:2/d/x/ws/video?token=${token}`]) {
    assert.ok(!redactSecrets(text, secrets).includes(token), text);
  }
  assert.equal(redactSecrets("http://h/?a=1&token=q1&b=2", new Set()), "http://h/?a=1&token=<redacted>&b=2");

  // A restored device is settled once its session start answered, not merely live.
  assert.equal(sessionSettled({ state: "live", session: null, session_error: null }), false);
  assert.equal(sessionSettled({ state: "live", session: { id: "s_1" }, session_error: null }), true);
  assert.equal(sessionSettled({ state: "live", session: null, session_error: { error_code: "backend_failed" } }), true);
  assert.equal(sessionSettled({ state: "offline", session: { id: "s_1" } }), false);
  assert.equal(sessionSettled(null), false);

  const root = "/tmp/autonom-canvas-split-captures-x";
  assert.equal(captureOk({ path: `${root}/emulator-5580/a.png`, size: 10 }, root, "png"), true);
  assert.equal(captureOk({ path: `${root}/emulator-5580/a.mp4`, size: 10 }, root, "png"), false, "wrong type");
  assert.equal(captureOk({ path: `${root}/emulator-5580/a.png`, size: 0 }, root, "png"), false, "empty");
  assert.equal(captureOk({ path: "/Users/x/Downloads/Autonom/a.png", size: 10 }, root, "png"), false, "outside");
  assert.equal(captureOk({ path: `${root}/../a.png`, size: 10 }, root, "png"), false, "escapes");
  assert.equal(captureOk(null, root, "png"), false);

  const log = [
    { device: "a", kind: "tools", name: "app.launch", ok: false, duration_ms: 5, error_code: "x" },
    { device: "a", kind: "tools", name: "app.launch", ok: true, duration_ms: 40, error_code: null },
    { device: "b", kind: "tools", name: "app.open_url", ok: true, duration_ms: 9 },
    { device: "a", kind: "input", name: "app.open_url", ok: true, duration_ms: 9 },
  ];
  assert.deepEqual(opEntries(log, "a", ["app.launch", "app.open_url"]), [
    { op: "app.launch", count: 2, ok: true, duration_ms: 40, error_code: null },
    { op: "app.open_url", count: 0, ok: null, duration_ms: null, error_code: null }]);
  assert.equal(opEntriesOk(opEntries(log, "a", ["app.launch"])), true);
  assert.equal(opEntriesOk(opEntries(log, "a", ["app.launch", "app.open_url"])), false);
  assert.equal(opEntriesOk([{ op: "x", count: 1, ok: true, duration_ms: null }]), false, "a duration is required");
  assert.equal(opEntriesOk([]), false);

  assert.deepEqual(healthOf({ tools: [{ name: "adb", found: true, ok: true }, { name: "xcrun_simctl", found: true, ok: false }] }),
    { adb: { found: true, ok: true }, xcrun_simctl: { found: true, ok: false } });
  assert.deepEqual(healthOf({}), { adb: null, xcrun_simctl: null });
  assert.deepEqual(hostLoad([1.234, 40, 0.005]), [1.23, 40, 0.01]);

  const at = "2026-10-08T10:00:03.000Z";
  const since = Date.parse("2026-10-08T10:00:00.000Z");
  const bg = { platform: "android", focused: false, profile: "background",
    profile_applied: { profile: "background", fps_cap: 30, max_size: 1024, scale_factor: null, applied_at: at } };
  assert.equal(appliedMatches(bg, false, since), true);
  assert.equal(appliedMatches(bg, false, since + 4000), false, "applied before the switch does not count");
  assert.equal(appliedMatches({ ...bg, profile_applied: { ...bg.profile_applied, profile: "focused" } }, false, since), false,
    "the wanted profile alone is not enough");
  assert.equal(appliedMatches({ ...bg, profile_applied: null }, false, since), false);
  assert.equal(appliedMatches({ ...bg, profile_applied: { ...bg.profile_applied, max_size: null } }, false, since), false);
  const fg = { platform: "ios", focused: true, profile_applied: { profile: "focused", fps_cap: 60, scale_factor: 1, applied_at: at } };
  assert.equal(appliedMatches(fg, true, since), true);
  assert.equal(appliedMatches({ ...fg, focused: false }, true, since), false);
  assert.equal(appliedMatches({ platform: "ios", focused: false,
    profile_applied: { profile: "background", fps_cap: 30, scale_factor: 0.5, applied_at: at } }, false, since), true);
  assert.equal(appliedElapsed([bg, fg], since), 3000);
  assert.equal(appliedElapsed([bg, { profile_applied: null }], since), null);

  const row = { transport: "scrcpy", bytes: 10, packets: 1, viewer_closed: false, fallback: null };
  assert.equal(fourStreamsOk([row, { ...row, transport: "idb" }, row, { ...row, transport: "idb" }]), true);
  assert.equal(fourStreamsOk([row, row, row]), false, "four devices");
  assert.equal(fourStreamsOk([row, row, row, { ...row, transport: "screencap" }]), false, "a screenshot fallback fails");
  assert.equal(fourStreamsOk([row, row, row, { ...row, bytes: 0 }]), false);
  assert.equal(fourStreamsOk([row, row, row, { ...row, viewer_closed: true }]), false);
  assert.equal(fourStreamsOk([row, row, row, { ...row, fallback: { error: "x" } }]), false);
  assert.equal(fourStreamsOk([row, row, row, { ...row, packets: 0 }]), false, "state messages alone are not a stream");

  const packet = new Uint8Array([3, 1, 0, 0, 0, 0, 0, 0, 0, 7, 0xaa, 0xbb]).buffer;
  assert.deepEqual(countVideoMessage(packet), { bytes: 2, packet: true, keyFrame: true, other: 0 });
  assert.deepEqual(countVideoMessage(new Uint8Array([1, 0, 0, 4]).buffer), { bytes: 0, packet: false, keyFrame: false, other: 4 });
  assert.deepEqual(countVideoMessage('{"t":"state"}'), { bytes: 0, packet: false, keyFrame: false, other: 13 });

  assert.deepEqual(canvasSessionCounts([
    { platform: "android", started_by: null },
    { platform: "ios", started_by: { kind: "canvas" }, stopped_at: "t" },
    { platform: "ios", started_by: { kind: "canvas" } },
    { platform: "android", started_by: { kind: "canvas" }, stopped_at: "t" },
  ]), { ios: { started: 2, stopped: 1 }, android: { started: 1, stopped: 1 } });
  assert.deepEqual(canvasSessionCounts(undefined), {});

  assert.deepEqual(reportNames({}), ["split-live.json", "split.json"]);
  assert.deepEqual(reportNames({ ui: true }), ["split-live.json", "split.json"]);
  assert.deepEqual(reportNames({ four: true }), ["split-four.json"]);
  assert.deepEqual(reportNames({ boot: true }), ["split-boot.json"]);
  assert.deepEqual(reportNames({ four: true, boot: true }), ["split-four.json", "split-boot.json"]);

  assert.equal(keyframeDue(4999, 4999, 0), false, "not before the quiet window");
  assert.equal(keyframeDue(5000, 5000, 0), true);
  assert.equal(keyframeDue(7000, 2000, 1), false, "one ask per quiet window");
  assert.equal(keyframeDue(10_000, 5000, 1), true);
}

if (args["self-test"]) {
  selfTest();
  console.log("ok: canvas_split_live self-test");
  process.exit(0);
}

// The target guard runs before anything else: only the allowed devices, never emulator-5554.
if (args.serial !== undefined && args.serial !== DEVICES.primary.target) {
  usage(args.serial === REFUSED_SERIAL ? `${REFUSED_SERIAL} is never used by this check`
    : `the primary device is ${DEVICES.primary.target}, not ${args.serial}`);
}
if (args.udid !== undefined && args.udid !== DEVICES.fastIos.target) {
  usage(`the iOS device is ${DEVICES.fastIos.name} (${DEVICES.fastIos.target}), not ${args.udid}`);
}
const phases = planPhases(args);
const playwrightRoot = process.env.PLAYWRIGHT_ROOT || DEFAULT_PLAYWRIGHT_ROOT;

if (args["dry-run"]) {
  console.log(JSON.stringify({
    dry_run: true,
    phases,
    devices: {
      base: [DEVICES.primary, DEVICES.fastIos],
      four: args.four ? [DEVICES.primary, DEVICES.fastIos, DEVICES.splitAndroid, DEVICES.splitIos] : null,
      boot: args.boot ? [DEVICES.bootAndroid] : null,
    },
    refused: [REFUSED_SERIAL, "any target not listed above"],
    canvas: "autonom canvas serve --port 0 (workspace, temporary AUTONOM_HOME and captures folder)",
    ui: args.ui ? { playwright_root: playwrightRoot, viewports: VIEWPORTS, schemes: SCHEMES } : null,
    evidence: reportNames(args).map((name) => join(resolve(args["evidence-dir"] ?? "<temporary directory>"), name)),
    restores: ["devices detached and Canvases stopped", "network detached and stopped",
      "emulator-5580 back on the home screen (after the app launch and URL)",
      "primary session stopped", "Simulators and emulators booted by this run shut down (emulator-5586 too)",
      "temporary AUTONOM_HOME and captures folder removed (unless --keep-home)"],
  }, null, 2));
  process.exit(0);
}

if (!args["evidence-dir"]) usage("--evidence-dir is required");
const outDir = resolve(args["evidence-dir"]);
await mkdir(outDir, { recursive: true });
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-canvas-split-home-"));
const capturesDir = await mkdtemp(join(tmpdir(), "autonom-canvas-split-captures-"));
const liveEnv = { ...process.env, AUTONOM_HOME: autonomHome, AUTONOM_CAPTURES_DIR: capturesDir };
delete liveEnv.XDG_STATE_HOME;

const report = {
  started_at: new Date().toISOString(), phases, host_load_start: hostLoad(),
  sections: Object.fromEntries(phases.map((name) => [name, { ok: false, steps: [] }])),
  restore: { ok: false, steps: [] }, ok: false,
};
const state = {
  canvases: new Set(), primarySession: null, simulatorsBooted: [], emulatorsBooted: [],
  networkStarted: false, networkAttached: false, primaryForeground: false,
};

/** Write the report (so far) under every name of this run's cases. */
async function writeReport() {
  const text = `${redactSecrets(JSON.stringify(report, null, 2))}\n`;
  const files = reportNames(args).map((name) => join(outDir, name));
  for (const file of files) await writeFile(file, text);
  return files;
}

// --- process helpers ------------------------------------------------------------------------

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function run(file, argv, { timeout = 60_000, env = liveEnv } = {}) {
  return new Promise((resolvePromise) => {
    execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (error.code ?? 1) : 0, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
      });
  });
}

/** adb on one allowed emulator. */
async function adb(target, argv, options) {
  guard("android", target);
  return await run(args.adb, ["-s", target, ...argv], options);
}

/** simctl on one allowed Simulator (`argv` holds the UDID where simctl wants it). */
async function simctl(udid, argv, options) {
  guard("ios", udid);
  return await run(args.xcrun, ["simctl", ...argv], options);
}

/** The Autonom CLI in this run's AUTONOM_HOME; JSON stdout parsed when it is JSON. */
async function autonom(argv, options = {}) {
  const result = await run(args.python, [AUTONOM, ...argv], { timeout: 240_000, ...options });
  try { result.json = JSON.parse(result.stdout); } catch { result.json = null; }
  try { result.error = JSON.parse(result.stderr.trim().split("\n").pop() || "null"); } catch { result.error = null; }
  return result;
}

function targetFlags(device) {
  guard(device.platform, device.target);
  return device.platform === "android"
    ? ["--serial", device.target, ...(args.adb !== "adb" ? ["--adb", args.adb] : [])]
    : ["--platform", "ios", "--udid", device.target, ...(args.xcrun !== "xcrun" ? ["--simctl", args.xcrun] : [])];
}

async function waitFor(predicate, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value || Date.now() >= deadline) return value;
    await sleep(intervalMs);
  }
}

// --- evidence -------------------------------------------------------------------------------

function step(section, name, ok, detail = {}) {
  const entry = { name, ok: Boolean(ok), at: new Date().toISOString(), ...detail };
  report.sections[section].steps.push(entry);
  console.error(`${entry.at} ${entry.ok ? "ok" : "FAILED"} ${section}: ${name}`);
  // The report so far, so a run stopped from outside still leaves its evidence.
  writeReport().catch(() => {});
  return entry.ok;
}

async function phase(name, work) {
  const section = report.sections[name];
  try {
    await work(name);
  } catch (error) {
    step(name, "phase error", false, { error: errorText(error, 400) });
  }
  section.ok = section.steps.length > 0 && section.steps.every((entry) => entry.ok);
}

async function restoreStep(name, work) {
  try {
    const result = await work();
    const ok = typeof result === "object" && result !== null ? Boolean(result.ok) : Boolean(result);
    report.restore.steps.push({ name, ok, ...(typeof result === "object" && result !== null ? result : {}) });
  } catch (error) {
    report.restore.steps.push({ name, ok: false, error: errorText(error, 300) });
  }
}

// --- devices --------------------------------------------------------------------------------

async function androidOnline(device) {
  return (await adb(device.target, ["get-state"])).stdout.trim() === "device";
}

async function simulatorState(device) {
  const listed = await simctl(device.target, ["list", "devices", "--json"]);
  const found = Object.values(JSON.parse(listed.stdout || "{}").devices ?? {}).flat()
    .find((item) => item.udid === device.target);
  if (!found) return { state: null, name: null };
  return { state: found.state, name: found.name };
}

/** A Simulator in state Booted; booted here (and remembered for the restore) when it was Shutdown. */
async function ensureSimulator(device) {
  const found = await simulatorState(device);
  if (!found.state) throw new Error(`${device.name} is not listed by simctl`);
  if (found.name !== device.name) throw new Error(`${device.target} is named ${found.name}; refusing it`);
  if (found.state === "Booted") return false;
  if (found.state !== "Shutdown") throw new Error(`${device.name} is ${found.state}; it must be Booted or Shutdown`);
  const booted = await simctl(device.target, ["boot", device.target]);
  if (booted.code !== 0) throw new Error(`simctl boot ${device.name} failed`);
  state.simulatorsBooted.push(device);
  await simctl(device.target, ["bootstatus", device.target, "-b"], { timeout: 300_000 });
  return true;
}

/** An emulator of an allowed AVD on its fixed port; booted here when it was not running. */
async function ensureEmulator(device) {
  if (await androidOnline(device)) return false;
  const booted = await autonom(["devices", "boot", "--avd", device.avd, "--port", String(device.port), "--timeout", "180",
    ...(args.adb !== "adb" ? ["--adb", args.adb] : [])], { timeout: 260_000 });
  if (booted.code !== 0 || booted.json?.serial !== device.target) {
    throw new Error(`devices boot ${device.avd} failed: ${errorText(booted.error?.error ?? booted.stderr, 200)}`);
  }
  if (!booted.json.already_running) state.emulatorsBooted.push(device);
  return true;
}

async function shutdownDevice(device) {
  const result = await autonom([...targetFlags(device), "devices", "shutdown"], { timeout: 120_000 });
  return { ok: result.code === 0, device: deviceId(device) };
}

// --- the Canvas -----------------------------------------------------------------------------

/**
 * A random Canvas token that never starts with "-" (argparse would read such an argv item as
 * an option: "argument --token: expected one argument"); it is passed as `--token=VALUE` too.
 */
function liveToken(random = randomBytes) {
  for (;;) {
    const token = random(18).toString("base64url");
    if (!token.startsWith("-")) return token;
  }
}

/** `autonom canvas serve --port 0` with no device (a workspace Canvas), supervised by the CLI. */
async function startCanvas({ workspace = null, extra = [] } = {}) {
  const token = liveToken();
  liveSecrets.add(token);
  const argv = [AUTONOM, ...(args.adb !== "adb" ? ["--adb", args.adb] : []), ...(args.xcrun !== "xcrun" ? ["--simctl", args.xcrun] : []),
    "canvas", "serve", "--port", "0", `--token=${token}`, "--captures-dir", capturesDir,
    ...(workspace ? ["--workspace", workspace] : []), ...extra];
  const child = spawn(args.python, argv, { cwd: ROOT, env: liveEnv, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  let found;
  try {
    found = await new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error("the Canvas did not print its URL within 45 s")), 45_000);
      child.stdout.on("data", () => {
        const match = previewUrl(output);
        if (!match) return;
        clearTimeout(timer);
        resolvePromise(match);
      });
      exited.then((code) => {
        clearTimeout(timer);
        reject(new Error(`the Canvas exited ${code} before it started: ${errorText(output, 300)}`));
      });
    });
  } catch (error) {
    if (running()) child.kill("SIGTERM");
    throw error;
  }
  const origin = `http://127.0.0.1:${found.port}`;
  const request = async (method, path, body, { timeoutMs = 180_000 } = {}) => {
    const headers = { Authorization: `Bearer ${token}`, "X-Autonom-Origin": "agent" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${origin}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
    });
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("json")) {
      const bytes = (await response.arrayBuffer()).byteLength;
      return { status: response.status, body: {}, bytes, contentType };
    }
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    return { status: response.status, body: payload ?? {} };
  };
  const canvas = {
    url: found.url, port: found.port, origin, token, running, exited, request,
    stop: async () => {
      const stopped = await autonom(["canvas", "stop", "--port", String(found.port)], { timeout: 60_000 });
      const code = await Promise.race([exited, sleep(20_000).then(() => "timeout")]);
      if (code === "timeout" && running()) {
        child.kill("SIGTERM");
        await Promise.race([exited, sleep(8000)]);
      }
      state.canvases.delete(canvas);
      return { ok: stopped.code === 0 && !running(), stop_code: stopped.code };
    },
  };
  state.canvases.add(canvas);
  return canvas;
}

async function workspaceOf(canvas) {
  return (await canvas.request("GET", "/api/workspace")).body;
}

async function summaryOf(canvas, id) {
  const devices = (await canvas.request("GET", "/api/devices")).body?.devices ?? [];
  return devices.find((item) => item.id === id) ?? null;
}

async function attach(canvas, device, body = {}) {
  guard(device.platform, device.target);
  return await canvas.request("POST", "/api/devices", { platform: device.platform, target: device.target, ...body });
}

async function waitLive(canvas, id, timeoutMs = ATTACH_WAIT_MS) {
  return await waitFor(async () => {
    const summary = await summaryOf(canvas, id);
    return summary?.state === "live" ? summary : null;
  }, timeoutMs, 1000);
}

/** waitLive, then until the device's session start has answered (restored devices turn live first). */
async function waitSettled(canvas, id, timeoutMs = ATTACH_WAIT_MS) {
  return await waitFor(async () => {
    const summary = await summaryOf(canvas, id);
    return sessionSettled(summary) ? summary : null;
  }, timeoutMs, 1000);
}

async function activity(canvas, device) {
  const answer = await canvas.request("GET", `/api/activity?limit=500&device=${encodeURIComponent(device)}`);
  return answer.body?.entries ?? [];
}

/**
 * One viewer of `/d/<id>/ws/video`. `bytes` counts encoded video only (binary PACKET
 * messages); session and config headers and JSON state messages are counted apart, so a
 * state message alone never passes for a stream.
 */
function videoViewer(canvas, id) {
  const socket = new WebSocket(`ws://127.0.0.1:${canvas.port}${devicePath(id)}/ws/video?token=${encodeURIComponent(canvas.token)}`);
  socket.binaryType = "arraybuffer";
  const viewer = { bytes: 0, packets: 0, key_frames: 0, other_bytes: 0, state: null, open: false, closed: false, socket };
  socket.addEventListener("open", () => { viewer.open = true; });
  socket.addEventListener("message", (event) => {
    const counted = countVideoMessage(event.data);
    viewer.bytes += counted.bytes;
    viewer.packets += counted.packet ? 1 : 0;
    viewer.key_frames += counted.keyFrame ? 1 : 0;
    viewer.other_bytes += counted.other;
    // The latest state message: on Android it carries the in-use stream max_size.
    if (typeof event.data === "string") {
      try {
        const parsed = JSON.parse(event.data);
        if (parsed?.t === "state") viewer.state = parsed;
      } catch { /* not JSON */ }
    }
  });
  socket.addEventListener("close", () => { viewer.closed = true; });
  socket.addEventListener("error", () => { viewer.closed = true; });
  viewer.close = () => { try { socket.close(); } catch { /* closed */ } };
  return viewer;
}

/** The device's control socket as an agent; `send` is a no-op once it is closed. */
async function controlFor(canvas, id) {
  const ws = new WebSocket(`ws://127.0.0.1:${canvas.port}${devicePath(id)}/ws/control?token=${encodeURIComponent(canvas.token)}&origin=agent`);
  const opened = await Promise.race([
    new Promise((resolvePromise) => {
      ws.addEventListener("open", () => resolvePromise(true));
      ws.addEventListener("error", () => resolvePromise(false));
    }),
    sleep(10_000).then(() => false),
  ]);
  return {
    opened,
    send: (message) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)); },
    close: () => { try { ws.close(); } catch { /* closed */ } },
  };
}

async function bytesWithin(canvas, id, windowMs) {
  const viewer = videoViewer(canvas, id);
  await sleep(windowMs);
  viewer.close();
  return viewer.bytes;
}

async function sessionShow(sessionId) {
  const shown = await autonom(["--session-id", sessionId, "session", "show"]);
  return shown.json?.session ?? shown.json ?? null;
}

async function journalCount(sessionId) {
  const read = await autonom(["--session-id", sessionId, "journal", "--max", "5000"]);
  return read.json?.total_matched ?? read.json?.count ?? null;
}

async function currentSessionId() {
  const shown = await autonom(["session", "show"]);
  return shown.json?.session?.session_id ?? shown.json?.session_id ?? null;
}

// --- base case ------------------------------------------------------------------------------

const ANDROID = deviceId(DEVICES.primary);
const IOS = deviceId(DEVICES.fastIos);
let baseCanvas = null;

async function basePhase(section) {
  if (!(await androidOnline(DEVICES.primary))) throw new Error(`${DEVICES.primary.target} is not online`);
  await ensureSimulator(DEVICES.fastIos);
  const started = await autonom([...targetFlags(DEVICES.primary), "session", "start"]);
  state.primarySession = started.json?.session?.session_id ?? null;
  if (!step(section, "primary session started", started.code === 0 && state.primarySession,
    { error_code: started.error?.error_code ?? null })) return;

  baseCanvas = await startCanvas();
  const first = await workspaceOf(baseCanvas);
  step(section, "workspace Canvas with no device", first.mode === "workspace" && (first.devices ?? []).length === 0,
    { tabs: first.tabs?.length ?? null });
  const tab1 = first.active_tab;
  const layout = await baseCanvas.request("POST", `/api/tabs/${tab1}`, { layout: 2 });
  step(section, "tab 1 layout 2", layout.status === 200 && layout.body?.tab?.layout === 2, { http: layout.status });

  const viaCli = await autonom(["canvas", "attach", "--port", String(baseCanvas.port), ...targetFlags(DEVICES.primary), "--tab", tab1]);
  const androidSession = viaCli.json?.device?.session ?? null;
  step(section, "attach emulator-5580 reuses the primary session", viaCli.code === 0 && androidSession?.reused === true &&
    androidSession?.started_by_canvas === false && androidSession?.id === state.primarySession,
  { code: viaCli.code, session: androidSession, error_code: viaCli.error?.error_code ?? null });
  const iosAttach = await attach(baseCanvas, DEVICES.fastIos, { tab: tab1 });
  const iosSession = iosAttach.body?.device?.session ?? null;
  state.iosSession = iosSession?.id ?? null;
  step(section, "attach Autonom-Fast-Test starts its own session", iosAttach.status === 201 &&
    iosSession?.started_by_canvas === true && iosSession?.reused === false && Boolean(iosSession?.id),
  { http: iosAttach.status, error_code: iosAttach.body?.error_code ?? null });
  const androidLive = await waitLive(baseCanvas, ANDROID);
  const iosLive = await waitLive(baseCanvas, IOS);
  step(section, "both devices live in tab 1", androidLive?.tab === tab1 && iosLive?.tab === tab1,
    { android: androidLive?.state ?? null, ios: iosLive?.state ?? null });
  step(section, "current.json still names the primary", (await currentSessionId()) === state.primarySession);

  const streams = await watchStreams(baseCanvas, [DEVICES.primary, DEVICES.fastIos]);
  step(section, "both tiles stream", streams.every((row) => row.bytes > 0 && row.packets > 0 && !row.viewer_closed),
    { limit_ms: IOS_STREAM_LIMIT_MS, devices: streams, host_load: hostLoad() });

  // Input reaches only the focused device.
  await baseCanvas.request("POST", "/api/focus", { id: ANDROID });
  const journalsBefore = { android: await journalCount(state.primarySession), ios: state.iosSession ? await journalCount(state.iosSession) : null };
  const beforeA = countEntries(await activity(baseCanvas, ANDROID), ANDROID, "tap");
  const beforeI = countEntries(await activity(baseCanvas, IOS), IOS, "tap");
  const tap = await baseCanvas.request("POST", `${devicePath(ANDROID)}/tap`, { x: 10, y: 10 });
  await sleep(1500);
  const afterA = countEntries(await activity(baseCanvas, ANDROID), ANDROID, "tap");
  const afterI = countEntries(await activity(baseCanvas, IOS), IOS, "tap");
  step(section, "a tap reaches only the focused device", tap.status === 200 && afterA === beforeA + 1 && afterI === beforeI,
    { http: tap.status, android: [beforeA, afterA], ios: [beforeI, afterI] });
  const journalsAfter = { android: await journalCount(state.primarySession), ios: state.iosSession ? await journalCount(state.iosSession) : null };
  step(section, "each device journals into its own session",
    journalsAfter.android > journalsBefore.android && journalsAfter.ios === journalsBefore.ios,
    { before: journalsBefore, after: journalsAfter });

  await focusSwitchStep(section);

  await networkOnOneDevice(section);
  await parityOnPrimary(section);
  if (args.ui) await phase("ui", () => uiPhase(baseCanvas));

  // Tab 2 and "Move here".
  const tab2 = (await baseCanvas.request("POST", "/api/tabs", { name: "Two" })).body?.tab?.id;
  const moved = await baseCanvas.request("POST", `/api/devices/${encodeURIComponent(IOS)}/move`, { tab: tab2 });
  const iosAfterMove = await summaryOf(baseCanvas, IOS);
  step(section, "Move here takes the iOS device to tab 2 with its session", moved.status === 200 && iosAfterMove?.tab === tab2 &&
    iosAfterMove?.session?.id === state.iosSession, { http: moved.status });

  // Killing scrcpy-server on emulator-5580: iOS keeps streaming, Android recovers.
  // Precondition: the iOS viewer receives video before the kill. On a heavily loaded host the
  // idb stream can fail to restart (the Canvas then falls back to screenshots for good and
  // refuses the video socket); that is recorded and the device reconnected once, so the kill
  // below is measured against a running iOS stream.
  const androidViewer = videoViewer(baseCanvas, ANDROID);
  let iosViewer = videoViewer(baseCanvas, IOS);
  let iosBefore = Boolean(await waitFor(async () => iosViewer.bytes > 0, IOS_STREAM_LIMIT_MS, 250));
  let iosFallback = null;
  let iosReconnected = false;
  if (!iosBefore) {
    iosViewer.close();
    const fallback = (await activity(baseCanvas, IOS)).filter((entry) => entry?.kind === "stream" && entry?.name === "fallback").at(-1);
    iosFallback = { transport: (await summaryOf(baseCanvas, IOS))?.transport ?? null, error: fallback ? errorText(fallback.error, 200) : null,
      host_load: hostLoad() };
    const reconnect = await baseCanvas.request("POST", `/api/devices/${encodeURIComponent(IOS)}/reconnect`, {});
    iosReconnected = reconnect.status === 202 && Boolean(await waitLive(baseCanvas, IOS));
    iosViewer = videoViewer(baseCanvas, IOS);
    iosBefore = Boolean(await waitFor(async () => iosViewer.bytes > 0, IOS_STREAM_LIMIT_MS, 250));
  }
  await sleep(2000);
  const transport = (await summaryOf(baseCanvas, ANDROID))?.transport ?? null;
  const iosControl = await controlFor(baseCanvas, IOS);
  const killed = await adb(DEVICES.primary.target, ["shell", "pkill", "-f", "com.genymobile.scrcpy"]);
  const killedAt = Date.now();
  const iosMark = iosViewer.bytes;
  const loadAtKill = hostLoad();
  // The same iOS viewer, opened before the kill, must keep receiving video afterwards: wait
  // (bounded) for it to grow, and it must not have been closed meanwhile. A static Simulator
  // screen sends few frames, so after IOS_QUIET_MS without any, the viewer asks the iOS stream
  // for a key frame (every IOS_QUIET_MS); the bytes still have to come from that stream.
  let keyframeRequests = 0;
  let askedAt = killedAt;
  const iosKept = Boolean(await waitFor(async () => {
    if (iosViewer.closed) return false;
    if (iosViewer.bytes > iosMark) return true;
    if (keyframeDue(Date.now() - killedAt, Date.now() - askedAt, keyframeRequests)) {
      iosControl.send({ t: "system", op: "keyframe" });
      keyframeRequests += 1;
      askedAt = Date.now();
    }
    return false;
  }, IOS_STREAM_LIMIT_MS, 250)) && !iosViewer.closed;
  const iosAfterMs = Date.now() - killedAt;
  iosControl.close();
  const iosStillLive = (await summaryOf(baseCanvas, IOS))?.state === "live";
  const recovered = await waitFor(async () => {
    const summary = await summaryOf(baseCanvas, ANDROID);
    if (summary?.state !== "live") return null;
    return (await bytesWithin(baseCanvas, ANDROID, 2000)) > 0 ? summary : null;
  }, RECOVER_LIMIT_MS, 1000);
  androidViewer.close();
  iosViewer.close();
  step(section, "scrcpy-server killed: iOS streams on, Android recovers",
    transport === "scrcpy" && iosBefore && iosKept && iosStillLive && Boolean(recovered),
    { transport, pkill_code: killed.code, ios_streamed_before_kill: iosBefore, ios_fallback_before_kill: iosFallback,
      ios_reconnected: iosReconnected, ios_streamed: iosKept, ios_live: iosStillLive,
      ios_bytes_before_kill: iosMark, ios_bytes_after_kill: iosViewer.bytes - iosMark,
      ios_first_bytes_ms: iosKept ? iosAfterMs : null, ios_control_open: iosControl.opened,
      ios_keyframe_requests: keyframeRequests,
      android_recovered: Boolean(recovered), host_load: loadAtKill });

  // Restart while both devices are attached: tabs come back and both attach again.
  const beforeRestart = await workspaceOf(baseCanvas);
  const stopped = await baseCanvas.stop();
  step(section, "Canvas stopped for the restart", stopped.ok, stopped);
  baseCanvas = await startCanvas();
  // Restored devices turn live before their session starts answer: wait for both answers.
  const restoredAndroid = await waitSettled(baseCanvas, ANDROID);
  const restoredIos = await waitSettled(baseCanvas, IOS);
  const afterRestart = await workspaceOf(baseCanvas);
  step(section, "restart restores both tabs and attaches both devices", sameTabs(beforeRestart, afterRestart) &&
    restoredAndroid?.tab === beforeRestart.tabs?.[0]?.id && restoredIos?.tab === tab2,
  { tabs: afterRestart.tabs?.length ?? null, android: restoredAndroid?.state ?? null, ios: restoredIos?.state ?? null });
  state.iosSession = restoredIos?.session?.id ?? null;
  step(section, "after the restart the primary is reused again", restoredAndroid?.session?.id === state.primarySession,
    { reused: restoredAndroid?.session?.reused ?? null });
  const iosRestoredSession = state.iosSession ? await sessionShow(state.iosSession) : null;
  step(section, "after the restart iOS has a live Canvas-started session",
    restoredIos?.session?.started_by_canvas === true && Boolean(iosRestoredSession) && !iosRestoredSession.stopped_at,
    { session: restoredIos?.session ?? null, session_error: restoredIos?.session_error?.error_code ?? null });

  // Detach both: only the Canvas-started session ends; the Canvas keeps serving.
  const detachA = await baseCanvas.request("POST", `/api/devices/${encodeURIComponent(ANDROID)}/detach`, {});
  const viaCliDetach = await autonom(["canvas", "detach", "--port", String(baseCanvas.port), "--device-id", IOS]);
  step(section, "detach both", detachA.status === 200 && detachA.body?.session_stopped === false &&
    viaCliDetach.code === 0 && viaCliDetach.json?.session_stopped === true,
  { android: detachA.body?.session_stopped ?? null, ios: viaCliDetach.json?.session_stopped ?? null,
    android_http: detachA.status, ios_code: viaCliDetach.code, ios_error_code: viaCliDetach.error?.error_code ?? null,
    ios_stderr: viaCliDetach.code === 0 ? null : errorText(viaCliDetach.stderr, 300) });
  const primary = await sessionShow(state.primarySession);
  const iosStopped = state.iosSession ? await sessionShow(state.iosSession) : null;
  step(section, "primary session still live, Canvas-started session stopped",
    primary && !primary.stopped_at && iosStopped && Boolean(iosStopped.stopped_at),
    { primary_found: Boolean(primary), primary_stopped: Boolean(primary?.stopped_at),
      ios_found: Boolean(iosStopped), ios_stopped: Boolean(iosStopped?.stopped_at) });
  const serving = await workspaceOf(baseCanvas);
  step(section, "Canvas keeps serving with no device", serving.ok === true && (serving.devices ?? []).length === 0);
  const listed = await autonom(["canvas", "list"]);
  step(section, "canvas list names it without a token", listed.code === 0 &&
    (listed.json?.canvases ?? []).some((item) => item.port === baseCanvas.port) && !listed.stdout.includes(baseCanvas.token));
  const finalStop = await baseCanvas.stop();
  step(section, "autonom canvas stop", finalStop.ok, finalStop);
  step(section, "discovery file removed", !existsSync(join(autonomHome, "canvas", `${baseCanvas.port}.json`)));
  baseCanvas = null;
}

/**
 * A focus switch changes the APPLIED stream profiles within 5 s (CR-2): viewers keep both
 * streams running so the switch really restarts them; the step is judged on profile_applied
 * (set only once a restart succeeded), never on the planned profile, and on the Android
 * stream's in-use max_size from its /status. The elapsed time runs from the switch to the later
 * applied_at; the 5000 ms oracle reading is recorded, up to 5250 ms is accepted.
 */
async function focusSwitchStep(section) {
  const viewers = [videoViewer(baseCanvas, ANDROID), videoViewer(baseCanvas, IOS)];
  await waitFor(async () => viewers.every((viewer) => viewer.bytes > 0), IOS_STREAM_LIMIT_MS, 250);
  const streamingBefore = viewers.map((viewer) => viewer.bytes > 0);
  const before = await Promise.all([summaryOf(baseCanvas, ANDROID), summaryOf(baseCanvas, IOS)]);
  const switched = Date.now();
  await baseCanvas.request("POST", "/api/focus", { id: IOS });
  let androidState = null;
  let last = null;
  const profiles = await waitFor(async () => {
    const [android, ios] = await Promise.all([summaryOf(baseCanvas, ANDROID), summaryOf(baseCanvas, IOS)]);
    last = { android, ios };
    if (!appliedMatches(android, false, switched) || !appliedMatches(ios, true, 0)) return null;
    // The Android stream itself: the max_size its running scrcpy session was asked for, from
    // the state message the stream's viewer gets (a /status poll would ask the device each time).
    androidState = viewers[0].state;
    const inUse = androidState?.max_size;
    return Number.isFinite(inUse) && inUse > 0 && inUse <= 1024 ? { android, ios } : null;
  }, PROFILE_LIMIT_MS + 5000, 250);
  const observedMs = Date.now() - switched;
  for (const viewer of viewers) viewer.close();
  // /status of the Android stream once, for the record: what scrcpy was asked for and why.
  const androidStatus = await baseCanvas.request("GET", `${devicePath(ANDROID)}/status`, undefined, { timeoutMs: 30_000 })
    .then((answer) => answer.body ?? null, () => null);
  // On a miss: what was seen last, and the profile restarts the activity log holds.
  const profileLog = async (id) => (await activity(baseCanvas, id))
    .filter((entry) => entry?.kind === "stream" && (entry?.name === "profile" || entry?.name === "start"))
    .slice(-4).map((entry) => ({ name: entry.name, ok: entry.ok ?? null, at: entry.at ?? entry.ts ?? null,
      duration_ms: entry.duration_ms ?? null, summary: entry.summary ?? null, error: entry.error ? errorText(entry.error, 160) : null }));
  const missed = profiles ? null : {
    android: { wanted: last?.android?.profile ?? null, applied: last?.android?.profile_applied ?? null,
      transport: last?.android?.transport ?? null, stream_max_size: androidState?.max_size ?? null,
      status_max_size: androidStatus?.scrcpy?.max_size ?? null, log: await profileLog(ANDROID) },
    ios: { wanted: last?.ios?.profile ?? null, applied: last?.ios?.profile_applied ?? null,
      transport: last?.ios?.transport ?? null, log: await profileLog(IOS) },
  };
  const ios = profiles?.ios ?? null;
  // The iOS device may already run the focused profile (it never left it): then nothing restarts
  // and only the Android demotion counts for the elapsed time.
  const restartedIos = ios && Date.parse(ios.profile_applied.applied_at) >= switched;
  const elapsed = profiles ? appliedElapsed(restartedIos ? [profiles.android, ios] : [profiles.android], switched) : null;
  const applied = (summary) => summary?.profile_applied ?? null;
  step(section, "focus switch changes the applied fps and size within 5 s",
    Boolean(profiles) && elapsed !== null && elapsed <= PROFILE_LIMIT_MS + 250,
    { oracle_ms: PROFILE_LIMIT_MS, elapsed_ms: elapsed, observed_ms: observedMs, within_oracle: elapsed !== null && elapsed <= PROFILE_LIMIT_MS,
      streaming_before: streamingBefore,
      before: { android: applied(before[0]), ios: applied(before[1]) },
      android: profiles ? { ...applied(profiles.android), wanted_profile: profiles.android.profile,
        stream_max_size: androidState?.max_size ?? null, status_max_size: androidStatus?.scrcpy?.max_size ?? null,
        stream_max_size_source: androidStatus?.scrcpy?.max_size_source ?? null, transport: profiles.android.transport } : null,
      ios: ios ? { ...applied(ios), wanted_profile: ios.profile, restarted: Boolean(restartedIos), transport: ios.transport } : null,
      missed, host_load: hostLoad() });
}

/** Every regular file below `directory` with its size (missing directory: none). */
async function filesBelow(directory) {
  const found = [];
  let names = [];
  try { names = await readdir(directory, { withFileTypes: true }); } catch { return found; }
  for (const item of names) {
    const path = join(directory, item.name);
    if (item.isDirectory()) found.push(...(await filesBelow(path)));
    else if (item.isFile()) found.push({ path, size: (await stat(path)).size, mtimeMs: (await stat(path)).mtimeMs });
  }
  return found;
}

/**
 * Tools parity on the primary through the Canvas API (contract 6.5): a screenshot and a ~3 s
 * recording land in this run's temporary captures folder (never the real ~/Downloads), an app
 * launches and a URL opens, the activity log holds these ops with durations and results, and
 * /api/health reports adb and xcrun_simctl ok.
 */
async function parityOnPrimary(section) {
  const call = (op, payload = {}) => baseCanvas.request("POST", `${devicePath(ANDROID)}/tools/call`, { op, payload });
  const downloads = join(homedir(), "Downloads", "Autonom");
  const since = Date.now();
  const captureFile = async (name) => (await filesBelow(capturesDir)).find((file) => file.path.endsWith(`/${name}`)) ?? null;

  const shot = await call("capture.screenshot");
  const shotName = shot.body?.result?.capture?.name ?? null;
  const shotFile = shotName ? await captureFile(shotName) : null;
  step(section, "screenshot lands in the temporary captures folder", shot.status === 200 && captureOk(shotFile, capturesDir, "png"),
    { http: shot.status, error_code: shot.body?.error_code ?? null, size: shotFile?.size ?? null });

  const started = await call("record.start");
  await sleep(RECORD_MS);
  const stopped = await call("record.stop");
  const clipName = stopped.body?.result?.capture?.name ?? null;
  const clipFile = clipName ? await captureFile(clipName) : null;
  const durationMs = stopped.body?.result?.duration_ms ?? null;
  step(section, "a ~3 s recording lands in the temporary captures folder", started.status === 200 && stopped.status === 200 &&
    captureOk(clipFile, capturesDir, "mp4") && Number.isFinite(durationMs) && durationMs >= 1000,
  { http: [started.status, stopped.status], error_code: stopped.body?.error_code ?? started.body?.error_code ?? null,
    size: clipFile?.size ?? null, duration_ms: durationMs });

  state.primaryForeground = true;
  const launched = await call("app.launch", { app_id: LAUNCH_APP });
  step(section, "an app launches through the Canvas", launched.status === 200 && launched.body?.result?.launched === LAUNCH_APP,
    { http: launched.status, error_code: launched.body?.error_code ?? null });
  const opened = await call("app.open_url", { url: OPEN_URL });
  step(section, "a URL opens through the Canvas", opened.status === 200 && opened.body?.result?.opened === true,
    { http: opened.status, error_code: opened.body?.error_code ?? null, handled_by: opened.body?.result?.handled_by ?? null });

  const rows = opEntries(await activity(baseCanvas, ANDROID), ANDROID, PARITY_OPS);
  step(section, "activity log holds these ops with durations and results", opEntriesOk(rows), { entries: rows });
  const health = await baseCanvas.request("GET", "/api/health");
  const tools = healthOf(health.body);
  step(section, "health reports adb and xcrun_simctl ok", health.status === 200 &&
    HEALTH_TOOLS.every((name) => tools[name]?.ok === true), { http: health.status, tools });

  const touched = (await filesBelow(downloads)).filter((file) => file.mtimeMs >= since).length;
  step(section, "the real ~/Downloads/Autonom is not written", touched === 0, { new_files: touched });
}

async function networkOnOneDevice(section) {
  const call = (id, op, payload = {}) => baseCanvas.request("POST", `${devicePath(id)}/tools/call`, { op, payload });
  const before = (await adb(DEVICES.primary.target, ["shell", "settings", "get", "global", "http_proxy"])).stdout.trim();
  const started = await call(ANDROID, "network.start", { acknowledged: true });
  state.networkStarted = started.status === 200;
  const attached = await call(ANDROID, "network.attach", { acknowledged: true });
  state.networkAttached = attached.status === 200;
  const now = (await adb(DEVICES.primary.target, ["shell", "settings", "get", "global", "http_proxy"])).stdout.trim();
  const iosStatus = await call(IOS, "network.status");
  const iosProxy = iosStatus.body?.result?.proxy ?? null;
  step(section, "network attach changes only the Android device",
    state.networkAttached && /^10\.0\.2\.2:\d+$/.test(now) && iosStatus.status === 200 && iosProxy?.running !== true,
    { android_http_proxy_set: /^10\.0\.2\.2:\d+$/.test(now), ios_proxy_running: iosProxy?.running ?? null });
  await networkOff();
  const after = (await adb(DEVICES.primary.target, ["shell", "settings", "get", "global", "http_proxy"])).stdout.trim();
  step(section, "network detached and stopped", !state.networkAttached && !state.networkStarted && after === before);
}

async function networkOff() {
  if (!baseCanvas) return;
  const call = (op) => baseCanvas.request("POST", `${devicePath(ANDROID)}/tools/call`, { op, payload: {} });
  if (state.networkAttached && (await call("network.detach")).status === 200) state.networkAttached = false;
  if (state.networkStarted && (await call("network.stop")).status === 200) state.networkStarted = false;
}

// --- --ui -----------------------------------------------------------------------------------

async function loadPlaywright() {
  const entry = join(playwrightRoot, "node_modules/playwright/index.mjs");
  if (!existsSync(entry)) throw new Error(`Playwright not found at ${entry}; set PLAYWRIGHT_ROOT`);
  return await import(pathToFileURL(entry).href);
}

async function uiPhase(canvas) {
  const section = "ui";
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  // Both devices live and present in their slots before any page is measured: a page shows
  // a slot that is not present as "not running" and never loads its tile.
  const ready = await waitFor(async () => {
    const [android, ios] = [await summaryOf(canvas, ANDROID), await summaryOf(canvas, IOS)];
    const slots = ((await workspaceOf(canvas))?.tabs ?? []).flatMap((tab) => tab.slots ?? []).filter(Boolean);
    const present = (id) => slots.some((slot) => slot.device === id && slot.present === true);
    return android?.state === "live" && ios?.state === "live" && present(ANDROID) && present(IOS) ? true : null;
  }, ATTACH_WAIT_MS, 500);
  step(section, "both devices live and present before the pages", Boolean(ready), { host_load: hostLoad() });
  const workspace = await workspaceOf(canvas);
  const tab1 = workspace.active_tab;
  try {
    for (const viewport of VIEWPORTS) {
      for (const scheme of SCHEMES) {
        const label = `${viewport.width}x${viewport.height}-${scheme}`;
        const context = await browser.newContext({ viewport, colorScheme: scheme });
        const page = await context.newPage();
        try {
          await page.goto(canvas.url);
          await page.waitForFunction(() => document.querySelectorAll("#ws-tabs [role=tab]").length > 0, null, { timeout: 20_000 });
          await page.waitForFunction(() => document.querySelectorAll("iframe.tile-frame").length === 2, null, { timeout: 30_000 });
          await page.waitForTimeout(2000);
          const layout = await page.evaluate(() => ({
            overflow: document.documentElement.scrollWidth > window.innerWidth,
            tabs: document.querySelectorAll("#ws-tabs [role=tab]").length,
            tabbar_visible: document.getElementById("ws-tabs").getBoundingClientRect().height > 0,
            tiles: document.querySelectorAll("#ws-grid section.tile").length,
          }));
          await page.screenshot({ path: join(outDir, `split-${label}-tiles.png`) });
          step(section, `${label}: tab bar and two tiles, no sideways scroll`,
            !layout.overflow && layout.tabbar_visible && layout.tabs >= 1 && layout.tiles === 2, layout);

          // A tap made through the focused tile's page reaches only that device.
          const frames = page.frames().filter((frame) => frame.url().includes("/d/"));
          const androidFrame = frames.find((frame) => decodeURIComponent(frame.url()).includes(ANDROID));
          await page.locator("#ws-grid section.tile").first().dispatchEvent("pointerdown");
          const beforeA = countEntries(await activity(canvas, ANDROID), ANDROID, "tap");
          const beforeI = countEntries(await activity(canvas, IOS), IOS, "tap");
          const tapped = androidFrame
            ? await Promise.race([
              androidFrame.evaluate(async () => { try { await post("/tap", { x: 10, y: 10 }); return true; } catch { return false; } }),
              sleep(15_000).then(() => false)])
            : false;
          await page.waitForTimeout(1500);
          const afterA = countEntries(await activity(canvas, ANDROID), ANDROID, "tap");
          const afterI = countEntries(await activity(canvas, IOS), IOS, "tap");
          step(section, `${label}: a tap in the focused tile reaches only that device`,
            tapped && afterA === beforeA + 1 && afterI === beforeI, { android: [beforeA, afterA], ios: [beforeI, afterI] });

          // The Actions drawer of the focused tile.
          await page.locator("#ws-grid section.tile").first().getByRole("button", { name: "Actions" }).click();
          const drawerOpen = androidFrame
            ? await androidFrame.waitForFunction(() => !document.getElementById("actions").hidden, null, { timeout: 10_000 })
              .then(() => true, () => false)
            : false;
          const drawerOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
          await page.screenshot({ path: join(outDir, `split-${label}-actions.png`) });
          step(section, `${label}: Actions drawer opens`, drawerOpen && !drawerOverflow, { overflow: drawerOverflow });
          await page.locator("#ws-grid section.tile").first().getByRole("button", { name: "Actions" }).click();

          // The empty-tile picker of a new tab, then back to tab 1.
          await page.click("#ws-new-tab");
          const picker = await page.waitForSelector("#ws-grid .picker", { timeout: 10_000 }).then(() => true, () => false);
          const pickerOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
          await page.screenshot({ path: join(outDir, `split-${label}-picker.png`) });
          step(section, `${label}: a new tab shows the picker`, picker && !pickerOverflow, { overflow: pickerOverflow });
          const extra = (await workspaceOf(canvas)).tabs?.filter((tab) => tab.id !== tab1 && !(tab.slots ?? []).some(Boolean)) ?? [];
          for (const tab of extra) await canvas.request("POST", `/api/tabs/${tab.id}/close`, {});
          await canvas.request("POST", `/api/tabs/${tab1}/activate`, {});
        } catch (error) {
          // What the page showed when it failed: a screenshot and the tile states.
          const seen = await page.evaluate(() => ({
            tabs: document.querySelectorAll("#ws-tabs [role=tab]").length,
            tiles: [...document.querySelectorAll("#ws-grid section.tile")].map((tile) => tile.textContent.replace(/\s+/g, " ").slice(0, 120)),
            frames: document.querySelectorAll("iframe.tile-frame").length,
            path: location.pathname,
          })).catch(() => null);
          await page.screenshot({ path: join(outDir, `split-${label}-error.png`) }).catch(() => {});
          const devices = (await canvas.request("GET", "/api/devices").catch(() => null))?.body?.devices ?? [];
          step(section, `${label}: page error`, false, { error: errorText(error, 300), page: seen,
            devices: devices.map((item) => ({ id: item.id, state: item.state, tab: item.tab })), host_load: hostLoad() });
        } finally {
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
}

// --- --four ---------------------------------------------------------------------------------

async function fourPhase(section) {
  const four = [DEVICES.primary, DEVICES.fastIos, DEVICES.splitAndroid, DEVICES.splitIos];
  if (!(await androidOnline(DEVICES.primary))) throw new Error(`${DEVICES.primary.target} is not online`);
  await ensureSimulator(DEVICES.fastIos);
  step(section, "Autonom_Split_API36 on emulator-5584 running", await ensureEmulator(DEVICES.splitAndroid).then(() => true));
  step(section, "Autonom-Split-Test booted", await ensureSimulator(DEVICES.splitIos).then(() => true));
  const canvas = await startCanvas({ workspace: "four", extra: ["--ephemeral", "--bootable", `avd:${DEVICES.bootAndroid.avd}@${DEVICES.bootAndroid.port}`] });
  try {
    const tab = (await workspaceOf(canvas)).active_tab;
    await canvas.request("POST", `/api/tabs/${tab}`, { layout: 4 });
    for (const device of four) {
      const answer = await attach(canvas, device, { tab });
      step(section, `attach ${device.name}`, answer.status === 201, { http: answer.status, error_code: answer.body?.error_code ?? null });
    }
    const live = await Promise.all(four.map((device) => waitLive(canvas, deviceId(device))));
    step(section, "four tiles live in one tab", live.every((summary) => summary?.tab === tab), { states: live.map((item) => item?.state ?? null) });
    await fourStreamsStep(section, canvas, four);
    const bootWasOnline = await androidOnline(DEVICES.bootAndroid);
    // The 5th device is offered only to a full tab: a tab with room would take it (and a boot
    // probe would really boot an emulator).
    if (!live.every((summary) => summary?.tab === tab)) {
      step(section, "a 5th device for that tab is refused 409 tab_full", false, { reason: "the tab is not full; not probed" });
      return;
    }
    const running = (await canvas.request("GET", "/api/targets")).body?.running ?? [];
    const probe = fifthProbe(running, four.map(deviceId));
    const fifth = probe.kind === "attach"
      ? await attach(canvas, { platform: probe.platform, target: probe.target }, { tab })
      : await canvas.request("POST", "/api/boot", { kind: "avd", name: probe.name, tab });
    step(section, "a 5th device for that tab is refused 409 tab_full", fifth.status === 409 && fifth.body?.error_code === "tab_full",
      { probe: probe.kind, http: fifth.status, error_code: fifth.body?.error_code ?? null });
    // A boot wrongly accepted for the 5th tile: wait for it to settle so the restore can shut it down.
    const jobId = probe.kind === "boot" ? fifth.body?.job?.id : null;
    if (jobId) {
      await waitFor(async () => {
        const job = (await canvas.request("GET", `/api/boot/${jobId}`)).body?.job;
        return job && (job.state === "done" || job.state === "failed");
      }, BOOT_WAIT_MS, 2000);
    }
    if (!bootWasOnline && await androidOnline(DEVICES.bootAndroid)) state.emulatorsBooted.push(DEVICES.bootAndroid);
  } finally {
    const stopped = await canvas.stop();
    step(section, "Canvas stopped", stopped.ok, stopped);
  }
}

/**
 * CR-1: a video viewer for all four devices at the same time; each must receive video bytes
 * within IOS_STREAM_LIMIT_MS over scrcpy or idb (a screenshot fallback fails). Quiet iOS
 * viewers ask their stream for a key frame every IOS_QUIET_MS. Records per device the
 * transport, bytes, time to the first bytes and fallbacks, and the host load.
 */
async function fourStreamsStep(section, canvas, four) {
  const loadBefore = hostLoad();
  const rows = await watchStreams(canvas, four);
  step(section, "four video streams at the same time", fourStreamsOk(rows),
    { limit_ms: IOS_STREAM_LIMIT_MS, devices: rows, host_load_before: loadBefore, host_load: hostLoad() });
}

/**
 * A video viewer for every device in `devices` at the same time; waits (bounded by
 * IOS_STREAM_LIMIT_MS) until each received encoded video, a quiet viewer asking its stream for
 * a key frame every IOS_QUIET_MS, then keeps them open 3 s more. One row per device.
 */
async function watchStreams(canvas, four) {
  const ids = four.map(deviceId);
  const opened = Date.now();
  const viewers = ids.map((id) => videoViewer(canvas, id));
  const firstAt = ids.map(() => null);
  // A quiet viewer asks its stream for a key frame (iOS sends frames only on screen changes).
  const controls = await Promise.all(ids.map((id) => controlFor(canvas, id)));
  const asks = ids.map(() => ({ count: 0, at: opened }));
  await waitFor(async () => {
    const now = Date.now();
    viewers.forEach((viewer, index) => {
      if (viewer.bytes > 0 && firstAt[index] === null) firstAt[index] = now - opened;
      const control = controls[index];
      if (control?.opened && viewer.bytes === 0 && keyframeDue(now - opened, now - asks[index].at, asks[index].count)) {
        control.send({ t: "system", op: "keyframe" });
        asks[index].count += 1;
        asks[index].at = now;
      }
    });
    return viewers.every((viewer) => viewer.bytes > 0);
  }, IOS_STREAM_LIMIT_MS, 250);
  // All four keep streaming together a little longer, with every viewer still open.
  const marks = viewers.map((viewer) => viewer.bytes);
  await sleep(3000);
  const summaries = await Promise.all(ids.map((id) => summaryOf(canvas, id)));
  const rows = await Promise.all(ids.map(async (id, index) => {
    const fallback = (await activity(canvas, id)).filter((entry) => entry?.kind === "stream" && entry?.name === "fallback").at(-1);
    return {
      device: four[index].name, platform: four[index].platform, transport: summaries[index]?.transport ?? null,
      state: summaries[index]?.state ?? null, bytes: viewers[index].bytes, packets: viewers[index].packets,
      key_frames: viewers[index].key_frames, other_bytes: viewers[index].other_bytes,
      bytes_last_3s: viewers[index].bytes - marks[index],
      first_bytes_ms: firstAt[index], viewer_closed: viewers[index].closed, keyframe_requests: asks[index].count,
      fallback: fallback ? { error: errorText(fallback.error, 200) } : null,
    };
  }));
  for (const viewer of viewers) viewer.close();
  for (const control of controls) control?.close();
  return rows;
}

// --- --boot ---------------------------------------------------------------------------------

/** Android serials the Canvas sees running (GET /api/targets). */
async function runningEmulators(canvas) {
  const running = (await canvas.request("GET", "/api/targets")).body?.running ?? [];
  return running.filter((item) => item.platform === "android").map((item) => item.target).sort();
}

/**
 * An AVD that exists but was not passed with --bootable (Autonom_Split_API36) is refused
 * with 403 boot_not_allowed, no job starts and no new emulator serial appears.
 */
async function refusedBootStep(section, canvas) {
  const other = DEVICES.splitAndroid;
  const wasOnline = await androidOnline(other);
  const before = await runningEmulators(canvas);
  const refused = await canvas.request("POST", "/api/boot", { kind: "avd", name: other.avd });
  // Give a wrongly started boot time to show up before looking again.
  await sleep(5000);
  const after = await runningEmulators(canvas);
  const appeared = after.filter((serial) => !before.includes(serial));
  const nowOnline = await androidOnline(other);
  step(section, "a non-allowlisted AVD is refused",
    refused.status === 403 && refused.body?.error_code === "boot_not_allowed" && !refused.body?.job
      && appeared.length === 0 && nowOnline === wasOnline,
    { avd: other.avd, http: refused.status, error_code: refused.body?.error_code ?? null,
      job: refused.body?.job?.id ?? null, before, after, appeared, [`${other.target}_online`]: nowOnline });
}

async function bootPhase(section) {
  const device = DEVICES.bootAndroid;
  if (await androidOnline(device)) {
    step(section, `${device.target} is not running before the boot`, false, { reason: "already running; it would not count as booted by the Canvas" });
    return;
  }
  const canvas = await startCanvas({ workspace: "boot", extra: ["--ephemeral", "--bootable", `avd:${device.avd}@${device.port}`, "--shutdown-booted"] });
  let attached = false;
  try {
    const bootable = (await canvas.request("GET", "/api/targets")).body?.bootable ?? [];
    step(section, "the picker offers the AVD", bootable.some((item) => item.kind === "avd" && item.name === device.avd));
    await refusedBootStep(section, canvas);
    const started = await canvas.request("POST", "/api/boot", { kind: "avd", name: device.avd });
    const jobId = started.body?.job?.id;
    step(section, "boot job accepted", started.status === 202 && Boolean(jobId), { http: started.status });
    const job = jobId ? await waitFor(async () => {
      const answer = (await canvas.request("GET", `/api/boot/${jobId}`)).body?.job;
      return answer && (answer.state === "done" || answer.state === "failed") ? answer : null;
    }, BOOT_WAIT_MS, 2000) : null;
    const summary = job?.state === "done" ? await waitLive(canvas, deviceId(device)) : null;
    attached = Boolean(summary);
    step(section, "booted and attached", job?.state === "done" && summary?.booted_by_canvas === true,
      { state: job?.state ?? null, error_code: job?.error_code ?? null, target: job?.target ?? null });
  } finally {
    const stopped = await canvas.stop();
    step(section, "Canvas stopped", stopped.ok, stopped);
  }
  if (attached) {
    const gone = await waitFor(async () => !(await androidOnline(device)), 90_000, 2000);
    step(section, "the AVD it booted is shut down at Canvas stop", gone);
    if (!gone) state.emulatorsBooted.push(device);
  }
}

/** The session.json records of this run's temporary AUTONOM_HOME. */
async function canvasSessionRecords() {
  const root = join(autonomHome, "sessions");
  const records = [];
  let names = [];
  try { names = await readdir(root, { withFileTypes: true }); } catch { return records; }
  for (const item of names) {
    if (!item.isDirectory()) continue;
    try { records.push(JSON.parse(await readFile(join(root, item.name, "session.json"), "utf8"))); } catch { /* not a session */ }
  }
  return records;
}

// --- main -----------------------------------------------------------------------------------

try {
  await phase("base", basePhase);
  // A base case that stopped early leaves its Canvas (and its devices attached): end it first.
  if (baseCanvas) {
    await networkOff();
    await restoreStep("base Canvas stopped after an early end", () => baseCanvas.stop());
    baseCanvas = null;
  }
  if (args.ui && !report.sections.ui.steps.length) step("ui", "ran with the base case", false, { reason: "the base case stopped before the UI pass" });
  if (args.four) await phase("four", fourPhase);
  if (args.boot) await phase("boot", bootPhase);
} catch (error) {
  report.error = errorText(error, 500);
} finally {
  await restoreStep("network detached and stopped", async () => { await networkOff(); return !state.networkAttached && !state.networkStarted; });
  if (state.primaryForeground) {
    await restoreStep(`${DEVICES.primary.name} back on the home screen`, async () =>
      (await adb(DEVICES.primary.target, ["shell", "input", "keyevent", "KEYCODE_HOME"])).code === 0);
  }
  for (const canvas of [...state.canvases]) await restoreStep(`Canvas on port ${canvas.port} stopped`, () => canvas.stop());
  if (state.primarySession) {
    await restoreStep("primary session stopped", async () =>
      (await autonom(["--session-id", state.primarySession, "session", "stop"])).code === 0);
  }
  for (const device of state.emulatorsBooted) await restoreStep(`${device.name} shut down`, () => shutdownDevice(device));
  for (const device of state.simulatorsBooted) {
    await restoreStep(`${device.name} shut down again`, async () =>
      (await simctl(device.target, ["shutdown", device.target], { timeout: 120_000 })).code === 0);
  }
  report.restore.ok = report.restore.steps.every((entry) => entry.ok);
  report.ok = !report.error && phases.every((name) => report.sections[name].ok) && report.restore.ok;
  report.finished_at = new Date().toISOString();
  report.host_load_end = hostLoad();
  // CR-7: the Canvas-started sessions per platform, read before the temporary home goes.
  report.canvas_sessions = await canvasSessionRecords().then(canvasSessionCounts, (error) => ({ error: errorText(error, 200) }));
  const files = await writeReport();
  if (!args["keep-home"]) {
    await rm(autonomHome, { recursive: true, force: true });
    await rm(capturesDir, { recursive: true, force: true });
  }
  console.log(`${report.ok ? "ok" : "FAILED"}: ${files.join(", ")}`);
  process.exitCode = report.ok ? 0 : 1;
}
