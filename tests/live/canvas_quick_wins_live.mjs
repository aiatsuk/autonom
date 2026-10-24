#!/usr/bin/env node
/**
 * Live check of the Canvas quick wins: keyboard shortcuts, the Display menu and the Setup
 * checklist (VER-003 on Android, VER-004 on iOS).
 *
 * Not part of `node --test tests/*.test.mjs`. Each run makes its own device, uses only that
 * device and deletes it again in a finally block:
 *
 *   node tests/live/canvas_quick_wins_live.mjs --platform android --create-avd --evidence-dir DIR
 *   node tests/live/canvas_quick_wins_live.mjs --platform ios --create-simulator --evidence-dir DIR
 *
 * Android creates the AVD Autonom_QW_Live_API36 (android-36 google_apis_playstore arm64-v8a),
 * boots it headless on a free even port it picks (never 5554, 5580, 5584 or 5586, never a port
 * another emulator or process holds), and at the end kills that emulator by its own port and
 * deletes the AVD. An AVD of that name that already exists is refused, never reused or deleted.
 * iOS creates the Simulator "Autonom QW live <YYYYMMDD-HHMM>" (iPhone 17 Pro, iOS 27.0), boots
 * it, and at the end shuts it down and deletes it. No other emulator or Simulator is touched:
 * every adb call names the run's own serial, every simctl call its own UDID, and the report
 * compares the other devices before and after.
 *
 * The Canvas is this checkout's (android-emulator-browser.mjs) on --port 0 with a random token,
 * a temporary AUTONOM_HOME and a temporary captures folder. Chromium comes from Playwright,
 * resolved as tests/live/canvas_ios_live.mjs does: --playwright, AUTONOM_PLAYWRIGHT,
 * ~/pr/platform/node_modules/playwright, then a `playwright` package Node can resolve;
 * /opt/pw-browsers is the browser folder when it exists and PLAYWRIGHT_BROWSERS_PATH is not
 * set. It runs headed with its window off screen unless --headless is given. Chords are pressed
 * with Cmd on macOS and Ctrl elsewhere, as the page expects.
 *
 * Android (canvas-quick-wins-android.json), in order:
 *   checklist_missing  the Canvas streams with a copy of the real scrcpy-server
 *                      (--scrcpy-server), while AUTONOM_SCRCPY_SERVER, the path its Setup
 *                      checklist checks, names a file that does not exist yet: the Activity
 *                      tab shows scrcpy-server Missing with a fix command and Copy, and the
 *                      /api/health entry has a non-empty `fix`.
 *   checklist_ticks    the script then creates that file (a copy of the real server) and,
 *                      without a click, the item shows OK within 10 s; no health refresh
 *                      request starts after that, and refreshes were at least 4.5 s apart.
 *   home               Settings in front, the screen focused, Cmd+Shift+H: the launcher is
 *                      in front (adb dumpsys activity), exactly one {t:"system",op:"home"} and
 *                      no key message was sent.
 *   capture            Cmd+S, then Cmd+R, 3 s, Cmd+R: one PNG and one MP4 in the captures
 *                      folder, the drawer's capture.screenshot, record.start and record.stop
 *                      calls, and the page did not reload.
 *   unmapped           Cmd+A in the Settings search field goes to the device as KEYCODE_A
 *                      with META (CTRL off macOS) and fires no shortcut. Whether the device
 *                      selected the field's text is reported (`unmapped.device`), not checked.
 *   display            Frame rate 15, then Size Half: /status `stream_settings` fps 15 and a
 *                      max size of about half, the video's long side about half, frames back
 *                      within 10 s of each pick, one stream restart per change.
 *   display_reload     after a reload: still 15 fps and Half, and the menu shows them.
 *   cleanup            Canvas stopped, the emulator gone from adb and its process ended, the
 *                      AVD deleted, temporary folders removed, other devices as before.
 *
 * iOS (canvas-quick-wins-ios.json):
 *   home               Settings in front (waited for up to 15 s, else `front_before_timeout`),
 *                      Cmd+Shift+H: within 10 s `idb ui describe-all` shows another labelled
 *                      front application, or no labelled one plus a home-screen sign (a
 *                      non-Application "Settings" element; `home_evidence` names the rule);
 *                      one home message sent and nothing else.
 *   rotate_message     Cmd+Right: the page says "Rotate is not available on iOS yet" and sends
 *                      no control message.
 *   field_focus        focus in the Tools drawer's deeplink field, Cmd+L: no message sent and
 *                      the field's value unchanged.
 *   capture            as on Android.
 *   display            Frame rate 15, then On change: /status fps 15, then 0; frames go on and
 *                      the page opens no new video socket; the companion is not restarted.
 *   cleanup            Canvas and the companions it or the idb CLI started stopped, the
 *                      Simulator shut down and deleted, temporary folders removed, other
 *                      Simulators as before.
 *
 * Each report has `ok`, `setup`, `checks` (every one must be true), the details of each step,
 * the host load, the browser name and version, and timings, also when a step fails midway (a
 * failed step does not stop the later ones, and cleanup always runs). The token never enters
 * the report. Exit 0 when every check passes, 1 otherwise, 2 for a usage error; --help prints
 * this usage and touches nothing.
 */
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, readdir, rm, stat, writeFile } from "node:fs/promises";
import { createConnection } from "node:net";
import { homedir, loadavg, tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs as parseCliArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "../..");
const SCRIPTS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts");
const CANVAS = join(SCRIPTS, "android-emulator-browser.mjs");

const AVD_NAME = "Autonom_QW_Live_API36";
const SYSTEM_IMAGE = "system-images;android-36;google_apis_playstore;arm64-v8a";
const AVD_DEVICE = "pixel_9";
// Emulator console ports the run may take (even, its adb port is the next one); ports other
// live scripts use are never taken.
const PORT_FIRST = 5560;
const PORT_LAST = 5680;
const RESERVED_PORTS = new Set([5554, 5580, 5584, 5586]);
const SIM_NAME_PREFIX = "Autonom QW live ";
const SIM_DEVICE_TYPE = "iPhone 17 Pro";
const SIM_RUNTIME = "iOS 27.0";
const SETTINGS_IOS = "com.apple.Preferences";
const SETTINGS_ANDROID = "com.android.settings";
const SETTINGS_SEARCH_ANDROID = "com.google.android.settings.intelligence";
const LOAD_BAR = 60;
const TICK_BAR_MS = 10_000;
const FRAMES_BAR_MS = 10_000;
// How long cleanup waits for an interrupted avdmanager or simctl create to end by itself.
const CREATE_SETTLE_MS = 90_000;
const ROTATE_MESSAGE = "Rotate is not available on iOS yet";
const OFF_SCREEN_WINDOW = "--window-position=3000,3000";
// Android meta state bits (scrcpy-lib.mjs META_STATE).
const META_ON = 0x10000;
const CTRL_ON = 0x1000;
const KEYCODE_A = 29;
// What the unmapped-chord step types into the Settings search field; never a secret.
const FIELD_TEXT = "abc";
const DEEPLINK_TEXT = "autonom-qw://check";

const USAGE = `usage: node tests/live/canvas_quick_wins_live.mjs --platform android --create-avd [options]
       node tests/live/canvas_quick_wins_live.mjs --platform ios --create-simulator [options]

Live check of the Canvas keyboard shortcuts, Display menu and Setup checklist on a device
the run creates for itself and deletes at the end (VER-003, VER-004).

  --platform android|ios   which case to run (required)
  --create-avd             Android: create, boot, use and delete AVD ${AVD_NAME}
                           (${SYSTEM_IMAGE});
                           required with --platform android
  --create-simulator       iOS: create, boot, use and delete the Simulator
                           "${SIM_NAME_PREFIX}<YYYYMMDD-HHMM>" (${SIM_DEVICE_TYPE}, ${SIM_RUNTIME});
                           required with --platform ios
  --evidence-dir DIR       where canvas-quick-wins-<platform>.json is written
                           (default: a new temporary folder)
  --playwright DIR         a node_modules/playwright folder (default: AUTONOM_PLAYWRIGHT,
                           ~/pr/platform/node_modules/playwright, then the playwright package)
  --headless               run Chromium headless (default: headed, window off screen)
  --python PATH            Python for the Canvas bridges (default: python3)
  --adb PATH               Android: adb (default: the SDK's platform-tools, then PATH)
  --emulator PATH          Android: emulator (default: the SDK's emulator, then PATH)
  --avdmanager PATH        Android: avdmanager (default: the SDK's cmdline-tools/latest, then PATH)
  --scrcpy-server PATH     Android: the scrcpy-server 4.1 to copy (default: AUTONOM_SCRCPY_SERVER,
                           SCRCPY_SERVER_PATH, then the server of scrcpy on PATH)
  --boot-timeout SECONDS   Android: how long the new emulator may take to boot (default: 600)
  --xcrun PATH             iOS: xcrun (default: xcrun)
  --idb PATH               iOS: the idb CLI used for describe-all (default: idb)
  --idb-companion PATH     iOS: idb_companion for the Canvas (default: its own discovery)
  --keep-home              keep the temporary AUTONOM_HOME (it may hold the run's journal)
  --help                   print this and exit; touches no device

Exit 0 when every check passes, 1 when one fails, 2 for a usage error.`;

const OPTIONS = {
  help: { type: "boolean", default: false },
  platform: { type: "string" },
  "create-avd": { type: "boolean", default: false },
  "create-simulator": { type: "boolean", default: false },
  "evidence-dir": { type: "string" },
  playwright: { type: "string" },
  headless: { type: "boolean", default: false },
  python: { type: "string", default: "python3" },
  adb: { type: "string" },
  emulator: { type: "string" },
  avdmanager: { type: "string" },
  "scrcpy-server": { type: "string" },
  "boot-timeout": { type: "string", default: "600" },
  xcrun: { type: "string", default: "xcrun" },
  idb: { type: "string", default: "idb" },
  "idb-companion": { type: "string" },
  "keep-home": { type: "boolean", default: false },
};

function usage(message) {
  console.error(`canvas_quick_wins_live: ${message}`);
  console.error(USAGE);
  process.exit(2);
}

let args;
try {
  ({ values: args } = parseCliArgs({ options: OPTIONS, allowPositionals: false }));
} catch (error) {
  usage(error.message);
}
if (args.help) {
  console.log(USAGE);
  process.exit(0);
}
if (args.platform !== "android" && args.platform !== "ios") usage("--platform must be android or ios");
if (args.platform === "android" && !args["create-avd"]) {
  usage("--platform android needs --create-avd: the run never uses an existing emulator");
}
if (args.platform === "ios" && !args["create-simulator"]) {
  usage("--platform ios needs --create-simulator: the run never uses an existing Simulator");
}
if (args.platform === "android" && args["create-simulator"]) usage("--create-simulator is for --platform ios");
if (args.platform === "ios" && args["create-avd"]) usage("--create-avd is for --platform android");
const bootTimeoutS = Number(args["boot-timeout"]);
if (!Number.isInteger(bootTimeoutS) || bootTimeoutS < 60 || bootTimeoutS > 1800) {
  usage("--boot-timeout must be a whole number of seconds from 60 to 1800");
}
if (args.platform === "ios" && process.platform !== "darwin") usage("--platform ios needs macOS");

const platform = args.platform;
// The chord modifier: Cmd on macOS, Ctrl elsewhere, as the page reads navigator.platform.
const MOD = process.platform === "darwin" ? "Meta" : "Control";
const MOD_META_BIT = process.platform === "darwin" ? META_ON : CTRL_ON;

// --- small helpers ------------------------------------------------------------------------

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

const secrets = new Set();

/** Text for the report with every secret of the run taken out, cut to `max` characters. */
function clean(value, max = 600) {
  let text = String(value ?? "");
  for (const secret of secrets) if (secret) text = text.split(secret).join("<token>");
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function run(file, argv, { timeout = 60_000, env = process.env, input, onSpawn } = {}) {
  return new Promise((resolvePromise) => {
    const child = execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (typeof error.code === "number" ? error.code : 1) : 0,
          stdout: String(stdout ?? ""), stderr: String(stderr ?? ""), error: error && typeof error.code !== "number" ? error.message : null });
      });
    if (input !== undefined) {
      child.stdin?.on("error", () => {});
      child.stdin?.end(input);
    }
    onSpawn?.(child);
  });
}

/**
 * A device-creating command (avdmanager create, simctl create) that cleanup can wait for.
 * Its promise, child and result stay in the record, so a SIGINT/SIGTERM that arrives while
 * it runs never leaves a device behind unseen.
 */
function startCreate(file, argv, options) {
  const record = { child: null, result: null, killed: false };
  record.promise = run(file, argv, { ...options, onSpawn: (child) => { record.child = child; } })
    .then((result) => { record.result = result; return result; });
  return record;
}

/** Waits for a create that cleanup found still running; stops it only when it takes too long. */
async function settleCreate(record, waitMs = CREATE_SETTLE_MS) {
  if (!record || record.result) return record?.result ?? null;
  const done = await Promise.race([record.promise.then(() => true), sleep(waitMs).then(() => false)]);
  if (!done && record.child && record.child.exitCode === null && record.child.signalCode === null) {
    record.killed = true;
    record.child.kill("SIGTERM");
    await Promise.race([record.promise, sleep(10_000)]);
  }
  return record.result;
}

async function waitFor(predicate, timeoutMs, intervalMs = 250) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value || Date.now() >= deadline) return value;
    await sleep(intervalMs);
  }
}

function hostLoad() {
  return loadavg().map((value) => Math.round(value * 100) / 100);
}

/** A random token that never starts with "-", so no argument parser reads it as a flag. */
function liveToken() {
  for (;;) {
    const token = randomBytes(18).toString("base64url");
    if (!token.startsWith("-")) return token;
  }
}

function whichSync(name) {
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    const candidate = join(directory, name);
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

/** The first of `candidates` that exists, else `fallback` (a name looked up on PATH). */
function tool(explicit, candidates, fallback) {
  if (explicit) return resolve(explicit);
  for (const candidate of candidates) if (candidate && existsSync(candidate)) return candidate;
  return whichSync(fallback) ?? fallback;
}

/** Files below `directory` (recursively), as relative paths. */
async function filesBelow(directory) {
  const found = [];
  const walk = async (folder, prefix) => {
    let entries = [];
    try {
      entries = await readdir(folder, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) await walk(join(folder, entry.name), relative);
      else found.push(relative);
    }
  };
  await walk(directory, "");
  return found;
}

/** Whether a TCP port on 127.0.0.1 accepts a connection. */
function portInUse(port) {
  return new Promise((resolvePromise) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    const done = (value) => {
      socket.destroy();
      resolvePromise(value);
    };
    socket.setTimeout(500, () => done(false));
    socket.once("connect", () => done(true));
    socket.once("error", () => done(false));
  });
}

// --- the run's state ------------------------------------------------------------------------

const startedAt = Date.now();
const outDir = resolve(args["evidence-dir"] ?? await mkdtemp(join(tmpdir(), "autonom-qw-live-evidence-")));
await mkdir(outDir, { recursive: true });
const scratch = await mkdtemp(join(tmpdir(), "autonom-qw-live-"));
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-qw-live-home-"));
const capturesDir = join(scratch, "captures");
await mkdir(capturesDir, { recursive: true });

const report = {
  case: platform === "android" ? "VER-003" : "VER-004",
  platform,
  ok: false,
  setup: {},
  host: { loadavg_start: hostLoad(), load_bar: LOAD_BAR },
  browser: null,
  steps: {},
  checks: {},
  timings_ms: {},
};
report.host.load_above_bar = report.host.loadavg_start[0] >= LOAD_BAR;

const state = {
  canvas: null,
  browser: null,
  page: null,
  network: [],
  // Android
  adb: null,
  serial: null,
  port: null,
  emulatorChild: null,
  avdCreated: false,
  // iOS
  udid: null,
  simulatorCreated: false,
  cliCompanion: null,
  otherDevicesBefore: null,
  // Set by SIGINT/SIGTERM: from then on the run makes no new device, process or page.
  aborting: false,
  avdCreate: null,
  simulatorCreate: null,
};

/** Called right before anything is created, in the same tick as the creation itself. */
function assertNotAborting(what) {
  if (state.aborting) throw new Error(`interrupted; ${what} was not started`);
}

/** One step: its details go to report.steps[name]; a throw marks its check false and the run goes on. */
async function step(name, work) {
  const started = Date.now();
  const detail = (report.steps[name] = {});
  try {
    const ok = await work(detail);
    report.checks[name] = ok === true;
  } catch (error) {
    detail.error = clean(error?.stack ?? error?.message ?? error);
    report.checks[name] = false;
  }
  report.timings_ms[name] = Date.now() - started;
}

// --- Playwright and the page -----------------------------------------------------------------

async function loadPlaywright() {
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && existsSync("/opt/pw-browsers")) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = "/opt/pw-browsers";
  }
  const candidates = [args.playwright, process.env.AUTONOM_PLAYWRIGHT,
    join(homedir(), "pr/platform/node_modules/playwright")].filter(Boolean);
  for (const directory of candidates) {
    const entry = join(directory, "index.mjs");
    if (existsSync(entry)) return await import(pathToFileURL(entry).href);
  }
  try {
    return await import("playwright");
  } catch {
    throw new Error(`Playwright was not found (tried ${candidates.join(", ") || "nothing"} and the playwright package); ` +
      "pass --playwright <node_modules/playwright>");
  }
}

/** The Canvas page: counts its WebSockets per path and records health and tools requests. */
async function openPage() {
  if (!state.browser) {
    const { chromium } = await loadPlaywright();
    const launchArgs = args.headless ? [] : [OFF_SCREEN_WINDOW, "--disable-backgrounding-occluded-windows",
      "--disable-renderer-backgrounding", "--disable-background-timer-throttling"];
    state.browser = await chromium.launch({ headless: args.headless, args: launchArgs });
    report.browser = { name: "chromium", version: state.browser.version(), headless: args.headless };
  }
  const context = await state.browser.newContext({ viewport: { width: 1280, height: 1000 } });
  await context.addInitScript(() => {
    const Native = window.WebSocket;
    window.__qwSockets = [];
    window.WebSocket = class extends Native {
      constructor(url, protocols) {
        super(url, protocols);
        try {
          window.__qwSockets.push({ path: new URL(url, location.href).pathname, at: performance.now() });
        } catch {
          window.__qwSockets.push({ path: "?", at: performance.now() });
        }
      }
    };
  });
  const page = await context.newPage();
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/api/health")) {
      state.network.push({ at: Date.now(), kind: "health", refresh: /[?&]refresh=1\b/.test(url) });
    } else if (url.includes("/tools/call") && request.method() === "POST") {
      let op = null;
      try {
        op = request.postDataJSON()?.op ?? null;
      } catch {
        op = null;
      }
      state.network.push({ at: Date.now(), kind: "tools", op: typeof op === "string" ? op : null });
    }
  });
  state.page = page;
  await page.goto(state.canvas.url);
  await waitStreaming(page);
  await installRecorder(page);
  return page;
}

async function waitStreaming(page) {
  const transport = platform === "android" ? "scrcpy" : "idb";
  await page.waitForFunction((wanted) => {
    const value = window.autonomCanvas?.stats();
    return value && value.transport === wanted && value.session === "streaming" && value.control === 1 &&
      value.framesDecoded > 0;
  }, transport, { timeout: 60_000, polling: 100 });
}

/**
 * Records every control message the page sends (window.sendControl, a global function the
 * page calls by name), keeping only t, op, a, code and meta: never text.
 */
async function installRecorder(page) {
  await page.evaluate(() => {
    window.__qwSent = [];
    window.__qwMarker = Math.random().toString(36).slice(2);
    if (window.__qwWrapped) return;
    const original = window.sendControl;
    window.sendControl = function recordControl(message) {
      const kept = {};
      for (const key of ["t", "op", "a", "code", "meta", "fps", "size"]) if (message && key in message) kept[key] = message[key];
      window.__qwSent.push(kept);
      return original.apply(this, arguments);
    };
    window.__qwWrapped = true;
  });
}

async function resetSent(page) {
  await page.evaluate(() => { window.__qwSent = []; });
}

/** Control messages sent since the last reset, pings left out. */
async function sentMessages(page) {
  return (await page.evaluate(() => window.__qwSent ?? [])).filter((message) => message.t !== "ping");
}

async function marker(page) {
  return await page.evaluate(() => window.__qwMarker ?? null);
}

/** Focus the device screen (the video canvas, else the screenshot image). */
async function focusScreen(page) {
  return await page.evaluate(() => {
    const video = document.getElementById("video");
    const screen = document.getElementById("screen");
    const target = video && !video.hidden ? video : screen;
    target?.focus();
    return document.activeElement === target;
  });
}

async function stats(page) {
  return await page.evaluate(() => window.autonomCanvas.stats());
}

async function chord(page, keys) {
  await page.keyboard.press(keys.map((key) => (key === "MOD" ? MOD : key)).join("+"));
}

// --- the Canvas ------------------------------------------------------------------------------

async function startCanvas(argv, env) {
  assertNotAborting("the Canvas");
  const token = liveToken();
  secrets.add(token);
  const child = spawn(process.execPath, [CANVAS, ...argv, "--port", "0", "--token", token, "--python", args.python,
    "--captures-dir", capturesDir], { cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output = (output + chunk.toString()).slice(-20_000); });
  child.stderr.on("data", (chunk) => { output = (output + chunk.toString()).slice(-20_000); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  const stop = async () => {
    if (!running()) return await exited;
    child.kill("SIGTERM");
    const code = await Promise.race([exited, sleep(10_000).then(() => "timeout")]);
    if (code === "timeout" && running()) child.kill("SIGKILL");
    return await exited;
  };
  const canvas = { child, token, running, stop, exited, output: () => clean(output, 2000) };
  state.canvas = canvas;
  canvas.url = await new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`the Canvas did not print its URL within 60 s: ${canvas.output()}`)), 60_000);
    child.stdout.on("data", () => {
      const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise(match[1]);
    });
    exited.then((code) => {
      clearTimeout(timer);
      reject(new Error(`the Canvas exited ${code} before it started: ${canvas.output()}`));
    });
  });
  canvas.origin = new URL(canvas.url).origin;
  canvas.request = async (method, path, body) => {
    const headers = { Authorization: `Bearer ${token}`, "X-Autonom-Origin": "agent" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${canvas.origin}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(60_000),
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    return { status: response.status, body: payload ?? {} };
  };
  canvas.status = async () => (await canvas.request("GET", "/status")).body;
  return canvas;
}

/** The highest command log seq so far, to count the stream restarts that follow. */
async function activityMark() {
  const answer = await state.canvas.request("GET", "/api/activity?after=0&limit=500");
  return Math.max(0, ...(answer.body.entries ?? []).map((entry) => entry.seq ?? 0));
}

async function streamRestartsSince(mark) {
  const answer = await state.canvas.request("GET", `/api/activity?after=${mark}&limit=500`);
  return (answer.body.entries ?? []).filter((entry) => entry.seq > mark && entry.kind === "stream" && entry.name === "profile")
    .map((entry) => ({ seq: entry.seq, ok: entry.ok, summary: clean(entry.summary, 120) }));
}

// --- shared steps ----------------------------------------------------------------------------

/** Cmd+S, then Cmd+R, 3 s, Cmd+R: one new PNG and one new MP4, the drawer's three calls, no reload. */
async function captureStep(detail) {
  const page = state.page;
  const before = new Set(await filesBelow(capturesDir));
  const mark = await marker(page);
  const from = Date.now();
  detail.focused = await focusScreen(page);
  await chord(page, ["MOD", "KeyS"]);
  const png = await waitFor(async () => (await filesBelow(capturesDir)).filter((file) => !before.has(file) && file.endsWith(".png")), 30_000, 500);
  detail.png_after_ms = Date.now() - from;
  await focusScreen(page);
  await chord(page, ["MOD", "KeyR"]);
  detail.recording_started = Boolean(await waitFor(async () =>
    (await page.getAttribute("#actions-record", "aria-pressed").catch(() => null)) === "true", 20_000));
  await sleep(3000);
  await focusScreen(page);
  await chord(page, ["MOD", "KeyR"]);
  detail.recording_stopped = Boolean(await waitFor(async () =>
    (await page.getAttribute("#actions-record", "aria-pressed").catch(() => null)) === "false", 30_000));
  await waitFor(async () => (await filesBelow(capturesDir)).some((file) => !before.has(file) && file.endsWith(".mp4")), 60_000, 500);
  const added = (await filesBelow(capturesDir)).filter((file) => !before.has(file));
  detail.png = added.filter((file) => file.endsWith(".png")).length;
  detail.mp4 = added.filter((file) => file.endsWith(".mp4")).length;
  detail.png_seen_in_time = Array.isArray(png) && png.length > 0;
  detail.ops = state.network.filter((entry) => entry.kind === "tools" && entry.at >= from &&
    ["capture.screenshot", "record.start", "record.stop"].includes(entry.op)).map((entry) => entry.op);
  detail.no_reload = (await marker(page)) === mark && mark !== null;
  return detail.focused && detail.png === 1 && detail.mp4 === 1 && detail.no_reload &&
    detail.ops.join() === "capture.screenshot,record.start,record.stop";
}

async function openSettingsPanel(page) {
  if ((await page.getAttribute("#canvas-settings-toggle", "aria-expanded")) !== "true") {
    await page.click("#canvas-settings-toggle");
  }
  await page.waitForFunction(() => {
    const select = document.getElementById("display-fps");
    return select && !select.disabled;
  }, null, { timeout: 20_000 });
}

/** Picks a Display menu value the way the menu does: the select's value and a change event. */
async function pickDisplay(page, id, value) {
  await page.evaluate(({ id, value }) => {
    const select = document.getElementById(id);
    select.value = String(value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }, { id, value });
}

async function menuValues(page) {
  return await page.evaluate(() => ({
    fps: document.getElementById("display-fps")?.value ?? null,
    size: document.getElementById("display-size")?.value ?? null,
  }));
}

/**
 * Watches the page for a stream restart: the session leaving "streaming" and the first frame
 * after it came back. Returns a reader of what it saw.
 */
async function watchRestart(page) {
  await page.evaluate(() => {
    if (window.__qwWatch?.timer) clearInterval(window.__qwWatch.timer);
    const watch = { pickedAt: performance.now(), leftAt: null, backAt: null };
    watch.timer = setInterval(() => {
      const value = window.autonomCanvas.stats();
      if (value.session !== "streaming" && watch.leftAt === null) watch.leftAt = performance.now();
      if (watch.leftAt !== null && value.session === "streaming" && value.lastFrameAt > watch.leftAt && watch.backAt === null) {
        watch.backAt = value.lastFrameAt;
        clearInterval(watch.timer);
        watch.timer = null;
      }
    }, 20);
    window.__qwWatch = watch;
  });
  return async () => await page.evaluate(() => {
    const watch = window.__qwWatch;
    return { left: watch.leftAt !== null, back_ms: watch.backAt === null ? null : Math.round(watch.backAt - watch.pickedAt) };
  });
}

/** Waits until a frame newer than now reaches the page; ms it took, or null. */
async function nextFrame(page, timeoutMs) {
  const mark = await page.evaluate(() => performance.now());
  const from = Date.now();
  const seen = await waitFor(async () => (await stats(page)).lastFrameAt > mark, timeoutMs, 100);
  return seen ? Date.now() - from : null;
}

function longSide(value) {
  return Math.max(Number(value?.width) || 0, Number(value?.height) || 0);
}

// --- Android ---------------------------------------------------------------------------------

async function adb(argv, options) {
  return await run(state.adb, ["-s", state.serial, ...argv], options);
}

async function shell(command, options) {
  return (await adb(["shell", command], options)).stdout.trim();
}

async function adbSerials() {
  const listed = await run(state.adb, ["devices"], { timeout: 30_000 });
  return listed.stdout.split("\n").slice(1).map((line) => line.trim().split(/\s+/)).filter((parts) => parts[0])
    .map(([serial, status]) => `${serial} ${status ?? ""}`.trim()).sort();
}

async function frontPackage() {
  const dump = await shell("dumpsys activity activities");
  return dump.match(/topResumedActivity=ActivityRecord\{\S+ \S+ ([^/\s]+)\//)?.[1] ??
    dump.match(/mResumedActivity: ActivityRecord\{\S+ \S+ ([^/\s]+)\//)?.[1] ?? null;
}

async function launcherPackage() {
  const resolved = await shell("cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME");
  const line = resolved.split("\n").map((item) => item.trim()).filter((item) => item.includes("/")).at(-1);
  return line ? line.split("/")[0] : null;
}

async function openSettings() {
  await shell("input keyevent KEYCODE_HOME");
  for (const name of [SETTINGS_ANDROID, SETTINGS_SEARCH_ANDROID]) await shell(`am force-stop ${name}`);
  await shell("am start -W -a android.settings.SETTINGS");
  const front = await waitFor(async () => (await frontPackage()) === SETTINGS_ANDROID, 10_000, 500);
  await sleep(1000);
  return Boolean(front);
}

async function uiNodes() {
  const { stdout } = await adb(["exec-out", "uiautomator", "dump", "/dev/tty"], { timeout: 60_000 });
  const xml = String(stdout ?? "");
  return [...xml.matchAll(/<node [^>]*>/g)].map((match) => {
    const node = match[0];
    const attr = (name) => node.match(new RegExp(`${name}="([^"]*)"`))?.[1] ?? "";
    const bounds = attr("bounds").match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/);
    return {
      text: attr("text"), id: attr("resource-id"), desc: attr("content-desc"), cls: attr("class"),
      focused: attr("focused") === "true",
      center: bounds ? { x: Math.round((Number(bounds[1]) + Number(bounds[3])) / 2), y: Math.round((Number(bounds[2]) + Number(bounds[4])) / 2) } : null,
    };
  });
}

async function focusedFieldText() {
  const nodes = await uiNodes();
  return nodes.find((node) => node.focused && /EditText/.test(node.cls))?.text ??
    nodes.find((node) => /EditText/.test(node.cls))?.text ?? null;
}

/** A free even console port: no adb serial on it and neither it nor port+1 accepting connections. */
async function freePort() {
  const serials = (await adbSerials()).map((line) => line.split(" ")[0]);
  for (let port = PORT_FIRST; port <= PORT_LAST; port += 2) {
    if (RESERVED_PORTS.has(port) || serials.includes(`emulator-${port}`)) continue;
    if (await portInUse(port) || await portInUse(port + 1)) continue;
    return port;
  }
  throw new Error(`no free emulator port from ${PORT_FIRST} to ${PORT_LAST}`);
}

async function listAvds(emulatorPath) {
  const listed = await run(emulatorPath, ["-list-avds"], { timeout: 60_000 });
  return listed.stdout.split("\n").map((line) => line.trim()).filter((line) => line && !line.startsWith("INFO"));
}

/** The real scrcpy-server 4.1 as the Canvas would find it, through the Canvas's own resolution. */
async function realScrcpyServer() {
  const { resolveScrcpyServer } = await import(pathToFileURL(join(SCRIPTS, "scrcpy-session.mjs")).href);
  const { SCRCPY_PROTOCOL_VERSION } = await import(pathToFileURL(join(SCRIPTS, "scrcpy-lib.mjs")).href);
  const found = await resolveScrcpyServer({
    serverPath: args["scrcpy-server"] ?? null,
    version: null,
    env: process.env,
    findExecutable: async (name) => {
      const path = whichSync(name);
      if (!path) throw new Error(`${name} not found on PATH`);
      return path;
    },
  });
  if (!found.ok) throw new Error(`no usable scrcpy-server: ${found.reason}`);
  if (found.version !== SCRCPY_PROTOCOL_VERSION) {
    throw new Error(`scrcpy-server ${found.version} found at ${found.serverPath}; the Canvas needs ${SCRCPY_PROTOCOL_VERSION}`);
  }
  return { path: found.serverPath, version: found.version, source: found.source };
}

async function androidSetup() {
  const sdk = process.env.ANDROID_HOME || process.env.ANDROID_SDK_ROOT ||
    (process.platform === "darwin" ? join(homedir(), "Library/Android/sdk") : join(homedir(), "Android/Sdk"));
  state.adb = tool(args.adb, [join(sdk, "platform-tools/adb")], "adb");
  const emulatorPath = tool(args.emulator, [join(sdk, "emulator/emulator")], "emulator");
  const avdmanager = tool(args.avdmanager, [join(sdk, "cmdline-tools/latest/bin/avdmanager")], "avdmanager");
  report.setup.tools = { sdk, adb: state.adb, emulator: emulatorPath, avdmanager };
  report.setup.system_image = SYSTEM_IMAGE;
  report.setup.system_image_present = existsSync(join(sdk, "system-images/android-36/google_apis_playstore/arm64-v8a"));
  state.otherDevicesBefore = await adbSerials();

  const server = await realScrcpyServer();
  report.setup.scrcpy_server = { source: server.source, version: server.version };
  const streamServer = join(scratch, "stream", `scrcpy-server-v${server.version}`);
  await mkdir(join(scratch, "stream"), { recursive: true });
  await copyFile(server.path, streamServer);
  // The path the Setup checklist checks: its folder exists, the file not yet.
  const healthServer = join(scratch, "health", `scrcpy-server-v${server.version}`);
  await mkdir(join(scratch, "health"), { recursive: true });
  state.realServer = server.path;
  state.healthServer = healthServer;

  if ((await listAvds(emulatorPath)).includes(AVD_NAME)) {
    throw new Error(`the AVD ${AVD_NAME} already exists; it is not this run's, so it is neither used nor deleted. ` +
      `Delete it yourself (avdmanager delete avd -n ${AVD_NAME}) and run again.`);
  }
  state.emulatorPath = emulatorPath;
  state.avdmanager = avdmanager;
  assertNotAborting("avdmanager create avd");
  state.avdCreate = startCreate(avdmanager, ["create", "avd", "-n", AVD_NAME, "-k", SYSTEM_IMAGE, "-d", AVD_DEVICE],
    { timeout: 180_000, input: "no\n" });
  const created = await state.avdCreate.promise;
  // The AVD is this run's own only when its own create succeeded. A failed create, even with the
  // AVD listed afterwards, may have lost a race with another run ("already exists"), so that AVD
  // is never used or deleted.
  state.avdCreated = created.code === 0;
  report.setup.avd = { name: AVD_NAME, device: AVD_DEVICE, created: state.avdCreated, create_code: created.code };
  if (created.code !== 0) throw new Error(`avdmanager create avd failed: ${clean(created.stderr || created.stdout, 400)}`);
  assertNotAborting("the emulator");

  state.port = await freePort();
  state.serial = `emulator-${state.port}`;
  report.setup.serial = state.serial;
  const bootFrom = Date.now();
  let emulatorOutput = "";
  assertNotAborting("the emulator");
  state.emulatorChild = spawn(emulatorPath, ["-avd", AVD_NAME, "-port", String(state.port), "-no-window", "-no-audio",
    "-no-boot-anim", "-no-snapshot"], { stdio: ["ignore", "pipe", "pipe"] });
  state.emulatorChild.stdout.on("data", (chunk) => { emulatorOutput = (emulatorOutput + chunk).slice(-4000); });
  state.emulatorChild.stderr.on("data", (chunk) => { emulatorOutput = (emulatorOutput + chunk).slice(-4000); });
  state.emulatorExited = new Promise((resolvePromise) => {
    state.emulatorChild.once("exit", (code, signal) => resolvePromise(code ?? signal));
    // An emulator that cannot be started at all ends here too, never as an uncaught error.
    state.emulatorChild.once("error", (error) => {
      state.emulatorSpawnError = error.message;
      resolvePromise("error");
    });
  });
  const booted = await waitFor(async () => {
    if (state.emulatorSpawnError) return "spawn-error";
    if (state.emulatorChild.exitCode !== null) return "exited";
    return (await shell("getprop sys.boot_completed", { timeout: 15_000 })) === "1";
  }, bootTimeoutS * 1000, 2000);
  if (booted !== true) {
    throw new Error(booted === "spawn-error" ? `the emulator did not start: ${clean(state.emulatorSpawnError, 300)}`
      : booted === "exited" ? `the emulator exited: ${clean(emulatorOutput, 600)}`
        : `the emulator did not boot within ${bootTimeoutS} s`);
  }
  report.timings_ms.boot = Date.now() - bootFrom;
  // The serial is the run's own emulator, by its AVD name.
  const name = (await adb(["emu", "avd", "name"], { timeout: 15_000 })).stdout.split("\n")[0].trim();
  if (name !== AVD_NAME) throw new Error(`${state.serial} runs ${name || "an unknown AVD"}, not ${AVD_NAME}; refusing it`);
  await shell("input keyevent KEYCODE_WAKEUP");
  await shell("wm dismiss-keyguard");
  report.setup.launcher = await launcherPackage();

  const env = { ...process.env, AUTONOM_HOME: autonomHome, AUTONOM_CAPTURES_DIR: capturesDir, AUTONOM_SCRCPY_SERVER: healthServer };
  delete env.SCRCPY_SERVER_PATH;
  // The checklist's adb and emulator are the ones this run uses.
  if (!process.env.AUTONOM_ADB && existsSync(state.adb)) env.AUTONOM_ADB = state.adb;
  if (!process.env.AUTONOM_EMULATOR && existsSync(emulatorPath)) env.AUTONOM_EMULATOR = emulatorPath;
  const canvasFrom = Date.now();
  await startCanvas(["--platform", "android", "--serial", state.serial, "--adb", state.adb, "--transport", "auto",
    "--scrcpy-server", streamServer], env);
  report.setup.canvas = { transport: (await state.canvas.status()).transport ?? null,
    health_scrcpy_server_exists_at_start: existsSync(healthServer) };
  await openPage();
  report.timings_ms.canvas_and_page = Date.now() - canvasFrom;
}

async function healthItems(page) {
  return await page.evaluate(() => [...document.querySelectorAll("#actions-health [data-tool]")].map((node) => ({
    tool: node.getAttribute("data-tool"), state: node.getAttribute("data-state"),
    fix: node.querySelector(".setup-command")?.textContent ?? "",
    copy: Boolean(node.querySelector(".setup-copy")),
  })));
}

async function androidSteps() {
  const page = state.page;

  await step("checklist_missing", async (detail) => {
    await page.click("#actions-toggle");
    await page.click("#actions-tab-activity");
    const item = await waitFor(async () => (await healthItems(page)).find((entry) => entry.tool === "scrcpy_server"), 30_000, 250);
    detail.items = (await healthItems(page)).map(({ tool, state: itemState }) => ({ tool, state: itemState }));
    detail.scrcpy_server = item ? { state: item.state, fix_shown: item.fix.trim().length > 0, copy: item.copy } : null;
    const health = await state.canvas.request("GET", "/api/health");
    const entry = (health.body.tools ?? []).find((tool) => tool.name === "scrcpy_server");
    detail.api = entry ? { found: entry.found, ok: entry.ok, group: entry.group ?? null,
      fix_present: typeof entry.fix === "string" && entry.fix.trim().length > 0 } : null;
    return item?.state === "missing" && item.fix.trim().length > 0 && item.copy && detail.api?.fix_present === true &&
      detail.api.found === false;
  });

  await step("checklist_ticks", async (detail) => {
    const from = Date.now();
    await copyFile(state.realServer, state.healthServer);
    const ok = await waitFor(async () => (await healthItems(page)).find((entry) => entry.tool === "scrcpy_server")?.state === "ok",
      TICK_BAR_MS + 5000, 200);
    const okAt = Date.now();
    detail.ticked_after_ms = ok ? okAt - from : null;
    // Refresh requests must stop once the needed items are OK.
    await sleep(12_000);
    const refreshes = state.network.filter((entry) => entry.kind === "health" && entry.refresh);
    detail.refreshes_after_file = refreshes.filter((entry) => entry.at >= from).length;
    detail.refreshes_after_ok = refreshes.filter((entry) => entry.at > okAt + 500).length;
    const times = refreshes.map((entry) => entry.at);
    detail.refresh_intervals_ms = times.slice(1).map((time, index) => time - times[index]);
    detail.items = (await healthItems(page)).map(({ tool, state: itemState }) => ({ tool, state: itemState }));
    return Boolean(ok) && detail.ticked_after_ms <= TICK_BAR_MS && detail.refreshes_after_ok === 0 &&
      detail.refresh_intervals_ms.every((interval) => interval >= 4500);
  });

  await step("home", async (detail) => {
    detail.settings_in_front = await openSettings();
    await resetSent(page);
    const mark = await marker(page);
    detail.focused = await focusScreen(page);
    await chord(page, ["MOD", "Shift", "KeyH"]);
    const launcher = report.setup.launcher;
    detail.front = await waitFor(async () => {
      const front = await frontPackage();
      return front === launcher ? front : null;
    }, 8000, 400) ?? await frontPackage();
    const sent = await sentMessages(page);
    detail.system_home = sent.filter((message) => message.t === "system" && message.op === "home").length;
    detail.key_messages = sent.filter((message) => message.t === "key").length;
    detail.meta_keys = sent.filter((message) => message.t === "key" && (message.meta & (META_ON | CTRL_ON))).length;
    detail.no_reload = (await marker(page)) === mark;
    return detail.settings_in_front && detail.focused && Boolean(launcher) && detail.front === launcher &&
      detail.system_home === 1 && detail.key_messages === 0 && detail.no_reload;
  });

  await step("capture", captureStep);

  await step("unmapped", async (detail) => {
    detail.settings_in_front = await openSettings();
    const field = (await uiNodes()).find((node) => node.center && /search/i.test(`${node.text} ${node.id} ${node.desc}`));
    detail.search_found = Boolean(field);
    if (field) await shell(`input tap ${field.center.x} ${field.center.y}`);
    await sleep(1500);
    await shell(`input text ${FIELD_TEXT}`);
    await sleep(1000);
    const before = await focusedFieldText();
    detail.field_before_matches = before === FIELD_TEXT;
    await resetSent(page);
    detail.focused = await focusScreen(page);
    await chord(page, ["MOD", "KeyA"]);
    await sleep(800);
    const sent = await sentMessages(page);
    detail.sent = sent.filter((message) => message.t === "key").map((message) => ({ a: message.a, code: message.code, meta: message.meta }));
    detail.a_with_meta = sent.some((message) => message.t === "key" && message.a === "down" && message.code === KEYCODE_A &&
      (message.meta & MOD_META_BIT) !== 0);
    detail.system_messages = sent.filter((message) => message.t === "system").length;
    // What the device made of it, reported only: a key typed now replaces a selection.
    detail.device = { front_after_chord: await frontPackage() };
    await page.keyboard.press("KeyZ");
    await sleep(1000);
    const after = await focusedFieldText();
    detail.device.text_replaced_by_next_key = after === "z";
    detail.device.field_unchanged = after === `${FIELD_TEXT}z` || after === FIELD_TEXT;
    await shell("input keyevent KEYCODE_ESCAPE");
    await shell("input keyevent KEYCODE_HOME");
    return detail.focused && detail.a_with_meta && detail.system_messages === 0;
  });

  await step("display", async (detail) => {
    report.checks.display_one_restart_each = false;
    await openSettingsPanel(page);
    const before = (await state.canvas.status()).stream_settings ?? null;
    const beforeStats = await stats(page);
    detail.before = { settings: before, video: { width: beforeStats.width, height: beforeStats.height } };
    const mark = await activityMark();

    let watch = await watchRestart(page);
    await pickDisplay(page, "display-fps", 15);
    const fpsApplied = await waitFor(async () => (await state.canvas.status()).stream_settings?.fps === 15, FRAMES_BAR_MS + 5000, 250);
    await waitFor(async () => (await watch()).back_ms !== null, FRAMES_BAR_MS + 5000, 200);
    detail.fps_pick = { applied: Boolean(fpsApplied), ...await watch() };

    // The next change waits for the 3 s restart spacing to pass.
    await sleep(3500);
    watch = await watchRestart(page);
    await pickDisplay(page, "display-size", "half");
    const sizeApplied = await waitFor(async () => {
      const settings = (await state.canvas.status()).stream_settings;
      return settings?.choice?.size === "half" && settings.fps === 15 ? settings : null;
    }, FRAMES_BAR_MS + 5000, 250);
    await waitFor(async () => (await watch()).back_ms !== null, FRAMES_BAR_MS + 5000, 200);
    await waitFor(async () => longSide(await stats(page)) < longSide(beforeStats) * 0.75, 10_000, 200);
    const afterStats = await stats(page);
    detail.size_pick = { applied: Boolean(sizeApplied), ...await watch() };
    detail.after = { settings: sizeApplied || (await state.canvas.status()).stream_settings,
      video: { width: afterStats.width, height: afterStats.height }, max_fps: (await state.canvas.status()).scrcpy?.max_fps ?? null };
    const ratio = longSide(beforeStats) ? longSide(afterStats) / longSide(beforeStats) : null;
    detail.long_side_ratio = ratio === null ? null : Math.round(ratio * 1000) / 1000;
    detail.restarts = await streamRestartsSince(mark);
    detail.menu = await menuValues(page);
    const framesBack = [detail.fps_pick.back_ms, detail.size_pick.back_ms].every((ms) => ms !== null && ms <= FRAMES_BAR_MS);
    report.checks.display_one_restart_each = detail.restarts.length === 2 && detail.restarts.every((entry) => entry.ok === true);
    return detail.fps_pick.applied && detail.size_pick.applied && detail.after.settings?.fps === 15 &&
      ratio !== null && ratio >= 0.4 && ratio <= 0.6 && framesBack;
  });

  await step("display_reload", async (detail) => {
    const before = report.steps.display?.after?.video ?? null;
    const mark = await activityMark();
    await page.reload();
    await waitStreaming(page);
    await installRecorder(page);
    await openSettingsPanel(page);
    await sleep(3000);
    const settings = (await state.canvas.status()).stream_settings ?? null;
    const now = await stats(page);
    detail.settings = settings;
    detail.menu = await menuValues(page);
    detail.video = { width: now.width, height: now.height };
    detail.frame_after_ms = await nextFrame(page, FRAMES_BAR_MS);
    detail.restarts_after_reload = (await streamRestartsSince(mark)).length;
    return settings?.fps === 15 && settings?.choice?.fps === 15 && settings?.choice?.size === "half" &&
      detail.menu.fps === "15" && detail.menu.size === "half" && before !== null && longSide(now) === longSide(before);
  });
}

async function androidCleanup(detail) {
  detail.canvas_exit = state.canvas ? await state.canvas.stop().catch((error) => clean(error.message)) : null;
  if (state.emulatorChild && !state.emulatorSpawnError) {
    if (state.emulatorChild.exitCode === null && state.emulatorChild.signalCode === null) {
      await adb(["emu", "kill"], { timeout: 30_000 });
      const exited = await Promise.race([state.emulatorExited.then(() => true), sleep(60_000).then(() => false)]);
      if (!exited) {
        state.emulatorChild.kill("SIGTERM");
        if (!(await Promise.race([state.emulatorExited.then(() => true), sleep(15_000).then(() => false)]))) {
          state.emulatorChild.kill("SIGKILL");
          await Promise.race([state.emulatorExited, sleep(10_000)]);
        }
      }
    }
    detail.emulator_process_ended = state.emulatorChild.exitCode !== null || state.emulatorChild.signalCode !== null;
    detail.serial_gone = Boolean(await waitFor(async () =>
      !(await adbSerials()).some((line) => line.split(" ")[0] === state.serial), 30_000, 1000));
  } else {
    detail.emulator_process_ended = true;
    detail.serial_gone = true;
  }
  detail.avd_create_attempted = Boolean(state.avdCreate);
  if (state.avdCreate) {
    // An interrupt may have come while avdmanager create was still running: wait for it to end,
    // so the AVD it makes is seen and deleted rather than left behind.
    const created = await settleCreate(state.avdCreate);
    detail.avd_create_code = created?.code ?? null;
    detail.avd_create_stopped_by_cleanup = state.avdCreate.killed;
    state.avdCreated = created?.code === 0;
    if (state.avdCreated) {
      const deleted = await run(state.avdmanager, ["delete", "avd", "-n", AVD_NAME], { timeout: 120_000 });
      detail.avd_delete_code = deleted.code;
    }
    // Checked by listing, whether or not the AVD was the run's own.
    detail.avd_gone = !(await listAvds(state.emulatorPath)).includes(AVD_NAME);
    if (!detail.avd_gone && !state.avdCreated) {
      detail.avd_left = `${AVD_NAME} is listed but this run's create did not succeed, so it may belong to another run ` +
        `and was not deleted; if it is not in use, delete it with avdmanager delete avd -n ${AVD_NAME}`;
    }
  } else {
    // No create was started, so this run made no AVD.
    detail.avd_gone = true;
  }
  if (state.otherDevicesBefore) {
    const after = (await adbSerials()).filter((line) => line.split(" ")[0] !== state.serial);
    const before = state.otherDevicesBefore.filter((line) => line.split(" ")[0] !== state.serial);
    detail.other_devices_untouched = after.join("\n") === before.join("\n");
    if (!detail.other_devices_untouched) detail.other_devices = { before, after };
  } else {
    detail.other_devices_untouched = true;
  }
  return detail.serial_gone && detail.emulator_process_ended && detail.avd_gone && detail.other_devices_untouched;
}

// --- iOS -------------------------------------------------------------------------------------

async function simctl(argv, options) {
  return await run(args.xcrun, ["simctl", ...argv], options);
}

async function simulators() {
  const listed = await simctl(["list", "devices", "--json"], { timeout: 60_000 });
  if (listed.code !== 0) throw new Error(`simctl list devices failed: ${clean(listed.stderr, 300)}`);
  return Object.values(JSON.parse(listed.stdout).devices ?? {}).flat();
}

function stamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

/**
 * One `idb ui describe-all` read: `front` is the first Application element's label ("" when the
 * tree has elements but no labelled Application, null when it cannot be read), `elements` the
 * flat element list of that same read.
 */
async function readIosFront() {
  const described = await run(args.idb, ["ui", "describe-all", "--udid", state.udid, "--json"], { timeout: 60_000 });
  if (described.code !== 0) return { front: null, elements: [] };
  let elements = null;
  try {
    elements = JSON.parse(described.stdout);
  } catch {
    elements = described.stdout.split("\n").map((line) => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean).flat();
  }
  const list = Array.isArray(elements) ? elements.flat() : [];
  const app = list.find(isIosApplication);
  return { front: app ? String(app.AXLabel ?? app.label ?? "").trim() : (list.length ? "" : null), elements: list };
}

function isIosApplication(element) {
  return element?.type === "Application" || element?.role === "AXApplication";
}

/** The first Application element's label in `idb ui describe-all`, or null when it cannot be read. */
async function frontIosApp() {
  return (await readIosFront()).front;
}

/**
 * Which rule shows that a describe-all read with no labelled Application element is the home
 * screen, or null. The companion names UIAccessibility traits in `traits` (SpringBoard icons
 * carry LaunchIcon) and may type an icon `Icon`; the plainest sign is any non-Application element
 * labelled "Settings" (the Settings icon; inside Settings the read has a labelled Application).
 */
function iosHomeEvidence(elements) {
  const icons = elements.filter((element) => element && !isIosApplication(element) &&
    String(element.AXLabel ?? element.label ?? "").trim() === "Settings");
  const traitsOf = (element) => {
    const traits = element.traits ?? element.AXTraits ?? [];
    return Array.isArray(traits) ? traits.map(String) : String(traits).split(/[\s,]+/);
  };
  if (icons.some((element) => traitsOf(element).includes("LaunchIcon"))) return "settings_launch_icon_trait";
  if (icons.some((element) => element.type === "Icon")) return "settings_icon_type";
  // Not the Settings page's own "Settings" heading, should a read inside Settings lack its Application.
  if (icons.some((element) => element.type !== "Heading" && element.role !== "AXHeading")) return "settings_label_not_application";
  return null;
}

async function cliCompanions() {
  const listed = await run("pgrep", ["-fl", `idb_companion --udid ${state.udid}`]);
  return listed.stdout.split("\n").map((line) => line.trim()).filter(Boolean)
    .map((line) => ({ pid: Number(line.split(/\s+/)[0]), cli: line.includes("/tmp/idb/") })).filter((entry) => entry.pid);
}

/** The UDID a finished simctl create printed, taken as the run's own Simulator; null when it failed. */
function adoptCreatedSimulator(record) {
  const result = record?.result;
  if (!result) return null;
  const udid = result.stdout.trim().split("\n").at(-1)?.trim() ?? "";
  if (result.code !== 0 || !/^[0-9A-F-]{36}$/i.test(udid)) return null;
  state.udid = udid;
  state.simulatorName = record.name;
  state.simulatorCreated = true;
  return udid;
}

async function iosSetup() {
  report.setup.tools = { xcrun: args.xcrun, idb: args.idb, idb_companion: args["idb-companion"] ?? null };
  const before = await simulators();
  state.otherDevicesBefore = before.map((device) => `${device.udid} ${device.state}`).sort();
  const types = JSON.parse((await simctl(["list", "devicetypes", "--json"])).stdout || "{}").devicetypes ?? [];
  const runtimes = JSON.parse((await simctl(["list", "runtimes", "--json"])).stdout || "{}").runtimes ?? [];
  const type = types.find((item) => item.name === SIM_DEVICE_TYPE);
  const runtime = runtimes.find((item) => item.name === SIM_RUNTIME && item.isAvailable !== false);
  if (!type) throw new Error(`the device type ${SIM_DEVICE_TYPE} is not installed`);
  if (!runtime) throw new Error(`the runtime ${SIM_RUNTIME} is not installed`);
  const name = `${SIM_NAME_PREFIX}${stamp()}`;
  if (before.some((device) => device.name === name)) throw new Error(`a Simulator named "${name}" already exists; run again in a minute`);
  assertNotAborting("simctl create");
  state.simulatorCreate = startCreate(args.xcrun, ["simctl", "create", name, type.identifier, runtime.identifier],
    { timeout: 120_000 });
  state.simulatorCreate.name = name;
  const created = await state.simulatorCreate.promise;
  const udid = adoptCreatedSimulator(state.simulatorCreate);
  if (!udid) throw new Error(`simctl create failed: ${clean(created.stderr || created.stdout, 300)}`);
  report.setup.simulator = { name, udid, device_type: type.identifier, runtime: runtime.identifier };
  const bootFrom = Date.now();
  assertNotAborting("simctl boot");
  const booted = await simctl(["boot", udid], { timeout: 120_000 });
  if (booted.code !== 0) throw new Error(`simctl boot failed: ${clean(booted.stderr, 300)}`);
  const status = await simctl(["bootstatus", udid, "-b"], { timeout: 600_000 });
  if (status.code !== 0) throw new Error(`simctl bootstatus failed: ${clean(status.stderr, 300)}`);
  report.timings_ms.boot = Date.now() - bootFrom;
  state.companionsBefore = (await cliCompanions()).map((entry) => entry.pid);

  const env = { ...process.env, AUTONOM_HOME: autonomHome, AUTONOM_CAPTURES_DIR: capturesDir };
  const argv = ["--platform", "ios", "--target", udid, "--simctl", args.xcrun, "--transport", "auto"];
  if (args["idb-companion"]) argv.push("--idb-companion", resolve(args["idb-companion"]));
  const canvasFrom = Date.now();
  await startCanvas(argv, env);
  report.setup.canvas = { transport: (await state.canvas.status()).transport ?? null };
  await openPage();
  report.timings_ms.canvas_and_page = Date.now() - canvasFrom;
}

/** Starts the idb CLI's companion for describe-all when none runs; remembered for cleanup. */
async function ensureCliCompanion() {
  if ((await cliCompanions()).some((entry) => entry.cli)) return;
  const before = (await cliCompanions()).map((entry) => entry.pid);
  await run(args.idb, ["connect", state.udid], { timeout: 60_000 });
  state.cliCompanion = (await cliCompanions()).filter((entry) => entry.cli && !before.includes(entry.pid)).map((entry) => entry.pid);
}

async function freshIosSettings() {
  await simctl(["terminate", state.udid, SETTINGS_IOS]);
  const launched = await simctl(["launch", state.udid, SETTINGS_IOS]);
  await sleep(2500);
  return launched.code === 0;
}

async function iosSteps() {
  const page = state.page;

  await step("home", async (detail) => {
    await ensureCliCompanion();
    detail.settings_launched = await freshIosSettings();
    // Right after `simctl launch` Settings may not be in front yet: wait for it (15 s, 500 ms).
    const beforeFrom = Date.now();
    let before = null;
    await waitFor(async () => (before = await frontIosApp()) === "Settings", 15_000, 500);
    detail.front_before = before;
    detail.front_before_wait_ms = Date.now() - beforeFrom;
    detail.front_before_timeout = before !== "Settings";
    if (detail.front_before_timeout) return false;
    await resetSent(page);
    detail.focused = await focusScreen(page);
    await chord(page, ["MOD", "Shift", "KeyH"]);
    // Home: another labelled front application, or "" (no labelled Application element) only
    // when the same read shows a home-screen sign (iosHomeEvidence).
    let last = { front: null, elements: [] };
    const after = await waitFor(async () => {
      last = await readIosFront();
      if (last.front === null || last.front === "Settings") return null;
      if (last.front !== "") return { front: last.front, evidence: "other_front_application" };
      const evidence = iosHomeEvidence(last.elements);
      return evidence ? { front: "", evidence } : null;
    }, 10_000, 500);
    detail.front_after = after ? after.front : last.front;
    detail.home_evidence = after ? after.evidence : null;
    const sent = await sentMessages(page);
    detail.system_home = sent.filter((message) => message.t === "system" && message.op === "home").length;
    detail.other_messages = sent.filter((message) => !(message.t === "system" && message.op === "home")).length;
    return detail.settings_launched && detail.front_before === "Settings" && detail.focused &&
      detail.home_evidence !== null && detail.system_home === 1 && detail.other_messages === 0;
  });

  await step("rotate_message", async (detail) => {
    await resetSent(page);
    detail.focused = await focusScreen(page);
    await chord(page, ["MOD", "ArrowRight"]);
    await sleep(600);
    const sent = await sentMessages(page);
    detail.messages = sent.length;
    detail.note = (await stats(page)).note ?? null;
    detail.notice = await page.evaluate(() => document.getElementById("notice")?.textContent ?? "");
    detail.message_shown = detail.note === ROTATE_MESSAGE || detail.notice.includes(ROTATE_MESSAGE);
    return detail.focused && detail.message_shown && detail.messages === 0;
  });

  await step("field_focus", async (detail) => {
    if ((await page.getAttribute("#tools-toggle", "aria-pressed")) !== "true") await page.click("#tools-toggle");
    await page.waitForSelector("#tools-deeplink", { state: "visible", timeout: 15_000 });
    await page.fill("#tools-deeplink", DEEPLINK_TEXT);
    await page.focus("#tools-deeplink");
    detail.field_focused = await page.evaluate(() => document.activeElement?.id === "tools-deeplink");
    await resetSent(page);
    await chord(page, ["MOD", "KeyL"]);
    await sleep(800);
    detail.messages = (await sentMessages(page)).length;
    detail.field_unchanged = (await page.inputValue("#tools-deeplink")) === DEEPLINK_TEXT;
    await page.fill("#tools-deeplink", "");
    if ((await page.getAttribute("#tools-toggle", "aria-pressed")) === "true") await page.click("#tools-toggle");
    return detail.field_focused && detail.messages === 0 && detail.field_unchanged;
  });

  await step("capture", captureStep);

  await step("display", async (detail) => {
    await openSettingsPanel(page);
    const statusBefore = await state.canvas.status();
    const socketsBefore = await page.evaluate(() => window.__qwSockets.filter((entry) => entry.path.endsWith("/ws/video")).length);
    detail.before = { settings: statusBefore.stream_settings ?? null,
      companion_starts: statusBefore.idb?.companion_starts ?? null, restarts: statusBefore.idb?.restarts ?? null };

    await pickDisplay(page, "display-fps", 15);
    const fps15 = await waitFor(async () => (await state.canvas.status()).stream_settings?.fps === 15, FRAMES_BAR_MS, 250);
    detail.fps15 = { applied: Boolean(fps15), frame_after_ms: await nextFrame(page, FRAMES_BAR_MS) };

    await sleep(1000);
    await pickDisplay(page, "display-fps", 0);
    const fps0 = await waitFor(async () => (await state.canvas.status()).stream_settings?.fps === 0, FRAMES_BAR_MS, 250);
    // On change sends a frame only when the screen changes: open Settings to change it.
    const framePromise = nextFrame(page, FRAMES_BAR_MS);
    await freshIosSettings();
    detail.on_change = { applied: Boolean(fps0), frame_after_ms: await framePromise };

    const statusAfter = await state.canvas.status();
    const socketsAfter = await page.evaluate(() => window.__qwSockets.filter((entry) => entry.path.endsWith("/ws/video")).length);
    const pageStats = await stats(page);
    detail.after = { settings: statusAfter.stream_settings ?? null,
      companion_starts: statusAfter.idb?.companion_starts ?? null, restarts: statusAfter.idb?.restarts ?? null };
    detail.video_sockets_opened = socketsAfter - socketsBefore;
    detail.video_socket_open = pageStats.video === 1;
    detail.menu = await menuValues(page);
    return detail.fps15.applied && detail.fps15.frame_after_ms !== null && detail.on_change.applied &&
      detail.on_change.frame_after_ms !== null && detail.video_sockets_opened === 0 && detail.video_socket_open &&
      detail.after.companion_starts === detail.before.companion_starts && detail.after.restarts === detail.before.restarts;
  });
}

async function iosCleanup(detail) {
  detail.canvas_exit = state.canvas ? await state.canvas.stop().catch((error) => clean(error.message)) : null;
  detail.simulator_create_attempted = Boolean(state.simulatorCreate);
  if (state.simulatorCreate && !state.simulatorCreated) {
    // An interrupt may have come while simctl create was still running: wait for it to end and
    // take the UDID it printed, so that Simulator is deleted rather than left behind.
    await settleCreate(state.simulatorCreate);
    detail.simulator_create_code = state.simulatorCreate.result?.code ?? null;
    detail.simulator_create_stopped_by_cleanup = state.simulatorCreate.killed;
    adoptCreatedSimulator(state.simulatorCreate);
  }
  if (state.cliCompanion?.length) {
    await run(args.idb, ["disconnect", state.udid], { timeout: 30_000 });
  }
  if (state.udid) {
    await sleep(1000);
    // Companions for the run's own Simulator only: the Canvas's and the idb CLI's.
    const left = (await cliCompanions()).filter((entry) => !(state.companionsBefore ?? []).includes(entry.pid));
    for (const entry of left) {
      try { process.kill(entry.pid, "SIGTERM"); } catch { /* already gone */ }
    }
    detail.companions_stopped = left.length;
  }
  if (state.simulatorCreated && state.udid) {
    const listed = (await simulators()).find((device) => device.udid === state.udid);
    // Only the Simulator this run created, by its UDID and its name.
    if (listed && listed.name === state.simulatorName) {
      if (listed.state !== "Shutdown") await simctl(["shutdown", state.udid], { timeout: 120_000 });
      const deleted = await simctl(["delete", state.udid], { timeout: 120_000 });
      detail.simulator_delete_code = deleted.code;
    }
    detail.simulator_gone = !(await simulators()).some((device) => device.udid === state.udid);
  } else if (state.simulatorCreate) {
    // A create that printed no UDID (failed or stopped): look for a new Simulator of the run's
    // name. It is reported, not deleted, since another run in the same minute may own it.
    const before = new Set((state.otherDevicesBefore ?? []).map((line) => line.split(" ")[0]));
    const strays = (await simulators()).filter((device) => device.name === state.simulatorCreate.name &&
      !before.has(device.udid));
    detail.simulator_gone = strays.length === 0;
    if (strays.length) {
      detail.simulator_left = strays.map((device) => device.udid);
      detail.simulator_left_reason = `this run's simctl create printed no UDID, so these "${state.simulatorCreate.name}" ` +
        "Simulators may belong to another run and were not deleted";
    }
  } else {
    // No create was started, so this run made no Simulator.
    detail.simulator_gone = true;
  }
  if (state.otherDevicesBefore) {
    const strays = new Set(detail.simulator_left ?? []);
    const after = (await simulators()).filter((device) => device.udid !== state.udid && !strays.has(device.udid))
      .map((device) => `${device.udid} ${device.state}`).sort();
    detail.other_devices_untouched = after.join("\n") === state.otherDevicesBefore.join("\n");
    if (!detail.other_devices_untouched) detail.other_devices = { before: state.otherDevicesBefore, after };
  } else {
    detail.other_devices_untouched = true;
  }
  return detail.simulator_gone && detail.other_devices_untouched;
}

// --- the run ---------------------------------------------------------------------------------

let cleanedUp = null;

/** Everything the run made goes, once, whatever happened before. */
function cleanup() {
  cleanedUp ??= (async () => {
    const detail = (report.steps.cleanup = {});
    try {
      await state.browser?.close().catch(() => {});
      const deviceOk = platform === "android" ? await androidCleanup(detail) : await iosCleanup(detail);
      detail.canvas_stopped = !state.canvas || !state.canvas.running();
      await rm(scratch, { recursive: true, force: true });
      if (!args["keep-home"]) await rm(autonomHome, { recursive: true, force: true });
      detail.temporary_removed = !existsSync(scratch) && (args["keep-home"] || !existsSync(autonomHome));
      report.checks.cleanup = deviceOk && detail.canvas_stopped && detail.temporary_removed;
    } catch (error) {
      detail.error = clean(error?.stack ?? error?.message ?? error);
      report.checks.cleanup = false;
    }
  })();
  return cleanedUp;
}

let finished = null;

/**
 * Writes the report and exits, once: an interrupt and the main flow both end here, and only the
 * first call writes, so the report is never written twice. An interrupted run exits 130.
 */
function finish(code) {
  finished ??= writeReportAndExit(state.aborting ? 130 : code);
  return finished;
}

async function writeReportAndExit(code) {
  report.ok = !report.error && Object.keys(report.checks).length > 0 && Object.values(report.checks).every(Boolean);
  report.host.loadavg_end = hostLoad();
  report.seconds = Math.round((Date.now() - startedAt) / 100) / 10;
  report.autonom_home = { kept: args["keep-home"], path: args["keep-home"] ? autonomHome : null };
  const reportPath = join(outDir, `canvas-quick-wins-${platform}.json`);
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify({ case: report.case, platform, ok: report.ok, report: reportPath }));
  process.exit(code ?? (report.ok ? 0 : 1));
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    state.aborting = true;
    report.error = `interrupted by ${signal}`;
    cleanup().finally(() => finish(130));
  });
}

try {
  if (platform === "android") {
    await androidSetup();
    report.checks.setup = true;
    await androidSteps();
  } else {
    await iosSetup();
    report.checks.setup = true;
    await iosSteps();
  }
} catch (error) {
  // What the run collected before it failed stays in the report; an interrupt keeps its own message.
  const message = clean(error?.stack ?? error?.message ?? error);
  if (state.aborting) report.error_after_interrupt = message;
  else report.error = message;
} finally {
  await cleanup();
}
// A run that never got to its steps has failed, whatever cleanup says.
if (report.checks.setup !== true) report.checks.setup = false;
await finish();
