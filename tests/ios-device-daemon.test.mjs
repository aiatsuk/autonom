// The iOS device daemon (ios-device-daemon.mjs) against a fake idb_companion, a fake idb
// client, a fake xcrun and a fake process list, each test in its own temporary state base.
// No real companion, simulator or idb client is used.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync,
} from "node:fs";
import { connect as netConnect } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";
import { isDeepStrictEqual } from "node:util";

import {
  ACT_REPLY_MARGIN_MS,
  CODE,
  COMPANION_STOP_MS,
  CONFIRM_MS,
  CONTAINER_AREA_SHARE,
  COVERAGE_THRESHOLD,
  DAEMON_SCRIPT,
  DEFAULT_HID_WAIT_MS,
  DEFAULT_IDLE_MS,
  DTUHIDD_FLAG,
  DaemonConnection,
  DaemonError,
  FIRST_TOUCH_CONFIRM_MS,
  HOST_LOAD_FACTOR,
  HOST_LOAD_FILE_ENV,
  HOST_LOAD_RECHECK_MS,
  MIN_STUCK_INTERVAL_MS,
  STUCK_BACKOFF_MAX_FACTOR,
  ARM_WAIT_SPARE_MS,
  QUIET_STUCK_MS,
  QUIET_STUCK_PROBE_MS,
  QUIET_STUCK_SIGNAL,
  STUCK_PROBE_FRESH_MS,
  STUCK_SILENT_PROBES,
  STUCK_INTERVAL_ENV,
  STUCK_RESTART_INTERVAL_MS,
  GRACE_MS,
  HID_UNCONFIRMED_HINT,
  HID_WAIT_ENV,
  HINT,
  INPUT_OPS,
  INPUT_REPLY_SLACK_MS,
  IOSD_SCHEMA,
  MAX_LINE_BYTES,
  MAX_SOCKET_PATH_BYTES,
  MIN_COMPANION_VERSION,
  MIN_HID_WAIT_MS,
  MIN_IDLE_MS,
  OBSERVE_TIMEOUT_MS,
  OPS,
  PRIMING_KEYCODE,
  QUIET_WARM_MS,
  QUIET_WINDOW_MS,
  RESTART_WAIT_MS,
  STAGE,
  STATUS_BAR_PT,
  WEB_CONTENT_APP_NAMES,
  WEB_CONTENT_BUNDLES,
  chooseReadBackend,
  companionVersionFrom,
  daemonPaths,
  deviceHubIn,
  ensureDaemon,
  frameCoverage,
  freshSilentRun,
  hidWaitLimit,
  hostLoad,
  idleLimit,
  inputDurationMs,
  inputWarning,
  liveCompanionEntry,
  parseNotifyValue,
  parseVersion,
  pidAlive,
  requestDaemon,
  socketPathFor,
  stuckRestartGap,
  stuckRestartInterval,
  validateInput,
  versionAtLeast,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-device-daemon.mjs";
import {
  HID_CLOSE_WAIT_MS, HID_IDLE_CLOSE_MS, IdbCompanionClient, STREAM_STOP_TIMEOUT_MS, encodeHidEvent, grpcFrame,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";
import * as http2 from "node:http2";

const COMPANION_MODULE = new URL(
  "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs",
  import.meta.url,
).href;
const UDID = "5D1D0C1E-7E57-4A11-9D00-00000000AA01";
const COMPANION_ENV_KEYS = ["PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "DEVELOPER_DIR"];
const SETTINGS = readFileSync(new URL("./fixtures/idb_describe_all_sample.json", import.meta.url), "utf8");

function element(type, label, frame, extra = {}) {
  return { type, AXLabel: label, frame, role: `AX${type}`, ...extra };
}
const SCREEN = { x: 0, y: 0, width: 402, height: 874 };
// Safari on example.com through `ax`: only Safari's own controls, none of the page.
const SAFARI_AX = JSON.stringify([
  element("Application", "Safari", SCREEN),
  element("Button", "Tabs", { x: 340, y: 800, width: 44, height: 44 }),
  element("TextField", "Address", { x: 60, y: 800, width: 270, height: 44 }),
  element("Button", "Back", { x: 10, y: 800, width: 44, height: 44 }),
]);
const SAFARI_AXBRIDGE = JSON.stringify([
  element("Application", "Safari", SCREEN),
  element("StaticText", "Example Domain", { x: 20, y: 140, width: 360, height: 40 }),
  element("Link", "More information...", { x: 20, y: 300, width: 200, height: 24 }),
]);

// ---------------------------------------------------------------------------
// Constants and pure rules
// ---------------------------------------------------------------------------

test("the design constants are pinned", () => {
  assert.equal(DEFAULT_IDLE_MS, 300_000);
  assert.equal(MIN_IDLE_MS, 1000);
  assert.equal(MAX_SOCKET_PATH_BYTES, 103);
  assert.equal(MAX_LINE_BYTES, 4 * 1024 * 1024);
  assert.deepEqual([...MIN_COMPANION_VERSION], [1, 6, 4]);
  assert.equal(CONFIRM_MS, 500);
  assert.equal(GRACE_MS, 300);
  assert.equal(FIRST_TOUCH_CONFIRM_MS, 1500);
  assert.equal(QUIET_STUCK_MS, 3000);
  assert.equal(QUIET_STUCK_SIGNAL, "RUN_LOOP_IDLE");
  assert.equal(STUCK_RESTART_INTERVAL_MS, 30_000);
  assert.equal(QUIET_STUCK_PROBE_MS, 2000);
  assert.equal(STUCK_PROBE_FRESH_MS, 3000);
  assert.equal(STUCK_SILENT_PROBES, 2);
  assert.equal(STUCK_BACKOFF_MAX_FACTOR, 20);
  assert.equal(HOST_LOAD_FACTOR, 6);
  assert.equal(HOST_LOAD_RECHECK_MS, 10_000);
  assert.equal(HOST_LOAD_FILE_ENV, "AUTONOM_IOSD_LOAD_FILE");
  assert.equal(QUIET_WARM_MS, 2000);
  assert.equal(QUIET_WINDOW_MS, 250);
  assert.equal(OBSERVE_TIMEOUT_MS, 3000);
  assert.equal(RESTART_WAIT_MS, 6000);
  assert.equal(COMPANION_STOP_MS, 3000);
  assert.equal(COVERAGE_THRESHOLD, 0.5);
  assert.equal(STATUS_BAR_PT, 60);
  assert.equal(CONTAINER_AREA_SHARE, 0.9);
  assert.deepEqual([...WEB_CONTENT_BUNDLES], ["com.apple.mobilesafari"]);
  assert.deepEqual([...WEB_CONTENT_APP_NAMES], ["Safari"]);
  assert.equal(PRIMING_KEYCODE, 225);
  assert.equal(DTUHIDD_FLAG, "com.apple.coredevice.dtuhidd.active");
  assert.equal(IOSD_SCHEMA, "autonom-iosd/v1");
  assert.deepEqual([...OPS], ["tree", "tap", "swipe", "button", "key", "quiet", "act", "companion", "attach", "detach", "status", "stop"]);
  assert.deepEqual([...INPUT_OPS], ["tap", "swipe", "button", "key"]);
  for (const code of [CODE.DISPLAY_NOT_LIT, CODE.INPUT_UNCONFIRMED, CODE.INPUT_BLOCKED, CODE.COMPANION_TOO_OLD]) assert.ok(HINT[code]);
  assert.equal(HID_WAIT_ENV, "AUTONOM_IOSD_HID_WAIT_MS");
  assert.equal(MIN_HID_WAIT_MS, 100);
  assert.match(HID_UNCONFIRMED_HINT, /did not confirm it/);
});

test("an input's deadline is DEFAULT_HID_WAIT_MS (15 s), set by AUTONOM_IOSD_HID_WAIT_MS, never below 100 ms", () => {
  assert.equal(DEFAULT_HID_WAIT_MS, 15_000);
  assert.ok(DEFAULT_HID_WAIT_MS < HID_CLOSE_WAIT_MS, "the daemon gives up well before the companion module's own 60 s limit");
  assert.equal(hidWaitLimit({}), 15_000);
  assert.equal(hidWaitLimit({ AUTONOM_IOSD_HID_WAIT_MS: "500" }), 500);
  assert.equal(hidWaitLimit({ AUTONOM_IOSD_HID_WAIT_MS: "5" }), 100);
  assert.equal(hidWaitLimit({ AUTONOM_IOSD_HID_WAIT_MS: "later" }), 15_000);
  assert.equal(hidWaitLimit({ AUTONOM_IOSD_HID_WAIT_MS: "" }), 15_000);
  assert.equal(inputDurationMs({ op: "tap", x: 1, y: 2 }), 0);
  assert.equal(inputDurationMs({ op: "tap", x: 1, y: 2, duration: 1.5 }), 1500);
  assert.equal(inputDurationMs({ op: "swipe", x1: 0, y1: 0, x2: 1, y2: 1, duration: 0.5 }), 500);
  assert.equal(inputDurationMs({ op: "key", code: 4 }), 0);
});

/** A float constant `NAME = 20.0` of scripts/autonom_lib/ios_idb.py. */
function cliConstant(name) {
  const source = readFileSync(new URL("../scripts/autonom_lib/ios_idb.py", import.meta.url), "utf8");
  const match = new RegExp(`^${name} = ([0-9.]+)$`, "m").exec(source);
  assert.ok(match, `${name} is defined in ios_idb.py`);
  return Number(match[1]) * 1000;
}

test("REV-029: the daemon answers an input before the CLI stops waiting for it, plain and observed", () => {
  const plainBudget = cliConstant("DAEMON_INPUT_BUDGET_S");
  const actMargin = cliConstant("ACT_MARGIN_S");
  assert.equal(plainBudget, 20_000);
  assert.equal(actMargin, 25_000);
  // The reply after the deadline waits at most for the cancelled calls to close.
  assert.ok(STREAM_STOP_TIMEOUT_MS < INPUT_REPLY_SLACK_MS, "the slack covers the cancel's stop timeout");
  assert.equal(INPUT_REPLY_SLACK_MS, 3000);
  // A plain tap, swipe, button or key: the CLI waits DAEMON_INPUT_BUDGET_S (+ the
  // input's duration on both sides), the daemon replies by its deadline + slack.
  assert.ok(DEFAULT_HID_WAIT_MS + INPUT_REPLY_SLACK_MS < plainBudget,
    `${DEFAULT_HID_WAIT_MS} + ${INPUT_REPLY_SLACK_MS} < ${plainBudget}`);
  assert.ok(plainBudget - (DEFAULT_HID_WAIT_MS + INPUT_REPLY_SLACK_MS) >= 1000, "at least a second to spare");
  // An act: the CLI waits timeout_ms + ACT_MARGIN_S; the daemon replies by
  // timeout_ms + ACT_REPLY_MARGIN_MS (+ slack), and the input, its confirmation and
  // grace windows fit inside that margin.
  assert.equal(ACT_REPLY_MARGIN_MS, 20_000);
  assert.ok(ACT_REPLY_MARGIN_MS + INPUT_REPLY_SLACK_MS < actMargin, `${ACT_REPLY_MARGIN_MS} + ${INPUT_REPLY_SLACK_MS} < ${actMargin}`);
  // The widest confirmation window is the first touch's on a quiet stream.
  assert.ok(FIRST_TOUCH_CONFIRM_MS > CONFIRM_MS);
  assert.ok(DEFAULT_HID_WAIT_MS + INPUT_REPLY_SLACK_MS + FIRST_TOUCH_CONFIRM_MS + GRACE_MS < ACT_REPLY_MARGIN_MS);
  // The observed-input worst case also fits the CLI's own wait margin.
  assert.ok(DEFAULT_HID_WAIT_MS + INPUT_REPLY_SLACK_MS + FIRST_TOUCH_CONFIRM_MS + GRACE_MS < actMargin,
    `${DEFAULT_HID_WAIT_MS} + ${INPUT_REPLY_SLACK_MS} + ${FIRST_TOUCH_CONFIRM_MS} + ${GRACE_MS} < ${actMargin}`);
});

test("REV-055/REV-056: a stuck check's probes and the replacement fit inside an input's deadline and leave the input its spare", () => {
  // Before a touch: every silent probe of the proof at full length, then the wait for the
  // replacement, and ARM_WAIT_SPARE_MS for the input itself, inside the default deadline.
  assert.ok(STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + RESTART_WAIT_MS + ARM_WAIT_SPARE_MS < DEFAULT_HID_WAIT_MS,
    `${STUCK_SILENT_PROBES} x ${QUIET_STUCK_PROBE_MS} + ${RESTART_WAIT_MS} + ${ARM_WAIT_SPARE_MS} < ${DEFAULT_HID_WAIT_MS}`);
  // A healthy companion's probe stream reported in 0.37-1.47 s live: the limit covers it.
  assert.ok(QUIET_STUCK_PROBE_MS > 1470);
  assert.ok(STUCK_PROBE_FRESH_MS <= QUIET_STUCK_MS, "a probe's answer never outlives one silence window");
  // REV-056: one silent probe is no proof (about 5 % of a healthy companion's probes are silent).
  assert.ok(STUCK_SILENT_PROBES >= 2);
});

test("REV-056: silent probe answers count in a row only while the last one is silent and fresh", () => {
  assert.equal(freshSilentRun(null, 0), 0);
  assert.equal(freshSilentRun({}, 0), 0);
  assert.equal(freshSilentRun({ probe: { result: "ok", at: 100, silentRun: 0 } }, 200), 0, "an ok answer ends a run");
  assert.equal(freshSilentRun({ probe: { result: "silent", at: 100, silentRun: 1 } }, 200), 1);
  assert.equal(freshSilentRun({ probe: { result: "silent", at: 100, silentRun: 2 } }, 100 + STUCK_PROBE_FRESH_MS - 1), 2);
  assert.equal(freshSilentRun({ probe: { result: "silent", at: 100, silentRun: 2 } }, 100 + STUCK_PROBE_FRESH_MS), 0, "a stale answer counts no more");
  assert.equal(freshSilentRun({ probe: { result: "silent", at: 100 } }, 200), 1, "a silent answer is at least one");
});

test("the socket path is a hashed 0700 directory under TMPDIR and at most 103 bytes", () => {
  const short = socketPathFor("/Users/test/.autonom", UDID, { tmp: "/tmp" });
  assert.match(short.path, /^\/tmp\/autonom-iosd-[0-9a-f]{12}\/d\.sock$/);
  assert.equal(short.dir, short.path.slice(0, -"/d.sock".length));
  assert.equal(short.fits, true);
  assert.equal(short.bytes, Buffer.byteLength(short.path));
  assert.notEqual(socketPathFor("/Users/other/.autonom", UDID, { tmp: "/tmp" }).path, short.path, "per state base");
  assert.notEqual(socketPathFor("/Users/test/.autonom", "OTHER-UDID", { tmp: "/tmp" }).path, short.path, "per UDID");
  assert.equal(socketPathFor("/Users/test/.autonom", UDID, { tmp: "/tmp" }).path, short.path, "stable");
  const exact = `/${"x".repeat(MAX_SOCKET_PATH_BYTES - "/autonom-iosd-000000000000/d.sock".length - 1)}`;
  assert.equal(socketPathFor("b", UDID, { tmp: exact }).bytes, MAX_SOCKET_PATH_BYTES);
  assert.equal(socketPathFor("b", UDID, { tmp: exact }).fits, true);
  assert.equal(socketPathFor("b", UDID, { tmp: `${exact}y` }).fits, false);
});

test("the idle limit is 5 minutes, shortened by AUTONOM_IOSD_IDLE_MS or --idle-ms, never below 1 s", () => {
  assert.equal(idleLimit({ env: {} }), 300_000);
  assert.equal(idleLimit({ env: { AUTONOM_IOSD_IDLE_MS: "2000" } }), 2000);
  assert.equal(idleLimit({ env: { AUTONOM_IOSD_IDLE_MS: "10" } }), 1000);
  assert.equal(idleLimit({ env: { AUTONOM_IOSD_IDLE_MS: "soon" } }), 300_000);
  assert.equal(idleLimit({ flag: "4000", env: { AUTONOM_IOSD_IDLE_MS: "2000" } }), 4000);
});

test("the stuck-replacement interval is 30 s, shortened by AUTONOM_IOSD_STUCK_INTERVAL_MS, never below 1 s", () => {
  assert.equal(STUCK_INTERVAL_ENV, "AUTONOM_IOSD_STUCK_INTERVAL_MS");
  assert.equal(MIN_STUCK_INTERVAL_MS, 1000);
  assert.equal(stuckRestartInterval({}), STUCK_RESTART_INTERVAL_MS);
  assert.equal(stuckRestartInterval({ AUTONOM_IOSD_STUCK_INTERVAL_MS: "8000" }), 8000);
  assert.equal(stuckRestartInterval({ AUTONOM_IOSD_STUCK_INTERVAL_MS: "10" }), 1000);
  assert.equal(stuckRestartInterval({ AUTONOM_IOSD_STUCK_INTERVAL_MS: "" }), STUCK_RESTART_INTERVAL_MS);
  assert.equal(stuckRestartInterval({ AUTONOM_IOSD_STUCK_INTERVAL_MS: "soon" }), STUCK_RESTART_INTERVAL_MS);
});

test("r523: stuck replacements in a row without a sign of health need 30 s, 30 s, 60 s, 120 s, ... at most 10 minutes", () => {
  const interval = STUCK_RESTART_INTERVAL_MS;
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 7, 50].map((streak) => stuckRestartGap(interval, streak)),
    [30_000, 30_000, 60_000, 120_000, 240_000, 480_000, 600_000, 600_000, 600_000]);
  assert.equal(stuckRestartGap(interval, STUCK_BACKOFF_MAX_FACTOR), 10 * 60_000, "capped at 10 minutes");
  // Scaled with the test interval, cap included.
  assert.deepEqual([0, 1, 2, 3, 6].map((streak) => stuckRestartGap(1000, streak)), [1000, 1000, 2000, 4000, 20_000]);
  assert.equal(stuckRestartGap(1000, -3), 1000);
  assert.equal(stuckRestartGap(1000, Number.NaN), 1000);
});

test("r523: the host counts as overloaded above 6 per CPU of 1-minute load (60 on 10 CPUs); a test file may stand in for os.loadavg()", (t) => {
  const ten = { cpuCount: () => 10 };
  assert.deepEqual(hostLoad({}, { ...ten, loadavg: () => [60, 1, 1] }), { load_1m: 60, cpus: 10, load_limit: 60, overloaded: false });
  assert.deepEqual(hostLoad({}, { ...ten, loadavg: () => [60.016, 1, 1] }), { load_1m: 60.02, cpus: 10, load_limit: 60, overloaded: true });
  // The r523 loop (205-510 on 10 CPUs) is overloaded, and so is the untested 60-100 band; the
  // loads at which a replacement fixed a stuck companion (17.6, 27.6) and a healthy probe
  // answered in 1.47 s (29) are not.
  for (const load of [205, 424, 510, 60.5, 61, 80, 100]) assert.equal(hostLoad({}, { ...ten, loadavg: () => [load] }).overloaded, true, String(load));
  for (const load of [9, 17.6, 27.6, 29, 50, 59.9]) assert.equal(hostLoad({}, { ...ten, loadavg: () => [load] }).overloaded, false, String(load));
  assert.equal(hostLoad({}, { cpuCount: () => 8, loadavg: () => [48.5] }).overloaded, true, "6 per CPU on 8 CPUs too");
  assert.equal(hostLoad({}, { cpuCount: () => 8, loadavg: () => [48] }).overloaded, false, "6 per CPU on 8 CPUs too");
  assert.equal(hostLoad({}, { cpuCount: () => 0, loadavg: () => [11] }).load_limit, HOST_LOAD_FACTOR, "at least one CPU");
  const root = mkdtempSync(join(tmpdir(), "iosd-load-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const file = join(root, "load");
  writeFileSync(file, "250.5 1 1\n");
  assert.deepEqual(hostLoad({ [HOST_LOAD_FILE_ENV]: file }, { ...ten, loadavg: () => [1] }), { load_1m: 250.5, cpus: 10, load_limit: 60, overloaded: true });
  writeFileSync(file, "not a number");
  assert.equal(hostLoad({ [HOST_LOAD_FILE_ENV]: file }, { ...ten, loadavg: () => [7] }).load_1m, 7, "an unreadable number falls back");
  assert.equal(hostLoad({ [HOST_LOAD_FILE_ENV]: join(root, "missing") }, { ...ten, loadavg: () => [8] }).load_1m, 8, "a missing file falls back");
  assert.equal(typeof hostLoad({}).overloaded, "boolean", "the real host load is readable");
});

test("companion versions are read from --version, its text or the Homebrew path, and gated at 1.6.4", () => {
  assert.deepEqual(parseVersion("idb_companion 1.6.4"), [1, 6, 4]);
  assert.deepEqual(parseVersion("1.6"), [1, 6, 0]);
  assert.equal(parseVersion("none"), null);
  assert.equal(versionAtLeast("1.6.4"), true);
  assert.equal(versionAtLeast("1.7.0"), true);
  assert.equal(versionAtLeast("2.0"), true);
  assert.equal(versionAtLeast("1.6.3"), false);
  assert.equal(versionAtLeast("1.1.8"), false);
  assert.equal(versionAtLeast(null), null);
  assert.equal(companionVersionFrom('{"version":"1.1.8","build_date":"Jan 1 2024"}\n'), "1.1.8");
  assert.equal(companionVersionFrom('{"build_date":"Oct 1 2026","build_time":"08:27:41"}\n',
    "/opt/homebrew/Cellar/idb-companion/1.6.4/libexec/idb_companion"), "1.6.4");
  assert.equal(companionVersionFrom("idb_companion version 1.6.5\n"), "1.6.5");
  assert.equal(companionVersionFrom('{"build_date":"Oct 1 2026"}', "/usr/local/bin/idb_companion"), null);
});

test("the dtuhidd flag and the Device Hub check turn an unconfirmed input into a cause", () => {
  assert.equal(parseNotifyValue(`${DTUHIDD_FLAG} 1\n`), 1);
  assert.equal(parseNotifyValue(`${DTUHIDD_FLAG} 0\n`), 0);
  assert.equal(parseNotifyValue("0\n"), 0);
  assert.equal(parseNotifyValue("garbage"), null);
  const hub = "/Applications/Xcode.app/Contents/Applications/DeviceHub.app/Contents/MacOS/DeviceHub";
  const serviceHub = "/Applications/Xcode.app/Contents/SharedFrameworks/DVTInstrumentsFoundation.framework/Resources/DTServiceHub";
  assert.equal(deviceHubIn(`/sbin/launchd\n${hub}\n`), true);
  assert.equal(deviceHubIn(`/sbin/launchd\n${serviceHub}\n`), false, "DTServiceHub is not Device Hub");
  assert.equal(inputWarning({ flag: 1, deviceHub: false }).code, CODE.INPUT_BLOCKED);
  assert.match(inputWarning({ flag: 1, deviceHub: false }).hint, /Reboot the simulator/);
  assert.equal(inputWarning({ flag: 1, deviceHub: true }).code, CODE.INPUT_UNCONFIRMED);
  assert.equal(inputWarning({ flag: 0, deviceHub: false }).code, CODE.INPUT_UNCONFIRMED);
  assert.equal(inputWarning({ flag: null, deviceHub: null }).code, CODE.INPUT_UNCONFIRMED);
});

test("frame coverage counts the content rows below the status bar and skips containers", () => {
  const settings = JSON.parse(SETTINGS);
  const coverage = frameCoverage(settings);
  assert.ok(coverage > 0.7 && coverage <= 1, `Settings coverage ${coverage}`);
  assert.ok(frameCoverage(JSON.parse(SAFARI_AX)) < 0.1);
  // A full-screen group alone covers nothing: it is a container.
  assert.equal(frameCoverage([element("Application", "A", SCREEN), element("Group", null, SCREEN)]), 0);
  // Half of the rows below the status bar: [60, 467) of [60, 874).
  const half = (874 - 60) / 2;
  assert.equal(frameCoverage([element("Application", "A", SCREEN), element("Button", "b", { x: 0, y: 60, width: 50, height: half })]), 0.5);
  assert.equal(frameCoverage([]), null);
  assert.equal(frameCoverage("not a list"), null);
  assert.equal(frameCoverage([{ type: "Button", frame: { x: 0, y: "bad", width: 1, height: 1 } }]), null);
});

test("ID-09 rule: ax for native screens, axbridge for web apps, web views and low coverage", () => {
  const native = chooseReadBackend(JSON.parse(SETTINGS));
  assert.equal(native.axbridge, false);
  assert.equal(native.reason, null);
  assert.equal(chooseReadBackend(JSON.parse(SAFARI_AX)).reason, "web_content_app");
  const bundle = chooseReadBackend([element("Application", "Browser", SCREEN, { bundle_id: "com.apple.mobilesafari" }),
    ...JSON.parse(SETTINGS).slice(1)]);
  assert.deepEqual([bundle.axbridge, bundle.reason], [true, "web_content_app"]);
  const webView = chooseReadBackend([...JSON.parse(SETTINGS), element("WebView", null, { x: 0, y: 100, width: 402, height: 300 })]);
  assert.deepEqual([webView.axbridge, webView.reason], [true, "web_view"]);
  const area = chooseReadBackend([...JSON.parse(SETTINGS), { type: "Group", role: "AXWebArea", frame: { x: 0, y: 0, width: 10, height: 10 } }]);
  assert.equal(area.reason, "web_view");
  const half = (874 - 60) / 2;
  const at = (height) => chooseReadBackend([element("Application", "Game", SCREEN), element("Button", "b", { x: 0, y: 60, width: 50, height })]);
  assert.deepEqual([at(half).axbridge, at(half).coverage], [false, 0.5], "exactly the threshold stays on ax");
  assert.deepEqual([at(half - 10).axbridge, at(half - 10).reason], [true, "low_coverage"]);
  assert.equal(chooseReadBackend(null).axbridge, false, "an unparsable read keeps ax");
});

test("inputs are validated before anything is sent", () => {
  assert.deepEqual(validateInput({ op: "tap", x: 10, y: "20" }), { op: "tap", x: 10, y: 20 });
  assert.deepEqual(validateInput({ op: "tap", x: 1, y: 2, duration: 0.5 }), { op: "tap", x: 1, y: 2, duration: 0.5 });
  assert.deepEqual(validateInput({ op: "swipe", x1: 1, y1: 2, x2: 3, y2: 4 }), { op: "swipe", x1: 1, y1: 2, x2: 3, y2: 4, duration: 0.5 });
  assert.deepEqual(validateInput({ op: "button", name: "home" }), { op: "button", name: "HOME" });
  assert.deepEqual(validateInput({ op: "key", code: "40" }), { op: "key", code: 40 });
  for (const bad of [{ op: "tap", x: -1, y: 0 }, { op: "tap", x: "a", y: 0 }, { op: "button", name: "POWER" },
    { op: "key", code: 1.5 }, { op: "fly" }, null]) {
    assert.throws(() => validateInput(bad), (error) => error instanceof DaemonError && error.code === CODE.INVALID_REQUEST
      && error.stage === STAGE.BEFORE_SEND);
  }
});

test("only a live local companion in idb's state is adopted", () => {
  const entries = [
    { udid: UDID, is_local: true, pid: 999_999_9, path: "/tmp/idb/dead.sock" },
    { udid: "OTHER", is_local: true, pid: process.pid, path: "/tmp/idb/other.sock" },
    { udid: UDID, is_local: false, pid: process.pid, host: "10.0.0.1", port: 10882 },
  ];
  assert.equal(liveCompanionEntry(entries, UDID), null);
  entries.push({ udid: UDID, is_local: true, pid: process.pid, path: "/tmp/idb/live.sock" });
  assert.deepEqual(liveCompanionEntry(entries, UDID), { pid: process.pid, endpoint: { socketPath: "/tmp/idb/live.sock" } });
});

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

const FAKE_COMPANION = (paths) => `#!${process.execPath}
import * as http2 from "node:http2";
import { appendFileSync, existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import {
  GrpcMessageParser, decodeAccessibilityInfoRequest, decodeHidEvent, encodeAccessibilityInfoResponse,
  encodeAccessibilityQuiescenceResponse, grpcFrame,
} from ${JSON.stringify(COMPANION_MODULE)};
const config = JSON.parse(readFileSync(${JSON.stringify(paths.config)}, "utf8"));
const log = (record) => appendFileSync(${JSON.stringify(paths.events)}, JSON.stringify({ pid: process.pid, ...record }) + "\\n");
process.stdout.on("error", () => {});
const args = process.argv.slice(2);
if (args.includes("--version")) {
  console.log(JSON.stringify({ version: config.version ?? "1.6.4", build_date: "Oct 1 2026" }));
  process.exit(0);
}
const sock = args[args.indexOf("--grpc-domain-sock") + 1];
const quietStreams = new Set();
let quietCalls = 0;
let hidActive = null;
const hidWaiting = [];
// Like idb_companion's, a quiet stream reports touches only once it sent its first event:
// config.quietColdMs delays that first event on every call, quietSilent never sends it.
const warmStreams = new Set();
// stuckAfterMs: like idb_companion 1.6.4 at times, the first companion process (a marker
// file keeps a later one healthy) sends one BUSY with RUN_LOOP_IDLE pending this long
// after it started and then nothing but target changes, on every stream, until replaced.
// stuckEvent replaces that last report; stuckTargetChanges: [ms, ...] after it, one
// target_changed each (an app launched on the stuck companion; [ms, "target_exited"] for an
// app that exits instead), with stuckTargetBusy one
// more BUSY with RUN_LOOP_IDLE pending after each (as live: the new app's only report).
// A new stream of a stuck companion sends that BUSY as its first event and then nothing
// (seen live), or with stuckProbeSilent (probe streams, not the daemon's first one: its
// first event is that BUSY when it opens late) nothing at all (seen live too). SIGUSR2
// makes any companion process stuck the same way at once (a new episode); SIGUSR1 ends it
// with a new app that reports normally (target_changed, then QUIET), or on a companion
// that is not stuck sends those two reports. SIGWINCH makes any companion process
// healthy-silent at once, as healthySilentAfterMs does.
// healthySilentAfterMs: a healthy companion (REV-055) sends that BUSY this long after it
// started and then no state report, but a new stream gets its first event (QUIET, after
// probeColdMs for every call after the first) and touches are reported as usual.
const STUCK_EVENT = config.stuckEvent ?? { type: "state_changed", pid: 77, state: "BUSY", busySignals: ["ANIMATIONS_INACTIVE", "RUN_LOOP_IDLE"] };
const stuckMarker = ${JSON.stringify(paths.events)} + ".stuck";
let stuck = false;
const toWarm = (event) => {
  for (const send of quietStreams) if (warmStreams.has(send)) send(encodeAccessibilityQuiescenceResponse(event));
};
const broadcast = (event) => {
  if (stuck && event.type !== "target_changed" && event.type !== "target_exited") return;
  toWarm(event);
};
const goStuck = () => {
  toWarm(STUCK_EVENT);
  stuck = true;
  log({ k: "stuck" });
  // An entry is ms (a target_changed) or [ms, type] (target_changed or target_exited).
  (config.stuckTargetChanges ?? []).forEach((entry, i) => {
    const [ms, type] = Array.isArray(entry) ? entry : [entry, "target_changed"];
    setTimeout(() => {
      broadcast({ type, pid: 78 + i });
      if (config.stuckTargetBusy) toWarm(STUCK_EVENT);
      log({ k: type });
    }, ms);
  });
};
if (config.healthySilentAfterMs) {
  setTimeout(() => {
    toWarm(STUCK_EVENT);
    log({ k: "healthy_silent" });
  }, config.healthySilentAfterMs);
}
process.on("SIGUSR2", goStuck);
process.on("SIGWINCH", () => {
  toWarm(STUCK_EVENT);
  log({ k: "healthy_silent" });
});
// stuckFromStart: every companion process is stuck from its start (r523: each new
// companion's first report was that BUSY). SIGHUP ends it silently: no report is sent,
// the next touch is reported as usual.
if (config.stuckFromStart) {
  stuck = true;
  log({ k: "stuck" });
}
process.on("SIGHUP", () => {
  stuck = false;
  log({ k: "unstuck_silent" });
});
process.on("SIGUSR1", () => {
  stuck = false;
  broadcast({ type: "target_changed", pid: 90 });
  broadcast({ type: "state_changed", pid: 90, state: "QUIET" });
  log({ k: "unstuck" });
});
if (config.stuckAfterMs && !existsSync(stuckMarker)) {
  setTimeout(() => {
    writeFileSync(stuckMarker, String(process.pid));
    goStuck();
  }, config.stuckAfterMs);
}
let touchesSeen = 0;
const afterTouch = () => {
  if (config.touches === false) return;
  touchesSeen += 1;
  log({ k: "touch_up", n: touchesSeen, warm: warmStreams.size, stuck });
  if (stuck) return;
  // touchLimit: only the first N touches are reported, later ones never reach the app.
  if (config.touchLimit !== undefined && touchesSeen > config.touchLimit) return;
  // touchDelays: the delay of the Nth touch's report (a cold app reports its first late).
  const delay = config.touchDelays?.[touchesSeen - 1] ?? config.touchDelayMs ?? 30;
  const seen = touchesSeen;
  setTimeout(() => {
    broadcast({ type: "touches_completed", pid: 77 });
    // targetChangeAfterTouch: N: another app comes to the front 50 ms after the Nth report.
    if (config.targetChangeAfterTouch === seen) {
      setTimeout(() => {
        broadcast({ type: "target_changed", pid: 78 });
        log({ k: "target_changed" });
      }, 50);
    }
    for (const [delay, state] of config.afterTouchStates ?? []) {
      setTimeout(() => broadcast({ type: "state_changed", pid: 77, state }), delay);
    }
    if (config.busyMs) {
      broadcast({ type: "state_changed", pid: 77, state: "BUSY" });
      setTimeout(() => broadcast({ type: "state_changed", pid: 77, state: "QUIET" }), config.busyMs);
    }
  }, delay);
};
const server = http2.createServer();
server.on("session", (session) => session.on("error", () => {}));
server.on("stream", (stream, headers) => {
  const method = headers[":path"].split("/").pop();
  stream.on("error", () => {});
  const parser = new GrpcMessageParser();
  let trailers = { "grpc-status": "0" };
  let responded = false;
  const open = () => {
    if (responded) return;
    responded = true;
    stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
    stream.on("wantTrailers", () => stream.sendTrailers(trailers));
  };
  const send = (bytes) => !stream.destroyed && stream.write(grpcFrame(bytes));
  const end = (code = 0, message = "") => {
    trailers = { "grpc-status": String(code) };
    if (message) trailers["grpc-message"] = encodeURIComponent(message);
    if (!stream.destroyed && !stream.writableEnded) stream.end();
  };
  if (method === "accessibility_quiescence") {
    log({ k: "quiet_open" });
    if (config.quiescence === "unimplemented") {
      stream.respond({ ":status": 200, "content-type": "application/grpc", "grpc-status": "12",
        "grpc-message": encodeURIComponent("Method not found: idb.CompanionService/accessibility_quiescence") }, { endStream: true });
      return;
    }
    open();
    quietCalls += 1;
    quietStreams.add(send);
    stream.on("close", () => { quietStreams.delete(send); warmStreams.delete(send); });
    const call = quietCalls;
    const first = () => {
      if (stream.destroyed) return;
      // probeSilentCalls: [n, ...]: the nth quiet call of a healthy companion never sends
      // its first event (REV-056: a healthy companion's probe stream silent for 2 s at times).
      if (!stuck && config.probeSilentCalls?.includes(call)) {
        log({ k: "quiet_silent_call", call });
        return;
      }
      warmStreams.add(send);
      log({ k: "quiet_first", call, stuck });
      if (stuck && config.stuckProbeSilent && call > 1) return;
      send(encodeAccessibilityQuiescenceResponse(stuck ? STUCK_EVENT : { type: "state_changed", pid: 77, state: "QUIET" }));
    };
    if (!config.quietSilent) {
      const cold = call > 1 && config.probeColdMs !== undefined ? config.probeColdMs : config.quietColdMs;
      if (cold) setTimeout(first, cold);
      else first();
    }
    // The frontmost app exits once: the companion finishes the call after target_exited.
    if (config.quietEndMs && quietCalls === 1) {
      setTimeout(() => {
        send(encodeAccessibilityQuiescenceResponse({ type: "target_exited", pid: 77 }));
        quietStreams.delete(send);
        end(0);
      }, config.quietEndMs);
    }
    return;
  }
  if (method === "hid") {
    // Like idb_companion: one hid call at a time. A second call (another gRPC session, or
    // an idb ui text process) gets no answer and none of its events are performed until
    // the open call ends.
    const serve = () => {
      hidActive = stream;
      log({ k: "hid_open" });
      open();
      stream.on("data", (chunk) => {
        for (const message of parser.push(chunk)) {
          const event = decodeHidEvent(message);
          log({ k: "hid", e: event });
          if (event?.touch && event.direction === "up") afterTouch();
        }
      });
      // hidEndDelayMs: the companion's status on a call comes this late; hidEndCode: not OK.
      stream.on("end", () => {
        const finish = () => { send(Buffer.alloc(0)); end(config.hidEndCode ?? 0, config.hidEndCode ? "hid failed" : ""); };
        if (config.hidEndDelayMs) setTimeout(finish, config.hidEndDelayMs);
        else finish();
      });
    };
    stream.on("close", () => {
      const waiting = hidWaiting.indexOf(serve);
      if (waiting >= 0) hidWaiting.splice(waiting, 1);
      if (hidActive !== stream) return;
      hidActive = null;
      log({ k: "hid_close" });
      hidWaiting.shift()?.();
    });
    if (hidActive) {
      log({ k: "hid_wait" });
      hidWaiting.push(serve);
    } else {
      serve();
    }
    return;
  }
  if (method === "accessibility_info") {
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        const request = decodeAccessibilityInfoRequest(message);
        log({ k: "read", backend: request.backend, filter: request.filter, format: request.format });
        open();
        if (config.unlit) return end(2, "Cannot read the accessibility of a display that is not lit yet");
        const failure = config.axError?.[request.backend];
        if (failure) return end(2, failure);
        send(encodeAccessibilityInfoResponse(config.ax?.[request.backend] ?? config.ax?.default ?? "[]"));
        end(0);
      }
    });
    return;
  }
  stream.on("data", () => {});
  open();
  end(12, "unimplemented " + method);
});
rmSync(sock, { force: true });
server.listen(sock, () => {
  log({ k: "start", sock });
  console.log(JSON.stringify({ grpc_path: sock }));
});
process.on("SIGTERM", () => { log({ k: "sigterm" }); process.exit(0); });
`;

const FAKE_IDB = (paths) => `#!${process.execPath}
import { spawn } from "node:child_process";
import { appendFileSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const args = process.argv.slice(2);
appendFileSync(${JSON.stringify(paths.idbLog)}, JSON.stringify({ argv: args, env: Object.keys(process.env).sort() }) + "\\n");
const state = ${JSON.stringify(paths.state)};
const read = () => { try { return JSON.parse(readFileSync(state, "utf8")); } catch { return []; } };
const write = (list) => { writeFileSync(state + ".tmp", JSON.stringify(list)); renameSync(state + ".tmp", state); };
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (error) { return error.code === "EPERM"; } };
const config = JSON.parse(readFileSync(${JSON.stringify(paths.config)}, "utf8")).idb ?? {};
const [command, udid] = args;
if (command === "connect") {
  if (config.failConnect) process.exit(1);
  if (read().some((entry) => entry.udid === udid && alive(entry.pid))) process.exit(0);
  const sock = join(${JSON.stringify(paths.sockets)}, "c" + process.pid + ".sock");
  const child = spawn(${JSON.stringify(paths.companion)}, ["--udid", udid, "--grpc-domain-sock", sock, "--only", "simulator"], {
    detached: true, stdio: ["ignore", "pipe", "ignore"],
  });
  const timer = setTimeout(() => process.exit(3), 10000);
  let text = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    text += chunk;
    if (!text.includes("\\n")) return;
    clearTimeout(timer);
    child.stdout.destroy();
    child.unref();
    write([...read().filter((entry) => entry.udid !== udid), { udid, is_local: true, pid: child.pid, path: sock }]);
    // connectDelayMs: the companion is up and registered, idb itself returns later;
    // laterConnectDelayMs: the same for every companion after the first.
    let started = 0;
    try { started = Number(readFileSync(state + ".started", "utf8")) || 0; } catch {}
    writeFileSync(state + ".started", String(started + 1));
    const delay = started > 0 && config.laterConnectDelayMs !== undefined ? config.laterConnectDelayMs : config.connectDelayMs ?? 0;
    setTimeout(() => process.exit(0), delay);
  });
} else if (command === "disconnect") {
  write(read().filter((entry) => entry.udid !== udid));
  process.exit(0);
} else {
  process.exit(2);
}
`;

const FAKE_XCRUN = (paths) => `#!${process.execPath}
import { appendFileSync, readFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(${JSON.stringify(paths.xcrunLog)}, JSON.stringify(args) + "\\n");
const config = JSON.parse(readFileSync(${JSON.stringify(paths.config)}, "utf8")).xcrun ?? {};
if (args[0] === "simctl" && args[1] === "spawn" && args[3] === "notifyutil" && args[4] === "-g") {
  console.log(args[5] + " " + (config.flag ?? 0));
  process.exit(0);
}
process.exit(1);
`;

const FAKE_PS = (paths) => `#!${process.execPath}
import { readFileSync } from "node:fs";
const config = JSON.parse(readFileSync(${JSON.stringify(paths.config)}, "utf8")).ps ?? {};
const lines = ["/sbin/launchd", "/usr/libexec/logd",
  "/Applications/Xcode.app/Contents/SharedFrameworks/DVTInstrumentsFoundation.framework/Resources/DTServiceHub"];
if (config.deviceHub) lines.push("/Applications/Xcode.app/Contents/Applications/DeviceHub.app/Contents/MacOS/DeviceHub");
console.log(lines.join("\\n"));
`;

function script(path, text) {
  writeFileSync(path, text);
  chmodSync(path, 0o755);
}

function jsonLines(path) {
  if (!existsSync(path)) return [];
  // Only complete lines: a fake may be appending its next record while this reads.
  const lines = readFileSync(path, "utf8").split("\n");
  lines.pop();
  return lines.filter(Boolean).map((line) => JSON.parse(line));
}

// The daemon replies once its HID writes are flushed; the fake companion logs them when it
// reads them, so a record can land a little after the reply. Poll for it, at most 2 s.
const RECORD_WAIT_MS = 2000;

/** Reads `read()` until `done(value)` holds or 2 s pass, and returns the last value either way. */
async function recorded(read, done) {
  const deadline = Date.now() + RECORD_WAIT_MS;
  let value = read();
  while (!done(value) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
    value = read();
  }
  return value;
}

/** Waits up to 2 s for the record to equal `expected`, then asserts it (a diff on timeout). */
async function assertRecorded(read, expected, message) {
  assert.deepEqual(await recorded(read, (value) => isDeepStrictEqual(value, expected)), expected, message);
}

async function waitFor(check, { timeoutMs = 8000, stepMs = 25, message = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${message}`);
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
}

function daemonProcesses(base) {
  const listing = execFileSync("ps", ["-axo", "pid=,command="], { encoding: "utf8" });
  return listing.split("\n")
    .filter((line) => line.includes(DAEMON_SCRIPT) && line.includes(`--udid ${UDID}`) && line.includes(base))
    .map((line) => Number.parseInt(line.trim(), 10));
}

/** One temporary world: state base, fakes, their logs, and cleanup of every process it started. */
function harness(t, config = {}, { idleMs = 60_000 } = {}) {
  const root = mkdtempSync(join(tmpdir(), "iosd-"));
  const bin = join(root, "bin");
  const sockets = join(root, "s");
  mkdirSync(bin);
  mkdirSync(sockets);
  const developer = join(root, "Xcode.app", "Contents", "Developer");
  mkdirSync(developer, { recursive: true });
  writeFileSync(join(root, "Xcode.app", "Contents", "version.plist"),
    "<plist><dict><key>CFBundleShortVersionString</key>\n<string>27.1</string></dict></plist>\n");
  const paths = {
    root, bin, sockets, base: join(root, "home"),
    config: join(root, "config.json"), events: join(root, "events.jsonl"), idbLog: join(root, "idb.jsonl"),
    xcrunLog: join(root, "xcrun.jsonl"), state: join(root, "idb-state.json"), load: join(root, "load"),
    companion: join(bin, "idb_companion"), idb: join(bin, "idb"), xcrun: join(bin, "xcrun"),
  };
  const world = {
    paths,
    setConfig(next) {
      writeFileSync(paths.config, JSON.stringify(next));
    },
    // The host's 1-minute load as the daemon reads it (0 unless a test says otherwise).
    setLoad(value) {
      writeFileSync(paths.load, String(value));
    },
    env: {
      PATH: `${bin}${delimiter}${process.env.PATH}`, HOME: process.env.HOME ?? root, TMPDIR: tmpdir(),
      AUTONOM_IDB: paths.idb, AUTONOM_IDB_COMPANION_BIN: paths.companion, AUTONOM_IDB_STATE_FILE: paths.state,
      AUTONOM_SIMCTL: paths.xcrun, AUTONOM_IOSD_IDLE_MS: String(idleMs), DEVELOPER_DIR: developer,
      CUSTOM_SECRET: "synthetic-secret", [HOST_LOAD_FILE_ENV]: paths.load,
    },
    events: () => jsonLines(paths.events),
    hid: () => jsonLines(paths.events).filter((r) => r.k === "hid").map((r) => r.e),
    reads: () => jsonLines(paths.events).filter((r) => r.k === "read"),
    companionPids: () => [...new Set(jsonLines(paths.events).filter((r) => r.k === "start").map((r) => r.pid))],
    idbCalls: () => jsonLines(paths.idbLog),
    xcrunCalls: () => jsonLines(paths.xcrunLog),
    registryPath: daemonPaths(paths.base, UDID).registry,
    registry: () => {
      try {
        return JSON.parse(readFileSync(daemonPaths(paths.base, UDID).registry, "utf8"));
      } catch {
        return null;
      }
    },
    daemonPids: new Set(),
    async start(options = {}) {
      const status = await ensureDaemon({ udid: UDID, stateBase: paths.base, env: world.env, ...options });
      if (status.pid) world.daemonPids.add(status.pid);
      return status;
    },
    async connect(status) {
      const connection = await DaemonConnection.connect(status.socket);
      t.after(() => connection.close());
      return connection;
    },
  };
  world.setConfig({ version: "1.6.4", ax: { AX: SETTINGS, AXBRIDGE: SAFARI_AXBRIDGE }, ...config });
  world.setLoad(0);
  script(paths.companion, FAKE_COMPANION(paths));
  script(paths.idb, FAKE_IDB(paths));
  script(paths.xcrun, FAKE_XCRUN(paths));
  script(join(bin, "ps"), FAKE_PS(paths));
  t.after(async () => {
    for (const pid of [...world.daemonPids, ...daemonProcesses(paths.base)]) {
      if (!pidAlive(pid)) continue;
      try {
        process.kill(pid, "SIGTERM");
      } catch {
        // Gone.
      }
      await waitFor(() => !pidAlive(pid), { timeoutMs: 5000, message: "daemon exit" }).catch(() => {
        try {
          process.kill(pid, "SIGKILL");
        } catch {
          // Gone.
        }
      });
    }
    for (const pid of world.companionPids()) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        // Gone.
      }
    }
    rmSync(socketPathFor(paths.base, UDID).dir, { recursive: true, force: true });
    rmSync(root, { recursive: true, force: true });
  });
  return world;
}

function tapPairs(events) {
  return events.filter((e) => e?.touch).map((e) => `${e.direction}@${e.touch.x},${e.touch.y}`);
}

// ---------------------------------------------------------------------------
// ID-01: one daemon per simulator, found through the registry
// ---------------------------------------------------------------------------

test("ID-01-S01: two clients at once get one daemon, one socket, a 0700 directory and a path of at most 103 bytes", async (t) => {
  const world = harness(t);
  const [a, b] = await Promise.all([world.start(), world.start()]);
  assert.equal(a.status, "serving");
  assert.equal(b.status, "serving");
  assert.equal(a.pid, b.pid);
  assert.equal(a.socket, b.socket);
  assert.deepEqual(daemonProcesses(world.paths.base), [a.pid], "exactly one daemon process for the UDID");
  const registry = world.registry();
  assert.equal(registry.schema, IOSD_SCHEMA);
  assert.equal(registry.socket, a.socket);
  assert.equal(registry.pid, a.pid);
  assert.equal(registry.udid, UDID);
  assert.equal(registry.state, "serving");
  assert.equal(registry.companion.owned, true);
  assert.ok(pidAlive(registry.companion.pid));
  assert.match(registry.companion.socket, /\.sock$/);
  assert.equal(registry.versions.companion, "1.6.4");
  assert.equal(registry.versions.xcode, "27.1");
  assert.equal(registry.versions.xcode_path, join(world.paths.root, "Xcode.app"));
  assert.ok("coresimulator" in registry.versions);
  assert.ok(!Number.isNaN(Date.parse(registry.started_at)));
  assert.equal(registry.idle_ms, 60_000);
  assert.equal(statSync(registry.socket).isSocket(), true);
  assert.equal(statSync(socketPathFor(world.paths.base, UDID).dir).mode & 0o777, 0o700);
  assert.ok(Buffer.byteLength(registry.socket) <= 103);
  assert.equal(statSync(world.registryPath).mode & 0o777, 0o600);
  assert.equal(world.companionPids().length, 1, "one companion");
  assert.equal(world.idbCalls().filter((c) => c.argv[0] === "connect").length, 1);
});

test("ID-01-S01: concurrent starts in one process spawn one daemon, even with a slow idb connect", async (t) => {
  const world = harness(t, { idb: { connectDelayMs: 1500 } });
  const spawns = join(world.paths.root, "spawns.txt");
  const nodePath = join(world.paths.bin, "counting-node");
  script(nodePath, `#!/bin/sh\necho spawn >> ${JSON.stringify(spawns)}\nexec ${JSON.stringify(process.execPath)} "$@"\n`);
  const [a, b, c] = await Promise.all([world.start({ nodePath }), world.start({ nodePath }), world.start({ nodePath })]);
  assert.deepEqual([a.status, b.status, c.status], ["serving", "serving", "serving"]);
  assert.equal(new Set([a.pid, b.pid, c.pid]).size, 1);
  assert.equal(readFileSync(spawns, "utf8").trim().split("\n").length, 1, "one daemon process was spawned");
  assert.deepEqual(daemonProcesses(world.paths.base), [a.pid]);
  assert.equal(existsSync(daemonPaths(world.paths.base, UDID).startLock), false, "the start lock is released");
});

test("ID-01-S01: two daemon processes started at the same moment leave one serving, the other reports it", async (t) => {
  const world = harness(t);
  const launch = () => new Promise((resolve) => {
    const child = spawn(process.execPath, [DAEMON_SCRIPT, "--udid", UDID, "--state-base", world.paths.base],
      { env: world.env, stdio: ["ignore", "pipe", "ignore"] });
    let text = "";
    child.stdout.on("data", (chunk) => {
      text += chunk;
      if (text.includes("\n")) resolve({ child, status: JSON.parse(text.split("\n")[0]) });
    });
  });
  const results = await Promise.all([launch(), launch()]);
  for (const { child } of results) world.daemonPids.add(child.pid);
  const statuses = results.map((r) => r.status.status).sort();
  assert.deepEqual(statuses, ["already_running", "serving"]);
  assert.equal(results[0].status.socket, results[1].status.socket);
  const loser = results.find((r) => r.status.status === "already_running");
  await waitFor(() => !pidAlive(loser.child.pid), { message: "the second daemon to exit" });
  assert.equal(daemonProcesses(world.paths.base).length, 1);
  for (const { child } of results) child.stdout.destroy();
});

test("ID-03/RISK-003: a SIGTERM while idb connect runs stops the companion it started and leaves no files", async (t) => {
  const world = harness(t, { idb: { connectDelayMs: 2000 } });
  const child = spawn(process.execPath, [DAEMON_SCRIPT, "--udid", UDID, "--state-base", world.paths.base],
    { env: world.env, stdio: ["ignore", "pipe", "ignore"] });
  world.daemonPids.add(child.pid);
  let text = "";
  child.stdout.on("data", (chunk) => { text += chunk; });
  const exited = new Promise((resolve) => child.once("exit", (code) => resolve(code)));
  // The companion is registered in idb's state while idb connect has not returned yet.
  const companionPid = await waitFor(() => {
    try {
      return JSON.parse(readFileSync(world.paths.state, "utf8")).find((entry) => entry.udid === UDID)?.pid;
    } catch {
      return null;
    }
  }, { message: "idb connect to register the companion" });
  assert.ok(existsSync(daemonPaths(world.paths.base, UDID).daemonLock), "the daemon holds its lock");
  process.kill(child.pid, "SIGTERM");
  const code = await exited;
  assert.notEqual(code, null, "the daemon exited by itself, not by the signal's default action");
  const status = JSON.parse(text.split("\n")[0]);
  assert.equal(status.status, "failed");
  assert.equal(status.error.reason, "stopped");
  assert.equal(pidAlive(companionPid), false, "the owned companion was stopped");
  assert.ok(world.idbCalls().some((c) => c.argv[0] === "disconnect"), "idb disconnect ran");
  assert.equal(world.registry(), null);
  assert.equal(existsSync(socketPathFor(world.paths.base, UDID).dir), false);
  assert.equal(existsSync(daemonPaths(world.paths.base, UDID).daemonLock), false);
});

test("ID-01-S02: a stale registry is replaced and its socket removed", async (t) => {
  const world = harness(t);
  const paths = daemonPaths(world.paths.base, UDID);
  mkdirSync(paths.dir, { recursive: true });
  // A dead pid and a socket file nobody serves any more (its server was killed).
  const dead = spawn(process.execPath, ["-e", ""]);
  await new Promise((resolve) => dead.once("exit", resolve));
  const staleDir = mkdtempSync(join(tmpdir(), "autonom-iosd-stale"));
  t.after(() => rmSync(staleDir, { recursive: true, force: true }));
  const staleSocket = join(staleDir, "d.sock");
  const holder = spawn(process.execPath, ["-e",
    `require("node:net").createServer().listen(${JSON.stringify(staleSocket)}, () => console.log("up"))`], { stdio: ["ignore", "pipe", "ignore"] });
  await new Promise((resolve) => holder.stdout.once("data", resolve));
  holder.kill("SIGKILL");
  await new Promise((resolve) => holder.once("exit", resolve));
  assert.equal(statSync(staleSocket).isSocket(), true, "the stale socket file is left behind");
  // The current socket path also holds a leftover file.
  const current = socketPathFor(world.paths.base, UDID);
  mkdirSync(current.dir, { recursive: true, mode: 0o700 });
  writeFileSync(current.path, "");
  writeFileSync(paths.registry, JSON.stringify({ schema: IOSD_SCHEMA, udid: UDID, pid: dead.pid, socket: staleSocket,
    companion: null, versions: {}, started_at: new Date(0).toISOString(), idle_ms: 300000 }));
  const status = await world.start();
  assert.equal(status.status, "serving");
  assert.notEqual(status.pid, dead.pid);
  assert.ok(pidAlive(status.pid));
  assert.equal(world.registry().pid, status.pid, "the registry names the new daemon");
  assert.equal(world.registry().socket, current.path);
  assert.equal(existsSync(staleSocket), false, "the stale socket is gone");
  assert.equal(statSync(current.path).isSocket(), true, "the leftover file was replaced by the live socket");
  const reply = await requestDaemon(status.socket, { id: "s", op: "status" });
  assert.equal(reply.ok, true);
});

test("RISK-009: a socket path over 103 bytes refuses the start with daemon_start_failed and creates nothing", async (t) => {
  const world = harness(t);
  const longTmp = join(world.paths.root, "t".repeat(120));
  const env = { ...world.env, TMPDIR: longTmp };
  await assert.rejects(ensureDaemon({ udid: UDID, stateBase: world.paths.base, env }),
    (error) => error instanceof DaemonError && error.code === CODE.DAEMON_START_FAILED && error.extra.reason === "socket_path_too_long");
  assert.equal(existsSync(longTmp), false);
  assert.equal(world.companionPids().length, 0, "no companion was started");
  assert.equal(world.idbCalls().length, 0);
  assert.equal(world.registry(), null);
});

// ---------------------------------------------------------------------------
// ID-02: the daemon owns the companion of its simulator
// ---------------------------------------------------------------------------

test("ID-02: idb connect runs under the companion environment allowlist only", async (t) => {
  const world = harness(t);
  await world.start();
  const connect = world.idbCalls().find((call) => call.argv[0] === "connect");
  assert.deepEqual(connect.argv, ["connect", UDID]);
  for (const key of connect.env) {
    assert.ok(COMPANION_ENV_KEYS.includes(key) || key === "__CF_USER_TEXT_ENCODING", `unexpected variable ${key} reached idb`);
  }
  assert.ok(!connect.env.includes("CUSTOM_SECRET"));
  assert.ok(!connect.env.includes("AUTONOM_IOSD_IDLE_MS"));
});

test("ID-02/ID-03: a companion idb already lists is used, not owned, and left running on stop", async (t) => {
  const world = harness(t);
  // Another idb user started the companion first.
  execFileSync(world.paths.idb, ["connect", UDID], { env: world.env });
  const [companion] = world.companionPids();
  assert.ok(pidAlive(companion));
  const status = await world.start();
  assert.equal(status.companion.owned, false);
  assert.equal(status.companion.pid, companion);
  assert.equal(world.idbCalls().length, 1, "the daemon ran no idb connect of its own");
  const stop = await requestDaemon(status.socket, { id: "x", op: "stop" });
  assert.equal(stop.ok, true);
  await waitFor(() => !pidAlive(status.pid), { message: "daemon exit" });
  assert.ok(pidAlive(companion), "the companion it did not start keeps running");
  assert.equal(world.idbCalls().filter((c) => c.argv[0] === "disconnect").length, 0);
  assert.equal(world.registry(), null);
});

test("ID-02-S01: a crashed companion is replaced within 6 s, the next tree works and no input is sent again", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const connection = await world.connect(status);
  const tap = await connection.request({ op: "tap", x: 10, y: 10 });
  assert.equal(tap.ok, true);
  await assertRecorded(() => tapPairs(world.hid()), ["down@10,10", "up@10,10"]);
  const first = status.companion.pid;
  const crashedAt = Date.now();
  process.kill(first, "SIGKILL");
  const restarted = await waitFor(() => {
    const registry = world.registry();
    return registry?.state === "serving" && registry.companion?.pid !== first ? registry : null;
  }, { timeoutMs: 6000, message: "a new companion" });
  assert.ok(Date.now() - crashedAt < 6000);
  assert.equal(restarted.companion.owned, true);
  assert.equal(world.companionPids().length, 2, "exactly one new companion");
  assert.equal(world.idbCalls().filter((c) => c.argv[0] === "connect").length, 2, "restarted through idb connect");
  const tree = await connection.request({ op: "tree" });
  assert.equal(tree.ok, true, JSON.stringify(tree.error));
  assert.equal(tree.ax_backend, "ax");
  assert.deepEqual(tapPairs(world.hid()), ["down@10,10", "up@10,10"], "the tap sent before the crash is not re-sent");
  const statusReply = await connection.request({ op: "status" });
  assert.equal(statusReply.restarts, 1);
  assert.equal(connection.open, true, "the client connection survived the companion restart");
});

// ---------------------------------------------------------------------------
// ID-03: idle stop
// ---------------------------------------------------------------------------

test("ID-03-S01: with no request for the idle limit the daemon stops and leaves nothing behind", async (t) => {
  const world = harness(t, {}, { idleMs: 2000 });
  const status = await world.start();
  const companion = status.companion.pid;
  const socketDir = socketPathFor(world.paths.base, UDID).dir;
  assert.ok(existsSync(status.socket));
  await waitFor(() => !pidAlive(status.pid), { timeoutMs: 8000, message: "idle stop" });
  assert.equal(existsSync(world.registryPath), false, "registry removed");
  assert.equal(existsSync(status.socket), false, "socket removed");
  assert.equal(existsSync(socketDir), false, "socket directory removed");
  assert.equal(existsSync(daemonPaths(world.paths.base, UDID).daemonLock), false, "lock released");
  await waitFor(() => !pidAlive(companion), { timeoutMs: 4000, message: "companion exit" });
  assert.deepEqual(world.idbCalls().map((c) => c.argv[0]), ["connect", "disconnect"]);
  assert.deepEqual(JSON.parse(readFileSync(world.paths.state, "utf8")), [], "idb no longer lists it");
  assert.ok(world.events().some((e) => e.k === "sigterm" && e.pid === companion), "stopped with SIGTERM");
  const log = readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8");
  assert.match(log, /"event":"stopping","reason":"idle"/);
});

test("ID-03-S02: an attached Canvas keeps the daemon alive past the idle limit; detach lets it stop", async (t) => {
  const world = harness(t, {}, { idleMs: 2000 });
  const status = await world.start();
  const canvas = await world.connect(status);
  const attach = await canvas.request({ op: "attach" });
  assert.equal(attach.ok, true);
  assert.equal(attach.attached, 1);
  assert.equal(attach.socket, status.companion.socket, "the Canvas learns the companion socket");
  await new Promise((resolve) => setTimeout(resolve, 3000));
  assert.ok(pidAlive(status.pid), "still running while attached");
  const detach = await canvas.request({ op: "detach" });
  assert.equal(detach.attached, 0);
  await waitFor(() => !pidAlive(status.pid), { timeoutMs: 6000, message: "idle stop after detach" });
});

test("ID-03: a Canvas whose connection drops no longer keeps the daemon alive", async (t) => {
  const world = harness(t, {}, { idleMs: 1000 });
  const status = await world.start();
  const canvas = await DaemonConnection.connect(status.socket);
  await canvas.request({ op: "attach" });
  await new Promise((resolve) => setTimeout(resolve, 1500));
  assert.ok(pidAlive(status.pid));
  canvas.close();
  await waitFor(() => !pidAlive(status.pid), { timeoutMs: 5000, message: "idle stop after the Canvas left" });
});

/** A raw connection that keeps every line it gets, for tests of write order and closing. */
async function rawConnection(t, socketPath) {
  const socket = netConnect(socketPath);
  await new Promise((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", reject);
  });
  socket.on("error", () => {});
  const raw = {
    socket, lines: [], closed: false,
    /** One status round trip, so the daemon has surely accepted this connection. */
    async accepted() {
      socket.write(`${JSON.stringify({ id: "accepted", op: "status" })}\n`);
      await waitFor(() => raw.lines.some((line) => line.id === "accepted"), { timeoutMs: 5000, message: "status reply" });
    },
  };
  let buffer = "";
  socket.setEncoding("utf8");
  socket.on("data", (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      raw.lines.push(JSON.parse(buffer.slice(0, index)));
      buffer = buffer.slice(index + 1);
    }
  });
  socket.on("close", () => {
    raw.closed = true;
  });
  t.after(() => socket.destroy());
  return raw;
}

test("ID-03: an attach and a detach sent in one write run in order, so the idle stop still comes", async (t) => {
  const world = harness(t, {}, { idleMs: 1000 });
  const status = await world.start();
  const raw = await rawConnection(t, status.socket);
  raw.socket.write(`${JSON.stringify({ id: "A", op: "attach" })}\n${JSON.stringify({ id: "D", op: "detach" })}\n`);
  await waitFor(() => raw.lines.filter((line) => line.id !== null).length >= 2, { timeoutMs: 5000, message: "two replies" });
  const replies = raw.lines.filter((line) => line.id !== null);
  assert.deepEqual(replies.map((line) => line.id), ["A", "D"], "replies come in request order");
  assert.deepEqual(replies.map((line) => [line.ok, line.attached]), [[true, 1], [true, 0]]);
  // The connection stays open; with nothing attached the idle stop must still come.
  await waitFor(() => !pidAlive(status.pid), { timeoutMs: 6000, message: "idle stop with the connection still open" });
  assert.equal(existsSync(world.registryPath), false, "registry removed");
  const closing = raw.lines.find((line) => line.closing === true);
  assert.ok(closing, "the daemon wrote its closing line");
  assert.deepEqual([closing.id, closing.ok, closing.error.code, closing.error.stage], [null, false, "daemon_unreachable", "before_send"]);
});

test("ID-03/ID-05: a request arriving while the daemon stops gets a before_send reply and reaches nothing", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const one = await world.connect(status);
  const two = await rawConnection(t, status.socket);
  const idle = await rawConnection(t, status.socket);
  await idle.accepted();
  // Client 2 has a long tap in flight, so its connection stays open past the stop.
  two.socket.write(`${JSON.stringify({ id: "long", op: "tap", x: 10, y: 10, duration: 1 })}\n`);
  await waitFor(() => tapPairs(world.hid()).length >= 1, { timeoutMs: 4000, message: "the long tap to start" });
  const stop = await one.request({ op: "stop" });
  assert.equal(stop.ok, true);
  two.socket.write(`${JSON.stringify({ id: "T", op: "tap", x: 50, y: 50 })}\n`);
  await waitFor(() => two.lines.some((line) => line.id === "T"), { timeoutMs: 5000, message: "a reply to the late tap" });
  const late = two.lines.find((line) => line.id === "T");
  assert.deepEqual([late.ok, late.error.code, late.error.stage], [false, "daemon_unreachable", "before_send"]);
  await waitFor(() => two.closed && idle.closed, { timeoutMs: 8000, message: "both connections closed" });
  // Every connection that had nothing in flight got the closing line before it closed.
  const closing = idle.lines.find((line) => line.closing === true);
  assert.ok(closing, "an idle connection gets the closing line");
  assert.equal(closing.error.stage, "before_send");
  const lateIndex = two.lines.findIndex((line) => line.id === "T");
  const longIndex = two.lines.findIndex((line) => line.id === "long");
  const closingIndex = two.lines.findIndex((line) => line.closing === true);
  if (longIndex >= 0) {
    assert.ok(closingIndex > Math.max(lateIndex, longIndex), "the closing line comes after every reply");
  } else {
    // The stop finished while the tap was in flight: no closing line may then claim it never ran.
    assert.equal(closingIndex, -1, "no closing line while a request on the connection is unanswered");
  }
  assert.equal(tapPairs(world.hid()).some((entry) => entry.endsWith("@50,50")), false, "the late tap was never sent");
  await waitFor(() => !pidAlive(status.pid), { timeoutMs: 6000, message: "daemon exit" });
});

test("ID-05: a client whose request the daemon never ran after its closing line sees before_send", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const one = await world.connect(status);
  const two = await DaemonConnection.connect(status.socket);
  t.after(() => two.close());
  const raw = await rawConnection(t, status.socket);
  await raw.accepted();
  assert.equal((await two.request({ op: "status" })).ok, true);
  // A request read after the closing line is not run and not answered.
  const stop = await one.request({ op: "stop" });
  assert.equal(stop.ok, true);
  raw.socket.write(`${JSON.stringify({ id: "T", op: "tap", x: 60, y: 60 })}\n`);
  const late = await two.request({ id: "T2", op: "tap", x: 70, y: 70 }).then(
    (reply) => reply,
    (error) => ({ ok: false, error: { code: error.code, stage: error.stage } }),
  );
  assert.deepEqual([late.ok, late.error.code, late.error.stage], [false, "daemon_unreachable", "before_send"]);
  await waitFor(() => raw.closed, { timeoutMs: 5000, message: "the raw connection to close" });
  assert.equal(raw.lines.some((line) => line.id === "T"), false, "nothing after the closing line was answered");
  assert.ok(raw.lines.some((line) => line.closing === true));
  await two.closed;
  assert.ok(two.closing, "the client kept the closing line");
  assert.equal(two.replies.some((reply) => reply.closing), false, "the closing line is not a reply");
  const after = await two.request({ op: "status" }).catch((error) => error);
  assert.deepEqual([after.code, after.stage], ["daemon_unreachable", "before_send"]);
  assert.equal(tapPairs(world.hid()).length, 0, "no tap reached the device");
});

// ---------------------------------------------------------------------------
// ID-04: version gate
// ---------------------------------------------------------------------------

test("ID-04-S01: a 1.1.8 companion without accessibility_quiescence makes the daemon unsupported", async (t) => {
  const world = harness(t, { version: "1.1.8", quiescence: "unimplemented" });
  const status = await world.start();
  assert.equal(status.status, "unsupported");
  assert.equal(status.reason, "companion_too_old");
  assert.equal(status.companion_version, "1.1.8");
  for (const op of ["tree", "tap", "swipe", "button", "key", "quiet", "act", "attach", "detach", "companion"]) {
    const reply = await requestDaemon(status.socket, { id: op, op, x: 1, y: 1 });
    assert.equal(reply.ok, false, op);
    assert.equal(reply.error.code, "companion_too_old", op);
    assert.equal(reply.error.version, "1.1.8", op);
    assert.equal(reply.error.stage, "before_send", op);
    assert.match(reply.error.hint, /brew upgrade idb-companion/);
  }
  const statusReply = await requestDaemon(status.socket, { id: "s", op: "status" });
  assert.equal(statusReply.ok, true);
  assert.equal(statusReply.status, "unsupported");
  assert.equal(statusReply.companion_version, "1.1.8");
  assert.equal(world.companionPids().length, 0, "no companion was started for an old version");
  assert.equal(world.hid().length, 0);
});

test("ID-04: a companion that answers accessibility_quiescence with UNIMPLEMENTED is unsupported and stopped", async (t) => {
  const world = harness(t, { version: "1.6.4", quiescence: "unimplemented" });
  const status = await world.start();
  assert.equal(status.status, "unsupported");
  assert.equal(status.reason, "companion_too_old");
  assert.equal(status.companion, null);
  const [companion] = world.companionPids();
  await waitFor(() => !pidAlive(companion), { timeoutMs: 5000, message: "the probed companion to stop" });
  assert.deepEqual(world.idbCalls().map((c) => c.argv[0]), ["connect", "disconnect"]);
  const reply = await requestDaemon(status.socket, { id: "t", op: "tree" });
  assert.equal(reply.error.code, "companion_too_old");
});

// ---------------------------------------------------------------------------
// ID-05: request protocol
// ---------------------------------------------------------------------------

test("ID-05-S01: malformed and unknown requests get invalid_request and no connection closes", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const one = await world.connect(status);
  const two = await world.connect(status);
  const [notJson, statusReply] = await Promise.all([one.sendRaw("not json"), two.request({ id: "9", op: "status" })]);
  const fly = await one.request({ id: "2", op: "fly" });
  assert.deepEqual([notJson.id, notJson.ok, notJson.error.code, notJson.error.stage], [null, false, "invalid_request", "before_send"]);
  assert.equal(typeof notJson.error.message, "string");
  assert.deepEqual([fly.id, fly.ok, fly.error.code, fly.error.stage], ["2", false, "invalid_request", "before_send"]);
  assert.deepEqual([statusReply.id, statusReply.ok], ["9", true]);
  assert.equal(statusReply.schema, IOSD_SCHEMA);
  const missingId = await one.sendRaw(JSON.stringify({ op: "status" }));
  assert.equal(missingId.error.code, "invalid_request");
  const badTap = await one.request({ id: "b", op: "tap", x: -5, y: 1 });
  assert.deepEqual([badTap.ok, badTap.error.code, badTap.error.stage], [false, "invalid_request", "before_send"]);
  const badBackend = await one.request({ id: "c", op: "tree", ax_backend: "dom" });
  assert.equal(badBackend.error.code, "invalid_request");
  assert.equal(one.open, true);
  assert.equal(two.open, true);
  assert.equal((await one.request({ id: "3", op: "status" })).ok, true, "client 1 still works");
  assert.equal(world.hid().filter((e) => e?.touch).length, 0, "nothing invalid reached the device");
});

test("ID-05: an oversized line is refused and the connection keeps working", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const client = await world.connect(status);
  const reply = await client.sendRaw(`{"id":"big","op":"status","pad":"${"x".repeat(MAX_LINE_BYTES + 10)}"}`, { timeoutMs: 15_000 });
  assert.equal(reply.error.code, "invalid_request");
  assert.match(reply.error.message, /longer than/);
  assert.equal((await client.request({ id: "after", op: "status" })).ok, true);
});

test("ID-05-S02: two clients tapping at once get two complete down/up pairs, never interleaved", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const one = await world.connect(status);
  const two = await world.connect(status);
  const [a, b] = await Promise.all([
    one.request({ op: "tap", x: 10, y: 10, duration: 0.15 }),
    two.request({ op: "tap", x: 20, y: 20, duration: 0.15 }),
  ]);
  assert.deepEqual([a.ok, a.sent, b.ok, b.sent], [true, true, true, true]);
  const pairs = await recorded(() => tapPairs(world.hid()), (list) => list.length >= 4);
  assert.equal(pairs.length, 4);
  const firstPoint = pairs[0].split("@")[1];
  const secondPoint = pairs[2].split("@")[1];
  assert.deepEqual(pairs, [`down@${firstPoint}`, `up@${firstPoint}`, `down@${secondPoint}`, `up@${secondPoint}`]);
  assert.deepEqual(new Set([firstPoint, secondPoint]), new Set(["10,10", "20,20"]));
  // A read runs beside a long input instead of waiting behind it.
  const started = Date.now();
  const [, tree] = await Promise.all([
    one.request({ op: "tap", x: 30, y: 30, duration: 1 }),
    (async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const reply = await two.request({ op: "tree" });
      return { reply, at: Date.now() - started };
    })(),
  ]);
  assert.equal(tree.reply.ok, true);
  assert.ok(tree.at < 900, `the read waited for the input (${tree.at} ms)`);
});

test("ID-05: button, key and swipe are sent in full and answered with sent: true", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const client = await world.connect(status);
  assert.equal((await client.request({ op: "button", name: "HOME" })).sent, true);
  assert.equal((await client.request({ op: "key", code: 40 })).sent, true);
  assert.equal((await client.request({ op: "swipe", x1: 100, y1: 600, x2: 100, y2: 200, duration: 0.1 })).sent, true);
  const events = await recorded(() => world.hid(), (list) => list.some((e) => e?.touch && e.direction === "up"));
  assert.deepEqual(events.filter((e) => e?.button), [{ button: "HOME", direction: "down" }, { button: "HOME", direction: "up" }]);
  assert.deepEqual(events.filter((e) => e?.key === 40), [{ key: 40, direction: "down" }, { key: 40, direction: "up" }]);
  const touches = events.filter((e) => e?.touch);
  assert.deepEqual(touches[0], { touch: { x: 100, y: 600 }, direction: "down" });
  assert.deepEqual(touches.at(-1), { touch: { x: 100, y: 200 }, direction: "up" });
  assert.ok(touches.length >= 3, "the finger moves between start and end");
  assert.equal(touches.filter((e) => e.direction === "up").length, 1);
  const companion = await client.request({ op: "companion" });
  assert.deepEqual([companion.socket, companion.pid, companion.owned], [status.companion.socket, status.companion.pid, true]);
});

// ---------------------------------------------------------------------------
// ID-09: ax by default, axbridge for web content
// ---------------------------------------------------------------------------

test("ID-09: a native screen is read once with ax; a web page is read again with axbridge and the interactable filter", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const client = await world.connect(status);
  const native = await client.request({ op: "tree" });
  assert.equal(native.ok, true);
  assert.equal(native.ax_backend, "ax");
  assert.equal(native.json, SETTINGS, "the companion's JSON is passed through unchanged");
  assert.ok(native.coverage >= COVERAGE_THRESHOLD);
  assert.deepEqual(world.reads().map((r) => [r.backend, r.filter, r.format]), [["AX", "ALL", "LEGACY"]]);
});

test("ID-09-S02: Safari's page text comes from axbridge", async (t) => {
  const world = harness(t, { ax: { AX: SAFARI_AX, AXBRIDGE: SAFARI_AXBRIDGE } });
  const status = await world.start();
  const client = await world.connect(status);
  const web = await client.request({ op: "tree" });
  assert.equal(web.ok, true);
  assert.equal(web.ax_backend, "axbridge");
  assert.equal(web.ax_reason, "web_content_app");
  assert.match(web.json, /Example Domain/);
  assert.deepEqual(world.reads().map((r) => [r.backend, r.filter]), [["AX", "ALL"], ["AXBRIDGE", "INTERACTABLE"]]);
  const forced = await client.request({ op: "tree", ax_backend: "ax" });
  assert.equal(forced.ax_backend, "ax", "an explicit backend is kept");
  const bridged = await client.request({ op: "tree", ax_backend: "axbridge", filter: "all" });
  assert.equal(bridged.ax_backend, "axbridge");
  assert.deepEqual(world.reads().slice(2).map((r) => [r.backend, r.filter]), [["AX", "ALL"], ["AXBRIDGE", "ALL"]]);
});

test("ID-09: an ax read failing with 'No translation object' is read with axbridge", async (t) => {
  const world = harness(t, { axError: { AX: "No translation object returned for the frontmost application" } });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "t", op: "tree" });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.ax_backend, "axbridge");
  assert.equal(reply.ax_reason, "no_translation_object");
  assert.deepEqual(world.reads().map((r) => r.backend), ["AX", "AXBRIDGE"]);
});

// ---------------------------------------------------------------------------
// ID-10: settle and confirmed input on the observe path
// ---------------------------------------------------------------------------

test("ID-10: an act sends once, confirms through touches_completed, waits the grace period and reads once", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const client = await world.connect(status);
  const started = Date.now();
  const reply = await client.request({ op: "act", input: { op: "tap", x: 50, y: 400 } });
  const elapsed = Date.now() - started;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true);
  assert.equal(reply.confirmed_by, "touches_completed");
  assert.equal(reply.method, "quiet-stream");
  assert.equal(reply.settled, true);
  assert.deepEqual(reply.warnings, []);
  assert.equal(reply.ax_backend, "ax");
  assert.equal(reply.json, SETTINGS);
  assert.ok(reply.waited_ms >= GRACE_MS - 20, `grace ${reply.waited_ms}`);
  assert.ok(elapsed < 1000, `ID-10-S02: no quiet event, reply within 1 s (${elapsed} ms)`);
  await assertRecorded(() => tapPairs(world.hid()), ["down@50,400", "up@50,400"]);
  assert.equal(world.reads().length, 1);
});

test("ID-10: an app that turns busy after the touch is read only once it is quiet again", async (t) => {
  const world = harness(t, { busyMs: 600 });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 } });
  assert.equal(reply.ok, true);
  assert.equal(reply.input_confirmed, true);
  assert.equal(reply.settled, true);
  assert.ok(reply.waited_ms >= 600, `waited ${reply.waited_ms}`);
  assert.ok(reply.waited_ms < OBSERVE_TIMEOUT_MS);
  const busyLong = harness(t, { busyMs: 5000 });
  const other = await busyLong.start();
  const capped = await requestDaemon(other.socket, { id: "b", op: "act", input: { op: "tap", x: 5, y: 5 }, timeout_ms: 800 });
  assert.equal(capped.ok, true);
  assert.equal(capped.settled, false, "the observe timeout bounds the wait");
  assert.ok(capped.waited_ms < 1500);
  assert.ok(capped.json, "the screen is still returned");
});

test("ID-10-S03: input that never reaches the app keeps ok true, says input_unconfirmed and sends once", async (t) => {
  const world = harness(t, { touches: false });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 10, y: 20 } });
  assert.equal(reply.ok, true);
  assert.equal(reply.input_confirmed, false);
  assert.equal(reply.method, "quiet-stream");
  assert.equal(reply.warnings.length, 1);
  assert.equal(reply.warnings[0].code, "input_unconfirmed");
  assert.match(reply.warnings[0].hint, /sent once/);
  assert.ok(reply.json, "the screen is still returned");
  await assertRecorded(() => tapPairs(world.hid()), ["down@10,20", "up@10,20"], "exactly one down/up pair");
  assert.deepEqual(world.xcrunCalls(), [["simctl", "spawn", UDID, "notifyutil", "-g", DTUHIDD_FLAG]]);
});

test("ID-10: a touches_completed of an earlier tap does not confirm a later act", async (t) => {
  // The first tap is reported 150 ms after it ends; the act's own tap is never reported.
  const world = harness(t, { touchLimit: 1, touchDelayMs: 150 });
  const status = await world.start();
  const client = await world.connect(status);
  const tap = await client.request({ op: "tap", x: 10, y: 10 });
  assert.equal(tap.ok, true);
  const reply = await client.request({ op: "act", input: { op: "tap", x: 20, y: 20 }, read: false });
  assert.equal(reply.ok, true);
  assert.equal(reply.input_confirmed, false);
  assert.equal(reply.confirmed_by, null);
  assert.deepEqual(reply.warnings.map((w) => w.code), ["input_unconfirmed"]);
  assert.equal(world.xcrunCalls().length, 1, "the ID-12 cause check ran");
  await assertRecorded(() => tapPairs(world.hid()), ["down@10,10", "up@10,10", "down@20,20", "up@20,20"]);
});

test("ID-05-S02/ID-10: an act queued behind another client's tap confirms only through its own touch", async (t) => {
  const run = async (config) => {
    const world = harness(t, config);
    const status = await world.start();
    const one = await world.connect(status);
    const two = await world.connect(status);
    const tap = one.request({ op: "tap", x: 10, y: 10, duration: 0.2 });
    await new Promise((resolve) => setTimeout(resolve, 50));
    const act = await two.request({ op: "act", input: { op: "tap", x: 20, y: 20 }, read: false });
    assert.equal((await tap).ok, true);
    await assertRecorded(() => tapPairs(world.hid()), ["down@10,10", "up@10,10", "down@20,20", "up@20,20"], "the act waited its turn");
    return act;
  };
  const lost = await run({ touchLimit: 1 });
  assert.equal(lost.ok, true);
  assert.equal(lost.input_confirmed, false, "the first tap's completion is not the act's");
  assert.deepEqual(lost.warnings.map((w) => w.code), ["input_unconfirmed"]);
  const both = await run({});
  assert.equal(both.input_confirmed, true, "with both touches reported the act is confirmed");
  assert.equal(both.confirmed_by, "touches_completed");
  assert.deepEqual(both.warnings, []);
});

// ---------------------------------------------------------------------------
// ID-10: the first observed touch on a new companion (a cold quiet stream)
// ---------------------------------------------------------------------------

/** The daemon log's records of one event. */
function logged(world, event) {
  return jsonLines(daemonPaths(world.paths.base, UDID).log).filter((record) => record.event === event);
}

/** Waits (bounded) until the daemon has logged `count` records of the event, then returns all of them. */
async function loggedAtLeast(world, event, count = 1, { timeoutMs = 8000 } = {}) {
  await waitFor(() => logged(world, event).length >= count, { timeoutMs, message: `${count} "${event}" record(s) in the daemon log` });
  return logged(world, event);
}

/** Waits (bounded) until the registry satisfies `check`, then returns it. */
function registryWhen(world, check, message, { timeoutMs = 8000 } = {}) {
  return waitFor(() => {
    const registry = world.registry();
    return registry && check(registry) ? registry : null;
  }, { timeoutMs, message });
}

/**
 * Waits (bounded) until the daemon itself holds the app BUSY (its status' app_state), not
 * only the fake that sent the report: a test that times QUIET_STUCK_MS, or opens a quiet
 * wait meant to hold the idle check away, starts from the daemon's receipt, so a slow host
 * cannot let the wait see a QUIET app and return at once, or the touch come too early.
 */
async function daemonSawBusy(status, message = "the daemon to hold the BUSY report") {
  await waitFor(async () => (await requestDaemon(status.socket, { id: "busy", op: "status" }))?.app_state === "busy", { message });
}

/** Waits for the `n`th stuck replacement (it is logged once the new companion serves); returns its pid. */
async function replacedServing(world, n, timeoutMs) {
  const records = await loggedAtLeast(world, "companion_restarted", n, { timeoutMs });
  return records[n - 1].pid;
}

test("ID-10: the start waits for the quiet stream's first event, so the first act right after it is confirmed", async (t) => {
  // The companion's stream reports no touch before its first event, 700 ms after the call opened.
  const world = harness(t, { quietColdMs: 700 });
  const startedAt = Date.now();
  const status = await world.start();
  assert.equal(status.status, "serving");
  assert.ok(Date.now() - startedAt >= 650, "serving only once the stream sent its first event");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 4, y: 4 } });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true);
  assert.equal(reply.confirmed_by, "touches_completed");
  assert.deepEqual(reply.warnings, []);
  const [warm] = await loggedAtLeast(world, "warm");
  assert.equal(warm.quiet_armed, true);
  assert.ok(warm.quiet_ms >= 600, `waited ${warm.quiet_ms} ms for the first event`);
  assert.deepEqual(world.events().filter((e) => e.k === "touch_up").map((e) => e.warm), [1], "the touch met a warm stream");
});

test("ID-10: a touch that finds the quiet stream reopened and cold waits for its first event and is confirmed", async (t) => {
  // The first call ends after 1 s; every call sends its first event 600 ms after it opened.
  const world = harness(t, { quietEndMs: 1000, quietColdMs: 600 });
  const status = await world.start();
  await waitFor(() => world.events().filter((e) => e.k === "quiet_open").length === 2, { message: "the quiet stream to reopen" });
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true);
  const [wait] = await loggedAtLeast(world, "quiet_arm_wait");
  assert.equal(wait.armed, true);
  assert.ok(wait.ms > 0);
  assert.deepEqual(world.events().filter((e) => e.k === "touch_up").map((e) => e.warm), [1]);
  await assertRecorded(() => tapPairs(world.hid()), ["down@6,6", "up@6,6"], "sent once");
});

test("ID-10: a touch still waiting for a cold quiet stream when the companion dies is refused unsent (before_send)", { timeout: 20_000 }, async (t) => {
  const world = harness(t, { quietEndMs: 1000, quietColdMs: 600 });
  const status = await world.start();
  await waitFor(() => world.events().filter((e) => e.k === "quiet_open").length === 2, { message: "the quiet stream to reopen" });
  const pending = requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false }, { timeoutMs: 20_000 });
  await new Promise((resolve) => setTimeout(resolve, 100));
  process.kill(status.companion.pid, "SIGKILL");
  const killedAt = Date.now();
  const reply = await pending;
  assert.equal(reply.ok, false, JSON.stringify(reply));
  assert.equal(reply.error.stage, STAGE.BEFORE_SEND, "nothing of it was written");
  assert.equal(reply.error.code, CODE.DAEMON_UNREACHABLE);
  assert.ok(Date.now() - killedAt < 1500, "the wait ended with the connection");
  assert.deepEqual(tapPairs(world.hid()), [], "the tap was never sent");
});

test("ID-10: a stop while the start waits for a silent quiet stream ends the wait at once", { timeout: 20_000 }, async (t) => {
  const world = harness(t, { quietSilent: true });
  const child = spawn(process.execPath, [DAEMON_SCRIPT, "--udid", UDID, "--state-base", world.paths.base],
    { env: world.env, stdio: ["ignore", "ignore", "ignore"] });
  world.daemonPids.add(child.pid);
  const exited = new Promise((resolve) => child.once("exit", resolve));
  await waitFor(() => world.events().some((e) => e.k === "quiet_open"), { message: "the quiet stream to open" });
  await new Promise((resolve) => setTimeout(resolve, 200));
  const stoppedAt = Date.now();
  process.kill(child.pid, "SIGTERM");
  await exited;
  assert.ok(Date.now() - stoppedAt < QUIET_WARM_MS - 500, `stopped in ${Date.now() - stoppedAt} ms`);
});

test("ID-10: a quiet stream that never sends an event is waited for once; its touches stay unconfirmed, never confirmed by default", { timeout: 20_000 }, async (t) => {
  const world = harness(t, { quietSilent: true });
  const startedAt = Date.now();
  const status = await world.start();
  const startMs = Date.now() - startedAt;
  assert.equal(status.status, "serving", "the daemon serves after the wait ran out");
  assert.ok(startMs >= QUIET_WARM_MS - 100, `the start waited for the first event (${startMs} ms)`);
  const [warm] = await loggedAtLeast(world, "warm");
  assert.equal(warm.quiet_armed, false);
  const client = await world.connect(status);
  const sentAt = Date.now();
  const first = await client.request({ op: "act", input: { op: "tap", x: 1, y: 1 }, read: false });
  const firstMs = Date.now() - sentAt;
  assert.equal(first.ok, true, JSON.stringify(first.error));
  assert.equal(first.input_confirmed, false, "no touches_completed came: not confirmed");
  assert.equal(first.confirmed_by, null);
  assert.deepEqual(first.warnings.map((w) => w.code), ["input_unconfirmed"]);
  assert.ok(firstMs < FIRST_TOUCH_CONFIRM_MS + GRACE_MS + 1500, `no second wait for the first event (${firstMs} ms)`);
  assert.deepEqual(logged(world, "quiet_arm_wait"), [], "the given-up stream is not waited for again");
  const second = await client.request({ op: "act", input: { op: "tap", x: 2, y: 2 }, read: false });
  assert.equal(second.input_confirmed, false);
  await assertRecorded(() => tapPairs(world.hid()), ["down@1,1", "up@1,1", "down@2,2", "up@2,2"], "each tap once");
});

test("ID-10: the first touch on a new companion has the longer confirmation window, later touches the 500 ms one", { timeout: 20_000 }, async (t) => {
  // The app reports the first touch 900 ms after it ended (cold), the second 900 ms too.
  const world = harness(t, { touchDelays: [900, 900] });
  const status = await world.start();
  const client = await world.connect(status);
  const first = await client.request({ op: "act", input: { op: "tap", x: 3, y: 3 }, read: false });
  assert.equal(first.ok, true, JSON.stringify(first.error));
  assert.equal(first.input_confirmed, true, "reported within FIRST_TOUCH_CONFIRM_MS");
  assert.deepEqual(first.warnings, []);
  // The first touch's late report is counted for it, never for the next one.
  const second = await client.request({ op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(second.input_confirmed, false, "a later touch reported after 900 ms is outside CONFIRM_MS");
  assert.deepEqual(second.warnings.map((w) => w.code), ["input_unconfirmed"]);
  // An explicit confirm_ms is kept as it is, also for the first touch.
  const other = harness(t, { touchDelays: [900] });
  const otherStatus = await other.start();
  const explicit = await requestDaemon(otherStatus.socket, { id: "e", op: "act", input: { op: "tap", x: 5, y: 5 }, confirm_ms: 500, read: false });
  assert.equal(explicit.input_confirmed, false);
  const windows = (await loggedAtLeast(world, "act_timing", 2)).map((record) => record.window_ms);
  assert.deepEqual(windows, [FIRST_TOUCH_CONFIRM_MS, CONFIRM_MS]);
});

test("ID-10: the first touch after the quiet stream reports a new target gets the longer window again; a late report is logged", { timeout: 20_000 }, async (t) => {
  // The first touch is reported at once; then another app comes to the front (target_changed);
  // the app reports the first touch in it 900 ms late, the next one too (outside CONFIRM_MS).
  const world = harness(t, { touchDelays: [30, 900, 900], targetChangeAfterTouch: 1 });
  const status = await world.start();
  const client = await world.connect(status);
  const first = await client.request({ op: "act", input: { op: "tap", x: 3, y: 3 }, read: false });
  assert.equal(first.input_confirmed, true, JSON.stringify(first));
  await waitFor(() => world.events().some((e) => e.k === "target_changed"), { message: "the target change" });
  await waitFor(() => logged(world, "quiet_target").length === 1, { message: "the daemon to see the target change" });
  const second = await client.request({ op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(second.ok, true, JSON.stringify(second.error));
  assert.equal(second.input_confirmed, true, "the first touch on the new target is reported within FIRST_TOUCH_CONFIRM_MS");
  const third = await client.request({ op: "act", input: { op: "tap", x: 5, y: 5 }, read: false });
  assert.equal(third.input_confirmed, false, "a later touch on the same target has CONFIRM_MS: never confirmed by a late report");
  assert.deepEqual(third.warnings.map((w) => w.code), ["input_unconfirmed"]);
  assert.deepEqual((await loggedAtLeast(world, "act_timing", 3)).map((record) => record.window_ms),
    [FIRST_TOUCH_CONFIRM_MS, FIRST_TOUCH_CONFIRM_MS, CONFIRM_MS]);
  // The third touch's report, 900 ms after it, came after its window: logged, counted for nothing.
  await waitFor(() => logged(world, "touch_late").length === 1, { message: "the late report to be logged" });
  const [late] = await loggedAtLeast(world, "touch_late");
  assert.equal(late.window_ms, CONFIRM_MS);
  assert.ok(late.ms >= CONFIRM_MS, JSON.stringify(late));
  await assertRecorded(() => tapPairs(world.hid()), ["down@3,3", "up@3,3", "down@4,4", "up@4,4", "down@5,5", "up@5,5"], "each tap once");
});

test("ID-10: a stuck quiet stream gets its owned companion replaced while idle once two probes in a row stay silent; the next touch is reported", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300 });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  // QUIET_STUCK_MS after the stuck event, with nothing in flight, a probe; silent, a second
  // one at once on its own new stream; silent too, the companion is replaced.
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 8000, message: "the replacement" });
  const probes = await loggedAtLeast(world, "quiet_probe", STUCK_SILENT_PROBES);
  assert.equal(probes.length, STUCK_SILENT_PROBES, JSON.stringify(probes));
  // Each probe stream got only the stuck companion's BUSY with RUN_LOOP_IDLE pending: no proof of health.
  assert.deepEqual(probes.map((p) => [p.when, p.result, p.busy_reports, p.limit_ms, p.silent_run]),
    [["idle", "silent", 1, QUIET_STUCK_PROBE_MS, 1], ["idle", "silent", 1, QUIET_STUCK_PROBE_MS, 2]], JSON.stringify(probes));
  for (const probe of probes) assert.ok(probe.ms >= QUIET_STUCK_PROBE_MS - 20, JSON.stringify(probe));
  // Each on its own new quiet call of the stuck companion (the daemon's stream, then two probes).
  assert.equal(world.events().filter((e) => e.k === "quiet_open" && e.pid === first).length, 1 + STUCK_SILENT_PROBES);
  const [stuckRecord] = await loggedAtLeast(world, "quiet_stuck");
  assert.equal(stuckRecord.when, "idle");
  assert.equal(stuckRecord.replace, true);
  assert.equal(stuckRecord.silent_probes, STUCK_SILENT_PROBES);
  assert.ok(stuckRecord.silent_ms >= QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS - 50, JSON.stringify(stuckRecord));
  assert.equal(stuckRecord.probe_ms, probes[1].ms);
  assert.deepEqual(stuckRecord.signals, ["ANIMATIONS_INACTIVE", QUIET_STUCK_SIGNAL]);
  assert.equal(logged(world, "companion_restarted")[0].reason, "quiet_stuck");
  await waitFor(() => !pidAlive(first), { message: "the stuck companion to stop" });
  assert.ok(world.idbCalls().some((c) => c.argv[0] === "disconnect"), "idb disconnect for the owned one");
  const registry = await registryWhen(world, (r) => r.companion?.pid !== first && r.state === "serving", "the registry to name the new companion");
  assert.notEqual(registry.companion.pid, first);
  assert.equal(registry.state, "serving");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "reported on the new companion");
  await assertRecorded(() => tapPairs(world.hid()), ["down@4,4", "up@4,4"], "sent once");
});

test("ID-10: a touch that finds the stream stuck probes it and, silent twice in a row, replaces the owned companion before it is written", { timeout: 30_000 }, async (t) => {
  // This time the stuck companion's new stream sends nothing at all (seen live too).
  const world = harness(t, { stuckAfterMs: 300, stuckProbeSilent: true });
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  // A quiet wait in flight (the app is busy) keeps the idle check away; the touch
  // comes after QUIET_STUCK_MS.
  // (1 s of margin: the daemon must have counted QUIET_STUCK_MS since the BUSY report it
  // received a little after the fake sent it, also on a busy host.)
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  assert.equal(logged(world, "companion_restarted").length, 0, "not replaced while a request ran");
  assert.equal(logged(world, "quiet_probe").length, 0, "not probed while a request ran");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the touch went to the new companion and was reported");
  const probes = await loggedAtLeast(world, "quiet_probe", STUCK_SILENT_PROBES);
  assert.deepEqual(probes.map((p) => [p.when, p.result, p.busy_reports ?? 0, p.limit_ms, p.silent_run]),
    [["touch", "silent", 0, QUIET_STUCK_PROBE_MS, 1], ["touch", "silent", 0, QUIET_STUCK_PROBE_MS, 2]], JSON.stringify(probes));
  const [stuckRecord] = await loggedAtLeast(world, "quiet_stuck");
  assert.equal(stuckRecord.when, "touch");
  assert.equal(stuckRecord.replace, true);
  assert.equal(stuckRecord.silent_probes, STUCK_SILENT_PROBES);
  await registryWhen(world, (r) => r.companion?.pid !== first, "the registry to name the new companion");
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => [e.pid !== first, e.stuck]), [[true, false]], "written once, on the new companion only");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  await waiting.catch(() => {});
});

test("REV-055: a healthy companion silent after BUSY with RUN_LOOP_IDLE pending answers the probe: kept while idle, and the next tap is confirmed at once", { timeout: 30_000 }, async (t) => {
  // The probe stream's first event comes late (1 s), still within QUIET_STUCK_PROBE_MS.
  const world = harness(t, { healthySilentAfterMs: 300, probeColdMs: 1000 });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().some((e) => e.k === "healthy_silent"), { message: "the companion to go silent" });
  const [probe] = await loggedAtLeast(world, "quiet_probe", 1, { timeoutMs: QUIET_STUCK_MS + QUIET_STUCK_PROBE_MS + 3000 });
  assert.deepEqual([probe.when, probe.result], ["idle", "ok"], JSON.stringify(probe));
  assert.deepEqual(probe.first, { type: "state_changed", state: "QUIET", busy_signals: [] });
  assert.ok(probe.ms >= 900 && probe.ms < QUIET_STUCK_PROBE_MS, JSON.stringify(probe));
  // Still silent several windows later: no second idle probe within the interval, no replacement.
  await new Promise((resolve) => setTimeout(resolve, 2 * QUIET_STUCK_MS));
  assert.equal(logged(world, "quiet_probe").length, 1, "idle probes at most once per interval");
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "companion_restarted").length, 0);
  // The tap: probed again (the answer is older than STUCK_PROBE_FRESH_MS), ok, then sent and reported.
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  const ms = performance.now() - t0;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the same companion reports it");
  // One probe and the confirmation window, plus the reply slack for a loaded host.
  assert.ok(ms < QUIET_STUCK_PROBE_MS + FIRST_TOUCH_CONFIRM_MS + INPUT_REPLY_SLACK_MS, `the tap took ${Math.round(ms)} ms`);
  const probes = logged(world, "quiet_probe");
  assert.deepEqual(probes.map((p) => [p.when, p.result]), [["idle", "ok"], ["touch", "ok"]]);
  assert.equal(world.registry().companion.pid, first);
  assert.ok(pidAlive(first));
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "companion_restarted").length, 0);
  await assertRecorded(() => tapPairs(world.hid()), ["down@6,6", "up@6,6"], "sent once");
});

test("REV-055: a report on the open stream while a probe waits ends the probe as healthy: no replacement", { timeout: 30_000 }, async (t) => {
  // The probe stream would answer only after 5 s (past QUIET_STUCK_PROBE_MS); a new app
  // reports on the open stream 0.5 s into the probe.
  const world = harness(t, { healthySilentAfterMs: 300, probeColdMs: 5000 });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().filter((e) => e.k === "quiet_open" && e.pid === first).length === 2, { timeoutMs: QUIET_STUCK_MS + 5000, message: "the probe stream to open" });
  await new Promise((resolve) => setTimeout(resolve, 500));
  process.kill(first, "SIGUSR1");
  const [probe] = await loggedAtLeast(world, "quiet_probe");
  assert.equal(probe.result, "cleared", JSON.stringify(probe));
  assert.ok(probe.ms < QUIET_STUCK_PROBE_MS, JSON.stringify(probe));
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_PROBE_MS));
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "companion_restarted").length, 0);
  assert.equal(world.registry().companion.pid, first);
});

test("REV-055: a touch on a healthy companion whose probe just answered is not probed again", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { healthySilentAfterMs: 300 });
  const status = await world.start();
  await loggedAtLeast(world, "quiet_probe", 1, { timeoutMs: QUIET_STUCK_MS + QUIET_STUCK_PROBE_MS + 3000 });
  // Within STUCK_PROBE_FRESH_MS of the idle probe's ok.
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true);
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result]), [["idle", "ok"]]);
  assert.equal(logged(world, "companion_restarted").length, 0);
});

test("REV-056: a touch whose deadline leaves no room for both probes and a replacement is not probed: sent once on the current companion, reported as it is; the idle check replaces it after", { timeout: 45_000 }, async (t) => {
  const limit = 2500;
  const world = harness(t, { stuckAfterMs: 300, stuckProbeSilent: true });
  world.env[HID_WAIT_ENV] = String(limit);
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  // A quiet wait in flight keeps the idle check away; the touch comes after QUIET_STUCK_MS.
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  const ms = performance.now() - t0;
  // limit - ARM_WAIT_SPARE_MS is less than two probes and a replacement: nothing probed,
  // the touch written once on the stuck companion, truthfully unconfirmed, by its deadline.
  assert.ok(limit - ARM_WAIT_SPARE_MS < STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + RESTART_WAIT_MS);
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "the stuck companion reported nothing");
  assert.deepEqual(reply.warnings.map((w) => w.code), ["input_unconfirmed"]);
  assert.ok(ms < limit + INPUT_REPLY_SLACK_MS, `answered after ${Math.round(ms)} ms`);
  assert.deepEqual(logged(world, "quiet_suspect").map((r) => [r.when, r.reason]), [["touch", "deadline"]]);
  assert.equal(logged(world, "quiet_probe").filter((p) => p.when === "touch").length, 0, "not probed before the touch");
  assert.equal(logged(world, "quiet_stuck").filter((r) => r.when === "touch").length, 0, "not replaced before the touch");
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => [e.pid === first, e.stuck]), [[true, true]], "written once, on the stuck companion");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  // Once nothing runs the idle check carries on: two silent probes, then the replacement.
  await waiting.catch(() => {});
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 10_000, message: "the idle replacement" });
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result, p.silent_run]), [["idle", "silent", 1], ["idle", "silent", 2]]);
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.when, r.replace]), [["idle", true]]);
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "never sent again");
});

test("REV-056: a stuck check before a touch runs both probes at full length inside the deadline; a replacement that then does not fit refuses the touch unsent", { timeout: 60_000 }, async (t) => {
  // Room for two full probes and RESTART_WAIT_MS with 1 s to spare at each step.
  const limit = ARM_WAIT_SPARE_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + RESTART_WAIT_MS + 1000;
  // The new companion's idb connect returns only after 9 s: longer than what the deadline
  // leaves after both probes, so a wait for it past the deadline would show.
  const world = harness(t, { stuckAfterMs: 300, stuckProbeSilent: true, idb: { laterConnectDelayMs: 9000 } });
  world.env[HID_WAIT_ENV] = String(limit);
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  const ms = performance.now() - t0;
  const probes = logged(world, "quiet_probe").filter((p) => p.when === "touch");
  assert.deepEqual(probes.map((p) => [p.result, p.limit_ms, p.silent_run]),
    [["silent", QUIET_STUCK_PROBE_MS, 1], ["silent", QUIET_STUCK_PROBE_MS, 2]], JSON.stringify(probes));
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.when, r.replace, r.silent_probes]), [["touch", true, STUCK_SILENT_PROBES]]);
  // Both probes ran before the deadline; the replacement did not fit in what was left:
  // refused before anything was written, answered by the deadline (and its slack), never later.
  assert.ok(ms >= STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS, `answered after ${Math.round(ms)} ms`);
  assert.equal(reply.ok, false, JSON.stringify(reply));
  assert.equal(reply.error.stage, "before_send", JSON.stringify(reply.error));
  assert.ok(ms < limit + INPUT_REPLY_SLACK_MS, `answered after ${Math.round(ms)} ms`);
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.deepEqual(tapPairs(world.hid()), [], "nothing of the touch was sent");
  assert.ok(world.events().filter((e) => e.k === "touch_up").length === 0);
  await registryWhen(world, (r) => r.companion?.pid !== first && r.state === "serving", "the replacement to finish anyway", { timeoutMs: 15_000 });
  await waiting.catch(() => {});
});

test("REV-056: one silent probe and then an ok one keep a healthy companion while idle: no replacement", { timeout: 30_000 }, async (t) => {
  // The healthy companion's first probe stream (its second quiet call) stays silent; the next answers.
  const world = harness(t, { healthySilentAfterMs: 300, probeSilentCalls: [2] });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().some((e) => e.k === "healthy_silent"), { message: "the companion to go silent" });
  const probes = await loggedAtLeast(world, "quiet_probe", 2, { timeoutMs: QUIET_STUCK_MS + 2 * QUIET_STUCK_PROBE_MS + 4000 });
  assert.deepEqual(probes.map((p) => [p.when, p.result, p.silent_run ?? 0]), [["idle", "silent", 1], ["idle", "ok", 0]], JSON.stringify(probes));
  assert.ok(world.events().some((e) => e.k === "quiet_silent_call" && e.call === 2), "the first probe stream was the silent one");
  // Still silent windows later: the ok answer ended the run; no probe within the interval, no replacement.
  await new Promise((resolve) => setTimeout(resolve, 2 * QUIET_STUCK_MS));
  assert.equal(logged(world, "quiet_probe").length, 2);
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "companion_restarted").length, 0);
  assert.equal(world.registry().companion.pid, first);
  assert.ok(pidAlive(first));
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the same companion reports it");
  assert.equal(logged(world, "companion_restarted").length, 0);
});

test("REV-056: before a touch, one silent probe and then an ok one keep a healthy companion; the touch is confirmed on it", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { healthySilentAfterMs: 300, probeSilentCalls: [2] });
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => world.events().some((e) => e.k === "healthy_silent"), { message: "the companion to go silent" });
  // A quiet wait in flight keeps the idle check away; the touch comes after QUIET_STUCK_MS.
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  assert.equal(logged(world, "quiet_probe").length, 0, "not probed while a request ran");
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false }, { timeoutMs: 25_000 });
  const ms = performance.now() - t0;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the same companion reports it");
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result, p.silent_run ?? 0]), [["touch", "silent", 1], ["touch", "ok", 0]]);
  // Two probes and the confirmation window, plus the reply slack for a loaded host (the probe list above is the exact proof).
  assert.ok(ms < 2 * QUIET_STUCK_PROBE_MS + FIRST_TOUCH_CONFIRM_MS + INPUT_REPLY_SLACK_MS, `the tap took ${Math.round(ms)} ms`);
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "companion_restarted").length, 0);
  assert.equal(world.registry().companion.pid, first);
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => e.pid === first), [true], "written once, on the same companion");
  await assertRecorded(() => tapPairs(world.hid()), ["down@6,6", "up@6,6"], "sent once");
  await waiting.catch(() => {});
});

test("REV-055: a request that arrives while an idle probe runs is let finish before a silent probe replaces the companion", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300 });
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  // The idle probe opens the companion's second quiet call; a quiet wait arrives during it.
  await waitFor(() => world.events().filter((e) => e.k === "quiet_open" && e.pid === first).length === 2, { timeoutMs: QUIET_STUCK_MS + 5000, message: "the idle probe to open" });
  const reply = await client.request({ op: "quiet", timeout_ms: QUIET_STUCK_PROBE_MS + 1000, quiet_ms: 250 });
  assert.equal(reply.ok, true, JSON.stringify(reply));
  const [probe] = logged(world, "quiet_probe");
  assert.deepEqual([probe?.when, probe?.result], ["idle", "silent"], "the probe ended while the request ran");
  assert.equal(logged(world, "companion_restarted").length, 0, "not replaced while the request ran");
  assert.ok(pidAlive(first));
  // Once it finished, the probe's fresh silent answer is followed by the second probe at
  // once (not held by the idle interval); silent too, the companion is replaced.
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: QUIET_STUCK_PROBE_MS + 8000, message: "the replacement" });
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result, p.silent_run]), [["idle", "silent", 1], ["idle", "silent", 2]]);
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.when, r.replace, r.silent_probes]), [["idle", true, STUCK_SILENT_PROBES]]);
});

// The fake's `stuck` records (of one companion process when pid is given).
function stuckAt(world, pid) {
  return world.events().filter((e) => e.k === "stuck" && (pid === undefined || e.pid === pid));
}

test("REV-043: target changes from a stuck companion keep it stuck; it is replaced while idle and the next touch is reported", { timeout: 30_000 }, async (t) => {
  // Stuck 300 ms after start, then two apps launched on it, 1 s and 1.5 s after the stuck report.
  const world = harness(t, { stuckAfterMs: 300, stuckTargetChanges: [1000, 1500] });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().filter((e) => e.k === "target_changed").length === 2, { message: "both target changes" });
  // The fake sent them; the daemon logs each when it reads it (later, on a busy host).
  assert.ok((await loggedAtLeast(world, "quiet_target", 2)).length >= 2, "the daemon saw the target changes");
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 6000, message: "the replacement" });
  const [probe, second] = await loggedAtLeast(world, "quiet_probe", STUCK_SILENT_PROBES);
  // Silence is counted from the BUSY report, not from the last target change: the probe
  // began QUIET_STUCK_MS after the report (1.5 s later had the target change counted).
  const silentAtProbe = probe.silent_ms - probe.ms;
  assert.ok(silentAtProbe >= QUIET_STUCK_MS && silentAtProbe < QUIET_STUCK_MS + 1200, JSON.stringify(probe));
  assert.deepEqual([probe.result, second.result, second.silent_run], ["silent", "silent", 2]);
  const [stuckRecord] = await loggedAtLeast(world, "quiet_stuck");
  assert.equal(stuckRecord.when, "idle");
  assert.equal(stuckRecord.replace, true);
  const proof = QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS;
  assert.ok(stuckRecord.silent_ms >= proof - 50 && stuckRecord.silent_ms < proof + 2500, JSON.stringify(stuckRecord));
  assert.deepEqual(stuckRecord.signals, ["ANIMATIONS_INACTIVE", QUIET_STUCK_SIGNAL]);
  await registryWhen(world, (r) => r.companion?.pid !== first, "the registry to name the new companion");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "reported on the new companion");
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => [e.pid !== first, e.stuck]), [[true, false]], "written once, on the new companion only");
});

test("REV-055: apps launched on a stuck companion, each with one BUSY report, neither end its silence nor void its probe", { timeout: 30_000 }, async (t) => {
  // As live: every app launched on the stuck companion reports target_changed and one BUSY
  // with RUN_LOOP_IDLE pending, every 1.5 s; none of them ends the suspicion.
  // They go on far past the expected replacement (about 300 + QUIET_STUCK_MS + 2 probes = 7.3 s
  // plus the start), so a slow host does not decide the test.
  const changes = [1000, 2500, 4000, 5500, 7000, 8500, 10000, 11500, 13000, 14500, 16000, 17500, 19000];
  const world = harness(t, { stuckAfterMs: 300, stuckTargetChanges: changes, stuckTargetBusy: true });
  const t0 = performance.now();
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 12_000, message: "the replacement" });
  // Replaced about QUIET_STUCK_MS + two probes after the first BUSY, before the launches stop.
  assert.ok(performance.now() - t0 < 300 + changes.at(-1), `replaced only after ${Math.round(performance.now() - t0)} ms`);
  const probes = await loggedAtLeast(world, "quiet_probe", STUCK_SILENT_PROBES);
  assert.deepEqual(probes.map((p) => [p.result, p.silent_run]), [["silent", 1], ["silent", 2]], JSON.stringify(probes));
  for (const probe of probes) assert.ok(probe.busy_reports >= 1, JSON.stringify(probe));
  const launchedOnFirst = world.events().filter((e) => e.k === "target_changed" && e.pid === first).length;
  assert.ok(launchedOnFirst < changes.length, `${launchedOnFirst} launches before the replacement`);
  await registryWhen(world, (r) => r.companion?.pid !== first && r.state === "serving", "the registry to name the new companion");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(reply.input_confirmed, true, "reported on the new companion");
});

test("REV-043: a touch after target changes from a stuck companion replaces it before it is written", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300, stuckTargetChanges: [800, 1300] });
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => stuckAt(world).length === 1, { message: "the companion to get stuck" });
  // A quiet wait in flight keeps the idle check away; the touch comes after QUIET_STUCK_MS.
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  await waitFor(() => world.events().filter((e) => e.k === "target_changed").length === 2, { message: "both apps to come to the front" });
  assert.equal(world.events().filter((e) => e.k === "target_changed").length, 2, "both apps came to the front first");
  assert.equal(logged(world, "companion_restarted").length, 0, "not replaced while a request ran");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the touch went to the new companion and was reported");
  const probes = await loggedAtLeast(world, "quiet_probe", STUCK_SILENT_PROBES);
  assert.deepEqual(probes.map((p) => [p.when, p.result, p.silent_run]), [["touch", "silent", 1], ["touch", "silent", 2]]);
  const [stuckRecord] = await loggedAtLeast(world, "quiet_stuck");
  assert.equal(stuckRecord.when, "touch");
  assert.equal(stuckRecord.replace, true);
  await registryWhen(world, (r) => r.companion?.pid !== first, "the registry to name the new companion");
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => [e.pid !== first, e.stuck]), [[true, false]], "written once, on the new companion only");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  await waiting.catch(() => {});
});

test("REV-043: a new app that reports normally after the stuck report clears it: no probe, no replacement", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300 });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => stuckAt(world).length === 1, { message: "the companion to get stuck" });
  await new Promise((resolve) => setTimeout(resolve, 700));
  process.kill(first, "SIGUSR1");
  await waitFor(() => world.events().some((e) => e.k === "unstuck"), { message: "the new app" });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1500));
  assert.equal(logged(world, "quiet_stuck").length, 0, "a QUIET report ended the silence");
  assert.equal(logged(world, "quiet_probe").length, 0, "nothing left to probe");
  assert.equal(logged(world, "companion_restarted").length, 0);
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the same companion reports it");
  assert.equal(world.registry().companion.pid, first);
  assert.equal(logged(world, "quiet_stuck").length, 0);
  assert.equal(logged(world, "quiet_probe").length, 0);
});

test("REV-044: a BUSY report without RUN_LOOP_IDLE pending, then silence (a busy main thread), is neither probed nor replaced", { timeout: 30_000 }, async (t) => {
  const world = harness(t, {
    stuckAfterMs: 300, stuckEvent: { type: "state_changed", pid: 77, state: "BUSY", busySignals: ["ANIMATIONS_INACTIVE"] },
  });
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => stuckAt(world).length === 1, { message: "the app to go busy and silent" });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1500));
  assert.equal(logged(world, "quiet_stuck").length, 0, "not stuck while idle");
  assert.equal(logged(world, "quiet_probe").length, 0, "not suspected while idle");
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 7, y: 7 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "the busy app reported nothing");
  assert.equal(logged(world, "quiet_stuck").length, 0, "not stuck before the touch either");
  assert.equal(logged(world, "quiet_probe").length, 0, "not probed before the touch either");
  assert.equal(logged(world, "companion_restarted").length, 0);
  assert.equal(world.registry().companion.pid, first);
  assert.ok(pidAlive(first));
});

test("REV-046: a stuck episode within the interval is neither probed nor replaced until the interval has passed; then it is", { timeout: 60_000 }, async (t) => {
  const interval = 8000;
  const world = harness(t, { stuckAfterMs: 300 });
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  const first = status.companion.pid;
  // Episode 1: probed and replaced while idle.
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 8000, message: "the first replacement" });
  const replacedAt = performance.now();
  const secondRegistry = await registryWhen(world, (r) => r.companion?.pid !== first, "the registry to name the second companion");
  const second = secondRegistry.companion.pid;
  assert.notEqual(second, first);
  await waitFor(() => world.events().some((e) => e.k === "quiet_first" && e.pid === second), { message: "the new stream's first event" });
  // Episode 2, at once: within the interval it is logged as a suspicion and left.
  process.kill(second, "SIGUSR2");
  const [suspect] = await loggedAtLeast(world, "quiet_suspect", 1, { timeoutMs: QUIET_STUCK_MS + 5000 });
  assert.deepEqual([suspect.when, suspect.reason, suspect.owned], ["idle", "too_soon", true]);
  assert.ok(performance.now() - replacedAt < interval, "the second episode was noticed within the interval");
  // A touch within the interval is not probed either (no replacement could follow): sent, unconfirmed.
  const early = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 7, y: 7 }, read: false });
  assert.equal(early.ok, true, JSON.stringify(early.error));
  assert.equal(early.input_confirmed, false, "the stuck companion reported nothing");
  assert.deepEqual(logged(world, "quiet_suspect").map((r) => [r.when, r.reason]), [["idle", "too_soon"], ["touch", "too_soon"]]);
  assert.equal(logged(world, "quiet_probe").length, STUCK_SILENT_PROBES, "only episode 1 was probed");
  assert.equal(logged(world, "companion_restarted").length, 1);
  assert.equal(world.registry().companion.pid, second);
  // Once the interval since episode 1 has passed, the same episode is probed and replaced while idle.
  await waitFor(() => logged(world, "companion_restarted").length === 2, { timeoutMs: interval + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 8000, message: "the second replacement" });
  assert.ok(performance.now() - replacedAt >= interval - 200, `replaced again after ${Math.round(performance.now() - replacedAt)} ms`);
  const probes = logged(world, "quiet_probe");
  assert.deepEqual(probes.map((p) => [p.when, p.result, p.silent_run]), [["idle", "silent", 1], ["idle", "silent", 2], ["idle", "silent", 1], ["idle", "silent", 2]]);
  const stuck = logged(world, "quiet_stuck");
  assert.deepEqual(stuck.map((r) => [r.when, r.replace]), [["idle", true], ["idle", true]]);
  const third = (await registryWhen(world, (r) => r.companion?.pid !== second && r.companion?.pid !== first, "the registry to name the third companion")).companion.pid;
  assert.ok(third !== first && third !== second);
  const reply = await requestDaemon(status.socket, { id: "b", op: "act", input: { op: "tap", x: 8, y: 8 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "reported on the third companion");
});

test("r523: on an overloaded host a stuck companion is neither probed nor replaced, a touch goes once and is reported as it is; once the load drops it is", { timeout: 45_000 }, async (t) => {
  const interval = 1000;
  const world = harness(t, { stuckAfterMs: 300 });
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  // Far above HOST_LOAD_FACTOR per CPU on any host.
  world.setLoad(1_000_000);
  const status = await world.start();
  const first = status.companion.pid;
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  const [suspect] = await loggedAtLeast(world, "quiet_suspect", 1, { timeoutMs: QUIET_STUCK_MS + 5000 });
  assert.deepEqual([suspect.when, suspect.reason, suspect.owned, suspect.load_1m], ["idle", "host_load", true, 1_000_000], JSON.stringify(suspect));
  assert.ok(Number.isInteger(suspect.cpus) && suspect.cpus >= 1, JSON.stringify(suspect));
  assert.equal(suspect.load_limit, HOST_LOAD_FACTOR * suspect.cpus);
  assert.equal(suspect.overloaded, undefined, "the flag itself is not logged");
  // A touch is not probed either: written once on the stuck companion, truthfully unconfirmed.
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "the stuck companion reported nothing");
  assert.deepEqual(reply.warnings.map((w) => w.code), ["input_unconfirmed"]);
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  // Several re-checks (one per interval here) later: still no probe, no replacement, logged once per check.
  await new Promise((resolve) => setTimeout(resolve, 3 * interval + 500));
  assert.deepEqual(logged(world, "quiet_suspect").map((r) => [r.when, r.reason]), [["idle", "host_load"], ["touch", "host_load"]]);
  assert.equal(logged(world, "quiet_probe").length, 0, "never probed while overloaded");
  assert.equal(logged(world, "quiet_stuck").length, 0, "never replaced while overloaded");
  assert.equal(world.companionPids().length, 1, "no other companion started");
  assert.ok(pidAlive(first));
  // The load drops: the next re-check probes (two silent) and replaces it.
  world.setLoad(0);
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: interval + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 8000, message: "the replacement once the load dropped" });
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result, p.silent_run]), [["idle", "silent", 1], ["idle", "silent", 2]]);
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.when, r.replace, r.gap_ms, r.streak]), [["idle", true, interval, 0]]);
  await registryWhen(world, (r) => r.companion?.pid !== first && r.state === "serving", "the registry to name the new companion");
  const after = await requestDaemon(status.socket, { id: "b", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(after.ok, true, JSON.stringify(after.error));
  assert.equal(after.input_confirmed, true, "reported on the new companion");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5", "down@6,6", "up@6,6"], "each sent once");
});

test("r523: a load that rises while a touch's probes run stops the replacement; the touch goes once on the current companion", { timeout: 45_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300, stuckProbeSilent: true });
  const status = await world.start();
  const first = status.companion.pid;
  const client = await world.connect(status);
  await waitFor(() => world.events().some((e) => e.k === "stuck"), { message: "the companion to get stuck" });
  // A quiet wait in flight keeps the idle check away; the touch comes after QUIET_STUCK_MS.
  await daemonSawBusy(status);
  const waiting = client.request({ op: "quiet", timeout_ms: QUIET_STUCK_MS + 4000, quiet_ms: 250 });
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 1000));
  const opens = () => world.events().filter((e) => e.k === "quiet_open" && e.pid === first).length;
  const before = opens();
  const tap = requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  // The first probe stream opened: the host turns overloaded during the probes.
  await waitFor(() => opens() > before, { message: "the touch's first probe" });
  world.setLoad(1_000_000);
  const reply = await tap;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "sent on the stuck companion, reported as it is");
  assert.deepEqual(logged(world, "quiet_probe").filter((p) => p.when === "touch").map((p) => [p.result, p.silent_run]), [["silent", 1], ["silent", 2]]);
  assert.deepEqual(logged(world, "quiet_suspect").map((r) => [r.when, r.reason]), [["touch", "host_load"]]);
  assert.equal(logged(world, "quiet_stuck").length, 0, "not replaced");
  const touches = world.events().filter((e) => e.k === "touch_up");
  assert.deepEqual(touches.map((e) => [e.pid === first, e.stuck]), [[true, true]], "written once, on the stuck companion");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  await waiting.catch(() => {});
  assert.equal(world.registry().companion.pid, first);
  assert.equal(logged(world, "companion_restarted").length, 0);
});

test("r523: stuck replacements in a row with no sign of health double the gap (5, 5, 10, 20 s here); a confirmed touch resets it", { timeout: 120_000 }, async (t) => {
  const interval = 5000;
  // Every companion is stuck from its start, as live: its first report is that BUSY.
  const world = harness(t, { stuckFromStart: true });
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  const stuckRecords = () => logged(world, "quiet_stuck");
  const at = (record) => Date.parse(record.ts);
  const proof = QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS;
  await waitFor(() => logged(world, "companion_restarted").length >= 3, { timeoutMs: 3 * proof + 2 * interval + 30_000, message: "three replacements" });
  const stuck = stuckRecords();
  assert.deepEqual(stuck.map((r) => [r.when, r.replace, r.gap_ms, r.streak]),
    [["idle", true, interval, 0], ["idle", true, interval, 1], ["idle", true, 2 * interval, 2]], JSON.stringify(stuck));
  // The third waited out its doubled gap (logged once as too soon), counted from the second.
  assert.ok(at(stuck[2]) - at(stuck[1]) >= 2 * interval - 100, `${at(stuck[2]) - at(stuck[1])} ms`);
  assert.ok(logged(world, "quiet_suspect").some((r) => r.reason === "too_soon" && r.gap_ms === 2 * interval), JSON.stringify(logged(world, "quiet_suspect")));
  // The fourth companion, stuck too, now waits 4 x the interval: logged, not replaced.
  await waitFor(() => logged(world, "quiet_suspect").some((r) => r.reason === "too_soon" && r.gap_ms === 4 * interval), { timeoutMs: QUIET_STUCK_MS + 8000, message: "the quadrupled gap" });
  assert.equal(logged(world, "companion_restarted").length, 3);
  assert.equal(logged(world, "quiet_backoff_reset").length, 0, "no sign of health so far");
  // The fourth companion recovers silently; a touch on it is reported: the backoff resets.
  const fourth = (await registryWhen(world, (r) => r.state === "serving" && r.companion?.pid, "the fourth companion")).companion.pid;
  process.kill(fourth, "SIGHUP");
  await waitFor(() => world.events().some((e) => e.k === "unstuck_silent" && e.pid === fourth), { message: "the silent recovery" });
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 4, y: 4 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true, "the recovered companion reported it");
  const [reset] = await loggedAtLeast(world, "quiet_backoff_reset");
  assert.deepEqual([reset.why, reset.streak], ["touch", 3]);
  // A new episode on it is replaced after the plain interval again.
  process.kill(fourth, "SIGUSR2");
  await waitFor(() => logged(world, "companion_restarted").length === 4, { timeoutMs: proof + interval + 10_000, message: "the replacement after the reset" });
  const last = stuckRecords().at(-1);
  assert.deepEqual([last.replace, last.gap_ms, last.streak], [true, interval, 0], JSON.stringify(last));
  assert.equal(logged(world, "quiet_stuck").length, 4, "the stuck companions were each logged once");
});

// REV-059: the companions in these tests are healthy at start (their first event, QUIET,
// comes while the daemon still warms them up, so it says nothing); SIGUSR2 makes one stuck
// once it serves, as 3 of 6 new companions did in the live r523 run.
const STUCK_PROOF_MS = QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS;

test("REV-059: a replacement's stuck BUSY and its target changes while it serves are no sign of health: the backoff is kept and doubles", { timeout: 120_000 }, async (t) => {
  const interval = 2000;
  // Each episode: that BUSY on the open stream, then an app launched and one that exits.
  const world = harness(t, { stuckTargetChanges: [[300, "target_changed"], [600, "target_exited"]] });
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  let pid = status.companion.pid;
  for (let n = 1; n <= 3; n += 1) {
    process.kill(pid, "SIGUSR2");
    await waitFor(() => world.events().some((e) => e.k === "target_exited" && e.pid === pid), { message: `episode ${n}'s target changes` });
    pid = await replacedServing(world, n, STUCK_PROOF_MS + 4 * interval + 15_000);
  }
  const stuck = logged(world, "quiet_stuck");
  assert.deepEqual(stuck.map((r) => [r.when, r.replace, r.gap_ms, r.streak]),
    [["idle", true, interval, 0], ["idle", true, interval, 1], ["idle", true, 2 * interval, 2]], JSON.stringify(stuck));
  assert.deepEqual(logged(world, "quiet_backoff_reset"), [], "neither that BUSY nor a target change resets it");
  // The daemon got every report on its open stream: each BUSY, and both target changes of each episode.
  const targets = logged(world, "quiet_target");
  assert.deepEqual([targets.filter((r) => r.type === "target_changed").length, targets.filter((r) => r.type === "target_exited").length], [3, 3], JSON.stringify(targets));
  assert.deepEqual(logged(world, "quiet_probe").map((p) => p.result), Array(6).fill("silent"));
});

test("REV-059: after a replacement, a state report while the new companion serves resets the backoff, and so does a probe that answers", { timeout: 120_000 }, async (t) => {
  const interval = 2000;
  const world = harness(t);
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  const wait = STUCK_PROOF_MS + 2 * interval + 15_000;
  process.kill(status.companion.pid, "SIGUSR2");
  const second = await replacedServing(world, 1, wait);
  // A report other than that BUSY, not a touch (a new app: target_changed, then QUIET): the streak (1) starts over.
  process.kill(second, "SIGUSR1");
  const [byReport] = await loggedAtLeast(world, "quiet_backoff_reset", 1);
  assert.deepEqual([byReport.why, byReport.streak], ["report", 1], JSON.stringify(byReport));
  process.kill(second, "SIGUSR2");
  const third = await replacedServing(world, 2, wait);
  // The next one goes silent after that BUSY but is healthy: the idle probe answers, the streak (1) starts over.
  process.kill(third, "SIGWINCH");
  const resets = await loggedAtLeast(world, "quiet_backoff_reset", 2, { timeoutMs: STUCK_PROOF_MS + interval + 8000 });
  assert.deepEqual(resets.map((r) => [r.why, r.streak]), [["report", 1], ["probe", 1]], JSON.stringify(resets));
  assert.deepEqual(logged(world, "quiet_probe").filter((p) => p.result === "ok").map((p) => p.when), ["idle"]);
  process.kill(third, "SIGUSR2");
  await replacedServing(world, 3, wait);
  const stuck = logged(world, "quiet_stuck");
  assert.deepEqual(stuck.map((r) => [r.when, r.replace, r.gap_ms, r.streak]),
    [["idle", true, interval, 0], ["idle", true, interval, 0], ["idle", true, interval, 0]], JSON.stringify(stuck));
});

test("REV-059: a load that rises during the idle check's second probe stops the replacement: the idle check looks at the load before it replaces", { timeout: 60_000 }, async (t) => {
  const interval = 1000;
  const world = harness(t);
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  const first = status.companion.pid;
  const opens = () => world.events().filter((e) => e.k === "quiet_open" && e.pid === first).length;
  process.kill(first, "SIGUSR2");
  // The daemon's own stream, the first probe's and the second probe's: the host turns overloaded.
  await waitFor(() => opens() >= 1 + STUCK_SILENT_PROBES, { timeoutMs: STUCK_PROOF_MS + 8000, message: "the second idle probe" });
  world.setLoad(1_000_000);
  const [suspect] = await loggedAtLeast(world, "quiet_suspect", 1, { timeoutMs: QUIET_STUCK_PROBE_MS + 5000 });
  assert.deepEqual([suspect.when, suspect.reason], ["idle", "host_load"], JSON.stringify(suspect));
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result, p.silent_run]), [["idle", "silent", 1], ["idle", "silent", 2]]);
  // Checked again once per interval while overloaded: the two silent answers are recorded
  // (they go stale after STUCK_PROBE_FRESH_MS, so once the load drops the daemon probes twice
  // again before it replaces), yet no replacement while overloaded.
  await new Promise((resolve) => setTimeout(resolve, 3 * interval + 500));
  assert.equal(logged(world, "quiet_stuck").length, 0, "never replaced while overloaded");
  assert.equal(logged(world, "companion_restarted").length, 0);
  assert.equal(world.companionPids().length, 1, "no other companion started");
  assert.ok(pidAlive(first));
  world.setLoad(0);
  await waitFor(() => logged(world, "companion_restarted").length === 1, { timeoutMs: interval + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 8000, message: "the replacement once the load dropped" });
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.when, r.replace]), [["idle", true]]);
});

test("REV-059: a touch inside the backoff gap is not probed: sent at once on the current companion and reported as it is", { timeout: 60_000 }, async (t) => {
  const interval = 20_000;
  const world = harness(t);
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  process.kill(status.companion.pid, "SIGUSR2");
  const second = await replacedServing(world, 1, STUCK_PROOF_MS + 15_000);
  const probes = logged(world, "quiet_probe").length;
  assert.equal(probes, STUCK_SILENT_PROBES);
  // The new companion is stuck at once: a touch QUIET_STUCK_MS later is well inside the 20 s gap.
  process.kill(second, "SIGUSR2");
  await daemonSawBusy(status);
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 500));
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 5, y: 5 }, read: false }, { timeoutMs: 25_000 });
  const ms = performance.now() - t0;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "the stuck companion reported nothing");
  // Its confirmation window only, never the probes (2 x QUIET_STUCK_PROBE_MS) on top.
  assert.ok(ms < STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 1000, `the tap took ${Math.round(ms)} ms`);
  assert.equal(logged(world, "quiet_probe").length, probes, "not probed");
  assert.deepEqual(logged(world, "quiet_suspect").filter((r) => r.when === "touch").map((r) => [r.reason, r.gap_ms]), [["too_soon", interval]]);
  assert.equal(logged(world, "quiet_stuck").length, 1, "only the first replacement");
  await assertRecorded(() => tapPairs(world.hid()), ["down@5,5", "up@5,5"], "sent once");
  assert.ok(pidAlive(second));
});

test("REV-067: at streak 2 a touch inside the doubled gap but past the plain interval is not probed: too_soon with the doubled gap_ms, a quick reply, sent once", { timeout: 90_000 }, async (t) => {
  // The touch path must use the backoff gap (2 x interval at streak 2), not the plain interval:
  // at streak 1 the two are equal, so only a touch at streak >= 2 tells them apart.
  const interval = 8000;
  const world = harness(t);
  world.env[STUCK_INTERVAL_ENV] = String(interval);
  const status = await world.start();
  const wait = STUCK_PROOF_MS + interval + 15_000;
  process.kill(status.companion.pid, "SIGUSR2");
  const second = await replacedServing(world, 1, wait);
  process.kill(second, "SIGUSR2");
  const third = await replacedServing(world, 2, wait);
  // The second replacement is the last: its gap needed the plain interval (streak 1), the next needs twice that.
  const replacedAt = Date.now();
  assert.deepEqual(logged(world, "quiet_stuck").map((r) => [r.replace, r.gap_ms, r.streak]), [[true, interval, 0], [true, interval, 1]]);
  const probes = logged(world, "quiet_probe").length;
  assert.equal(probes, 2 * STUCK_SILENT_PROBES);
  // The third is stuck at once. The touch comes QUIET_STUCK_MS after the daemon holds that BUSY and
  // at least half an interval past the plain interval counted from the replacement (the daemon's
  // own clock started before mine, so its time since then is at least what I measure).
  process.kill(third, "SIGUSR2");
  await daemonSawBusy(status);
  await new Promise((resolve) => setTimeout(resolve, QUIET_STUCK_MS + 500));
  const past = replacedAt + interval + 500 - Date.now();
  if (past > 0) await new Promise((resolve) => setTimeout(resolve, past));
  assert.ok(Date.now() - replacedAt < 2 * interval - 4000, "the touch still inside the doubled gap with room to spare");
  const t0 = performance.now();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 7, y: 7 }, read: false }, { timeoutMs: 25_000 });
  const ms = performance.now() - t0;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "the stuck companion reported nothing");
  assert.ok(ms < STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 1000, `the tap took ${Math.round(ms)} ms`);
  assert.equal(logged(world, "quiet_probe").length, probes, "not probed for the touch");
  assert.deepEqual(logged(world, "quiet_suspect").filter((r) => r.when === "touch").map((r) => [r.reason, r.gap_ms]), [["too_soon", 2 * interval]]);
  assert.equal(logged(world, "quiet_stuck").length, 2, "no third replacement");
  await assertRecorded(() => tapPairs(world.hid()), ["down@7,7", "up@7,7"], "sent once");
  assert.ok(pidAlive(third));
});

test("ID-10: a stuck companion the daemon does not own is probed and left running; its touches stay unconfirmed", { timeout: 30_000 }, async (t) => {
  const world = harness(t, { stuckAfterMs: 300 });
  execFileSync(world.paths.idb, ["connect", UDID], { env: world.env });
  const [companion] = world.companionPids();
  const status = await world.start();
  assert.equal(status.companion.owned, false);
  await waitFor(() => logged(world, "quiet_stuck").length === 1, { timeoutMs: QUIET_STUCK_MS + STUCK_SILENT_PROBES * QUIET_STUCK_PROBE_MS + 5000, message: "the stuck stream to be noticed" });
  const [stuckRecord] = logged(world, "quiet_stuck");
  assert.deepEqual([stuckRecord.replace, stuckRecord.reason, stuckRecord.silent_probes], [false, "not_owned", STUCK_SILENT_PROBES]);
  assert.deepEqual(logged(world, "quiet_probe").map((p) => [p.when, p.result]), [["idle", "silent"], ["idle", "silent"]]);
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 6, y: 6 }, read: false });
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, false, "nothing reported it: never confirmed");
  assert.deepEqual(reply.warnings.map((w) => w.code), ["input_unconfirmed"]);
  // Not probed before the touch: nothing could follow for a companion it does not own.
  assert.equal(logged(world, "quiet_probe").length, STUCK_SILENT_PROBES);
  assert.equal(logged(world, "quiet_stuck").length, 1, "logged once, not again on each check");
  assert.deepEqual(logged(world, "quiet_suspect").map((r) => [r.when, r.reason]), [["touch", "not_owned"]]);
  assert.ok(pidAlive(companion), "the companion it did not start keeps running");
  assert.equal(world.registry().companion.pid, companion);
  assert.equal(logged(world, "companion_restarted").length, 0);
  process.kill(companion, "SIGTERM");
});

test("ID-10: the act's grace window runs while the companion's status is on its way, and the reply still waits for it", { timeout: 20_000 }, async (t) => {
  // The companion answers each hid call 400 ms after it ended; the touch is reported in 30 ms.
  const world = harness(t, { hidEndDelayMs: 400 });
  const status = await world.start();
  const client = await world.connect(status);
  const sentAt = Date.now();
  const reply = await client.request({ op: "act", input: { op: "tap", x: 8, y: 8 } });
  const elapsed = Date.now() - sentAt;
  assert.equal(reply.ok, true, JSON.stringify(reply.error));
  assert.equal(reply.input_confirmed, true);
  assert.ok(elapsed >= 400, `the reply waited for the companion's status (${elapsed} ms)`);
  // In sequence (status, then the touch and the grace) it would take 400 + 300 + the read.
  assert.ok(elapsed < 400 + GRACE_MS, `the grace overlapped the status wait (${elapsed} ms)`);
  const [timing] = await loggedAtLeast(world, "act_timing");
  assert.ok(timing.flushed >= 400 && timing.confirmed < timing.flushed, JSON.stringify(timing));
  assert.ok(timing.read >= timing.flushed, "the read came after the status");
  await assertRecorded(() => tapPairs(world.hid()), ["down@8,8", "up@8,8"]);
});

test("ID-10: an act whose hid call the companion fails is not ok, even though the app reported the touch", { timeout: 20_000 }, async (t) => {
  const world = harness(t, { hidEndDelayMs: 200, hidEndCode: 13 });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 9, y: 9 } }, { timeoutMs: 20_000 });
  assert.equal(reply.ok, false, JSON.stringify(reply));
  assert.equal(reply.error.code, CODE.INPUT_UNCONFIRMED);
  assert.equal(reply.error.stage, STAGE.AFTER_SEND);
  assert.equal(reply.error.unconfirmed_events, 2);
  assert.equal(world.reads().length, 0, "no read after an input the companion did not confirm");
  assert.ok(world.events().some((e) => e.k === "touch_up"), "the fake app did see the touch");
  await assertRecorded(() => tapPairs(world.hid()), ["down@9,9", "up@9,9"], "sent once, never again");
});

test("ID-10: ui wait --settled needs a quiet event after the busy one", async (t) => {
  // After the touch the app reports QUIET, then BUSY, then QUIET again 600 ms later.
  const world = harness(t, { touchDelayMs: 100, afterTouchStates: [[0, "QUIET"], [20, "BUSY"], [600, "QUIET"]] });
  const status = await world.start();
  const client = await world.connect(status);
  assert.equal((await client.request({ op: "tap", x: 1, y: 1 })).ok, true);
  const reply = await client.request({ op: "quiet", timeout_ms: 3000, quiet_ms: 250 });
  assert.equal(reply.ok, true);
  assert.equal(reply.state, "quiet");
  assert.equal(reply.timed_out, false);
  assert.ok(reply.waited_ms >= 450, `the earlier QUIET ended the wait at once (${reply.waited_ms} ms)`);
});

test("ID-10: ui wait --settled uses the open quiet stream", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "q", op: "quiet", timeout_ms: 1000, quiet_ms: 100 });
  assert.equal(reply.ok, true);
  assert.equal(reply.state, "quiet");
  assert.equal(reply.method, "quiet-stream");
  assert.equal(reply.timed_out, false);
  assert.ok(reply.waited_ms < 500);
  assert.equal(world.events().filter((e) => e.k === "quiet_open").length, 1, "no new quiet call per request");
});

// ---------------------------------------------------------------------------
// ID-11: input path warmed up at start
// ---------------------------------------------------------------------------

test("ID-11-S01: the daemon sends one harmless key-up before the first tap and logs it", async (t) => {
  const world = harness(t);
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start, before any request");
  await requestDaemon(status.socket, { id: "t", op: "tap", x: 1, y: 2 });
  await assertRecorded(() => world.hid(), [
    { key: PRIMING_KEYCODE, direction: "up" },
    { touch: { x: 1, y: 2 }, direction: "down" },
    { touch: { x: 1, y: 2 }, direction: "up" },
  ]);
  const log = readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8");
  assert.match(log, /"event":"hid_primed"/);
});

// ---------------------------------------------------------------------------
// The companion serves one hid call at a time: the daemon's call ends when idle
// ---------------------------------------------------------------------------

/**
 * Another client's whole hid call on the daemon's companion, as `idb ui text` or
 * `idb ui key` makes it: open, write, half-close, wait for the status. `completed` is true
 * when it closed with status 0 within `timeoutMs`; a call still waiting is cancelled.
 */
async function otherClientHid(socketPath, events, { timeoutMs = 2000 } = {}) {
  const other = new IdbCompanionClient({ socketPath });
  const started = Date.now();
  try {
    const call = other.openCall("hid");
    let status = null;
    call.on("error", () => {});
    call.on("data", () => {});
    call.on("response", (headers) => { status = headers["grpc-status"] ?? status; });
    call.on("trailers", (trailers) => { status = trailers["grpc-status"] ?? status; });
    const closed = new Promise((resolve) => call.once("close", () => resolve(true)));
    for (const event of events) call.write(grpcFrame(encodeHidEvent(event)));
    call.end();
    let timer;
    const done = await Promise.race([closed, new Promise((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); })]);
    clearTimeout(timer);
    if (!done && !call.destroyed) call.close(http2.constants.NGHTTP2_CANCEL);
    return { completed: done && status === "0", ms: Date.now() - started };
  } finally {
    await other.close({ timeoutMs: 200 });
  }
}

const TEXT_EVENTS = [{ key: 11, direction: "down" }, { key: 11, direction: "up" }, { key: 12, direction: "down" }, { key: 12, direction: "up" }];

test("ID-15: after a daemon tap another client's hid call (idb ui text) completes within 2 s", { timeout: 20_000 }, async (t) => {
  assert.equal(HID_IDLE_CLOSE_MS, 250);
  const world = harness(t);
  const status = await world.start();
  const tap = await requestDaemon(status.socket, { id: "t", op: "tap", x: 30, y: 40 });
  assert.equal(tap.ok, true, JSON.stringify(tap.error));
  const other = await otherClientHid(world.registry().companion.socket, TEXT_EVENTS);
  assert.equal(other.completed, true, `the other client's hid call completed (${other.ms} ms)`);
  await assertRecorded(() => world.hid(), [
    { key: PRIMING_KEYCODE, direction: "up" },
    { touch: { x: 30, y: 40 }, direction: "down" },
    { touch: { x: 30, y: 40 }, direction: "up" },
    ...TEXT_EVENTS,
  ]);
  const reply = await requestDaemon(status.socket, { id: "s", op: "status" });
  assert.ok(reply.hid_idle_closes >= 1, `idle closes ${reply.hid_idle_closes}`);
  assert.equal(reply.hid_primed, true);
});

test("ID-11/ID-15: after priming with no input another client's hid call completes", { timeout: 20_000 }, async (t) => {
  const world = harness(t);
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const other = await otherClientHid(world.registry().companion.socket, TEXT_EVENTS);
  assert.equal(other.completed, true, `the other client's hid call completed (${other.ms} ms)`);
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }, ...TEXT_EVENTS]);
  // The daemon still serves input afterwards, on a new call, without priming again.
  const act = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 7, y: 8 }, read: false });
  assert.equal(act.ok, true, JSON.stringify(act.error));
  assert.equal(act.input_confirmed, true);
  assert.equal(act.confirmed_by, "touches_completed");
  await assertRecorded(() => world.hid(), [
    { key: PRIMING_KEYCODE, direction: "up" }, ...TEXT_EVENTS,
    { touch: { x: 7, y: 8 }, direction: "down" }, { touch: { x: 7, y: 8 }, direction: "up" },
  ], "one priming key-up for the companion, not one per call");
});

test("ID-05/ID-10: inputs across idle closes each open a call, are confirmed by their own touch and go out once", { timeout: 20_000 }, async (t) => {
  const world = harness(t);
  const status = await world.start();
  const client = await world.connect(status);
  for (const [index, x] of [11, 22, 33].entries()) {
    const act = await client.request({ op: "act", input: { op: "tap", x, y: x }, read: false });
    assert.equal(act.ok, true, JSON.stringify(act.error));
    assert.equal(act.input_confirmed, true, `act ${index}`);
    // Wait for the daemon's call to end, so the next act opens a new one.
    await waitFor(() => world.events().filter((e) => e.k === "hid_close").length >= index + 1, { timeoutMs: 4000, message: `idle close ${index}` });
  }
  await assertRecorded(() => tapPairs(world.hid()), ["down@11,11", "up@11,11", "down@22,22", "up@22,22", "down@33,33", "up@33,33"]);
  assert.ok(world.events().filter((e) => e.k === "hid_open").length >= 3, "a new call per input after an idle close");
  const log = readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8");
  assert.doesNotMatch(log, /"event":"hid_drop"/, "an idle close is not logged as a drop");
  assert.equal((log.match(/"event":"hid_primed"/g) ?? []).length, 1);
});

/**
 * Another client that takes the companion's hid call and keeps it (a Canvas long press,
 * a long `idb ui text`): resolves once its key-down was performed, i.e. it is being
 * served; `release()` sends the key-up, ends the call and waits for it to close.
 */
async function holdHid(t, world) {
  await waitFor(() => world.events().filter((e) => e.k === "hid_open").length
    === world.events().filter((e) => e.k === "hid_close").length, { message: "no daemon call on the companion" });
  const holder = new IdbCompanionClient({ socketPath: world.registry().companion.socket });
  t.after(() => holder.close({ timeoutMs: 200 }));
  const call = holder.openCall("hid");
  call.on("error", () => {});
  call.on("data", () => {});
  const closed = new Promise((resolve) => call.once("close", resolve));
  call.write(grpcFrame(encodeHidEvent({ key: 99, direction: "down" })));
  await waitFor(() => world.hid().some((e) => e?.key === 99), { message: "the holder's call to be served" });
  return {
    async release() {
      call.write(grpcFrame(encodeHidEvent({ key: 99, direction: "up" })));
      call.end();
      await closed;
    },
  };
}

test("REV-024: a tap sent while another client holds the hid call past 2.5 s is performed once, in order, and only then answered", { timeout: 30_000 }, async (t) => {
  const world = harness(t);
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const holder = await holdHid(t, world);
  const heldAt = Date.now();
  const one = await world.connect(status);
  const two = await world.connect(status);
  const answered = {};
  const first = one.request({ op: "tap", x: 30, y: 40 }, { timeoutMs: 20_000 }).then((reply) => { answered.first = Date.now(); return reply; });
  // The daemon's call for the first tap waits behind the holder's.
  await waitFor(() => world.events().some((e) => e.k === "hid_wait"), { message: "the daemon's call to wait" });
  const second = two.request({ op: "tap", x: 50, y: 60 }, { timeoutMs: 20_000 }).then((reply) => { answered.second = Date.now(); return reply; });
  // Longer than the old 2 s stop timeout plus the idle window, after which the waiting call was cancelled.
  await new Promise((resolve) => setTimeout(resolve, Math.max(0, 2600 - (Date.now() - heldAt))));
  assert.equal(answered.first, undefined, "the tap is not answered while its events have not been performed");
  assert.equal(tapPairs(world.hid()).length, 0, "nothing performed while the holder holds the call");
  const releasedAt = Date.now();
  await holder.release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.ok, true, JSON.stringify(a.error));
  assert.equal(b.ok, true, JSON.stringify(b.error));
  assert.ok(answered.first >= releasedAt, "the first tap was answered after its events were performed");
  assert.ok(answered.second >= answered.first, "the second tap was answered after the first");
  await assertRecorded(() => world.hid().filter((e) => e?.key !== PRIMING_KEYCODE), [
    { key: 99, direction: "down" }, { key: 99, direction: "up" },
    { touch: { x: 30, y: 40 }, direction: "down" }, { touch: { x: 30, y: 40 }, direction: "up" },
    { touch: { x: 50, y: 60 }, direction: "down" }, { touch: { x: 50, y: 60 }, direction: "up" },
  ], "each tap once, in arrival order, after the holder");
  const reply = await requestDaemon(status.socket, { id: "s", op: "status" });
  assert.equal(reply.hid_lost, 0);
  assert.doesNotMatch(readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8"), /"event":"hid_(lost|unconfirmed)"/);
});

test("REV-024: a tap whose call is never served within the safety limit is answered input_unconfirmed after_send, never ok", { timeout: 30_000 }, async (t) => {
  const world = harness(t);
  world.env[HID_WAIT_ENV] = "600";
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const holder = await holdHid(t, world);
  const client = await world.connect(status);
  const tap = await client.request({ op: "tap", x: 30, y: 40 }, { timeoutMs: 20_000 });
  assert.equal(tap.ok, false, "a tap that was not performed is not ok");
  assert.equal(tap.error.code, CODE.INPUT_UNCONFIRMED);
  assert.equal(tap.error.stage, STAGE.AFTER_SEND);
  assert.equal(tap.error.hint, HID_UNCONFIRMED_HINT);
  assert.equal(tap.error.unconfirmed_events, 2);
  assert.match(tap.error.message, /not confirmed/);
  const act = await client.request({ op: "act", input: { op: "tap", x: 5, y: 6 }, read: false }, { timeoutMs: 20_000 });
  assert.equal(act.ok, false, "an observed tap is not ok either");
  assert.equal(act.error.code, CODE.INPUT_UNCONFIRMED);
  assert.equal(act.error.stage, STAGE.AFTER_SEND);
  assert.equal(tapPairs(world.hid()).length, 0, "neither tap was performed");
  await holder.release();
  // Once the holder is gone input works again, and earlier losses are not blamed on it.
  const next = await client.request({ op: "tap", x: 7, y: 8 }, { timeoutMs: 20_000 });
  assert.equal(next.ok, true, JSON.stringify(next.error));
  await assertRecorded(() => tapPairs(world.hid()), ["down@7,8", "up@7,8"], "only the tap after the release, once");
  const reply = await client.request({ op: "status" });
  assert.equal(reply.hid_lost, 4);
  assert.match(readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8"), /"event":"hid_unconfirmed","events":2,"op":"tap"/);
});

test("REV-029: with a holder that never lets go, a tap, a queued key and an act are answered by their deadline and never performed later", { timeout: 30_000 }, async (t) => {
  const world = harness(t);
  const limit = 1200; // DEFAULT_HID_WAIT_MS scaled down; the default's relation is pinned above
  world.env[HID_WAIT_ENV] = String(limit);
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const holder = await holdHid(t, world);
  const one = await world.connect(status);
  const two = await world.connect(status);
  const three = await world.connect(status);
  const timed = (connection, request) => {
    const sentAt = performance.now();
    return connection.request(request, { timeoutMs: 20_000 }).then((reply) => ({ reply, ms: performance.now() - sentAt }));
  };
  const tap = timed(one, { op: "tap", x: 30, y: 40 });
  await waitFor(() => world.events().some((e) => e.k === "hid_wait"), { message: "the daemon's call to wait" });
  // Queued behind the tap in the daemon: its deadline counts from its own arrival.
  const key = timed(two, { op: "key", code: 41 });
  const act = timed(three, { op: "act", input: { op: "tap", x: 5, y: 6 }, timeout_ms: 500 });
  const [a, b, c] = await Promise.all([tap, key, act]);
  for (const [name, { reply, ms }] of [["tap", a], ["key", b], ["act", c]]) {
    assert.equal(reply.ok, false, `${name} is not ok: ${JSON.stringify(reply)}`);
    assert.ok(ms < limit + INPUT_REPLY_SLACK_MS, `${name} answered in ${Math.round(ms)} ms, inside ${limit} + ${INPUT_REPLY_SLACK_MS}`);
  }
  assert.equal(a.reply.error.code, CODE.INPUT_UNCONFIRMED);
  assert.equal(a.reply.error.stage, STAGE.AFTER_SEND);
  assert.equal(a.reply.error.hint, HID_UNCONFIRMED_HINT);
  assert.equal(a.reply.error.unconfirmed_events, 2);
  // The key and the act waited behind the tap: refused before they were sent, or given
  // up after; never ok, and either way nothing of theirs is performed later.
  for (const { reply } of [b, c]) {
    assert.ok([CODE.INPUT_UNCONFIRMED, CODE.BACKEND_FAILED].includes(reply.error.code), JSON.stringify(reply.error));
    if (reply.error.stage === STAGE.BEFORE_SEND) assert.match(reply.error.message, /was not sent/);
    else assert.equal(reply.error.code, CODE.INPUT_UNCONFIRMED);
  }
  await holder.release();
  // Long enough for a call that was not cancelled to be served after the holder.
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.deepEqual(tapPairs(world.hid()), [], "no tap was performed after its client got the answer");
  assert.equal(world.hid().filter((e) => e?.key === 41).length, 0, "nor the key");
  const next = await one.request({ op: "tap", x: 7, y: 8 }, { timeoutMs: 20_000 });
  assert.equal(next.ok, true, JSON.stringify(next.error));
  await assertRecorded(() => tapPairs(world.hid()), ["down@7,8", "up@7,8"], "the stream still works, the tap once");
});

test("REV-032: an input that starts late and then waits behind a holder is cancelled at its arrival deadline, not at the client's close wait, and never performed", { timeout: 30_000 }, async (t) => {
  const world = harness(t);
  // DEFAULT_HID_WAIT_MS scaled down. The late start (PRESS_S) is longer than
  // INPUT_REPLY_SLACK_MS, so the companion client's own close wait, which counts from
  // the half-close and not from arrival, would only give up after the reply is due.
  const limit = 4500;
  const PRESS_S = 3.6;
  world.env[HID_WAIT_ENV] = String(limit);
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const one = await world.connect(status);
  const two = await world.connect(status);
  // A long press is performed first; the tap that arrives during it starts only after it.
  const pressSent = performance.now();
  const press = one.request({ op: "tap", x: 9, y: 9, duration: PRESS_S }, { timeoutMs: 20_000 })
    .then((reply) => ({ reply, at: performance.now() }));
  await waitFor(() => world.hid().some((e) => e?.touch?.x === 9 && e.direction === "down"), { message: "the long press to go down" });
  // Another client asks for the companion's hid call while the press holds it: it is
  // served next, before the daemon's call for the tap, and keeps it.
  const holder = new IdbCompanionClient({ socketPath: world.registry().companion.socket });
  t.after(() => holder.close({ timeoutMs: 200 }));
  const held = holder.openCall("hid");
  held.on("error", () => {});
  held.on("data", () => {});
  const heldClosed = new Promise((resolve) => held.once("close", resolve));
  held.write(grpcFrame(encodeHidEvent({ key: 99, direction: "down" })));
  await waitFor(() => world.events().some((e) => e.k === "hid_wait"), { message: "the holder's call to wait behind the press" });
  const arrived = performance.now();
  const tap = two.request({ op: "tap", x: 30, y: 40 }, { timeoutMs: 20_000 })
    .then((reply) => ({ reply, ms: performance.now() - arrived }));
  const pressed = await press;
  assert.equal(pressed.reply.ok, true, JSON.stringify(pressed.reply.error));
  assert.ok(pressed.at - arrived > INPUT_REPLY_SLACK_MS,
    `the tap could start only ${Math.round(pressed.at - arrived)} ms after its arrival, later than the ${INPUT_REPLY_SLACK_MS} ms slack`);
  assert.ok(pressed.at - pressSent < limit + PRESS_S * 1000, "the press itself was in time");
  await waitFor(() => world.hid().some((e) => e?.key === 99), { message: "the holder's call to be served after the press" });
  // The holder lets go once the tap is answered, or once its answer is overdue: a call
  // still waiting then would be served and the tap performed late.
  const due = limit + INPUT_REPLY_SLACK_MS;
  const outcome = await Promise.race([tap, new Promise((resolve) => setTimeout(() => resolve(null), Math.max(0, due - (performance.now() - arrived))))]);
  held.write(grpcFrame(encodeHidEvent({ key: 99, direction: "up" })));
  held.end();
  await heldClosed;
  const { reply, ms } = outcome ?? await tap;
  assert.ok(outcome, `the tap was answered by arrival + ${limit} + ${INPUT_REPLY_SLACK_MS} ms (took ${Math.round(ms)} ms)`);
  assert.equal(reply.ok, false, `the tap is not ok: ${JSON.stringify(reply)}`);
  assert.equal(reply.error.code, CODE.INPUT_UNCONFIRMED);
  assert.equal(reply.error.stage, STAGE.AFTER_SEND, "it was written (it did start), then given up");
  assert.equal(reply.error.unconfirmed_events, 2);
  // Long enough for a call that was not cancelled to be served after the holder.
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.deepEqual(tapPairs(world.hid()), ["down@9,9", "up@9,9"], "the late tap was never performed, before or after the release");
  const next = await two.request({ op: "tap", x: 7, y: 8 }, { timeoutMs: 20_000 });
  assert.equal(next.ok, true, JSON.stringify(next.error));
  await assertRecorded(() => tapPairs(world.hid()), ["down@9,9", "up@9,9", "down@7,8", "up@7,8"], "the stream still works");
});

test("REV-029: a long press gets its own duration on top of the deadline", { timeout: 30_000 }, async (t) => {
  const world = harness(t);
  world.env[HID_WAIT_ENV] = "300";
  const status = await world.start();
  await assertRecorded(() => world.hid(), [{ key: PRIMING_KEYCODE, direction: "up" }], "primed at start");
  const client = await world.connect(status);
  // 0.8 s held, longer than the 300 ms limit: performed, confirmed and ok.
  const press = await client.request({ op: "tap", x: 9, y: 9, duration: 0.8 }, { timeoutMs: 20_000 });
  assert.equal(press.ok, true, JSON.stringify(press.error));
  await assertRecorded(() => tapPairs(world.hid()), ["down@9,9", "up@9,9"], "the long press once");
});

// ---------------------------------------------------------------------------
// ID-12: lost input gets a cause
// ---------------------------------------------------------------------------

test("ID-12-S01: the dtuhidd flag set with no Device Hub turns the warning into ios_input_blocked", async (t) => {
  const world = harness(t, { touches: false, xcrun: { flag: 1 }, ps: { deviceHub: false } });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 3, y: 4 } });
  assert.equal(reply.ok, true);
  assert.equal(reply.input_confirmed, false);
  assert.deepEqual(reply.warnings.map((w) => w.code), ["ios_input_blocked"]);
  assert.match(reply.warnings[0].hint, /Reboot the simulator/);
  assert.equal(reply.warnings[0].dtuhidd_active, 1);
  assert.equal(reply.warnings[0].device_hub, false);
  assert.deepEqual(world.xcrunCalls(), [["simctl", "spawn", UDID, "notifyutil", "-g", DTUHIDD_FLAG]],
    "no simulator command other than the flag read");
});

test("ID-12: with Device Hub running the warning stays input_unconfirmed", async (t) => {
  const world = harness(t, { touches: false, xcrun: { flag: 1 }, ps: { deviceHub: true } });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 3, y: 4 } });
  assert.deepEqual(reply.warnings.map((w) => w.code), ["input_unconfirmed"]);
  assert.equal(reply.warnings[0].device_hub, true);
});

// ---------------------------------------------------------------------------
// ID-13: unlit display
// ---------------------------------------------------------------------------

test("ID-13-S01: a display that is not lit yet fails with ios_display_not_lit after at most two reads", async (t) => {
  const world = harness(t, { unlit: true });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "t", op: "tree" });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "ios_display_not_lit");
  assert.match(reply.error.hint, /foldable|display change/);
  assert.equal(world.reads().length, 2, "one read and one retry, no loop");
});

// ---------------------------------------------------------------------------
// Logging and status
// ---------------------------------------------------------------------------

test("the log carries ops and timings, never tree contents; status reports counts and recent timings", async (t) => {
  const world = harness(t, { ax: { AX: SAFARI_AX, AXBRIDGE: SAFARI_AXBRIDGE } });
  const status = await world.start();
  const client = await world.connect(status);
  await client.request({ op: "tree" });
  await client.request({ op: "tap", x: 1, y: 1 });
  const reply = await client.request({ op: "status" });
  assert.equal(reply.ok, true);
  assert.equal(reply.status, "serving");
  assert.equal(reply.requests, 3);
  assert.deepEqual(reply.requests_by_op, { tree: 1, tap: 1, status: 1 });
  assert.deepEqual(reply.timings.map((entry) => entry.op), ["tree", "tap"]);
  assert.ok(reply.timings.every((entry) => Number.isInteger(entry.ms)));
  assert.equal(reply.hid_primed, true);
  assert.ok(reply.uptime_ms > 0);
  const log = readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8");
  assert.match(log, /"event":"request","op":"tree","ms":\d+,"ok":true/);
  assert.doesNotMatch(log, /Example Domain|Address|Tabs/);
  assert.doesNotMatch(log, /synthetic-secret/);
});

// ---------------------------------------------------------------------------
// Error paths
// ---------------------------------------------------------------------------

test("a quiet stream the companion finishes is reopened and keeps confirming input", async (t) => {
  const world = harness(t, { quietEndMs: 100 });
  const status = await world.start();
  await waitFor(() => world.events().filter((e) => e.k === "quiet_open").length === 2, { message: "the quiet stream to reopen" });
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 9, y: 9 } });
  assert.equal(reply.ok, true);
  assert.equal(reply.input_confirmed, true);
  await loggedAtLeast(world, "quiet_reopened");
});

test("while the companion cannot be replaced, input fails before it is sent and the restart keeps trying", async (t) => {
  const world = harness(t);
  const status = await world.start();
  const config = JSON.parse(readFileSync(world.paths.config, "utf8"));
  world.setConfig({ ...config, idb: { failConnect: true } });
  process.kill(status.companion.pid, "SIGKILL");
  const client = await world.connect(status);
  await waitFor(async () => (await client.request({ op: "status" })).status === "companion_restarting",
    { timeoutMs: 3000, message: "the restart to begin" });
  assert.equal(world.registry().state, "companion_restarting", "the registry says so too");
  const tap = await client.request({ op: "tap", x: 7, y: 7 }, { timeoutMs: 15_000 });
  assert.equal(tap.ok, false);
  assert.equal(tap.error.code, "daemon_unreachable");
  assert.equal(tap.error.stage, "before_send");
  assert.equal(tapPairs(world.hid()).length, 0, "nothing was sent");
  const failedConnects = world.idbCalls().filter((c) => c.argv[0] === "connect").length;
  assert.ok(failedConnects >= 3, `restart attempts with backoff (${failedConnects})`);
  world.setConfig(config);
  const back = await waitFor(() => (world.registry()?.state === "serving" && world.registry().companion?.pid !== status.companion.pid
    ? world.registry() : null), { timeoutMs: 12_000, message: "the companion to come back" });
  assert.ok(pidAlive(back.companion.pid));
  const tree = await client.request({ op: "tree" });
  assert.equal(tree.ok, true);
  const log = readFileSync(daemonPaths(world.paths.base, UDID).log, "utf8");
  const backoffs = [...log.matchAll(/"event":"companion_restart_failed".*?"backoff_ms":(\d+)/g)].map((m) => Number(m[1]));
  assert.deepEqual(backoffs.slice(0, 3), [500, 1000, 2000], "0.5 s doubling");
});

test("an act whose read fails after the input reports after_send with the input result", async (t) => {
  const world = harness(t, { unlit: true });
  const status = await world.start();
  const reply = await requestDaemon(status.socket, { id: "a", op: "act", input: { op: "tap", x: 2, y: 2 } });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, "ios_display_not_lit");
  assert.equal(reply.error.stage, "after_send");
  assert.equal(reply.input_confirmed, true);
  assert.equal(reply.method, "quiet-stream");
  await assertRecorded(() => tapPairs(world.hid()), ["down@2,2", "up@2,2"]);
});

test("a failing idb connect refuses the start with daemon_start_failed and leaves no files", async (t) => {
  const world = harness(t, { idb: { failConnect: true } });
  await assert.rejects(world.start(), (error) => error instanceof DaemonError && error.code === CODE.DAEMON_START_FAILED
    && error.extra.reason === "companion_start_failed");
  assert.equal(world.registry(), null);
  assert.equal(existsSync(socketPathFor(world.paths.base, UDID).dir), false);
  assert.equal(existsSync(daemonPaths(world.paths.base, UDID).daemonLock), false);
  assert.equal(existsSync(daemonPaths(world.paths.base, UDID).startLock), false);
  assert.deepEqual(daemonProcesses(world.paths.base), []);
});
