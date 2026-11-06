#!/usr/bin/env node
/**
 * Live check VER-003 (ios-canvas-unify, CU-01..CU-05): the iOS Canvas on the iOS device
 * daemon, in a real browser, on a fresh Simulator this script creates and deletes itself.
 *
 * Not part of `node --test tests/*.test.mjs`. With --create-simulator the script creates
 * "Autonom CU live <date time>" (iPhone 17 Pro, iOS 27.0), boots it and opens Settings,
 * starts this checkout's Canvas on it (`--transport auto`, its own temporary AUTONOM_HOME
 * and captures folder) and drives the page with Playwright Chromium. Every simctl call that
 * names a device names the one it created: no other Simulator is booted, shut down or
 * touched. A finally block stops the Canvas and the device daemon it started, shuts the
 * created Simulator down, deletes it and removes the temporary folders.
 *
 *   node tests/live/ios_canvas_unify_live.mjs --create-simulator --evidence-dir DIR \
 *     [--max-load 60] [--playwright DIR] [--python python3] [--xcrun xcrun] [--headless] [--keep-home]
 *
 * Checks, each recorded as `checks.<name>` with `ok` and what was observed:
 *   still     30 s on a still Settings screen with the page visible: at most 5 frames
 *             (framesDecoded / framesRendered deltas from window.autonomCanvas.stats()); ps
 *             sampled every 500 ms over the Canvas process tree (the Canvas, its tools
 *             server and the device daemon it started) finds no new simctl or xcrun process;
 *             companion, daemon and Canvas CPU over the window are recorded (CU-01-S01, CU-04-S01).
 *   join      a second page on the same device: its first frame within 1 s of its video
 *             socket opening (the time from navigation is recorded too) (CU-01-S02).
 *   scale     a page sized to a quarter tile (about 300 CSS px wide at devicePixelRatio 2,
 *             the size a tile of the 4-tile workspace grid has): /status
 *             stream_settings.scale_factor is 0.5 (CU-02-S01). The workspace grid itself is
 *             not used: a workspace attaches devices from discovery, which this check keeps
 *             away from other Simulators.
 *   hidden    the page hidden for 5 s (its window minimized through CDP; when that does not
 *             hide it, document.visibilityState is overridden and visibilitychange fired, as
 *             the unit tests do): /status idb.video_stopped within 2 s, and after it is shown
 *             again a new frame within 1 s (CU-03-S01).
 *   capture   the page's Capture path (the autonom:capture event the Capture button and
 *             Cmd+S send; POST /tools/call capture.screenshot when no page handles it): one
 *             PNG 1206x2622 in the captures folder, reply `source: "companion"`, and no
 *             simctl or xcrun process in the Canvas tree meanwhile (ps every 100 ms) (CU-05-S01).
 *   shutdown  `xcrun simctl shutdown <created udid>`: the tile shows "The Simulator is shut
 *             down" within 3 s of the command returning (CU-04-S02).
 *   cleanup   the Canvas and its daemon stopped, no companion left for the created
 *             Simulator, the Simulator deleted and the temporary folders removed.
 *
 * Writes DIR/ios-canvas-unify.json and DIR/ios-canvas-unify-capture.png. Prints one JSON
 * line at the end. Exit 0 when every check is ok, 1 when a check failed, 2 for usage, 3 for
 * an environment fault (load above --max-load, no Playwright or Chromium, no Xcode or iOS
 * 27.0 runtime, the Simulator could not be created or booted).
 *
 * Playwright comes from --playwright, AUTONOM_PLAYWRIGHT, ~/pr/platform/node_modules/playwright,
 * then a `playwright` package Node can resolve; /opt/pw-browsers is used as the browser
 * folder when it exists and PLAYWRIGHT_BROWSERS_PATH is not set. Chromium runs headed with
 * its window off screen (as canvas_ios_live.mjs), --headless runs it headless.
 */
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { cpus, homedir, loadavg, tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs as parseCliArgs } from "node:util";

const ROOT = resolve(import.meta.dirname, "../..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const REPORT_NAME = "ios-canvas-unify.json";
const CAPTURE_NAME = "ios-canvas-unify-capture.png";
const EXIT = Object.freeze({ ok: 0, failed: 1, usage: 2, environment: 3 });

// The Simulator this script creates (27.1 does not create on this Mac).
const DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro";
const RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-0";
const NAME_PREFIX = "Autonom CU live";
const SETTINGS = "com.apple.Preferences";
const FULL_SIZE = { width: 1206, height: 2622 };

// VER-003 bars.
const STILL_MS = 30_000;
const STILL_FRAMES_MAX = 5;
const PS_INTERVAL_MS = 500;
const CAPTURE_PS_INTERVAL_MS = 100;
const JOIN_BAR_MS = 1000;
const SCALE_WANTED = 0.5;
const SCALE_DPR = 2;
const HIDDEN_MS = 5000;
const VIDEO_STOP_BAR_MS = 2000;
const SHOWN_FRAME_BAR_MS = 1000;
const SHUTDOWN_BAR_MS = 3000;
const SHUT_DOWN_TEXT = "The Simulator is shut down";
// The rounding of canvas-devices.mjs tileScale, to size the quarter-tile page.
const IOS_SCALE_STEP = 0.25;
// After Settings opens: the screen settles before the still window starts.
const SETTLE_MS = 5000;
const OFF_SCREEN_WINDOW = "--window-position=3000,3000";
const CHECK_NAMES = ["still", "join", "scale", "hidden", "capture", "shutdown", "cleanup"];

const USAGE = `usage: ios_canvas_unify_live.mjs --create-simulator --evidence-dir DIR [--max-load 60]
       [--playwright DIR] [--python python3] [--xcrun xcrun] [--headless] [--keep-home]

Live check VER-003: creates its own Simulator "${NAME_PREFIX} <date>" (iPhone 17 Pro, iOS 27.0),
runs this checkout's iOS Canvas on it in Playwright Chromium and checks still, join, scale,
hidden, capture, shutdown and cleanup. It never touches another Simulator and deletes its own
at the end. Writes DIR/${REPORT_NAME}. Exit 0 all ok, 1 a check failed, 2 usage, 3 environment.`;

function usage(message) {
  console.error(`ios_canvas_unify_live: ${message}`);
  console.error(USAGE);
  process.exit(EXIT.usage);
}

let args;
try {
  ({ values: args } = parseCliArgs({
    strict: true,
    options: {
      help: { type: "boolean", short: "h", default: false },
      "create-simulator": { type: "boolean", default: false },
      "evidence-dir": { type: "string" },
      "max-load": { type: "string", default: "60" },
      playwright: { type: "string" },
      python: { type: "string", default: "python3" },
      xcrun: { type: "string", default: "xcrun" },
      headless: { type: "boolean", default: false },
      "keep-home": { type: "boolean", default: false },
    },
  }));
} catch (error) {
  usage(error.message);
}

if (args.help) {
  console.log(USAGE);
  process.exit(EXIT.ok);
}
if (!args["create-simulator"]) usage("--create-simulator is required; this check runs only on a Simulator it creates");
if (!args["evidence-dir"]) usage("--evidence-dir is required");
const maxLoad = Number(args["max-load"]);
if (!Number.isFinite(maxLoad) || maxLoad <= 0) usage("--max-load must be a positive number");

const outDir = resolve(args["evidence-dir"]);

class EnvironmentFault extends Error {}

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function round(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function hostLoad() {
  return loadavg().map((value) => round(value, 2));
}

// --- state the finally block cleans up ----------------------------------------------------

const state = {
  autonomHome: null,
  capturesDir: null,
  liveEnv: { ...process.env },
  udid: null,
  simulatorName: null,
  canvas: null,
  browser: null,
  samplers: new Set(),
};

function run(file, argv, { timeout = 60_000, env } = {}) {
  return new Promise((resolvePromise) => {
    execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env: env ?? state.liveEnv },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (error.code ?? 1) : 0, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
      });
  });
}

/** simctl without a device argument (list, create). */
async function simctl(argv, options) {
  return await run(args.xcrun, ["simctl", ...argv], options);
}

/** simctl `<verb> <created udid> ...`: refuses to run before the Simulator was created. */
async function simctlOwn(verb, rest = [], options) {
  if (!state.udid) throw new Error(`simctl ${verb}: no Simulator was created by this run`);
  return await simctl([verb, state.udid, ...rest], options);
}

async function listedDevices() {
  const listed = await simctl(["list", "devices", "--json"]);
  if (listed.code !== 0) throw new Error(`simctl list devices failed: ${listed.stderr.trim()}`);
  return Object.values(JSON.parse(listed.stdout).devices ?? {}).flat();
}

/** The created Simulator as simctl lists it, or null once it is gone. */
async function ownDevice() {
  if (!state.udid) return null;
  return (await listedDevices()).find((device) => device.udid === state.udid) ?? null;
}

// --- environment ---------------------------------------------------------------------------

async function loadPlaywright() {
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && existsSync("/opt/pw-browsers")) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = "/opt/pw-browsers";
  }
  const candidates = [args.playwright, process.env.AUTONOM_PLAYWRIGHT,
    join(homedir(), "pr/platform/node_modules/playwright")].filter(Boolean);
  for (const directory of candidates) {
    const entry = join(directory, "index.mjs");
    if (existsSync(entry)) return { module: await import(pathToFileURL(entry).href), source: directory };
  }
  try {
    return { module: await import("playwright"), source: "playwright package" };
  } catch {
    throw new EnvironmentFault(`Playwright was not found (tried ${candidates.join(", ") || "nothing"} and the playwright ` +
      "package); pass --playwright <node_modules/playwright>");
  }
}

/** Xcode's simctl answers and lists the iOS 27.0 runtime and the iPhone 17 Pro device type. */
async function checkXcode(report) {
  const runtimes = await simctl(["list", "runtimes", "--json"]);
  if (runtimes.code !== 0) throw new EnvironmentFault(`xcrun simctl does not answer: ${runtimes.stderr.trim() || runtimes.code}`);
  const runtime = (JSON.parse(runtimes.stdout).runtimes ?? []).find((item) => item.identifier === RUNTIME);
  if (!runtime) throw new EnvironmentFault(`the runtime ${RUNTIME} is not installed`);
  if (runtime.isAvailable === false) throw new EnvironmentFault(`the runtime ${RUNTIME} is not available: ${runtime.availabilityError ?? ""}`);
  const types = await simctl(["list", "devicetypes", "--json"]);
  if (types.code !== 0) throw new EnvironmentFault(`simctl list devicetypes failed: ${types.stderr.trim()}`);
  if (!(JSON.parse(types.stdout).devicetypes ?? []).some((item) => item.identifier === DEVICE_TYPE)) {
    throw new EnvironmentFault(`the device type ${DEVICE_TYPE} is not known to simctl`);
  }
  report.setup.xcode = { runtime: runtime.identifier, runtime_version: runtime.version ?? null, build: runtime.buildversion ?? null };
}

function localStamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}.${pad(date.getMinutes())}.${pad(date.getSeconds())}`;
}

/** Create, boot and open Settings on this run's own Simulator. */
async function createSimulator(report) {
  const name = `${NAME_PREFIX} ${localStamp()}`;
  if ((await listedDevices()).some((device) => device.name === name)) {
    throw new EnvironmentFault(`a Simulator named "${name}" already exists; not creating a second one`);
  }
  state.simulatorName = name;
  const created = await simctl(["create", name, DEVICE_TYPE, RUNTIME], { timeout: 120_000 });
  const udid = created.stdout.trim();
  if (created.code !== 0 || !/^[0-9A-F-]{36}$/i.test(udid)) {
    throw new EnvironmentFault(`simctl create failed: ${created.stderr.trim() || created.stdout.trim() || created.code}`);
  }
  state.udid = udid;
  report.setup.simulator = { name, udid, device_type: DEVICE_TYPE, runtime: RUNTIME, created: true };
  const bootStarted = Date.now();
  const booted = await simctlOwn("boot", [], { timeout: 120_000 });
  if (booted.code !== 0) throw new EnvironmentFault(`simctl boot failed: ${booted.stderr.trim()}`);
  const status = await simctlOwn("bootstatus", ["-b"], { timeout: 300_000 });
  if (status.code !== 0) throw new EnvironmentFault(`simctl bootstatus failed: ${status.stderr.trim()}`);
  report.setup.simulator.booted = true;
  report.setup.simulator.boot_ms = Date.now() - bootStarted;
  const launched = await simctlOwn("launch", [SETTINGS], { timeout: 60_000 });
  if (launched.code !== 0) throw new Error(`simctl launch Settings failed: ${launched.stderr.trim()}`);
  report.setup.settings_opened = true;
}

// --- processes -----------------------------------------------------------------------------

/** "[[dd-]hh:]mm:ss.cc" of ps `time` in seconds. */
function cpuSeconds(text) {
  const [days, rest] = text.includes("-") ? text.split("-") : ["0", text];
  const seconds = rest.split(":").reduce((total, part) => total * 60 + Number(part), 0);
  return Number(days) * 86_400 + seconds;
}

/** One `ps` reading: pid -> {pid, ppid, cpu (seconds), command}. */
async function psSnapshot() {
  const listed = await run("ps", ["-axo", "pid=,ppid=,time=,command="], { timeout: 10_000 });
  const processes = new Map();
  for (const line of listed.stdout.split("\n")) {
    const match = line.match(/^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$/);
    if (!match) continue;
    processes.set(Number(match[1]), { pid: Number(match[1]), ppid: Number(match[2]), cpu: cpuSeconds(match[3]), command: match[4] });
  }
  return processes;
}

/** The pids of `rootPid` and everything below it in one ps reading. */
function treeOf(processes, rootPid) {
  const children = new Map();
  for (const process of processes.values()) {
    if (!children.has(process.ppid)) children.set(process.ppid, []);
    children.get(process.ppid).push(process.pid);
  }
  const tree = new Set();
  const queue = processes.has(rootPid) ? [rootPid] : [];
  while (queue.length) {
    const pid = queue.shift();
    if (tree.has(pid)) continue;
    tree.add(pid);
    queue.push(...(children.get(pid) ?? []));
  }
  return tree;
}

/** Whether a ps command line runs simctl or xcrun (directly or through a shell). */
function isSimctl(command) {
  const [executable = "", ...rest] = command.trim().split(/\s+/);
  const name = basename(executable);
  if (name === "simctl" || name === "xcrun") return true;
  return ["sh", "bash", "zsh"].includes(name) && /(^|[\s/])(xcrun|simctl)(\s|$)/.test(rest.join(" "));
}

/** A command line for the report: short, with any token taken out. */
function shortCommand(command) {
  return command.replace(/(--token[=\s]+)\S+/g, "$1[redacted]").replace(/token=[^&\s]+/g, "token=[redacted]").slice(0, 160);
}

/**
 * Samples the process tree under `rootPid` every `intervalMs` until stop(): the processes
 * that appear in it after the first reading, and those of them that run simctl or xcrun.
 * A process that starts and ends between two readings is not seen.
 */
function startSampler(rootPid, intervalMs) {
  let stopped = false;
  let samples = 0;
  let baseline = null;
  let atStart = [];
  const appeared = new Map();
  const samplerStarted = Date.now();
  const loop = (async () => {
    while (!stopped) {
      const started = Date.now();
      const processes = await psSnapshot();
      const tree = treeOf(processes, rootPid);
      samples += 1;
      if (baseline === null) {
        baseline = tree;
        atStart = [...tree].map((pid) => processes.get(pid)).filter((process) => isSimctl(process.command))
          .map((process) => ({ pid: process.pid, command: shortCommand(process.command) }));
      } else {
        for (const pid of tree) {
          if (baseline.has(pid) || appeared.has(pid)) continue;
          appeared.set(pid, { pid, ppid: processes.get(pid).ppid, command: shortCommand(processes.get(pid).command),
            simctl: isSimctl(processes.get(pid).command), at_ms: started - samplerStarted });
        }
      }
      await sleep(Math.max(0, intervalMs - (Date.now() - started)));
    }
  })();
  const sampler = {
    async stop() {
      stopped = true;
      await loop;
      state.samplers.delete(sampler);
      const spawned = [...appeared.values()];
      return {
        interval_ms: intervalMs, samples, root_pid: rootPid,
        simctl_running_at_start: atStart,
        new_processes: spawned.length,
        // When each was first seen, in ms from the sampler's start (within one interval).
        new_process_commands: spawned.slice(0, 20).map((process) => ({ at_ms: process.at_ms, command: process.command })),
        simctl_spawns: spawned.filter((process) => process.simctl).length,
        simctl_spawn_commands: spawned.filter((process) => process.simctl).map((process) => ({ at_ms: process.at_ms, command: process.command })),
      };
    },
  };
  state.samplers.add(sampler);
  return sampler;
}

async function pgrep(pattern) {
  const listed = await run("pgrep", ["-f", pattern]);
  return listed.stdout.split("\n").map((line) => Number(line.trim())).filter(Boolean);
}

/** The device daemon this run's Canvas started: its registry under the temporary AUTONOM_HOME. */
async function daemonPid() {
  if (!state.autonomHome || !state.udid) return null;
  try {
    const registry = JSON.parse(await readFile(join(state.autonomHome, "iosd", `${state.udid}.json`), "utf8"));
    if (Number.isInteger(registry.pid) && registry.pid > 0) return registry.pid;
  } catch {
    // No registry (yet): look in the process table.
  }
  const pids = await pgrep(`ios-device-daemon.mjs --udid ${state.udid} --state-base ${state.autonomHome}`);
  return pids[0] ?? null;
}

async function companionPids() {
  return state.udid ? await pgrep(`idb_companion --udid ${state.udid}`) : [];
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** SIGTERM, then SIGKILL after `graceMs`; true once the process is gone. */
async function stopPid(pid, graceMs = 6000) {
  if (!pid || !alive(pid)) return true;
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    return !alive(pid);
  }
  const deadline = Date.now() + graceMs;
  while (Date.now() < deadline && alive(pid)) await sleep(100);
  if (alive(pid)) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // Gone meanwhile.
    }
    await sleep(300);
  }
  return !alive(pid);
}

/** CPU use of `pid` between two ps readings over `seconds`, in percent of one core. */
function cpuPercent(before, after, pid, seconds) {
  const was = before.get(pid);
  const now = after.get(pid);
  if (!was || !now || !(seconds > 0)) return null;
  return round(((now.cpu - was.cpu) / seconds) * 100, 2);
}

// --- Canvas --------------------------------------------------------------------------------

/**
 * Start this checkout's Canvas on the created Simulator with `--transport auto`; resolves
 * once it prints its URL. When it does not get that far it is stopped before the error.
 */
async function startCanvas() {
  const token = randomBytes(18).toString("base64url");
  const argv = [CANVAS, "--platform", "ios", "--target", state.udid, "--simctl", args.xcrun, "--port", "0",
    "--token", token, "--transport", "auto", "--python", args.python, "--captures-dir", state.capturesDir];
  const child = spawn(process.execPath, argv, { cwd: ROOT, env: state.liveEnv, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  const stop = async () => {
    if (!running()) return await exited;
    child.kill("SIGTERM");
    const code = await Promise.race([exited, sleep(10_000).then(() => "timeout")]);
    if (code !== "timeout") return code;
    child.kill("SIGKILL");
    return await exited;
  };
  state.canvas = { child, stop, running };
  const url = await new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${shortCommand(output)}`)), 45_000);
    child.stdout.on("data", () => {
      const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:\d+\/#token=\S+)/);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise(match[1]);
    });
    exited.then((code) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${output.replace(token, "[redacted]").slice(-2000)}`));
    });
  });
  const origin = new URL(url).origin;
  const headers = { Authorization: `Bearer ${token}` };
  Object.assign(state.canvas, {
    url, origin, token, pid: child.pid,
    async status() {
      return await (await fetch(`${origin}/status`, { headers })).json();
    },
    async call(op, payload = {}) {
      const response = await fetch(`${origin}/tools/call`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
        body: JSON.stringify({ op, payload }),
      });
      return { status: response.status, body: await response.json().catch(() => null) };
    },
  });
  return state.canvas;
}

/** The /status fields of the iOS stream this check reads. */
function streamStatus(status) {
  const idb = status?.idb ?? {};
  return {
    transport: status?.transport ?? null,
    fps_setting: status?.stream_settings?.fps ?? null,
    scale_factor: status?.stream_settings?.scale_factor ?? null,
    video_width: idb.width ?? null, video_height: idb.height ?? null,
    frames_per_second: idb.frames_per_second ?? null, watchers: idb.watchers ?? null,
    video_stopped: idb.video_stopped ?? null, video_stops: idb.video_stops ?? null,
    booted: idb.booted ?? null, target_state: idb.target_state ?? null,
    daemon_subscribed: idb.daemon_subscribed ?? null, companion_source: idb.companion_source ?? null,
    companion_pid: idb.companion_pid ?? null, packets: idb.packets ?? null, key_frames: idb.key_frames ?? null,
    streams_opened: idb.streams_opened ?? null, stream_restarts: idb.stream_restarts ?? null,
    forced_key_frames: idb.forced_key_frames ?? null, video_clients: idb.video_clients ?? null,
    last_error: status?.last_error ?? null,
  };
}

// --- browser -------------------------------------------------------------------------------

const browserMode = {
  headless: args.headless,
  args: args.headless ? [] : [OFF_SCREEN_WINDOW, "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling"],
};

/**
 * Runs in every page before its own script: the time the first /ws/video socket opened and
 * the time of the first decoded frame (performance.now(), ms from navigation).
 */
function timingScript() {
  window.__liveTimes = { videoOpen: null, firstFrame: null };
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(url, protocols) {
      super(url, protocols);
      if (String(url).includes("/ws/video")) {
        this.addEventListener("open", () => { window.__liveTimes.videoOpen ??= performance.now(); });
      }
    }
  };
  const tick = () => {
    const stats = window.autonomCanvas?.stats?.();
    if (stats && stats.framesDecoded > 0) {
      window.__liveTimes.firstFrame = performance.now();
      return;
    }
    setTimeout(tick, 5);
  };
  setTimeout(tick, 5);
}

async function openPage(canvas, { viewport = { width: 1280, height: 1000 }, deviceScaleFactor = 1 } = {}) {
  const page = await state.browser.newPage({ viewport, deviceScaleFactor });
  await page.addInitScript(timingScript);
  const opened = Date.now();
  await page.goto(canvas.url);
  await page.waitForFunction(() => (window.autonomCanvas?.stats()?.framesDecoded ?? 0) > 0, null,
    { timeout: 30_000, polling: 50 }).catch(async (error) => {
    const seen = await page.evaluate(() => window.autonomCanvas?.stats() ?? null).catch(() => null);
    throw new Error(`no frame on the page within 30 s (${error.message}); page stats ${JSON.stringify(seen && {
      transport: seen.transport, mode: seen.mode, session: seen.session, note: seen.note })}`);
  });
  const totalMs = Date.now() - opened;
  const times = await page.evaluate(() => window.__liveTimes);
  const first = await stats(page);
  return {
    page,
    total_ms: totalMs,
    navigation_to_frame_ms: round(times.firstFrame, 0),
    socket_to_frame_ms: times.firstFrame !== null && times.videoOpen !== null ? round(times.firstFrame - times.videoOpen, 0) : null,
    transport: first.transport, mode: first.mode, width: first.width, height: first.height,
  };
}

async function stats(page) {
  return await page.evaluate(() => window.autonomCanvas.stats());
}

async function pollUntil(predicate, timeoutMs, intervalMs = 100) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() >= deadline) return null;
    await sleep(intervalMs);
  }
}

/** The scale canvas-devices.mjs tileScale gives a picture shown at `rect` CSS px and `dpr`. */
function expectedScale(rect, dpr) {
  const needed = Math.max(
    Math.min(rect.width, rect.height) * dpr / Math.min(FULL_SIZE.width, FULL_SIZE.height),
    Math.max(rect.width, rect.height) * dpr / Math.max(FULL_SIZE.width, FULL_SIZE.height));
  return Math.min(1, Math.max(IOS_SCALE_STEP, Math.round(needed / IOS_SCALE_STEP) * IOS_SCALE_STEP));
}

async function deviceRect(page) {
  return await page.evaluate(() => {
    const rect = document.getElementById("device").getBoundingClientRect();
    return { width: Math.round(rect.width), height: Math.round(rect.height) };
  });
}

/** Hide the page: minimize its window through CDP; else override document.visibilityState. */
async function hidePage(page) {
  const visibility = () => page.evaluate(() => document.visibilityState).catch(() => null);
  if (!browserMode.headless) {
    try {
      const cdp = await page.context().newCDPSession(page);
      const { windowId } = await cdp.send("Browser.getWindowForTarget");
      await cdp.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "minimized" } });
      if (await pollUntil(async () => (await visibility()) === "hidden", 1500, 50)) {
        return {
          method: "window minimized (CDP Browser.setWindowBounds)",
          async show() {
            await cdp.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
          },
        };
      }
      await cdp.send("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
    } catch {
      // No window to minimize here: emulate below.
    }
  }
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  return {
    method: "document.visibilityState overridden and visibilitychange fired",
    async show() {
      await page.evaluate(() => {
        delete document.visibilityState;
        delete document.hidden;
        document.dispatchEvent(new Event("visibilitychange"));
      });
    },
  };
}

/** Width and height from a PNG's IHDR, or null for anything else. */
function pngSize(bytes) {
  const signature = "89504e470d0a1a0a";
  if (bytes.length < 24 || bytes.subarray(0, 8).toString("hex") !== signature) return null;
  if (bytes.subarray(12, 16).toString("ascii") !== "IHDR") return null;
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

async function filesUnder(directory, suffix) {
  const found = [];
  const walk = async (folder) => {
    let entries = [];
    try {
      entries = await readdir(folder, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const path = join(folder, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile() && entry.name.toLowerCase().endsWith(suffix) && !entry.name.startsWith(".")) found.push(path);
    }
  };
  await walk(directory);
  return found;
}

// --- the checks ----------------------------------------------------------------------------

function record(report, name, values) {
  report.checks[name] = { ...values, ok: Boolean(values.ok), ran: true };
  return report.checks[name];
}

async function checkStill(report, canvas, page) {
  await sleep(SETTLE_MS);
  const companion = (await canvas.status()).idb?.companion_pid ?? (await companionPids())[0] ?? null;
  const daemon = await daemonPid();
  const statusBefore = streamStatus(await canvas.status());
  const statsBefore = await stats(page);
  const psBefore = await psSnapshot();
  const cpuBefore = cpus();
  const sampler = startSampler(canvas.pid, PS_INTERVAL_MS);
  const started = Date.now();
  await sleep(STILL_MS);
  const ps = await sampler.stop();
  const seconds = (Date.now() - started) / 1000;
  const psAfter = await psSnapshot();
  const statsAfter = await stats(page);
  const statusAfter = streamStatus(await canvas.status());
  const tree = treeOf(psAfter, canvas.pid);
  const treeCpu = [...tree].filter((pid) => pid !== companion && psBefore.has(pid))
    .reduce((sum, pid) => sum + (psAfter.get(pid).cpu - psBefore.get(pid).cpu), 0);
  // Busy share of all host CPUs over the window (os.cpus()), to judge the host's own load.
  let busy = 0;
  let total = 0;
  cpus().forEach((cpu, index) => {
    const was = cpuBefore[index]?.times;
    if (!was) return;
    const spent = Object.keys(cpu.times).reduce((sum, key) => sum + cpu.times[key] - was[key], 0);
    total += spent;
    busy += spent - (cpu.times.idle - was.idle);
  });
  const framesDecoded = statsAfter.framesDecoded - statsBefore.framesDecoded;
  const framesRendered = statsAfter.framesRendered - statsBefore.framesRendered;
  const frames = Math.max(framesDecoded, framesRendered);
  const enoughSamples = ps.samples >= Math.floor(STILL_MS / PS_INTERVAL_MS / 2);
  record(report, "still", {
    ok: frames <= STILL_FRAMES_MAX && ps.simctl_spawns === 0 && enoughSamples,
    seconds: round(seconds, 2),
    frames, frames_max: STILL_FRAMES_MAX, frames_decoded: framesDecoded, frames_rendered: framesRendered,
    frames_per_minute: round((frames / seconds) * 60, 1),
    packets: Number.isInteger(statusBefore.packets) && Number.isInteger(statusAfter.packets) ? statusAfter.packets - statusBefore.packets : null,
    fps_setting: statusAfter.fps_setting, companion_source: statusAfter.companion_source,
    daemon_subscribed: statusAfter.daemon_subscribed,
    simctl_spawns: ps.simctl_spawns, simctl_spawns_per_minute: round((ps.simctl_spawns / seconds) * 60, 1),
    ps,
    cpu_percent: {
      companion: companion ? cpuPercent(psBefore, psAfter, companion, seconds) : null,
      canvas: cpuPercent(psBefore, psAfter, canvas.pid, seconds),
      daemon: daemon ? cpuPercent(psBefore, psAfter, daemon, seconds) : null,
      canvas_tree: round((treeCpu / seconds) * 100, 2),
      host_busy: total ? round((busy / total) * 100, 1) : null,
    },
    pids: { companion, canvas: canvas.pid, daemon, companion_in_canvas_tree: companion ? tree.has(companion) : null },
    status_before: statusBefore, status_after: statusAfter,
  });
}

async function checkJoin(report, canvas) {
  const joined = await openPage(canvas);
  await sleep(500);
  const status = streamStatus(await canvas.status());
  record(report, "join", {
    ok: joined.socket_to_frame_ms !== null && joined.socket_to_frame_ms <= JOIN_BAR_MS,
    socket_to_frame_ms: joined.socket_to_frame_ms, bar_ms: JOIN_BAR_MS,
    navigation_to_frame_ms: joined.navigation_to_frame_ms, goto_to_frame_ms: joined.total_ms,
    transport: joined.transport, mode: joined.mode, video_clients: status.video_clients,
    forced_key_frames: status.forced_key_frames, streams_opened: status.streams_opened,
  });
  return joined.page;
}

/** A page sized to a quarter tile at devicePixelRatio 2; the pages opened before are closed. */
async function checkScale(report, canvas, earlierPages) {
  let viewport = { width: 640, height: 760 };
  const opened = await openPage(canvas, { viewport, deviceScaleFactor: SCALE_DPR });
  const { page } = opened;
  for (const earlier of earlierPages) await earlier.close().catch(() => {});
  let rect = await deviceRect(page);
  const tries = [{ viewport, rect, expected: expectedScale(rect, SCALE_DPR) }];
  // The device frame follows the page height: aim the picture at about 655 CSS px tall
  // (1310 device px, half of 2622).
  for (let attempt = 0; attempt < 4 && expectedScale(rect, SCALE_DPR) !== SCALE_WANTED && rect.height > 0; attempt += 1) {
    const factor = 655 / rect.height;
    viewport = { width: Math.max(320, Math.round(viewport.width * factor)), height: Math.max(400, Math.round(viewport.height * factor)) };
    await page.setViewportSize(viewport);
    await sleep(400);
    rect = await deviceRect(page);
    tries.push({ viewport, rect, expected: expectedScale(rect, SCALE_DPR) });
  }
  const started = Date.now();
  const reached = await pollUntil(async () => {
    const status = streamStatus(await canvas.status());
    return status.scale_factor === SCALE_WANTED ? status : null;
  }, 6000, 100);
  const status = reached ?? streamStatus(await canvas.status());
  // The stream's own picture size follows the scale once the restart is through.
  await sleep(1500);
  const settled = streamStatus(await canvas.status());
  const dpr = await page.evaluate(() => window.devicePixelRatio);
  record(report, "scale", {
    ok: reached !== null,
    method: "page sized to a quarter tile (4-tile workspace grid not used)",
    scale_factor: status.scale_factor, wanted: SCALE_WANTED, reached_ms: reached ? Date.now() - started : null,
    tile_css: rect, device_pixel_ratio: dpr, expected_from_tile: expectedScale(rect, dpr), viewport, tries,
    video_width: settled.video_width, video_height: settled.video_height, watchers: settled.watchers,
    page_first_frame_ms: opened.total_ms,
  });
  return page;
}

async function checkHidden(report, canvas, page) {
  const before = await stats(page);
  const hidden = await hidePage(page);
  const hiddenAt = Date.now();
  const stopped = await pollUntil(async () => ((await canvas.status()).idb?.video_stopped === true ? Date.now() : null), 4000, 100);
  const stoppedMs = stopped === null ? null : stopped - hiddenAt;
  const statusHidden = streamStatus(await canvas.status());
  await sleep(Math.max(0, hiddenAt + HIDDEN_MS - Date.now()));
  const atShow = await stats(page);
  const packetsBeforeShow = (await canvas.status()).idb?.packets ?? null;
  const showStarted = Date.now();
  await hidden.show();
  const shown = await pollUntil(async () => {
    const now = await page.evaluate(() => window.autonomCanvas.stats().framesDecoded).catch(() => 0);
    return now > atShow.framesDecoded ? Date.now() : null;
  }, 5000, 20);
  const shownFrameMs = shown === null ? null : shown - showStarted;
  await sleep(600);
  const statusShown = streamStatus(await canvas.status());
  record(report, "hidden", {
    ok: stoppedMs !== null && stoppedMs <= VIDEO_STOP_BAR_MS && shownFrameMs !== null && shownFrameMs <= SHOWN_FRAME_BAR_MS,
    method: hidden.method, hidden_ms: HIDDEN_MS,
    video_stopped_ms: stoppedMs, stop_bar_ms: VIDEO_STOP_BAR_MS,
    frames_while_hidden: atShow.framesDecoded - before.framesDecoded,
    packets_while_stopped: Number.isInteger(packetsBeforeShow) && Number.isInteger(statusHidden.packets)
      ? packetsBeforeShow - statusHidden.packets : null,
    shown_frame_ms: shownFrameMs, shown_bar_ms: SHOWN_FRAME_BAR_MS,
    status_hidden: { video_stopped: statusHidden.video_stopped, watchers: statusHidden.watchers, video_stops: statusHidden.video_stops },
    status_shown: { video_stopped: statusShown.video_stopped, watchers: statusShown.watchers, frames_per_second: statusShown.frames_per_second },
  });
}

async function checkCapture(report, canvas, page) {
  const before = await filesUnder(state.capturesDir, ".png");
  const sampler = startSampler(canvas.pid, CAPTURE_PS_INTERVAL_MS);
  let method = "page autonom:capture event (Capture button / Cmd+S path)";
  let answer = null;
  let http = null;
  let requestPayload = null;
  try {
    const response = page.waitForResponse((item) => item.url().includes("/tools/call") &&
      (item.request().postData() ?? "").includes("capture.screenshot"), { timeout: 60_000 });
    response.catch(() => {});
    const handled = await page.evaluate(() => !window.dispatchEvent(new CustomEvent("autonom:capture",
      { cancelable: true, detail: { action: "screenshot" } })));
    if (handled) {
      const reply = await response;
      http = reply.status();
      answer = await reply.json().catch(() => null);
      try {
        requestPayload = JSON.parse(reply.request().postData() ?? "null")?.payload ?? null;
      } catch {
        requestPayload = null;
      }
    } else {
      method = "POST /tools/call capture.screenshot (no page handled the capture event)";
      const reply = await canvas.call("capture.screenshot", {});
      http = reply.status;
      answer = reply.body;
      requestPayload = {};
    }
  } finally {
    await sleep(300);
  }
  const ps = await sampler.stop();
  const after = await filesUnder(state.capturesDir, ".png");
  const added = after.filter((path) => !before.includes(path));
  let size = null;
  let saved = null;
  if (added.length === 1) {
    const bytes = await readFile(added[0]);
    size = pngSize(bytes);
    saved = join(outDir, CAPTURE_NAME);
    await copyFile(added[0], saved);
  }
  const result = answer?.result ?? {};
  record(report, "capture", {
    ok: http === 200 && answer?.ok !== false && result.source === "companion" && added.length === 1 &&
      size?.width === FULL_SIZE.width && size?.height === FULL_SIZE.height && ps.simctl_spawns === 0,
    method, http, source: result.source ?? null, fallback_reason: result.fallback_reason ?? null,
    warning: result.warning ?? null, error: answer?.ok === false ? answer.error ?? null : null,
    error_code: answer?.error_code ?? null, request_payload: requestPayload,
    pngs_added: added.length, png: size, wanted: FULL_SIZE, capture_name: result.capture?.name ?? null,
    capture_size_bytes: result.capture?.size ?? null, evidence_file: saved,
    simctl_spawns: ps.simctl_spawns, ps,
  });
}

async function checkShutdown(report, canvas, page) {
  const shown = () => page.evaluate((text) => {
    const caption = document.getElementById("caption")?.textContent ?? "";
    const notice = document.getElementById("notice")?.textContent ?? "";
    return caption.includes(text) || notice.includes(text);
  }, SHUT_DOWN_TEXT).catch(() => false);
  const started = Date.now();
  let shownAt = null;
  // The tile is watched while the command runs too: it may show before simctl returns.
  const watching = pollUntil(async () => (await shown() ? Date.now() : null), 60_000, 50).then((at) => { shownAt = at; });
  const shutdown = await simctlOwn("shutdown", [], { timeout: 120_000 });
  const ended = Date.now();
  await Promise.race([watching, sleep(10_000)]);
  const status = streamStatus(await canvas.status().catch(() => null));
  const fromEnd = shownAt === null ? null : Math.max(0, shownAt - ended);
  record(report, "shutdown", {
    ok: shutdown.code === 0 && fromEnd !== null && fromEnd <= SHUTDOWN_BAR_MS,
    simctl_code: shutdown.code, simctl_error: shutdown.code === 0 ? null : shutdown.stderr.trim().slice(0, 300),
    command_ms: ended - started, shown_after_command_ms: fromEnd,
    shown_from_command_start_ms: shownAt === null ? null : shownAt - started, bar_ms: SHUTDOWN_BAR_MS,
    text: SHUT_DOWN_TEXT, status_booted: status.booted, status_target_state: status.target_state,
  });
}

// --- run and clean up ----------------------------------------------------------------------

async function runChecks(report) {
  const { module: playwright, source } = await loadPlaywright();
  const executable = playwright.chromium?.executablePath?.();
  if (!executable || !existsSync(executable)) {
    throw new EnvironmentFault(`Playwright Chromium is not installed (${executable ?? "no path"}); run \`npx playwright install chromium\``);
  }
  report.browser = { ...browserMode, playwright: source };
  await checkXcode(report);

  state.autonomHome = await mkdtemp(join(tmpdir(), "autonom-cu-live-home-"));
  state.capturesDir = await mkdtemp(join(tmpdir(), "autonom-cu-live-captures-"));
  state.liveEnv = { ...process.env, AUTONOM_HOME: state.autonomHome, AUTONOM_CAPTURES_DIR: state.capturesDir };
  report.setup.temp = { autonom_home: state.autonomHome, captures_dir: state.capturesDir };

  await createSimulator(report);
  await sleep(SETTLE_MS);

  const canvas = await startCanvas();
  const firstStatus = await canvas.status();
  report.setup.canvas = { pid: canvas.pid, origin: canvas.origin, ...streamStatus(firstStatus) };
  state.browser = await playwright.chromium.launch({ headless: browserMode.headless, args: browserMode.args });
  // Every process the Canvas tree starts while the checks run (for the report, not a check).
  const wholeRun = startSampler(canvas.pid, PS_INTERVAL_MS);
  // When each step began, in ms from the whole-run sampler's start (to place its spawns).
  const runStarted = Date.now();
  report.timeline = {};
  const mark = (name) => { report.timeline[name] = Date.now() - runStarted; };
  try {
    mark("first_page");
    const first = await openPage(canvas);
    report.setup.first_page = { goto_to_frame_ms: first.total_ms, socket_to_frame_ms: first.socket_to_frame_ms,
      transport: first.transport, mode: first.mode, width: first.width, height: first.height };
    report.setup.canvas_shows_simulator = first.transport === "idb" && first.mode === "webcodecs";
    mark("still");
    report.host.loadavg_before_still = hostLoad();
    await checkStill(report, canvas, first.page);
    report.host.loadavg_after_still = hostLoad();
    mark("join");
    const second = await checkJoin(report, canvas);
    mark("scale");
    const quarter = await checkScale(report, canvas, [first.page, second]);
    mark("hidden");
    await checkHidden(report, canvas, quarter);
    mark("capture");
    await checkCapture(report, canvas, quarter);
    mark("shutdown");
    await checkShutdown(report, canvas, quarter);
    mark("end");
  } finally {
    report.whole_run_processes = await wholeRun.stop();
  }
}

let cleaning = null;

/** Stop everything this run started and delete its Simulator; records what happened. */
function cleanup(report) {
  cleaning ??= (async () => {
    const done = { steps: [] };
    const step = async (name, work) => {
      try {
        done[name] = await work();
      } catch (error) {
        done[name] = false;
        done.steps.push(`${name}: ${error.message}`);
      }
    };
    for (const sampler of [...state.samplers]) await sampler.stop().catch(() => {});
    await step("browser_closed", async () => {
      if (state.browser) await state.browser.close();
      return true;
    });
    const daemon = await daemonPid().catch(() => null);
    await step("canvas_stopped", async () => {
      if (!state.canvas) return true;
      const code = await state.canvas.stop();
      done.canvas_exit = code;
      return !state.canvas.running();
    });
    await step("daemon_stopped", async () => {
      const pid = daemon ?? await daemonPid();
      done.daemon_pid = pid;
      return pid ? await stopPid(pid) : true;
    });
    await step("companions_left", async () => {
      await sleep(500);
      const left = await companionPids();
      for (const pid of left) await stopPid(pid, 3000);
      done.companions_stopped = left;
      return (await companionPids()).length;
    });
    await step("simulator_deleted", async () => {
      if (!state.udid) return null;
      const device = await ownDevice();
      if (device && device.state !== "Shutdown") {
        const shutdown = await simctlOwn("shutdown", [], { timeout: 120_000 });
        if (shutdown.code !== 0 && !/current state: Shutdown/i.test(shutdown.stderr)) {
          done.steps.push(`shutdown: ${shutdown.stderr.trim().slice(0, 200)}`);
        }
      }
      if (device) {
        const deleted = await simctlOwn("delete", [], { timeout: 120_000 });
        if (deleted.code !== 0) done.steps.push(`delete: ${deleted.stderr.trim().slice(0, 200)}`);
      }
      return (await ownDevice()) === null;
    });
    await step("temp_removed", async () => {
      const folders = [state.capturesDir, args["keep-home"] ? null : state.autonomHome].filter(Boolean);
      for (const folder of folders) await rm(folder, { recursive: true, force: true });
      return folders.every((folder) => !existsSync(folder));
    });
    done.autonom_home_kept = args["keep-home"] && Boolean(state.autonomHome);
    const createdSimulator = Boolean(state.udid);
    record(report, "cleanup", {
      ...done,
      ok: done.browser_closed === true && done.canvas_stopped === true && done.daemon_stopped === true &&
        done.companions_left === 0 && (createdSimulator ? done.simulator_deleted === true : true) && done.temp_removed === true,
    });
  })();
  return cleaning;
}

const startedAt = Date.now();
const report = {
  check: "VER-003",
  ok: false,
  started_at: new Date(startedAt).toISOString(),
  host: { cpus: cpus().length, loadavg_start: hostLoad(), max_load: maxLoad },
  setup: {},
  checks: {},
};
for (const name of CHECK_NAMES) report.checks[name] = { ok: false, ran: false };
let environmentFault = false;

async function finish() {
  report.host.loadavg_end = hostLoad();
  report.seconds = round((Date.now() - startedAt) / 1000, 1);
  report.ok = !environmentFault && !report.error && CHECK_NAMES.every((name) => report.checks[name]?.ok === true);
  report.exit = environmentFault ? EXIT.environment : report.ok ? EXIT.ok : EXIT.failed;
  const reportPath = join(outDir, REPORT_NAME);
  try {
    await mkdir(outDir, { recursive: true });
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  } catch (error) {
    console.error(`ios_canvas_unify_live: could not write ${reportPath}: ${error.message}`);
  }
  const failed = CHECK_NAMES.filter((name) => report.checks[name]?.ok !== true);
  console.log(JSON.stringify({ check: "VER-003", ok: report.ok, exit: report.exit, failed,
    environment_fault: report.environment_fault ?? null, report: reportPath }));
  process.exit(report.exit);
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, async () => {
    report.error = `interrupted by ${signal}`;
    await cleanup(report).catch(() => {});
    await finish();
  });
}

try {
  await mkdir(outDir, { recursive: true });
  const load = loadavg()[0];
  if (load > maxLoad) {
    throw new EnvironmentFault(`the 1-minute load average is ${round(load, 2)}, above --max-load ${maxLoad}; not starting`);
  }
  await runChecks(report);
} catch (error) {
  if (error instanceof EnvironmentFault) {
    environmentFault = true;
    report.environment_fault = error.message;
  } else {
    // What the checks collected before the failure stays in the report.
    report.error = error.stack ?? error.message;
  }
} finally {
  await cleanup(report).catch((error) => { report.cleanup_error = error.message; });
}
await finish();
