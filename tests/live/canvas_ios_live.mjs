#!/usr/bin/env node
/**
 * Live acceptance for the Mobile Canvas iOS fast transport (idb).
 *
 * Not part of `node --test tests/*.test.mjs`: each case starts this checkout's Canvas on
 * the one test Simulator Autonom-Fast-Test with `--transport auto`, checks that it chose
 * idb and started its own idb_companion, and drives the page in Chromium through
 * Playwright. Any other UDID is refused. The Simulator is booted only when it is shut
 * down, and shut down again at the end in that case; Settings is closed again when the
 * case opened it. A case passes only when the Simulator is back in its initial state.
 *
 * Chromium runs headed by default, its window placed off screen (--window-position), since
 * headless Chromium fires animation frames unevenly and so cannot show 60 fps steadily;
 * --headless runs it headless anyway. The report names the mode and the animation frame
 * rate the page saw.
 *
 * Every run gets its own temporary AUTONOM_HOME under the OS temp directory, handed to
 * the Canvas (and so to its journal bridge) and to every Autonom CLI call, so a case can
 * never read, stop or write the user's own sessions. It is removed at the end unless
 * --keep-home is given.
 *
 *   node tests/live/canvas_ios_live.mjs --udid 3760A5A8-E59D-4AE6-B1AF-A626908D7B61 \
 *     --case video|input|controls [--evidence-dir DIR] [--idb-companion PATH] [--playwright DIR] \
 *     [--seconds 10] [--python python3] [--xcrun xcrun] [--idb idb] [--headless] [--keep-home]
 *
 * Playwright comes from --playwright, AUTONOM_PLAYWRIGHT, ~/pr/platform/node_modules/playwright,
 * then a `playwright` package Node can resolve; /opt/pw-browsers is used as the browser
 * folder when it exists and PLAYWRIGHT_BROWSERS_PATH is not set.
 *
 * Cases:
 *   video  first a static control window (no input), then --seconds of Settings flung up
 *          and down once a second by a finger driven from a 16 ms timer, so the list's
 *          momentum changes the screen on (nearly) every frame. For each window: frames
 *          decoded, dropped and presented by the page, presented fps and frame interval
 *          percentiles, distinct-frame fps (a presented frame counts when the picture differs
 *          from the one before), the stream's packet rate, the animation frame rate, finger
 *          moves and HID events per second, and host CPU load (load averages around them).
 *          Then SSIM of a settled frame against a simctl screenshot (outside the screen's
 *          rounded corners and camera cutout, which the screenshot masks and the stream
 *          does not), a second tab joining (time to its first frame), exactly one companion
 *          stream, and no companion left after the Canvas stops. Writes video.json.
 *   input  a finger held still barely changes the picture and the same finger dragged
 *          changes it on most frames; an agent takeover while it is still held lifts it
 *          and refuses later human input; a tap opens a Settings row, Home leaves Settings,
 *          text reaches the Settings search field (only its length is kept), and every
 *          action is one journal record naming idb. Writes input.json. It reads the
 *          screen with `autonom ui tree`, which needs the companion the idb CLI uses; when
 *          none runs for the test Simulator, `idb connect` starts it and the case stops it.
 *   controls through the Canvas control socket (as an agent, with the token): Volume up then
 *          Volume down, read back as `sim_volume` in the Simulator's audiosettings.plist, which
 *          must rise and then return (the Mac's output volume is read before and after, and
 *          must not change), then the same through HTTP POST /key KEYCODE_VOLUME_UP/DOWN (the
 *          action bridge presses the buttons with `idb ui button`). A freshly booted Simulator keeps a placeholder there until its audio
 *          service writes the level, so the case waits 30 s after a boot it made, and while the
 *          value is still a placeholder pairs of Volume up and Volume down (no net change) go
 *          first (in the report as `priming`); a paste of the test string, read back with clipboard-get (the
 *          report keeps only whether it matched); a mirrored two-finger pinch, which must be
 *          one HID event sent at release, then a one-finger tap at the status bar (two HID
 *          events); the journal must hold the pinch as a gesture with pointers 2, the tap as a
 *          gesture with pointers 1, two system records (after any priming ones), two key records
 *          (VOLUME_UP, VOLUME_DOWN) and one paste record. The Simulator
 *          pasteboard is put back as found. Writes controls.json. Like input, it starts a
 *          session and, when none runs, the idb CLI's companion, which types the paste.
 *
 * Each report has `ok` and the steps done so far also when the case fails midway; the
 * script exits non-zero when its oracle fails.
 */
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { cpus, homedir, loadavg, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs as parseCliArgs } from "node:util";

import { withoutTypedText } from "./live-report.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const AUTONOM = join(ROOT, "scripts/autonom.py");
const CASES = ["video", "input", "controls"];
// The only Simulator these cases may touch (C-4).
const TEST_UDID = "3760A5A8-E59D-4AE6-B1AF-A626908D7B61";
const TEST_NAME = "Autonom-Fast-Test";
const SETTINGS = "com.apple.Preferences";
// VER-003 and VER-004 bars.
const FPS_BAR = 55;
const SSIM_BAR = 0.99;
const JOIN_BAR_MS = 2500;
const HELD_CHANGES_BAR = 10;
// These prove the scrolling really happened (the presented-fps bar measures the transport):
// while the list is flung, the median second must show at least DISTINCT_MEDIAN_BAR distinct
// frames and every second at least DISTINCT_SECOND_BAR, and the static control window none
// at all, or the distinct-frame detector cannot tell a still screen from a moving one.
const DISTINCT_MEDIAN_BAR = 45;
const DISTINCT_SECOND_BAR = 30;
const STATIC_MS = 3000;
// The scroll driver flings Settings: every FLING_EVERY_MS a finger goes down at one of these
// heights (0..1 of the screen), moves to the other in STROKE_MS (one move per 16 ms timer
// tick) and lifts at speed; the next stroke goes the other way and catches the list still
// moving. While a finger drags it (60 moves per second) the Simulator changes its screen on
// only every other frame, and a finger catching the moving list stops it for about four
// frames; the list's own momentum animation changes it on every frame. So the strokes are
// short and a second apart, and most of the window is momentum.
const SCROLL_FROM = 0.65;
const SCROLL_TO = 0.45;
const FLING_EVERY_MS = 1000;
const STROKE_MS = 96;
const SCROLL_STEP_MS = 16;
// After the last fling: the picture must stay still this long before it counts as settled.
const STILL_MS = 1000;
const STILL_WAIT_MAX_MS = 15_000;
// A still Simulator screen is still sent at 60 fps as repeated frames of about 250-550 bytes;
// a stream packet larger than this most likely carries a changed picture. Only reported: the
// frames that sharpen a still picture are larger too (about 8 per second on a still screen).
const REPEAT_PACKET_BYTES = 1000;
// Distinct frames are told apart by a 64x64 luma copy of this band of the picture (0..1 of
// its height): the lower half, away from the status bar clock and any animated row icon.
// The stream repeats a still picture at 60 fps and each repeat refines it a little, so a
// frame counts as changed only when at least PICTURE_MOVED_PIXELS of those pixels differ
// from the frame before by more than PICTURE_LEVEL (of 255).
const SIGNATURE_BAND = [0.45, 0.95];
const PICTURE_LEVEL = 12;
const PICTURE_MOVED_PIXELS = 16;
// Also reported, to show how the counts depend on the level: pixels moved by more than this.
const PICTURE_LOW_LEVEL = 4;
const OFF_SCREEN_WINDOW = "--window-position=3000,3000";
// What the input case types into the Settings search field; never a secret. The report
// keeps only its length.
const SEARCH_TEXT = "bluetooth";
// After the scrolling stops: a key frame comes every 2 s and settled frames sharpen.
const SETTLE_MS = 2500;
// The controls case: the only text it pastes, and where the Simulator keeps its volume.
const CLIP_TEXT = "autonom-clip-test";
const VOLUME_SETTLE_MS = 30_000;
const AUDIO_SETTINGS = join(homedir(), "Library/Developer/CoreSimulator/Devices", TEST_UDID,
  "data/var/run/simulatoraudio/audiosettings.plist");

const { values: args } = parseCliArgs({
  options: {
    udid: { type: "string" },
    case: { type: "string" },
    "evidence-dir": { type: "string" },
    "idb-companion": { type: "string" },
    playwright: { type: "string" },
    seconds: { type: "string", default: "10" },
    python: { type: "string", default: "python3" },
    xcrun: { type: "string", default: "xcrun" },
    idb: { type: "string", default: "idb" },
    headless: { type: "boolean", default: false },
    "keep-home": { type: "boolean", default: false },
  },
});

function usage(message) {
  console.error(`canvas_ios_live: ${message}`);
  console.error(`usage: canvas_ios_live.mjs --udid ${TEST_UDID} --case ${CASES.join("|")} ` +
    "[--evidence-dir DIR] [--idb-companion PATH] [--playwright DIR] [--seconds N] [--python PATH] [--xcrun PATH] " +
    "[--idb PATH] [--headless] [--keep-home]");
  process.exit(2);
}

if (!args.udid) usage("--udid is required; the cases never pick a Simulator by themselves");
if (args.udid !== TEST_UDID) usage(`the cases run only on ${TEST_NAME} (${TEST_UDID}), not on ${args.udid}`);
if (!CASES.includes(args.case)) usage(`--case must be one of ${CASES.join(", ")}`);
const seconds = Number(args.seconds);
if (!Number.isFinite(seconds) || seconds < 2 || seconds > 120) usage("--seconds must be from 2 to 120");

const udid = args.udid;
const outDir = resolve(args["evidence-dir"] ?? await mkdtemp(join(tmpdir(), "autonom-canvas-ios-live-")));
await mkdir(outDir, { recursive: true });
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-canvas-ios-live-home-"));
const liveEnv = { ...process.env, AUTONOM_HOME: autonomHome };

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function run(file, argv, { timeout = 60_000, env } = {}) {
  return new Promise((resolvePromise) => {
    execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env: env ?? liveEnv },
      (error, stdout, stderr) => {
        resolvePromise({ code: error ? (error.code ?? 1) : 0, stdout, stderr: String(stderr ?? "") });
      });
  });
}

async function simctl(argv, options) {
  return await run(args.xcrun, ["simctl", ...argv], options);
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

/** The Autonom CLI on the test Simulator, inside this run's own AUTONOM_HOME. */
async function autonom(argv) {
  return await run(args.python, [AUTONOM, "--platform", "ios", "--udid", udid, ...argv], { timeout: 120_000 });
}

/** The test Simulator as simctl lists it; refuses a UDID whose name is not the test one. */
async function simulator() {
  const listed = await simctl(["list", "devices", "--json"]);
  if (listed.code !== 0) throw new Error(`simctl list failed: ${listed.stderr}`);
  const device = Object.values(JSON.parse(listed.stdout).devices ?? {}).flat().find((item) => item.udid === udid);
  if (!device) throw new Error(`${TEST_NAME} (${udid}) is not listed by simctl`);
  if (device.name !== TEST_NAME) throw new Error(`${udid} is named ${device.name}, not ${TEST_NAME}; refusing to use it`);
  return device;
}

// When this run booted the Simulator (Date.now() at the end of bootstatus), else null.
let bootedAt = null;

/** Boot the Simulator only when it is shut down; returns how to put it back. */
async function prepareSimulator(report) {
  const device = await simulator();
  report.simulator = { name: device.name, initial_state: device.state, booted_by_case: false };
  if (device.state === "Shutdown") {
    const booted = await simctl(["boot", udid]);
    if (booted.code !== 0) throw new Error(`simctl boot failed: ${booted.stderr}`);
    report.simulator.booted_by_case = true;
    const status = await simctl(["bootstatus", udid, "-b"], { timeout: 300_000 });
    if (status.code !== 0) throw new Error(`simctl bootstatus failed: ${status.stderr}`);
    bootedAt = Date.now();
  } else if (device.state !== "Booted") {
    throw new Error(`${TEST_NAME} is ${device.state}; it must be Booted or Shutdown`);
  }
  report.simulator.settings_was_running = await settingsRunning();
}

/**
 * Put the Simulator back as the case found it, recording every simctl failure: Settings is
 * closed when the case opened it (a failed terminate counts only while Settings still
 * runs), and a Simulator the case booted is shut down again.
 */
async function restoreSimulator(report) {
  const simulatorReport = report.simulator;
  if (!simulatorReport) return;
  if (!simulatorReport.settings_was_running) {
    const terminated = await simctl(["terminate", udid, SETTINGS]);
    simulatorReport.settings_closed = terminated.code === 0 || !(await settingsRunning());
    if (!simulatorReport.settings_closed) simulatorReport.settings_close_error = terminated.stderr.trim();
  }
  if (simulatorReport.booted_by_case) {
    const shutdown = await simctl(["shutdown", udid], { timeout: 120_000 });
    simulatorReport.shut_down_again = shutdown.code === 0;
    if (!simulatorReport.shut_down_again) simulatorReport.shutdown_error = shutdown.stderr.trim();
  }
  simulatorReport.final_state = (await simulator().catch(() => null))?.state ?? null;
}

/** Whether restoreSimulator put the Simulator back in its initial state. */
function restored(simulatorReport) {
  if (!simulatorReport?.initial_state) return false;
  if (simulatorReport.final_state !== simulatorReport.initial_state) return false;
  if (simulatorReport.booted_by_case && simulatorReport.shut_down_again !== true) return false;
  // Settings no longer matters once the Simulator the case booted is shut down again.
  if (!simulatorReport.booted_by_case && !simulatorReport.settings_was_running &&
    simulatorReport.settings_closed !== true) return false;
  return true;
}

async function settingsRunning() {
  const listed = await simctl(["spawn", udid, "launchctl", "list"]);
  return listed.stdout.includes(SETTINGS);
}

/** Settings opened afresh at its top. */
async function freshSettings() {
  await simctl(["terminate", udid, SETTINGS]);
  const launched = await simctl(["launch", udid, SETTINGS]);
  if (launched.code !== 0) throw new Error(`simctl launch Settings failed: ${launched.stderr}`);
  await sleep(2000);
}

/**
 * Process ids of idb_companion processes a Canvas started for this Simulator: their socket
 * is in a private autonom-idb- folder, unlike the companions the idb CLI starts itself.
 */
async function canvasCompanions() {
  const listed = await run("pgrep", ["-fl", `idb_companion --udid ${udid} --grpc-domain-sock`]);
  return listed.stdout.split("\n").filter((line) => line.includes("/autonom-idb-"))
    .map((line) => Number(line.trim().split(/\s+/)[0])).filter(Boolean);
}

/** Process ids of the companion the idb CLI (and so the Autonom CLI) uses for this Simulator. */
async function cliCompanions() {
  const listed = await run("pgrep", ["-fl", `idb_companion --udid ${udid} --grpc-domain-sock /tmp/idb/`]);
  return listed.stdout.split("\n").filter((line) => line.includes("/tmp/idb/"))
    .map((line) => Number(line.trim().split(/\s+/)[0])).filter(Boolean);
}

/** Starts the idb CLI's companion with `idb connect` when none runs; records what it did. */
async function ensureCliCompanion(report) {
  const running = await cliCompanions();
  report.cli_companion = { started: false, pids: running };
  if (running.length) return;
  const connected = await run(args.idb, ["connect", udid], { timeout: 60_000 });
  const started = await cliCompanions();
  report.cli_companion = { started: started.length > 0, pids: started, connect_code: connected.code };
  if (!started.length) throw new Error(`idb connect started no companion: ${connected.stderr || connected.stdout}`);
}

/** Stops the companion ensureCliCompanion started (disconnect, then SIGTERM). */
async function stopCliCompanion(report) {
  const companion = report.cli_companion;
  if (!companion?.started) return;
  await run(args.idb, ["disconnect", udid], { timeout: 30_000 });
  for (const pid of companion.pids) {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // Already gone.
    }
  }
  await sleep(1000);
  const left = (await cliCompanions()).filter((pid) => companion.pids.includes(pid));
  companion.stopped = left.length === 0;
}

/**
 * Start this checkout's Canvas on the test Simulator with `--transport auto`; resolves once
 * it prints its URL. When it does not get that far, the Canvas is stopped (SIGTERM, then
 * SIGKILL) before the error is thrown, so it cannot outlive the case.
 */
async function startCanvas() {
  const token = randomBytes(18).toString("base64url");
  const argv = [CANVAS, "--platform", "ios", "--target", udid, "--simctl", args.xcrun, "--port", "0",
    "--token", token, "--transport", "auto", "--python", args.python];
  if (args["idb-companion"]) argv.push("--idb-companion", resolve(args["idb-companion"]));
  const child = spawn(process.execPath, argv, { cwd: ROOT, env: liveEnv, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  let url;
  try {
    url = await new Promise((resolvePromise, reject) => {
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
  } catch (error) {
    if (running()) {
      child.kill("SIGTERM");
      await Promise.race([exited, sleep(6000)]);
      if (running()) child.kill("SIGKILL");
    }
    throw error;
  }
  const origin = new URL(url).origin;
  const headers = { Authorization: `Bearer ${token}` };
  return {
    child, url, origin, token, exited, output: () => output,
    async status() {
      return await (await fetch(`${origin}/status`, { headers })).json();
    },
    async control(mode, from = "agent") {
      const response = await fetch(`${origin}/control`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json", "X-Autonom-Origin": from },
        body: JSON.stringify({ mode }),
      });
      return await response.json();
    },
    async stop() {
      if (!running()) return await exited;
      child.kill("SIGTERM");
      return await exited;
    },
  };
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

let browser = null;

/** How the browser runs, for the report. */
const browserMode = {
  headless: args.headless,
  args: args.headless ? [] : [OFF_SCREEN_WINDOW, "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling"],
};

async function openPage(canvas) {
  if (!browser) {
    const { chromium } = await loadPlaywright();
    browser = await chromium.launch({ headless: browserMode.headless, args: browserMode.args });
  }
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const opened = Date.now();
  await page.goto(canvas.url);
  await page.waitForFunction(() => {
    const value = window.autonomCanvas?.stats();
    return value && value.transport === "idb" && value.session === "streaming" && value.control === 1 &&
      value.framesDecoded > 0;
  }, null, { timeout: 30_000, polling: 50 });
  const first = await stats(page);
  const firstFrameMs = Date.now() - opened;
  await installSampler(page);
  return { page, firstFrameMs, first };
}

async function stats(page) {
  return await page.evaluate(() => window.autonomCanvas.stats());
}

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

/**
 * Installs window.liveSampler(band, width, height) in the page: `take()` returns a luma copy
 * of that band of the picture (0..1 of its height) scaled to width x height, and
 * `compare(a, b)` how many of its pixels differ by more than PICTURE_LEVEL (`moved`) and by
 * more than PICTURE_LOW_LEVEL (`movedLow`), with `changed` when at least
 * PICTURE_MOVED_PIXELS moved.
 */
async function installSampler(page) {
  await page.evaluate(({ level, lowLevel, movedPixels }) => {
    window.liveSampler = (band, width, height) => {
      const video = document.getElementById("video");
      const copy = document.createElement("canvas");
      copy.width = width;
      copy.height = height;
      const context = copy.getContext("2d");
      return {
        take() {
          const top = Math.round(video.height * band[0]);
          context.drawImage(video, 0, top, video.width, Math.round(video.height * band[1]) - top, 0, 0, width, height);
          const data = context.getImageData(0, 0, width, height).data;
          const luma = new Uint8Array(width * height);
          for (let i = 0, p = 0; p < luma.length; i += 4, p += 1) {
            luma[p] = Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]);
          }
          return luma;
        },
        compare(a, b) {
          let moved = 0;
          let movedLow = 0;
          for (let p = 0; p < a.length; p += 1) {
            const difference = Math.abs(a[p] - b[p]);
            if (difference > level) moved += 1;
            if (difference > lowLevel) movedLow += 1;
          }
          return { moved, movedLow, changed: moved >= movedPixels };
        },
      };
    };
  }, { level: PICTURE_LEVEL, lowLevel: PICTURE_LOW_LEVEL, movedPixels: PICTURE_MOVED_PIXELS });
}

/**
 * One measurement window in the page. The page's draw() is wrapped for the window: each
 * frame it presents is timed as it is drawn and its picture compared with the one before
 * (liveSampler over SIGNATURE_BAND); a presented frame whose picture changed is a distinct
 * frame. Animation frames are counted too. The number of pixels that moved per presented frame is kept for the report. The
 * stream packets reaching the decoder in the window are timed too, so the report can
 * estimate how often the Simulator itself changed the picture (packets above
 * REPEAT_PACKET_BYTES) next to how often the page showed a change.
 *
 * With `drive`, the fling driver runs on a SCROLL_STEP_MS timer (not tied to animation
 * frames) from before the window (until the picture first moves, then 300 ms more) to the
 * end of the stroke under way when it ends.
 */
async function measureWindow(page, durationMs, drive) {
  return await page.evaluate(async ({ durationMs, drive, band, from, to, every, stroke, stepMs, repeatBytes }) => {
    const canvas = window.autonomCanvas;
    const sampler = window.liveSampler(band, 64, 64);
    // The page script's own decode(), a global function, wrapped for the window only.
    const packets = [];
    const changedPackets = [];
    let measuring = false;
    const pageDecode = window.decode;
    window.decode = (key, pts, data) => {
      if (measuring) {
        const now = performance.now();
        packets.push(now);
        if (data.length > repeatBytes) changedPackets.push(now);
      }
      return pageDecode(key, pts, data);
    };
    const wait = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
    let driver = null;
    if (drive) {
      const begin = performance.now();
      let stopping = false;
      let down = null;
      let strokes = 0;
      let sent = 0;
      let finished;
      const done = new Promise((resolvePromise) => { finished = resolvePromise; });
      const timer = setInterval(() => {
        const elapsed = performance.now() - begin;
        const cycle = Math.floor(elapsed / every);
        const offset = elapsed - cycle * every;
        const [start, end] = cycle % 2 ? [to, from] : [from, to];
        if (down === null && offset < stroke) {
          if (stopping) {
            clearInterval(timer);
            finished();
            return;
          }
          down = { cycle, start, end };
          strokes += 1;
          canvas.send({ t: "touch", a: "down", id: 1, x: 0.5, y: start });
        } else if (down !== null && down.cycle === cycle && offset < stroke) {
          canvas.send({ t: "touch", a: "move", id: 1, x: 0.5, y: down.start + (down.end - down.start) * offset / stroke });
          if (measuring) sent += 1;
        } else if (down !== null) {
          canvas.send({ t: "touch", a: "move", id: 1, x: 0.5, y: down.end });
          canvas.send({ t: "touch", a: "up", id: 1, x: 0.5, y: down.end });
          down = null;
        }
      }, stepMs);
      driver = {
        async stop() {
          stopping = true;
          await done;
          return { strokes, moves: sent };
        },
      };
      // Until the list first moves (the first touches can take a while), at most 3 s.
      const still = sampler.take();
      const waitedFrom = performance.now();
      while (performance.now() - waitedFrom < 3000 && !sampler.compare(sampler.take(), still).changed) await wait(16);
      await wait(300);
    }
    const times = [];
    const distinct = [];
    const moved = [];
    const movedLow = [];
    let ticks = 0;
    const before = canvas.stats();
    let previous = sampler.take();
    // The page script's draw(), a global function scheduled by name, wrapped for the window.
    const pageDraw = window.draw;
    window.draw = () => {
      const rendered = canvas.stats().framesRendered;
      pageDraw();
      if (!measuring || canvas.stats().framesRendered === rendered) return;
      const now = performance.now();
      times.push(now);
      const current = sampler.take();
      const compared = sampler.compare(current, previous);
      moved.push(compared.moved);
      movedLow.push(compared.movedLow);
      if (compared.changed) distinct.push(now);
      previous = current;
    };
    const started = performance.now();
    measuring = true;
    await new Promise((resolvePromise) => {
      const tick = () => {
        ticks += 1;
        if (performance.now() - started < durationMs) requestAnimationFrame(tick);
        else resolvePromise();
      };
      requestAnimationFrame(tick);
    });
    const ended = performance.now();
    measuring = false;
    window.decode = pageDecode;
    window.draw = pageDraw;
    const after = canvas.stats();
    const driven = await driver?.stop() ?? { strokes: 0, moves: 0 };
    return {
      times, distinct, moved, movedLow, ticks, started, ended, stats: after, strokes: driven.strokes,
      moves: driven.moves, packets, changedPackets,
      decoded: after.framesDecoded - before.framesDecoded,
      dropped: after.framesDropped - before.framesDropped,
      rendered: after.framesRendered - before.framesRendered,
    };
  }, { durationMs, drive, band: SIGNATURE_BAND, from: SCROLL_FROM, to: SCROLL_TO, every: FLING_EVERY_MS,
    stroke: STROKE_MS, stepMs: SCROLL_STEP_MS, repeatBytes: REPEAT_PACKET_BYTES });
}

/**
 * Waits until the picture has not changed (liveSampler over the whole picture) for
 * STILL_MS; resolves with how long that took, or null after STILL_WAIT_MAX_MS.
 */
async function waitStill(page) {
  return await page.evaluate(async ({ stillMs, maxMs }) => {
    const sampler = window.liveSampler([0, 1], 64, 140);
    const started = performance.now();
    let previous = sampler.take();
    let stillSince = started;
    return await new Promise((resolvePromise) => {
      const tick = () => {
        const now = performance.now();
        const current = sampler.take();
        if (sampler.compare(current, previous).changed) stillSince = now;
        previous = current;
        if (now - stillSince >= stillMs) resolvePromise(Math.round(now - started));
        else if (now - started >= maxMs) resolvePromise(null);
        else requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }, { stillMs: STILL_MS, maxMs: STILL_WAIT_MAX_MS });
}

function percentile(sorted, share) {
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(share * sorted.length) - 1));
  return Math.round(sorted[index] * 100) / 100;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
}

/** fps per whole second of the measurement and frame interval percentiles. */
function frameReport(times, { started, ended }) {
  const perSecond = [];
  for (let second = 0; started + (second + 1) * 1000 <= ended; second += 1) {
    const from = started + second * 1000;
    perSecond.push(times.filter((time) => time >= from && time < from + 1000).length);
  }
  const intervals = times.slice(1).map((time, index) => time - times[index]).sort((a, b) => a - b);
  return {
    frames: times.length,
    seconds: Math.round((ended - started) / 10) / 100,
    fps_mean: Math.round((times.length / ((ended - started) / 1000)) * 10) / 10,
    fps_per_second: perSecond,
    fps_median: median(perSecond),
    interval_ms: {
      p50: percentile(intervals, 0.5), p90: percentile(intervals, 0.9), p95: percentile(intervals, 0.95),
      p99: percentile(intervals, 0.99), max: intervals.length ? Math.round(intervals.at(-1) * 100) / 100 : null,
    },
  };
}

/** Percentiles of a list of counts, and how many of them are zero. */
function spread(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return { zero: sorted.filter((value) => value === 0).length, p10: percentile(sorted, 0.1),
    p25: percentile(sorted, 0.25), p50: percentile(sorted, 0.5), p90: percentile(sorted, 0.9) };
}

/** One window measured by measureWindow, with the host CPU load over it, for the report. */
function windowReport(measured, cpuBusyPct) {
  const seconds = (measured.ended - measured.started) / 1000;
  const rate = (count) => Math.round((count / seconds) * 10) / 10;
  const presented = frameReport(measured.times, measured);
  const distinct = frameReport(measured.distinct, measured);
  const changed = frameReport(measured.changedPackets, measured);
  return {
    seconds: presented.seconds,
    strokes: measured.strokes,
    // Finger moves the driver sent in the window, and HID events the Canvas sent the companion
    // (counted from just before to just after the window).
    moves_fps: rate(measured.moves),
    hid_events_fps: measured.hidEvents === null ? null : rate(measured.hidEvents),
    decoded: measured.decoded,
    dropped: measured.dropped,
    rendered: measured.rendered,
    decoded_fps: rate(measured.decoded),
    dropped_fps: rate(measured.dropped),
    presented,
    distinct: { frames: distinct.frames, fps_mean: distinct.fps_mean, fps_per_second: distinct.fps_per_second,
      fps_median: distinct.fps_median, fps_min: distinct.fps_per_second.length ? Math.min(...distinct.fps_per_second) : null },
    moved_pixels: spread(measured.moved),
    moved_pixels_low_level: spread(measured.movedLow),
    // In presentation order, to tell a Simulator that renders fewer frames (a regular
    // pattern) from a list that stops at its end (runs of zeros).
    moved_pixels_sequence: measured.moved,
    stream: { packets_fps: rate(measured.packets.length), changed_fps: changed.fps_mean,
      changed_fps_per_second: changed.fps_per_second, changed_frames: changed.frames },
    shown_share_of_changes: changed.frames ? Math.round((distinct.frames / changed.frames) * 1000) / 1000 : null,
    animation_frames_fps: rate(measured.ticks),
    host_cpu_busy_pct: cpuBusyPct,
  };
}

async function measuredWindow(canvas, page, durationMs, drive) {
  const hidBefore = (await canvas.status()).idb?.hid_events;
  const before = cpus();
  const measured = await measureWindow(page, durationMs, drive);
  const busy = cpuBusy(before, cpus());
  const hidAfter = (await canvas.status()).idb?.hid_events;
  measured.hidEvents = Number.isInteger(hidBefore) && Number.isInteger(hidAfter) ? hidAfter - hidBefore : null;
  return { measured, report: windowReport(measured, busy) };
}

/**
 * SSIM of the page's current picture against a simctl screenshot, on luma, in 8x8 windows
 * every 4 pixels (as ffmpeg's ssim filter does). Both are compared in the page, so the
 * browser decodes the PNG with its own colour management.
 *
 * The screenshot is taken with `--mask=alpha`: the parts of the framebuffer the device
 * hides (its rounded corners and the camera cutout) are transparent in it. The companion's
 * stream draws the cutout as a black pill the screenshot does not have, which alone takes
 * SSIM from about 0.999 to about 0.986 on Settings, so windows with any transparent
 * reference pixel are left out; the report gives their share (`masked_share`).
 */
async function settledSsim(page) {
  const shot = join(outDir, "settled-simctl.png");
  const taken = await simctl(["io", udid, "screenshot", "--type=png", "--mask=alpha", shot]);
  if (taken.code !== 0) throw new Error(`simctl screenshot failed: ${taken.stderr}`);
  const reference = `data:image/png;base64,${(await readFile(shot)).toString("base64")}`;
  const result = await page.evaluate(async (referenceUrl) => {
    const live = document.getElementById("video");
    const width = live.width;
    const height = live.height;
    const liveData = live.getContext("2d").getImageData(0, 0, width, height).data;
    const image = new Image();
    image.src = referenceUrl;
    await image.decode();
    const scratch = document.createElement("canvas");
    scratch.width = width;
    scratch.height = height;
    const context = scratch.getContext("2d");
    context.drawImage(image, 0, 0, width, height);
    const refData = context.getImageData(0, 0, width, height).data;
    const luma = (data) => {
      const out = new Float32Array(width * height);
      for (let i = 0, p = 0; p < out.length; i += 4, p += 1) out[p] = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      return out;
    };
    const a = luma(liveData);
    const b = luma(refData);
    const c1 = (0.01 * 255) ** 2;
    const c2 = (0.03 * 255) ** 2;
    // 1 where the reference pixel is shown on the device (fully opaque in the masked shot).
    const shown = new Uint8Array(width * height);
    for (let i = 3, p = 0; p < shown.length; i += 4, p += 1) shown[p] = refData[i] === 255 ? 1 : 0;
    let sum = 0;
    let windows = 0;
    let masked = 0;
    for (let y = 0; y + 8 <= height; y += 4) {
      for (let x = 0; x + 8 <= width; x += 4) {
        let hidden = false;
        for (let dy = 0; dy < 8 && !hidden; dy += 1) {
          const row = (y + dy) * width + x;
          for (let dx = 0; dx < 8; dx += 1) if (!shown[row + dx]) { hidden = true; break; }
        }
        if (hidden) {
          masked += 1;
          continue;
        }
        let sa = 0; let sb = 0; let saa = 0; let sbb = 0; let sab = 0;
        for (let dy = 0; dy < 8; dy += 1) {
          let p = (y + dy) * width + x;
          for (let dx = 0; dx < 8; dx += 1, p += 1) {
            const va = a[p];
            const vb = b[p];
            sa += va; sb += vb; saa += va * va; sbb += vb * vb; sab += va * vb;
          }
        }
        const ma = sa / 64;
        const mb = sb / 64;
        const va = saa / 64 - ma * ma;
        const vb = sbb / 64 - mb * mb;
        const cov = sab / 64 - ma * mb;
        sum += ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2));
        windows += 1;
      }
    }
    const pngUrl = live.toDataURL("image/png");
    return { ssim: windows ? sum / windows : 0, width, height, windows, masked_share: masked / (windows + masked),
      reference: { width: image.naturalWidth, height: image.naturalHeight }, pngUrl };
  }, reference);
  await writeFile(join(outDir, "settled-page.png"), Buffer.from(result.pngUrl.split(",")[1], "base64"));
  delete result.pngUrl;
  return { ...result, ssim: Math.round(result.ssim * 10000) / 10000,
    masked_share: Math.round(result.masked_share * 10000) / 10000, screenshot: shot };
}

// Why `ui tree` calls failed, for the report (the last few, cut short). A call made after
// typing can echo the screen, so the typed words are taken out of what is kept.
const uiTreeErrors = [];

/** The compact UI tree nodes from the Autonom CLI; null when it failed. */
async function uiNodes() {
  const tree = await autonom(["ui", "tree", "--max-nodes", "400"]);
  const failed = (why) => {
    // Taken out before the cut, so a word cut in half at the end cannot slip through.
    const output = withoutTypedText((tree.stderr || tree.stdout).trim(), [SEARCH_TEXT, CLIP_TEXT]);
    uiTreeErrors.push(`${why}: ${output.slice(0, 300)}`);
    if (uiTreeErrors.length > 5) uiTreeErrors.shift();
    return null;
  };
  if (tree.code !== 0) return failed(`exit ${tree.code}`);
  try {
    return JSON.parse(tree.stdout).nodes ?? failed("no nodes");
  } catch {
    return failed("not JSON");
  }
}

const label = (node) => `${node.desc ?? ""} ${node.text ?? ""}`.trim();

/**
 * The UI tree once a node matching `predicate` is in it (a freshly launched Settings can take
 * seconds to show its rows on a busy host), or the last tree read after `timeoutMs`.
 */
async function nodesWith(predicate, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  let nodes = null;
  do {
    nodes = await uiNodes();
    if (findNode(nodes, predicate)) return nodes;
    await sleep(500);
  } while (Date.now() < deadline);
  return nodes;
}

function findNode(nodes, predicate) {
  return (nodes ?? []).find((node) => node.bounds && predicate(node)) ?? null;
}

/** A node's centre as 0..1 page coordinates of the portrait screen in points. */
function centre(node, points) {
  const [x1, y1, x2, y2] = node.bounds;
  return { x: ((x1 + x2) / 2) / points.width, y: ((y1 + y2) / 2) / points.height };
}

async function tapAt(page, at) {
  await page.evaluate(async ({ x, y }) => {
    window.autonomCanvas.send({ t: "touch", a: "down", id: 9, x, y });
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 80));
    window.autonomCanvas.send({ t: "touch", a: "up", id: 9, x, y });
  }, at);
}

/**
 * A finger put down and held: first still for 1 s, then dragged for 1 s, then held still
 * again and left down (the caller lifts it, by a takeover). Every animation frame of both
 * 1 s phases a 36x80 luma copy of the whole picture (liveSampler) counts as changed when
 * it changed from the last one, so the dragged phase can be compared with the still one.
 */
async function heldDrag(page) {
  return await page.evaluate(async () => {
    const canvas = window.autonomCanvas;
    const sampler = window.liveSampler([0, 1], 36, 80);
    const wait = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
    const rendered = () => canvas.stats().framesRendered;
    // Samples every animation frame until `phase` resolves.
    const sample = async (phase) => {
      let moving = true;
      let samples = 0;
      let changed = 0;
      let previous = sampler.take();
      const renderedBefore = rendered();
      const sampling = new Promise((resolvePromise) => {
        const tick = () => {
          const current = sampler.take();
          samples += 1;
          if (sampler.compare(current, previous).changed) changed += 1;
          previous = current;
          if (moving) requestAnimationFrame(tick);
          else resolvePromise();
        };
        requestAnimationFrame(tick);
      });
      await phase();
      moving = false;
      await sampling;
      return { samples, changed, rendered: rendered() - renderedBefore };
    };
    canvas.send({ t: "touch", a: "down", id: 3, x: 0.5, y: 0.7 });
    await wait(300);
    const still = await sample(() => wait(1000));
    const dragged = await sample(async () => {
      const started = performance.now();
      for (let i = 1; performance.now() - started < 1000; i += 1) {
        canvas.send({ t: "touch", a: "move", id: 3, x: 0.5, y: Math.max(0.45, 0.7 - i * 0.004) });
        await wait(16);
      }
    });
    await wait(400);
    return {
      still: { samples: still.samples, changed: still.changed, rendered: still.rendered },
      samples: dragged.samples, changed: dragged.changed, rendered_while_moving: dragged.rendered,
    };
  });
}

/** `sim_volume` of the test Simulator (read only), or null when it cannot be read. */
async function simVolume() {
  const read = await run("plutil", ["-extract", "sim_volume", "raw", "-o", "-", AUDIO_SETTINGS]);
  const value = Number(read.stdout.trim());
  return read.code === 0 && read.stdout.trim() !== "" && Number.isFinite(value) ? value : null;
}

/** Whether `value` is a level the Simulator writes (floor(k * 6.25)), not its boot placeholder. */
function volumeOnGrid(value) {
  return value !== null && Array.from({ length: 17 }, (_, k) => Math.floor(k * 6.25)).includes(value);
}

/** The Mac's output volume (read only), to show the Simulator buttons leave it alone. */
async function hostVolume() {
  const read = await run("osascript", ["-e", "output volume of (get volume settings)"]);
  return read.code === 0 ? read.stdout.trim() : null;
}

/** `xcrun simctl <argv>` with `input` on stdin; resolves with its exit code only. */
function simctlWithInput(argv, input) {
  return new Promise((resolvePromise) => {
    const child = spawn(args.xcrun, ["simctl", ...argv], { env: liveEnv, stdio: ["pipe", "ignore", "ignore"] });
    child.on("error", () => resolvePromise(1));
    child.on("close", (code) => resolvePromise(code ?? 1));
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

/**
 * The Canvas control socket opened with the token as an agent. `next` resolves with the first
 * message (seen so far or later) that matches; replies are kept only in memory.
 */
async function controlSocket(canvas) {
  const url = `${canvas.origin.replace(/^http/, "ws")}/ws/control?token=${encodeURIComponent(canvas.token)}&origin=agent`;
  const ws = new WebSocket(url);
  const messages = [];
  const waiters = new Set();
  ws.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    messages.push(message);
    for (const waiter of [...waiters]) {
      if (!waiter.predicate(message)) continue;
      waiters.delete(waiter);
      clearTimeout(waiter.timer);
      waiter.resolve(message);
    }
  };
  await new Promise((resolvePromise, reject) => {
    ws.onopen = resolvePromise;
    ws.onerror = () => reject(new Error("the control socket did not open"));
  });
  let pings = 0;
  const socket = {
    send: (message) => ws.send(JSON.stringify(message)),
    next(predicate, timeoutMs = 10_000, what = "a control message", { since = 0 } = {}) {
      const found = messages.slice(since).find(predicate);
      if (found) return Promise.resolve(found);
      return new Promise((resolvePromise, reject) => {
        const waiter = { predicate, resolve: resolvePromise };
        waiter.timer = setTimeout(() => {
          waiters.delete(waiter);
          reject(new Error(`timed out waiting for ${what}`));
        }, timeoutMs);
        waiters.add(waiter);
      });
    },
    /** Resolves once every message sent before it was handled (messages run in order). */
    async handled(timeoutMs = 30_000) {
      const ts = -(++pings);
      const since = messages.length;
      socket.send({ t: "ping", ts });
      await socket.next((message) => message.t === "pong" && message.ts === ts, timeoutMs, "the pong", { since });
    },
    mark: () => messages.length,
    // Error replies only: they name what failed, never clipboard text.
    errors: () => messages.filter((message) => message.t === "error").map((message) => ({ for: message.for, message: message.message })),
    close: () => ws.close(),
  };
  return socket;
}

/** Canvas journal entries (verbs and details) of the run's session; only lengths of text. */
async function canvasJournal() {
  const listed = await autonom(["journal", "--max", "200"]);
  const journal = JSON.parse(listed.stdout || "{}");
  const all = (journal.entries ?? []).filter((entry) => entry.verb?.startsWith("ui "));
  const allDetails = await Promise.all(all.map(async (entry) => entry.result?.detail
    ? JSON.parse(await readFile(join(dirname(journal.journal), entry.result.detail), "utf8").catch(() => "null"))
    : null));
  const entries = all.filter((_, index) => allDetails[index]?.canvas === true);
  const details = allDetails.filter((detail) => detail?.canvas === true);
  return { entries, details };
}

/** Mark `report.ok` from its checks: every check passes and the case threw nothing. */
function settle(report) {
  report.ok = !report.error && Boolean(report.checks) && Object.values(report.checks).every(Boolean);
}

/** Companions this run started that are still running after the Canvas stopped: stop them. */
async function stopLeftCompanions(report, companionsBefore) {
  const left = (await canvasCompanions()).filter((pid) => !companionsBefore.includes(pid));
  for (const pid of left) {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      // Already gone.
    }
  }
  if (left.length) report.left_companions_stopped = left;
}

const cases = {
  async video(report) {
    report.host = { cpus: cpus().length, loadavg_start: hostLoad() };
    report.browser = browserMode;
    await prepareSimulator(report);
    const companionsBefore = await canvasCompanions();
    report.companions_before = companionsBefore.length;
    let canvas = null;
    let companionPid = null;
    try {
      canvas = await startCanvas();
      report.transport = (await canvas.status()).transport ?? null;
      await freshSettings();
      const first = await openPage(canvas);
      report.first_tab = { first_frame_ms: first.firstFrameMs, codec: first.first.codec, mode: first.first.mode };
      await sleep(1000);
      report.host.loadavg_before_windows = hostLoad();
      report.static_window = (await measuredWindow(canvas, first.page, STATIC_MS, false)).report;
      const scrolled = await measuredWindow(canvas, first.page, seconds * 1000, true);
      report.scrolling = scrolled.report;
      report.host.loadavg_after_windows = hostLoad();
      report.page = {
        width: scrolled.measured.stats.width, height: scrolled.measured.stats.height,
        decoded_total: scrolled.measured.stats.framesDecoded, dropped_total: scrolled.measured.stats.framesDropped,
        rendered_total: scrolled.measured.stats.framesRendered, errors: scrolled.measured.stats.errors,
        mode: scrolled.measured.stats.mode,
      };
      const stillAfterMs = await waitStill(first.page);
      await sleep(SETTLE_MS);
      report.quality = { still_after_ms: stillAfterMs, ...await settledSsim(first.page) };
      const before = await canvas.status();
      const joining = await openPage(canvas);
      report.second_tab = { first_frame_ms: joining.firstFrameMs };
      await sleep(1000);
      const status = await canvas.status();
      companionPid = status.idb?.companion_pid ?? null;
      const running = await canvasCompanions();
      report.stream = {
        transport: status.transport, video_clients: status.idb?.video_clients, streams_opened: status.idb?.streams_opened,
        stream_restarts: status.idb?.stream_restarts, forced_key_frames: status.idb?.forced_key_frames,
        companion_starts: status.idb?.companion_starts, restarts: status.idb?.restarts,
        streams_opened_before_join: before.idb?.streams_opened, key_frames: status.idb?.key_frames,
        packets: status.idb?.packets, companions_running: running.filter((pid) => !companionsBefore.includes(pid)).length,
      };
    } finally {
      report.canvas_exit = canvas ? await canvas.stop() : null;
    }
    await sleep(1000);
    const after = await canvasCompanions();
    report.after_stop = {
      companion_alive: companionPid !== null && after.includes(companionPid),
      companions_left: after.filter((pid) => !companionsBefore.includes(pid)).length,
    };
    await stopLeftCompanions(report, companionsBefore);
    report.host.loadavg_end = hostLoad();
    const scrolling = report.scrolling;
    const still = report.static_window;
    report.checks = {
      transport_idb: report.transport === "idb" && report.stream.transport === "idb",
      fps_median: scrolling.presented.fps_median !== null && scrolling.presented.fps_median >= FPS_BAR,
      // The screen really changed through the whole scroll window.
      screen_changing: scrolling.distinct.fps_median !== null && scrolling.distinct.fps_median >= DISTINCT_MEDIAN_BAR &&
        scrolling.distinct.fps_min !== null && scrolling.distinct.fps_min >= DISTINCT_SECOND_BAR,
      static_control: still.distinct.fps_per_second.length > 0 && still.distinct.frames === 0,
      full_resolution: report.page.width === 1206 && report.page.height === 2622,
      webcodecs: report.page.mode === "webcodecs",
      ssim: report.quality.ssim >= SSIM_BAR && report.quality.masked_share <= 0.05,
      second_tab_in_time: report.second_tab.first_frame_ms <= JOIN_BAR_MS,
      // A key frame forced by Stop and Start on the same stream is not a second stream.
      one_stream: report.stream.streams_opened - report.stream.stream_restarts === 1 &&
        report.stream.video_clients === 2 && report.stream.companion_starts === 1 && report.stream.companions_running === 1,
      nothing_left: report.canvas_exit === 0 && !report.after_stop.companion_alive && report.after_stop.companions_left === 0,
    };
  },

  async input(report) {
    report.host = { cpus: cpus().length, loadavg_start: hostLoad() };
    report.browser = browserMode;
    await prepareSimulator(report);
    await ensureCliCompanion(report);
    const started = await autonom(["session", "start"]);
    if (started.code !== 0) throw new Error(`autonom session start failed: ${started.stderr || started.stdout}`);
    report.session_id = JSON.parse(started.stdout).session?.session_id ?? null;
    const companionsBefore = await canvasCompanions();
    let canvas = null;
    let companionPid = null;
    try {
      canvas = await startCanvas();
      report.transport = (await canvas.status()).transport ?? null;
      await freshSettings();
      const { page } = await openPage(canvas);
      const points = (await canvas.status()).idb?.points ?? { width: 402, height: 874 };
      report.points = points;
      companionPid = (await canvas.status()).idb?.companion_pid ?? null;

      // Held still, then dragged, then taken over by an agent while still held.
      report.held_drag = await heldDrag(page);
      const hidHeld = (await canvas.status()).idb?.hid_events;
      const taken = await canvas.control("takeover");
      await sleep(500);
      const hidLifted = (await canvas.status()).idb?.hid_events;
      const refused = await page.evaluate(async () => {
        window.autonomCanvas.send({ t: "touch", a: "move", id: 3, x: 0.5, y: 0.6 });
        window.autonomCanvas.send({ t: "touch", a: "up", id: 3, x: 0.5, y: 0.6 });
        window.autonomCanvas.send({ t: "touch", a: "down", id: 5, x: 0.5, y: 0.5 });
        window.autonomCanvas.send({ t: "touch", a: "up", id: 5, x: 0.5, y: 0.5 });
        window.autonomCanvas.send({ t: "system", op: "home" });
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 800));
        return window.autonomCanvas.stats().note;
      });
      const hidAfter = (await canvas.status()).idb?.hid_events;
      const released = await canvas.control("release");
      report.takeover = {
        owner: taken.control_owner, note: refused, hid_events_held: hidHeld, hid_events_lifted: hidLifted,
        hid_events_after: hidAfter, released_owner: released.control_owner,
        // The takeover sent exactly one HID event, the UP of the held finger.
        held_finger_lifted: Number.isInteger(hidHeld) && hidLifted === hidHeld + 1,
        refused: taken.control_owner === "agent" && hidAfter === hidLifted && /owned by agent/.test(refused ?? ""),
      };

      await freshSettings();
      const isGeneral = (node) => /^general$/i.test(node.desc ?? node.text ?? "");
      const settings = await nodesWith(isGeneral);
      const general = findNode(settings, isGeneral);
      report.tap = { target_found: Boolean(general) };
      // What the screen showed instead (Settings labels only, nothing typed).
      if (!general) report.tap.seen = (settings ?? []).slice(0, 30).map((node) => `${node.role ?? ""}:${label(node)}`);
      if (general) {
        await tapAt(page, centre(general, points));
        await sleep(1500);
        const inGeneral = (node) => /^about$/i.test(label(node)) || (node.role === "button" && /^settings$/i.test(node.desc ?? ""));
        report.tap.opened = Boolean(findNode(await nodesWith(inGeneral, 10_000), inGeneral));
      }

      await page.evaluate(() => window.autonomCanvas.send({ t: "system", op: "home" }));
      await sleep(2000);
      const home = await uiNodes();
      report.home = {
        left_settings: Array.isArray(home) &&
          !findNode(home, (node) => /^(general|about)$/i.test(label(node))),
      };

      await freshSettings();
      // The field itself, not the Settings row named Search above it.
      const isField = (node) => ["searchfield", "textfield", "search"].includes(node.role);
      const searchNodes = await nodesWith(isField);
      const field = findNode(searchNodes, isField) ??
        findNode(searchNodes, (node) => node.role !== "button" && /^search$/i.test(label(node)));
      report.text = { field_found: Boolean(field), text_len: SEARCH_TEXT.length };
      // A copy taken before typing: errors of the reads that follow stay out of the report.
      report.ui_tree_errors = [...uiTreeErrors];
      if (field) {
        await tapAt(page, centre(field, points));
        await sleep(1200);
        await page.evaluate((text) => window.autonomCanvas.send({ t: "text", text }), SEARCH_TEXT);
        report.text.typed = Boolean(await waitFor(async () => {
          const nodes = await uiNodes();
          return findNode(nodes, (node) => String(node.text ?? "").toLowerCase().includes(SEARCH_TEXT)) ? true : null;
        }, 15_000, "the typed text in the search field").catch(() => false));
      }

      await sleep(1500);
      const listed = await autonom(["journal", "--max", "200"]);
      const journal = JSON.parse(listed.stdout || "{}");
      // Only the Canvas's own entries: the `ui tree` calls of this case may be journaled too.
      const all = (journal.entries ?? []).filter((entry) => entry.verb?.startsWith("ui "));
      const allDetails = await Promise.all(all.map(async (entry) => entry.result?.detail
        ? JSON.parse(await readFile(join(dirname(journal.journal), entry.result.detail), "utf8").catch(() => "null"))
        : null));
      const entries = all.filter((_, index) => allDetails[index]?.canvas === true);
      const details = allDetails.filter((detail) => detail?.canvas === true);
      const verbs = {};
      for (const entry of entries) verbs[entry.verb] = (verbs[entry.verb] ?? 0) + 1;
      report.journal = {
        verbs,
        origins: [...new Set(entries.map((entry) => entry.origin))],
        // Only lengths of typed text go into the report.
        details: details.map((detail) => detail && { kind: detail.kind, transport: detail.transport ?? null,
          origin: detail.origin, text_len: detail.text_len ?? undefined, moves: detail.moves ?? undefined, op: detail.op ?? undefined }),
      };
      // The held drag (lifted by the takeover), the tap on General and the tap on the search
      // field are one gesture each; Home one system action; the text one text action.
      report.journal.expected_gestures = 1 + (report.tap.target_found ? 1 : 0) + (report.text.field_found ? 1 : 0);
      report.journal.ok = (verbs["ui gesture"] ?? 0) === report.journal.expected_gestures && verbs["ui system"] === 1 &&
        (verbs["ui text"] ?? 0) === (report.text.field_found ? 1 : 0) &&
        report.journal.origins.every((origin) => origin === "human") &&
        details.every((detail) => detail?.transport === "idb");
    } finally {
      report.canvas_exit = canvas ? await canvas.stop() : null;
      const stopped = await autonom(["session", "stop"]);
      report.session_stopped = stopped.code === 0;
      await stopCliCompanion(report);
    }
    await sleep(1000);
    const after = await canvasCompanions();
    report.after_stop = {
      companion_alive: companionPid !== null && after.includes(companionPid),
      companions_left: after.filter((pid) => !companionsBefore.includes(pid)).length,
    };
    await stopLeftCompanions(report, companionsBefore);
    report.host.loadavg_end = hostLoad();
    const held = report.held_drag;
    report.checks = {
      transport_idb: report.transport === "idb",
      held_drag_changes_frames: held.changed >= HELD_CHANGES_BAR && held.changed >= held.still.changed + HELD_CHANGES_BAR,
      takeover_lifts_held_finger: report.takeover.held_finger_lifted === true,
      takeover_refuses_human_input: report.takeover.refused === true,
      tap_opens_row: report.tap.opened === true,
      home_leaves_settings: report.home.left_settings === true,
      text_typed: report.text.typed === true,
      journal: report.journal.ok === true,
      nothing_left: report.canvas_exit === 0 && !report.after_stop.companion_alive && report.after_stop.companions_left === 0 &&
        (!report.cli_companion?.started || report.cli_companion.stopped === true),
    };
  },
};

cases.controls = async function controls(report) {
  report.host = { cpus: cpus().length, loadavg_start: hostLoad(), output_volume_before: await hostVolume() };
  await prepareSimulator(report);
  await ensureCliCompanion(report);
  const started = await autonom(["session", "start"]);
  if (started.code !== 0) throw new Error(`autonom session start failed: ${started.stderr || started.stdout}`);
  report.session_id = JSON.parse(started.stdout).session?.session_id ?? null;
  // The pasteboard as found, kept in memory only and put back at the end.
  const original = await simctl(["pbpaste", udid]);
  const companionsBefore = await canvasCompanions();
  let canvas = null;
  let control = null;
  let companionPid = null;
  try {
    canvas = await startCanvas();
    report.transport = (await canvas.status()).transport ?? null;
    await freshSettings();
    control = await controlSocket(canvas);
    const state = await control.next((message) => message.t === "state" && message.session === "streaming", 30_000,
      "a streaming session");
    report.clipboard_available = state.clipboard === true;
    companionPid = (await canvas.status()).idb?.companion_pid ?? null;

    // The Simulator writes sim_volume as floor(k * 6.25), k = 0..16, once its audio service
    // runs; until then (some seconds after boot) the file holds a placeholder (60) and button
    // presses change the level without writing it. So while the value is off that grid,
    // Volume up then Volume down (no net change) go first until the file shows the level.
    // Its audio service starts some time after boot, so a Simulator this run booted gets
    // VOLUME_SETTLE_MS from the end of boot before any press.
    if (bootedAt !== null) await sleep(Math.max(0, bootedAt + VOLUME_SETTLE_MS - Date.now()));
    const priming = [];
    for (let attempt = 0; attempt < 8 && !volumeOnGrid(await simVolume()); attempt += 1) {
      if (attempt) await sleep(3000);
      for (const op of ["volume-up", "volume-down"]) {
        control.send({ t: "system", op });
        priming.push(op);
        await sleep(600);
      }
      await waitFor(async () => (volumeOnGrid(await simVolume()) ? true : null), 2500, "sim_volume").catch(() => null);
    }
    // The start counts once the value has held still for a moment (a late write would move it).
    for (let previous = null, value = await simVolume(), checks = 0; previous !== value && checks < 10; checks += 1) {
      await sleep(1500);
      previous = value;
      value = await simVolume();
    }
    // Volume up, then down: the Simulator's own volume rises and comes back.
    const start = await simVolume();
    control.send({ t: "system", op: "volume-up" });
    const afterUp = await waitFor(async () => {
      const value = await simVolume();
      return value !== null && start !== null && value > start ? { value } : null;
    }, 5000, "sim_volume to rise").catch(() => null);
    control.send({ t: "system", op: "volume-down" });
    const afterDown = await waitFor(async () => {
      const value = await simVolume();
      return value !== null && value === start ? { value } : null;
    }, 5000, "sim_volume to return").catch(() => null);
    report.volume = {
      plist: AUDIO_SETTINGS, priming, start, after_up: afterUp?.value ?? await simVolume(), after_down: afterDown?.value ?? await simVolume(),
    };
    report.volume.rose = afterUp !== null;
    report.volume.returned = afterDown !== null;

    // The same through HTTP POST /key with the Android names: the action bridge presses the
    // Simulator's volume buttons with `idb ui button`.
    const bridgeStart = await simVolume();
    const pressKey = async (key) => {
      const response = await fetch(`${canvas.origin}/key`, {
        method: "POST",
        headers: { Authorization: `Bearer ${canvas.token}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
        body: JSON.stringify({ key }),
      });
      return { status: response.status, body: await response.json().catch(() => null) };
    };
    const keyUp = await pressKey("KEYCODE_VOLUME_UP");
    const bridgeUp = await waitFor(async () => {
      const value = await simVolume();
      return value !== null && bridgeStart !== null && value > bridgeStart ? { value } : null;
    }, 5000, "sim_volume to rise through the bridge").catch(() => null);
    const keyDown = await pressKey("KEYCODE_VOLUME_DOWN");
    const bridgeDown = await waitFor(async () => {
      const value = await simVolume();
      return value !== null && value === bridgeStart ? { value } : null;
    }, 5000, "sim_volume to return through the bridge").catch(() => null);
    report.bridge_volume = {
      start: bridgeStart, up: { status: keyUp.status, key: keyUp.body?.key ?? null, error: keyUp.body?.error },
      down: { status: keyDown.status, key: keyDown.body?.key ?? null, error: keyDown.body?.error },
      after_up: bridgeUp?.value ?? null, after_down: bridgeDown?.value ?? await simVolume(),
    };
    report.bridge_volume.ok = keyUp.status === 200 && keyDown.status === 200 && bridgeUp !== null && bridgeDown !== null;

    // Paste the test string, then read the clipboard back; only the match is reported.
    const pasteMark = control.mark();
    control.send({ t: "paste", text: CLIP_TEXT });
    const pasted = await control.next((message) => message.t === "paste" || (message.t === "error" && message.for === "paste"),
      30_000, "the paste reply", { since: pasteMark });
    const readMark = control.mark();
    control.send({ t: "clipboard-get" });
    const read = await control.next((message) => message.t === "clipboard" ||
      (message.t === "error" && message.for === "clipboard-get"), 15_000, "the clipboard reply", { since: readMark });
    report.clipboard = {
      text_len: CLIP_TEXT.length, paste_reply: pasted.t, typed: pasted.typed ?? null,
      paste_error: pasted.t === "error" ? pasted.message : undefined,
      read_reply: read.t, read_error: read.t === "error" ? read.message : undefined,
      read_back_matches: read.t === "clipboard" && read.text === CLIP_TEXT,
    };

    // A mirrored pinch, as the page sends a Ctrl-drag (the mirror first), then a tap.
    await control.handled();
    await sleep(500);
    const hidBefore = (await canvas.status()).idb?.hid_events;
    control.send({ t: "touch", a: "down", id: -1001, x: 0.6, y: 0.55 });
    control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.45 });
    for (let step = 1; step <= 20; step += 1) {
      const d = 0.1 + step * 0.006;
      control.send({ t: "touch", a: "move", id: -1001, x: 0.5 + d, y: 0.5 + d / 2 });
      control.send({ t: "touch", a: "move", id: 1, x: 0.5 - d, y: 0.5 - d / 2 });
      await sleep(16);
    }
    await control.handled();
    const hidBeforeRelease = (await canvas.status()).idb?.hid_events;
    control.send({ t: "touch", a: "up", id: -1001, x: 0.62, y: 0.56 });
    control.send({ t: "touch", a: "up", id: 1, x: 0.38, y: 0.44 });
    await control.handled();
    await sleep(2500);
    const hidAfterPinch = (await canvas.status()).idb?.hid_events;
    // The status bar: a tap there only scrolls a list to its top.
    control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.03 });
    await sleep(80);
    control.send({ t: "touch", a: "up", id: 2, x: 0.5, y: 0.03 });
    await control.handled();
    await sleep(800);
    const after = await canvas.status();
    report.pinch = {
      hid_events_before: hidBefore, hid_events_before_release: hidBeforeRelease, hid_events_after_pinch: hidAfterPinch,
      hid_events_after_tap: after.idb?.hid_events, last_error: after.last_error ?? null,
    };
    report.pinch.nothing_before_release = Number.isInteger(hidBefore) && hidBeforeRelease === hidBefore;
    report.pinch.one_event_at_release = Number.isInteger(hidBefore) && hidAfterPinch === hidBefore + 1;
    report.pinch.tap_after = Number.isInteger(hidAfterPinch) && after.idb?.hid_events === hidAfterPinch + 2 &&
      !/idb hid/.test(after.last_error ?? "");
    report.control_errors = control.errors();

    await sleep(1500);
    const { entries, details } = await canvasJournal();
    const verbs = {};
    for (const entry of entries) verbs[entry.verb] = (verbs[entry.verb] ?? 0) + 1;
    const gestures = details.filter((detail) => detail?.kind === "gesture");
    report.journal = {
      verbs,
      origins: [...new Set(entries.map((entry) => entry.origin))],
      gestures: gestures.map((detail) => ({ pointers: detail.pointers, moves: detail.moves, transport: detail.transport })),
      // Only lengths: a paste record never holds its text.
      pastes: details.filter((detail) => detail?.kind === "paste").map((detail) => ({ text_len: detail.text_len, has_text: "text" in detail })),
      systems: details.filter((detail) => detail?.kind === "system").map((detail) => detail.op),
      // The bridge's own key records (POST /key) name the iOS button they pressed.
      keys: details.filter((detail) => detail?.kind === "key").map((detail) => detail.key),
    };
    report.journal.ok = gestures.length === 2 && gestures[0].pointers === 2 && gestures[1].pointers === 1 &&
      report.journal.systems.join() === [...(report.volume?.priming ?? []), "volume-up", "volume-down"].join() &&
      report.journal.pastes.length === 1 && report.journal.pastes[0].text_len === CLIP_TEXT.length &&
      !report.journal.pastes[0].has_text && report.journal.origins.every((origin) => origin === "agent") &&
      report.journal.keys.join() === "VOLUME_UP,VOLUME_DOWN" &&
      details.every((detail) => detail?.kind === "key" || detail?.transport === "idb");
  } finally {
    control?.close();
    report.canvas_exit = canvas ? await canvas.stop() : null;
    const stopped = await autonom(["session", "stop"]);
    report.session_stopped = stopped.code === 0;
    await stopCliCompanion(report);
    const empty = original.code !== 0 || /There are no items on the device's pasteboard/.test(original.stdout) ? "" : original.stdout;
    report.pasteboard_restored = (await simctlWithInput(["pbcopy", udid], empty)) === 0;
  }
  await sleep(1000);
  const left = await canvasCompanions();
  report.after_stop = {
    companion_alive: companionPid !== null && left.includes(companionPid),
    companions_left: left.filter((pid) => !companionsBefore.includes(pid)).length,
  };
  await stopLeftCompanions(report, companionsBefore);
  report.host.loadavg_end = hostLoad();
  report.host.output_volume_after = await hostVolume();
  report.checks = {
    transport_idb: report.transport === "idb",
    clipboard_available: report.clipboard_available === true,
    volume_rose: report.volume?.rose === true,
    volume_returned: report.volume?.returned === true,
    bridge_volume: report.bridge_volume?.ok === true,
    host_volume_unchanged: report.host.output_volume_before !== null &&
      report.host.output_volume_after === report.host.output_volume_before,
    paste_set_and_typed: report.clipboard?.paste_reply === "paste" && report.clipboard.typed === true,
    clipboard_read_back: report.clipboard?.read_back_matches === true,
    pinch_nothing_before_release: report.pinch?.nothing_before_release === true,
    pinch_one_event_at_release: report.pinch?.one_event_at_release === true,
    tap_after_pinch: report.pinch?.tap_after === true,
    no_control_errors: Array.isArray(report.control_errors) && report.control_errors.length === 0,
    journal: report.journal?.ok === true,
    pasteboard_restored: report.pasteboard_restored === true,
    nothing_left: report.canvas_exit === 0 && !report.after_stop.companion_alive && report.after_stop.companions_left === 0 &&
      (!report.cli_companion?.started || report.cli_companion.stopped === true),
  };
};

const startedAt = Date.now();
const report = { case: args.case, udid, ok: false };
try {
  await cases[args.case](report);
} catch (error) {
  // What the case collected before it failed stays in the report.
  report.error = error.stack ?? error.message;
} finally {
  await browser?.close().catch(() => {});
  // A case that failed before its own cleanup still stops the companion it started.
  if (report.cli_companion?.started && report.cli_companion.stopped === undefined) {
    await stopCliCompanion(report).catch((error) => { report.cli_companion.stop_error = error.message; });
  }
  await restoreSimulator(report).catch((error) => {
    report.restore_error = error.message;
  });
}
// The case passes only when the Simulator is back as it was (and, for input, the session
// it started is stopped), whatever the other checks say.
report.checks = { ...report.checks, restored: !report.restore_error && restored(report.simulator) };
if (args.case === "input" || args.case === "controls") report.checks.session_stopped = report.session_stopped === true;
settle(report);
report.seconds = (Date.now() - startedAt) / 1000;
report.autonom_home = { path: autonomHome, kept: args["keep-home"] };
const reportPath = join(outDir, `${args.case}.json`);
await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
if (!args["keep-home"]) await rm(autonomHome, { recursive: true, force: true });
console.log(JSON.stringify({ case: args.case, ok: report.ok, report: reportPath }));
process.exit(report.ok ? 0 : 1);
