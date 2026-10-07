#!/usr/bin/env node
/**
 * Live end-to-end check of the Mobile Canvas Tools drawer (requirement wc-010).
 *
 * Not part of `node --test tests/*.test.mjs`: it starts this checkout's real Canvas on one
 * test device, calls the drawer's /tools routes panel by panel and checks each effect on the
 * device itself, then puts back everything it changed. Test devices only:
 *
 *   node tests/live/canvas_tools_live.mjs --platform android --serial emulator-5580 \
 *     [--ui] [--evidence-dir DIR] [--app-id PKG] [--location-app PKG] [--adb PATH] \
 *     [--python PATH] [--keep-home] [--dry-run]
 *   node tests/live/canvas_tools_live.mjs --platform ios --udid 3760A5A8-E59D-4AE6-B1AF-A626908D7B61 \
 *     [--ui] [--evidence-dir DIR] [--app-id BUNDLE] [--push-app BUNDLE] [--xcrun PATH] [--python PATH] [--keep-home]
 *     [--dry-run]
 *   node tests/live/canvas_tools_live.mjs --self-test   (the pure helpers only; no Canvas or device)
 *
 * Android runs only on emulator-5580 and iOS only on the Autonom-Fast-Test Simulator; any
 * other target (emulator-5554 above all) is refused before a Canvas starts or a device is
 * touched. Every run gets its own temporary AUTONOM_HOME (sessions, journal, proxy, the mock
 * registry), removed at the end unless --keep-home, and starts an Autonom session on the
 * target there (on iOS with --log-stream, so the log feed has a stream to follow).
 *
 * Panels and their device checks:
 *   app       Android: a requested runtime permission of --app-id (default com.android.chrome)
 *             is granted and revoked (in the order its state allows), each read back from
 *             `dumpsys package` and permissions.list, then put back; the location is set with a
 *             location-subscribing app (--location-app, default Google Maps) in front, read back
 *             (delivered, or requested only), Clear and an invalid fix are refused, and the
 *             emulator default 37.4220,-122.0840 is set again at the end.
 *             iOS: a privacy service this simctl offers (camera when it has it, else microphone,
 *             photos, ...) is granted to --app-id (default com.apple.mobilesafari), revoked, and
 *             reset at the end (simctl cannot read it back); the location is set and cleared;
 *             read-back refused.
 *   simulate  appearance dark/light read back from the device and restored; battery set and
 *             reset (skipped when the device already had an override); biometric match;
 *             Android network offline/online read back from `settings` (skipped unless Wi-Fi
 *             and mobile data were both on); iOS push to --push-app (default com.apple.Preferences:
 *             the Simulator refuses pushes to Safari); refusals for push on Android, network on
 *             iOS and a control outside the allowlist.
 *   network   status, a start without consent refused, start, attach (Android: the global
 *             http_proxy setting changes, and is back as found after detach), a plain request
 *             made on the device through the proxy to a local test server of this run, which
 *             must be in network.requests within 5 s, detach, stop.
 *   mocks     add, a device request through the proxy that gets the mock's status, the flow
 *             listed as mocked, hit count 1, request details without bodies, update, disable,
 *             enable, remove, clear, an unknown id refused.
 *   logs      the feed starts (on iOS narrowed with `match` to this run's lines: a booted
 *             Simulator logs faster than any reader of the ring), shows a readiness line
 *             written on the device, the reader catches up with the newest line, a marker
 *             line written on the device (Android `log`, iOS `syslog` in the Simulator)
 *             arrives within 5 s, and the feed stops on request.
 *
 * Device requests: on Android `toybox nc` sends an absolute-form GET to the proxy that the
 * device's http_proxy setting names (its stdin is held open: nc closes the connection as soon
 * as its stdin ends, before the proxy answers). On iOS `xmllint` runs in the Simulator with the
 * proxy environment the attach records for launches (SIMCTL_CHILD_http_proxy), the attach's own
 * per-process mechanism; nothing on the host changes.
 *
 * The session journal is then checked for `canvas <op>` records and for the absence of the
 * mock body marker. With --ui the page is driven in headless Chromium through Playwright
 * (from PLAYWRIGHT_ROOT, default /Users/gmh-basket/pr/defuddle-eval) at 1440x900 and
 * 390x844, light and dark: each tab opens, nothing scrolls sideways, on the desktop the
 * inspector is hidden and the device not covered; one screenshot per width, theme and tab.
 *
 * Writes <evidence-dir>/tools-<platform>.json (and the screenshots). Evidence holds counts,
 * statuses, codes and test markers only: never log text, request or mock bodies, or the
 * Canvas token. Exits 0 when every panel and every restore passed, 1 otherwise, 2 on bad
 * arguments or a refused target. --dry-run prints the plan and touches nothing.
 */
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "../..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const AUTONOM = join(ROOT, "scripts/autonom.py");
const ANDROID_SERIAL = "emulator-5580";
const REFUSED_SERIAL = "emulator-5554";
const IOS_UDID = "3760A5A8-E59D-4AE6-B1AF-A626908D7B61";
const IOS_NAME = "Autonom-Fast-Test";
const DEFAULT_PLAYWRIGHT_ROOT = "/Users/gmh-basket/pr/defuddle-eval";
const EMULATOR_DEFAULT_FIX = { latitude: 37.422, longitude: -122.084 };
const TEST_FIX = { latitude: 52.3702, longitude: 4.8952 };
const PANELS = ["app", "simulate", "network", "mocks", "logs"];
const TABS = ["app", "simulate", "network", "mocks", "logs"];
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const SCHEMES = ["light", "dark"];
const MOCK_HOST = "autonom-tools-test.example";
const MOCK_STATUS = 418;
// The plan's limits for an observation, and how long the script waits to say what happened.
const CAPTURE_LIMIT_MS = 5000;
const CAPTURE_WAIT_MS = 15_000;
const MARKER_LIMIT_MS = 5000;
const MARKER_WAIT_MS = 15_000;
const LOG_PAGE = 500;
const PRIVACY_PREFERENCE = ["camera", "microphone", "photos", "location", "contacts", "calendar", "reminders"];
// A device-side URL: plain HTTP with a host (and port) and a path of safe characters only, so it
// can be written into one `adb shell` line without quoting.
const DEVICE_URL = /^http:\/\/[A-Za-z0-9.-]+(:\d{1,5})?\/[A-Za-z0-9._\/-]*$/;
// Secrets of this run (the Canvas token), taken out of everything written to the evidence.
const liveSecrets = new Set();

const USAGE = `usage: canvas_tools_live.mjs --platform android --serial ${ANDROID_SERIAL} | --platform ios --udid ${IOS_UDID}
  [--ui] [--evidence-dir DIR] [--app-id ID] [--location-app PKG] [--push-app BUNDLE] [--adb PATH] [--xcrun PATH]
  [--python PATH] [--keep-home] [--dry-run] [--help]
       canvas_tools_live.mjs --self-test`;

let args;
try {
  ({ values: args } = parseArgs({
    options: {
      platform: { type: "string" },
      serial: { type: "string" },
      udid: { type: "string" },
      ui: { type: "boolean", default: false },
      "evidence-dir": { type: "string" },
      "app-id": { type: "string" },
      "location-app": { type: "string", default: "com.google.android.apps.maps" },
      "push-app": { type: "string", default: "com.apple.Preferences" },
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
  console.error(`canvas_tools_live: ${message}`);
  console.error(USAGE);
  process.exit(2);
}

if (args.help) {
  console.log(USAGE);
  process.exit(0);
}

if (args["self-test"]) {
  selfTest();
  console.log("ok: canvas_tools_live self-test");
  process.exit(0);
}

// --- pure helpers (checked by --self-test) --------------------------------------------------

/**
 * `text` with the Canvas token taken out: every known secret of this run, a `token=` value in
 * a URL fragment or query (a Playwright navigation error quotes the whole Canvas URL), and a
 * Bearer value. Backslashes and quotes end a value, so serialized JSON stays valid.
 */
function redactSecrets(text, secrets = liveSecrets) {
  let out = String(text ?? "");
  for (const secret of secrets) if (secret) out = out.split(secret).join("<redacted>");
  return out
    .replace(/([#?&;]token=)[^&#\s"'<>\\]*/gi, "$1<redacted>")
    .replace(/(Bearer\s+)[^\s"'\\]+/gi, "$1<redacted>");
}

/** An error's message for the evidence: redacted first, then cut to `max` characters. */
function errorText(error, max = 300) {
  return redactSecrets(String(error?.message ?? error)).slice(0, max);
}

/**
 * The Android permission to flip, from the tools process's own `permissions.list`: CAMERA when
 * its state is known, else the first with a known state. The `granted` it reports decides the
 * action, so it is also what the restore puts back. null when no state is known.
 */
function planPermissionFlip(permissions) {
  const known = (permissions ?? []).filter((item) => typeof item?.granted === "boolean");
  const chosen = known.find((item) => item.name === "android.permission.CAMERA") ?? known[0];
  if (!chosen) return null;
  return { permission: chosen.name, granted: chosen.granted, action: chosen.granted ? "revoke" : "grant" };
}

/** The permissions.set action that puts the flipped permission back as found, or null. */
function permissionRestoreAction(found) {
  if (!found?.permission || typeof found.permissionGranted !== "boolean") return null;
  return found.permissionGranted ? "grant" : "reset";
}

/** The two permissions.set actions of the Android check: the flip first, then back. */
function permissionSequence(flip) {
  if (!flip) return [];
  return flip.action === "grant" ? ["grant", "revoke"] : ["revoke", "grant"];
}

/**
 * The iOS privacy service to grant and revoke: one of PRIVACY_PREFERENCE that this simctl
 * offers (permissions.list lists simctl's own services), else its first service; never `all`.
 * Xcode 27's simctl has no camera service, so camera is not assumed.
 */
function pickPrivacyService(permissions) {
  const names = (permissions ?? []).map((item) => item?.name).filter((name) => typeof name === "string" && name && name !== "all");
  return PRIVACY_PREFERENCE.find((name) => names.includes(name)) ?? names[0] ?? null;
}

function assertDeviceUrl(url) {
  if (!DEVICE_URL.test(url)) throw new Error(`not a plain device test URL: ${url}`);
  return url;
}

/** `host` and `port` of a proxy written as host:port (an http_proxy setting, an attach's device_proxy). */
function parseProxy(text) {
  const match = String(text ?? "").trim().match(/^([A-Za-z0-9.-]+):(\d{1,5})$/);
  return match && Number(match[2]) > 0 ? { host: match[1], port: Number(match[2]) } : null;
}

/**
 * The `adb shell` line that sends an absolute-form GET for `url` to the proxy at host:port.
 * toybox nc closes the connection as soon as its stdin ends, before the proxy has answered
 * (the first live run got no answer and no recorded flow), so stdin is held open for
 * `holdSeconds` while the answer is read.
 */
function androidProxyGetCommand(url, proxy, holdSeconds = 3) {
  assertDeviceUrl(url);
  if (!parseProxy(`${proxy?.host}:${proxy?.port}`)) throw new Error("no proxy host:port");
  const { host } = new URL(url);
  const request = `GET ${url} HTTP/1.1\\r\\nHost: ${host}\\r\\nConnection: close\\r\\n\\r\\n`;
  return `(printf '${request}'; sleep ${holdSeconds}) | toybox nc -w 5 ${proxy.host} ${proxy.port}`;
}

/**
 * simctl arguments and environment for a GET of `url` made inside the Simulator through the
 * proxy: `xmllint` (in the iOS runtime, with HTTP) reads http_proxy, which simctl passes from
 * SIMCTL_CHILD_http_proxy, the variable the attach records for launches.
 */
function iosProxyGet(url, proxy, udid, baseEnv = {}) {
  assertDeviceUrl(url);
  if (!parseProxy(`${proxy?.host}:${proxy?.port}`)) throw new Error("no proxy host:port");
  return {
    argv: ["simctl", "spawn", udid, "xmllint", "--noout", url],
    env: { ...baseEnv, SIMCTL_CHILD_http_proxy: `http://${proxy.host}:${proxy.port}` },
  };
}

/** The status of the first HTTP status line in `answer`, or null. */
function httpStatus(answer) {
  return Number(String(answer ?? "").match(/^HTTP\/1\.[01] (\d{3})/)?.[1]) || null;
}

/**
 * The recorded flow of this run among network.requests rows: its path holds the run's marker.
 * The marker is in the path, not the query: the flow store keeps the path without the query.
 */
function findFlow(rows, marker) {
  return (rows ?? []).find((item) => String(item?.path ?? "").includes(marker)) ?? null;
}

/** A timed observation passes when it was made and inside the plan's limit. */
function withinLimit(found, elapsedMs, limitMs) {
  return Boolean(found) && Number.isFinite(elapsedMs) && elapsedMs >= 0 && elapsedMs <= limitMs;
}

/** No wait before the next log read while pages come back full (the reader is behind), else `idleMs`. */
function nextReadDelay(pageLength, limit, idleMs) {
  return pageLength >= limit ? 0 : idleMs;
}

function selfTest() {
  const token = "AbC-_123tokenValue";
  const secrets = new Set([token]);
  const navigation = `page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:61234/#token=${token}`;
  for (const text of [navigation, `GET http://127.0.0.1:1/x?token=${token}&a=1`,
    `Authorization: Bearer ${token}`, `the token ${token} leaked`]) {
    assert.ok(!redactSecrets(text, secrets).includes(token), text);
  }
  // Unknown values are still caught by their shape.
  assert.equal(redactSecrets("at http://127.0.0.1:2/#token=zzz9", new Set()), "at http://127.0.0.1:2/#token=<redacted>");
  assert.equal(redactSecrets("http://h/?a=1&token=q1&b=2", new Set()), "http://h/?a=1&token=<redacted>&b=2");
  // A redacted JSON document still parses.
  const json = JSON.stringify({ error: `at "http://h/#token=${token}"` });
  assert.deepEqual(JSON.parse(redactSecrets(json, new Set())), { error: 'at "http://h/#token=<redacted>"' });
  liveSecrets.add(token);
  try {
    assert.ok(!errorText(new Error(navigation)).includes(token));
    assert.ok(!errorText(new Error(`${"x".repeat(290)} #token=${token}`)).includes(token.slice(0, 5)));
  } finally {
    liveSecrets.delete(token);
  }

  // The restore follows the tools process's own pre-change value, whatever dumpsys said.
  const flip = planPermissionFlip([{ name: "android.permission.RECORD_AUDIO", granted: false },
    { name: "android.permission.CAMERA", granted: true }]);
  assert.deepEqual(flip, { permission: "android.permission.CAMERA", granted: true, action: "revoke" });
  const found = { permission: flip.permission, permissionGranted: flip.granted, dumpsysGranted: null };
  assert.equal(permissionRestoreAction(found), "grant");
  assert.equal(permissionRestoreAction({ permission: "p", permissionGranted: false, dumpsysGranted: true }), "reset");
  assert.equal(permissionRestoreAction({ permission: "p", permissionGranted: null }), null);
  assert.equal(permissionRestoreAction({ permission: null, permissionGranted: true }), null);
  assert.deepEqual(planPermissionFlip([{ name: "android.permission.CAMERA", granted: null },
    { name: "android.permission.ACCESS_FINE_LOCATION", granted: false }]),
  { permission: "android.permission.ACCESS_FINE_LOCATION", granted: false, action: "grant" });
  assert.equal(planPermissionFlip([{ name: "android.permission.CAMERA", granted: null }]), null);
  assert.equal(planPermissionFlip(undefined), null);
  // Both directions are checked: granted reads back, and denied after revoke.
  assert.deepEqual(permissionSequence({ action: "grant" }), ["grant", "revoke"]);
  assert.deepEqual(permissionSequence({ action: "revoke" }), ["revoke", "grant"]);
  assert.deepEqual(permissionSequence(null), []);

  // iOS: only a service this simctl offers (Xcode 27 has no camera); never `all`.
  const xcode27 = ["all", "calendar", "contacts-limited", "contacts", "location", "photos", "microphone", "siri"]
    .map((name) => ({ name, granted: null }));
  assert.equal(pickPrivacyService(xcode27), "microphone");
  assert.equal(pickPrivacyService([{ name: "camera" }, { name: "microphone" }]), "camera");
  assert.equal(pickPrivacyService([{ name: "all" }, { name: "siri" }]), "siri");
  assert.equal(pickPrivacyService([{ name: "all" }]), null);
  assert.equal(pickPrivacyService(undefined), null);

  // Android device request: stdin stays open until the proxy has answered.
  assert.deepEqual(parseProxy("10.0.2.2:60136"), { host: "10.0.2.2", port: 60136 });
  assert.equal(parseProxy(":0"), null);
  assert.equal(parseProxy("10.0.2.2:0"), null);
  assert.equal(parseProxy("host:1; reboot"), null);
  const command = androidProxyGetCommand("http://autonom-tools-test.example/autonom-tools-test/ab12", { host: "10.0.2.2", port: 60136 });
  assert.equal(command, "(printf 'GET http://autonom-tools-test.example/autonom-tools-test/ab12 HTTP/1.1\\r\\n" +
    "Host: autonom-tools-test.example\\r\\nConnection: close\\r\\n\\r\\n'; sleep 3) | toybox nc -w 5 10.0.2.2 60136");
  assert.match(androidProxyGetCommand("http://127.0.0.1:4567/autonom-tools-capture/ab12", { host: "10.0.2.2", port: 1 }),
    /Host: 127\.0\.0\.1:4567\\r\\n/);
  for (const url of ["http://h/x?run=1", "http://h/x'; reboot", "https://h/x", "http://h/x y"]) {
    assert.throws(() => androidProxyGetCommand(url, { host: "10.0.2.2", port: 1 }), /not a plain device test URL/, url);
  }
  assert.throws(() => androidProxyGetCommand("http://h/x", null), /no proxy/);

  // iOS device request: xmllint inside the Simulator with the attach's proxy variable.
  const ios = iosProxyGet("http://autonom-tools-test.example/autonom-tools-test/ab12", { host: "127.0.0.1", port: 62326 },
    IOS_UDID, { AUTONOM_HOME: "/tmp/h" });
  assert.deepEqual(ios.argv, ["simctl", "spawn", IOS_UDID, "xmllint", "--noout",
    "http://autonom-tools-test.example/autonom-tools-test/ab12"]);
  assert.deepEqual(ios.env, { AUTONOM_HOME: "/tmp/h", SIMCTL_CHILD_http_proxy: "http://127.0.0.1:62326" });
  assert.throws(() => iosProxyGet("http://h/x", { host: "127.0.0.1" }, IOS_UDID), /no proxy/);

  assert.equal(httpStatus("HTTP/1.1 418 I'm a teapot\r\ncontent-length: 5\r\n\r\nhello"), 418);
  assert.equal(httpStatus(""), null);
  assert.equal(httpStatus("garbage HTTP/1.1 200 OK"), null);

  // The flow store keeps the path without the query: the run marker must be in the path.
  const rows = [{ id: "f_1", path: "/generate_204" }, { id: "f_2", path: "/autonom-tools-test/ab12", url: "http://h/autonom-tools-test/ab12" }];
  assert.equal(findFlow(rows, "ab12").id, "f_2");
  assert.equal(findFlow([{ id: "f_3", path: "/autonom-tools-test", url: "http://h/autonom-tools-test?run=ab12" }], "ab12"), null);
  assert.equal(findFlow(undefined, "ab12"), null);

  // Timed observations: inside the limit only, and only when made.
  assert.equal(withinLimit(true, 5000, 5000), true);
  assert.equal(withinLimit(true, 5001, 5000), false);
  assert.equal(withinLimit(false, 10, 5000), false);
  assert.equal(withinLimit(true, null, 5000), false);
  assert.equal(withinLimit({ id: "f" }, 0, 5000), true);

  // A reader behind a burst reads again at once; a caught-up one waits.
  assert.equal(nextReadDelay(500, 500, 200), 0);
  assert.equal(nextReadDelay(499, 500, 200), 200);
  assert.equal(nextReadDelay(0, 500, 200), 200);
}

// The target guard runs before anything else: no Canvas, no adb, no simctl for another device.
const platform = args.platform;
if (platform !== "android" && platform !== "ios") usage("--platform must be android or ios");
if (platform === "android") {
  if (args.udid) usage("--udid is for --platform ios");
  if (args.serial === REFUSED_SERIAL) usage(`${REFUSED_SERIAL} is never used by this check`);
  if (args.serial !== ANDROID_SERIAL) usage(`Android runs only on --serial ${ANDROID_SERIAL}, not ${args.serial ?? "(none)"}`);
} else {
  if (args.serial) usage("--serial is for --platform android");
  if (args.udid !== IOS_UDID) usage(`iOS runs only on ${IOS_NAME} (--udid ${IOS_UDID}), not ${args.udid ?? "(none)"}`);
}
const PACKAGE_ID = /^[A-Za-z0-9_][A-Za-z0-9_.]{0,254}$/;
const appId = args["app-id"] ?? (platform === "android" ? "com.android.chrome" : "com.apple.mobilesafari");
if (!PACKAGE_ID.test(appId)) usage("--app-id must be a package or bundle id");
if (!PACKAGE_ID.test(args["location-app"])) usage("--location-app must be a package id");
if (!PACKAGE_ID.test(args["push-app"])) usage("--push-app must be a bundle id");
const target = platform === "android" ? ANDROID_SERIAL : IOS_UDID;
const playwrightRoot = process.env.PLAYWRIGHT_ROOT || DEFAULT_PLAYWRIGHT_ROOT;

if (args["dry-run"]) {
  console.log(JSON.stringify({
    dry_run: true, platform, target, app_id: appId,
    location_app: platform === "android" ? args["location-app"] : null,
    push_app: platform === "ios" ? args["push-app"] : null,
    panels: PANELS, ui: args.ui ? { playwright_root: playwrightRoot, viewports: VIEWPORTS, schemes: SCHEMES, tabs: TABS } : null,
    evidence: join(resolve(args["evidence-dir"] ?? "<temporary directory>"), `tools-${platform}.json`),
    restores: platform === "android"
      ? ["permission as found", `location ${EMULATOR_DEFAULT_FIX.latitude},${EMULATOR_DEFAULT_FIX.longitude}`, "location app stopped if started here",
        "appearance as found", "battery reset", "network online", "proxy detached and stopped", "mocks removed"]
      : ["privacy service reset", "location cleared", "appearance as found", "battery (status bar) reset",
        "proxy detached and stopped", "mocks removed", "Simulator shut down again if booted here"],
  }, null, 2));
  process.exit(0);
}

const outDir = resolve(args["evidence-dir"] ?? await mkdtemp(join(tmpdir(), "autonom-canvas-tools-live-")));
await mkdir(outDir, { recursive: true });
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-canvas-tools-home-"));
const liveEnv = { ...process.env, AUTONOM_HOME: autonomHome };
const runId = randomBytes(4).toString("hex");
// Every log line this run writes holds logMarker (the journal must never hold it); the
// readiness lines and the timed marker are told apart by their ends.
const logMarker = `autonom-tools-marker-${runId}`;
const readyMarker = `${logMarker}-ready`;
const timedMarker = `${logMarker}-now`;
const bodyMarker = `autonom-tools-body-${runId}`;

const report = {
  platform, target, app_id: appId, started_at: new Date().toISOString(),
  panels: Object.fromEntries(PANELS.map((name) => [name, { ok: false, steps: [] }])),
  restore: { ok: false, steps: [] }, journal: null, ui: null, ok: false,
};

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function run(file, argv, { timeout = 60_000, env = liveEnv, input } = {}) {
  return new Promise((resolvePromise) => {
    const child = execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (error.code ?? 1) : 0, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
      });
    if (input !== undefined) child.stdin.end(input);
  });
}

/** adb on the one test emulator; the serial is fixed above and never taken from elsewhere. */
async function adb(argv, options) {
  return await run(args.adb, ["-s", ANDROID_SERIAL, ...argv], options);
}

async function shell(command) {
  return (await adb(["shell", command])).stdout.trim();
}

async function simctl(argv, options) {
  return await run(args.xcrun, ["simctl", ...argv], options);
}

/** The Autonom CLI on this target, inside this run's own AUTONOM_HOME. */
async function autonom(argv, options = {}) {
  // Tool paths only when given: the CLI finds adb and xcrun itself otherwise.
  const targetFlags = platform === "android"
    ? ["--serial", ANDROID_SERIAL, ...(args.adb !== "adb" ? ["--adb", args.adb] : [])]
    : ["--platform", "ios", "--udid", IOS_UDID, ...(args.xcrun !== "xcrun" ? ["--simctl", args.xcrun] : [])];
  return await run(args.python, [AUTONOM, ...targetFlags, ...argv], { timeout: 180_000, ...options });
}

async function waitFor(predicate, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value || Date.now() >= deadline) return value;
    await sleep(intervalMs);
  }
}

// --- the Canvas ----------------------------------------------------------------------------

async function startCanvas() {
  const token = randomBytes(18).toString("base64url");
  liveSecrets.add(token);
  const argv = platform === "android"
    ? [CANVAS, "--platform", "android", "--serial", ANDROID_SERIAL, "--adb", args.adb]
    : [CANVAS, "--platform", "ios", "--target", IOS_UDID, "--simctl", args.xcrun];
  argv.push("--port", "0", "--token", token, "--transport", "auto", "--python", args.python);
  const child = spawn(process.execPath, argv, { cwd: ROOT, env: liveEnv, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  const stop = async () => {
    if (!running()) return await exited;
    child.kill("SIGTERM");
    const code = await Promise.race([exited, sleep(8000).then(() => "timeout")]);
    if (code === "timeout" && running()) child.kill("SIGKILL");
    return await exited;
  };
  let url;
  try {
    url = await new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error("the Canvas did not print its URL within 45 s")), 45_000);
      child.stdout.on("data", () => {
        const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/);
        if (!match) return;
        clearTimeout(timer);
        resolvePromise(match[1]);
      });
      exited.then((code) => {
        clearTimeout(timer);
        reject(new Error(`the Canvas exited ${code} before it started`));
      });
    });
  } catch (error) {
    await stop();
    throw error;
  }
  const origin = new URL(url).origin;
  const headers = { Authorization: `Bearer ${token}`, "X-Autonom-Origin": "agent" };
  const request = async (method, path, body) => {
    const response = await fetch(`${origin}${path}`, {
      method,
      headers: body === undefined ? headers : { ...headers, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    return { status: response.status, body: payload ?? {} };
  };
  return {
    url, origin, stop, running,
    call: (op, payload = {}) => request("POST", "/tools/call", { op, payload }),
    logs: (after, limit = 500) => request("GET", `/tools/logs?after=${after}&limit=${limit}`),
    feed: (active, packageName = null, match = null) => request("POST", "/tools/logs", { active, package: packageName, match }),
    status: () => request("GET", "/status"),
  };
}

// --- recording ------------------------------------------------------------------------------

/** One step: its name, ok, and only the fields given (never bodies or log text). */
function step(section, name, ok, detail = {}) {
  const entry = { name, ok: Boolean(ok), ...detail };
  section.steps.push(entry);
  return entry;
}

/** The status, code and outcome of a /tools/call answer, for the evidence. */
function brief(answer) {
  return { http: answer.status, error_code: answer.body?.error_code ?? null };
}

function expectOk(section, name, answer, check = () => true, detail = {}) {
  const ok = answer.status === 200 && answer.body?.ok === true && Boolean(check(answer.body.result ?? {}));
  step(section, name, ok, { ...brief(answer), ...detail });
  return ok ? answer.body.result ?? {} : null;
}

function expectRefused(section, name, answer, status, code) {
  return step(section, name, answer.status === status && answer.body?.error_code === code,
    { ...brief(answer), expected: { http: status, error_code: code } });
}

async function panel(name, work) {
  const section = report.panels[name];
  try {
    await work(section);
    section.ok = section.steps.length > 0 && section.steps.every((entry) => entry.ok || entry.skipped);
  } catch (error) {
    step(section, "panel error", false, { error: errorText(error, 300) });
    section.ok = false;
  }
}

/** A restore step runs whatever failed before it. */
async function restoreStep(name, work) {
  try {
    const result = await work();
    const detailed = typeof result === "object" && result !== null;
    const { ok = true, ...detail } = detailed ? result : {};
    step(report.restore, name, detailed ? ok : result !== false, detail);
  } catch (error) {
    step(report.restore, name, false, { error: errorText(error, 300) });
  }
}

// --- device reads ---------------------------------------------------------------------------

/** granted true/false of `permission` from the runtime permissions of `dumpsys package`, or null. */
async function androidGranted(packageName, permission) {
  const dump = await shell(`dumpsys package ${packageName}`);
  const runtime = dump.slice(Math.max(0, dump.indexOf("runtime permissions:")));
  const match = runtime.match(new RegExp(`${permission.replace(/\./g, "\\.")}: granted=(true|false)`));
  return match ? match[1] === "true" : null;
}

async function androidNightMode() {
  return (await shell("cmd uimode night")).match(/Night mode:\s*(\w+)/)?.[1]?.toLowerCase() ?? null;
}

async function iosAppearance() {
  return (await simctl(["ui", IOS_UDID, "appearance"])).stdout.trim().toLowerCase() || null;
}

/** Whether the Simulator's status bar has no overrides, read by the library's own parser. */
async function iosStatusBarClean() {
  const code = "import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom autonom_lib import ios_simctl\n" +
    "overrides, why = ios_simctl.read_status_bar(sys.argv[2], sys.argv[3])\nprint(json.dumps({'overrides': overrides, 'why': why}))";
  const result = await run(args.python, ["-c", code, join(ROOT, "scripts"), args.xcrun, IOS_UDID]);
  try {
    const parsed = JSON.parse(result.stdout.trim().split("\n").pop());
    return parsed.why === null && parsed.overrides !== null && Object.keys(parsed.overrides).length === 0;
  } catch {
    return false;
  }
}

async function androidPackageRunning(packageName) {
  return (await shell(`pidof ${packageName}`)) !== "";
}

// --- Android panels -------------------------------------------------------------------------

const state = { permission: null, permissionGranted: null, dumpsysGranted: null, privacyService: null, deviceProxy: null,
  captureServer: null, locationAppStarted: false, nightMode: null,
  appearance: null, batteryChanged: false, networkToggled: false, httpProxy: null, proxyStarted: false,
  attached: false, mockIds: new Set(), appSet: false, locationSet: false, iosStatusBarClean: false };

async function androidApp(canvas, section) {
  const context = expectOk(section, "context", await canvas.call("context"),
    (result) => result.platform === "android" && result.session?.app_id === appId && result.location_clearable === false);
  if (!context) return;
  const listed = expectOk(section, "permissions.list", await canvas.call("permissions.list", { app_id: appId }),
    (result) => result.readable === true && Array.isArray(result.permissions) && result.permissions.length > 0);
  const flip = listed ? planPermissionFlip(listed.permissions) : null;
  if (listed && !flip) {
    // The plan needs a granted-then-denied read-back: without a known state it cannot be made.
    step(section, "permission with a known granted state", false, { reason: "permissions.list knows no granted state" });
  }
  if (flip) {
    const chosen = { name: flip.permission, granted: flip.granted };
    // The restore puts back the value that decided the action, not the separate dumpsys read.
    state.permission = chosen.name;
    state.permissionGranted = chosen.granted;
    state.dumpsysGranted = await androidGranted(appId, chosen.name);
    step(section, "permission state matches dumpsys", state.dumpsysGranted === chosen.granted,
      { permission: chosen.name, granted: chosen.granted, dumpsys_granted: state.dumpsysGranted });
    for (const action of permissionSequence(flip)) {
      const want = action === "grant";
      if (!expectOk(section, `permissions.set ${action}`, await canvas.call("permissions.set",
        { app_id: appId, service: chosen.name, action }))) break;
      state.appSet = true;
      const now = await androidGranted(appId, chosen.name);
      step(section, `dumpsys reads ${want ? "granted" : "denied"}`, now === want, { granted: now });
      const again = await canvas.call("permissions.list", { app_id: appId });
      expectOk(section, `permissions.list reads ${want ? "granted" : "denied"}`, again,
        (result) => result.permissions.find((item) => item.name === chosen.name)?.granted === want);
    }
  }
  // Location: an app that subscribes to updates, so the emulator delivers the fix.
  const locationApp = args["location-app"];
  const installed = (await shell(`pm path ${locationApp}`)).startsWith("package:");
  if (installed) {
    const wasRunning = await androidPackageRunning(locationApp);
    await shell(`monkey -p ${locationApp} -c android.intent.category.LAUNCHER 1`);
    state.locationAppStarted = !wasRunning;
    await sleep(3000);
  }
  step(section, "location app in front", installed, { location_app: locationApp, installed, skipped: !installed });
  const set = expectOk(section, "location.set", await canvas.call("location.set", TEST_FIX),
    (result) => result.via === "emulator_console");
  if (set) {
    state.locationSet = true;
    const close = (a, b) => Math.abs(a - b) < 0.001;
    const read = await waitFor(async () => {
      const answer = await canvas.call("location.get");
      const result = answer.body?.result;
      return result && result.delivered === true ? answer : null;
    }, 15_000, 1500) ?? await canvas.call("location.get");
    const result = read.body?.result ?? {};
    const delivered = result.delivered === true && close(result.latitude, TEST_FIX.latitude) && close(result.longitude, TEST_FIX.longitude);
    const requestedOnly = result.delivered === false && result.requested &&
      close(result.requested.latitude, TEST_FIX.latitude) && close(result.requested.longitude, TEST_FIX.longitude);
    step(section, "location.get delivered or requested only", read.status === 200 && (delivered || requestedOnly),
      { ...brief(read), delivered: result.delivered ?? null });
  }
  expectRefused(section, "location.clear refused on Android", await canvas.call("location.clear"), 409, "unsupported_on_platform");
  expectRefused(section, "invalid coordinates refused", await canvas.call("location.set", { latitude: 91, longitude: -181 }),
    400, "invalid_coordinates");
}

async function androidSimulate(canvas, section) {
  state.nightMode = await androidNightMode();
  for (const mode of ["dark", "light"]) {
    const result = expectOk(section, `appearance ${mode}`, await canvas.call("simulate", { control: "appearance", action: mode }),
      (value) => value.verified === true);
    if (result) state.appearance = mode;
    step(section, `uimode reads ${mode}`, (await androidNightMode()) === (mode === "dark" ? "yes" : "no"));
  }
  const battery = await shell("dumpsys battery");
  if (battery.includes("UPDATES STOPPED")) {
    step(section, "battery", true, { skipped: true, reason: "the battery already had an override" });
  } else {
    state.batteryChanged = true;
    expectOk(section, "battery set 42", await canvas.call("simulate", { control: "battery", action: "set", values: { level: 42 } }),
      (value) => value.verified === true);
    step(section, "dumpsys battery level 42", /\n\s*level: 42\b/.test(`\n${await shell("dumpsys battery")}`));
    if (expectOk(section, "battery reset", await canvas.call("simulate", { control: "battery", action: "reset" }))) {
      state.batteryChanged = false;
    }
    step(section, "dumpsys battery not overridden", !(await shell("dumpsys battery")).includes("UPDATES STOPPED"));
  }
  expectOk(section, "biometric match", await canvas.call("simulate", { control: "biometric", action: "match" }));
  const wifi = await shell("settings get global wifi_on");
  const data = await shell("settings get global mobile_data");
  if (wifi === "1" && data === "1") {
    state.networkToggled = true;
    expectOk(section, "network offline", await canvas.call("simulate", { control: "network", action: "offline" }));
    step(section, "Wi-Fi off", (await waitFor(async () => (await shell("settings get global wifi_on")) === "0", 10_000)) === true);
    if (expectOk(section, "network online", await canvas.call("simulate", { control: "network", action: "online" }))) {
      state.networkToggled = false;
    }
    step(section, "Wi-Fi on", (await waitFor(async () => (await shell("settings get global wifi_on")) === "1", 10_000)) === true);
  } else {
    step(section, "network offline/online", true, { skipped: true, reason: "Wi-Fi and mobile data were not both on" });
  }
  expectRefused(section, "push refused on Android",
    await canvas.call("simulate", { control: "push", action: "send", values: { app_id: appId, payload: { aps: {} } } }),
    409, "unsupported_capability");
  expectRefused(section, "control outside the allowlist refused",
    await canvas.call("simulate", { control: "clipboard", action: "set" }), 400, "flow_command_invalid");
}

// --- iOS panels -----------------------------------------------------------------------------

async function iosApp(canvas, section) {
  expectOk(section, "context", await canvas.call("context"),
    (result) => result.platform === "ios" && result.session !== null && result.location_readable === false &&
      result.location_clearable === true);
  const listed = expectOk(section, "permissions.list", await canvas.call("permissions.list", { app_id: appId }),
    (result) => result.readable === false && result.permissions.length > 0 &&
      result.permissions.every((item) => item.granted === null));
  // The services come from this simctl's own list: Xcode 27 has no camera.
  const service = pickPrivacyService(listed?.permissions);
  step(section, "privacy service offered by simctl", service !== null, { service });
  if (service) {
    for (const action of ["grant", "revoke"]) {
      const answer = await canvas.call("permissions.set", { app_id: appId, service, action });
      if (answer.status === 200) {
        state.privacyService = service;
        state.appSet = true;
      }
      if (!expectOk(section, `permissions.set ${action} ${service}`, answer,
        (result) => result.action === action && result.service === service && result.app_id === appId)) break;
    }
  }
  expectRefused(section, "location.get refused on iOS", await canvas.call("location.get"), 409, "unsupported_on_platform");
  if (expectOk(section, "location.set", await canvas.call("location.set", TEST_FIX), (result) => result.via === "simctl")) {
    state.locationSet = true;
  }
  if (expectOk(section, "location.clear", await canvas.call("location.clear"))) state.locationSet = false;
  expectRefused(section, "invalid coordinates refused", await canvas.call("location.set", { latitude: "north", longitude: 4 }),
    400, "invalid_coordinates");
}

async function iosSimulate(canvas, section) {
  state.nightMode = await iosAppearance();
  for (const mode of ["dark", "light"]) {
    if (expectOk(section, `appearance ${mode}`, await canvas.call("simulate", { control: "appearance", action: mode }),
      (value) => value.observed === mode)) state.appearance = mode;
    step(section, `simctl reads ${mode}`, (await iosAppearance()) === mode);
  }
  const clean = await iosStatusBarClean();
  if (!clean) {
    step(section, "battery", true, { skipped: true, reason: "the status bar already had overrides (reset would clear them)" });
  } else {
    state.iosStatusBarClean = true;
    state.batteryChanged = true;
    expectOk(section, "battery set 42 charging",
      await canvas.call("simulate", { control: "battery", action: "set", values: { level: 42, state: "charging" } }),
      (value) => value.verified === true);
    if (expectOk(section, "battery reset", await canvas.call("simulate", { control: "battery", action: "reset" }))) {
      state.batteryChanged = false;
    }
  }
  // The Simulator refuses pushes to Safari ("Source is not authorized"): a Settings push is accepted.
  expectOk(section, "push send",
    await canvas.call("simulate", { control: "push", action: "send",
      values: { app_id: args["push-app"], payload: { aps: { alert: { title: "Autonom", body: "Tools live check" } } } } }),
    (result) => result.app_id === args["push-app"], { app_id: args["push-app"] });
  expectOk(section, "biometric match", await canvas.call("simulate", { control: "biometric", action: "match" }));
  expectRefused(section, "network refused on iOS",
    await canvas.call("simulate", { control: "network", action: "offline" }), 409, "unsupported_capability");
  expectRefused(section, "control outside the allowlist refused",
    await canvas.call("simulate", { control: "status-bar", action: "clear" }), 400, "flow_command_invalid");
}

// --- shared panels --------------------------------------------------------------------------

/**
 * A local HTTP server of this run on 127.0.0.1: the target of the plain device request. The
 * device reaches it only through the proxy (which runs on this host), so a flow to it in
 * network.requests is a request the device made through the proxy.
 */
async function startCaptureServer() {
  const server = { hits: 0, close: null, port: null };
  const http = createServer((request, response) => {
    if (String(request.url ?? "").includes(runId)) server.hits += 1;
    response.writeHead(200, { "Content-Type": "text/plain", Connection: "close" });
    response.end("autonom tools capture\n");
  });
  await new Promise((resolvePromise, reject) => {
    http.once("error", reject);
    http.listen(0, "127.0.0.1", resolvePromise);
  });
  server.port = http.address().port;
  server.close = () => new Promise((resolvePromise) => {
    http.closeAllConnections?.();
    http.close(() => resolvePromise());
  });
  return server;
}

/** The proxy the device uses: Android's http_proxy setting, iOS's attach `device_proxy`. */
async function deviceProxyNow(attachResult) {
  if (platform === "android") return parseProxy(await shell("settings get global http_proxy"));
  return parseProxy(attachResult?.device_proxy);
}

/**
 * A GET of `url` made on the device through `proxy`. Android: the HTTP status the device read
 * (toybox nc). iOS: xmllint reports no status, so `status` is null and the recorded flow says it.
 */
async function deviceGet(url, proxy) {
  if (platform === "android") {
    const answer = await adb(["shell", androidProxyGetCommand(url, proxy)], { timeout: 30_000 });
    return { code: answer.code, status: httpStatus(answer.stdout.trim()) };
  }
  const { argv, env } = iosProxyGet(url, proxy, IOS_UDID, liveEnv);
  const answer = await run(args.xcrun, argv, { timeout: 30_000, env });
  // xmllint exits non-zero for a body that is not XML; the request itself still went out.
  return { code: answer.code, status: null };
}

/**
 * Send a device request for `url` and wait until network.requests (filtered by `host`) lists
 * its flow: the flow, the milliseconds from sending to the listing, and the device's answer.
 * The wait goes on past the plan's limit, up to CAPTURE_WAIT_MS, so a slow capture is told
 * apart from none.
 */
async function deviceRequestCaptured(canvas, url, host, proxy) {
  const started = Date.now();
  // Never left rejected while the listing is polled: a failure is reported, not thrown.
  const sent = deviceGet(url, proxy).catch((error) => ({ code: null, status: null, error: errorText(error, 200) }));
  let flow = null;
  let elapsed = null;
  let listings = 0;
  while (Date.now() - started < CAPTURE_WAIT_MS) {
    const answer = await canvas.call("network.requests", { host, max: 50 });
    listings += 1;
    flow = findFlow(answer.body?.result?.requests, runId);
    if (flow) {
      elapsed = Date.now() - started;
      break;
    }
    await sleep(250);
  }
  const device = await sent;
  return { flow, elapsed, device, listings };
}

async function networkPanel(canvas, section) {
  expectOk(section, "network.status before", await canvas.call("network.status"), (result) => result.proxy?.running === false);
  expectRefused(section, "start without consent refused", await canvas.call("network.start", {}), 400, "consent_required");
  if (expectOk(section, "network.start", await canvas.call("network.start", { acknowledged: true }))) state.proxyStarted = true;
  expectOk(section, "network.status running", await canvas.call("network.status"), (result) => result.proxy?.running === true);
  if (platform === "android") state.httpProxy = await shell("settings get global http_proxy");
  const attached = expectOk(section, "network.attach", await canvas.call("network.attach", { acknowledged: true }));
  if (attached) state.attached = true;
  if (platform === "android") {
    const now = await shell("settings get global http_proxy");
    step(section, "device http_proxy points at the proxy", /^10\.0\.2\.2:\d+$/.test(now), { http_proxy: now });
  } else if (attached) {
    step(section, "attach records the launch proxy", /^127\.0\.0\.1:\d+$/.test(String(attached.device_proxy ?? "")),
      { device_proxy: attached.device_proxy ?? null, mode: attached.mode ?? null });
  }
  state.deviceProxy = attached ? await deviceProxyNow(attached) : null;
  if (!state.deviceProxy) {
    step(section, "a device request captured within 5 s", false, { reason: "no device proxy after attach" });
    return;
  }
  // A plain request (no mock) from the device to this run's local server, through the proxy.
  state.captureServer = await startCaptureServer();
  const url = `http://127.0.0.1:${state.captureServer.port}/autonom-tools-capture/${runId}`;
  const capture = await deviceRequestCaptured(canvas, url, "127.0.0.1", state.deviceProxy);
  const flow = capture.flow;
  step(section, "a device request captured within 5 s",
    withinLimit(flow, capture.elapsed, CAPTURE_LIMIT_MS) && flow.status === 200 && !flow.mocked && state.captureServer.hits >= 1,
    { elapsed_ms: capture.elapsed, limit_ms: CAPTURE_LIMIT_MS, flow_status: flow?.status ?? null, mocked: flow?.mocked ?? null,
      device_status: capture.device.status, device_exit: capture.device.code, device_error: capture.device.error ?? null,
      server_hits: state.captureServer.hits, listings: capture.listings });
}

async function mocksPanel(canvas, section) {
  const added = expectOk(section, "mocks.add", await canvas.call("mocks.add",
    { url_glob: `*${MOCK_HOST}/autonom-tools-test*`, status: MOCK_STATUS, body: bodyMarker, note: "tools live check" }),
  (result) => typeof result.id === "string" || typeof result.id === "number");
  if (!added) return;
  const id = String(added.id);
  state.mockIds.add(id);
  if (state.attached && state.deviceProxy) {
    // A request from the device itself, through the proxy it uses; the run marker is in the path.
    const url = `http://${MOCK_HOST}/autonom-tools-test/${runId}`;
    const sent = await deviceRequestCaptured(canvas, url, MOCK_HOST, state.deviceProxy);
    const flow = sent.flow;
    if (platform === "android") {
      step(section, "device request gets the mock's status", sent.device.status === MOCK_STATUS,
        { status: sent.device.status, expected: MOCK_STATUS });
    } else {
      // xmllint shows no status: the proxy's record of what the device was answered decides.
      step(section, "device request sent from the Simulator", sent.device.code !== null && flow !== null,
        { exit: sent.device.code });
    }
    step(section, "network.requests lists it as mocked with the mock's status",
      Boolean(flow && flow.mocked === true && flow.status === MOCK_STATUS && (flow.mock_id == null || String(flow.mock_id) === id)),
      { status: flow?.status ?? null, mocked: flow?.mocked ?? null, elapsed_ms: sent.elapsed });
    if (flow) {
      const detail = await canvas.call("network.request", { id: flow.id });
      const request = detail.body?.result?.request ?? {};
      step(section, "network.request has previews only", detail.status === 200 &&
        !Object.keys(request).some((key) => /body$/.test(key) && !/preview/.test(key)), brief(detail));
    }
    const hits = await canvas.call("mocks.list");
    expectOk(section, "mocks.list counts one hit", hits,
      (result) => (result.mocks.find((mock) => String(mock.id) === id)?.hits ?? 0) === 1,
      { hits: hits.body?.result?.mocks?.find((mock) => String(mock.id) === id)?.hits ?? null });
  } else {
    // The plan needs a device request answered by the mock: without an attach it is not made.
    step(section, "device request through the proxy", false, { reason: "the proxy is not attached" });
  }
  expectOk(section, "mocks.update status 503", await canvas.call("mocks.update", { id, status: 503 }));
  expectOk(section, "mocks.list shows 503", await canvas.call("mocks.list"),
    (result) => result.mocks.find((mock) => String(mock.id) === id)?.response?.status === 503);
  expectOk(section, "mocks.disable", await canvas.call("mocks.disable", { id }));
  expectOk(section, "mocks.list shows it disabled", await canvas.call("mocks.list"),
    (result) => result.mocks.find((mock) => String(mock.id) === id)?.enabled === false);
  expectOk(section, "mocks.enable", await canvas.call("mocks.enable", { id }));
  if (expectOk(section, "mocks.remove", await canvas.call("mocks.remove", { id }),
    (result) => String(result.removed) === id)) state.mockIds.delete(id);
  expectRefused(section, "unknown mock refused", await canvas.call("mocks.remove", { id }), 404, "mock_not_found");
  for (const n of [1, 2]) {
    const extra = expectOk(section, `mocks.add ${n} for clear`, await canvas.call("mocks.add",
      { url_glob: `*${MOCK_HOST}/clear-${n}*`, status: 200 }));
    if (extra) state.mockIds.add(String(extra.id));
  }
  if (expectOk(section, "mocks.clear", await canvas.call("mocks.clear"), (result) => result.removed === 2)) state.mockIds.clear();
  expectOk(section, "mocks.list empty", await canvas.call("mocks.list"), (result) => result.mocks.length === 0);
}

async function networkStop(canvas, section) {
  if (state.attached && expectOk(section, "network.detach", await canvas.call("network.detach"))) state.attached = false;
  if (platform === "android" && state.httpProxy !== null) {
    const now = await shell("settings get global http_proxy");
    step(section, "device http_proxy back as found", now === state.httpProxy);
  }
  if (state.proxyStarted && expectOk(section, "network.stop", await canvas.call("network.stop"))) state.proxyStarted = false;
  expectOk(section, "network.status stopped", await canvas.call("network.status"), (result) => result.proxy?.running === false);
}

/** Write `text` into the device log: Android `log`, iOS `syslog` inside the Simulator. */
async function writeLogLine(text) {
  // The iOS runtime has no `logger`; its `syslog -s` sends a message into the unified log.
  return platform === "android"
    ? await adb(["shell", "log", "-t", "AutonomTools", text])
    : await simctl(["spawn", IOS_UDID, "syslog", "-s", "-l", "notice", text]);
}

async function logsPanel(canvas, section) {
  // iOS: a booted Simulator logs thousands of lines a second, more than a reader of the
  // 2000-line ring can keep up with (the first live run read 4000 lines and lost 81704), so
  // the feed is narrowed at its source to this run's lines with `match`. Android follows
  // the whole log, as the page does by default.
  const match = platform === "ios" ? logMarker : null;
  const started = await canvas.feed(true, null, match);
  step(section, "feed started", started.status === 200 && (started.body?.match ?? null) === match,
    { http: started.status, match: started.body?.match ?? null });
  const first = await waitFor(async () => {
    const read = await canvas.logs(0, 1);
    return read.body?.running ? read : null;
  }, 10_000);
  step(section, "feed running", Boolean(first), { error: first ? null : errorText((await canvas.logs(0, 1)).body?.error ?? "", 200) });
  let after = 0;
  let lines = 0;
  let dropped = 0;
  // `running` is true once the follow process is spawned, but it starts at the device's
  // "now", which it reads only after it has started (more than a second on a loaded host):
  // a line written before that is never in the feed. The first live run wrote its marker
  // in that gap. So a readiness line is written (again every 1.5 s) until the feed shows
  // one, and only then is the timed marker written.
  const readyStart = Date.now();
  let ready = false;
  let lastWrite = 0;
  while (!ready && Date.now() - readyStart < 20_000) {
    if (Date.now() - lastWrite >= 1500) {
      lastWrite = Date.now();
      await writeLogLine(readyMarker);
    }
    const read = await canvas.logs(after, LOG_PAGE);
    const got = read.body?.lines ?? [];
    after = read.body?.next ?? after;
    ready = got.some((line) => JSON.stringify(line).includes(readyMarker));
    if (!ready) await sleep(nextReadDelay(got.length, LOG_PAGE, 200));
  }
  step(section, "feed delivers device lines", ready, { ready_ms: Date.now() - readyStart });
  // Catch up with the newest line first, reading again at once while pages come back full,
  // so only lines after the marker is written are left to scan (an iOS Simulator can log
  // thousands of lines a second).
  const catchUp = Date.now();
  let caughtUp = false;
  while (!caughtUp && Date.now() - catchUp < 10_000) {
    const read = await canvas.logs(after, LOG_PAGE);
    const got = read.body?.lines ?? [];
    lines += got.length;
    dropped += Number(read.body?.dropped) || 0;
    after = read.body?.next ?? after;
    caughtUp = got.length < LOG_PAGE;
  }
  step(section, "reader caught up with the newest line", caughtUp,
    { catch_up_ms: Date.now() - catchUp, lines_read: lines, dropped });
  const sentAt = Date.now();
  const wrote = await writeLogLine(timedMarker);
  step(section, "marker written on the device", wrote.code === 0, { marker: timedMarker, exit: wrote.code });
  let elapsed = null;
  let last = null;
  const before = { lines, dropped };
  while (elapsed === null && Date.now() - sentAt < MARKER_WAIT_MS) {
    const read = await canvas.logs(after, LOG_PAGE);
    last = read.body ?? null;
    const got = last?.lines ?? [];
    lines += got.length;
    dropped += Number(last?.dropped) || 0;
    after = last?.next ?? after;
    if (got.some((line) => JSON.stringify(line).includes(timedMarker))) elapsed = Date.now() - sentAt;
    else await sleep(nextReadDelay(got.length, LOG_PAGE, 200));
  }
  step(section, "marker arrives within 5 s", withinLimit(elapsed !== null, elapsed, MARKER_LIMIT_MS),
    { elapsed_ms: elapsed, limit_ms: MARKER_LIMIT_MS, lines_read: lines - before.lines, dropped: dropped - before.dropped,
      running: last?.running ?? null, feed_error: last?.error ? errorText(last.error, 200) : null });
  const stopped = await canvas.feed(false);
  step(section, "feed stopped", stopped.status === 200 && stopped.body?.running === false);
  const status = await canvas.status();
  step(section, "/status tools.logs.running false", status.body?.tools?.logs?.running === false);
}

// --- journal --------------------------------------------------------------------------------

async function checkJournal() {
  const files = [];
  const walk = async (directory) => {
    for (const entry of await readdir(directory, { withFileTypes: true }).catch(() => [])) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.name === "journal.ndjson") files.push(path);
    }
  };
  await walk(autonomHome);
  let canvasActions = 0;
  let bodyLeak = false;
  let markerLeak = false;
  for (const file of files) {
    const text = await readFile(file, "utf8").catch(() => "");
    bodyLeak ||= text.includes(bodyMarker);
    markerLeak ||= text.includes(logMarker);
    for (const line of text.split("\n")) {
      let record = null;
      try { record = line.trim() ? JSON.parse(line) : null; } catch { record = null; }
      if (record?.kind === "action" && typeof record.verb === "string" && record.verb.startsWith("canvas ")) canvasActions += 1;
    }
  }
  report.journal = { files: files.length, canvas_actions: canvasActions, mock_body_in_journal: bodyLeak,
    log_marker_in_journal: markerLeak, ok: canvasActions > 0 && !bodyLeak && !markerLeak };
}

// --- UI -------------------------------------------------------------------------------------

async function loadPlaywright() {
  const entry = join(playwrightRoot, "node_modules/playwright/index.mjs");
  if (!existsSync(entry)) throw new Error(`Playwright not found at ${entry}; set PLAYWRIGHT_ROOT`);
  return await import(pathToFileURL(entry).href);
}

async function uiMatrix(canvas) {
  const ui = { playwright_root: playwrightRoot, runs: [], ok: false };
  report.ui = ui;
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  try {
    for (const viewport of VIEWPORTS) {
      for (const scheme of SCHEMES) {
        const entry = { viewport: `${viewport.width}x${viewport.height}`, scheme, tabs: [], ok: false };
        ui.runs.push(entry);
        const context = await browser.newContext({ viewport, colorScheme: scheme });
        const page = await context.newPage();
        try {
          await page.goto(canvas.url);
          await page.waitForFunction(() => Boolean(window.autonomTools), null, { timeout: 20_000 });
          await page.waitForTimeout(1500);
          await page.click("#tools-toggle");
          await page.waitForFunction(() => {
            const source = document.getElementById("tools-app-source");
            return source && !/^Loading/.test(source.textContent);
          }, null, { timeout: 30_000 });
          entry.context_loaded = await page.evaluate(() => !/not available/.test(document.getElementById("tools-app-source").textContent));
          // Only test rows reach the screenshots: the request list and the log show the markers.
          await page.evaluate(({ host, marker }) => {
            for (const [id, value, type] of [["tools-req-host", host, "change"], ["tools-log-filter", marker, "input"]]) {
              const field = document.getElementById(id);
              field.value = value;
              field.dispatchEvent(new Event(type, { bubbles: true }));
            }
          }, { host: MOCK_HOST, marker: logMarker });
          for (const tab of TABS) {
            await page.click(`#tools-tab-${tab}`);
            await page.waitForTimeout(700);
            const check = await page.evaluate((name) => {
              const rect = (id) => document.getElementById(id)?.getBoundingClientRect() ?? null;
              const panelEl = document.getElementById(`tools-panel-${name}`);
              const drawer = rect("tools");
              const device = rect("device");
              const desktop = window.innerWidth > 760;
              return {
                overflow: document.documentElement.scrollWidth > window.innerWidth,
                panel_visible: Boolean(panelEl && !panelEl.hidden && panelEl.getBoundingClientRect().height > 0),
                selected: document.getElementById(`tools-tab-${name}`).getAttribute("aria-selected") === "true",
                inspector_hidden: getComputedStyle(document.getElementById("inspector")).display === "none",
                device_covered: desktop && drawer && device ? device.right > drawer.left + 1 : false,
                sheet_full_width: !desktop && drawer ? Math.abs(drawer.width - window.innerWidth) <= 1 : null,
              };
            }, tab);
            const file = `tools-${platform}-${entry.viewport}-${scheme}-${tab}.png`;
            await page.screenshot({ path: join(outDir, file) });
            check.ok = !check.overflow && check.panel_visible && check.selected && check.inspector_hidden &&
              !check.device_covered && check.sheet_full_width !== false;
            entry.tabs.push({ tab, screenshot: file, ...check });
          }
          // Keyboard: the arrow key moves from Logs back to App.
          await page.focus("#tools-tab-logs");
          await page.keyboard.press("ArrowRight");
          entry.keyboard = await page.evaluate(() => document.getElementById("tools-tab-app").getAttribute("aria-selected") === "true");
          await page.click("#tools-toggle");
          entry.closed = await page.evaluate(() => document.getElementById("tools").hidden);
          entry.ok = entry.context_loaded && entry.keyboard && entry.closed && entry.tabs.every((tab) => tab.ok);
        } catch (error) {
          entry.error = errorText(error, 300);
        } finally {
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
  ui.ok = ui.runs.every((entry) => entry.ok);
}

// --- restore --------------------------------------------------------------------------------

async function restoreAll(canvas) {
  if (canvas) {
    if (state.mockIds.size) await restoreStep("mocks removed", async () => (await canvas.call("mocks.clear")).status === 200);
    if (state.attached) await restoreStep("proxy detached", async () => (await canvas.call("network.detach")).status === 200);
    if (state.proxyStarted) await restoreStep("proxy stopped", async () => (await canvas.call("network.stop")).status === 200);
    await canvas.feed(false).catch(() => null);
    if (platform === "android") {
      const permissionAction = permissionRestoreAction(state);
      if (permissionAction) {
        await restoreStep("permission as found", async () => {
          await canvas.call("permissions.set", { app_id: appId, service: state.permission, action: permissionAction });
          let now = await androidGranted(appId, state.permission);
          let readFrom = "dumpsys";
          if (now === null) {
            // dumpsys could not be parsed: the tools process's own read-back decides.
            const listed = await canvas.call("permissions.list", { app_id: appId });
            now = listed.body?.result?.permissions?.find((item) => item.name === state.permission)?.granted ?? null;
            readFrom = "tools";
          }
          return { ok: now === state.permissionGranted, permission: state.permission, granted: now, read_from: readFrom };
        });
      }
      await restoreStep("location set to the emulator default", async () => {
        const answer = await canvas.call("location.set", EMULATOR_DEFAULT_FIX);
        return answer.status === 200;
      });
      if (state.nightMode) {
        await restoreStep("appearance as found", async () => {
          await canvas.call("simulate", { control: "appearance", action: state.nightMode === "yes" ? "dark" : "light" });
          return (await androidNightMode()) === state.nightMode;
        });
      }
      if (state.batteryChanged) await restoreStep("battery reset", async () => (await canvas.call("simulate", { control: "battery", action: "reset" })).status === 200);
      if (state.networkToggled) await restoreStep("network online", async () => (await canvas.call("simulate", { control: "network", action: "online" })).status === 200);
    } else {
      if (state.privacyService) {
        await restoreStep("privacy service reset", async () => {
          const answer = await canvas.call("permissions.set", { app_id: appId, service: state.privacyService, action: "reset" });
          return { ok: answer.status === 200, service: state.privacyService, ...brief(answer) };
        });
      }
      if (state.locationSet) await restoreStep("location cleared", async () => (await canvas.call("location.clear")).status === 200);
      if (state.nightMode) {
        await restoreStep("appearance as found", async () => {
          await canvas.call("simulate", { control: "appearance", action: state.nightMode });
          return (await iosAppearance()) === state.nightMode;
        });
      }
      if (state.batteryChanged && state.iosStatusBarClean) {
        await restoreStep("battery (status bar) reset", async () => (await canvas.call("simulate", { control: "battery", action: "reset" })).status === 200);
      }
    }
  }
  if (platform === "android" && state.locationAppStarted) {
    await restoreStep("location app stopped", async () => {
      await shell(`am force-stop ${args["location-app"]}`);
      return !(await androidPackageRunning(args["location-app"]));
    });
  }
}

// --- main -----------------------------------------------------------------------------------

let canvas = null;
let sessionStarted = false;
let simulatorBooted = false;
try {
  if (platform === "android") {
    const device = await adb(["get-state"]);
    if (device.stdout.trim() !== "device") throw new Error(`${ANDROID_SERIAL} is not online (${device.stdout.trim() || device.stderr.trim()})`);
  } else {
    const listed = await simctl(["list", "devices", "--json"]);
    const found = Object.values(JSON.parse(listed.stdout || "{}").devices ?? {}).flat().find((item) => item.udid === IOS_UDID);
    if (!found) throw new Error(`${IOS_NAME} is not listed by simctl`);
    if (found.name !== IOS_NAME) throw new Error(`${IOS_UDID} is named ${found.name}; refusing it`);
    report.simulator = { initial_state: found.state, booted_here: false };
    if (found.state === "Shutdown") {
      const booted = await simctl(["boot", IOS_UDID]);
      if (booted.code !== 0) throw new Error("simctl boot failed");
      simulatorBooted = true;
      report.simulator.booted_here = true;
      await simctl(["bootstatus", IOS_UDID, "-b"], { timeout: 300_000 });
    } else if (found.state !== "Booted") {
      throw new Error(`${IOS_NAME} is ${found.state}; it must be Booted or Shutdown`);
    }
  }
  const sessionArgs = platform === "android" ? ["session", "start", "--app-id", appId] : ["session", "start", "--log-stream"];
  const started = await autonom(sessionArgs);
  if (started.code !== 0) throw new Error(`autonom session start failed: ${started.stdout.slice(0, 300)}`);
  sessionStarted = true;
  canvas = await startCanvas();
  report.tools_available = (await canvas.status()).body?.tools?.available ?? null;

  await panel("app", (section) => (platform === "android" ? androidApp : iosApp)(canvas, section));
  await panel("simulate", (section) => (platform === "android" ? androidSimulate : iosSimulate)(canvas, section));
  await panel("network", (section) => networkPanel(canvas, section));
  await panel("mocks", (section) => mocksPanel(canvas, section));
  // The network panel closes after the mocks used its proxy (its ok covers both parts).
  await panel("network", (section) => networkStop(canvas, section));
  await panel("logs", (section) => logsPanel(canvas, section));
} catch (error) {
  report.error = errorText(error, 500);
} finally {
  await restoreAll(canvas);
  if (state.captureServer) await state.captureServer.close().catch(() => null);
  if (canvas && args.ui) {
    try {
      await uiMatrix(canvas);
    } catch (error) {
      report.ui = { ...(report.ui ?? {}), ok: false, error: errorText(error, 300) };
    }
  }
  if (canvas) await restoreStep("Canvas stopped", async () => { await canvas.stop(); return !canvas.running(); });
  await checkJournal().catch((error) => { report.journal = { ok: false, error: errorText(error, 300) }; });
  if (sessionStarted) await restoreStep("session stopped", async () => (await autonom(["session", "stop"])).code === 0);
  if (simulatorBooted) {
    await restoreStep("Simulator shut down again", async () => (await simctl(["shutdown", IOS_UDID], { timeout: 120_000 })).code === 0);
  }
  report.restore.ok = report.restore.steps.every((entry) => entry.ok);
  report.ok = !report.error && PANELS.every((name) => report.panels[name].ok) && report.restore.ok &&
    report.journal?.ok === true && (!args.ui || report.ui?.ok === true);
  report.finished_at = new Date().toISOString();
  const file = join(outDir, `tools-${platform}.json`);
  // Last line of defence: nothing that looks like the token reaches the file.
  await writeFile(file, `${redactSecrets(JSON.stringify(report, null, 2))}\n`);
  if (!args["keep-home"]) await rm(autonomHome, { recursive: true, force: true });
  console.log(`${report.ok ? "ok" : "FAILED"}: ${file}`);
  process.exitCode = report.ok ? 0 : 1;
}
