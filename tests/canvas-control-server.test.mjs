// End-to-end tests of the Canvas host control lease (contract 6.8, cases 1-12): a real Canvas
// process, the real Python bridge (scripts/autonom_canvas_bridge.py, behind a small wrapper
// that logs and may delay its requests), a fake adb, an in-process fake scrcpy device, a fake
// tools process and a fake autonom.py. Every file goes to a temporary AUTONOM_HOME; no
// device, emulator, Simulator or browser is involved.
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const ROOT = resolve(import.meta.dirname, "..");
const SCRIPTS = join(ROOT, "scripts");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const BRIDGE = join(SCRIPTS, "autonom_canvas_bridge.py");
const TOKEN = "canvas-control-test-token";
const SERIAL = "emulator-5584";
const SESSION = "s_canvaslease1";
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Wl8sAAAAASUVORK5CYII=";
const H264 = 0x68323634;
const CONFIG = Buffer.from([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x29, 0x8d, 0x68, 0x0a, 0x02,
  0, 0, 0, 1, 0x68, 0xce, 0x3c, 0x80]);
// The /status keys of 9acc89c (single-device), and the `device` keys of a workspace's /d/<id>/status.
const STATUS_KEYS = ["platform", "serial", "display", "transport", "requested_transport", "fallback_reason", "ffmpeg",
  "screenrecord_h264", "screenrecord_max_size", "direct_h264_url", "frames_sent", "last_frame_at", "stream_clients",
  "uptime_seconds", "last_error", "control_owner", "input_paused", "scrcpy", "idb", "tools"];
const DEVICE_STATUS_KEYS = ["id", "state", "tab", "primary", "focused", "profile", "fps_cap", "max_size", "scale_factor",
  "session", "profile_applied"];
const LEASE_KEYS = ["state", "holder", "generation", "expires_at", "mine", "took_over_from", "ended_by", "idle_s"];

// adb: get-state, screencap, wm, input (held while <log>.input-hold exists; each input line
// also records the lease file's generation at that moment), and the scrcpy start-up commands.
// While <log>.wm-hold exists, a `wm size` the bridge runs is held and, like a real
// `adb shell`, reads its standard input (what it read goes to <log>.stdin); each line
// records whether the bridge ran it.
const FAKE_ADB = String.raw`
import { appendFileSync, existsSync, readFileSync, readSync } from "node:fs";
import { connect } from "node:net";
const args = process.argv.slice(2);
const rest = args[0] === "-s" ? args.slice(2) : args;
const [command, ...tail] = rest;
const line = rest.join(" ");
const isInput = command === "shell" && tail[0] === "input";
let lease = null;
if (isInput) {
  try {
    const file = JSON.parse(readFileSync(process.env.FAKE_LEASE_FILE, "utf8"));
    lease = { generation: file.generation, holder: file.holder ? file.holder.id : null };
  } catch { lease = null; }
}
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify({ args: rest, lease, bridge: process.env.FAKE_IN_BRIDGE === "1" }) + "\n");
const pause = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
if (command === "get-state") console.log("device");
else if (line === "devices") console.log("List of devices attached\n" + process.env.FAKE_SERIAL + "\tdevice");
else if (command === "exec-out" && tail[0] === "screencap") process.stdout.write(Buffer.from(process.env.FAKE_PNG, "base64"));
else if (command === "exec-out" && tail[0] === "screenrecord") { console.error("screenrecord: unrecognized option"); process.exitCode = 1; }
else if (line === "shell wm size" && process.env.FAKE_IN_BRIDGE === "1" && existsSync(process.env.FAKE_ADB_LOG + ".wm-hold")) {
  // One blocking read, as adb shell forwards its input; the null device answers end of file.
  const buffer = Buffer.alloc(65536);
  let read = 0;
  try { read = readSync(0, buffer, 0, buffer.length, null); } catch { read = 0; }
  if (read > 0) appendFileSync(process.env.FAKE_ADB_LOG + ".stdin", buffer.subarray(0, read));
  const deadline = Date.now() + 8000;
  while (existsSync(process.env.FAKE_ADB_LOG + ".wm-hold") && Date.now() < deadline) pause(20);
  console.log("Physical size: 1080x2400");
}
else if (line === "shell wm size") console.log("Physical size: 1080x2400");
else if (line === "shell wm density") console.log("Physical density: 420");
else if (isInput) {
  const hold = process.env.FAKE_ADB_LOG + ".input-hold";
  const deadline = Date.now() + 8000;
  while (existsSync(hold) && Date.now() < deadline) pause(20);
}
else if (command === "push") {
  // Held while the test keeps the scrcpy session in its "starting" state.
  const hold = process.env.FAKE_ADB_LOG + ".push-hold";
  const deadline = Date.now() + 20000;
  while (existsSync(hold) && Date.now() < deadline) pause(20);
  console.log(tail[0] + ": 1 file pushed");
}
else if (command === "forward" && tail[0] === "tcp:0") console.log(process.env.FAKE_SCRCPY_PORT);
else if (command === "forward") process.exitCode = 0;
else if (command === "shell" && tail.length === 1 && tail[0].includes("list_encoders=true")) process.exitCode = 0;
else if (command === "shell" && tail.length === 1 && tail[0].includes("app_process")) {
  const socket = connect(Number(process.env.FAKE_SCRCPY_LAUNCHER_PORT), "127.0.0.1");
  socket.on("connect", () => socket.write(JSON.stringify({ command: tail[0] }) + "\n"));
  socket.on("error", () => process.exit(1));
  socket.on("close", () => process.exit(1));
  process.on("SIGTERM", () => process.exit(143));
}
else if (command === "shell" && tail.length === 1 && tail[0].startsWith("pkill -f ")) process.exitCode = 1;
else { console.error("unsupported fake adb command: " + line); process.exitCode = 2; }
`;

// The bridge the Canvas starts (with --python node): the real Python bridge behind a pipe that
// logs every request line, delays control-acquire by FAKE_ACQUIRE_DELAY_MS, and writes its pid.
const BRIDGE_WRAPPER = String.raw`
import { appendFileSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
writeFileSync(process.env.FAKE_BRIDGE_PID, String(process.pid));
const child = spawn(process.env.REAL_PYTHON, [process.env.REAL_BRIDGE, ...process.argv.slice(2)],
  { stdio: ["pipe", "inherit", "inherit"], env: { ...process.env, FAKE_IN_BRIDGE: "1" } });
const delay = Number(process.env.FAKE_ACQUIRE_DELAY_MS ?? 0);
createInterface({ input: process.stdin }).on("line", (line) => {
  appendFileSync(process.env.FAKE_BRIDGE_LOG, line + "\n");
  const message = JSON.parse(line);
  if (message.op === "control-acquire" && delay > 0) setTimeout(() => child.stdin.write(line + "\n"), delay);
  else child.stdin.write(line + "\n");
}).on("close", () => child.stdin.end());
child.on("exit", (code) => process.exit(code ?? 1));
`;

// The tools process: every request answered ok and logged.
const FAKE_TOOLS = String.raw`
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";
createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  appendFileSync(process.env.FAKE_TOOLS_LOG, line + "\n");
  process.stdout.write(JSON.stringify({ id: message.id, ok: true, result: { op: message.op } }) + "\n");
}).on("close", () => process.exit(0));
`;

// autonom.py for a workspace: session start writes a real session record, session stop marks it
// stopped (the CLI path the Canvas and the test use); logs follow idles.
const FAKE_AUTONOM = String.raw`
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const args = process.argv.slice(2);
const flag = (name) => { const at = args.indexOf(name); return at >= 0 ? args[at + 1] : null; };
const verb = args.find((arg) => ["session", "logs", "devices"].includes(arg));
const action = verb ? args[args.indexOf(verb) + 1] : null;
const recordPath = (id) => join(process.env.AUTONOM_HOME, "sessions", id, "session.json");
if (verb === "session" && action === "start") {
  const id = process.env.FAKE_SESSION_ID;
  const directory = join(process.env.AUTONOM_HOME, "sessions", id);
  mkdirSync(directory, { recursive: true });
  const target = flag("--serial");
  writeFileSync(recordPath(id), JSON.stringify({ schema_version: 2, session_id: id, platform: "android",
    target_id: target, serial: target, artifacts_dir: directory, started_at: new Date().toISOString() }));
  console.log(JSON.stringify({ ok: true, session: { session_id: id, primary: false } }));
} else if (verb === "session" && action === "stop") {
  const id = flag("--session-id");
  const record = JSON.parse(readFileSync(recordPath(id), "utf8"));
  record.stopped_at = new Date().toISOString();
  writeFileSync(recordPath(id), JSON.stringify(record));
  console.log(JSON.stringify({ ok: true, session: { session_id: id } }));
} else if (verb === "logs") {
  setInterval(() => {}, 1000);
} else {
  console.error(JSON.stringify({ ok: false, error_code: "usage_error", error: "fake autonom: " + args.join(" ") }));
  process.exit(2);
}
`;

let realPython = null;
async function python() {
  realPython ??= (await execFileAsync("python3", ["-c", "import sys; print(sys.executable)"])).stdout.trim();
  return realPython;
}

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

async function waitFor(predicate, timeoutMs = 8000, what = "condition") {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(25);
  }
}

/** scrcpy control messages as scrcpy-server reads them (touch, key, scroll and the rest by size). */
function decodeControlMessages(buffer) {
  const messages = [];
  let offset = 0;
  const sizes = { 4: 2, 8: 2, 10: 2, 5: 1, 6: 1, 7: 1, 11: 1, 17: 1 };
  for (;;) {
    const data = buffer.subarray(offset);
    if (!data.length) break;
    const type = data[0];
    let size;
    let message;
    if (type === 0) {
      size = 14;
      if (data.length < size) break;
      message = { type: "key", action: data[1], keycode: data.readInt32BE(2) };
    } else if (type === 1) {
      if (data.length < 5) break;
      size = 5 + data.readUInt32BE(1);
      if (data.length < size) break;
      message = { type: "text" };
    } else if (type === 2) {
      size = 32;
      if (data.length < size) break;
      message = { type: "touch", action: data[1], pointerId: Number(data.readBigInt64BE(2)),
        x: data.readInt32BE(10), y: data.readInt32BE(14) };
    } else if (type === 3) {
      size = 21;
      if (data.length < size) break;
      message = { type: "scroll" };
    } else if (type === 9) {
      if (data.length < 14) break;
      size = 14 + data.readUInt32BE(10);
      if (data.length < size) break;
      message = { type: "set-clipboard" };
    } else if (sizes[type]) {
      size = sizes[type];
      if (data.length < size) break;
      message = { type: `type-${type}` };
    } else {
      throw new Error(`unknown control message type ${type}`);
    }
    messages.push(message);
    offset += size;
  }
  return { messages, rest: buffer.subarray(offset) };
}

/** An in-process scrcpy-server stand-in behind the fake adb's forward and app_process. */
class FakeScrcpyDevice extends EventEmitter {
  constructor() {
    super();
    this.size = { width: 540, height: 1200 };
    this.messages = [];
    this.running = null;
    this.sockets = new Set();
  }

  async listen() {
    const track = (socket) => {
      this.sockets.add(socket);
      socket.on("close", () => this.sockets.delete(socket));
      socket.on("error", () => {});
    };
    this.forward = createServer((socket) => {
      track(socket);
      this.#accept(socket);
    });
    this.launcher = createServer((socket) => {
      track(socket);
      let text = "";
      socket.on("data", (chunk) => {
        text += chunk.toString();
        if (!text.includes("\n") || this.running?.launcher === socket) return;
        const server = { launcher: socket, video: null, control: null, pending: Buffer.alloc(0) };
        socket.on("close", () => {
          if (this.running === server) this.#end(server);
        });
        this.running = server;
      });
    });
    await Promise.all([this.forward, this.launcher].map((server) =>
      new Promise((resolvePromise) => server.listen(0, "127.0.0.1", resolvePromise))));
    return { forwardPort: this.forward.address().port, launcherPort: this.launcher.address().port };
  }

  #accept(socket) {
    const server = this.running;
    if (!server || (server.video && server.control)) {
      socket.destroy();
      return;
    }
    socket.on("close", () => {
      if (this.running === server) this.#end(server);
    });
    if (!server.video) {
      server.video = socket;
      socket.write(Buffer.from([0]));
      return;
    }
    server.control = socket;
    socket.on("data", (chunk) => {
      server.pending = Buffer.concat([server.pending, chunk]);
      const { messages, rest } = decodeControlMessages(server.pending);
      server.pending = rest;
      for (const message of messages) {
        this.messages.push(message);
        if (message.type === "type-17") {
          this.sendSession();
          this.sendConfig();
          this.sendPacket();
        }
      }
    });
    const meta = Buffer.alloc(68);
    meta.write("Fake Pixel", 0, "utf8");
    meta.writeUInt32BE(H264, 64);
    server.video.write(meta);
    this.sendSession();
  }

  #end(server) {
    server.video?.destroy();
    server.control?.destroy();
    server.launcher.destroy();
    if (this.running === server) this.running = null;
  }

  get connected() {
    return Boolean(this.running?.video && this.running?.control);
  }

  #writeVideo(buffer) {
    const video = this.running?.video;
    if (video && !video.destroyed) video.write(buffer);
  }

  sendSession() {
    const header = Buffer.alloc(12);
    header[0] = 0x80;
    header.writeUInt32BE(this.size.width, 4);
    header.writeUInt32BE(this.size.height, 8);
    this.#writeVideo(header);
  }

  sendConfig() {
    const header = Buffer.alloc(12);
    header.writeBigUInt64BE(1n << 62n, 0);
    header.writeUInt32BE(CONFIG.length, 8);
    this.#writeVideo(Buffer.concat([header, CONFIG]));
  }

  sendPacket() {
    const payload = Buffer.alloc(16, 1);
    payload.writeUInt32BE(1, 0);
    payload[4] = 0x65;
    const header = Buffer.alloc(12);
    header.writeBigUInt64BE(1n << 61n, 0);
    header.writeUInt32BE(payload.length, 8);
    this.#writeVideo(Buffer.concat([header, payload]));
  }

  touches() {
    return this.messages.filter((message) => message.type === "touch");
  }

  async close() {
    for (const socket of this.sockets) socket.destroy();
    await Promise.all([this.forward, this.launcher].map((server) =>
      new Promise((resolvePromise) => server.close(() => resolvePromise()))));
  }
}

/** One masked client frame. */
function clientFrame(opcode, payload) {
  const mask = randomBytes(4);
  const length = payload.length;
  const header = length < 126 ? Buffer.from([0x80 | opcode, 0x80 | length])
    : Buffer.from([0x80 | opcode, 0x80 | 126, length >> 8, length & 0xff]);
  const masked = Buffer.alloc(length);
  for (let i = 0; i < length; i += 1) masked[i] = payload[i] ^ mask[i & 3];
  return Buffer.concat([header, mask, masked]);
}

/** A raw RFC 6455 client of the control socket (text messages only). */
class TestSocket extends EventEmitter {
  constructor(socket, rest) {
    super();
    this.socket = socket;
    this.messages = [];
    this.buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => this.#data(chunk));
    if (rest.length) this.#data(rest);
  }

  #data(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const data = this.buffer;
      if (data.length < 2) return;
      const opcode = data[0] & 0x0f;
      let length = data[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (data.length < 4) return;
        length = data.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (data.length < 10) return;
        length = Number(data.readBigUInt64BE(2));
        offset = 10;
      }
      if (data.length < offset + length) return;
      const payload = data.subarray(offset, offset + length);
      this.buffer = data.subarray(offset + length);
      if (opcode === 1) {
        const json = JSON.parse(payload.toString("utf8"));
        this.messages.push(json);
        this.emit("message", json);
      } else if (opcode === 8) {
        this.socket.end();
      }
    }
  }

  send(value) {
    this.socket.write(clientFrame(1, Buffer.from(JSON.stringify(value), "utf8")));
  }

  close() {
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(1000, 0);
    this.socket.write(clientFrame(8, payload));
  }

  /** The first message (seen already or from now on) that matches. */
  next(predicate, timeoutMs = 8000) {
    const found = this.messages.find(predicate);
    if (found) return Promise.resolve(found);
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => {
        this.off("message", listener);
        reject(new Error("timed out waiting for a WebSocket message"));
      }, timeoutMs);
      const listener = (message) => {
        if (!predicate(message)) return;
        clearTimeout(timer);
        this.off("message", listener);
        resolvePromise(message);
      };
      this.on("message", listener);
    });
  }

  /** The next matching message from now on. */
  reply(predicate, timeoutMs = 8000) {
    const start = this.messages.length;
    return this.next((message) => this.messages.indexOf(message) >= start && predicate(message), timeoutMs);
  }
}

function openControl(canvas, path = "/ws/control") {
  return new Promise((resolvePromise, reject) => {
    const socket = connect(canvas.port, "127.0.0.1");
    let data = Buffer.alloc(0);
    socket.on("error", reject);
    socket.on("connect", () => {
      socket.write([
        `GET ${path}?token=${TOKEN} HTTP/1.1`, `Host: 127.0.0.1:${canvas.port}`, "Upgrade: websocket",
        "Connection: Upgrade", `Sec-WebSocket-Key: ${randomBytes(16).toString("base64")}`,
        "Sec-WebSocket-Version: 13", "", "",
      ].join("\r\n"));
    });
    const onData = (chunk) => {
      data = Buffer.concat([data, chunk]);
      const end = data.indexOf("\r\n\r\n");
      if (end < 0) return;
      socket.off("data", onData);
      const status = Number(data.toString("latin1", 0, end).split(" ")[1]);
      if (status !== 101) {
        socket.destroy();
        reject(new Error(`upgrade answered ${status}`));
        return;
      }
      resolvePromise(new TestSocket(socket, data.subarray(end + 4)));
    };
    socket.on("data", onData);
  });
}

async function makeWorld(t, { env = {} } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-lease-"));
  const bin = join(directory, "bin");
  await mkdir(bin);
  const home = join(directory, "autonom-home");
  const files = {
    fakeAdb: join(directory, "fake-adb.mjs"),
    wrapper: join(directory, "bridge-wrapper.mjs"),
    fakeTools: join(directory, "fake-tools.mjs"),
    fakeAutonom: join(directory, "fake-autonom.mjs"),
    serverFile: join(directory, "scrcpy-server-v4.1"),
    adbLog: join(directory, "adb.log"),
    bridgeLog: join(directory, "bridge.log"),
    toolsLog: join(directory, "tools.log"),
    bridgePid: join(directory, "bridge.pid"),
  };
  await writeFile(files.fakeAdb, `#!${process.execPath}\n${FAKE_ADB}`);
  await chmod(files.fakeAdb, 0o755);
  await writeFile(files.wrapper, BRIDGE_WRAPPER);
  await writeFile(files.fakeTools, FAKE_TOOLS);
  await writeFile(files.fakeAutonom, FAKE_AUTONOM);
  await writeFile(files.serverFile, "not a real server");
  for (const log of [files.adbLog, files.bridgeLog, files.toolsLog]) await writeFile(log, "");
  // The single-device Canvas journals into this current session on its target.
  const artifacts = join(home, "sessions", SESSION);
  await mkdir(artifacts, { recursive: true });
  await writeFile(join(home, "sessions", "current.json"), JSON.stringify({
    schema_version: 2, session_id: SESSION, platform: "android", target_id: SERIAL, serial: SERIAL,
    artifacts_dir: artifacts,
  }));
  const device = new FakeScrcpyDevice();
  const ports = await device.listen();
  const world = {
    directory, home, artifacts, device, children: [], ...files,
    leaseFile: join(home, "control", `android__${SERIAL}.json`),
    env: {
      PATH: bin,
      HOME: directory,
      AUTONOM_HOME: home,
      PYTHONDONTWRITEBYTECODE: "1",
      REAL_PYTHON: await python(),
      REAL_BRIDGE: BRIDGE,
      FAKE_SERIAL: SERIAL,
      FAKE_PNG: PNG_BASE64,
      FAKE_ADB_LOG: files.adbLog,
      FAKE_LEASE_FILE: join(home, "control", `android__${SERIAL}.json`),
      FAKE_BRIDGE_LOG: files.bridgeLog,
      FAKE_BRIDGE_PID: files.bridgePid,
      FAKE_TOOLS_LOG: files.toolsLog,
      FAKE_SESSION_ID: "s_workspace1",
      FAKE_SCRCPY_PORT: String(ports.forwardPort),
      FAKE_SCRCPY_LAUNCHER_PORT: String(ports.launcherPort),
      ...env,
    },
  };
  t.after(async () => {
    for (const child of world.children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    try { process.kill(Number(readFileSync(files.bridgePid, "utf8")), "SIGKILL"); } catch { /* gone */ }
    await device.close();
    await rm(directory, { recursive: true, force: true });
  });
  return world;
}

/** Start a Canvas; resolves once it printed its URL. */
function startCanvas(world, { transport = "screencap", args = [] } = {}) {
  const target = args.includes("--device") ? [] : ["--serial", SERIAL];
  const child = spawn(process.execPath, [
    CANVAS, "--adb", world.fakeAdb, ...target, "--port", "0", "--token", TOKEN, "--transport", transport,
    "--python", process.execPath, "--bridge", world.wrapper, "--tools", world.fakeTools, "--autonom", world.fakeAutonom,
    "--control-idle", "15", ...(transport === "scrcpy" ? ["--scrcpy-server", world.serverFile] : []), ...args,
  ], { cwd: ROOT, env: world.env, stdio: ["ignore", "pipe", "pipe"] });
  world.children.push(child);
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code) => resolvePromise({ code, output })));
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${output}`)), 20_000);
    child.stdout.on("data", () => {
      const match = output.match(/Open this exact URL in the visible \S+ side-panel browser: http:\/\/127\.0\.0\.1:(\d+)\//);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise({ child, port: Number(match[1]), origin: `http://127.0.0.1:${match[1]}`, exited,
        output: () => output, holder: `canvas:${match[1]}:${child.pid}` });
    });
    exited.then(({ code }) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${output}`));
    });
  });
}

async function stopCanvas(canvas) {
  canvas.child.kill("SIGTERM");
  return await canvas.exited;
}

async function http(canvas, method, path, { body, origin = "human" } = {}) {
  const response = await fetch(`${canvas.origin}${path}`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, "X-Autonom-Origin": origin,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not JSON */ }
  return { status: response.status, json, text };
}

const tap = (canvas, x, y, origin = "human", base = "") => http(canvas, "POST", `${base}/tap`, { body: { x, y }, origin });
const status = async (canvas, base = "") => (await http(canvas, "GET", `${base}/status`)).json;

function readLease(world) {
  try {
    return JSON.parse(readFileSync(world.leaseFile, "utf8"));
  } catch {
    return null;
  }
}

async function lines(path) {
  return (await readFile(path, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function inputs(world) {
  return (await lines(world.adbLog)).filter((entry) => entry.args[0] === "shell" && entry.args[1] === "input");
}

async function bridgeOps(world, op) {
  return (await lines(world.bridgeLog)).filter((message) => message.op === op);
}

async function journalEntries(world) {
  return lines(join(world.artifacts, "journal.ndjson")).catch(() => []);
}

/** Run Python with the control module in the world's AUTONOM_HOME; answers the JSON it prints. */
async function pythonControl(world, code) {
  const program = `import json, sys\nsys.path.insert(0, ${JSON.stringify(SCRIPTS)})\n` +
    `from autonom_lib import control, errors\ntry:\n${code.split("\n").map((line) => `    ${line}`).join("\n")}\n` +
    "except errors.AutonomError as error:\n    print(json.dumps({'error_code': error.code, **error.extra}))\n";
  const { stdout } = await execFileAsync(await python(), ["-c", program], { env: world.env });
  return JSON.parse(stdout.trim().split("\n").at(-1));
}

const agentAcquire = (world, holder = "agent:test") => pythonControl(world,
  `print(json.dumps(control.acquire("android", ${JSON.stringify(SERIAL)}, holder_id=${JSON.stringify(holder)}, ` +
  `kind="agent", label="test agent", ttl_s=60)))`);
const agentRelease = (world, holder = "agent:test") => pythonControl(world,
  `print(json.dumps(control.release("android", ${JSON.stringify(SERIAL)}, holder_id=${JSON.stringify(holder)})))`);

async function connectedControl(world, canvas) {
  const control = await openControl(canvas);
  await waitFor(() => world.device.connected, 10_000, "the fake device connection");
  await control.next((message) => message.t === "state" && message.session === "streaming" && message.width);
  return control;
}

test("1: agent input without a lease file answers as before and creates no lease file", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world);
  const answer = await tap(canvas, 10, 20, "agent");
  assert.equal(answer.status, 200, answer.text);
  assert.equal(answer.json.ok, true);
  assert.deepEqual([answer.json.x, answer.json.y], [10, 20]);
  assert.equal("control" in answer.json, false);
  assert.equal(existsSync(world.leaseFile), false);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 0);
  // The bridge request carried no claim and, on a single-device Canvas, no session.
  const [request] = await bridgeOps(world, "tap");
  assert.deepEqual(Object.keys(request).sort(), ["id", "op", "origin", "payload"]);
  const reported = await status(canvas);
  assert.equal(reported.lease.state, "none");
  assert.equal(reported.session_stopped, false);
  assert.equal(existsSync(join(world.home, "control")), false, "a read created the lease directory");
  await stopCanvas(canvas);
});

test("2: the first human tap in Shared state takes the lease before it reaches adb, journals no control record and keeps the answer", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world);
  const answer = await tap(canvas, 30, 40);
  assert.equal(answer.status, 200, answer.text);
  assert.equal(answer.json.ok, true);
  assert.deepEqual([answer.json.x, answer.json.y], [30, 40]);
  assert.equal("control" in answer.json, false, "the HTTP answer keeps its body");
  const lease = readLease(world);
  assert.deepEqual(lease.holder, { kind: "human", id: canvas.holder, label: `Canvas ${canvas.port}` });
  assert.equal(lease.generation, 1);
  const [first] = await inputs(world);
  assert.deepEqual(first.args, ["shell", "input", "tap", "30", "40"]);
  assert.deepEqual(first.lease, { generation: 1, holder: canvas.holder }, "the lease existed when the tap reached adb");
  assert.equal((await inputs(world)).length, 1);
  // The bridge request carried this Canvas's claim.
  const [request] = await bridgeOps(world, "tap");
  assert.deepEqual(request.control, { holder_id: canvas.holder, generation: 1 });
  const journal = await waitFor(async () => {
    const entries = await journalEntries(world);
    return entries.length >= 1 && entries;
  }, 5000, "the tap's journal entry");
  assert.deepEqual(journal.map((entry) => entry.verb), ["ui tap"]);
  const detail = JSON.parse(await readFile(join(world.artifacts, journal[0].result.detail), "utf8"));
  assert.equal(detail.control.generation, 1);
  assert.match(detail.control.operation_id, /^op_[0-9a-f]{12}$/);
  let reported = await status(canvas);
  assert.equal(reported.control_owner, "shared");
  assert.equal(reported.lease.mine, true);
  assert.equal(reported.lease.state, "held");
  // An agent-origin tap of the same Canvas is still admitted; a second human tap keeps generation 1.
  assert.equal((await tap(canvas, 1, 2, "agent")).status, 200);
  assert.equal((await tap(canvas, 3, 4)).status, 200);
  assert.equal(readLease(world).generation, 1);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 1);
  assert.equal((await inputs(world)).length, 3);
  await sleep(300);
  assert.equal((await journalEntries(world)).filter((entry) => entry.verb === "ui control").length, 0);
  reported = await status(canvas);
  assert.equal(reported.control_owner, "shared");
  // The stop hands the lease back.
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0, exit.output);
  assert.equal(readLease(world).holder, null);
  assert.equal(readLease(world).generation, 1);
});

test("3+4: a live agent lease refuses human input without a takeover; Take over bumps the generation and Hand back frees it", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  const granted = await agentAcquire(world);
  assert.equal(granted.generation, 1);
  const refused = await tap(canvas, 5, 5);
  assert.equal(refused.status, 409);
  assert.deepEqual(Object.keys(refused.json).sort(), ["error", "error_code", "ok"]);
  assert.equal(refused.json.error_code, "control_held");
  assert.match(refused.json.error, /agent 'test agent' \(generation 1\)/);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  const error = await control.next((message) => message.t === "error" && message.for === "touch");
  assert.equal(error.code, "control_held");
  await sleep(200);
  assert.equal((await inputs(world)).length, 0);
  assert.equal(world.device.touches().length, 0);
  assert.equal(readLease(world).holder.id, "agent:test", "an input never takes over");
  // The page shows the foreign holder.
  let reported = await status(canvas);
  assert.deepEqual([reported.lease.state, reported.lease.mine, reported.lease.holder.id], ["held", false, "agent:test"]);
  // Explicit Take over (human).
  const takeover = await http(canvas, "POST", "/control", { body: { mode: "takeover" } });
  assert.equal(takeover.status, 200, takeover.text);
  assert.equal(takeover.json.control_owner, "human");
  assert.equal(takeover.json.lease.generation, 2);
  assert.equal(takeover.json.lease.mine, true);
  assert.equal(takeover.json.lease.took_over_from.id, "agent:test");
  assert.equal(readLease(world).holder.id, canvas.holder);
  const stale = await pythonControl(world,
    `control.admit("android", ${JSON.stringify(SERIAL)}, control.Claim("agent:test", 1))\nprint(json.dumps({"admitted": True}))`);
  assert.equal(stale.error_code, "control_generation_stale");
  const unclaimed = await pythonControl(world,
    `control.admit("android", ${JSON.stringify(SERIAL)}, None)\nprint(json.dumps({"admitted": True}))`);
  assert.equal(unclaimed.error_code, "control_held");
  // Hand back from the page socket: free, generation kept, the state goes out.
  const freed = control.reply((message) => message.t === "state" && message.lease?.state === "free");
  control.send({ t: "control", mode: "release" });
  const state = await freed;
  assert.equal(state.lease.generation, 2);
  assert.equal(state.lease.ended_by, "released");
  assert.equal(state.lease.took_over_from, null);
  assert.equal(state.owner, "shared");
  assert.equal(readLease(world).holder, null);
  assert.equal(readLease(world).generation, 2);
  reported = await status(canvas);
  assert.deepEqual([reported.lease.state, reported.lease.ended_by], ["free", "released"]);
  // The Take over and Hand back of the socket are journaled as before; the HTTP takeover is not.
  const journal = await waitFor(async () => {
    const entries = (await journalEntries(world)).filter((entry) => entry.verb === "ui control");
    return entries.length >= 1 && entries;
  }, 5000, "the control record");
  assert.equal(journal.length, 1);
  await stopCanvas(canvas);
});

test("5: after --control-idle seconds without human input the lease is released with an idle record; the next tap takes generation 2", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world);
  assert.equal((await tap(canvas, 10, 10)).status, 200);
  assert.equal(readLease(world).holder.id, canvas.holder);
  const tappedAt = Date.now();
  await waitFor(() => readLease(world)?.holder === null, 25_000, "the idle release");
  const elapsed = Date.now() - tappedAt;
  assert.ok(elapsed >= 14_000, `released after ${elapsed} ms`);
  assert.equal(readLease(world).generation, 1);
  const reported = await status(canvas);
  assert.deepEqual([reported.lease.state, reported.lease.ended_by, reported.lease.idle_s], ["free", "idle", 15]);
  const record = await waitFor(async () => (await journalEntries(world)).find((entry) => entry.verb === "ui control"),
    5000, "the idle record");
  const detail = JSON.parse(await readFile(join(world.artifacts, record.result.detail), "utf8"));
  assert.deepEqual([detail.mode, detail.owner, record.origin], ["idle", "human", "human"]);
  assert.equal((await tap(canvas, 11, 11)).status, 200);
  assert.equal(readLease(world).generation, 2);
  assert.equal((await status(canvas)).lease.ended_by, null);
  await stopCanvas(canvas);
});

test("6: five human taps at once in Shared state cause one acquire and five taps in order", { timeout: 60_000 }, async (t) => {
  // The acquire answers late (below AUTO_ACQUIRE_WAIT_MS), so all five wait for the same one.
  const world = await makeWorld(t, { env: { FAKE_ACQUIRE_DELAY_MS: "700" } });
  const canvas = await startCanvas(world);
  const taps = [];
  for (let i = 1; i <= 5; i += 1) {
    taps.push(tap(canvas, i * 10, i * 10));
    await sleep(40);
  }
  const answers = await Promise.all(taps);
  assert.deepEqual(answers.map((answer) => answer.status), [200, 200, 200, 200, 200]);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 1);
  assert.equal(readLease(world).generation, 1);
  assert.deepEqual((await inputs(world)).map((entry) => entry.args[3]), ["10", "20", "30", "40", "50"]);
  await stopCanvas(canvas);
});

test("7: input queued before a foreign change is refused stale and never sent; held pointers are lifted", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  // A pointer held down by the page (its first input takes the lease).
  control.send({ t: "touch", a: "down", id: 1, x: 0.25, y: 0.25 });
  await waitFor(() => world.device.touches().length === 1, 5000, "the down");
  assert.equal(readLease(world).holder.id, canvas.holder);
  // A human tap the device holds, and one queued behind it.
  writeFileSync(`${world.adbLog}.input-hold`, "");
  const first = tap(canvas, 1, 1);
  await waitFor(async () => (await inputs(world)).length === 1, 5000, "the first tap at adb");
  const second = tap(canvas, 2, 2);
  await waitFor(async () => (await bridgeOps(world, "tap")).length === 1 && (await sleep(200), true), 5000, "the queue");
  // Another Canvas takes the device over (a human takeover by another holder).
  const taken = await pythonControl(world, `print(json.dumps(control.acquire("android", ${JSON.stringify(SERIAL)}, ` +
    'holder_id="canvas:9:9", kind="human", label="Canvas 9", ttl_s=30, takeover=True)))');
  assert.equal(taken.generation, 2);
  // The held pointer is lifted: an UP reaches the device.
  await waitFor(() => world.device.touches().some((message) => message.action === 1), 5000, "the UP");
  const lost = await control.next((message) => message.t === "state" && message.lease?.holder?.id === "canvas:9:9");
  assert.equal(lost.lease.mine, false);
  assert.equal(lost.lease.ended_by, "lost");
  rmSync(`${world.adbLog}.input-hold`);
  assert.equal((await first).status, 200);
  const refused = await second;
  assert.equal(refused.status, 409);
  assert.equal(refused.json.error_code, "control_generation_stale");
  await sleep(300);
  assert.deepEqual((await inputs(world)).map((entry) => entry.args.slice(2)), [["tap", "1", "1"]]);
  // The lost lease is journaled.
  const record = await waitFor(async () => (await journalEntries(world)).find((entry) => entry.verb === "ui control"),
    5000, "the lost record");
  const detail = JSON.parse(await readFile(join(world.artifacts, record.result.detail), "utf8"));
  assert.equal(detail.mode, "lost");
  // Now refused by the live holder.
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  assert.equal((await control.next((message) => message.t === "error" && message.code === "control_held")).code,
    "control_held");
  await stopCanvas(canvas);
});

test("8: when the last human client closes, renewing stops and the lease expires (test TTL 5 s)", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t, { env: { AUTONOM_TEST_MODE: "1", AUTONOM_CANVAS_HUMAN_TTL_S: "5" } });
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => world.device.touches().length === 2, 5000, "the touch");
  const granted = readLease(world);
  assert.equal(granted.ttl_ms, 5000);
  // Renewed while the page is connected.
  await waitFor(() => (readLease(world)?.expires_at_ms ?? 0) > granted.expires_at_ms, 5000, "a renewal");
  control.close();
  const closedAt = Date.now();
  await waitFor(() => {
    const lease = readLease(world);
    return lease && lease.holder && Date.now() >= lease.expires_at_ms;
  }, 8000, "the lease expiry");
  assert.ok(Date.now() - closedAt <= 8000);
  await waitFor(async () => (await status(canvas)).lease.state === "expired", 3000, "the Canvas to see the expiry");
  assert.equal((await status(canvas)).lease.ended_by, "lost");
  await stopCanvas(canvas);
});

test("9+10: a workspace device bound to a stopped session refuses input; Tools leased ops follow the lease; state surfaces carry lease and session_stopped", { timeout: 90_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { args: ["--workspace", "lease", "--ephemeral", "--device", `android:${SERIAL}`] });
  const id = `android~${SERIAL}`;
  const base = `/d/${id}`;
  const listed = await waitFor(async () => {
    const devices = (await http(canvas, "GET", "/api/devices")).json?.devices ?? [];
    const device = devices.find((item) => item.id === id && item.state === "live" && item.session);
    return device;
  }, 20_000, "the live device with its session");
  assert.equal(listed.session.id, "s_workspace1");
  assert.deepEqual(Object.keys(listed.lease).sort(), [...LEASE_KEYS].sort());
  assert.equal(listed.lease.state, "none");
  assert.equal(listed.session_stopped, false);
  // A person's leased Tools op in Shared state takes the lease first; the request carries the binding.
  const launch = await http(canvas, "POST", `${base}/tools/call`, { body: { op: "app.launch", payload: { app_id: "com.example" } } });
  assert.equal(launch.status, 200, launch.text);
  assert.equal(readLease(world).holder.id, canvas.holder);
  assert.equal(readLease(world).session_id, "s_workspace1");
  const launched = (await lines(world.toolsLog)).find((message) => message.op === "app.launch");
  assert.deepEqual([launched.session_id, launched.control], ["s_workspace1", { holder_id: canvas.holder, generation: 1 }]);
  // The bridge's requests carry the session binding too.
  const [acquire] = await bridgeOps(world, "control-acquire");
  assert.equal(acquire.session_id, "s_workspace1");
  assert.equal((await http(canvas, "POST", `${base}/control`, { body: { mode: "release" } })).status, 200);
  assert.equal(readLease(world).holder, null);
  // Under a foreign agent lease the person's leased op is refused before the tools process.
  await agentAcquire(world);
  const before = (await lines(world.toolsLog)).length;
  const refused = await http(canvas, "POST", `${base}/tools/call`, { body: { op: "app.launch", payload: { app_id: "com.example" } } });
  assert.equal(refused.status, 409);
  assert.equal(refused.json.error_code, "control_held");
  assert.equal((await lines(world.toolsLog)).length, before);
  // Case 10: /status, /api/devices and /d/<id>/status.
  const deviceStatus = await status(canvas, base);
  for (const key of [...STATUS_KEYS, "device"]) assert.ok(key in deviceStatus, key);
  assert.deepEqual(Object.keys(deviceStatus.device), DEVICE_STATUS_KEYS);
  assert.deepEqual([deviceStatus.lease.state, deviceStatus.lease.holder.id, deviceStatus.session_stopped],
    ["held", "agent:test", false]);
  await agentRelease(world);
  // The session is stopped through the CLI: input is refused, the device shows it stopped.
  await execFileAsync(process.execPath, [world.fakeAutonom, "session", "stop", "--session-id", "s_workspace1"],
    { env: world.env });
  const stopped = await tap(canvas, 5, 5, "human", base);
  assert.equal(stopped.status, 409);
  assert.equal(stopped.json.error_code, "session_stopped");
  assert.match(stopped.json.error, /s_workspace1/);
  assert.equal((await inputs(world)).length, 0);
  assert.equal((await status(canvas, base)).session_stopped, true);
  const after = (await http(canvas, "GET", "/api/devices")).json.devices.find((item) => item.id === id);
  assert.equal(after.session_stopped, true);
  // The Canvas does not start a replacement session.
  assert.equal(readLease(world).holder, null);
  await stopCanvas(canvas);
});

test("10: the state messages of a single-device Canvas carry lease and session_stopped", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  const state = control.messages.find((message) => message.t === "state");
  assert.deepEqual(Object.keys(state.lease).sort(), [...LEASE_KEYS].sort());
  assert.deepEqual([state.lease.state, state.lease.mine, state.lease.idle_s, state.session_stopped], ["none", false, 15, false]);
  // `presets` stays the last field of a state message.
  assert.equal(Object.keys(state).at(-1), "presets");
  const reported = await status(canvas);
  for (const key of STATUS_KEYS) assert.ok(key in reported, key);
  assert.equal("device" in reported, false);
  const held = control.reply((message) => message.t === "state" && message.lease?.mine === true);
  control.send({ t: "key", a: "down", code: 29 });
  control.send({ t: "key", a: "up", code: 29 });
  const mine = await held;
  assert.deepEqual([mine.lease.state, mine.lease.generation, mine.lease.took_over_from], ["held", 1, null]);
  await stopCanvas(canvas);
});

test("11: a failed auto-acquire refuses the input: a dead bridge answers 502, a late grant 409 control_not_held", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  const pid = Number(await waitFor(() => existsSync(world.bridgePid) && readFileSync(world.bridgePid, "utf8"), 5000, "the bridge"));
  process.kill(pid, "SIGKILL");
  await waitFor(() => { try { process.kill(pid, 0); return false; } catch { return true; } }, 5000, "the bridge exit");
  await sleep(200);
  const answer = await tap(canvas, 1, 1);
  assert.equal(answer.status, 502, answer.text);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  const error = await control.next((message) => message.t === "error" && message.for === "touch");
  assert.match(error.message, /could not be taken/);
  await sleep(200);
  assert.equal(world.device.touches().length, 0);
  assert.equal((await inputs(world)).length, 0);
  assert.equal(existsSync(world.leaseFile), false);
  await stopCanvas(canvas);

  // A bridge that answers the acquire only after AUTO_ACQUIRE_WAIT_MS.
  const late = await makeWorld(t, { env: { FAKE_ACQUIRE_DELAY_MS: "3000" } });
  const slow = await startCanvas(late);
  const started = Date.now();
  const refused = await tap(slow, 7, 7);
  assert.equal(refused.status, 409, refused.text);
  assert.equal(refused.json.error_code, "control_not_held");
  assert.ok(Date.now() - started < 3000, "the input waited for the late answer");
  await waitFor(() => readLease(late)?.holder, 8000, "the late grant");
  await sleep(500);
  assert.equal((await inputs(late)).length, 0, "the refused tap reached adb after the late answer");
  assert.match((await status(slow)).last_error ?? "", /control/);
  await stopCanvas(slow);
});

test("12: a touch sequence reaches the device in order after one acquire; observation and host-side Tools ops never need the lease", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await connectedControl(world, canvas);
  for (const op of ["capture.screenshot", "mocks.add"]) {
    assert.equal((await http(canvas, "POST", "/tools/call", { body: { op, payload: {} } })).status, 200, op);
  }
  assert.equal(existsSync(world.leaseFile), false, "an observation op took the lease");
  control.send({ t: "touch", a: "down", id: 1, x: 0.1, y: 0.1 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.2, y: 0.2 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.3, y: 0.3 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.4, y: 0.4 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.4, y: 0.4 });
  await waitFor(() => world.device.touches().length === 5, 5000, "five touch messages");
  const width = world.device.size.width;
  assert.deepEqual(world.device.touches().map((message) => [message.action, Math.round(message.x / width * 10)]),
    [[0, 1], [2, 2], [2, 3], [2, 4], [1, 4]]);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 1);
  assert.equal(control.messages.filter((message) => message.t === "error").length, 0);
  // Under a foreign agent lease they are still admitted.
  control.send({ t: "control", mode: "release" });
  await waitFor(() => readLease(world)?.holder === null, 5000, "the hand back");
  await agentAcquire(world);
  for (const op of ["capture.screenshot", "mocks.add"]) {
    const answer = await http(canvas, "POST", "/tools/call", { body: { op, payload: {} } });
    assert.equal(answer.status, 200, `${op}: ${answer.text}`);
  }
  const leased = await http(canvas, "POST", "/tools/call", { body: { op: "location.set", payload: {} } });
  assert.deepEqual([leased.status, leased.json.error_code], [409, "control_held"]);
  await stopCanvas(canvas);
});

test("13: a human input refused because the scrcpy session is still starting takes no lease; once streaming it does", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const hold = `${world.adbLog}.push-hold`;
  writeFileSync(hold, "");
  t.after(() => rmSync(hold, { force: true }));
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  const control = await openControl(canvas);
  await control.next((message) => message.t === "state" && message.session === "starting");
  // No lease file yet: the refused inputs write none.
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  const refused = await control.next((message) => message.t === "error" && message.for === "touch");
  assert.match(refused.message, /The scrcpy session is not ready \(starting\)/);
  control.send({ t: "key", a: "down", key: 4 });
  control.send({ t: "text", text: "a" });
  await control.next((message) => message.t === "error" && message.for === "text");
  await sleep(300);
  assert.equal(existsSync(world.leaseFile), false, "a refused input wrote a lease file");
  assert.equal((await bridgeOps(world, "control-acquire")).length, 0);
  assert.equal((await status(canvas)).lease.state, "none");
  // A free lease (generation 1) keeps its generation while input is refused not ready.
  await agentAcquire(world);
  await agentRelease(world);
  assert.deepEqual([readLease(world).holder, readLease(world).generation], [null, 1]);
  const errorsBefore = control.messages.filter((message) => message.t === "error").length;
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  await waitFor(() => control.messages.filter((message) => message.t === "error").length > errorsBefore, 5000,
    "the second refusal");
  await sleep(300);
  assert.deepEqual([readLease(world).holder, readLease(world).generation], [null, 1]);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 0);
  assert.equal(world.device.touches().length, 0);
  // Once the session streams, the same input takes the lease and reaches the device.
  rmSync(hold);
  await waitFor(() => world.device.connected, 10_000, "the fake device connection");
  await control.next((message) => message.t === "state" && message.session === "streaming", 10_000);
  const errorsReady = control.messages.filter((message) => message.t === "error").length;
  control.send({ t: "touch", a: "down", id: 3, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 3, x: 0.5, y: 0.5 });
  await waitFor(() => world.device.touches().length === 2, 5000, "the admitted touch");
  const granted = readLease(world);
  assert.equal(granted.holder.id, canvas.holder);
  assert.equal(granted.holder.kind, "human");
  assert.equal(granted.generation, 2);
  assert.equal((await bridgeOps(world, "control-acquire")).length, 1);
  assert.equal(control.messages.filter((message) => message.t === "error").length, errorsReady);
  await stopCanvas(canvas);
});

test("14: right after a Take over and Hand back, a person's first touch takes the lease while the bridge runs adb; a refused down drops its moves and up quietly", { timeout: 60_000 }, async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, { transport: "scrcpy" });
  // Take over an agent lease and hand it back, as a person does from the page.
  assert.equal((await agentAcquire(world)).generation, 1);
  const takeover = await http(canvas, "POST", "/control", { body: { mode: "takeover" } });
  assert.equal(takeover.status, 200, takeover.text);
  assert.equal(takeover.json.lease.generation, 2);
  const handBack = await http(canvas, "POST", "/control", { body: { mode: "release" } });
  assert.equal(handBack.status, 200, handBack.text);
  assert.deepEqual([readLease(world).holder, readLease(world).generation], [null, 2]);
  const control = await connectedControl(world, canvas);
  // A bridge op runs adb (the screen-size of /status) when the touch arrives: the acquire
  // is written to the bridge while that adb process reads its standard input.
  const hold = `${world.adbLog}.wm-hold`;
  t.after(() => rmSync(hold, { force: true }));
  // The bridge answers in order: once this answer is back, no earlier request is pending.
  await status(canvas);
  const bridgeWm = async () => (await lines(world.adbLog)).filter((entry) =>
    entry.args.join(" ") === "shell wm size" && entry.bridge).length;
  const wmBefore = await bridgeWm();
  writeFileSync(hold, "");
  const statusAnswer = status(canvas);
  await waitFor(async () => (await bridgeWm()) > wmBefore, 5000, "the bridge's adb wm size");
  const errorsBefore = control.messages.filter((message) => message.t === "error").length;
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await waitFor(async () => (await bridgeOps(world, "control-acquire")).length === 1, 5000, "the acquire request");
  await sleep(300);
  rmSync(hold);
  const answered = await Promise.race([statusAnswer.then(() => true), sleep(5000).then(() => false)]);
  assert.ok(answered, "the /status the adb call answers never came back");
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => world.device.touches().length === 2, 5000, "the touch after the adb call");
  assert.deepEqual(control.messages.filter((message) => message.t === "error").slice(errorsBefore), []);
  assert.equal(readLease(world).holder.id, canvas.holder);
  assert.equal(readLease(world).generation, 3);
  assert.equal(existsSync(`${world.adbLog}.stdin`) ? readFileSync(`${world.adbLog}.stdin`, "utf8") : "", "",
    "adb read a bridge request");
  // A down refused by a foreign lease answers once; its moves and up are dropped quietly.
  control.send({ t: "control", mode: "release" });
  await waitFor(() => readLease(world)?.holder === null, 5000, "the hand back");
  await agentAcquire(world);
  const errorsHeld = control.messages.filter((message) => message.t === "error").length;
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 2, x: 0.6, y: 0.6 });
  control.send({ t: "touch", a: "up", id: 2, x: 0.6, y: 0.6 });
  control.send({ t: "ping", ts: 1 });
  await control.next((message) => message.t === "pong" && message.ts === 1);
  assert.deepEqual(control.messages.filter((message) => message.t === "error").slice(errorsHeld)
    .map((message) => [message.for, message.code]), [["touch", "control_held"]]);
  assert.equal(world.device.touches().length, 2);
  await stopCanvas(canvas);

  // An acquire that times out: the down is refused once, its move and up (read while it
  // waited, and after) are dropped without "is not down".
  const late = await makeWorld(t, { env: { FAKE_ACQUIRE_DELAY_MS: "2600" } });
  const slow = await startCanvas(late, { transport: "scrcpy" });
  const lateControl = await connectedControl(late, slow);
  lateControl.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  lateControl.send({ t: "touch", a: "move", id: 1, x: 0.6, y: 0.6 });
  const refused = await lateControl.next((message) => message.t === "error" && message.for === "touch", 5000);
  assert.equal(refused.code, "control_not_held");
  lateControl.send({ t: "touch", a: "up", id: 1, x: 0.6, y: 0.6 });
  lateControl.send({ t: "ping", ts: 2 });
  await lateControl.next((message) => message.t === "pong" && message.ts === 2);
  assert.deepEqual(lateControl.messages.filter((message) => message.t === "error").map((message) => message.code ?? null),
    ["control_not_held"]);
  assert.equal(late.device.touches().length, 0);
  await stopCanvas(slow);
});
