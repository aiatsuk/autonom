#!/usr/bin/env node
/**
 * Live acceptance and benchmark for the Mobile Canvas scrcpy transport.
 *
 * Not part of `node --test tests/*.test.mjs`: every case pushes and runs
 * scrcpy-server on the one explicit --serial, injects input there and drives a
 * headless browser through brow (the bench case: Chromium through Playwright, see
 * below). Use a test emulator only. Device settings the cases touch (show_touches,
 * auto-rotate, rotation, display size and density) are restored before they finish.
 *
 * Every run gets its own temporary AUTONOM_HOME under the OS temp directory, handed to
 * the Canvas (and so to its journal bridge) and to every Autonom CLI call, so a case can
 * never read, stop or write the user's own sessions. It is removed at the end unless
 * --keep-home is given.
 *
 *   node tests/live/canvas_scrcpy_live.mjs --serial <adb-serial> --case <name> \
 *     [--evidence-dir DIR] [--scrcpy-server <scrcpy-server-v4.1> [--scrcpy-version X.Y]] \
 *     [--adb PATH] [--brow PATH] [--keep-home] \
 *     [--repeat 3] [--drags 20] [--seconds 10] [--playwright DIR] [--headless]
 *
 * Without --scrcpy-server the Canvas finds the server itself (AUTONOM_SCRCPY_SERVER,
 * SCRCPY_SERVER_PATH, then an installed scrcpy), and the report records which source
 * it used. Cases: picture, bench, tabs, restart, input, journal, display. Each writes
 * <evidence-dir>/<case>.json (--out is another name for --evidence-dir), with the steps
 * done so far also when the case fails midway, and exits non-zero when its oracle fails.
 *
 * bench: --repeat runs, each with a Canvas of its own on the scrcpy transport. In each
 * run a finger driven from a 16 ms timer (not tied to animation frames) drags the Settings
 * list up and down without pause, so the screen changes on (nearly) every frame, and for
 * --seconds the report takes, per whole second: the frames the device produced (scrcpy
 * packets counted by the Canvas server), received, decoded, dropped and presented by the
 * page, distinct frames (a presented frame whose picture differs from the one before),
 * the animation frame rate, finger moves, frame interval percentiles, and the host's CPU
 * load and load averages. Then --drags separate drags, each from a settled screen, give
 * input-to-picture latency (median and p95). The case passes when the presented median
 * is at least 55 fps while the distinct median is at least 45 fps (the screen really
 * changed); latency is reported against its 150 ms bar without failing the case.
 * Chromium runs headed by default, its window placed off screen, since headless Chromium
 * fires animation frames unevenly and cannot present 60 fps steadily; --headless runs it
 * headless anyway. Playwright comes from --playwright, AUTONOM_PLAYWRIGHT,
 * ~/pr/platform/node_modules/playwright, then a `playwright` package Node can resolve;
 * /opt/pw-browsers is used as the browser folder when it exists and
 * PLAYWRIGHT_BROWSERS_PATH is not set.
 */
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { cpus, homedir, loadavg, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs as parseCliArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "../..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const AUTONOM = join(ROOT, "scripts/autonom.py");
const CASES = ["picture", "bench", "tabs", "restart", "input", "journal", "display"];
const SERVER_CLASS = "com.genymobile.scrcpy.Server";
// The scrcpy server itself; `sh -c CLASSPATH=... app_process ...` is only its launcher.
const SERVER_COMMAND = /^(?:\S*\/)?app_process/;
const SETTINGS_PACKAGE = "com.android.settings";
// The Settings search screen is a package of its own and can stay in front of Settings.
const SETTINGS_SEARCH_PACKAGE = "com.google.android.settings.intelligence";
const SETTINGS_FRONT_MS = 5000;
// The Settings homepage is portrait-locked, so rotation is tried with a browser page in front.
const ROTATABLE_PACKAGE = "com.android.chrome";
const ROTATABLE_ACTIVITY = "com.android.chrome/com.google.android.apps.chrome.Main";
// The status bar: its clock and icons change on their own.
const STATUS_BAR_PACKAGE = "com.android.systemui";
const REMOTE_JAR = "/data/local/tmp/autonom-scrcpy-4.1.jar";
// Bench bars (A60-002): the presented median while the screen changes on every frame, and
// the distinct-frame median that shows it really did. Latency keeps its CANVAS-014 bar,
// reported without failing the case.
const PRESENTED_BAR = 55;
const DISTINCT_BAR = 45;
const LATENCY_BAR_MS = 150;
// Input-to-picture latency: touch down to the first presented frame whose whole picture,
// drawn at 36x80, differs from the settled one by more than 1.5 per channel on average.
// A 48x48 patch at the finger missed 12 of 12 drags on sparse Settings rows.
const PROBE_SIZE = { width: 36, height: 80 };
const CHANGE_THRESHOLD = 1.5;
// Moves per bench stroke, one every BENCH_STEP_MS from a timer, not from animation frames:
// a drag from 75% to 30% of the screen height and back.
const BENCH_MOVES = 50;
const BENCH_STEP_MS = 16;
const BENCH_FROM = 0.75;
const BENCH_TO = 0.3;
// Distinct frames: the picture between 12% and 95% of its height (below the status bar,
// whose clock changes on its own), as 64x64 luma; a presented frame is distinct when at
// least 16 of its pixels differ from the frame before by more than 12 levels.
const DISTINCT_BAND = [0.12, 0.95];
const DISTINCT_SAMPLE = 64;
const DISTINCT_LEVEL = 12;
const DISTINCT_PIXELS = 16;
// Before a latency drag the previous fling settles for this long.
const LATENCY_SETTLE_MS = 700;
// A latency drag keeps watching for its first change this long after its last move.
const LATENCY_TAIL_MS = 300;
const OFF_SCREEN_WINDOW = "--window-position=3000,3000";
// The Canvas asks scrcpy for a key frame (RESET_VIDEO) at most once a second.
const NUDGE_SPACING_MS = 1100;
// Drag moves are spaced like a real finger's: Android lists do not scroll for a drag
// whose moves all arrive within a millisecond.
const DRAG_MOVE_INTERVAL_MS = 16;
// How long after the last move of a paced drag the screen is looked at.
const DRAG_SETTLE_MS = 600;
// What the input case types into the Settings search field: keys, then text, then a paste.
const TYPED_TEXT = "wifi Café ✓ naïve π ≈ 3.14 ✓";
// Where a scrcpy-server is configured; the picture case discovers an installed scrcpy without them.
const SERVER_VARIABLES = ["AUTONOM_SCRCPY_SERVER", "SCRCPY_SERVER_PATH"];
// The drag of the journal case.
const JOURNAL_MOVES = 40;
// Keys of other layouts, dispatched as browser key events to the page: Cyrillic on KeyQ
// and KeyW, AZERTY a on KeyQ, QWERTZ z on KeyY, US q on KeyQ, AltGr @ on KeyQ, then a dead
// acute followed by e-acute. Non-ASCII is written as escapes.
const LAYOUT_KEYS = [
  { code: "KeyQ", key: "\u0439" }, { code: "KeyW", key: "\u0446" },
  { code: "KeyQ", key: "a" },
  { code: "KeyY", key: "z" },
  { code: "KeyQ", key: "q" },
  { code: "KeyQ", key: "@", ctrlKey: true, altKey: true },
  { code: "Quote", key: "Dead" }, { code: "KeyE", key: "\u00e9" },
];
const LAYOUT_TEXT = "\u0439\u0446azq@\u00e9";
// The input dispatcher's line for a display without any finger down.
const NO_TOUCH = "TouchStatesByDisplay: <no displays touched>";
// The display case: the Size picker's presets in page order, with the values each one
// must read back as (DISPLAY-001); `default` reads back as the physical display.
const DISPLAY_PRESETS = [
  { id: "small", width: 720, height: 1280, density: 320 },
  { id: "pixel-11", width: 1080, height: 2424, density: 420 },
  { id: "pixel-fold", width: 2208, height: 1840, density: 420 },
  { id: "tablet", width: 2560, height: 1600, density: 320 },
  { id: "default" },
];
// The override a second Canvas finds already set, and must put back at its stop.
const PREEXISTING_DISPLAY = { size: "1600x2560", density: 300 };
// scrcpy scales the video to max_size and rounds each side to a multiple of 8, so the
// page's aspect ratio may differ from the preset's by this share.
const ASPECT_TOLERANCE = 0.02;
const DISPLAY_CHANGE_MS = 15_000;
// Android crossfades the old and the new picture for a moment after the size changes. A
// preset's screenshot waits until its size has been in effect this long and the page has
// drawn this many frames since, so it shows a settled frame.
const DISPLAY_SETTLE_MS = 1500;
const DISPLAY_SETTLE_FRAMES = 3;
const DISPLAY_SETTLE_WAIT_MS = 10_000;

const { values: args } = parseCliArgs({
  options: {
    serial: { type: "string" },
    case: { type: "string" },
    "scrcpy-server": { type: "string" },
    "scrcpy-version": { type: "string" },
    adb: { type: "string" },
    brow: { type: "string", default: "brow" },
    python: { type: "string", default: "python3" },
    session: { type: "string", default: "canvas-qa" },
    "evidence-dir": { type: "string" },
    out: { type: "string" },
    repeat: { type: "string", default: "3" },
    drags: { type: "string", default: "20" },
    seconds: { type: "string", default: "10" },
    playwright: { type: "string" },
    headless: { type: "boolean", default: false },
    "keep-home": { type: "boolean", default: false },
  },
});

function usage(message) {
  console.error(`canvas_scrcpy_live: ${message}`);
  console.error(`usage: canvas_scrcpy_live.mjs --serial SERIAL --case ${CASES.join("|")} ` +
    "[--evidence-dir DIR] [--scrcpy-server PATH [--scrcpy-version X.Y]] [--adb PATH] [--brow PATH] " +
    "[--repeat N] [--drags N] [--seconds N] [--playwright DIR] [--headless] [--keep-home]");
  process.exit(2);
}

if (!args.serial) usage("--serial is required; the cases never pick a device by themselves");
if (!CASES.includes(args.case)) usage(`--case must be one of ${CASES.join(", ")}`);
if (args["scrcpy-version"] && !args["scrcpy-server"]) {
  usage("--scrcpy-version names the version of --scrcpy-server; pass both");
}
for (const name of ["repeat", "drags", "seconds"]) {
  if (!/^[1-9]\d*$/.test(args[name])) usage(`--${name} must be a positive whole number`);
}
if (args["evidence-dir"] && args.out && resolve(args["evidence-dir"]) !== resolve(args.out)) {
  usage("--out is another name for --evidence-dir; give one directory");
}
const serverPath = args["scrcpy-server"] ? resolve(args["scrcpy-server"]) : null;
const serial = args.serial;
const adbPath = args.adb ?? "adb";
const evidenceDir = args["evidence-dir"] ?? args.out;
const outDir = evidenceDir ? resolve(evidenceDir) : await mkdtemp(join(tmpdir(), "autonom-canvas-live-"));
await mkdir(outDir, { recursive: true });
// This run's own Autonom machine store: its sessions, journals and process registry.
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-canvas-live-home-"));
const liveEnv = { ...process.env, AUTONOM_HOME: autonomHome };
// The scrcpy-server the Canvas chose, from its first scrcpy /status.
let discovery = null;

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function run(file, argv, { timeout = 30_000, encoding = "utf8", env } = {}) {
  return new Promise((resolvePromise) => {
    execFile(file, argv, { timeout, encoding, maxBuffer: 64 * 1024 * 1024, env: env ?? process.env },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (error.code ?? 1) : 0, stdout, stderr: String(stderr ?? "") });
      });
  });
}

async function adb(argv, options) {
  return await run(adbPath, ["-s", serial, ...argv], options);
}

async function shell(command) {
  return (await adb(["shell", command])).stdout.trim();
}

/** Put back a system setting read before a case; "null" means it was never set. */
async function restoreSetting(name, value) {
  if (value === "null" || value === "") await shell(`settings delete system ${name}`);
  else await shell(`settings put system ${name} ${value}`);
}

/** The Autonom CLI, always inside this run's own AUTONOM_HOME. */
async function autonom(argv) {
  return await run(args.python, [AUTONOM, ...argv], { env: liveEnv });
}

/** The package of the activity in front, from `dumpsys activity activities`; null if unknown. */
async function frontPackage() {
  const dump = await shell("dumpsys activity activities");
  return dump.match(/topResumedActivity=ActivityRecord\{\S+ \S+ ([^/\s]+)\//)?.[1] ?? null;
}

/**
 * Settings opened afresh at its top: a list an earlier case left at the bottom cannot
 * scroll further, and its search bar collapses once the list is scrolled. Its search
 * screen is stopped too, or it can stay in front of the homepage.
 */
async function resetSettings() {
  await shell("input keyevent KEYCODE_HOME");
  // force-stop of a package that is not installed does nothing.
  for (const name of [SETTINGS_PACKAGE, SETTINGS_SEARCH_PACKAGE]) await shell(`am force-stop ${name}`);
  await shell("am start -W -a android.settings.SETTINGS");
  await waitFor(async () => (await frontPackage()) === SETTINGS_PACKAGE, SETTINGS_FRONT_MS,
    "the Settings homepage in front");
  // The list settles a moment after the activity resumes.
  await sleep(1000);
}

async function saveScreencap(name) {
  const { stdout } = await adb(["exec-out", "screencap", "-p"], { encoding: "buffer" });
  const path = join(outDir, name);
  await writeFile(path, stdout);
  return path;
}

/** Process lines on the device that are scrcpy servers, without their `sh -c` launchers. */
async function serverProcesses() {
  return (await shell("ps -A -o PID,ARGS")).split("\n").filter((line) => {
    const [, command = ""] = line.trim().split(/\s+/);
    return SERVER_COMMAND.test(command) && line.includes(SERVER_CLASS);
  });
}

async function brow(session, argv, timeout = 60_000) {
  const result = await run(args.brow, ["--session", session, ...argv], { timeout });
  if (result.code !== 0) throw new Error(`brow ${argv[0]} failed: ${result.stderr || result.stdout}`);
  return result.stdout.trim();
}

/** Evaluate in the page; the expression must produce a JSON string within brow's 3 s. */
async function pageJson(session, expression) {
  const text = await brow(session, ["eval", "--mutate", expression]);
  return JSON.parse(text);
}

function stats(session) {
  return pageJson(session, "JSON.stringify(window.autonomCanvas ? window.autonomCanvas.stats() : null)");
}

function send(session, messages) {
  return pageJson(session,
    `JSON.stringify(${JSON.stringify(messages)}.map((message) => window.autonomCanvas.send(message)))`);
}

/**
 * Send `messages` from inside the page, one every DRAG_MOVE_INTERVAL_MS. The evaluation only
 * schedules them and returns at once, since brow stops an evaluation after 3 s. Resolves
 * DRAG_SETTLE_MS after the last one is due.
 */
async function sendPaced(session, messages) {
  await pageJson(session, `(() => {
    const messages = ${JSON.stringify(messages)};
    messages.forEach((message, index) =>
      setTimeout(() => window.autonomCanvas.send(message), (index + 1) * ${DRAG_MOVE_INTERVAL_MS}));
    return JSON.stringify({ scheduled: messages.length });
  })()`);
  await sleep(messages.length * DRAG_MOVE_INTERVAL_MS + DRAG_SETTLE_MS);
}

async function waitFor(predicate, timeoutMs, what) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await predicate();
    if (last) return last;
    await sleep(250);
  }
  throw new Error(`timed out waiting for ${what}`);
}

/** Start this checkout's Canvas on the serial; resolves once it prints its URL. */
async function startCanvas(transport, extraEnv = {}, { unsetEnv = [] } = {}) {
  const token = randomBytes(18).toString("base64url");
  const argv = [CANVAS, "--serial", serial, "--adb", adbPath, "--port", "0", "--token", token,
    "--transport", transport, "--python", args.python];
  const scrcpyCapable = transport === "scrcpy" || transport === "auto";
  // Without --scrcpy-server the Canvas runs its own discovery, as `autonom canvas serve` does.
  if (scrcpyCapable && serverPath) {
    argv.push("--scrcpy-server", serverPath);
    if (args["scrcpy-version"]) argv.push("--scrcpy-version", args["scrcpy-version"]);
  }
  const env = { ...liveEnv, ...extraEnv };
  for (const name of unsetEnv) delete env[name];
  const child = spawn(process.execPath, argv, {
    cwd: ROOT, env, stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code) => resolvePromise(code)));
  const url = await new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${output}`)), 30_000);
    child.stdout.on("data", () => {
      const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise(match[1]);
    });
    exited.then((code) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${output}`));
    });
  });
  const origin = new URL(url).origin;
  const canvas = {
    child, url, origin, token, exited, output: () => output,
    async status() {
      const response = await fetch(`${origin}/status`, { headers: { Authorization: `Bearer ${token}` } });
      return await response.json();
    },
    async control(mode, origin_ = "agent") {
      const response = await fetch(`${origin}/control`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json",
          "X-Autonom-Origin": origin_ },
        body: JSON.stringify({ mode }),
      });
      return await response.json();
    },
    async display(preset, origin_ = "agent") {
      const response = await fetch(`${origin}/display`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json",
          "X-Autonom-Origin": origin_ },
        body: JSON.stringify({ preset }),
      });
      return { status: response.status, body: await response.json() };
    },
    async stop() {
      if (child.exitCode !== null) return child.exitCode;
      child.kill("SIGTERM");
      return await exited;
    },
  };
  if (scrcpyCapable && !discovery) {
    try {
      const { scrcpy } = await canvas.status();
      discovery = scrcpy && { source: scrcpy.source, server_path: scrcpy.server_path, version: scrcpy.version };
    } catch (error) {
      await canvas.stop();
      throw error;
    }
  }
  return canvas;
}

/**
 * Open the page and wait until it can take input: a streaming session, an open control
 * channel (WebSocket readyState 1) and a decoded frame. Input sent while the session is
 * still starting is refused.
 */
async function openPage(session, canvas) {
  await brow(session, ["open", canvas.url]);
  return await waitFor(async () => {
    const value = await stats(session);
    return value && value.session === "streaming" && value.control === 1 && value.framesDecoded > 0
      ? value : null;
  }, 20_000, `a streaming session with control and a decoded frame in brow session ${session}`);
}

let lastNudgeAt = 0;

/**
 * Make the device send video whatever is on screen: a key frame request, which scrcpy
 * answers with a new config and key frame for every client. Unlike a scroll, it also
 * works on a screen that cannot scroll. Requests closer than a second apart would merge.
 */
async function nudge(session) {
  const wait = lastNudgeAt + NUDGE_SPACING_MS - Date.now();
  if (wait > 0) await sleep(wait);
  lastNudgeAt = Date.now();
  return await send(session, [{ t: "system", op: "keyframe" }]);
}

function median(values) {
  const sorted = [...values].filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function percentile(values, fraction) {
  const sorted = [...values].filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
}

/**
 * CPU seconds of the Canvas with every process below it (adb clients, ffmpeg), and
 * of the adb server, which carries both transports' device traffic but is no child.
 */
async function cpuSeconds(pid) {
  const { stdout } = await run("ps", ["-A", "-o", "pid=,ppid=,time=,command="]);
  const rows = stdout.trim().split("\n").map((line) => line.trim().split(/\s+/));
  const children = new Map();
  const time = new Map();
  const adbServers = [];
  for (const [child, parent, cpu, ...command] of rows) {
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(child);
    time.set(child, cpu);
    const line = command.join(" ");
    if (/(^|\/)adb\b/.test(command[0] ?? "") && line.includes("fork-server")) adbServers.push(child);
  }
  const parse = (text) => {
    const [days, clock] = text.includes("-") ? text.split("-") : ["0", text];
    return clock.split(":").reverse().reduce((sum, part, index) => sum + Number(part) * 60 ** index, 0) +
      Number(days) * 86_400;
  };
  let canvas = 0;
  const queue = [String(pid)];
  while (queue.length) {
    const current = queue.shift();
    if (time.has(current)) canvas += parse(time.get(current));
    queue.push(...(children.get(current) ?? []));
  }
  const adbServer = adbServers.reduce((sum, id) => sum + parse(time.get(id)), 0);
  return { canvas, adbServer, total: canvas + adbServer };
}

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

/** How the bench browser runs, for the report. */
const browserMode = {
  headless: args.headless,
  args: args.headless ? [] : [OFF_SCREEN_WINDOW, "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling"],
};

/** Busy share of all host CPUs between two os.cpus() readings, in percent. */
function cpuBusy(before, after) {
  let busy = 0;
  let total = 0;
  after.forEach((cpu, index) => {
    const was = before[index]?.times;
    if (!was) return;
    const spent = Object.keys(cpu.times).reduce((sum, key) => sum + cpu.times[key] - was[key], 0);
    total += spent;
    busy += spent - (cpu.times.idle - was.idle);
  });
  return total ? Math.round((busy / total) * 1000) / 10 : null;
}

function hostLoad() {
  return loadavg().map((value) => Math.round(value * 100) / 100);
}

/** Open the Canvas in a Playwright page and wait for a streaming scrcpy session with control. */
async function openBenchPage(browser, canvas) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  await page.goto(canvas.url);
  await page.waitForFunction(() => {
    const value = window.autonomCanvas?.stats();
    return value && value.transport === "scrcpy" && value.decoder === "webcodecs" && value.session === "streaming" &&
      value.control === 1 && value.framesDecoded > 0;
  }, null, { timeout: 30_000, polling: 50 });
  return page;
}

/**
 * The fps window, measured in the page. A finger driven from a BENCH_STEP_MS timer drags
 * the list up and down without pause: each stroke is BENCH_MOVES moves, and the next one
 * goes down in the same tick as the last one goes up. Once the picture first moves (and
 * 300 ms more), the page's draw() is wrapped for `durationMs`: each presented frame is
 * timed and compared with the one before (distinct frames), animation frames are counted,
 * and the Canvas server's scrcpy packet count (the frames the device produced) is read
 * from /status once a second.
 */
async function measureFps(page, token, durationMs) {
  return await page.evaluate(async (o) => {
    const canvas = window.autonomCanvas;
    const video = document.getElementById("video");
    const copy = document.createElement("canvas");
    copy.width = o.sample;
    copy.height = o.sample;
    const context = copy.getContext("2d", { willReadFrequently: true });
    const take = () => {
      const top = Math.round(video.height * o.band[0]);
      context.drawImage(video, 0, top, video.width, Math.round(video.height * o.band[1]) - top, 0, 0, o.sample, o.sample);
      const data = context.getImageData(0, 0, o.sample, o.sample).data;
      const luma = new Uint8Array(o.sample * o.sample);
      for (let i = 0, p = 0; p < luma.length; i += 4, p += 1) {
        luma[p] = Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]);
      }
      return luma;
    };
    const changed = (a, b) => {
      let moved = 0;
      for (let p = 0; p < a.length; p += 1) if (Math.abs(a[p] - b[p]) > o.level) moved += 1;
      return moved >= o.pixels;
    };
    const wait = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
    const serverPackets = async () => {
      const asked = performance.now();
      const response = await fetch("/status", { headers: { Authorization: "Bearer " + o.token } });
      const body = await response.json();
      return { at: (asked + performance.now()) / 2, packets: body.scrcpy ? body.scrcpy.packets : null };
    };

    // The finger: never tied to animation frames.
    let measuring = false;
    let moves = 0;
    let strokes = 0;
    let stroke = null;
    let stopping = false;
    let finished;
    const done = new Promise((resolvePromise) => { finished = resolvePromise; });
    const begin = (from, to) => {
      stroke = { from, to, step: 0 };
      strokes += 1;
      canvas.send({ t: "touch", a: "down", id: 1, x: 0.5, y: from });
    };
    begin(o.from, o.to);
    const timer = setInterval(() => {
      stroke.step += 1;
      const y = stroke.from + (stroke.to - stroke.from) * stroke.step / o.moves;
      canvas.send({ t: "touch", a: "move", id: 1, x: 0.5, y });
      if (measuring) moves += 1;
      if (stroke.step < o.moves) return;
      canvas.send({ t: "touch", a: "up", id: 1, x: 0.5, y });
      if (stopping) {
        clearInterval(timer);
        finished();
        return;
      }
      begin(stroke.to, stroke.from);
    }, o.stepMs);

    // Until the list first moves (at most 3 s), then 300 ms more.
    const still = take();
    const waitedFrom = performance.now();
    while (performance.now() - waitedFrom < 3000 && !changed(take(), still)) await wait(16);
    await wait(300);

    const times = [];
    const distinct = [];
    let ticks = 0;
    let previous = take();
    // The page script's draw(), a global function scheduled by name, wrapped for the window.
    const pageDraw = window.draw;
    window.draw = () => {
      const rendered = canvas.stats().framesRendered;
      pageDraw();
      if (!measuring || canvas.stats().framesRendered === rendered) return;
      const now = performance.now();
      times.push(now);
      const current = take();
      if (changed(current, previous)) distinct.push(now);
      previous = current;
    };
    const device = [await serverPackets()];
    const before = canvas.stats();
    const started = performance.now();
    measuring = true;
    const polling = (async () => {
      for (let second = 1; second * 1000 <= o.durationMs; second += 1) {
        await wait(Math.max(0, started + second * 1000 - performance.now()));
        device.push(await serverPackets());
      }
    })();
    await new Promise((resolvePromise) => {
      const tick = () => {
        ticks += 1;
        if (performance.now() - started < o.durationMs) requestAnimationFrame(tick);
        else resolvePromise();
      };
      requestAnimationFrame(tick);
    });
    const ended = performance.now();
    measuring = false;
    const after = canvas.stats();
    window.draw = pageDraw;
    await polling;
    stopping = true;
    await done;
    return {
      times, distinct, ticks, started, ended, device, moves, strokes,
      decoded: after.framesDecoded - before.framesDecoded,
      dropped: after.framesDropped - before.framesDropped,
      rendered: after.framesRendered - before.framesRendered,
      received: after.packets - before.packets,
      size: { width: after.width, height: after.height },
    };
  }, { token, durationMs, sample: DISTINCT_SAMPLE, band: DISTINCT_BAND, level: DISTINCT_LEVEL,
    pixels: DISTINCT_PIXELS, from: BENCH_FROM, to: BENCH_TO, moves: BENCH_MOVES, stepMs: BENCH_STEP_MS });
}

/**
 * Input-to-picture latency, measured in the page: `drags` drags, each from a settled
 * screen, its moves sent from a BENCH_STEP_MS timer. A drag's latency is the time from its
 * touch down to the first presented frame whose picture differs from the settled one; null
 * when none did within LATENCY_TAIL_MS of its last move.
 */
async function measureLatency(page, drags) {
  return await page.evaluate(async (o) => {
    const canvas = window.autonomCanvas;
    const wait = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
    const frame = () => new Promise((resolvePromise) => requestAnimationFrame(resolvePromise));
    const element = document.getElementById("video");
    const probe = document.createElement("canvas");
    probe.width = o.probe.width;
    probe.height = o.probe.height;
    const probeContext = probe.getContext("2d", { willReadFrequently: true });
    const grab = () => {
      probeContext.drawImage(element, 0, 0, probe.width, probe.height);
      return probeContext.getImageData(0, 0, probe.width, probe.height).data;
    };
    const difference = (a, b) => {
      let sum = 0;
      for (let i = 0; i < a.length; i += 4) sum += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]);
      return sum / (a.length / 4) / 3;
    };
    const latencies = [];
    const pageDraw = window.draw;
    try {
      for (let i = 0; i < o.drags; i += 1) {
        const up = i % 2 === 0;
        const [from, to] = up ? [o.from, o.to] : [o.to, o.from];
        await wait(o.settleMs);
        for (let k = 0; k < 3; k += 1) await frame();
        const baseline = grab();
        let latency = null;
        let started = 0;
        window.draw = () => {
          const rendered = canvas.stats().framesRendered;
          pageDraw();
          if (latency !== null || !started || canvas.stats().framesRendered === rendered) return;
          if (difference(grab(), baseline) > o.threshold) latency = performance.now() - started;
        };
        started = performance.now();
        canvas.send({ t: "touch", a: "down", id: 1, x: 0.5, y: from });
        await new Promise((resolvePromise) => {
          let step = 0;
          const timer = setInterval(() => {
            step += 1;
            const y = from + (to - from) * step / o.moves;
            canvas.send({ t: "touch", a: "move", id: 1, x: 0.5, y });
            if (step < o.moves) return;
            clearInterval(timer);
            canvas.send({ t: "touch", a: "up", id: 1, x: 0.5, y });
            resolvePromise();
          }, o.stepMs);
        });
        await wait(o.tailMs);
        window.draw = pageDraw;
        latencies.push(latency === null ? null : Math.round(latency));
      }
    } finally {
      window.draw = pageDraw;
    }
    return { latencies };
  }, { drags, probe: PROBE_SIZE, threshold: CHANGE_THRESHOLD, moves: BENCH_MOVES, stepMs: BENCH_STEP_MS,
    from: BENCH_FROM, to: BENCH_TO, settleMs: LATENCY_SETTLE_MS, tailMs: LATENCY_TAIL_MS });
}

function round(value, digits = 1) {
  if (value === null || !Number.isFinite(value)) return null;
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

/** Counts per whole second of the window, their median, the mean rate and interval percentiles. */
function frameReport(times, { started, ended }) {
  const perSecond = [];
  for (let second = 0; started + (second + 1) * 1000 <= ended; second += 1) {
    const from = started + second * 1000;
    perSecond.push(times.filter((time) => time >= from && time < from + 1000).length);
  }
  const intervals = times.slice(1).map((time, index) => time - times[index]);
  return {
    frames: times.length,
    fps_mean: round(times.length / ((ended - started) / 1000)),
    fps_per_second: perSecond,
    fps_median: median(perSecond),
    fps_min: perSecond.length ? Math.min(...perSecond) : null,
    interval_ms: {
      p50: round(percentile(intervals, 0.5), 2), p90: round(percentile(intervals, 0.9), 2),
      p95: round(percentile(intervals, 0.95), 2), p99: round(percentile(intervals, 0.99), 2),
      max: intervals.length ? round(Math.max(...intervals), 2) : null,
    },
  };
}

/** Frames the device produced per second, from the server's packet count read once a second. */
function deviceReport(samples) {
  const known = samples.filter((sample) => Number.isInteger(sample.packets));
  const perSecond = known.slice(1).map((sample, index) =>
    round((sample.packets - known[index].packets) / ((sample.at - known[index].at) / 1000)));
  const first = known[0];
  const last = known.at(-1);
  return {
    frames: known.length > 1 ? last.packets - first.packets : null,
    fps_mean: known.length > 1 ? round((last.packets - first.packets) / ((last.at - first.at) / 1000)) : null,
    fps_per_second: perSecond,
    fps_median: median(perSecond),
  };
}

/** The fps window measured by measureFps, with the host's CPU load over it. */
function fpsReport(measured, cpuBusyPct) {
  const seconds = (measured.ended - measured.started) / 1000;
  const rate = (count) => round(count / seconds);
  return {
    seconds: round(seconds, 2),
    video: measured.size,
    strokes: measured.strokes,
    moves_fps: rate(measured.moves),
    device: deviceReport(measured.device),
    received_fps: rate(measured.received),
    decoded_fps: rate(measured.decoded),
    dropped_fps: rate(measured.dropped),
    dropped: measured.dropped,
    presented: frameReport(measured.times, measured),
    distinct: frameReport(measured.distinct, measured),
    animation_frames_fps: rate(measured.ticks),
    host_cpu_busy_pct: cpuBusyPct,
  };
}

/** Runs land in `into.runs` as they finish, so a failing run leaves the earlier ones in the report. */
async function benchScrcpy(browser, repeat, drags, seconds, into) {
  const runs = (into.runs = []);
  for (let attempt = 0; attempt < repeat; attempt += 1) {
    const canvas = await startCanvas("scrcpy");
    let page = null;
    try {
      page = await openBenchPage(browser, canvas);
      await resetSettings();
      const item = { loadavg_before: hostLoad() };
      runs.push(item);
      const cpuBefore = await cpuSeconds(canvas.child.pid);
      const framesBefore = (await page.evaluate(() => window.autonomCanvas.stats())).framesRendered;
      const hostBefore = cpus();
      const measured = await measureFps(page, canvas.token, seconds * 1000);
      item.fps = fpsReport(measured, cpuBusy(hostBefore, cpus()));
      item.loadavg_after_fps = hostLoad();
      // The list is back near its top between the two parts: Settings opened afresh.
      await resetSettings();
      const { latencies } = await measureLatency(page, drags);
      const cpuAfter = await cpuSeconds(canvas.child.pid);
      const framesAfter = (await page.evaluate(() => window.autonomCanvas.stats())).framesRendered;
      const frames = Math.max(1, framesAfter - framesBefore);
      const perFrame = (key) => round(((cpuAfter[key] - cpuBefore[key]) * 1000) / frames, 2);
      Object.assign(item, {
        presented_fps_median: item.fps.presented.fps_median,
        distinct_fps_median: item.fps.distinct.fps_median,
        device_fps_median: item.fps.device.fps_median,
        decoded_fps: item.fps.decoded_fps,
        latency_median_ms: median(latencies),
        latency_p95_ms: percentile(latencies, 0.95),
        latency_missed: latencies.filter((value) => value === null).length,
        latencies,
        cpu_ms_per_frame: perFrame("total"),
        cpu_canvas_ms_per_frame: perFrame("canvas"),
        cpu_adb_server_ms_per_frame: perFrame("adbServer"),
        frames,
        loadavg_after: hostLoad(),
      });
    } finally {
      await page?.close().catch(() => {});
      await canvas.stop();
    }
  }
  const middle = (key) => median(runs.map((item) => item[key]));
  return Object.assign(into, {
    presented_fps_median: middle("presented_fps_median"),
    distinct_fps_median: middle("distinct_fps_median"),
    device_fps_median: middle("device_fps_median"),
    decoded_fps_median: middle("decoded_fps"),
    latency_median_ms: middle("latency_median_ms"),
    latency_p95_ms: middle("latency_p95_ms"),
    cpu_ms_per_frame: middle("cpu_ms_per_frame"),
  });
}

// Each case writes what it learns into `report` as it goes and sets report.ok last.
const cases = {
  async picture(report) {
    // auto, as `autonom canvas serve` runs by default; without --scrcpy-server it must
    // find the server of an installed scrcpy by itself.
    const canvas = await startCanvas("auto", {}, { unsetEnv: serverPath ? [] : SERVER_VARIABLES });
    try {
      await shell("am start -W -a android.settings.SETTINGS");
      report.first = await openPage(args.session, canvas);
      await nudge(args.session);
      report.screenshot = join(outDir, "picture-brow.png");
      await brow(args.session, ["screenshot", "-o", report.screenshot]);
      report.device_screencap = await saveScreencap("picture-device.png");
      const final = (report.stats = await stats(args.session));
      report.status = await canvas.status();
      report.ok = final.decoder === "webcodecs" && final.framesRendered > 0 &&
        final.status.includes("transport: scrcpy (webcodecs)") && report.status.transport === "scrcpy" &&
        (Boolean(serverPath) || discovery?.source === "scrcpy");
    } finally {
      await brow(args.session, ["close"]).catch(() => {});
      await canvas.stop();
    }
  },

  async tabs(report) {
    const canvas = await startCanvas("scrcpy");
    const second = `${args.session}2`;
    try {
      await openPage(args.session, canvas);
      await sleep(5000);
      await openPage(second, canvas);
      await resetSettings();
      const before = (report.before = [await stats(args.session), await stats(second)]);
      for (let i = 0; i < 4; i += 1) await nudge(args.session);
      await sleep(1000);
      const after = (report.after = [await stats(args.session), await stats(second)]);
      // A background headless tab may never run requestAnimationFrame, so it can decode
      // without rendering: receiving video is packets and decoded frames growing.
      const received = (report.received = after.map((value, index) => ({
        packets: value.packets > before[index].packets,
        framesDecoded: value.framesDecoded > before[index].framesDecoded,
        framesRendered: value.framesRendered > before[index].framesRendered,
      })));
      const servers = (report.servers = await serverProcesses());
      report.status = await canvas.status();
      report.ok = servers.length === 1 && received[0].framesRendered &&
        received.every((value) => value.packets && value.framesDecoded);
    } finally {
      await brow(second, ["close"]).catch(() => {});
      await brow(args.session, ["close"]).catch(() => {});
      await canvas.stop();
    }
  },

  async restart(report) {
    const canvas = await startCanvas("scrcpy");
    try {
      await openPage(args.session, canvas);
      await resetSettings();
      // A finger held when the server dies must not stay down on Android.
      await holdFinger(args.session, 5);
      report.held_before = (await touchState()).touched === true;
      const [line] = await serverProcesses();
      if (!line) throw new Error("no scrcpy server process on the device");
      const killedPid = (report.killed_pid = line.trim().split(/\s+/)[0]);
      const killedAt = Date.now();
      await shell(`kill ${killedPid}`);
      // The killed server is really gone...
      report.killed_gone = await waitFor(async () =>
        (await shell(`[ -d /proc/${killedPid} ] && echo alive || echo gone`)) === "gone",
      5000, "the killed server to exit");
      // ...the Canvas restarted it and streams again...
      report.restarted = await waitFor(async () => {
        const { scrcpy } = await canvas.status();
        return scrcpy && scrcpy.restarts >= 1 && scrcpy.session_state === "streaming"
          ? { restarts: scrcpy.restarts, seconds: (Date.now() - killedAt) / 1000 } : null;
      }, 15_000, "/status to show a restarted streaming session");
      // ...and the page gets frames from the restarted server.
      const framesAtRestart = (await stats(args.session)).framesRendered;
      report.resumed = await waitFor(async () => {
        await nudge(args.session).catch(() => {});
        const value = await stats(args.session);
        return value.session === "streaming" && value.framesRendered > framesAtRestart + 2
          ? { ...value, seconds: (Date.now() - killedAt) / 1000 } : null;
      }, 15_000, "video from the restarted server");
      report.held_finger_cleared = await touchCleared();
      report.touch_state_after = (await touchState()).text;
      // The Canvas lifted that finger already; its up changes nothing.
      await send(args.session, [{ t: "touch", a: "up", id: 5, x: 0.5, y: 0.5 }]);
    } finally {
      await brow(args.session, ["close"]).catch(() => {});
      report.canvas_exit = await canvas.stop();
    }
    // cleanup=true removes the jar a moment after the server ends.
    await sleep(2000);
    const forwards = (report.forwards = (await adb(["forward", "--list"])).stdout.split("\n")
      .filter((line) => line.includes(serial) && line.includes("localabstract:scrcpy_")));
    const servers = (report.servers = await serverProcesses());
    const jar = (report.jar = await shell(`ls ${REMOTE_JAR} 2>/dev/null || echo missing`));
    report.ok = report.killed_gone === true && report.restarted.restarts >= 1 && report.resumed.seconds <= 15 &&
      report.held_before && report.held_finger_cleared && !forwards.length && !servers.length && jar === "missing";
  },

  async input(report) {
    const canvas = await startCanvas("scrcpy");
    const steps = (report.steps = {});
    try {
      await openPage(args.session, canvas);
      await resetSettings();

      let before = await screenLayout();
      await send(args.session, [{ t: "touch", a: "down", id: 1, x: 0.5, y: 0.75 }]);
      await sendPaced(args.session,
        Array.from({ length: 20 }, (_, i) => ({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.75 - i * 0.02 })));
      const held = await screenLayout();
      await send(args.session, [{ t: "touch", a: "up", id: 1, x: 0.5, y: 0.35 }]);
      steps.drag_hold = { moved_before_release: layoutChanged(before, held), ...layoutSizes(before, held) };

      before = await screenLayout();
      await send(args.session, Array.from({ length: 5 }, () => ({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -2 })));
      await sleep(800);
      const scrolled = await screenLayout();
      steps.wheel = { scrolled: layoutChanged(before, scrolled), ...layoutSizes(before, scrolled) };

      await resetSettings();
      const field = await findNode(/search/i);
      if (field) {
        const display = (await canvas.status()).display;
        await send(args.session, [{ t: "touch", a: "down", id: 2, x: field.x / display.width, y: field.y / display.height },
          { t: "touch", a: "up", id: 2, x: field.x / display.width, y: field.y / display.height }]);
        await sleep(1500);
      }
      const keys = [[51, "w"], [37, "i"], [34, "f"], [37, "i"]];
      await send(args.session, keys.flatMap(([code]) => [{ t: "key", a: "down", code, meta: 0, repeat: 0 },
        { t: "key", a: "up", code, meta: 0, repeat: 0 }]));
      await send(args.session, [{ t: "text", text: " Café ✓ naïve" }, { t: "paste", text: " π ≈ 3.14 ✓" }]);
      await sleep(1500);
      const typed = await focusedText();
      steps.text = { field_found: Boolean(field), typed, expected: TYPED_TEXT, ok: typed === TYPED_TEXT };
      steps.keyboard = await keyboardStep();
      await send(args.session, [{ t: "system", op: "back" }, { t: "system", op: "back" }]);

      try {
        await rotateStep((steps.rotate = {}));
      } finally {
        // Later steps start from the launcher, whatever the rotate step left in front.
        await shell("input keyevent KEYCODE_HOME");
      }

      await canvas.control("takeover", "agent");
      await sleep(300);
      before = await screenLayout();
      await send(args.session, [{ t: "touch", a: "down", id: 3, x: 0.5, y: 0.75 }]);
      await sendPaced(args.session,
        Array.from({ length: 20 }, (_, i) => ({ t: "touch", a: "move", id: 3, x: 0.5, y: 0.75 - (i + 1) * 0.0175 })));
      await send(args.session, [{ t: "touch", a: "up", id: 3, x: 0.5, y: 0.4 }]);
      await sleep(800);
      const blocked = await stats(args.session);
      const afterBlocked = await screenLayout();
      steps.handoff = {
        unchanged: layoutSame(before, afterBlocked),
        ...layoutSizes(before, afterBlocked),
        owner: blocked.owner,
        note: blocked.note,
      };
      await canvas.control("release", "agent");
      report.status = await canvas.status();
      report.ok = steps.drag_hold.moved_before_release && steps.wheel.scrolled && steps.text.ok &&
        steps.keyboard.ok && !steps.rotate.skipped && steps.rotate.touch_held_before &&
        steps.rotate.touch_cleared && steps.rotate.landscape_input?.ok === true &&
        steps.handoff.unchanged && steps.handoff.owner === "agent" &&
        /owned by agent/.test(steps.handoff.note ?? "");
      if (steps.rotate.skipped) report.error = `rotate: ${steps.rotate.skipped}`;
    } finally {
      await brow(args.session, ["close"]).catch(() => {});
      await canvas.stop();
    }
  },

  async journal(report) {
    // A session of this run's own, in its own AUTONOM_HOME, on the Canvas serial.
    const target = ["--serial", serial, ...(args.adb ? ["--adb", args.adb] : [])];
    const started = await autonom(["session", "start", ...target]);
    if (started.code !== 0) throw new Error(`autonom session start failed: ${started.stderr || started.stdout}`);
    try {
      report.session_id = JSON.parse(started.stdout).session?.session_id ?? null;
      const canvas = await startCanvas("scrcpy");
      try {
        await shell("am start -W -a android.settings.SETTINGS");
        await openPage(args.session, canvas);
        await send(args.session, [{ t: "touch", a: "down", id: 1, x: 0.5, y: 0.7 },
          ...Array.from({ length: JOURNAL_MOVES }, (_, i) => ({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.7 - i * 0.005 })),
          { t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 }]);
        await send(args.session, Array.from({ length: 5 }, () => ({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 })));
        await sleep(600);
        await send(args.session, [{ t: "key", a: "down", code: 20, meta: 0, repeat: 0 },
          { t: "key", a: "up", code: 20, meta: 0, repeat: 0 }, { t: "text", text: "abc" },
          { t: "paste", text: "live-clipboard-secret" }, { t: "system", op: "home" }]);
        await sleep(1500);
        const listed = await autonom(["journal", "--max", "200"]);
        const journal = JSON.parse(listed.stdout || "{}");
        const entries = journal.entries ?? [];
        const verbs = (report.verbs = {});
        for (const entry of entries) verbs[entry.verb] = (verbs[entry.verb] ?? 0) + 1;
        report.journal = journal.journal;
        const text = JSON.stringify(entries);
        // Each Canvas entry is the human's, came over scrcpy, and has its action detail.
        const canvasEntries = entries.filter((entry) => entry.verb?.startsWith("ui "));
        const details = (report.details = await Promise.all(canvasEntries.map(async (entry) =>
          entry.result?.detail ? readJson(join(dirname(journal.journal), entry.result.detail)) : null)));
        const gesture = details.find((detail) => detail?.kind === "gesture");
        report.checks = {
          origin_human: canvasEntries.length > 0 && canvasEntries.every((entry) => entry.origin === "human"),
          via_scrcpy: canvasEntries.every((entry) => entry.result?.via === "scrcpy"),
          detail_transport_scrcpy: details.every((detail) => detail?.transport === "scrcpy" && detail.origin === "human"),
          gesture_moves: gesture?.moves ?? null,
        };
        report.ok = verbs["ui gesture"] === 1 && verbs["ui scroll"] === 1 && verbs["ui key"] === 1 &&
          verbs["ui text"] === 1 && verbs["ui paste"] === 1 && verbs["ui system"] === 1 &&
          !(text + JSON.stringify(details)).includes("live-clipboard-secret") && report.checks.origin_human &&
          report.checks.via_scrcpy &&
          report.checks.detail_transport_scrcpy && report.checks.gesture_moves === JOURNAL_MOVES;
      } finally {
        await brow(args.session, ["close"]).catch(() => {});
        await canvas.stop();
      }
    } finally {
      const stopped = await autonom(["session", "stop", ...target]);
      report.session_stopped = stopped.code === 0;
    }
  },

  /**
   * VER-005: every preset through the page's Size picker, one through POST /display, a
   * refused request during an agent takeover, the restore at SIGTERM, and a second Canvas
   * that finds an override already set and puts it back. The device server must keep
   * running throughout (DISPLAY-002). The device's own values are read first and put back
   * in the finally block whatever happens.
   */
  async display(report) {
    const session = `${args.session}-display`;
    const original = (report.original = await readDisplay());
    if (!original) throw new Error("wm did not report the display size and density");
    const steps = (report.steps = {});
    try {
      const canvas = await startCanvas("scrcpy");
      try {
        await openPage(session, canvas);
        const presets = (steps.presets = []);
        for (const preset of DISPLAY_PRESETS) {
          const step = { preset: preset.id };
          presets.push(step);
          await displayThroughPicker(session, preset, original, step);
        }
        const http = await canvas.display("pixel-11");
        const afterHttp = await readDisplay();
        steps.http = {
          status: http.status, body: http.body, wm: briefDisplay(afterHttp),
          ok: http.status === 200 && http.body.display?.preset === "pixel-11" &&
            showsValues(afterHttp, DISPLAY_PRESETS[1]),
        };
        steps.refused = await refusedDisplayStep(session, canvas);
        const status = await canvas.status();
        steps.status = status.display;
        steps.server_restarts = status.scrcpy?.restarts ?? null;
      } finally {
        await brow(session, ["close"]).catch(() => {});
        const exit = await canvas.stop();
        const stopped = await readDisplay();
        steps.stop = {
          exit, line: canvas.output().match(/Display restore[^\n]*/)?.[0] ?? null, wm: briefDisplay(stopped),
          ok: exit === 0 && sameOverrides(stopped, original),
        };
      }
      steps.preexisting = await preexistingDisplayStep(original);
      report.ok = steps.presets.length === DISPLAY_PRESETS.length && steps.presets.every((step) => step.ok) &&
        steps.http.ok && steps.refused.ok && steps.server_restarts === 0 && steps.stop.ok &&
        steps.preexisting.ok;
    } finally {
      const restored = await putDisplayBack(original);
      report.restored = { wm: briefDisplay(restored), ok: sameOverrides(restored, original) };
      if (!report.restored.ok) report.ok = false;
    }
  },

  async bench(report) {
    const repeat = (report.repeat = Number(args.repeat));
    const drags = (report.drags = Number(args.drags));
    const seconds = (report.seconds_per_window = Number(args.seconds));
    report.host = { cpus: cpus().length, loadavg_start: hostLoad() };
    report.browser = { ...browserMode };
    const touches = await shell("settings get system show_touches");
    await shell("settings put system show_touches 1");
    let browser = null;
    try {
      const { chromium } = await loadPlaywright();
      browser = await chromium.launch({ headless: browserMode.headless, args: browserMode.args });
      report.browser.version = browser.version();
      const scrcpy = await benchScrcpy(browser, repeat, drags, seconds, (report.scrcpy = {}));
      report.bars = {
        presented_median_at_least_55: scrcpy.presented_fps_median !== null && scrcpy.presented_fps_median >= PRESENTED_BAR,
        distinct_median_at_least_45: scrcpy.distinct_fps_median !== null && scrcpy.distinct_fps_median >= DISTINCT_BAR,
      };
      // Reported against its bar; it does not decide the case.
      report.latency = {
        median_ms: scrcpy.latency_median_ms,
        p95_ms: scrcpy.latency_p95_ms,
        at_most_150ms: scrcpy.latency_median_ms !== null && scrcpy.latency_median_ms <= LATENCY_BAR_MS,
      };
      report.ok = Object.values(report.bars).every(Boolean);
    } finally {
      await browser?.close().catch(() => {});
      await restoreSetting("show_touches", touches);
      report.show_touches_restored = (await shell("settings get system show_touches")) === touches;
      report.host.loadavg_end = hostLoad();
      if (!report.show_touches_restored) report.ok = false;
    }
  },
};

/**
 * Rotate through scrcpy and back, filling `step` as it goes. A portrait-locked activity
 * (the Settings homepage) never rotates, so a browser page is brought to the front first;
 * without one the step is skipped and says why. scrcpy 4.1 rotateDevice turns sensor
 * rotation back on when auto-rotate was on, and the emulator's sensor then puts the
 * screen back in portrait, so auto-rotate is off for the step; both rotation settings are
 * put back after it.
 */
async function rotateStep(step) {
  const installed = await adb(["shell", `pm path ${ROTATABLE_PACKAGE}`]);
  if (installed.code !== 0 || !installed.stdout.includes("package:")) {
    step.skipped = "no rotatable app in front";
    return;
  }
  await shell(`am start -W -a android.intent.action.VIEW -d about:blank -n ${ROTATABLE_ACTIVITY}`);
  await sleep(2000);
  step.app = ROTATABLE_PACKAGE;
  const rotation = {
    accelerometer: await shell("settings get system accelerometer_rotation"),
    user: await shell("settings get system user_rotation"),
  };
  const portrait = await stats(args.session);
  const landscape = (value) => value.width > value.height;
  Object.assign(step, { auto_rotate: rotation.accelerometer, before: [portrait.width, portrait.height] });
  try {
    await shell("settings put system accelerometer_rotation 0");
    // A finger held through the rotation must not stay down on Android.
    await holdFinger(args.session, 7);
    step.touch_held_before = (await touchState()).touched === true;
    await send(args.session, [{ t: "system", op: "rotate" }]);
    const rotated = await waitFor(async () => {
      const value = await stats(args.session);
      return value.width && landscape(value) !== landscape(portrait) ? value : null;
    }, 10_000, "the rotated video");
    step.after = [rotated.width, rotated.height];
    step.touch_cleared = await touchCleared();
    step.touch_state_after = (await touchState()).text;
    // The Canvas lifted that finger already; its up changes nothing.
    await send(args.session, [{ t: "touch", a: "up", id: 7, x: 0.5, y: 0.6 }]);
    step.landscape_input = await landscapeTap();
    await send(args.session, [{ t: "system", op: "rotate" }]);
    const back = await waitFor(async () => {
      const value = await stats(args.session);
      return value.width && landscape(value) === landscape(portrait) ? value : null;
    }, 10_000, "the video rotated back");
    step.back = [back.width, back.height];
  } finally {
    await restoreSetting("accelerometer_rotation", rotation.accelerometer);
    await restoreSetting("user_rotation", rotation.user);
  }
}

/**
 * The serial's `wm size` and `wm density`: the overrides (null for none), the physical
 * values and those in effect, with the raw text. Null when wm reports no physical display.
 */
async function readDisplay() {
  const sizeText = await shell("wm size");
  const densityText = await shell("wm density");
  const physical = sizeText.match(/Physical size: (\d+)x(\d+)/);
  const override = sizeText.match(/Override size: (\d+)x(\d+)/);
  const physicalDensity = densityText.match(/Physical density: (\d+)/);
  const overrideDensity = densityText.match(/Override density: (\d+)/);
  if (!physical || !physicalDensity) return null;
  const size = override ?? physical;
  return {
    text: { size: sizeText, density: densityText },
    size_override: override ? `${override[1]}x${override[2]}` : null,
    density_override: overrideDensity ? Number(overrideDensity[1]) : null,
    physical: { width: Number(physical[1]), height: Number(physical[2]), density: Number(physicalDensity[1]) },
    width: Number(size[1]),
    height: Number(size[2]),
    density: Number((overrideDensity ?? physicalDensity)[1]),
  };
}

function briefDisplay(wm) {
  return wm && { width: wm.width, height: wm.height, density: wm.density,
    size_override: wm.size_override, density_override: wm.density_override };
}

/** Whether the values in effect are `expected`'s width, height and density. */
function showsValues(wm, expected) {
  return Boolean(wm) && wm.width === expected.width && wm.height === expected.height &&
    wm.density === expected.density;
}

function sameOverrides(wm, original) {
  return Boolean(wm) && wm.size_override === original.size_override &&
    wm.density_override === original.density_override;
}

/** Put the display back as `original` had it: its overrides, or none. Never throws. */
async function putDisplayBack(original) {
  if (!original) return null;
  try {
    await shell(original.size_override ? `wm size ${original.size_override}` : "wm size reset");
    await shell(original.density_override ? `wm density ${original.density_override}` : "wm density reset");
    return await readDisplay();
  } catch {
    return null;
  }
}

/**
 * Choose `preset` in the page's Size picker as a person would (a select, or a menu button
 * whose options carry data-preset), then check the picker, the device's read-back and the
 * page's video aspect, and keep a screenshot.
 */
async function displayThroughPicker(session, preset, original, step) {
  const expected = preset.width ? preset : original.physical;
  const before = await stats(session);
  step.choose = await pageJson(session, `(() => {
    const id = ${JSON.stringify(preset.id)};
    const picker = document.getElementById("display-preset");
    if (!picker || picker.disabled) return JSON.stringify({ chosen: false, disabled: picker ? picker.disabled : null });
    if (picker.tagName === "SELECT") {
      picker.value = id;
      picker.dispatchEvent(new Event("change", { bubbles: true }));
      return JSON.stringify({ chosen: picker.value === id, via: "select" });
    }
    picker.click();
    const option = [...document.querySelectorAll("[data-preset]")].find((item) => item.dataset.preset === id);
    if (!option) return JSON.stringify({ chosen: false, via: "menu", missing: id });
    option.click();
    return JSON.stringify({ chosen: true, via: "menu" });
  })()`);
  const shown = await waitFor(async () => {
    const value = await stats(session);
    return value.display?.preset === preset.id && value.display.picker === preset.id &&
      value.display.pickerDisabled === false ? value : null;
  }, DISPLAY_CHANGE_MS, `the page to show ${preset.id} selected`);
  step.picker = shown.display;
  const wm = await readDisplay();
  step.wm = briefDisplay(wm);
  step.wm_text = wm?.text ?? null;
  const aspect = expected.width / expected.height;
  const video = await waitFor(async () => {
    const value = await stats(session);
    return value.width && value.height &&
      Math.abs(value.width / value.height - aspect) <= aspect * ASPECT_TOLERANCE ? value : null;
  }, DISPLAY_CHANGE_MS, `the page video at the ${preset.id} aspect`).catch(() => null);
  step.video = video ? [video.width, video.height] : null;
  step.video_before = [before.width, before.height];
  // A settled frame at the new size before the screenshot: the size has been in effect for
  // DISPLAY_SETTLE_MS, and DISPLAY_SETTLE_FRAMES frames were drawn since (each nudge asks
  // the device for a key frame, as a still screen sends none).
  const sizedAt = Date.now();
  const rendered = video?.framesRendered ?? 0;
  await sleep(DISPLAY_SETTLE_MS);
  const settled = await waitFor(async () => {
    const value = await stats(session);
    if (value.framesRendered - rendered >= DISPLAY_SETTLE_FRAMES) return value;
    await nudge(session).catch(() => {});
    return null;
  }, DISPLAY_SETTLE_WAIT_MS, `${DISPLAY_SETTLE_FRAMES} frames at the ${preset.id} size`).catch(() => null);
  step.settle = { ms: Date.now() - sizedAt, frames: settled ? settled.framesRendered - rendered : null };
  step.screenshot = join(outDir, `display-${preset.id}.png`);
  await brow(session, ["screenshot", "-o", step.screenshot]);
  const noOverride = !wm?.size_override && !wm?.density_override;
  step.ok = step.choose.chosen === true && showsValues(wm, expected) && Boolean(video) &&
    (preset.width ? true : noOverride);
}

/** During an agent takeover the page's request changes nothing and is refused. */
async function refusedDisplayStep(session, canvas) {
  const step = {};
  await canvas.control("takeover", "agent");
  try {
    const owned = await waitFor(async () => {
      const value = await stats(session);
      return value.owner === "agent" && value.display?.pickerDisabled === true ? value : null;
    }, 5000, "the page to see the agent's control");
    step.picker_disabled = owned.display.pickerDisabled;
    const before = await readDisplay();
    step.sent = await send(session, [{ t: "display", preset: "tablet" }]);
    await sleep(1500);
    const after = await readDisplay();
    step.note = (await stats(session)).note;
    step.before = briefDisplay(before);
    step.after = briefDisplay(after);
    step.ok = step.picker_disabled === true && /owned by agent/.test(step.note ?? "") &&
      JSON.stringify(step.before) === JSON.stringify(step.after);
  } finally {
    await canvas.control("release", "agent");
  }
  return step;
}

/**
 * A second Canvas starts while the device already has an override (fault injection): its
 * /status names no preset, it applies Small phone over HTTP, and its stop puts the
 * override back exactly.
 */
async function preexistingDisplayStep(original) {
  const step = {};
  await shell(`wm size ${PREEXISTING_DISPLAY.size}`);
  await shell(`wm density ${PREEXISTING_DISPLAY.density}`);
  const set = await readDisplay();
  step.set = briefDisplay(set);
  const canvas = await startCanvas("scrcpy");
  try {
    step.status = (await canvas.status()).display;
    step.applied = await canvas.display("small");
    step.during = briefDisplay(await readDisplay());
  } finally {
    step.exit = await canvas.stop();
  }
  const after = await readDisplay();
  step.after = briefDisplay(after);
  step.line = canvas.output().match(/Display restore[^\n]*/)?.[0] ?? null;
  const expected = { size_override: PREEXISTING_DISPLAY.size, density_override: PREEXISTING_DISPLAY.density };
  step.ok = sameOverrides(set, expected) && step.status?.preset === null && step.applied.status === 200 &&
    showsValues(step.during, DISPLAY_PRESETS[0]) && step.exit === 0 && sameOverrides(after, expected);
  step.original = briefDisplay(original);
  return step;
}

/** Put a finger down (pointer `id`) and move it a little, paced; no up. */
async function holdFinger(session, id) {
  await send(session, [{ t: "touch", a: "down", id, x: 0.5, y: 0.6 }]);
  await sendPaced(session, Array.from({ length: 4 }, (_, i) => ({ t: "touch", a: "move", id, x: 0.5, y: 0.6 - (i + 1) * 0.005 })));
}

/**
 * Whether the input dispatcher holds a finger down on any display (`dumpsys input`);
 * `touched` is null when the dump has no TouchStatesByDisplay line.
 */
async function touchState() {
  const dump = await shell("dumpsys input");
  const at = dump.indexOf("TouchStatesByDisplay:");
  if (at < 0) return { touched: null, text: null };
  const text = dump.slice(at).split("\n").slice(0, 3).join("\n").trim();
  return { touched: !text.startsWith(NO_TOUCH), text };
}

/** True once `dumpsys input` reports no touched display, within 5 s. */
async function touchCleared() {
  return await waitFor(async () => (await touchState()).touched === false, 5000, "no finger down on the device")
    .then(() => true, () => false);
}

/**
 * In landscape, tap Chrome's menu button, found in a UI dump: the menu opens only if the
 * tap is mapped to the rotated size. On about:blank the toolbar is the one control that is
 * always there, which makes it deterministic; Settings, the other app at hand, does not
 * rotate. The menu is closed again with Back.
 */
async function landscapeTap() {
  await sleep(1000);
  const result = { target: null, ok: false };
  const button = await findNode(/menu_button|Customize and control/);
  if (!button) return result;
  result.target = button.label.trim();
  result.screen = button.screen;
  result.menu_before = Boolean(await findNode(/app_menu_list/));
  const at = { x: button.x / button.screen.width, y: button.y / button.screen.height };
  result.at = [Number(at.x.toFixed(3)), Number(at.y.toFixed(3))];
  await send(args.session, [{ t: "touch", a: "down", id: 8, ...at }, { t: "touch", a: "up", id: 8, ...at }]);
  await sleep(1500);
  result.menu_opened = Boolean(await findNode(/app_menu_list/));
  result.ok = button.screen.width > button.screen.height && !result.menu_before && result.menu_opened;
  await send(args.session, [{ t: "system", op: "back" }]);
  await sleep(500);
  return result;
}

/**
 * Keys of other layouts typed on the screen: the focused search field is tapped again and
 * cleared, then browser key events go to the page's video element, as a keyboard would
 * send them, and the field must read LAYOUT_TEXT exactly.
 */
async function keyboardStep() {
  const step = { expected: LAYOUT_TEXT };
  const field = await findNode(/./, (node) => node.includes('focused="true"') && node.includes("EditText"));
  step.field_found = Boolean(field);
  if (field) {
    const at = { x: field.x / field.screen.width, y: field.y / field.screen.height };
    await send(args.session, [{ t: "touch", a: "down", id: 4, ...at }, { t: "touch", a: "up", id: 4, ...at }]);
    await sleep(500);
  }
  // End, then more Backspaces than the field holds.
  const press = (code) => [{ t: "key", a: "down", code, meta: 0, repeat: 0 }, { t: "key", a: "up", code, meta: 0, repeat: 0 }];
  await send(args.session, [...press(123), ...Array.from({ length: 40 }, () => press(67)).flat()]);
  await sleep(1000);
  // Evidence only: an empty field may report its hint as its text.
  step.after_clear = await focusedText();
  await pageJson(args.session, `(() => {
    const video = document.getElementById("video");
    for (const init of ${JSON.stringify(LAYOUT_KEYS)}) {
      for (const type of ["keydown", "keyup"]) {
        video.dispatchEvent(new KeyboardEvent(type, { bubbles: true, cancelable: true, ...init }));
      }
    }
    return JSON.stringify({ dispatched: ${LAYOUT_KEYS.length} });
  })()`);
  await sleep(2500);
  step.typed = await focusedText();
  // Exactly the layout text: anything left from before would show.
  step.ok = step.typed === LAYOUT_TEXT;
  return step;
}

/**
 * The UI hierarchy XML from uiautomator, or "" when the dump produced none. Only
 * `exec-out` passes the XML through; `shell` prints nothing but the "dumped to" line.
 */
async function uiDump() {
  const { stdout } = await adb(["exec-out", "uiautomator", "dump", "/dev/tty"]);
  const xml = String(stdout ?? "").replace(/\s*UI hier\w* dumped to:[^\n]*\s*$/, "").trim();
  return xml.includes("<hierarchy") ? xml : "";
}

/**
 * Center of the first node whose text, hint or resource id matches (and that `accept`
 * takes, given its raw XML), from uiautomator, with the screen size in the current
 * orientation: the right and bottom edges of everything in the dump.
 */
async function findNode(pattern, accept = () => true) {
  const dump = await uiDump();
  const nodes = [...dump.matchAll(/<node [^>]*>/g)].map((match) => match[0]);
  const screen = { width: 0, height: 0 };
  for (const node of nodes) {
    const bounds = node.match(/bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/);
    if (!bounds) continue;
    screen.width = Math.max(screen.width, Number(bounds[3]));
    screen.height = Math.max(screen.height, Number(bounds[4]));
  }
  for (const node of nodes) {
    const label = [/text="([^"]*)"/, /resource-id="([^"]*)"/, /content-desc="([^"]*)"/]
      .map((pattern_) => node.match(pattern_)?.[1] ?? "").join(" ");
    const bounds = node.match(/bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/);
    if (bounds && pattern.test(label) && accept(node)) {
      const [x1, y1, x2, y2] = bounds.slice(1).map(Number);
      return { x: (x1 + x2) / 2, y: (y1 + y2) / 2, label, screen };
    }
  }
  return null;
}

/**
 * The visible texts and their bounds from a UI dump, without the status bar, whose clock
 * and icons change on their own. Null when the dump failed or showed no text at all.
 */
async function screenLayout() {
  const dump = await uiDump();
  const nodes = [];
  for (const match of dump.matchAll(/<node [^>]*>/g)) {
    const node = match[0];
    if (node.includes(`package="${STATUS_BAR_PACKAGE}"`)) continue;
    const label = [/ text="([^"]*)"/, / content-desc="([^"]*)"/]
      .map((pattern) => node.match(pattern)?.[1] ?? "").filter(Boolean).join(" | ");
    const bounds = node.match(/bounds="([^"]*)"/)?.[1];
    if (label && bounds) nodes.push(`${label} ${bounds}`);
  }
  return nodes.length ? nodes : null;
}

function layoutSame(before, after) {
  return Boolean(before && after) && JSON.stringify(before) === JSON.stringify(after);
}

function layoutChanged(before, after) {
  return Boolean(before && after) && JSON.stringify(before) !== JSON.stringify(after);
}

/** Node counts for the report; null marks a dump that failed. */
function layoutSizes(before, after) {
  return { nodes_before: before?.length ?? null, nodes_after: after?.length ?? null };
}

async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, "utf8"));
  } catch {
    return null;
  }
}

/** An XML attribute value as text: the dump escapes &, <, >, quotes and some characters. */
function xmlText(value) {
  const named = { amp: "&", lt: "<", gt: ">", quot: "\"", apos: "'" };
  return value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (_, entity) => {
    if (entity[0] !== "#") return named[entity.toLowerCase()];
    return String.fromCodePoint(entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : Number(entity.slice(1)));
  });
}

async function focusedText() {
  const dump = await uiDump();
  const focused = [...dump.matchAll(/<node [^>]*>/g)].map((match) => match[0])
    .find((node) => node.includes('focused="true"') && node.includes("EditText"));
  return xmlText(focused?.match(/ text="([^"]*)"/)?.[1] ?? "");
}

const started = Date.now();
const report = { case: args.case, serial, ok: false };
try {
  await cases[args.case](report);
} catch (error) {
  // What the case collected before it failed stays in the report.
  report.ok = false;
  report.error = error.stack ?? error.message;
}
report.seconds = (Date.now() - started) / 1000;
report.discovery = discovery;
report.autonom_home = { path: autonomHome, kept: args["keep-home"] };
const reportPath = join(outDir, `${args.case}.json`);
await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
if (!args["keep-home"]) await rm(autonomHome, { recursive: true, force: true });
console.log(JSON.stringify({ case: args.case, ok: report.ok, report: reportPath }));
process.exit(report.ok ? 0 : 1);
