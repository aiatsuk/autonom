// End-to-end tests of the Canvas scrcpy transport against fakes only: a fake adb
// script, an in-process fake scrcpy device behind the fake forward port, and a
// fake journal bridge. No real adb device, emulator or browser is involved.
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test, { describe } from "node:test";
import { promisify } from "node:util";
import { Script, createContext } from "node:vm";

import {
  h264EncoderKind,
  parseH264Encoders,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-lib.mjs";
import {
  ENCODER_PROBE_TIMEOUT_MS,
  HEALTHY_STREAM_MS,
  probeVideoEncoders,
  restartDelay,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs";

const execFileAsync = promisify(execFile);
const ROOT = resolve(import.meta.dirname, "..");
const CANVAS = join(ROOT, "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs");
const TOKEN = "scrcpy-test-token";
const SERIAL = "fake-device-1";
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Wl8sAAAAASUVORK5CYII=";
const H264 = 0x68323634;
// SPS (Baseline, level 4.1) + PPS, as a device encoder sends them in its config packet.
const CONFIG = Buffer.from([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x29, 0x8d, 0x68, 0x0a, 0x02,
  0, 0, 0, 1, 0x68, 0xce, 0x3c, 0x80]);

// The fake display, shared by the fake adb and the fake bridge: physical 1080x2400 @ 420.
// Overrides live in the FAKE_WM_STATE file once wm sets one; before that,
// FAKE_WM_OVERRIDE ("WxH@D", either side may be empty) is what the device had already.
const FAKE_WM = String.raw`
const PHYSICAL = { size: "1080x2400", density: 420 };
function wmState() {
  try {
    return JSON.parse(readFileSync(process.env.FAKE_WM_STATE, "utf8"));
  } catch {
    const [size = "", density = ""] = (process.env.FAKE_WM_OVERRIDE ?? "").split("@");
    return { size: size || null, density: density ? Number(density) : null };
  }
}
function wmEffective(state = wmState()) {
  const [width, height] = (state.size ?? PHYSICAL.size).split("x").map(Number);
  return { width, height, density: state.density ?? PHYSICAL.density };
}
`;

const FAKE_ADB = String.raw`
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { connect } from "node:net";
${FAKE_WM}

const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify(args) + "\n");
const rest = args[0] === "-s" ? args.slice(2) : args;
const [command, ...tail] = rest;
const PNG = Buffer.from(process.env.FAKE_PNG, "base64");

if (command === "get-state") {
  console.log("device");
} else if (command === "exec-out" && tail[0] === "screencap") {
  // Once the display was changed, each picture's PNG header carries its size.
  if (existsSync(process.env.FAKE_WM_STATE ?? "")) {
    const { width, height } = wmEffective();
    PNG.writeUInt32BE(width, 16);
    PNG.writeUInt32BE(height, 20);
  }
  process.stdout.write(PNG);
} else if (command === "exec-out" && tail[0] === "screenrecord") {
  if (process.env.FAKE_SCREENRECORD === "h264" && tail.includes("--output-format=h264")) {
    process.stdout.write(Buffer.from([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x29, 0, 0, 0, 1, 0x68, 0xce]));
    // A capture runs until it is stopped; the start-up probe has a time limit.
    if (process.env.FAKE_SCREENRECORD_HOLD && !tail.includes("--time-limit")) setInterval(() => {}, 1000);
  } else {
    console.error("screenrecord: unrecognized option '--output-format=h264'");
    process.exitCode = 1;
  }
} else if (command === "shell" && tail.join(" ") === "screenrecord --help") {
  // Android 16 (API 36) help text: the hidden --output-format option is not listed.
  console.log("Usage: screenrecord [options] <filename>\n--size WIDTHxHEIGHT\n--bit-rate RATE\n--time-limit TIME");
} else if (command === "shell" && tail[0] === "wm" && ["size", "density"].includes(tail[1])) {
  const [, what, value] = tail;
  const state = wmState();
  const physical = String(PHYSICAL[what]);
  // While this file exists, setting or resetting the density fails; while it says
  // "read", reading the density fails too.
  const fails = existsSync(process.env.FAKE_WM_DENSITY_FAIL ?? "")
    ? readFileSync(process.env.FAKE_WM_DENSITY_FAIL, "utf8") : null;
  if (what === "density" && fails !== null && (value !== undefined || fails === "read")) {
    console.error(value === undefined ? "Error: could not read the density" : "Error: could not set the density");
    process.exitCode = 1;
  } else if (value === undefined) {
    console.log("Physical " + what + ": " + physical);
    if (state[what] !== null) console.log("Override " + what + ": " + state[what]);
  } else {
    // A slow device: FAKE_WM_SET_MS after the call the value is set and adb answers.
    setTimeout(() => {
      const now = wmState();
      // Like WindowManager, a value equal to the physical one clears the override.
      const cleared = value === "reset" || value === physical;
      now[what] = cleared ? null : what === "size" ? value : Number(value);
      writeFileSync(process.env.FAKE_WM_STATE, JSON.stringify(now));
    }, Number(process.env.FAKE_WM_SET_MS ?? 0));
  }
} else if (command === "push") {
  if (process.env.FAKE_PUSH_FAIL) {
    console.error("adb: error: failed to copy");
    process.exitCode = 1;
  } else {
    console.log(tail[0] + ": 1 file pushed");
  }
} else if (command === "forward" && tail[0] === "tcp:0") {
  // A slow adb server: the port is printed only after a while.
  setTimeout(() => console.log(process.env.FAKE_SCRCPY_PORT), Number(process.env.FAKE_FORWARD_DELAY_MS ?? 0));
} else if (command === "forward" && tail[0] === "--remove") {
  setTimeout(() => {}, Number(process.env.FAKE_REMOVE_DELAY_MS ?? 0));
} else if (command === "forward" && tail[0] === "--list") {
  // nothing is listed
} else if (command === "shell" && tail.length === 1 && tail[0].includes("list_encoders=true")) {
  // scrcpy-server listing its encoders: FAKE_ENCODERS is what it prints (nothing by
  // default, so the Canvas cannot tell), "fail" makes it exit 1, "hang" never answers.
  const listed = process.env.FAKE_ENCODERS ?? "";
  if (listed === "fail") {
    console.error("[server] ERROR: Could not list encoders");
    process.exitCode = 1;
  } else if (listed === "hang") {
    setInterval(() => {}, 1000);
  } else {
    process.stdout.write(listed);
  }
} else if (command === "shell" && tail.length === 1 && tail[0].includes("app_process")) {
  const socket = connect(Number(process.env.FAKE_SCRCPY_LAUNCHER_PORT), "127.0.0.1");
  socket.on("connect", () => socket.write(JSON.stringify({ command: tail[0] }) + "\n"));
  socket.on("error", () => process.exit(1));
  // The device ends the connection when its server dies.
  socket.on("close", () => process.exit(1));
  process.on("SIGTERM", () => process.exit(143));
} else if (command === "shell" && tail.length === 1 && tail[0].startsWith("pkill -f ")) {
  process.exitCode = 1;
} else if (command === "shell" && tail[0] === "input") {
  // A slow device: input answers FAKE_ADB_INPUT_MS after the call, as a long swipe does.
  setTimeout(() => {}, Number(process.env.FAKE_ADB_INPUT_MS ?? 0));
} else {
  console.error("unsupported fake adb command: " + rest.join(" "));
  process.exitCode = 2;
}
`;

const FAKE_BRIDGE = String.raw`
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { createInterface } from "node:readline";
${FAKE_WM}

// While this file exists, record answers wait: a journal slower than its input.
const HOLD = process.env.FAKE_BRIDGE_LOG + ".hold";
// A slow disk: each record is answered this long after it arrives.
const RECORD_MS = Number(process.env.FAKE_BRIDGE_RECORD_MS ?? 0);
// A slow device for HTTP input: taps, swipes, keys and text are answered this long after.
const INPUT_MS = Number(process.env.FAKE_BRIDGE_INPUT_MS ?? 0);
const INPUT_OPS = ["tap", "swipe", "key", "text"];
// While this file exists, input answers wait too: input that never finishes until a test says so.
const INPUT_HOLD = process.env.FAKE_BRIDGE_LOG + ".input-hold";
// While this one exists, only the text "<text>" waits.
const textHold = (message) => message.op === "text" && typeof message.payload?.text === "string"
  ? INPUT_HOLD + "." + message.payload.text : null;
const inputHeld = (message) => existsSync(INPUT_HOLD) || existsSync(textHold(message) ?? "");
// Like the real bridge, one line at a time: each is handled, and logged, only once the one
// before it was answered, so a record behind a slow swipe waits for it.
const IN_ORDER = Boolean(process.env.FAKE_BRIDGE_IN_ORDER);
const held = [];
const answer = (id, result) => process.stdout.write(JSON.stringify({ id, ok: true, result }) + "\n");
setInterval(() => {
  while (held.length && !existsSync(HOLD)) answer(...held.shift());
}, 20).unref();

/** Handle one line; resolves once it is answered (a held record counts as answered). */
function handle(line) {
  const message = JSON.parse(line);
  appendFileSync(process.env.FAKE_BRIDGE_LOG, line + "\n");
  let result = { ok: true };
  if (message.op === "screen-size") {
    // Like the real bridge, the size in effect: an override, else the physical size.
    const { width, height } = wmEffective();
    result = { ok: true, display: { width, height } };
  }
  if (message.op === "record") result = { ok: true, recorded: message.payload.kind, via: "scrcpy" };
  const later = (ms) => new Promise((resolvePromise) => setTimeout(() => {
    answer(message.id, result);
    resolvePromise();
  }, ms));
  if (message.op === "record" && (held.length || existsSync(HOLD))) held.push([message.id, result]);
  else if (message.op === "record" && RECORD_MS) return later(RECORD_MS);
  else if (INPUT_OPS.includes(message.op) && inputHeld(message)) {
    return new Promise((resolvePromise) => {
      const poll = setInterval(() => {
        if (inputHeld(message)) return;
        clearInterval(poll);
        answer(message.id, result);
        resolvePromise();
      }, 10);
    });
  }
  else if (INPUT_OPS.includes(message.op) && INPUT_MS) return later(INPUT_MS);
  else answer(message.id, result);
  return Promise.resolve();
}

let turn = Promise.resolve();
createInterface({ input: process.stdin }).on("line", (line) => {
  if (IN_ORDER) turn = turn.then(() => handle(line));
  else handle(line);
});
`;

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

// Tests that spend nearly all their time waiting on the Canvas's own timers: the 200 ms
// clipboard settle after each paste, the 15 s idle stop, the 2 s display reading, the
// 2.5 s key frame wait. Each has its own Canvas process, fake device and temporary
// directory, so they run side by side at the end of this file (see the describe below),
// which keeps the whole file well inside the time its gate allows.
const waitingTests = [];
function waitingTest(name, fn) {
  waitingTests.push([name, fn]);
}

async function waitFor(predicate, timeoutMs = 5000, what = "condition") {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(20);
  }
}

/** scrcpy control messages as scrcpy-server would read them (independent of scrcpy-lib). */
function decodeControlMessages(buffer) {
  const messages = [];
  let offset = 0;
  for (;;) {
    const data = buffer.subarray(offset);
    if (!data.length) break;
    const type = data[0];
    let size;
    let message;
    if (type === 0) {
      size = 14;
      if (data.length < size) break;
      message = { type: "key", action: data[1], keycode: data.readInt32BE(2),
        repeat: data.readInt32BE(6), meta: data.readInt32BE(10) };
    } else if (type === 1) {
      if (data.length < 5) break;
      size = 5 + data.readUInt32BE(1);
      if (data.length < size) break;
      message = { type: "text", text: data.toString("utf8", 5, size) };
    } else if (type === 2) {
      size = 32;
      if (data.length < size) break;
      message = { type: "touch", action: data[1], pointerId: Number(data.readBigInt64BE(2)),
        x: data.readInt32BE(10), y: data.readInt32BE(14), width: data.readUInt16BE(18),
        height: data.readUInt16BE(20), pressure: data.readUInt16BE(22),
        actionButton: data.readInt32BE(24), buttons: data.readInt32BE(28) };
    } else if (type === 3) {
      size = 21;
      if (data.length < size) break;
      message = { type: "scroll", x: data.readInt32BE(1), y: data.readInt32BE(5),
        width: data.readUInt16BE(9), height: data.readUInt16BE(11),
        hScroll: data.readInt16BE(13), vScroll: data.readInt16BE(15), buttons: data.readInt32BE(17) };
    } else if (type === 4 || type === 8 || type === 10) {
      size = 2;
      if (data.length < size) break;
      message = { type: { 4: "back-or-screen-on", 8: "get-clipboard", 10: "display-power" }[type],
        action: data[1] };
    } else if ([5, 6, 7, 11, 17].includes(type)) {
      size = 1;
      message = { type: { 5: "notifications", 6: "quick-settings", 7: "collapse", 11: "rotate",
        17: "reset-video" }[type] };
    } else if (type === 9) {
      if (data.length < 14) break;
      size = 14 + data.readUInt32BE(10);
      if (data.length < size) break;
      message = { type: "set-clipboard", sequence: data.readBigUInt64BE(1), paste: data[9] === 1,
        text: data.toString("utf8", 14, size) };
    } else {
      throw new Error(`unknown control message type ${type}`);
    }
    messages.push(message);
    offset += size;
  }
  return { messages, rest: buffer.subarray(offset) };
}

/**
 * An in-process scrcpy-server stand-in. The fake adb `forward tcp:0` prints the
 * port of `forward`, and its `shell ... app_process` connects to `launcher` and
 * stays alive while the device keeps that connection open.
 */
class FakeScrcpyDevice extends EventEmitter {
  constructor({
    width = 570, height = 1280, codec = H264, resetAnswers = true, listenDelayMs = 0,
    keep = true, clipboardAnswer = null, autoAck = true, inputLatencyMs = 20, dropStaleSize = false,
  } = {}) {
    super();
    this.listenDelayMs = listenDelayMs;
    this.size = { width, height };
    this.codec = codec;
    this.resetAnswers = resetAnswers;
    // Floods turn this off: control messages are then only counted, by type.
    this.keep = keep;
    // {text, delayMs}: answer every GET_CLIPBOARD with this text after the delay.
    this.clipboardAnswer = clipboardAnswer;
    // Like scrcpy-server: a SET_CLIPBOARD with a sequence is acknowledged once it is read.
    // Off, the test acknowledges with ack(), or never.
    this.autoAck = autoAck;
    // The focused text field: input reaches it this long after the device read it, and a
    // paste types whatever the clipboard holds by then, as on Android.
    this.inputLatencyMs = inputLatencyMs;
    this.clipboard = "";
    this.field = "";
    // Like scrcpy-server's PositionMapper: a touch or scroll made for another video size
    // than the current one is ignored (kept in `ignored`).
    this.dropStaleSize = dropStaleSize;
    this.ignored = [];
    // When each ignored message was read.
    this.ignoredAt = [];
    // Device pointer ids down on the device, from the touches it applied.
    this.pointersDown = new Set();
    // Keycodes down on the device, from the key events it applied.
    this.keysDown = new Set();
    this.counts = new Map();
    this.spawns = [];
    this.running = null;
    this.controlBytes = 0;
    this.messages = [];
    // When each kept message was read, by index.
    this.arrivals = [];
    this.seq = 0;
    this.sockets = new Set();
  }

  count(type) {
    return this.counts.get(type) ?? 0;
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
      let line = "";
      socket.on("data", (chunk) => {
        line += chunk.toString();
        if (!line.includes("\n")) return;
        const { command } = JSON.parse(line.split("\n")[0]);
        this.spawns.push(command);
        const server = { launcher: socket, command, video: null, control: null, pending: Buffer.alloc(0) };
        socket.on("close", () => {
          if (this.running === server) this.#end(server);
          this.emit("launcher-closed", server);
        });
        // A slow device: the server listens on its socket only after a while.
        setTimeout(() => {
          if (!socket.destroyed) this.running = server;
        }, this.listenDelayMs).unref();
        this.emit("spawn", command);
      });
    });
    await Promise.all([this.forward, this.launcher].map((server) =>
      new Promise((resolvePromise) => server.listen(0, "127.0.0.1", resolvePromise))));
    return { forwardPort: this.forward.address().port, launcherPort: this.launcher.address().port };
  }

  #accept(socket) {
    const server = this.running;
    // adb accepts a forwarded connection and closes it when nothing listens on the device.
    if (!server || (server.video && server.control)) {
      socket.destroy();
      return;
    }
    // Like scrcpy-server, the device server ends once its client closes a socket.
    socket.on("close", () => {
      if (this.running === server) this.#end(server);
    });
    if (!server.video) {
      server.video = socket;
      socket.write(Buffer.from([0]));
      return;
    }
    server.control = socket;
    socket.on("data", (chunk) => this.#onControl(server, chunk));
    const meta = Buffer.alloc(68);
    meta.write("Fake Pixel", 0, "utf8");
    meta.writeUInt32BE(this.codec, 64);
    server.video.write(meta);
    this.sendSession();
    this.emit("connected", server);
  }

  #onControl(server, chunk) {
    this.controlBytes += chunk.length;
    server.pending = Buffer.concat([server.pending, chunk]);
    const { messages, rest } = decodeControlMessages(server.pending);
    server.pending = rest;
    for (const message of messages) {
      this.counts.set(message.type, this.count(message.type) + 1);
      if (this.dropStaleSize && (message.type === "touch" || message.type === "scroll") &&
        (message.width !== this.size.width || message.height !== this.size.height)) {
        this.ignored.push(message);
        this.ignoredAt.push(Date.now());
        continue;
      }
      if (this.keep) {
        this.messages.push(message);
        this.arrivals.push(Date.now());
        this.#type(message);
        if (message.type === "touch" && message.action === 0) this.pointersDown.add(message.pointerId);
        if (message.type === "touch" && message.action === 1) this.pointersDown.delete(message.pointerId);
        if (message.type === "key" && message.action === 0) this.keysDown.add(message.keycode);
        if (message.type === "key" && message.action === 1) this.keysDown.delete(message.keycode);
      }
      this.emit("control", message);
      if (message.type === "set-clipboard" && message.sequence !== 0n && this.autoAck) {
        setTimeout(() => this.ack(message.sequence), 0);
      }
      if (message.type === "get-clipboard" && this.clipboardAnswer) {
        setTimeout(() => this.sendClipboard(this.clipboardAnswer.text), this.clipboardAnswer.delayMs);
      }
      if (message.type === "reset-video" && this.resetAnswers) {
        // A capture reset starts a new encoder session: session, config, key frame.
        this.sendSession();
        this.sendConfig();
        this.sendPacket({ key: true });
      }
    }
  }

  /**
   * scrcpy-server's Controller sets the clipboard at once and only injects the paste key,
   * so the app reads the clipboard when it handles that key, after any newer SET_CLIPBOARD.
   */
  #type(message) {
    let typed = null;
    if (message.type === "set-clipboard") {
      this.clipboard = message.text;
      if (message.paste) typed = () => this.clipboard;
    } else if (message.type === "text") {
      typed = () => message.text;
    } else if (message.type === "key" && message.action === 0 && message.keycode >= 29 && message.keycode <= 54) {
      typed = () => String.fromCharCode(97 + message.keycode - 29);
    }
    if (typed) setTimeout(() => { this.field += typed(); }, this.inputLatencyMs).unref();
  }

  /** ACK_CLIPBOARD: device message type 1, then the u64 sequence. */
  ack(sequence) {
    const message = Buffer.alloc(9);
    message[0] = 1;
    message.writeBigUInt64BE(sequence, 1);
    this.running?.control?.write(message);
  }

  #end(server) {
    server.video?.destroy();
    server.control?.destroy();
    server.launcher.destroy();
    if (this.running === server) this.running = null;
    this.emit("ended", server);
  }

  get connected() {
    return Boolean(this.running?.video && this.running?.control);
  }

  #writeVideo(buffer) {
    const video = this.running?.video;
    if (!video || video.destroyed) return false;
    video.write(buffer);
    return true;
  }

  /** The device rotates: its server maps events for this size now; sendSession() tells the client later. */
  resize(width, height) {
    this.size = { width, height };
  }

  sendSession(width = this.size.width, height = this.size.height) {
    this.size = { width, height };
    const header = Buffer.alloc(12);
    header[0] = 0x80;
    header.writeUInt32BE(width, 4);
    header.writeUInt32BE(height, 8);
    this.#writeVideo(header);
  }

  sendConfig(data = CONFIG) {
    const header = Buffer.alloc(12);
    header.writeBigUInt64BE(1n << 62n, 0);
    header.writeUInt32BE(data.length, 8);
    this.#writeVideo(Buffer.concat([header, data]));
  }

  /** A media packet whose payload carries its sequence number; returns the payload. */
  sendPacket({ key = false, size = 64 } = {}) {
    const seq = this.seq++;
    const payload = Buffer.alloc(Math.max(16, size), seq & 0xff);
    payload.writeUInt32BE(1, 0);
    payload[4] = key ? 0x65 : 0x41;
    payload.writeUInt32BE(seq, 5);
    const header = Buffer.alloc(12);
    let flags = BigInt(seq * 33_333);
    if (key) flags |= 1n << 61n;
    header.writeBigUInt64BE(flags, 0);
    header.writeUInt32BE(payload.length, 8);
    this.#writeVideo(Buffer.concat([header, payload]));
    return payload;
  }

  /** The device-side server dies: its sockets close and the adb shell exits. */
  crash() {
    if (this.running) this.#end(this.running);
  }

  sendClipboard(text) {
    const bytes = Buffer.from(text, "utf8");
    const message = Buffer.alloc(5 + bytes.length);
    message[0] = 0;
    message.writeUInt32BE(bytes.length, 1);
    bytes.copy(message, 5);
    this.running?.control?.write(message);
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
    : length < 65536 ? Buffer.from([0x80 | opcode, 0x80 | 126, length >> 8, length & 0xff])
      : (() => {
        const value = Buffer.alloc(10);
        value[0] = 0x80 | opcode;
        value[1] = 0x80 | 127;
        value.writeBigUInt64BE(BigInt(length), 2);
        return value;
      })();
  const masked = Buffer.alloc(length);
  for (let i = 0; i < length; i += 1) masked[i] = payload[i] ^ mask[i & 3];
  return Buffer.concat([header, mask, masked]);
}

/** A raw RFC 6455 client: arbitrary headers, and it can stop reading like a stalled tab. */
class TestSocket extends EventEmitter {
  constructor(socket, rest) {
    super();
    this.socket = socket;
    this.messages = [];
    // Floods turn this off: messages are then only counted, by JSON type, and emitted.
    this.keep = true;
    this.counts = new Map();
    this.received = 0;
    this.closeCode = null;
    this.closed = new Promise((resolvePromise) => {
      socket.on("close", () => resolvePromise(this.closeCode));
    });
    this.buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => this.#data(chunk));
    if (rest.length) this.#data(rest);
  }

  #data(chunk) {
    this.received += chunk.length;
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
      if (opcode === 1) this.#push({ text: payload.toString("utf8"), json: JSON.parse(payload.toString("utf8")) });
      else if (opcode === 2) this.#push({ binary: Buffer.from(payload) });
      else if (opcode === 8) {
        this.closeCode = payload.length >= 2 ? payload.readUInt16BE(0) : 1005;
        this.socket.end();
      }
    }
  }

  #push(message) {
    const type = message.json?.t ?? null;
    this.counts.set(type, this.count(type) + 1);
    if (this.keep) this.messages.push(message);
    this.emit("message", message);
  }

  #frame(opcode, payload) {
    this.socket.write(clientFrame(opcode, payload));
  }

  count(type) {
    return this.counts.get(type) ?? 0;
  }

  /** The next JSON message from now on that matches; earlier ones are not looked at. */
  until(predicate, timeoutMs = 10_000) {
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => {
        this.off("message", listener);
        reject(new Error("timed out waiting for a WebSocket message"));
      }, timeoutMs);
      const listener = (message) => {
        if (!message.json || !predicate(message.json)) return;
        clearTimeout(timer);
        this.off("message", listener);
        resolvePromise(message.json);
      };
      this.on("message", listener);
    });
  }

  /**
   * Send `count` text messages, `messageAt(i)`, as fast as the socket takes them. Frames
   * go out in batches, and only past `highWater` unsent bytes does this wait: when the
   * Canvas stops reading, the flood waits here, in the test process. Between batches the
   * fakes in this process (device, readers, /status checks) get their turn.
   */
  async flood(count, messageAt, highWater = 4 * 1024 * 1024) {
    const frames = new Map();
    let batch = [];
    let bytes = 0;
    const flush = async () => {
      if (!batch.length) return;
      this.socket.write(Buffer.concat(batch, bytes));
      batch = [];
      bytes = 0;
      await new Promise(setImmediate);
      if (this.socket.writableLength > highWater && !this.socket.destroyed) {
        await new Promise((resolvePromise) => {
          const done = () => {
            this.socket.off("drain", done);
            this.socket.off("close", done);
            resolvePromise();
          };
          this.socket.on("drain", done);
          this.socket.on("close", done);
        });
      }
    };
    for (let i = 0; i < count; i += 1) {
      const text = messageAt(i);
      let frame = frames.get(text);
      if (!frame) {
        frame = clientFrame(1, Buffer.from(text, "utf8"));
        if (frames.size < 4096) frames.set(text, frame);
      }
      batch.push(frame);
      bytes += frame.length;
      if (bytes >= 64 * 1024) await flush();
    }
    await flush();
  }

  send(value) {
    if (typeof value === "string") this.#frame(1, Buffer.from(value, "utf8"));
    else if (Buffer.isBuffer(value)) this.#frame(2, value);
    else this.#frame(1, Buffer.from(JSON.stringify(value), "utf8"));
  }

  close(code = 1000) {
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(code, 0);
    this.#frame(8, payload);
  }

  json(type) {
    return this.messages.filter((message) => message.json && (!type || message.json.t === type))
      .map((message) => message.json);
  }

  /** Video messages decoded from the Canvas binary layout. */
  video() {
    return this.messages.filter((message) => message.binary).map(({ binary }) => {
      if (binary[0] === 1) return { kind: "session", width: binary.readUInt32BE(1), height: binary.readUInt32BE(5) };
      if (binary[0] === 2) return { kind: "config", data: binary.subarray(1) };
      return {
        kind: "packet",
        keyFrame: (binary[1] & 1) === 1,
        pts: Number(binary.readBigUInt64BE(2)),
        data: binary.subarray(10),
        seq: binary.readUInt32BE(15),
      };
    });
  }

  packets() {
    return this.video().filter((message) => message.kind === "packet");
  }

  async next(predicate, timeoutMs = 5000) {
    const found = this.messages.find(predicate);
    if (found) return found;
    return await new Promise((resolvePromise, reject) => {
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

  reply(predicate, timeoutMs) {
    const start = this.messages.length;
    return this.next((message) => this.messages.indexOf(message) >= start && message.json &&
      predicate(message.json), timeoutMs).then((message) => message.json);
  }
}

/**
 * Open a WebSocket by hand; resolves {status, body} for a refused upgrade, and
 * status 0 when the connection closed without any response.
 */
function openSocket(port, path, { headers = {}, host = `127.0.0.1:${port}` } = {}) {
  return new Promise((resolvePromise, reject) => {
    const socket = connect(port, "127.0.0.1");
    let data = Buffer.alloc(0);
    let status = 0;
    let bodyStart = 0;
    socket.on("error", reject);
    socket.on("close", () => resolvePromise({ status, body: data.subarray(bodyStart).toString() }));
    socket.on("connect", () => {
      const lines = [
        `GET ${path} HTTP/1.1`,
        `Host: ${host}`,
        "Upgrade: websocket",
        "Connection: Upgrade",
        `Sec-WebSocket-Key: ${randomBytes(16).toString("base64")}`,
        "Sec-WebSocket-Version: 13",
        ...Object.entries(headers).map(([name, value]) => `${name}: ${value}`),
      ];
      socket.write(`${lines.join("\r\n")}\r\n\r\n`);
    });
    let length = null;
    const onData = (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (!status) {
        const end = data.indexOf("\r\n\r\n");
        if (end < 0) return;
        const head = data.toString("latin1", 0, end);
        status = Number(head.split(" ")[1]);
        bodyStart = end + 4;
        if (status === 101) {
          socket.off("data", onData);
          resolvePromise({ status, ws: new TestSocket(socket, data.subarray(bodyStart)) });
          return;
        }
        const match = head.match(/\r\ncontent-length: *(\d+)/i);
        length = match ? Number(match[1]) : null;
      }
      // Refused by the normal handler, which keeps the connection: done at its length.
      if (length !== null && data.length - bodyStart >= length) socket.destroy();
    };
    socket.on("data", onData);
  });
}

async function makeWorld(t, { env = {}, device = {} } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-scrcpy-"));
  const bin = join(directory, "bin");
  await mkdir(bin);
  const adbLog = join(directory, "adb.log");
  const bridgeLog = join(directory, "bridge.log");
  const wmState = join(directory, "wm-state.json");
  await writeFile(adbLog, "");
  await writeFile(bridgeLog, "");
  const fakeAdb = join(directory, "fake-adb.mjs");
  await writeFile(fakeAdb, `#!${process.execPath}\n${FAKE_ADB}`);
  await chmod(fakeAdb, 0o755);
  const fakeBridge = join(directory, "fake-bridge.mjs");
  await writeFile(fakeBridge, FAKE_BRIDGE);
  const serverFile = join(directory, "scrcpy-server-v4.1");
  await writeFile(serverFile, "not a real server");
  const fake = new FakeScrcpyDevice(device);
  const ports = await fake.listen();
  const world = {
    directory, bin, adbLog, bridgeLog, wmState, fakeAdb, fakeBridge, serverFile, device: fake, children: [],
    env: {
      // Only the temporary bin directory: no real adb, scrcpy or ffmpeg can be found.
      PATH: bin,
      HOME: directory,
      AUTONOM_HOME: join(directory, "autonom-home"),
      FAKE_ADB_LOG: adbLog,
      FAKE_BRIDGE_LOG: bridgeLog,
      FAKE_WM_STATE: wmState,
      FAKE_PNG: PNG_BASE64,
      FAKE_SCRCPY_PORT: String(ports.forwardPort),
      FAKE_SCRCPY_LAUNCHER_PORT: String(ports.launcherPort),
      ...env,
    },
  };
  t.after(async () => {
    for (const child of world.children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    await fake.close();
    await rm(directory, { recursive: true, force: true });
  });
  return world;
}

/** Start the Canvas; resolves once it prints its URL, or with its exit for a refused start. */
function startCanvas(world, args, { expectExit = false, nodeArgs = [] } = {}) {
  const child = spawn(process.execPath, [
    ...nodeArgs, CANVAS, "--adb", world.fakeAdb, "--serial", SERIAL, "--port", "0", "--token", TOKEN,
    "--python", process.execPath, "--bridge", world.fakeBridge, ...args,
  ], { cwd: ROOT, env: world.env, stdio: ["ignore", "pipe", "pipe"] });
  world.children.push(child);
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  const exited = new Promise((resolvePromise) => {
    child.once("exit", (code, signal) => resolvePromise({ code, signal, stdout, stderr }));
  });
  if (expectExit) return exited;
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(`Canvas did not start: ${stdout}${stderr}`)), 15_000);
    child.stdout.on("data", () => {
      const match = stdout.match(/Preview at (http:\/\/127\.0\.0\.1:(\d+)\/)/);
      if (!match) return;
      clearTimeout(timer);
      resolvePromise({
        child, origin: match[1].slice(0, -1), port: Number(match[2]), exited,
        output: () => stdout + stderr,
      });
    });
    exited.then(({ code }) => {
      clearTimeout(timer);
      reject(new Error(`Canvas exited ${code}: ${stdout}${stderr}`));
    });
  });
}

async function login(canvas) {
  const response = await fetch(`${canvas.origin}/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: TOKEN }),
  });
  assert.equal(response.status, 200);
  const { csrf } = await response.json();
  return { csrf, cookie: response.headers.get("set-cookie").split(";", 1)[0] };
}

async function status(canvas) {
  const response = await fetch(`${canvas.origin}/status`, { headers: { Authorization: `Bearer ${TOKEN}` } });
  assert.equal(response.status, 200);
  return await response.json();
}

async function pageSocket(canvas, auth, path, headers = {}) {
  const opened = await openSocket(canvas.port, `${path}?csrf=${encodeURIComponent(auth.csrf)}`, {
    headers: { Cookie: auth.cookie, Origin: canvas.origin, ...headers },
  });
  assert.equal(opened.status, 101, opened.body);
  return opened.ws;
}

/** An adb call that starts the streaming scrcpy-server (not the encoder listing). */
function isServerStart(args) {
  return args[2] === "shell" && Boolean(args[3]?.includes("app_process")) && !args[3].includes("list_encoders=true");
}

/** An adb call that runs scrcpy-server to list the device's encoders. */
function isEncoderProbe(args) {
  return args[2] === "shell" && Boolean(args[3]?.includes("list_encoders=true"));
}

async function adbCalls(world) {
  return (await readFile(world.adbLog, "utf8")).trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function bridgeCalls(world, op) {
  const lines = (await readFile(world.bridgeLog, "utf8")).trim().split("\n").filter(Boolean);
  return lines.map((line) => JSON.parse(line)).filter((message) => !op || message.op === op);
}

async function stopCanvas(canvas) {
  canvas.child.kill("SIGTERM");
  return await canvas.exited;
}

/** A Canvas on the scrcpy transport with a page session and a streaming fake device. */
async function streamingCanvas(t, options = {}) {
  const world = await makeWorld(t, options);
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile,
    ...(options.args ?? [])], { nodeArgs: options.nodeArgs });
  const auth = await login(canvas);
  return { world, canvas, auth, device: world.device };
}

async function connectedControl(t, options) {
  const setup = await streamingCanvas(t, options);
  const control = await pageSocket(setup.canvas, setup.auth, "/ws/control");
  await waitFor(() => setup.device.connected, 8000, "the fake device connection");
  await control.next((message) => message.json?.t === "state" && message.json.session === "streaming" &&
    message.json.width === setup.device.size.width);
  return { ...setup, control };
}

async function expectNoDeviceWrite(device, before, ms = 150) {
  await sleep(ms);
  assert.equal(device.controlBytes, before, "the device control socket received bytes");
}

// The most a flood of control messages may add to the Canvas process's resident memory.
const FLOOD_MEMORY_BOUND = 64 * 1024 * 1024;
// How long the slow side of a flood test stops completely while the flood arrives.
const FLOOD_STALL_MS = 1500;
// Messages each flood test first sends at full speed, so the Canvas heap has grown to
// its working size before memory is measured: the bound then measures what waits in
// queues, not how much room the garbage collector takes.
const FLOOD_WARM_UP = 20_000;
// For the same reason the young generation has a fixed size: left to grow, it alone
// adds up to 32 MiB of resident memory once input arrives fast, whatever is retained.
const FLOOD_NODE_ARGS = ["--min-semi-space-size=16", "--max-semi-space-size=16"];

/** Resident memory of a process, in bytes. */
async function residentBytes(pid) {
  const { stdout } = await execFileAsync("ps", ["-o", "rss=", "-p", String(pid)]);
  return Number(stdout.trim()) * 1024;
}

async function statusWithin(canvas, timeoutMs) {
  const response = await fetch(`${canvas.origin}/status`, {
    headers: { Authorization: `Bearer ${TOKEN}` }, signal: AbortSignal.timeout(timeoutMs),
  });
  assert.equal(response.status, 200);
  return await response.json();
}

// How long GET /status may take while a flood test samples it, unless the test allows more.
const STATUS_SAMPLE_MS = 10_000;

/**
 * Run `work` while sampling the Canvas process: its resident memory, and GET /status,
 * which must keep answering within `statusTimeoutMs`. Returns the peak memory growth
 * over the start. With more time allowed for /status, memory is also sampled on its own,
 * so a slow answer does not thin out the memory samples.
 */
async function watchCanvas(canvas, work, { statusTimeoutMs = STATUS_SAMPLE_MS } = {}) {
  const before = await residentBytes(canvas.child.pid);
  let peak = before;
  const latencies = [];
  let done = false;
  const measure = async () => {
    peak = Math.max(peak, await residentBytes(canvas.child.pid));
  };
  const sample = async () => {
    await measure();
    const started = Date.now();
    await statusWithin(canvas, statusTimeoutMs);
    return Date.now() - started;
  };
  const sampling = (async () => {
    while (!done) {
      latencies.push(await sample());
      await sleep(100);
    }
  })();
  const measuring = statusTimeoutMs > STATUS_SAMPLE_MS ? (async () => {
    while (!done) {
      await measure();
      await sleep(100);
    }
  })() : null;
  try {
    await work();
  } finally {
    done = true;
    await Promise.all([sampling, measuring]);
  }
  await sample();
  return { growth: peak - before, latencies };
}

function assertBounded(t, watched, what) {
  const mib = (watched.growth / (1024 * 1024)).toFixed(1);
  t.diagnostic(`${what}: Canvas memory grew ${mib} MiB at most; /status answered in ` +
    `${watched.latencies.join(", ")} ms meanwhile`);
  assert.ok(watched.growth < FLOOD_MEMORY_BOUND, `${what} grew Canvas memory by ${mib} MiB`);
  assert.ok(watched.latencies.length >= 1, `/status was not asked during ${what}`);
}

/** Wait until a client has read nothing new for a while. */
async function quiet(ws, ms = 300) {
  for (let seen = -1; seen !== ws.received;) {
    seen = ws.received;
    await sleep(ms);
  }
}

/** A streaming Canvas whose device and control client count messages instead of keeping them. */
async function floodSetup(t, options = {}) {
  const setup = await connectedControl(t, {
    ...options, nodeArgs: FLOOD_NODE_ARGS, device: { keep: false, ...options.device },
  });
  setup.control.keep = false;
  return setup;
}

/**
 * Messages on one connection are handled in order, so this pong means all before it were.
 * A slow machine can take a long time over a flood, so the wait goes on while the client
 * still receives something (the states a flood of handoff changes sends it), and fails
 * after `stillMs` without anything new, or after `limitMs` in all.
 */
async function allHandled(ws, stillMs = 20_000, limitMs = 90_000) {
  const ts = -Date.now();
  const started = Date.now();
  let listener;
  const pong = new Promise((resolvePromise) => {
    listener = (message) => {
      if (message.json?.t === "pong" && message.json.ts === ts) resolvePromise(true);
    };
    ws.on("message", listener);
  });
  ws.send({ t: "ping", ts });
  let seen = ws.received;
  let movedAt = started;
  try {
    for (;;) {
      const timer = sleep(100).then(() => false);
      if (await Promise.race([pong, timer])) return;
      if (ws.received !== seen) {
        seen = ws.received;
        movedAt = Date.now();
      }
      const now = Date.now();
      if (now - movedAt > stillMs || now - started > limitMs) {
        const states = ws.counts.has("state") ? `, ${ws.count("state")} states among them` : "";
        throw new Error(`timed out waiting for the pong after the messages before it: ${now - started} ms, ` +
          `the client read ${ws.received} bytes${states}, the last ${now - movedAt} ms ago`);
      }
    }
  } finally {
    ws.off("message", listener);
  }
}

async function warmUp(ws, messageAt) {
  await ws.flood(FLOOD_WARM_UP, messageAt);
  await allHandled(ws);
}

/** The slow side stops completely for a while as the flood starts, then goes on. */
async function stallDuring(flooding, stop, go) {
  stop();
  await sleep(FLOOD_STALL_MS);
  go();
  await flooding;
}

// The record kinds of the approved bridge contract; it refuses any other kind.
const RECORD_KINDS = ["gesture", "scroll", "key", "text", "paste", "system", "control", "display"];

/** Journal records (of one kind); each stands for exactly one action, so none carries a count. */
async function journaled(world, kind = null) {
  const records = await bridgeCalls(world, "record");
  for (const record of records) {
    assert.ok(RECORD_KINDS.includes(record.payload.kind), `record kind ${record.payload.kind}`);
    assert.equal(record.payload.count, undefined, "a record stands for more than one action");
  }
  return kind ? records.filter((record) => record.payload.kind === kind) : records;
}

test("discovery: an explicit --scrcpy-server file named v4.1 selects scrcpy", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "auto", "--scrcpy-server", world.serverFile]);
  const body = await status(canvas);
  assert.equal(body.transport, "scrcpy");
  assert.equal(body.fallback_reason, null);
  assert.equal(body.scrcpy.version, "4.1");
  assert.equal(body.scrcpy.server_path, world.serverFile);
  assert.equal(body.scrcpy.session_state, "idle");
  await stopCanvas(canvas);
});

test("discovery: --scrcpy-version names an unversioned file", async (t) => {
  const world = await makeWorld(t);
  const file = join(world.directory, "server.jar");
  await writeFile(file, "x");
  const canvas = await startCanvas(world, ["--scrcpy-server", file, "--scrcpy-version", "4.1"]);
  assert.equal((await status(canvas)).transport, "scrcpy");
  await stopCanvas(canvas);
});

test("discovery: AUTONOM_SCRCPY_SERVER wins over SCRCPY_SERVER_PATH", async (t) => {
  const world = await makeWorld(t);
  const old = join(world.directory, "scrcpy-server-v3.3");
  await writeFile(old, "x");
  world.env.AUTONOM_SCRCPY_SERVER = world.serverFile;
  world.env.SCRCPY_SERVER_PATH = old;
  const canvas = await startCanvas(world, []);
  const body = await status(canvas);
  assert.equal(body.transport, "scrcpy");
  assert.equal(body.scrcpy.source, "AUTONOM_SCRCPY_SERVER");
  await stopCanvas(canvas);
});

test("discovery: SCRCPY_SERVER_PATH is used when AUTONOM_SCRCPY_SERVER is unset", async (t) => {
  const world = await makeWorld(t);
  world.env.SCRCPY_SERVER_PATH = world.serverFile;
  const canvas = await startCanvas(world, []);
  const body = await status(canvas);
  assert.equal(body.transport, "scrcpy");
  assert.equal(body.scrcpy.source, "SCRCPY_SERVER_PATH");
  await stopCanvas(canvas);
});

test("discovery: an installed scrcpy provides its server and version", async (t) => {
  const world = await makeWorld(t);
  const prefix = join(world.directory, "Cellar/scrcpy/4.1");
  await mkdir(join(prefix, "bin"), { recursive: true });
  await mkdir(join(prefix, "share/scrcpy"), { recursive: true });
  const binary = join(prefix, "bin/scrcpy");
  await writeFile(binary, `#!/bin/sh\necho "scrcpy 4.1 <https://github.com/Genymobile/scrcpy>"\n`);
  await chmod(binary, 0o755);
  await writeFile(join(prefix, "share/scrcpy/scrcpy-server"), "x");
  // A package manager puts a symlink on PATH; the server is found under the real prefix.
  await symlink(binary, join(world.bin, "scrcpy"));
  const canvas = await startCanvas(world, []);
  const body = await status(canvas);
  assert.equal(body.transport, "scrcpy");
  assert.equal(body.scrcpy.source, "scrcpy");
  assert.equal(body.scrcpy.server_path, await realpath(join(prefix, "share/scrcpy/scrcpy-server")));
  await stopCanvas(canvas);
});

test("discovery: auto falls back to screencap with the reason when only a 3.3 server exists", async (t) => {
  const world = await makeWorld(t);
  const old = join(world.directory, "scrcpy-server-v3.3");
  await writeFile(old, "x");
  world.env.AUTONOM_SCRCPY_SERVER = old;
  const canvas = await startCanvas(world, []);
  const body = await status(canvas);
  assert.equal(body.transport, "screencap");
  assert.match(body.fallback_reason, /scrcpy-server 3\.3 found/);
  assert.match(body.fallback_reason, /ffmpeg/);
  assert.equal(body.scrcpy, null);
  const refused = await openSocket(canvas.port, `/ws/video?token=${TOKEN}`);
  assert.equal(refused.status, 409);
  await stopCanvas(canvas);
});

test("discovery: auto without any server falls back and says why", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "auto"]);
  const body = await status(canvas);
  assert.equal(body.transport, "screencap");
  assert.match(body.fallback_reason, /no scrcpy-server found/);
  await stopCanvas(canvas);
});

test("discovery: --transport scrcpy without a server exits naming canvas.scrcpy", async (t) => {
  const world = await makeWorld(t);
  const missing = await startCanvas(world, ["--transport", "scrcpy"], { expectExit: true });
  assert.notEqual(missing.code, 0);
  assert.match(missing.stderr, /canvas\.scrcpy/);
  assert.match(missing.stderr, /brew install scrcpy/);
  const old = join(world.directory, "scrcpy-server-v3.3");
  await writeFile(old, "x");
  const wrong = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", old], { expectExit: true });
  assert.notEqual(wrong.code, 0);
  assert.match(wrong.stderr, /canvas\.scrcpy/);
  assert.match(wrong.stderr, /3\.3/);
  // Nothing was pushed or started on the device for a refused transport.
  assert.ok(!(await adbCalls(world)).some((args) => args.includes("push")));
});

test("discovery: screenrecord H.264 is probed by running it, as API 36 help omits the option", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_SCREENRECORD: "h264" } });
  const canvas = await startCanvas(world, ["--transport", "screencap"]);
  const body = await status(canvas);
  assert.equal(body.screenrecord_h264, true);
  assert.equal(body.direct_h264_url, "/stream.h264");
  const probe = (await adbCalls(world)).find((args) => args.includes("screenrecord"));
  assert.deepEqual(probe.slice(2, 5), ["exec-out", "screenrecord", "--output-format=h264"]);
  await stopCanvas(canvas);

  const without = await makeWorld(t);
  const plain = await startCanvas(without, ["--transport", "screencap"]);
  assert.equal((await status(plain)).screenrecord_h264, false);
  await stopCanvas(plain);
});

test("discovery: auto falls back at runtime when the device server cannot stream h264", async (t) => {
  const world = await makeWorld(t, { device: { codec: 0x68323635 } });
  const canvas = await startCanvas(world, ["--scrcpy-server", world.serverFile]);
  assert.equal((await status(canvas)).transport, "scrcpy");
  const opened = await openSocket(canvas.port, `/ws/video?token=${TOKEN}`);
  assert.equal(opened.status, 101);
  assert.equal(await opened.ws.closed, 1000);
  const body = await status(canvas);
  assert.equal(body.transport, "screencap");
  assert.match(body.fallback_reason, /scrcpy failed to start: .*not h264/);
  await waitFor(() => world.device.running === null, 5000, "the failed server to end");
  assert.equal(world.device.spawns.length, 1, "auto kept retrying a server that cannot stream");
  // The page now gets the multipart stream.
  const controller = new AbortController();
  const stream = await fetch(`${canvas.origin}/stream.mjpeg`, {
    headers: { Authorization: `Bearer ${TOKEN}` }, signal: controller.signal,
  });
  const first = await stream.body.getReader().read();
  assert.match(Buffer.from(first.value).toString("latin1"), /Content-Type: image\/png/);
  controller.abort();
  await stopCanvas(canvas);
});

test("discovery: the page script parses and the scrcpy page exposes its diagnostics", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--scrcpy-server", world.serverFile]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  assert.doesNotThrow(() => new Script(script));
  assert.match(script, /window\.autonomCanvas=/);
  assert.match(script, /scrcpy \(webcodecs\)/);
  assert.match(script, /VideoDecoder/);
  await stopCanvas(canvas);
});

// What curl --http2 and Java's default HttpClient send on plain HTTP.
const H2C_HEADERS = {
  Connection: "Upgrade, HTTP2-Settings",
  Upgrade: "h2c",
  "HTTP2-Settings": "AAMAAABkAAQAoAAAAAIAAAAA",
};

/**
 * One HTTP/1.1 request over a raw socket, headers exactly as given; resolves
 * {status, headers, body} once the body has its Content-Length, or the socket closed.
 */
function rawRequest(port, { method = "GET", path = "/", headers = {}, body = "" } = {}) {
  return new Promise((resolvePromise, reject) => {
    const socket = connect(port, "127.0.0.1");
    let data = Buffer.alloc(0);
    let done = false;
    const parse = () => {
      const end = data.indexOf("\r\n\r\n");
      if (end < 0) return null;
      const lines = data.toString("latin1", 0, end).split("\r\n");
      const head = Object.fromEntries(lines.slice(1).map((line) => {
        const colon = line.indexOf(":");
        return [line.slice(0, colon).trim().toLowerCase(), line.slice(colon + 1).trim()];
      }));
      return { end, status: Number(lines[0].split(" ")[1]), headers: head };
    };
    const finish = (response, bodyBytes) => {
      if (done) return;
      done = true;
      socket.destroy();
      resolvePromise({ status: response.status, headers: response.headers, body: bodyBytes.toString("utf8") });
    };
    socket.on("error", reject);
    socket.on("connect", () => {
      const lines = [`${method} ${path} HTTP/1.1`, `Host: 127.0.0.1:${port}`,
        ...Object.entries(headers).map(([name, value]) => `${name}: ${value}`)];
      if (body) lines.push(`Content-Length: ${Buffer.byteLength(body)}`);
      socket.write(`${lines.join("\r\n")}\r\n\r\n${body}`);
    });
    socket.on("data", (chunk) => {
      data = Buffer.concat([data, chunk]);
      const response = parse();
      if (!response || response.headers["content-length"] === undefined) return;
      const length = Number(response.headers["content-length"]);
      if (data.length - response.end - 4 >= length) {
        finish(response, data.subarray(response.end + 4, response.end + 4 + length));
      }
    });
    socket.on("close", () => {
      const response = parse();
      if (!done) {
        if (!response) reject(new Error("the connection closed without a response"));
        else finish(response, data.subarray(response.end + 4));
      }
    });
  });
}

// Old Node: http.createServer without shouldUpgradeCallback, so every request with an
// Upgrade header reaches the 'upgrade' event. Says so on stderr when it strips the option.
const OLD_NODE_UPGRADE = `data:text/javascript,${encodeURIComponent(`
import http from "node:http";
import { syncBuiltinESMExports } from "node:module";
const createServer = http.createServer;
http.createServer = function (options, listener) {
  if (options && typeof options === "object" && "shouldUpgradeCallback" in options) {
    const { shouldUpgradeCallback, ...rest } = options;
    process.stderr.write("createServer without shouldUpgradeCallback\\n");
    return createServer.call(this, rest, listener);
  }
  return createServer.apply(this, arguments);
};
syncBuiltinESMExports();
`)}`;

test("auth: a plain GET /status that carries h2c Upgrade headers gets the normal answer", async (t) => {
  const { canvas } = await streamingCanvas(t);
  const response = await rawRequest(canvas.port, {
    path: "/status", headers: { ...H2C_HEADERS, Authorization: `Bearer ${TOKEN}` },
  });
  assert.equal(response.status, 200, response.body);
  const body = JSON.parse(response.body);
  assert.equal(body.transport, "scrcpy");
  assert.equal(body.serial, SERIAL);
  // Without the token it is still refused, by the normal handler.
  assert.equal((await rawRequest(canvas.port, { path: "/status", headers: H2C_HEADERS })).status, 401);
  await stopCanvas(canvas);
});

test("auth: an authenticated POST of the old API with h2c Upgrade headers and a body behaves as without them", async (t) => {
  const { canvas } = await streamingCanvas(t);
  const post = (mode, extra) => rawRequest(canvas.port, {
    method: "POST", path: "/control", body: JSON.stringify({ mode }),
    headers: { ...extra, Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
  });
  const plain = await post("pause", {});
  const upgraded = await post("pause", H2C_HEADERS);
  assert.deepEqual([upgraded.status, JSON.parse(upgraded.body)], [plain.status, JSON.parse(plain.body)]);
  assert.deepEqual([upgraded.status, JSON.parse(upgraded.body).input_paused], [200, true]);
  const resumed = await post("resume", H2C_HEADERS);
  assert.deepEqual([resumed.status, JSON.parse(resumed.body).input_paused], [200, false]);
  assert.equal((await status(canvas)).input_paused, false);
  await stopCanvas(canvas);
});

test("auth: where Node has no shouldUpgradeCallback, the upgrade fallback answers bodiless h2c requests normally, refuses a body with 400, and WebSockets still open", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile],
    { nodeArgs: ["--import", OLD_NODE_UPGRADE] });
  const answered = await rawRequest(canvas.port, {
    path: "/status", headers: { ...H2C_HEADERS, Authorization: `Bearer ${TOKEN}` },
  });
  assert.equal(answered.status, 200, answered.body);
  assert.equal(JSON.parse(answered.body).transport, "scrcpy");
  assert.equal(answered.headers.connection, "close");
  // The answer came from the fallback: the server was built without the callback.
  assert.match(canvas.output(), /createServer without shouldUpgradeCallback/, "the old-Node seam is not active");
  const withBody = await rawRequest(canvas.port, {
    method: "POST", path: "/control", body: JSON.stringify({ mode: "pause" }),
    headers: { ...H2C_HEADERS, Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
  });
  assert.equal(withBody.status, 400, withBody.body);
  assert.match(JSON.parse(withBody.body).error, /resend the request without the Upgrade header/);
  // Nothing was applied, and the Canvas still answers and still opens its WebSockets.
  const auth = await login(canvas);
  assert.equal((await status(canvas)).input_paused, false);
  const control = await pageSocket(canvas, auth, "/ws/control");
  await control.next((message) => message.json?.t === "state");
  const refused = await openSocket(canvas.port, "/ws/control", { headers: { Origin: "http://evil.test" } });
  assert.equal(refused.status, 403);
  control.close();
  await stopCanvas(canvas);
});

test("auth: where Node has no shouldUpgradeCallback, the upgrade fallback reads Content-Length as digits: any run of zeros is no body, a signed length or a body gets 400", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile],
    { nodeArgs: ["--import", OLD_NODE_UPGRADE] });
  const statusWith = (headers, body = "") => rawRequest(canvas.port, {
    path: "/status", body, headers: { ...H2C_HEADERS, Authorization: `Bearer ${TOKEN}`, ...headers },
  });
  for (const length of ["0", "00", "0000"]) {
    const answered = await statusWith({ "Content-Length": length });
    assert.equal(answered.status, 200, `Content-Length: ${length} ${answered.body}`);
    assert.equal(JSON.parse(answered.body).transport, "scrcpy");
    assert.equal(answered.headers.connection, "close");
  }
  assert.match(canvas.output(), /createServer without shouldUpgradeCallback/, "the old-Node seam is not active");
  // A signed length is refused: by Node's own parser where it checks, else by the fallback.
  assert.equal((await statusWith({ "Content-Length": "+0" })).status, 400);
  // Content-Length: 5 with its body.
  const withBody = await statusWith({}, "hello");
  assert.equal(withBody.status, 400, withBody.body);
  assert.match(JSON.parse(withBody.body).error, /resend the request without the Upgrade header/);
  await stopCanvas(canvas);
});

test("auth: WebSocket upgrades need cookie, csrf and same origin, or the token", async (t) => {
  const { canvas, auth, device } = await streamingCanvas(t);
  const port = canvas.port;
  const control = (query = "") => `/ws/control${query}`;
  const csrf = `?csrf=${encodeURIComponent(auth.csrf)}`;
  const refusals = [
    [{}, control(), 401],
    [{ headers: { Origin: canvas.origin } }, control("?token=wrong"), 401],
    [{ headers: { Cookie: auth.cookie, Origin: canvas.origin } }, control(), 403],
    [{ headers: { Cookie: auth.cookie, Origin: canvas.origin } }, control("?csrf=wrong"), 403],
    [{ headers: { Cookie: auth.cookie } }, control(csrf), 403],
    [{ headers: { Cookie: auth.cookie, Origin: "http://evil.test" } }, control(csrf), 403],
    [{ headers: { Origin: "http://evil.test" } }, control(`?token=${TOKEN}`), 403],
    [{ headers: { Cookie: auth.cookie, Origin: canvas.origin }, host: "evil.test" }, control(csrf), 403],
    [{ headers: { Cookie: auth.cookie, Origin: canvas.origin } }, `/ws/other${csrf}`, 404],
    [{}, control(`?token=${TOKEN}&origin=robot`), 400],
    [{ headers: { Cookie: auth.cookie, Origin: canvas.origin } }, `/ws/video?csrf=wrong`, 403],
  ];
  for (const [options, path, expected] of refusals) {
    const result = await openSocket(port, path, options);
    assert.equal(result.status, expected, `${path} ${JSON.stringify(options)}`);
  }
  // DNS-rebinding guard on plain HTTP as well.
  const rebound = await new Promise((resolvePromise, reject) => {
    const request = httpRequest({
      host: "127.0.0.1", port, path: "/status",
      headers: { Authorization: `Bearer ${TOKEN}`, Host: "evil.test" },
    }, (response) => {
      response.resume();
      resolvePromise(response.statusCode);
    });
    request.on("error", reject);
    request.end();
  });
  assert.equal(rebound, 403);
  assert.equal(device.spawns.length, 0, "a refused upgrade started the device server");

  const viaToken = await openSocket(port, `/ws/control?token=${TOKEN}`);
  assert.equal(viaToken.status, 101);
  const viaBearer = await openSocket(port, "/ws/control", { headers: { Authorization: `Bearer ${TOKEN}` } });
  assert.equal(viaBearer.status, 101);
  viaToken.ws.close();
  viaBearer.ws.close();

  const ws = await pageSocket(canvas, auth, "/ws/control");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  await ws.next((message) => message.json?.session === "streaming" && message.json.width === 570);
  const before = device.controlBytes;
  const malformed = [
    "NaN", "{", "[]", "null", "42", JSON.stringify("touch"), JSON.stringify({}),
    JSON.stringify({ t: "nope" }),
    JSON.stringify({ t: "touch", a: "down", id: 1, x: 2, y: 0.5 }),
    JSON.stringify({ t: "touch", a: "down", id: 1, x: -0.1, y: 0.5 }),
    JSON.stringify({ t: "touch", a: "hover", id: 1, x: 0.5, y: 0.5 }),
    JSON.stringify({ t: "touch", a: "down", id: 1.5, x: 0.5, y: 0.5 }),
    JSON.stringify({ t: "touch", a: "down", id: 1, x: "0.5", y: 0.5 }),
    JSON.stringify({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5, p: 3 }),
    JSON.stringify({ t: "touch", a: "move", id: 77, x: 0.5, y: 0.5 }),
    JSON.stringify({ t: "scroll", x: 0.5, y: 0.5, dx: 17, dy: 0 }),
    JSON.stringify({ t: "scroll", x: 0.5, y: 0.5, dx: 0 }),
    JSON.stringify({ t: "key", a: "down", code: 999, meta: 0 }),
    JSON.stringify({ t: "key", a: "press", code: 29 }),
    JSON.stringify({ t: "key", a: "down", code: 29, meta: 0x800000 }),
    JSON.stringify({ t: "key", a: "down", code: 29, repeat: -1 }),
    JSON.stringify({ t: "text", text: "" }),
    JSON.stringify({ t: "text", text: "x".repeat(301) }),
    JSON.stringify({ t: "text", text: "ok", sensitive: "yes" }),
    JSON.stringify({ t: "paste", text: 5 }),
    JSON.stringify({ t: "system", op: "reboot" }),
    JSON.stringify({ t: "control", mode: "own" }),
    JSON.stringify({ t: "ping", ts: "now" }),
    JSON.stringify({ t: "touch", a: "down", id: 2 ** 40, x: 0.5, y: 0.5 }),
    JSON.stringify({ t: "scroll", x: 0.5, y: 1.5, dx: 1, dy: 1 }),
  ];
  assert.equal(malformed.length, 30);
  for (const raw of malformed) {
    const answer = ws.reply((message) => message.t === "error");
    ws.send(raw);
    const error = await answer;
    assert.ok(error.message, raw);
  }
  ws.send(Buffer.from([1, 2, 3]));
  await ws.reply((message) => message.t === "error");
  const pong = ws.reply((message) => message.t === "pong");
  ws.send({ t: "ping", ts: 12 });
  assert.equal((await pong).ts, 12, "the connection stayed open after the errors");
  await expectNoDeviceWrite(device, before);

  // An oversize message closes this connection with 1009 and still writes nothing.
  ws.send(JSON.stringify({ t: "paste", text: "x".repeat(70 * 1024) }));
  assert.equal(await ws.closed, 1009);
  await expectNoDeviceWrite(device, before);
  assert.equal((await status(canvas)).transport, "scrcpy");
  await stopCanvas(canvas);
});

/** A plain HTTP request with any headers and request target; resolves {status, headers, body}. */
function rawHttp(port, { method = "GET", path = "/", headers = {}, body = "" } = {}) {
  return new Promise((resolvePromise, reject) => {
    const request = httpRequest({
      host: "127.0.0.1", port, method, path,
      headers: { Host: `127.0.0.1:${port}`, ...headers },
    }, (response) => {
      let text = "";
      response.on("data", (chunk) => { text += chunk.toString(); });
      response.on("end", () => resolvePromise({ status: response.statusCode, headers: response.headers, body: text }));
    });
    request.on("error", reject);
    request.end(body);
  });
}

test("auth: a request target that is not a valid URL is refused and the Canvas keeps running", async (t) => {
  const { canvas, auth } = await streamingCanvas(t);
  for (const target of ["http://a:99999/ws/video", "http://127.0.0.1:99999/ws/control"]) {
    const refused = await openSocket(canvas.port, target, { headers: { Cookie: auth.cookie, Origin: canvas.origin } });
    assert.equal(refused.status, 400, target);
  }
  const plain = await rawHttp(canvas.port, { path: "http://a:99999/status" });
  assert.equal(plain.status, 400);
  assert.equal(canvas.child.exitCode, null, "the Canvas ended");
  assert.equal((await status(canvas)).transport, "scrcpy");
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
});

test("auth: a reloaded page gets its CSRF value back with its cookie from its own origin only", async (t) => {
  const { canvas, auth, device } = await streamingCanvas(t);
  const resume = (headers) => rawHttp(canvas.port, {
    method: "POST", path: "/auth", body: "{}", headers: { "Content-Type": "application/json", ...headers },
  });
  const sameOrigin = await resume({ Cookie: auth.cookie, Origin: canvas.origin });
  assert.equal(sameOrigin.status, 200);
  assert.equal(JSON.parse(sameOrigin.body).csrf, auth.csrf);
  // Under Referrer-Policy: no-referrer a browser may send Origin: null on its own POST.
  const nullOrigin = await resume({ Cookie: auth.cookie, Origin: "null", "Sec-Fetch-Site": "same-origin" });
  assert.equal(JSON.parse(nullOrigin.body).csrf, auth.csrf);
  // Another local site shares the cookie but is not this origin.
  const otherPort = `http://127.0.0.1:${canvas.port === 1 ? 2 : 1}`;
  const refusals = [
    [{ Cookie: auth.cookie, Origin: otherPort, "Sec-Fetch-Site": "same-site" }, 403],
    [{ Cookie: auth.cookie, Origin: "http://evil.test" }, 403],
    [{ Cookie: auth.cookie }, 403],
    [{ Origin: canvas.origin }, 401],
    [{ Cookie: "autonom_session=forged", Origin: canvas.origin }, 401],
  ];
  for (const [headers, expected] of refusals) {
    const result = await resume(headers);
    assert.equal(result.status, expected, JSON.stringify(headers));
    assert.ok(!result.body.includes(auth.csrf), "the CSRF value leaked");
  }
  // With the value back, the page's WebSocket works again.
  const control = await pageSocket(canvas, { ...auth, csrf: JSON.parse(sameOrigin.body).csrf }, "/ws/control");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  control.close();
  await stopCanvas(canvas);
});

test("auth: the page cannot be framed by another local origin, so a framed page never gets the CSRF value", async (t) => {
  const { canvas, auth } = await streamingCanvas(t);
  const page = await rawHttp(canvas.port, { headers: { "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none" } });
  assert.equal(page.status, 200);
  const policy = page.headers["content-security-policy"];
  assert.match(policy, /(^|;)\s*frame-ancestors 'none'\s*(;|$)/);
  for (const directive of ["default-src 'self'", "img-src 'self' data:", "style-src 'unsafe-inline'",
    "script-src 'unsafe-inline'", "connect-src 'self'"]) {
    assert.ok(policy.includes(directive), `the policy lost ${directive}`);
  }
  assert.equal(page.headers["x-frame-options"], "DENY");
  assert.match(page.body, /window\.autonomCanvas=/);

  // A page on another port of 127.0.0.1 is the same site, so the cookie would go along.
  const otherPort = `http://127.0.0.1:${canvas.port === 1 ? 2 : 1}`;
  for (const destination of ["iframe", "frame", "embed", "object", "fencedframe"]) {
    const framed = await rawHttp(canvas.port, {
      headers: { Cookie: auth.cookie, "Sec-Fetch-Dest": destination, "Sec-Fetch-Site": "same-site" },
    });
    assert.equal(framed.status, 403, destination);
    assert.ok(!framed.body.includes("autonomCanvas"), `the page was served into ${destination}`);
  }

  // Only the page's own fetch() may resume the session's CSRF value.
  const resume = (headers) => rawHttp(canvas.port, {
    method: "POST", path: "/auth", body: "{}",
    headers: { "Content-Type": "application/json", Cookie: auth.cookie, Origin: canvas.origin,
      "Sec-Fetch-Site": "same-origin", ...headers },
  });
  for (const destination of ["iframe", "frame", "document"]) {
    const refused = await resume({ "Sec-Fetch-Dest": destination });
    assert.equal(refused.status, 403, destination);
    assert.ok(!refused.body.includes(auth.csrf), "the CSRF value leaked");
  }
  const fetched = await resume({ "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors" });
  assert.equal(fetched.status, 200);
  assert.equal(JSON.parse(fetched.body).csrf, auth.csrf);
  await stopCanvas(canvas);
});

// scrcpy-server 4.1 `list_encoders=true` as the API 36 emulator prints it: every encoder is software.
const SOFTWARE_ENCODERS = [
  "[server] INFO: Device: [Google] google sdk_gphone64_arm64 (Android 16)",
  "[server] INFO: List of video encoders:",
  "    --video-codec=h264 --video-encoder=c2.android.avc.encoder        (sw)",
  "    --video-codec=h264 --video-encoder=OMX.google.h264.encoder       (sw) (alias for c2.android.avc.encoder)",
  "    --video-codec=h265 --video-encoder=c2.android.hevc.encoder       (sw)",
  "    --video-codec=av1 --video-encoder=c2.android.av1.encoder         (sw)",
  "",
].join("\n");
// A phone: a vendor hardware H.264 encoder (and its alias) ahead of the software one.
const HARDWARE_ENCODERS = [
  "[server] INFO: List of video encoders:",
  "    --video-codec=h264 --video-encoder=OMX.qcom.video.encoder.avc    (hw) [vendor] (alias for c2.qti.avc.encoder)",
  "    --video-codec=h264 --video-encoder=c2.qti.avc.encoder            (hw) [vendor]",
  "    --video-codec=h264 --video-encoder=c2.android.avc.encoder        (sw)",
  "    --video-codec=h265 --video-encoder=c2.qti.hevc.encoder           (hw) [vendor]",
  "",
].join("\n");

test("encoders: the H.264 lines of list_encoders say hardware or software", () => {
  assert.deepEqual(parseH264Encoders(SOFTWARE_ENCODERS), [
    { name: "c2.android.avc.encoder", hardware: false, alias: false },
    { name: "OMX.google.h264.encoder", hardware: false, alias: true },
  ]);
  assert.deepEqual(h264EncoderKind(parseH264Encoders(SOFTWARE_ENCODERS)),
    { encoder: "software", name: "c2.android.avc.encoder" });
  // The first hardware encoder that is not an alias names the kind.
  assert.deepEqual(h264EncoderKind(parseH264Encoders(HARDWARE_ENCODERS)),
    { encoder: "hardware", name: "c2.qti.avc.encoder" });
  // A hardware encoder of another codec does not count.
  const hevcOnly = "    --video-codec=h265 --video-encoder=c2.qti.hevc.encoder (hw) [vendor]\n" +
    "    --video-codec=h264 --video-encoder=c2.android.avc.encoder (sw)\r\n";
  assert.deepEqual(h264EncoderKind(parseH264Encoders(hevcOnly)),
    { encoder: "software", name: "c2.android.avc.encoder" });
  // Nothing listed, no H.264 encoder, or a line that says neither (Android 9): cannot tell.
  assert.equal(h264EncoderKind(parseH264Encoders("")), null);
  assert.equal(h264EncoderKind(parseH264Encoders(undefined)), null);
  assert.equal(h264EncoderKind(parseH264Encoders("[server] ERROR: Could not list encoders\n")), null);
  assert.equal(h264EncoderKind(parseH264Encoders("    --video-codec=h265 --video-encoder=x (sw)\n")), null);
  assert.equal(h264EncoderKind(parseH264Encoders(
    "    --video-codec=h264 --video-encoder=OMX.google.h264.encoder\n" +
    "    --video-codec=h264 --video-encoder=c2.android.avc.encoder (sw)\n")), null);
  assert.equal(h264EncoderKind([]), null);
  assert.equal(h264EncoderKind(null), null);
});

test("encoders: the probe runs the pushed server once per serial, bounded, and a failure is null", async () => {
  const calls = [];
  const answers = {
    "probe-sw": async () => ({ stdout: SOFTWARE_ENCODERS, stderr: "" }),
    "probe-hw": async () => ({ stdout: HARDWARE_ENCODERS, stderr: "" }),
    "probe-fail": async () => { throw Object.assign(new Error("Command failed"), { code: 1 }); },
    "probe-timeout": async () => { throw Object.assign(new Error("killed"), { killed: true, signal: "SIGTERM" }); },
    "probe-empty": async () => ({ stdout: "", stderr: "" }),
    "probe-throws": () => { throw new Error("spawn failed"); },
  };
  const exec = (file, args, options) => {
    calls.push({ file, args, options });
    return answers[args[1]]();
  };
  const software = await probeVideoEncoders({ adbPath: "/fake/adb", serial: "probe-sw", exec });
  assert.equal(software.encoder, "software");
  assert.equal(software.name, "c2.android.avc.encoder");
  assert.equal(software.encoders.length, 2);
  assert.deepEqual(calls[0].args.slice(0, 3), ["-s", "probe-sw", "shell"]);
  assert.equal(calls[0].args[3], "CLASSPATH=/data/local/tmp/autonom-scrcpy-4.1.jar app_process / " +
    "com.genymobile.scrcpy.Server 4.1 log_level=info cleanup=false list_encoders=true");
  assert.equal(calls[0].options.timeout, ENCODER_PROBE_TIMEOUT_MS);
  assert.ok(ENCODER_PROBE_TIMEOUT_MS <= 5000, "the probe may hold the stream back too long");
  // Asked once per serial: a second session gets the first answer.
  assert.equal(await probeVideoEncoders({ adbPath: "/fake/adb", serial: "probe-sw", exec }), software);
  assert.equal(calls.length, 1);
  assert.equal((await probeVideoEncoders({ adbPath: "/fake/adb", serial: "probe-hw", exec })).encoder, "hardware");
  for (const serial of ["probe-fail", "probe-timeout", "probe-empty", "probe-throws"]) {
    assert.equal(await probeVideoEncoders({ adbPath: "/fake/adb", serial, exec }), null, serial);
    // A failed probe is not repeated either.
    assert.equal(await probeVideoEncoders({ adbPath: "/fake/adb", serial, exec }), null, serial);
  }
  assert.equal(calls.length, 6);
});

/** A scrcpy Canvas streaming to one video client; resolves once the device server connected. */
async function sizedCanvas(t, { env = {}, args = [] } = {}) {
  const setup = await streamingCanvas(t, { env, args });
  const control = await pageSocket(setup.canvas, setup.auth, "/ws/control");
  const viewer = await pageSocket(setup.canvas, setup.auth, "/ws/video");
  await waitFor(() => setup.device.connected, 8000, "the fake device connection");
  const calls = await adbCalls(setup.world);
  const start = calls.find(isServerStart);
  const options = Object.fromEntries(start[3].split(" ").filter((word) => word.includes("="))
    .map((word) => word.split("=", 2)));
  return { ...setup, control, viewer, calls, start, options };
}

test("encoders: without --max-size a software-only device streams 2048 on the long side", async (t) => {
  const { canvas, control, calls, options } = await sizedCanvas(t, { env: { FAKE_ENCODERS: SOFTWARE_ENCODERS } });
  assert.equal(options.max_size, "2048");
  // The probe ran after the push and before the server.
  const order = calls.map((args) => isEncoderProbe(args) ? "probe" : isServerStart(args) ? "server"
    : args[2] === "push" ? "push" : null).filter(Boolean);
  assert.deepEqual(order, ["push", "probe", "server"]);
  const body = await status(canvas);
  assert.equal(body.scrcpy.max_size, 2048);
  assert.equal(body.scrcpy.max_size_source, "encoder");
  assert.equal(body.scrcpy.encoder, "software");
  assert.equal(body.scrcpy.encoder_name, "c2.android.avc.encoder");
  const state = await control.next((message) => message.json?.t === "state" && message.json.max_size !== null);
  assert.equal(state.json.max_size, 2048);
  assert.equal(state.json.encoder, "software");
  assert.match(canvas.output(), /Stream size: 2048 on the long side \(software H\.264 encoder c2\.android\.avc\.encoder\)/);
  await stopCanvas(canvas);
});

test("encoders: without --max-size a hardware H.264 encoder streams native size", async (t) => {
  const { canvas, options } = await sizedCanvas(t, { env: { FAKE_ENCODERS: HARDWARE_ENCODERS } });
  assert.equal(options.max_size, "0");
  const body = await status(canvas);
  assert.deepEqual([body.scrcpy.max_size, body.scrcpy.max_size_source, body.scrcpy.encoder, body.scrcpy.encoder_name],
    [0, "encoder", "hardware", "c2.qti.avc.encoder"]);
  assert.match(canvas.output(), /Stream size: native \(hardware H\.264 encoder c2\.qti\.avc\.encoder\)/);
  await stopCanvas(canvas);
});

test("encoders: a failed or empty encoder list keeps 1280", async (t) => {
  for (const listed of ["fail", ""]) {
    const { canvas, calls, options } = await sizedCanvas(t, { env: { FAKE_ENCODERS: listed } });
    assert.equal(options.max_size, "1280", listed);
    assert.equal(calls.filter(isEncoderProbe).length, 1);
    const body = await status(canvas);
    assert.deepEqual([body.scrcpy.max_size, body.scrcpy.max_size_source, body.scrcpy.encoder],
      [1280, "default", null]);
    assert.match(canvas.output(), /Stream size: 1280 on the long side \(the device's encoders could not be read\)/);
    await stopCanvas(canvas);
  }
});

test("encoders: an explicit --max-size wins without a probe, and 0 is native", async (t) => {
  for (const [value, word] of [["1024", "1024 on the long side"], ["0", "native"]]) {
    const { canvas, calls, options } = await sizedCanvas(t, {
      env: { FAKE_ENCODERS: HARDWARE_ENCODERS }, args: ["--max-size", value],
    });
    assert.equal(options.max_size, value);
    assert.equal(calls.filter(isEncoderProbe).length, 0, "a probe ran despite --max-size");
    const body = await status(canvas);
    assert.deepEqual([body.scrcpy.max_size, body.scrcpy.max_size_source, body.scrcpy.encoder],
      [Number(value), "explicit", null]);
    assert.match(canvas.output(), new RegExp(`Stream size: ${word} \\(--max-size\\)`));
    await stopCanvas(canvas);
  }
});

test("encoders: before any session the status names no size", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_ENCODERS: SOFTWARE_ENCODERS } });
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile]);
  const body = await status(canvas);
  assert.deepEqual([body.scrcpy.max_size, body.scrcpy.max_size_source, body.scrcpy.encoder], [null, null, null]);
  assert.ok(!(await adbCalls(world)).some(isEncoderProbe), "the encoders were probed without a client");
  await stopCanvas(canvas);
});

test("encoders: once auto falls back from scrcpy, state and /status report the size of the transport in use, not the size chosen for scrcpy", async (t) => {
  for (const withFfmpeg of [true, false]) {
    // A software-only device whose scrcpy-server cannot stream H.264: scrcpy chooses 2048, then fails.
    const world = await makeWorld(t, {
      env: { FAKE_ENCODERS: SOFTWARE_ENCODERS, FAKE_SCREENRECORD: "h264" }, device: { codec: 0x68323635 },
    });
    const args = ["--scrcpy-server", world.serverFile];
    if (withFfmpeg) {
      const ffmpeg = join(world.directory, "fake-ffmpeg.mjs");
      await writeFile(ffmpeg, `#!${process.execPath}\n${FAKE_FFMPEG}`);
      await chmod(ffmpeg, 0o755);
      args.push("--ffmpeg", ffmpeg);
    }
    const canvas = await startCanvas(world, args);
    assert.equal((await status(canvas)).transport, "scrcpy");
    const auth = await login(canvas);
    const control = await pageSocket(canvas, auth, "/ws/control");
    await pageSocket(canvas, auth, "/ws/video");
    const fallen = await control.next((message) => message.json?.t === "state" && message.json.transport !== "scrcpy",
      10_000);
    assert.match(canvas.output(), /Stream size: 2048 on the long side/, "scrcpy did not choose its own size first");
    const body = await status(canvas);
    assert.deepEqual([body.scrcpy.max_size, body.scrcpy.max_size_source, body.scrcpy.encoder, body.scrcpy.encoder_name],
      [null, null, null, null], "the status names a scrcpy size that is not streaming");
    if (withFfmpeg) {
      assert.equal(fallen.json.transport, "screenrecord");
      assert.deepEqual([fallen.json.max_size, fallen.json.encoder], [1280, null]);
      assert.deepEqual([body.transport, body.screenrecord_max_size], ["screenrecord", 1280]);
    } else {
      assert.equal(fallen.json.transport, "screencap");
      assert.deepEqual([fallen.json.max_size, fallen.json.encoder], [null, null]);
      assert.deepEqual([body.transport, body.screenrecord_max_size], ["screencap", null]);
    }
    await stopCanvas(canvas);
  }
});

test("encoders: while scrcpy streams, /status names no screenrecord size", async (t) => {
  const { canvas } = await sizedCanvas(t, { env: { FAKE_ENCODERS: SOFTWARE_ENCODERS } });
  const body = await status(canvas);
  assert.deepEqual([body.transport, body.scrcpy.max_size, body.screenrecord_max_size], ["scrcpy", 2048, null]);
  await stopCanvas(canvas);
});

waitingTest("encoders: a probe that never answers holds the stream back no longer than its bound, once", async (t) => {
  const started = Date.now();
  const { canvas, viewer, device, world, options } = await sizedCanvas(t, { env: { FAKE_ENCODERS: "hang" } });
  const waited = Date.now() - started;
  assert.equal(options.max_size, "1280");
  assert.ok(waited >= ENCODER_PROBE_TIMEOUT_MS - 200, `the stream started after ${waited} ms, before the probe bound`);
  assert.ok(waited < ENCODER_PROBE_TIMEOUT_MS + 5000, `the stream started only after ${waited} ms`);
  // A restart reuses the answer: no second probe, no second wait.
  device.sendConfig();
  device.sendPacket({ key: true });
  await waitFor(() => viewer.packets().length === 1, 5000, "the first packet");
  device.crash();
  await waitFor(() => device.spawns.length === 2 && device.connected, 8000, "the restart");
  const calls = await adbCalls(world);
  assert.equal(calls.filter(isEncoderProbe).length, 1);
  assert.equal(calls.filter(isServerStart).length, 2);
  assert.equal((await status(canvas)).scrcpy.max_size, 1280);
  await stopCanvas(canvas);
});

test("fanout: one device server feeds every client, late joiners start at a key frame", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t);
  const a = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  device.sendConfig();
  const sent = [device.sendPacket({ key: true, size: 1000 })];
  for (let i = 0; i < 5; i += 1) sent.push(device.sendPacket({ size: 500 + i }));
  await waitFor(() => a.packets().length === 6, 5000, "client A packets");
  const firstVideo = a.video();
  assert.deepEqual(firstVideo[0], { kind: "session", width: 570, height: 1280 });
  assert.deepEqual(firstVideo[1].kind, "config");
  assert.ok(firstVideo[1].data.equals(CONFIG));
  // CANVAS-001: payload bytes reach the browser unchanged.
  a.packets().forEach((packet, index) => assert.ok(packet.data.equals(sent[index]), `packet ${index}`));
  assert.equal(a.packets()[0].keyFrame, true);

  const late = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => late.packets().length === 6, 5000, "late client replay");
  const lateVideo = late.video();
  assert.equal(lateVideo[0].kind, "session");
  assert.equal(lateVideo[1].kind, "config");
  assert.equal(late.packets()[0].keyFrame, true);
  assert.deepEqual(late.packets().map((packet) => packet.seq), [0, 1, 2, 3, 4, 5]);

  // A client that stops reading: its packets are dropped until a key frame, others unaffected.
  const stalled = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => stalled.packets().length === 6, 5000, "stalled client replay");
  stalled.socket.pause();
  const resetsBefore = device.messages.filter((message) => message.type === "reset-video").length;
  const burstStart = device.seq;
  // Larger than the 4 MiB backlog plus what the kernel socket buffers can hold.
  const burst = 300;
  for (let i = 0; i < burst; i += 1) {
    device.sendPacket({ size: 40 * 1024 });
    if (i % 10 === 9) await sleep(5);
  }
  const lastSeq = () => device.seq - 1;
  await waitFor(() => a.packets().at(-1)?.seq === lastSeq(), 10_000, "client A to receive the burst");
  await waitFor(() => late.packets().at(-1)?.seq === lastSeq(), 10_000, "late client to receive the burst");
  const resets = device.messages.filter((message) => message.type === "reset-video").length;
  assert.ok(resets > resetsBefore, "no RESET_VIDEO was sent for the stalled client");
  await sleep(3000);
  stalled.socket.resume();
  // Fresh packets after the stall let the Canvas see the drained backlog and ask for a key frame.
  for (let i = 0; i < 40; i += 1) {
    device.sendPacket({ size: 200 });
    await sleep(30);
  }
  await waitFor(() => stalled.packets().at(-1)?.seq === lastSeq(), 10_000, "stalled client to recover");

  for (const client of [a, late]) {
    const seqs = client.packets().map((packet) => packet.seq);
    assert.deepEqual(seqs, Array.from({ length: seqs.length }, (_, index) => index), "a gap for a live client");
  }
  const stalledPackets = stalled.packets();
  const gapAt = stalledPackets.findIndex((packet, index) => index && packet.seq !== stalledPackets[index - 1].seq + 1);
  assert.ok(gapAt > 0, "the stalled client received every packet");
  assert.equal(stalledPackets[gapAt].keyFrame, true, "the stalled client resumed without a key frame");
  t.diagnostic(`stalled client: ${stalledPackets.length} packets, gap after seq ${stalledPackets[gapAt - 1].seq}`);
  const burstBytes = stalledPackets.filter((packet) => packet.seq >= burstStart && packet.seq < burstStart + burst)
    .reduce((sum, packet) => sum + packet.data.length, 0);
  assert.ok(burstBytes < burst * 40 * 1024, "nothing of the burst was dropped");

  const spawns = (await adbCalls(world)).filter(isServerStart);
  assert.equal(spawns.length, 1, "more than one scrcpy-server was started");
  assert.equal(device.spawns.length, 1);
  assert.match(spawns[0][3], /^CLASSPATH=\/data\/local\/tmp\/autonom-scrcpy-4\.1\.jar app_process \/ com\.genymobile\.scrcpy\.Server 4\.1 scid=[0-9a-f]{8} /);
  for (const option of ["tunnel_forward=true", "audio=false", "control=true", "cleanup=true", "max_size=1280",
    "video_bit_rate=12000000", "max_fps=60", "video_codec=h264", "clipboard_autosync=false"]) {
    assert.ok(spawns[0][3].split(" ").includes(option), option);
  }
  const statusBody = await status(canvas);
  assert.equal(statusBody.scrcpy.video_clients, 3);
  assert.equal(statusBody.scrcpy.session_state, "streaming");
  assert.ok(statusBody.scrcpy.packets >= 6 + burst + 40);
  assert.ok(!(await adbCalls(world)).some((args) => args.includes("screencap")), "multipart capture ran");

  // Clean stop: sockets closed, adb shell ended, forward removed, server swept.
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  await waitFor(() => device.running === null, 3000, "the device server to end");
  const calls = await adbCalls(world);
  const forward = calls.find((args) => args[2] === "forward" && args[3] === "tcp:0");
  const scid = forward[4].replace("localabstract:scrcpy_", "");
  assert.ok(calls.some((args) => args[2] === "forward" && args[3] === "--remove" &&
    args[4] === `tcp:${world.env.FAKE_SCRCPY_PORT}`), "the adb forward was not removed");
  assert.ok(calls.some((args) => args[2] === "shell" && args[3] === `pkill -f 'scid=[${scid[0]}]${scid.slice(1)}'`));
});

waitingTest("fanout: the session restarts after the device server dies and stops when idle", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t);
  const viewer = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  device.sendConfig();
  device.sendPacket({ key: true });
  await waitFor(() => viewer.packets().length === 1, 5000, "the first packet");
  const started = Date.now();
  device.crash();
  await viewer.next((message) => message.json?.session === "restarting");
  await waitFor(() => device.spawns.length === 2 && device.connected, 8000, "the restart");
  assert.ok(Date.now() - started >= 900, "restarted without the 1 s backoff");
  device.sendConfig();
  device.sendPacket({ key: true });
  await waitFor(() => viewer.packets().length === 2, 5000, "video after the restart");
  await viewer.next((message) => message.json?.session === "streaming" &&
    viewer.messages.indexOf(message) > viewer.messages.findIndex((item) => item.json?.session === "restarting"));
  assert.equal((await status(canvas)).scrcpy.restarts, 1);
  const removed = (await adbCalls(world)).filter((args) => args[2] === "forward" && args[3] === "--remove");
  assert.equal(removed.length, 1, "the dead server's forward was not removed before the restart");

  // The last client leaves: the server keeps running for 15 s, then stops.
  viewer.close();
  await viewer.closed;
  await sleep(13_000);
  assert.ok(device.running, "the server stopped before 15 s without clients");
  await waitFor(() => device.running === null, 5000, "the idle stop");
  await waitFor(async () => (await adbCalls(world))
    .filter((args) => args[2] === "forward" && args[3] === "--remove").length === 2, 3000, "the idle forward removal");
  const idle = await status(canvas);
  assert.equal(idle.scrcpy.session_state, "idle");
  assert.equal(device.spawns.length, 2);
  await stopCanvas(canvas);
});

test("fanout: a Canvas stopped while the server starts leaves no forward or server", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t, { device: { listenDelayMs: 60_000 } });
  const control = await pageSocket(canvas, auth, "/ws/control");
  await control.next((message) => message.json?.session === "starting");
  await waitFor(() => device.spawns.length === 1, 8000, "the server spawn");
  const launcherClosed = new Promise((resolvePromise) => device.once("launcher-closed", resolvePromise));
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  await launcherClosed;
  const calls = await adbCalls(world);
  const forwards = calls.filter((args) => args[2] === "forward" && args[3] === "tcp:0").length;
  const removed = calls.filter((args) => args[2] === "forward" && args[3] === "--remove").length;
  assert.equal(forwards, 1);
  assert.equal(removed, 1, "a forward outlived the Canvas");
  assert.ok(calls.some((args) => args[2] === "shell" && args[3]?.startsWith("pkill -f 'scid=")),
    "the server that never connected was not swept");
});

test("fanout: a Canvas stopped while adb creates the forward still removes it", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t, { env: { FAKE_FORWARD_DELAY_MS: "1200" } });
  await pageSocket(canvas, auth, "/ws/control");
  await waitFor(async () => (await adbCalls(world)).some((args) => args[2] === "forward" && args[3] === "tcp:0"),
    8000, "adb forward to start");
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  const calls = await adbCalls(world);
  assert.ok(calls.some((args) => args[2] === "forward" && args[3] === "--remove" &&
    args[4] === `tcp:${world.env.FAKE_SCRCPY_PORT}`), "the forward created during the stop outlived the Canvas");
  assert.ok(!calls.some(isServerStart), "a server was started after the stop");
  assert.equal(device.spawns.length, 0);
});

test("fanout: a stopping Canvas refuses new clients and starts no second server", async (t) => {
  // The forward removal is slow, so the stop takes a while and the page reconnects meanwhile.
  const { world, canvas, auth, device } = await streamingCanvas(t, { env: { FAKE_REMOVE_DELAY_MS: "1500" } });
  const viewer = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  canvas.child.kill("SIGTERM");
  assert.equal(await viewer.closed, 1001);
  const again = await openSocket(canvas.port, `/ws/video?token=${TOKEN}`);
  assert.equal(again.status, 503);
  const page = await openSocket(canvas.port, `/ws/control?csrf=${encodeURIComponent(auth.csrf)}`, {
    headers: { Cookie: auth.cookie, Origin: canvas.origin },
  });
  assert.equal(page.status, 503);
  assert.equal((await rawHttp(canvas.port, { path: "/status", headers: { Authorization: `Bearer ${TOKEN}` } })).status, 503);
  const exit = await canvas.exited;
  assert.equal(exit.code, 0);
  const calls = await adbCalls(world);
  const count = (match) => calls.filter(match).length;
  assert.equal(count((args) => args[2] === "forward" && args[3] === "tcp:0"), 1);
  assert.equal(count(isServerStart), 1);
  assert.equal(count((args) => args[2] === "forward" && args[3] === "--remove"), 1);
  assert.equal(device.spawns.length, 1);
});

test("fanout: the key-frame cache stays under 8 MiB and multipart pages share one capture", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t);
  const viewer = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  device.sendConfig();
  device.sendPacket({ key: true, size: 40 * 1024 });
  // A reading client never lags, so only the cache limit can ask for this key frame.
  for (let i = 0; i < 210; i += 1) {
    device.sendPacket({ size: 40 * 1024 });
    if (i % 10 === 9) await sleep(5);
  }
  await waitFor(() => device.messages.some((message) => message.type === "reset-video"), 5000,
    "a key-frame request for the full cache");
  const resetKey = await waitFor(() => viewer.packets().find((packet, index) => index && packet.keyFrame), 5000,
    "the key frame that restarts the cache");
  const late = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => late.packets().length, 5000, "the late client replay");
  assert.equal(late.packets()[0].seq, resetKey.seq);
  assert.equal(late.packets()[0].keyFrame, true);

  // Pages without WebCodecs fall back to multipart, which shares one capture loop.
  const screencaps = async () => (await adbCalls(world)).filter((args) => args.includes("screencap")).length;
  const capturesBefore = await screencaps();
  const readers = [];
  const controllers = [];
  for (let i = 0; i < 2; i += 1) {
    const controller = new AbortController();
    controllers.push(controller);
    const stream = await fetch(`${canvas.origin}/stream.mjpeg`, {
      headers: { Authorization: `Bearer ${TOKEN}` }, signal: controller.signal,
    });
    readers.push(stream.body.getReader());
  }
  const counts = [0, 0];
  await Promise.all(readers.map(async (reader, index) => {
    const deadline = Date.now() + 1500;
    while (Date.now() < deadline) {
      const { value, done } = await reader.read();
      if (done) break;
      counts[index] += (Buffer.from(value).toString("latin1").match(/--autonom-frame/g) ?? []).length;
    }
  }));
  for (const controller of controllers) controller.abort();
  assert.ok(counts[0] > 1 && counts[1] > 1, `multipart frames: ${counts}`);
  // Two loops would capture once per frame per client; one loop once per frame.
  const captures = await screencaps() - capturesBefore;
  t.diagnostic(`multipart: ${captures} captures for ${counts.join(" and ")} frames`);
  assert.ok(captures <= Math.max(...counts) + 2, `${captures} captures for ${counts} frames`);
  await stopCanvas(canvas);
});

test("fanout: a control client that stops reading its replies is not read past its bound, and is read again once it reads", async (t) => {
  const { canvas, auth, device, control } = await connectedControl(t);
  const stalled = await pageSocket(canvas, auth, "/ws/control");
  await stalled.next((message) => message.json?.t === "state");
  stalled.socket.pause();
  const gets = () => device.messages.filter((message) => message.type === "get-clipboard").length;
  // The largest clipboard answer scrcpy-server can send.
  const text = "x".repeat(256 * 1024 - 5);
  // 64 answers are 16 MiB: far past the bound plus what the kernel socket buffers hold.
  const maxRounds = 64;
  let rounds = 0;
  for (; rounds < maxRounds; rounds += 1) {
    const before = gets();
    stalled.send({ t: "clipboard-get" });
    // The previous answer was handled, so a request the Canvas reads starts a new device request.
    const asked = await waitFor(() => gets() > before, 1500, "the stalled client's request").then(() => true, () => false);
    if (!asked) break;
    // The reading client joins the same request, so its answer shows the Canvas handled it.
    const answered = control.reply((message) => message.t === "clipboard");
    const pong = control.reply((message) => message.t === "pong");
    control.send({ t: "clipboard-get" });
    control.send({ t: "ping", ts: rounds });
    await pong;
    device.sendClipboard(text);
    assert.equal((await answered).text.length, text.length);
  }
  t.diagnostic(`the stalled control client was not read after ${rounds} answers`);
  assert.ok(rounds < maxRounds, "a control client that never reads was read without end");
  const pong = control.reply((message) => message.t === "pong");
  control.send({ t: "ping", ts: 99 });
  assert.equal((await pong).ts, 99, "the reading client was affected");
  assert.equal((await status(canvas)).scrcpy.control_clients, 2, "the stalled client was closed");

  // Once it reads its replies, its waiting request is read too, and answered.
  const before = gets();
  const late = stalled.until((message) => message.t === "clipboard" && message.text === "late answer");
  stalled.socket.resume();
  await waitFor(() => gets() === before + 1, 5000, "the request the stalled client sent last");
  device.sendClipboard("late answer");
  await late;
  assert.equal(stalled.json("clipboard").length, rounds + 1, "a clipboard answer was lost");
  assert.equal(stalled.closeCode, null);
  assert.deepEqual(stalled.json("error"), []);
  await stopCanvas(canvas);
});

test("fanout: state messages to a stalled video client stay bounded and it resumes with the current state", async (t) => {
  const { canvas, auth, device, control } = await connectedControl(t, { device: { resetAnswers: false } });
  const reader = await pageSocket(canvas, auth, "/ws/video");
  const stalled = await pageSocket(canvas, auth, "/ws/video");
  device.sendConfig();
  device.sendPacket({ key: true, size: 1000 });
  await waitFor(() => stalled.packets().length === 1 && reader.packets().length === 1, 5000, "the first key frame");
  stalled.socket.pause();
  // Past the 4 MiB backlog plus the kernel socket buffers: the stalled client drops packets.
  for (let i = 0; i < 400; i += 1) {
    device.sendPacket({ size: 40 * 1024 });
    if (i % 10 === 9) await sleep(5);
  }
  const lastBurst = device.seq - 1;
  await waitFor(() => reader.packets().at(-1)?.seq === lastBurst, 10_000, "the reading client to get the burst");
  const statesBefore = stalled.json("state").length;
  const controlStates = control.json("state").length;
  // Every handoff change is pushed to every client; a control client could send them without end.
  const changes = 201;
  for (let i = 0; i < changes; i += 1) control.send({ t: "control", mode: i % 2 ? "resume" : "pause" });
  await waitFor(() => control.json("state").length >= controlStates + changes, 10_000, "the handoff states");
  stalled.socket.resume();
  const resumeSeq = device.seq;
  await waitFor(() => {
    device.sendPacket({ key: true, size: 200 });
    return stalled.packets().some((packet) => packet.seq >= resumeSeq);
  }, 10_000, "the stalled client to resume at a key frame");
  const states = stalled.json("state").slice(statesBefore);
  t.diagnostic(`the stalled video client got ${states.length} of ${changes} state messages`);
  assert.ok(states.length < 20, `${states.length} state messages were queued for a client that did not read`);
  assert.equal(states.at(-1)?.paused, true, "the resumed client did not get the current state");
  const resumed = stalled.messages.findIndex((message) => message.binary && message.binary[0] === 3 &&
    message.binary.readUInt32BE(15) >= resumeSeq);
  const current = stalled.messages.findLastIndex((message, index) => index < resumed && message.json?.t === "state");
  assert.ok(current > 0 && stalled.messages[current].json.paused === true, "the current state did not come before the key frame");
  await stopCanvas(canvas);
});

test("fanout: a stalled video client keeps only the latest session and config, gets them before its next key frame, and Canvas memory stays bounded", async (t) => {
  const { canvas, auth, device } = await streamingCanvas(t, { device: { resetAnswers: false }, nodeArgs: FLOOD_NODE_ARGS });
  const reader = await pageSocket(canvas, auth, "/ws/video");
  const stalled = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => device.connected, 8000, "the fake device connection");
  device.sendConfig();
  device.sendPacket({ key: true, size: 1000 });
  await waitFor(() => stalled.packets().length === 1 && reader.packets().length === 1, 5000, "the first key frame");
  stalled.socket.pause();
  const pausedAt = stalled.video().length;
  // Past the 4 MiB backlog plus the kernel socket buffers: the stalled client drops packets.
  for (let i = 0; i < 400; i += 1) {
    device.sendPacket({ size: 40 * 1024 });
    if (i % 10 === 9) await sleep(5);
  }
  const lastBurst = device.seq - 1;
  await waitFor(() => reader.packets().at(-1)?.seq === lastBurst, 10_000, "the reading client to get the burst");
  reader.keep = false;
  // Every device reset or rotation sends a config, and a rotation a session: as many as
  // a client could cause with a key frame request a second for days.
  const configs = 200_000;
  const latestConfig = Buffer.concat([CONFIG, Buffer.from("latest")]);
  const binaryBefore = reader.count(null);
  let headers = 0;
  const watched = await watchCanvas(canvas, async () => {
    for (let i = 0; i < configs; i += 1) {
      if (i % 1000 === 0) {
        device.sendSession(i % 2000 ? 1280 : 570, i % 2000 ? 570 : 1280);
        headers += 1;
      }
      device.sendConfig();
      headers += 1;
      if (i % 1000 === 999) await sleep(1);
    }
    device.sendSession(720, 1600);
    device.sendConfig(latestConfig);
    headers += 2;
    await waitFor(() => reader.count(null) - binaryBefore === headers, 30_000, "the reading client to get every header");
  });
  assertBounded(t, watched, `${configs} configs and ${configs / 1000} sessions`);
  stalled.socket.resume();
  const resumeSeq = device.seq;
  await waitFor(() => {
    device.sendPacket({ key: true, size: 200 });
    return stalled.packets().some((packet) => packet.seq >= resumeSeq);
  }, 10_000, "the stalled client to resume at a key frame");
  const after = stalled.video().slice(pausedAt);
  const resumed = after.findIndex((message) => message.kind === "packet" && message.seq >= resumeSeq);
  const sessions = after.filter((message) => message.kind === "session");
  const configsAfter = after.filter((message) => message.kind === "config");
  t.diagnostic(`the stalled client got ${sessions.length} sessions and ${configsAfter.length} configs of ${headers} headers`);
  assert.ok(sessions.length <= 2 && configsAfter.length <= 2, "headers were queued for a client that did not read");
  // It decodes from the latest session and config, sent in that order before the key frame.
  const lastSession = after.findLastIndex((message, index) => index < resumed && message.kind === "session");
  const lastConfig = after.findLastIndex((message, index) => index < resumed && message.kind === "config");
  assert.ok(lastSession >= 0 && lastSession < lastConfig, "the latest session and config did not come before the key frame");
  assert.deepEqual([after[lastSession].width, after[lastSession].height], [720, 1600]);
  assert.ok(after[lastConfig].data.equals(latestConfig), "the stalled client did not get the latest config");
  await stopCanvas(canvas);
});

// Input tests wait across clipboard holds and other multi-step device exchanges. Such a
// step takes about 0.4 s alone but ran past a 3 s limit on a busy CI runner, so allow 10 s.
const INPUT_WAIT_MS = 10_000;

test("input: touches map to the current video size and every interruption lifts the pointer", async (t) => {
  const { canvas, auth, device, control } = await connectedControl(t);
  const touch = (a, id, x, y, extra = {}) => control.send({ t: "touch", a, id, x, y, ...extra });
  touch("down", 1, 0.5, 0.5);
  touch("move", 1, 0.25, 0.75);
  await waitFor(() => device.touches().length === 2, INPUT_WAIT_MS, "down and move");
  let [down, move] = device.touches();
  assert.deepEqual([down.action, down.x, down.y, down.width, down.height, down.pressure],
    [0, 285, 640, 570, 1280, 0xffff]);
  assert.deepEqual([move.action, move.x, move.y], [2, 142, 960]);
  assert.equal(move.pointerId, down.pointerId);

  // Rotation with the finger down: scrcpy-server ignores events made for the old size, so
  // the Canvas lifts the finger itself with the new size and refuses its later moves.
  device.sendSession(1280, 570);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280);
  await waitFor(() => device.touches().length === 3, INPUT_WAIT_MS, "the finger lifted with the new size");
  const lifted = device.touches()[2];
  assert.deepEqual([lifted.action, lifted.pointerId, lifted.x, lifted.y, lifted.width, lifted.height],
    [1, down.pointerId, 320, 427, 1280, 570]);
  const refusedMove = control.reply((message) => message.t === "error");
  touch("move", 1, 0.5, 0.5);
  assert.match((await refusedMove).message, /Pointer 1 was lifted because the video size changed/);
  // Its up finds it lifted already: nothing reaches the device, nothing is refused.
  touch("up", 1, 0.5, 0.5);
  // The next gesture uses the new size, so scrcpy-server never ignores it.
  touch("down", 1, 0.5, 0.5);
  touch("up", 1, 0.5, 0.5);
  await waitFor(() => device.touches().length === 5, INPUT_WAIT_MS, "a gesture after the rotation");
  const [rotated, up] = device.touches().slice(3);
  assert.deepEqual([rotated.action, rotated.x, rotated.y, rotated.width, rotated.height], [0, 640, 285, 1280, 570]);
  assert.deepEqual([up.action, up.pressure], [1, 0]);
  assert.equal(control.json("error").length, 1);

  // Multitouch: two pointers, distinct device ids, both lifted.
  touch("down", 5, 0.2, 0.2);
  touch("down", 6, 0.8, 0.8);
  touch("up", 5, 0.2, 0.2);
  touch("cancel", 6, 0.8, 0.8);
  await waitFor(() => device.touches().length === 10, INPUT_WAIT_MS, "multitouch");
  const multi = device.touches().slice(5);
  assert.notEqual(multi[0].pointerId, multi[1].pointerId);
  assert.deepEqual(multi.map((message) => message.action), [0, 0, 1, 3, 1]);

  // Close mid-drag: the Canvas sends the up itself.
  const other = await pageSocket(canvas, auth, "/ws/control");
  await other.next((message) => message.json?.t === "state");
  other.send({ t: "touch", a: "down", id: 9, x: 0.1, y: 0.1 });
  await waitFor(() => device.touches().length === 11, INPUT_WAIT_MS, "second client down");
  other.close();
  await waitFor(() => device.touches().length === 12, INPUT_WAIT_MS, "up after disconnect");
  assert.equal(device.touches()[11].action, 1);
  assert.equal(device.touches()[11].pointerId, device.touches()[10].pointerId);

  // Pause mid-drag through the WebSocket.
  touch("down", 1, 0.5, 0.5);
  await waitFor(() => device.touches().length === 13, INPUT_WAIT_MS, "down before pause");
  control.send({ t: "control", mode: "pause" });
  await waitFor(() => device.touches().length === 14, INPUT_WAIT_MS, "up after pause");
  assert.equal(device.touches()[13].action, 1);
  const paused = control.reply((message) => message.t === "error");
  touch("move", 1, 0.5, 0.6);
  assert.match((await paused).message, /paused/);
  control.send({ t: "control", mode: "resume" });
  await control.next((message) => message.json?.t === "state" && message.json.paused === false);

  // An agent takes over through HTTP mid-drag: the human pointer is lifted and refused.
  touch("down", 1, 0.5, 0.5);
  await waitFor(() => device.touches().length === 15, INPUT_WAIT_MS, "down before takeover");
  const takeover = await fetch(`${canvas.origin}/control`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
    body: JSON.stringify({ mode: "takeover" }),
  });
  assert.equal(takeover.status, 200);
  await waitFor(() => device.touches().length === 16, INPUT_WAIT_MS, "up after takeover");
  assert.equal(device.touches()[15].action, 1);
  await control.next((message) => message.json?.t === "state" && message.json.owner === "agent");
  const before = device.controlBytes;
  const refused = control.reply((message) => message.t === "error");
  touch("move", 1, 0.5, 0.7);
  assert.match((await refused).message, /owned by agent/);
  const refusedDown = control.reply((message) => message.t === "error");
  touch("down", 2, 0.5, 0.7);
  assert.equal((await refusedDown).for, "touch");
  await expectNoDeviceWrite(device, before);
  // The agent's own WebSocket input still works.
  const agent = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(agent.status, 101);
  agent.ws.send({ t: "system", op: "home" });
  await waitFor(() => device.messages.filter((message) => message.keycode === 3).length === 2, INPUT_WAIT_MS,
    "agent input");
  // Wait for the state this release produces: the page's earlier states say "shared" too,
  // and the agent's release and the page's keys travel on different connections.
  const released = control.reply((message) => message.t === "state" && message.owner === "shared", INPUT_WAIT_MS);
  agent.ws.send({ t: "control", mode: "release" });
  await released;

  // Keys, wheel, text, paste, clipboard and system actions.
  const count = device.messages.length;
  control.send({ t: "key", a: "down", code: 59, meta: 0x41 });
  control.send({ t: "key", a: "down", code: 29, meta: 0x41 });
  control.send({ t: "key", a: "up", code: 29, meta: 0x41 });
  control.send({ t: "key", a: "up", code: 59, meta: 0 });
  control.send({ t: "scroll", x: 0.5, y: 0.5, dx: -1, dy: 2 });
  control.send({ t: "text", text: "wifi" });
  control.send({ t: "text", text: "Café ✓ naïve" });
  control.send({ t: "paste", text: "π ≈ 3.14 ✓" });
  control.send({ t: "system", op: "back" });
  control.send({ t: "system", op: "notifications" });
  control.send({ t: "system", op: "rotate" });
  control.send({ t: "clipboard-get" });
  await waitFor(() => device.messages.length === count + 13, INPUT_WAIT_MS, "control messages");
  const sent = device.messages.slice(count);
  assert.deepEqual(sent.slice(0, 4).map((message) => [message.action, message.keycode, message.meta]),
    [[0, 59, 0x41], [0, 29, 0x41], [1, 29, 0x41], [1, 59, 0]]);
  assert.equal(sent[4].type, "scroll");
  assert.deepEqual([sent[4].x, sent[4].y, sent[4].width, sent[4].height], [640, 285, 1280, 570]);
  assert.ok(sent[4].hScroll < 0 && sent[4].vScroll > 0, "the wheel lost an axis");
  assert.deepEqual(sent[5], { type: "text", text: "wifi" });
  assert.deepEqual([sent[6].type, sent[6].paste, sent[6].text], ["set-clipboard", true, "Café ✓ naïve"]);
  assert.deepEqual([sent[7].type, sent[7].paste, sent[7].text], ["set-clipboard", true, "π ≈ 3.14 ✓"]);
  assert.deepEqual(sent.slice(8, 10).map((message) => [message.type, message.action]),
    [["back-or-screen-on", 0], ["back-or-screen-on", 1]]);
  assert.deepEqual(sent.slice(10).map((message) => message.type), ["notifications", "rotate", "get-clipboard"]);
  const clipboard = control.reply((message) => message.t === "clipboard");
  device.sendClipboard("from the device");
  assert.equal((await clipboard).text, "from the device");
  await stopCanvas(canvas);
});

test("input: clipboard requests share one device answer and an empty clipboard answers with no text", async (t) => {
  const { canvas, device, control } = await connectedControl(t);
  const opened = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(opened.status, 101);
  const agent = opened.ws;
  await agent.next((message) => message.json?.t === "state");
  const gets = () => device.messages.filter((message) => message.type === "get-clipboard").length;
  // Messages on one connection are handled in order, so a pong means the request before it was.
  const handled = async (ws) => {
    const pong = ws.reply((message) => message.t === "pong");
    ws.send({ t: "ping", ts: 1 });
    await pong;
  };

  // scrcpy-server sends nothing for an empty clipboard: the page still gets an answer.
  const started = Date.now();
  const empty = control.reply((message) => message.t === "clipboard", 6000);
  control.send({ t: "clipboard-get" });
  assert.equal((await empty).text, null);
  assert.ok(Date.now() - started >= 1500, "the empty answer came before the device had time to answer");
  assert.equal(gets(), 1);

  // The next device answer belongs to the client that asked next, not to the earlier one.
  const forAgent = agent.reply((message) => message.t === "clipboard");
  agent.send({ t: "clipboard-get" });
  await waitFor(() => gets() === 2, INPUT_WAIT_MS, "the agent request");
  device.sendClipboard("answer-for-agent");
  assert.equal((await forAgent).text, "answer-for-agent");
  await sleep(100);
  assert.equal(control.json("clipboard").length, 1, "the page got the agent's answer");

  // Two clients asking at once share one device request and its answer.
  const a = control.reply((message) => message.t === "clipboard");
  const b = agent.reply((message) => message.t === "clipboard");
  control.send({ t: "clipboard-get" });
  agent.send({ t: "clipboard-get" });
  await Promise.all([handled(control), handled(agent)]);
  assert.equal(gets(), 3);
  device.sendClipboard("shared text");
  assert.deepEqual([(await a).text, (await b).text], ["shared text", "shared text"]);

  // Repeated requests while one is pending neither pile up nor get refused.
  const errors = control.json("error").length;
  const once = control.reply((message) => message.t === "clipboard" || message.t === "error", 6000);
  for (let i = 0; i < 20; i += 1) control.send({ t: "clipboard-get" });
  await handled(control);
  assert.equal(gets(), 4);
  device.sendClipboard("once");
  assert.deepEqual(await once, { t: "clipboard", text: "once" });
  await sleep(100);
  assert.equal(control.json("error").length, errors);
  assert.equal(control.json("clipboard").length, 3);
  agent.close();
  await stopCanvas(canvas);
});

test("input: a device that stops reading stops the reading of input, in order, and a takeover still lifts held input", async (t) => {
  const { canvas, device, control } = await connectedControl(t);
  let pings = 0;
  const handled = (timeoutMs = 3000) => {
    const ts = ++pings;
    const pong = control.reply((message) => message.t === "pong" && message.ts === ts, timeoutMs);
    control.send({ t: "ping", ts });
    return pong;
  };
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.messages.length === 2, INPUT_WAIT_MS, "the held pointer and key");
  // A paused emulator or a stuck Controller: the device control socket is not read.
  device.running.control.pause();
  // Rounds of 200 texts of 300 bytes, the longest INJECT_TEXT: 60 KB of device input each,
  // and 400 rounds are 24 MB, far past the device bound plus what the kernel socket buffers
  // hold. (A paste would wait for the clipboard ack the paused device never sends.)
  const text = "p".repeat(300);
  const perRound = 200;
  const maxRounds = 400;
  let answered = 0;
  for (; answered < maxRounds; answered += 1) {
    for (let i = 0; i < perRound; i += 1) control.send({ t: "text", text });
    if (!(await handled(1000).then(() => true, () => false))) break;
  }
  t.diagnostic(`the Canvas stopped reading input after ${answered + 1} rounds of ${perRound * text.length} bytes`);
  assert.ok(answered < maxRounds, `all ${maxRounds} rounds were queued for a device that does not read`);
  assert.deepEqual(control.json("error"), [], "input was refused instead of waiting");

  // An agent takes over through HTTP: the human's pointer and key are lifted even now,
  // behind everything the device has not read yet. It hands control back before the
  // device reads again, so the texts the Canvas has not read yet are not refused.
  const handoff = async (mode) => {
    const response = await fetch(`${canvas.origin}/control`, {
      method: "POST",
      headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
      body: JSON.stringify({ mode }),
    });
    assert.equal(response.status, 200);
  };
  await handoff("takeover");
  await handoff("release");
  device.running.control.resume();
  await waitFor(() => device.messages.some((message) => message.type === "key" && message.action === 1),
    10_000, "the lifted key on the device");
  // The ping that was not read is read once the device has caught up.
  await handled(10_000);
  // Every text went out; nothing was lost or refused.
  await waitFor(() => device.count("text") === (answered + 1) * perRound, INPUT_WAIT_MS, "every text on the device");
  assert.deepEqual(device.messages.map((message) => message.type).filter((type) => type !== "text"),
    ["touch", "key", "touch", "key"]);
  assert.deepEqual(device.touches().map((message) => message.action), [0, 1]);
  assert.equal(device.touches()[1].pointerId, device.touches()[0].pointerId);
  assert.deepEqual(device.messages.filter((message) => message.type === "key")
    .map((message) => [message.action, message.keycode]), [[0, 59], [1, 59]]);
  assert.deepEqual(control.json("error"), []);

  // Input goes through again.
  control.send({ t: "touch", a: "down", id: 3, x: 0.1, y: 0.1 });
  control.send({ t: "touch", a: "up", id: 3, x: 0.1, y: 0.1 });
  await handled();
  await waitFor(() => device.touches().length === 4, INPUT_WAIT_MS, "input after the device read again");
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

// The Canvas sends an up again at a size change when it was written this recently (RECENT_UP_MS).
const RECENT_UP_MS = 1000;

/**
 * An agent takes control through HTTP, which lifts the human's held input at once. A new
 * connection each time: fetch() reusing a pooled one sometimes answered seconds late.
 */
async function agentTakeover(canvas) {
  const response = await rawHttp(canvas.port, {
    method: "POST", path: "/control", body: JSON.stringify({ mode: "takeover" }),
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
  });
  assert.equal(response.status, 200);
}

/** Several control messages in one socket write, so the Canvas reads them together. */
function sendBurst(ws, messages) {
  ws.socket.write(Buffer.concat(messages.map((message) => clientFrame(1, Buffer.from(JSON.stringify(message), "utf8")))));
}

/** The device's SET_CLIPBOARD messages. */
function clipboardWrites(device) {
  return device.messages.filter((message) => message.type === "set-clipboard");
}

/** Device control messages as short labels, in the order the device read them. */
function deviceLabels(messages) {
  return messages.map((message) => {
    if (message.type === "key") return `key ${message.action} ${message.keycode}`;
    if (message.type === "text") return `text ${message.text}`;
    if (message.type === "set-clipboard") return `paste ${message.text}`;
    return message.type;
  });
}

test("input: nothing follows a clipboard paste to the device until its ack and a settle delay, and input held meanwhile from every connection keeps its arrival order", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  const opened = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(opened.status, 101, opened.body);
  const agent = opened.ws;
  await agent.next((message) => message.json?.t === "state");
  control.send({ t: "paste", text: "B2ü" });
  await waitFor(() => device.count("set-clipboard") === 1, INPUT_WAIT_MS, "the paste");
  const [paste] = device.messages;
  assert.equal(paste.paste, true);
  assert.ok(paste.sequence > 0n, "the paste asked for no acknowledgement");
  const pasted = device.controlBytes;
  control.send({ t: "key", a: "down", code: 54 });
  control.send({ t: "key", a: "up", code: 54 });
  await sleep(80);
  agent.send({ t: "system", op: "home" });
  await sleep(80);
  control.send({ t: "text", text: "y" });
  await expectNoDeviceWrite(device, pasted, 150);
  // An ack for another sequence releases nothing.
  device.ack(paste.sequence + 1n);
  await expectNoDeviceWrite(device, pasted, 150);
  const ackedAt = Date.now();
  device.ack(paste.sequence);
  await waitFor(() => device.messages.length === 6, INPUT_WAIT_MS, "the held input");
  const settled = device.arrivals[1] - ackedAt;
  // CLIPBOARD_SETTLE_MS is 200; a few ms less only for timer granularity.
  assert.ok(settled >= 195, `input followed the ack after ${settled} ms`);
  assert.deepEqual(deviceLabels(device.messages),
    ["paste B2ü", "key 0 54", "key 1 54", "key 0 3", "key 1 3", "text y"]);
  assert.deepEqual(control.json("error"), []);
  assert.deepEqual(agent.json("error"), []);
  agent.close();
  await stopCanvas(canvas);
});

test("input: a burst of text and pastes reaches a device that reads its clipboard late in the order it was sent", async (t) => {
  const { canvas, device, control } = await connectedControl(t);
  // Send a burst at once; the device's text field once everything has reached it.
  const burst = async (messages) => {
    device.field = "";
    const before = device.messages.length;
    for (const message of messages) control.send(message);
    await waitFor(() => device.messages.length === before + messages.length, INPUT_WAIT_MS, "the burst on the device");
    await sleep(100);
    return device.field;
  };
  // Measured on an emulator without the ack wait: "xé2yé2z", once "xyzé2é2".
  assert.equal(await burst([
    { t: "text", text: "x" }, { t: "text", text: "é1" }, { t: "text", text: "y" },
    { t: "paste", text: "é2" }, { t: "key", a: "down", code: 54 }, { t: "key", a: "up", code: 54 },
  ]), "xé1yé2z");
  // Measured without the ack wait: "B2üB2ü".
  assert.equal(await burst([{ t: "text", text: "A1é" }, { t: "paste", text: "B2ü" }]), "A1éB2ü");
  assert.deepEqual(deviceLabels(device.messages),
    ["text x", "paste é1", "text y", "paste é2", "key 0 54", "key 1 54", "paste A1é", "paste B2ü"]);
  const sequences = device.messages.filter((message) => message.type === "set-clipboard")
    .map((message) => message.sequence);
  assert.equal(sequences.length, 4);
  sequences.forEach((sequence, index) => assert.ok(sequence > (sequences[index - 1] ?? 0n),
    `clipboard sequence ${sequence} after ${sequences[index - 1]}`));
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("input: a key between two clipboard texts keeps two clipboard pastes, in order, with the key between them", async (t) => {
  const { canvas, device, control } = await connectedControl(t);
  sendBurst(control, [{ t: "text", text: "é1" }, { t: "key", a: "down", code: 54 }, { t: "key", a: "up", code: 54 },
    { t: "text", text: "é2" }]);
  await waitFor(() => device.messages.length === 4, INPUT_WAIT_MS, "the burst on the device");
  await sleep(300);
  assert.deepEqual(deviceLabels(device.messages), ["paste é1", "key 0 54", "key 1 54", "paste é2"]);
  await stopCanvas(canvas);
});

test("input: text and pastes reach a device that reads its clipboard 120 ms after the paste key exactly", async (t) => {
  // The fake device's field applies input 120 ms after reading it and pastes whatever the
  // clipboard holds by then: slower than the old 50 ms settle, faster than 200 ms.
  const { canvas, device, control } = await connectedControl(t, { device: { inputLatencyMs: 120 } });
  let marker = 0;
  // Send a burst in one write, then wait until all of it is handled and the field settled.
  const typed = async (messages) => {
    device.field = "";
    const ts = --marker;
    const done = control.reply((message) => message.t === "pong" && message.ts === ts, 5000);
    sendBurst(control, [...messages, { t: "ping", ts }]);
    await done;
    await sleep(400);
    return device.field;
  };
  // The live failure: a Unicode text and a paste sent together, as two separate pastes.
  assert.equal(await typed([{ t: "text", text: "Café ✓ naïve" }, { t: "paste", text: " π ≈ 3.14 ✓" }]),
    "Café ✓ naïve π ≈ 3.14 ✓");
  assert.deepEqual(clipboardWrites(device).map((message) => message.text), ["Café ✓ naïve", " π ≈ 3.14 ✓"]);
  // Clipboard text with other input between: each paste is read before the next is set.
  assert.equal(await typed([{ t: "text", text: "x" }, { t: "text", text: "é1" }, { t: "text", text: "y" },
    { t: "paste", text: "é2" }, { t: "key", a: "down", code: 54 }, { t: "key", a: "up", code: 54 }]), "xé1yé2z");
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

waitingTest("input: a burst of 300 one-character Unicode texts gets 300 clipboard pastes in order and 300 journal records, none dropped", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  // More than the 256 journal records the Canvas lets wait: each text waits behind the
  // paste before it, so the burst is held as input, never as pending records.
  const count = 300;
  const letters = Array.from({ length: count }, (_, i) => String.fromCodePoint(0x4e00 + i));
  const started = Date.now();
  sendBurst(control, letters.map((text) => ({ t: "text", text })));
  await waitFor(() => clipboardWrites(device).length === count, 120_000, "every paste on the device");
  t.diagnostic(`${count} pastes took ${((Date.now() - started) / 1000).toFixed(1)} s`);
  assert.deepEqual(clipboardWrites(device).map((message) => message.text), letters);
  const records = await waitFor(async () => {
    const found = await journaled(world, "text");
    return found.length === count && found;
  }, 10_000, "one text record per message");
  assert.deepEqual(records.map(({ payload }) => payload.text), letters);
  assert.equal((await status(canvas)).scrcpy.journal_dropped, 0, "text records were dropped");
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("input: a paste the device never acknowledges holds later input for 1 s, then input goes on and /status says why", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "paste", text: "q" });
  control.send({ t: "text", text: "after" });
  await waitFor(() => device.messages.length === 2, INPUT_WAIT_MS, "the input after the paste");
  const waited = device.arrivals[1] - device.arrivals[0];
  assert.ok(waited >= 950 && waited < 3000, `input followed an unacknowledged paste after ${waited} ms`);
  assert.deepEqual(deviceLabels(device.messages), ["paste q", "text after"]);
  assert.match((await status(canvas)).last_error, /did not acknowledge/);
  // Nothing is held any more: input without the clipboard goes straight on.
  const sentAt = Date.now();
  control.send({ t: "text", text: "more" });
  await waitFor(() => device.messages.length === 3, INPUT_WAIT_MS, "input after the timeout");
  assert.ok(device.arrivals[2] - sentAt < 500, "input after the timeout was held");
  assert.deepEqual(control.json("error"), []);
  assert.equal((await stopCanvas(canvas)).code, 0);
});

test("input: a device server restart while input waits for a clipboard ack refuses that input at once and holds nothing after it", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "paste", text: "é" });
  await waitFor(() => device.count("set-clipboard") === 1, INPUT_WAIT_MS, "the paste");
  const pasted = device.controlBytes;
  control.send({ t: "key", a: "down", code: 29 });
  control.send({ t: "key", a: "up", code: 29 });
  control.send({ t: "text", text: "b" });
  await expectNoDeviceWrite(device, pasted, 150);
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  const restartedAt = Date.now();
  const streaming = control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  const refused = await waitFor(() => control.json("error").length >= 3 && control.json("error"), INPUT_WAIT_MS,
    "the held input refused");
  assert.ok(Date.now() - restartedAt < 500, "held input waited past the restart");
  assert.deepEqual(refused.map((error) => error.for), ["key", "key", "text"]);
  for (const error of refused) assert.match(error.message, /not ready/);
  await streaming;
  await waitFor(() => device.connected, INPUT_WAIT_MS, "the new device server");
  const before = device.messages.length;
  const sentAt = Date.now();
  control.send({ t: "text", text: "c" });
  await waitFor(() => device.messages.length === before + 1, INPUT_WAIT_MS, "input after the restart");
  assert.ok(device.arrivals[before] - sentAt < 500, "input after the restart was held");
  assert.deepEqual(deviceLabels(device.messages), ["paste é", "text c"]);
  assert.equal((await stopCanvas(canvas)).code, 0);
});

test("input: a takeover while input waits for a clipboard ack lifts the pointer after the paste and refuses the input held behind it", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "paste", text: "ü" });
  await waitFor(() => device.messages.length === 2, INPUT_WAIT_MS, "the pointer and the paste");
  const pasted = device.controlBytes;
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  control.send({ t: "text", text: "z" });
  await sleep(100);
  const takeover = await fetch(`${canvas.origin}/control`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
    body: JSON.stringify({ mode: "takeover" }),
  });
  assert.equal(takeover.status, 200);
  // The lifted pointer waits behind the paste like any other device input.
  await expectNoDeviceWrite(device, pasted, 150);
  const refused = waitFor(() => control.json("error").length === 2 && control.json("error"), INPUT_WAIT_MS,
    "the held input refused");
  device.ack(device.messages[1].sequence);
  await waitFor(() => device.messages.length === 3, INPUT_WAIT_MS, "the lifted pointer");
  assert.deepEqual(device.messages.map((message) => message.type === "touch" ? `touch ${message.action}`
    : message.type), ["touch 0", "set-clipboard", "touch 1"]);
  // Held input meets the handoff check when it is handled, after the takeover.
  const errors = await refused;
  assert.deepEqual(errors.map((error) => error.for), ["touch", "text"]);
  for (const error of errors) assert.match(error.message, /owned by agent/);
  await expectNoDeviceWrite(device, device.controlBytes, 150);
  const records = await waitFor(async () => {
    const found = await journaled(world);
    return found.length === 2 && found;
  }, INPUT_WAIT_MS, "the journal records");
  assert.deepEqual(recordSummaries(records), ["human paste", "human gesture"]);
  assert.deepEqual([records[1].payload.pointers, records[1].payload.moves], [1, 0]);
  await stopCanvas(canvas);
});

test("input: a rotation lifts a finger still down with the new size, sends an up made just before it again, and refuses the lifted finger's moves", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { device: { dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  await waitFor(() => device.touches().length === 2, INPUT_WAIT_MS, "the finger down");
  const [down] = device.touches();
  assert.deepEqual([...device.pointersDown], [down.pointerId]);
  // The device rotates with the finger down: events made for the old size would be ignored.
  device.sendSession(1280, 570);
  await waitFor(() => device.pointersDown.size === 0, INPUT_WAIT_MS, "the finger lifted on the device");
  const lifted = device.touches().at(-1);
  // Where the finger was, (0.5, 0.6), in the new size.
  assert.deepEqual([lifted.action, lifted.pointerId, lifted.x, lifted.y, lifted.width, lifted.height],
    [1, down.pointerId, 640, 342, 1280, 570]);
  const refused = control.reply((message) => message.t === "error");
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.7 });
  assert.match((await refused).message, /Pointer 1 was lifted because the video size changed; put it down again/);
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.7 });

  // The device rotates back while an up leaves the Canvas, made for the size it still knows:
  // the device ignores that up, and the Canvas sends it again once it hears of the new size.
  control.send({ t: "touch", a: "down", id: 2, x: 0.2, y: 0.2 });
  await waitFor(() => device.pointersDown.size === 1, INPUT_WAIT_MS, "the second finger down");
  device.resize(570, 1280);
  control.send({ t: "touch", a: "up", id: 2, x: 0.2, y: 0.2 });
  await waitFor(() => device.ignored.length === 1, INPUT_WAIT_MS, "the up made for the old size");
  assert.equal(device.pointersDown.size, 1);
  device.sendSession();
  await waitFor(() => device.pointersDown.size === 0, INPUT_WAIT_MS, "the up sent again with the new size");
  const resent = device.touches().at(-1);
  assert.deepEqual([resent.action, resent.x, resent.y, resent.width, resent.height], [1, 114, 256, 570, 1280]);

  const records = await waitFor(async () => {
    const found = await journaled(world, "gesture");
    return found.length === 2 && found;
  }, INPUT_WAIT_MS, "one record per gesture");
  assert.deepEqual(records.map(({ payload }) => [payload.pointers, payload.moves]), [[1, 1], [1, 0]]);
  assert.equal(control.json("error").length, 1);
  await stopCanvas(canvas);
});

test("input: an up written late, behind a paste, counts as recent at a rotation from when it was written", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false, dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => device.pointersDown.size === 1, INPUT_WAIT_MS, "the finger down");
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, INPUT_WAIT_MS, "the paste");
  // The device rotates; its server says so only later.
  device.resize(1280, 570);
  // An agent takes over: the finger is lifted, but its up waits behind the paste, which
  // the device never acknowledges, so for 1 s, and then goes out made for the old size.
  await agentTakeover(canvas);
  // The up was made before the takeover answered, and written no later than it arrived.
  const madeBy = Date.now();
  await waitFor(() => device.ignored.length === 1, INPUT_WAIT_MS, "the up, written after the paste for the old size");
  const writtenBy = device.ignoredAt[0];
  assert.equal(device.pointersDown.size, 1);
  // The new size comes more than RECENT_UP_MS after the up was made, so counting from then
  // would not send it again, and well under RECENT_UP_MS after it was written.
  await sleep(Math.max(0, madeBy + RECENT_UP_MS + 150 - Date.now()));
  const sessionAt = Date.now();
  assert.ok(sessionAt - madeBy > RECENT_UP_MS, `the size changed ${sessionAt - madeBy} ms after the up was made`);
  assert.ok(sessionAt - writtenBy < RECENT_UP_MS - 100,
    `the size changed ${sessionAt - writtenBy} ms after the up was written; the takeover took too long to time this`);
  device.sendSession();
  await waitFor(() => device.pointersDown.size === 0, INPUT_WAIT_MS, "the up sent again with the new size");
  const resent = device.touches().at(-1);
  assert.deepEqual([resent.action, resent.width, resent.height], [1, 1280, 570]);
  await stopCanvas(canvas);
});

test("input: an up written more than a second before the video size changes is not sent again", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => device.touches().length === 2 && device.pointersDown.size === 0, INPUT_WAIT_MS, "a tap");
  await sleep(RECENT_UP_MS + 300);
  device.sendSession(1280, 570);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280);
  await sleep(300);
  assert.equal(device.touches().length, 2, "an up written long before the size change was sent again");
  await stopCanvas(canvas);
});

test("input: an up still held behind a paste when the video size changes goes out again after it with the new size", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false, dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => device.pointersDown.size === 1, INPUT_WAIT_MS, "the finger down");
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, INPUT_WAIT_MS, "the paste");
  const [paste] = device.messages.filter((message) => message.type === "set-clipboard");
  await agentTakeover(canvas);
  // The device rotates, and says so, while the finger's up still waits behind the paste.
  device.sendSession(1280, 570);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280);
  assert.equal(device.touches().length, 1, "the up went out before the paste was acknowledged");
  device.ack(paste.sequence);
  await waitFor(() => device.pointersDown.size === 0, INPUT_WAIT_MS, "the up sent again with the new size");
  // The up made for the old size is ignored; the one sent again after it lifts the finger.
  assert.deepEqual(device.ignored.map((message) => [message.action, message.width, message.height]), [[1, 570, 1280]]);
  const lifted = device.touches().at(-1);
  assert.deepEqual([lifted.action, lifted.width, lifted.height], [1, 1280, 570]);
  await stopCanvas(canvas);
});

test("input: a finger and a key lifted behind a paste's ack are lifted through the restarted server when the device server dies before the ack", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.pointersDown.size === 1 && device.keysDown.size === 1, INPUT_WAIT_MS, "the finger and key down");
  const [down] = device.touches();
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, INPUT_WAIT_MS, "the paste");
  // The takeover lifts them, but their up and key-up wait behind the paste...
  const pasted = device.controlBytes;
  await agentTakeover(canvas);
  await expectNoDeviceWrite(device, pasted, 150);
  // ...when the device server dies, before any ack.
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.pointersDown.size === 0 && device.keysDown.size === 0, INPUT_WAIT_MS,
    "the finger and key lifted through the restarted server");
  const lifted = device.messages.filter((message) => message.action === 1);
  assert.ok(lifted.some((message) => message.type === "touch" && message.pointerId === down.pointerId));
  assert.ok(lifted.some((message) => message.type === "key" && message.keycode === 59));
  await stopCanvas(canvas);
});

test("input: an up and a key-up the device has not read when its server dies are lifted through the restarted server", async (t) => {
  const { canvas, device, control } = await connectedControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.pointersDown.size === 1 && device.keysDown.size === 1, INPUT_WAIT_MS, "the finger and key down");
  // A stuck Controller: the up and key-up are written but never read by this server.
  device.running.control.pause();
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "up", code: 59 });
  await sleep(150);
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.pointersDown.size === 0 && device.keysDown.size === 0, INPUT_WAIT_MS,
    "the finger and key lifted through the restarted server");
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("input: a finger and a key held when the device server dies are lifted through the restarted server once its video size is known", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.messages.length === 2, INPUT_WAIT_MS, "the held finger and key");
  const [down] = device.touches();
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  const before = device.messages.length;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.messages.length === before + 2, INPUT_WAIT_MS, "the lifts through the restarted server");
  const [up, keyUp] = device.messages.slice(before);
  assert.deepEqual([up.type, up.action, up.pointerId, up.x, up.y, up.width, up.height],
    ["touch", 1, down.pointerId, 285, 640, 570, 1280]);
  assert.deepEqual([keyUp.type, keyUp.action, keyUp.keycode], ["key", 1, 59]);
  const refused = control.reply((message) => message.t === "error");
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  assert.match((await refused).message, /Pointer 1 was lifted because the device server restarted/);
  // A new finger works through the new server.
  control.send({ t: "touch", a: "down", id: 1, x: 0.1, y: 0.1 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.1, y: 0.1 });
  await waitFor(() => device.messages.length === before + 4, INPUT_WAIT_MS, "a new gesture");
  await waitFor(async () => (await journaled(world)).length >= 3, INPUT_WAIT_MS, "the journal records");
  await sleep(300);
  // The interrupted gesture and key are journaled once, when the server died.
  assert.deepEqual(recordSummaries(await journaled(world)), ["human gesture", "human key 59", "human gesture"]);
  await stopCanvas(canvas);
});

const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);

/**
 * Just enough of a DOM for the page script, built from the served page: elements by id and
 * by the attribute selectors it queries, attributes, classes, focus, and events that bubble
 * through their parents to the document. Text is whatever the script sets. As in a browser,
 * an element that is disabled while it has the focus loses it to the body.
 */
function fakeDocument(html) {
  const listeners = new Map();
  const listen = (node, type, listener) => {
    if (!listeners.has(node)) listeners.set(node, new Map());
    const byType = listeners.get(node);
    byType.set(type, [...(byType.get(type) ?? []), listener]);
  };
  const dispatch = (target, type, init = {}) => {
    const event = { type, target, defaultPrevented: false, stopped: false, ...init,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
    for (let node = target; node && !event.stopped; node = node === document ? null : node.parent ?? document) {
      for (const listener of listeners.get(node)?.get(type) ?? []) listener.call(node, event);
    }
    return event;
  };
  const document = {
    activeElement: null,
    addEventListener: (type, listener) => listen(document, type, listener),
    dispatch: (type, init) => dispatch(document, type, init),
  };
  const elements = [];
  const make = (tag, attributes, parent) => {
    const names = new Set((attributes.class ?? "").split(/\s+/).filter(Boolean));
    const element = {
      tagName: tag.toUpperCase(), parent, attributes: new Map(Object.entries(attributes)), dataset: {},
      id: attributes.id ?? "", hidden: "hidden" in attributes,
      value: attributes.value ?? "", textContent: "", title: attributes.title ?? "",
      style: { setProperty(name, value) { this[name] = value; } },
      classList: {
        add: (name) => names.add(name), remove: (name) => names.delete(name), contains: (name) => names.has(name),
        toggle: (name, force = !names.has(name)) => (force ? names.add(name) : names.delete(name), force),
      },
      getContext: () => ({}),
      getAttribute: (name) => element.attributes.get(name) ?? null,
      setAttribute: (name, value) => element.attributes.set(name, String(value)),
      contains: (other) => {
        for (let node = other; node; node = node.parent) if (node === element) return true;
        return false;
      },
      focus: () => { document.activeElement = element; },
      addEventListener: (type, listener) => listen(element, type, listener),
      dispatch: (type, init) => dispatch(element, type, init),
      click: () => { if (!element.disabled) dispatch(element, "click", { detail: 0 }); },
    };
    let disabled = "disabled" in attributes;
    Object.defineProperty(element, "disabled", {
      enumerable: true,
      get: () => disabled,
      set: (value) => {
        disabled = Boolean(value);
        if (disabled && document.activeElement === element) document.activeElement = document.body ?? null;
      },
    });
    for (const [name, value] of Object.entries(attributes)) {
      if (name.startsWith("data-")) element.dataset[name.slice(5).replace(/-(\w)/g, (_, letter) => letter.toUpperCase())] = value;
    }
    elements.push(element);
    return element;
  };
  const markup = html.replace(/<script>[\s\S]*?<\/script>|<style>[\s\S]*?<\/style>/g, "");
  const open = [];
  for (const [, closing, tag, rest] of markup.matchAll(/<(\/?)([a-zA-Z][\w-]*)([^>]*)>/g)) {
    if (closing) {
      const index = open.findLastIndex((element) => element.tagName === tag.toUpperCase());
      if (index >= 0) open.length = index;
      continue;
    }
    const attributes = Object.fromEntries([...rest.matchAll(/([^\s="'/]+)(?:="([^"]*)")?/g)]
      .map(([, name, value = ""]) => [name, value]));
    const element = make(tag, attributes, open.at(-1) ?? null);
    if (!VOID_TAGS.has(tag.toLowerCase()) && !rest.trimEnd().endsWith("/")) open.push(element);
  }
  document.elements = elements;
  document.body = elements.find((element) => element.tagName === "BODY");
  document.getElementById = (id) => elements.find((element) => element.id === id) ?? null;
  document.querySelectorAll = (selector) => {
    const names = selector.split(",").map((part) => part.trim().match(/^\[([\w-]+)\]$/)?.[1]);
    if (names.some((name) => !name)) throw new Error(`the fake document cannot query ${selector}`);
    return elements.filter((element) => names.some((name) => element.attributes.has(name)));
  };
  return document;
}

/**
 * The Canvas page script of `html`, run in a node:vm sandbox over a fake document of that
 * page (its sign-in never answers), on the scrcpy transport with an open control socket.
 * `sent` collects the control messages it sends; `run` evaluates code inside the page.
 */
function runPage(html) {
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const document = fakeDocument(html);
  const sent = [];
  const sandbox = {
    location: { hash: "", pathname: "/", search: "", protocol: "http:", host: "127.0.0.1:1" },
    history: { replaceState() {} },
    document,
    fetch: () => new Promise(() => {}),
    URLSearchParams, TextEncoder, setTimeout, clearTimeout, setInterval, clearInterval,
    record: (text) => sent.push(JSON.parse(text)),
  };
  sandbox.window = sandbox;
  const page = createContext(sandbox);
  new Script(script).runInContext(page);
  const run = (code) => new Script(code).runInContext(page);
  run("view.transport=\"scrcpy\";controlSocket={readyState:1,send:record};");
  return { sandbox, sent, run, document };
}

/** The Android page as served (`html`), run by runPage. */
async function loadPage(t) {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--scrcpy-server", world.serverFile]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  await stopCanvas(canvas);
  return { ...runPage(html), html };
}

test("input: the page sends the characters of other keyboard layouts as text and keeps keycodes for US keys and shortcuts", async (t) => {
  const { sandbox, sent } = await loadPage(t);
  const press = (code, key, modifiers = {}) => {
    sent.length = 0;
    const event = { code, key, repeat: false, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
      ...modifiers, preventDefault() {} };
    sandbox.onKeyDown(event);
    sandbox.onKeyUp(event);
    return sent.map((message) => message.t === "text" ? `text ${message.text}`
      : `key ${message.a} ${message.code} ${message.meta}`);
  };
  const shift = { shiftKey: true };
  // US: keycodes, with the Shift meta state for shifted characters.
  assert.deepEqual(press("KeyA", "a"), ["key down 29 0", "key up 29 0"]);
  assert.deepEqual(press("Digit1", "!", shift), ["key down 8 65", "key up 8 65"]);
  assert.deepEqual(press("Space", " "), ["key down 62 0", "key up 62 0"]);
  assert.deepEqual(press("Enter", "Enter"), ["key down 66 0", "key up 66 0"]);
  assert.deepEqual(press("KeyC", "c", { ctrlKey: true }), ["key down 31 12288", "key up 31 12288"]);
  // AZERTY: A and Q swapped, digits shifted.
  assert.deepEqual(press("KeyQ", "a"), ["text a"]);
  assert.deepEqual(press("KeyA", "q"), ["text q"]);
  assert.deepEqual(press("Digit1", "&"), ["text &"]);
  assert.deepEqual(press("Digit1", "1", shift), ["text 1"]);
  assert.deepEqual(press("Semicolon", "m"), ["text m"]);
  // QWERTZ: Y and Z swapped, the rest of the letters as on US.
  assert.deepEqual(press("KeyZ", "y"), ["text y"]);
  assert.deepEqual(press("KeyY", "z"), ["text z"]);
  assert.deepEqual(press("KeyS", "s"), ["key down 47 0", "key up 47 0"]);
  assert.deepEqual(press("Digit2", "\"", shift), ["text \""]);
  // The AltGr layer (Ctrl+Alt, or the AltGraph modifier) and the macOS Option layer
  // (Alt alone) type their own characters, also on keys without a keycode.
  const altGr = { ctrlKey: true, altKey: true };
  assert.deepEqual(press("KeyL", "@", { altKey: true }), ["text @"], "German Mac Option+L");
  assert.deepEqual(press("KeyQ", "@", altGr), ["text @"], "Windows AltGr+Q");
  assert.deepEqual(press("KeyA", "\u0105", altGr), ["text \u0105"], "Polish AltGr+A");
  assert.deepEqual(press("KeyQ", "@", { getModifierState: (name) => name === "AltGraph" }), ["text @"]);
  const altGraph = { getModifierState: (name) => name === "AltGraph" };
  assert.deepEqual(press("KeyQ", "@", { ctrlKey: true, ...altGraph }), ["text @"], "Ctrl reported with AltGraph");
  // Ctrl+V and Cmd+V send nothing, so that the browser's paste event carries the clipboard;
  // an AltGr character on V is typed.
  assert.deepEqual(press("KeyV", "v", { ctrlKey: true }), [], "Ctrl+V");
  assert.deepEqual(press("KeyV", "v", { metaKey: true }), [], "Cmd+V");
  assert.deepEqual(press("KeyV", "\u201e", altGr), ["text \u201e"], "AltGr+V");
  assert.deepEqual(press("KeyV", "\u201e", { ctrlKey: true, ...altGraph }), ["text \u201e"], "Ctrl+V with AltGraph");
  assert.deepEqual(press("IntlBackslash", "\u2264", { altKey: true }), ["text \u2264"], "Mac Option+IntlBackslash");
  // Alt with the US character stays a keycode with the Alt meta state.
  assert.deepEqual(press("KeyA", "a", { altKey: true }), ["key down 29 18", "key up 29 18"]);
  // A dead key sends nothing; the composed character comes with the next key.
  assert.deepEqual(press("Quote", "Dead"), [], "Spanish dead acute");
  assert.deepEqual(press("KeyE", "\u00e9"), ["text \u00e9"]);
  assert.deepEqual(press("BracketLeft", "Dead"), [], "French dead circumflex");
  assert.deepEqual(press("KeyE", "\u00ea"), ["text \u00ea"]);
  assert.deepEqual(press("KeyQ", "Process", { isComposing: true }), [], "a key that feeds an input method");
  // Cyrillic (written as escapes): every letter is text, while a shortcut stays a keycode.
  const privet = [["KeyG", "\u043f"], ["KeyH", "\u0440"], ["KeyB", "\u0438"], ["KeyD", "\u0432"],
    ["KeyT", "\u0435"], ["KeyN", "\u0442"]];
  assert.deepEqual(privet.flatMap(([code, key]) => press(code, key)), privet.map(([, key]) => `text ${key}`));
  assert.deepEqual(press("KeyC", "\u0441", { ctrlKey: true }), ["key down 31 12288", "key up 31 12288"]);
});

test("input: the page refuses a paste or text box entry too large for one control message after JSON escaping, keeps the text and says why", async (t) => {
  const { sandbox, sent, run } = await loadPage(t);
  const paste = (text) => sandbox.onPaste({ clipboardData: { getData: () => text }, preventDefault() {} });
  // 50,000 bytes of text, but 66,000 as JSON: every quote becomes two bytes.
  const quoted = "a".repeat(34_000) + "\"".repeat(16_000);
  paste(quoted);
  assert.deepEqual(sent, [], "a paste past the control message limit was sent");
  assert.match(run("view.note"), /too large: 66\d{3} bytes as a control message after JSON escaping, at most 65536/);
  // A paste that fits still goes out.
  paste("a".repeat(50_000));
  assert.deepEqual(sent.map((message) => [message.t, message.text.length]), [["paste", 50_000]]);
  // The text box keeps an entry that does not fit, and sends one that does.
  sent.length = 0;
  run("view.note=\"\"");
  run(`textInput.value=${JSON.stringify(quoted)}`);
  await run("sendText()");
  assert.deepEqual(sent, [], "a text box entry past the control message limit was sent");
  assert.equal(run("textInput.value"), quoted, "the text box lost the entry it did not send");
  assert.match(run("view.note"), /the text is too large/);
  run("textInput.value=\"hello\"");
  await run("sendText()");
  assert.deepEqual(sent, [{ t: "text", text: "hello" }]);
  assert.equal(run("textInput.value"), "");
});

test("journal: one bridge record per completed action, never clipboard or sensitive text", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.1, y: 0.2 });
  for (let i = 1; i <= 40; i += 1) control.send({ t: "touch", a: "move", id: 1, x: 0.1, y: 0.2 + i / 100 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.1, y: 0.6 });
  for (let i = 0; i < 5; i += 1) control.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  control.send({ t: "key", a: "down", code: 66 });
  control.send({ t: "key", a: "down", code: 66, repeat: 1 });
  control.send({ t: "key", a: "up", code: 66 });
  control.send({ t: "text", text: "hello" });
  control.send({ t: "text", text: "hunter2-secret", sensitive: true });
  control.send({ t: "paste", text: "clipboard-secret-123" });
  control.send({ t: "system", op: "home" });
  control.send({ t: "system", op: "keyframe" });
  control.send({ t: "clipboard-get" });
  control.send({ t: "control", mode: "takeover" });
  control.send({ t: "control", mode: "release" });
  await waitFor(async () => (await bridgeCalls(world, "record")).length >= 9, 5000, "journal records");
  await sleep(600);
  const records = await bridgeCalls(world, "record");
  const kinds = records.map((record) => record.payload.kind);
  assert.deepEqual(kinds, ["gesture", "scroll", "key", "text", "text", "paste", "system", "control", "control"]);
  for (const record of records) {
    assert.equal(record.origin, "human");
    assert.equal(record.payload.transport, "scrcpy");
  }
  const [gesture, wheel, key, plain, sensitive, paste, system] = records.map((record) => record.payload);
  assert.equal(gesture.moves, 40);
  assert.equal(gesture.pointers, 1);
  assert.deepEqual(gesture.start, [108, 480]);
  assert.deepEqual(gesture.end, [108, 1440]);
  assert.ok(gesture.duration_ms >= 0);
  assert.deepEqual([wheel.events, wheel.dy], [5, -5]);
  assert.equal(key.key, 66);
  assert.deepEqual([plain.text, plain.text_len, plain.sensitive], ["hello", 5, false]);
  assert.equal(sensitive.text, undefined);
  assert.equal(sensitive.text_len, 14);
  assert.deepEqual(Object.keys(paste).sort(), ["kind", "text_len", "transport"]);
  assert.equal(system.op, "home");
  const everything = await readFile(world.bridgeLog, "utf8");
  assert.ok(!everything.includes("clipboard-secret-123"), "clipboard text reached the bridge");
  assert.ok(!everything.includes("hunter2-secret"), "sensitive text reached the bridge");
  // Streamed input never goes through the bridge actuators: only record and screen-size do.
  for (const call of await bridgeCalls(world)) assert.ok(["record", "screen-size"].includes(call.op), call.op);
  assert.equal(device.touches().length, 42);

  // A drag cut short by a disconnect is still one entry.
  control.send({ t: "touch", a: "down", id: 3, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 3, x: 0.5, y: 0.6 });
  await waitFor(() => device.touches().length === 44, 3000, "drag before disconnect");
  control.close();
  await waitFor(async () => (await bridgeCalls(world, "record")).length === 10, 5000, "the cut drag record");
  const last = (await bridgeCalls(world, "record")).at(-1).payload;
  assert.deepEqual([last.kind, last.moves], ["gesture", 1]);
  await stopCanvas(canvas);
});

/** Journal records as "<origin> <what>", in the order the bridge got them. */
function recordSummaries(records) {
  return records.map(({ origin, payload }) => `${origin} ${
    payload.kind === "key" ? `key ${payload.key}`
      : payload.kind === "control" ? `control ${payload.mode}` : payload.kind}`);
}

test("journal: a held key lifted on pause, takeover, disconnect or a device server restart gets one key record, as an interrupted gesture does", async (t) => {
  const { world, canvas, auth, device, control } = await connectedControl(t);
  const handoff = async (mode) => {
    const response = await fetch(`${canvas.origin}/control`, {
      method: "POST",
      headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
      body: JSON.stringify({ mode }),
    });
    assert.equal(response.status, 200);
  };
  const keys = (action) => device.messages.filter((message) => message.type === "key" && message.action === action)
    .map((message) => message.keycode);

  // Pause from the page while Shift is held and repeating: one record for the whole press.
  control.send({ t: "key", a: "down", code: 59, meta: 0x41 });
  control.send({ t: "key", a: "down", code: 59, meta: 0x41, repeat: 1 });
  await waitFor(() => keys(0).length === 2, 3000, "the held key");
  control.send({ t: "control", mode: "pause" });
  await waitFor(() => keys(1).length === 1, 3000, "the key lifted on pause");
  const resumed = control.reply((message) => message.t === "state" && message.paused === false);
  control.send({ t: "control", mode: "resume" });
  await resumed;

  // An agent takes over through HTTP while a finger and two keys are down.
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 29 });
  control.send({ t: "key", a: "down", code: 30 });
  await waitFor(() => keys(0).length === 4, 3000, "the second held keys");
  await handoff("takeover");
  await waitFor(() => keys(1).length === 3, 3000, "the keys lifted on takeover");
  await handoff("release");

  // The page takes over from an agent connection that holds a key: the record is the agent's.
  const opened = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(opened.status, 101, opened.body);
  const agent = opened.ws;
  await agent.next((message) => message.json?.t === "state" && message.json.owner === "shared");
  agent.send({ t: "key", a: "down", code: 33 });
  await waitFor(() => keys(0).length === 5, 3000, "the agent's held key");
  control.send({ t: "control", mode: "takeover" });
  await waitFor(() => keys(1).length === 4, 3000, "the agent's key lifted on takeover");
  const released = control.reply((message) => message.t === "state" && message.owner === "shared");
  control.send({ t: "control", mode: "release" });
  await released;

  // Another page holds a key and goes away.
  const other = await pageSocket(canvas, auth, "/ws/control");
  await other.next((message) => message.json?.t === "state");
  other.send({ t: "key", a: "down", code: 31 });
  await waitFor(() => keys(0).length === 6, 3000, "the other page's held key");
  other.close();
  await waitFor(() => keys(1).length === 5, 3000, "the key lifted on disconnect");

  // The device server dies with a key held: the key is gone with it, and still journaled.
  control.send({ t: "key", a: "down", code: 32 });
  await waitFor(() => keys(0).length === 7, 3000, "the key held before the restart");
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;

  const expected = [
    "human key 59", "human control pause", "human control resume",
    "human gesture", "human key 29", "human key 30",
    "agent key 33", "human control takeover", "human control release",
    "human key 31",
    "human key 32",
  ];
  const records = await waitFor(async () => {
    const found = await journaled(world);
    return found.length >= expected.length && found;
  }, 5000, "the journal records");
  await sleep(300);
  assert.equal((await journaled(world)).length, expected.length, "a lifted key got more than one record");
  assert.deepEqual(recordSummaries(records), expected);
  // A lifted key is recorded like a released one: its keycode, nothing else.
  for (const { payload } of records.filter((record) => record.payload.kind === "key")) {
    assert.deepEqual(Object.keys(payload).sort(), ["key", "kind", "transport"]);
    assert.equal(payload.transport, "scrcpy");
  }
  const gesture = records.find((record) => record.payload.kind === "gesture").payload;
  assert.deepEqual([gesture.pointers, gesture.moves], [1, 0]);
  // The device got an up for every key the Canvas lifted. Through the restarted server it
  // gets one for the key held when the server died, and again one for each key-up written
  // in the second before, which the dead server might not have read; that changes nothing.
  await waitFor(() => keys(1).slice(5).includes(32), 8000, "the key lifted through the restarted server");
  assert.deepEqual(keys(1).slice(0, 5), [59, 29, 30, 33, 31]);
  assert.ok(keys(1).slice(5).every((code) => [59, 29, 30, 33, 31, 32].includes(code)), `key-ups ${keys(1).slice(5)}`);
  // The finger lifted on the takeover, and maybe its up once more through the new server.
  const touches = device.touches();
  assert.deepEqual(touches.slice(0, 2).map((message) => message.action), [0, 1]);
  assert.ok(touches.slice(2).every((message) => message.action === 1 && message.pointerId === touches[0].pointerId));
  assert.deepEqual(control.json("error"), []);
  agent.close();
  await stopCanvas(canvas);
});

// Server sockets of the Canvas ignore pause(): a peer whose input keeps arriving although the
// Canvas stopped reading it, the case the held-input bound is there for.
const IGNORED_PAUSE = `data:text/javascript,${encodeURIComponent(`
import { Socket } from "node:net";
const pause = Socket.prototype.pause;
Socket.prototype.pause = function () { return this.server ? this : pause.call(this); };
`)}`;

// Reports on stderr each new longest held-input queue in the Canvas: the queue of a control
// connection is the only array of {data, isBinary, order} entries.
const HELD_PROBE = `data:text/javascript,${encodeURIComponent(`
const push = Array.prototype.push;
let longest = 0;
Array.prototype.push = function (...items) {
  const length = push.apply(this, items);
  const item = items[0];
  if (items.length === 1 && item && typeof item === "object" && "order" in item && "isBinary" in item &&
    length > longest) {
    longest = length;
    process.stderr.write("held-queue " + length + "\\n");
  }
  return length;
};
`)}`;

test("input: a connection that keeps one message held after every paste keeps a bounded held queue", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { nodeArgs: ["--import", HELD_PROBE] });
  const longest = () => Math.max(0, ...[...canvas.output().matchAll(/held-queue (\d+)/g)].map((match) => Number(match[1])));
  const cycles = 12;
  control.send({ t: "paste", text: "\u00e9" });
  for (let cycle = 1; cycle <= cycles; cycle += 1) {
    await waitFor(() => clipboardWrites(device).length === cycle, INPUT_WAIT_MS, `paste ${cycle}`);
    // While this paste waits: 600 pings, the next paste, and one message after it, which
    // stays held behind that paste when the rest has been handled.
    sendBurst(control, [...Array.from({ length: 600 }, (_, i) => ({ t: "ping", ts: cycle * 1000 + i })),
      { t: "paste", text: "\u00e9" }, { t: "ping", ts: -cycle }]);
  }
  await waitFor(() => clipboardWrites(device).length === cycles + 1, INPUT_WAIT_MS, "the last paste");
  await waitFor(() => control.json("pong").some((message) => message.ts === -cycles), INPUT_WAIT_MS, "the last held message");
  t.diagnostic(`the longest held queue had ${longest()} slots after ${cycles} pastes of 602 held messages each`);
  // Without compaction it grows by about 600 handled slots per paste.
  assert.ok(longest() > 0, "the probe saw no held queue");
  assert.ok(longest() < 2 * 602, `the held queue reached ${longest()} slots`);
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("journal: a connection refused for input past the held bound gets one key record per held key, as for its gesture", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { nodeArgs: ["--import", IGNORED_PAUSE] });
  const hold = `${world.bridgeLog}.hold`;
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  control.send({ t: "key", a: "down", code: 29, meta: 0x41 });
  await waitFor(() => device.messages.length === 3, 3000, "the held finger and keys");
  // 64 records wait for the bridge, so the Canvas stops reading this connection.
  await writeFile(hold, "");
  for (let i = 0; i < 64; i += 1) control.send({ t: "system", op: "back" });
  await waitFor(() => device.count("back-or-screen-on") === 128, 3000, "the actions up to the journal bound");
  // The input keeps coming anyway: past 1 MiB held the Canvas refuses the connection.
  const refused = control.reply((message) => message.t === "error", 10_000);
  const text = "p".repeat(60_000);
  for (let i = 0; i < 20; i += 1) control.send({ t: "paste", text });
  assert.match((await refused).message, /faster than the Canvas can apply/);
  assert.equal(await control.closed, 1013);
  await waitFor(() => device.messages.length === 3 + 128 + 3, 3000, "the finger and keys lifted");
  assert.deepEqual(device.messages.slice(-3).map((message) => [message.type, message.action, message.keycode]),
    [["touch", 1, undefined], ["key", 1, 59], ["key", 1, 29]]);
  assert.equal(device.count("set-clipboard"), 0, "held input reached the device after the refusal");

  await rm(hold);
  const expected = [...Array(64).fill("human system"), "human gesture", "human key 59", "human key 29"];
  const records = await waitFor(async () => {
    const found = await journaled(world);
    return found.length >= expected.length && found;
  }, 10_000, "the journal records");
  await sleep(300);
  assert.equal((await journaled(world)).length, expected.length, "a lifted key got more than one record");
  assert.deepEqual(recordSummaries(records), expected);
  for (const { payload } of records.filter((record) => record.payload.kind === "key")) {
    assert.deepEqual(Object.keys(payload).sort(), ["key", "kind", "transport"]);
  }
  await stopCanvas(canvas);
});

test("journal: a connection the bridge cannot keep up with is not read past 64 waiting records, nothing is refused, and every action is recorded in order", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  let pings = 0;
  const handled = (socket = control, timeoutMs = 3000) => {
    const ts = ++pings;
    const pong = socket.reply((message) => message.t === "pong" && message.ts === ts, timeoutMs);
    socket.send({ t: "ping", ts });
    return pong;
  };
  // A journal bridge that stops answering record, like the Python bridge on a slow disk.
  const hold = `${world.bridgeLog}.hold`;
  await writeFile(hold, "");
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  // Each action adds one journal record, also when it repeats the one before.
  const flood = 400;
  for (let i = 0; i < flood; i += 1) control.send({ t: "system", op: "back" });
  assert.equal(await handled(control, 1500).then(() => true, () => false), false,
    "the Canvas kept reading a connection whose journal records wait");
  // 16 records are with the bridge and 48 wait: 64 actions were applied.
  const applied = () => device.count("back-or-screen-on") / 2;
  await waitFor(() => applied() === 64, 3000, "the actions up to the journal bound");
  await sleep(200);
  assert.equal(applied(), 64, "the connection was read past its journal bound");
  assert.equal((await bridgeCalls(world, "record")).length, 16, "the journal sent more than 16 records unanswered");
  assert.deepEqual(control.json("error"), [], "input was refused instead of waiting");

  // The bound is per connection: another connection is still read.
  const opened = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(opened.status, 101, opened.body);
  const agent = opened.ws;
  agent.send({ t: "system", op: "app-switch" });
  await handled(agent);
  assert.deepEqual(agent.json("error"), []);
  await waitFor(() => device.messages.filter((message) => message.keycode === 187).length === 2, 3000,
    "the agent's action");

  // Once the bridge answers again, the rest is read and every action is recorded, in order.
  await rm(hold);
  await handled(control, 10_000);
  await waitFor(() => applied() === flood, 3000, "every action on the device");
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.6 });
  control.send({ t: "key", a: "up", code: 59 });
  await handled();
  assert.deepEqual(control.json("error"), []);
  const records = await waitFor(async () => {
    const found = await bridgeCalls(world, "record");
    return found.length === flood + 3 && found;
  }, 10_000, "every journal record");
  assert.deepEqual(records.filter((record) => record.origin === "human")
    .map((record) => record.payload.op ?? record.payload.kind),
  [...Array(flood).fill("back"), "gesture", "key"]);
  assert.deepEqual(records.filter((record) => record.origin === "agent").map((record) => record.payload.op),
    ["app-switch"]);
  for (const record of records) assert.equal(record.payload.count, undefined, "records were folded");
  const gesture = records.find((record) => record.payload.kind === "gesture").payload;
  assert.deepEqual([gesture.pointers, gesture.moves], [1, 0]);
  await stopCanvas(canvas);
});

test("input flood: 200k touch messages to a device that stops reading keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const moves = 48;
  const count = 200_000;
  const total = FLOOD_WARM_UP + count;
  const message = (i) => {
    const step = i % (moves + 2);
    const a = step === 0 ? "down" : step === moves + 1 ? "up" : "move";
    return JSON.stringify({ t: "touch", a, id: 7, x: step / 64, y: 0.5 });
  };
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    const socket = device.running.control;
    await stallDuring(flooding, () => socket.pause(), () => socket.resume());
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} touch messages`);
  assert.equal(control.count("error"), 0, "touch input was refused");
  await waitFor(() => device.count("touch") === total, 10_000, "every touch on the device");
  const records = await waitFor(async () => {
    const found = await journaled(world, "gesture");
    return found.length === total / (moves + 2) && found;
  }, 10_000, "one gesture record per gesture");
  for (const record of records) assert.deepEqual([record.payload.pointers, record.payload.moves], [1, moves]);
  await stopCanvas(canvas);
});

test("input flood: 200k scroll messages to a device that stops reading keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const count = 200_000;
  const total = FLOOD_WARM_UP + count;
  const message = (i) => JSON.stringify({ t: "scroll", x: (i % 50) / 50, y: 0.5, dx: 0, dy: i % 2 ? 1 : -1 });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    const socket = device.running.control;
    await stallDuring(flooding, () => socket.pause(), () => socket.resume());
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} scroll messages`);
  assert.equal(control.count("error"), 0, "scroll input was refused");
  await waitFor(() => device.count("scroll") === total, 10_000, "every scroll on the device");
  // Wheel bursts end after a still moment; together they hold every event.
  await waitFor(async () => {
    const records = await journaled(world, "scroll");
    return records.reduce((sum, record) => sum + record.payload.events, 0) === total;
  }, 10_000, "scroll records holding every event");
  await stopCanvas(canvas);
});

test("input flood: 200k key messages against a journal that stops answering keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const count = 200_000;
  const total = FLOOD_WARM_UP + count;
  // A held key repeats: each press is a down, 98 repeated downs and an up, and is
  // journaled once, so the journal fills while the flood is still arriving.
  const press = 100;
  const message = (i) => {
    const step = i % press;
    const code = Math.floor(i / press) % 2 ? 30 : 29;
    if (step === press - 1) return JSON.stringify({ t: "key", a: "up", code });
    return JSON.stringify({ t: "key", a: "down", code, repeat: step });
  };
  const hold = `${world.bridgeLog}.hold`;
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await stallDuring(flooding, () => writeFileSync(hold, ""), () => rmSync(hold));
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} key messages`);
  assert.equal(control.count("error"), 0, "key input was refused");
  await waitFor(() => device.count("key") === total, 10_000, "every key event on the device");
  const records = await waitFor(async () => {
    const found = await journaled(world, "key");
    return found.length === total / press && found;
  }, 10_000, "one key record per key press");
  assert.deepEqual(records.map((record) => record.payload.key),
    Array.from({ length: total / press }, (_, i) => (i % 2 ? 30 : 29)), "key presses out of order");
  await stopCanvas(canvas);
});

test("input flood: 100k text messages against a journal that stops answering keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const count = 100_000;
  const total = FLOOD_WARM_UP + count;
  const run = 500;
  const hold = `${world.bridgeLog}.hold`;
  const message = (i) => JSON.stringify({ t: "text", text: Math.floor(i / run) % 2 ? "b" : "a" });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await stallDuring(flooding, () => writeFileSync(hold, ""), () => rmSync(hold));
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} text messages`);
  assert.equal(control.count("error"), 0, "text input was refused");
  await waitFor(() => device.count("text") === total, 10_000, "every text on the device");
  const records = await waitFor(async () => {
    const found = await journaled(world, "text");
    return found.length === total && found;
  }, 10_000, "one text record per text");
  for (const letter of ["a", "b"]) {
    assert.equal(records.filter((record) => record.payload.text === letter).length, total / 2, `text ${letter}`);
  }
  await stopCanvas(canvas);
});

test("input flood: 100k paste messages of 1000 characters, each waiting for its clipboard ack from a device that stops reading, keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const count = 100_000;
  const message = JSON.stringify({ t: "paste", text: "p".repeat(1000) });
  let flooding;
  const watched = await watchCanvas(canvas, async () => {
    // 100 MB of pastes. Each one waits for its ack, so nearly all of them must wait in the
    // client and the kernel, not in Canvas memory; the paused device acks nothing for a while.
    flooding = control.flood(count, () => message);
    const socket = device.running.control;
    socket.pause();
    await sleep(FLOOD_STALL_MS);
    socket.resume();
    await sleep(FLOOD_STALL_MS);
  });
  assertBounded(t, watched, `${count} paste messages`);
  assert.equal(control.count("error"), 0, "paste input was refused");
  assert.equal(control.closeCode, null, "the flooding client was closed");
  const pasted = device.count("set-clipboard");
  t.diagnostic(`${pasted} of ${count} pastes reached the device in ${(2 * FLOOD_STALL_MS) / 1000} s`);
  assert.ok(pasted >= 2 && pasted < count / 100, `${pasted} pastes reached the device`);
  // The client goes away: what it still had waiting is dropped, nothing more is pasted.
  control.socket.destroy();
  await flooding;
  const records = await waitFor(async () => {
    const found = await journaled(world, "paste");
    return found.length === device.count("set-clipboard") && found;
  }, 10_000, "one paste record per paste on the device");
  for (const record of records) assert.deepEqual(Object.keys(record.payload).sort(), ["kind", "text_len", "transport"]);
  assert.equal((await status(canvas)).scrcpy.journal_dropped, 0, "paste records were dropped");
  await stopCanvas(canvas);
});

test("input flood: 100k clipboard-get messages while the client stops reading keep Canvas memory bounded", async (t) => {
  // The largest clipboard answer scrcpy-server sends, from a device slow to answer.
  const text = "c".repeat(256 * 1024 - 5);
  const { canvas, device, control } = await floodSetup(t, { device: { clipboardAnswer: { text, delayMs: 50 } } });
  const count = 100_000;
  let short = 0;
  control.on("message", (message) => {
    if (message.json?.t === "clipboard" && message.json.text?.length !== text.length) short += 1;
  });
  const message = () => JSON.stringify({ t: "clipboard-get" });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, message);
    // Answers of 256 KiB pile up for a client that does not read them.
    await stallDuring(flooding, () => control.socket.pause(), () => control.socket.resume());
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} clipboard-get messages`);
  assert.equal(control.count("error"), 0, "clipboard requests were refused");
  assert.equal(control.closeCode, null, "the client that read slowly was closed");
  await waitFor(() => control.count("clipboard") === device.count("get-clipboard"), 5000, "every clipboard answer");
  t.diagnostic(`${FLOOD_WARM_UP + count} requests shared ${device.count("get-clipboard")} device requests`);
  assert.equal(short, 0, "a clipboard answer was cut");
  await stopCanvas(canvas);
});

test("input flood: 100k system messages against a journal that stops answering keep Canvas memory bounded", async (t) => {
  const { world, canvas, device, control } = await floodSetup(t);
  const count = 100_000;
  const total = FLOOD_WARM_UP + count;
  const run = 1000;
  const hold = `${world.bridgeLog}.hold`;
  const message = (i) => JSON.stringify({ t: "system", op: Math.floor(i / run) % 2 ? "back" : "home" });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await stallDuring(flooding, () => writeFileSync(hold, ""), () => rmSync(hold));
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} system messages`);
  assert.equal(control.count("error"), 0, "system actions were refused");
  await waitFor(() => device.count("key") + device.count("back-or-screen-on") === 2 * total, 10_000,
    "every system action on the device");
  const records = await waitFor(async () => {
    const found = await journaled(world, "system");
    return found.length === total && found;
  }, 10_000, "one system record per action");
  for (const op of ["home", "back"]) {
    assert.equal(records.filter((record) => record.payload.op === op).length, total / 2, `system ${op}`);
  }
  await stopCanvas(canvas);
});

test("journal flood: 100k control messages against a journal that stops answering keep Canvas memory bounded", async (t) => {
  const { world, canvas, auth, control } = await floodSetup(t);
  // Every handoff change goes to every client, including ones that never read.
  const deaf = await pageSocket(canvas, auth, "/ws/control");
  const blind = await pageSocket(canvas, auth, "/ws/video");
  await deaf.next((message) => message.json?.t === "state");
  deaf.socket.pause();
  blind.socket.pause();
  const count = 100_000;
  const total = FLOOD_WARM_UP + count;
  const run = 500;
  const hold = `${world.bridgeLog}.hold`;
  let lastState = null;
  control.on("message", (message) => {
    if (message.json?.t === "state") lastState = message.json;
  });
  const message = (i) => JSON.stringify({ t: "control", mode: Math.floor(i / run) % 2 ? "resume" : "pause" });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await stallDuring(flooding, () => writeFileSync(hold, ""), () => rmSync(hold));
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} control messages`);
  assert.equal(control.count("error"), 0, "control messages were refused");
  // States are snapshots: clients that fell behind skipped most, and catch up with the last.
  await waitFor(() => lastState?.paused === false, 5000, "the flooding client's current state");
  for (const [name, ws] of [["the /ws/control client that never read", deaf], ["the /ws/video client that never read", blind]]) {
    const before = { states: ws.json("state").length, bytes: ws.received };
    const resumed = Date.now();
    ws.socket.resume();
    // The current state follows once the backlog has drained, and on a busy machine Canvas
    // may still be working through the flood then: a client that went quiet on an old
    // state gets more time, and one that never catches up still fails below.
    const caughtUp = async () => {
      await quiet(ws);
      return ws.json("state").at(-1).paused === false;
    };
    await waitFor(caughtUp, 10_000, "the current state").catch(() => {});
    const states = ws.json("state");
    if (states.at(-1).paused !== false) {
      const last = { ...states.at(-1) };
      delete last.presets;
      const now = await status(canvas);
      assert.fail(`${name} missed the current state: ${Date.now() - resumed} ms after it read again it had ` +
        `${states.length} states (${states.length - before.states} since) and ${ws.received} bytes ` +
        `(${ws.received - before.bytes} since); its last state was ${JSON.stringify(last)}; ` +
        `the Canvas has input_paused ${now.input_paused} and clients ${JSON.stringify(now.scrcpy)}`);
    }
  }
  t.diagnostic(`clients got ${control.count("state")}, ${deaf.json("state").length} and ` +
    `${blind.json("state").length} of ${total} state messages`);
  assert.ok(deaf.json("state").length < total / 2, "a control client that never read got every state");
  assert.ok(blind.json("state").length < total / 2, "a video client that never read got every state");
  const records = await waitFor(async () => {
    const found = await journaled(world, "control");
    return found.length === total && found;
  }, 10_000, "one control record per handoff change");
  for (const mode of ["pause", "resume"]) {
    assert.equal(records.filter((record) => record.payload.mode === mode).length, total / 2, `control ${mode}`);
  }
  assert.equal((await status(canvas)).input_paused, false);
  await stopCanvas(canvas);
});

// Holds what the Canvas writes to a WebSocket opened with "X-Test-Link: held", from the
// first message after its 101 answer on: a peer that reads nothing, but without the
// kernel socket buffers, so all the Canvas sent it waits in the Canvas socket. SIGUSR2
// lets every held link go, and they stay open from then on.
const HELD_LINK = `data:text/javascript,${encodeURIComponent(`
import { Server } from "node:http";
const releases = [];
const emit = Server.prototype.emit;
Server.prototype.emit = function (event, request, socket, ...rest) {
  if (event === "upgrade" && request.headers["x-test-link"] === "held") hold(socket);
  return emit.call(this, event, request, socket, ...rest);
};
function hold(socket) {
  const write = socket._write;
  const writev = socket._writev;
  // The 101 answer goes out; the link holds from the next write until SIGUSR2.
  let answered = false;
  let open = true;
  let held = null;
  const pass = (send) => {
    if (open) send();
    else held = send;
    if (!answered) {
      answered = true;
      open = false;
    }
  };
  socket._write = function (chunk, encoding, callback) {
    pass(() => write.call(this, chunk, encoding, callback));
  };
  socket._writev = function (chunks, callback) {
    pass(() => writev.call(this, chunks, callback));
  };
  releases.push(() => {
    open = true;
    const send = held;
    held = null;
    send?.();
  });
}
process.on("SIGUSR2", () => {
  for (const release of releases.splice(0)) release();
  process.stderr.write("held links open\\n");
});
`)}`;

// A larger high-water mark than Node's default for every stream of the Canvas.
const LARGE_HIGH_WATER = `data:text/javascript,${encodeURIComponent(`
import { setDefaultHighWaterMark } from "node:stream";
setDefaultHighWaterMark(false, 256 * 1024);
`)}`;

/** A video and a control client of the page whose links hold everything after the upgrade. */
async function heldClients(canvas, auth) {
  const headers = { "X-Test-Link": "held" };
  const video = await pageSocket(canvas, auth, "/ws/video", headers);
  const control = await pageSocket(canvas, auth, "/ws/control", headers);
  await waitFor(async () => {
    const { scrcpy } = await status(canvas);
    return scrcpy.video_clients === 1 && scrcpy.control_clients === 2 && scrcpy.key_frames >= 1;
  }, 5000, "both held clients and the key frame the video client asked for");
  return { video, control };
}

async function openHeldLinks(canvas) {
  canvas.child.kill("SIGUSR2");
  await waitFor(() => canvas.output().includes("held links open"), 5000, "the held links to open");
}

/** A state message without the constant presets, for messages. */
function stateText(state) {
  const shown = { ...state };
  delete shown.presets;
  return JSON.stringify(shown);
}

test("state catch-up: a client that fell behind gets the current state once its socket drains, whatever the socket's high-water mark", async (t) => {
  // With a high-water mark above the state bound, a client can be behind by more than the
  // bound while its socket owes no "drain" event, so that event alone never catches it up.
  const { canvas, auth, control } = await connectedControl(t, {
    nodeArgs: ["--import", HELD_LINK, "--import", LARGE_HIGH_WATER],
  });
  const behind = await heldClients(canvas, auth);
  // About 550 KB of states that all say paused, past the state bound and the high-water
  // mark, then the current one, which does not.
  const pauses = 1000;
  for (let i = 0; i < pauses; i += 1) control.send({ t: "control", mode: "pause" });
  control.send({ t: "control", mode: "resume" });
  await allHandled(control);
  assert.equal(control.json("state").at(-1).paused, false);
  await openHeldLinks(canvas);
  for (const [name, ws] of Object.entries(behind)) {
    await waitFor(() => ws.json("state").at(-1)?.paused === false, 5000, `the current state on the ${name} client`)
      .catch(() => assert.fail(`the ${name} client that fell behind missed the current state: it got ` +
        `${ws.json("state").length} states, the last ${stateText(ws.json("state").at(-1))}`));
    // Skipped states stay skipped: a client behind keeps at most a bound of them.
    assert.ok(ws.json("state").length < pauses, `the ${name} client got every state`);
  }
  t.diagnostic(`the held clients got ${behind.video.json("state").length} and ` +
    `${behind.control.json("state").length} of ${pauses + 1} states`);
  await stopCanvas(canvas);
});

test("state catch-up: a client whose backlog never drains gets the current state ahead of the next packet or reply it is sent", async (t) => {
  // A client behind on a slow link whose backlog never empties gets no "drain" event: the
  // video packets or replies it is sent anyway are its only chance of the current state.
  const { canvas, auth, device, control } = await connectedControl(t, { nodeArgs: ["--import", HELD_LINK] });
  const behind = await heldClients(canvas, auth);
  // More than the state bound of states that all say paused, then the current one.
  const pauses = 200;
  for (let i = 0; i < pauses; i += 1) control.send({ t: "control", mode: "pause" });
  control.send({ t: "control", mode: "resume" });
  await allHandled(control);
  const { packets } = (await status(canvas)).scrcpy;
  // The next packet for the video client; an error reply, then a key press it can watch on
  // the device, for the control client (its messages are handled in order).
  const next = device.sendPacket({ size: 200 }).readUInt32BE(5);
  const keys = device.count("key");
  behind.control.send("not json");
  behind.control.send({ t: "key", a: "down", code: 29 });
  behind.control.send({ t: "key", a: "up", code: 29 });
  await waitFor(async () => (await status(canvas)).scrcpy.packets > packets && device.count("key") === keys + 2,
    5000, "the packet and the key press handled");
  await openHeldLinks(canvas);
  await waitFor(() => behind.video.packets().some((packet) => packet.seq === next), 5000, "the next packet");
  await waitFor(() => behind.control.json("error").length === 1, 5000, "the error reply");
  const before = (ws, isNext) => {
    const at = ws.messages.findIndex(isNext);
    return ws.messages.slice(0, at).filter((message) => message.json?.t === "state").map((message) => message.json);
  };
  const cases = [
    ["video", "packet", before(behind.video, (message) => message.binary?.[0] === 3 && message.binary.readUInt32BE(15) === next)],
    ["control", "reply", before(behind.control, (message) => message.json?.t === "error")],
  ];
  for (const [name, what, states] of cases) {
    assert.ok(states.length < pauses, `the ${name} client got every state`);
    assert.equal(states.at(-1)?.paused, false, `the ${name} client that fell behind got the next ${what} ` +
      `before the current state; the last of its ${states.length} states before it: ${stateText(states.at(-1))}`);
  }
  await stopCanvas(canvas);
});

test("input flood: 300k ping messages from a client that reads its pongs slowly keep Canvas memory bounded", async (t) => {
  const { canvas, control } = await floodSetup(t);
  const count = 300_000;
  const message = (i) => JSON.stringify({ t: "ping", ts: i });
  await warmUp(control, message);
  const watched = await watchCanvas(canvas, async () => {
    const flooding = control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await stallDuring(flooding, () => control.socket.pause(), () => control.socket.resume());
    await allHandled(control);
  });
  assertBounded(t, watched, `${count} ping messages`);
  assert.equal(control.closeCode, null, "the client that read slowly was closed");
  // Each flood ends with one more ping, from allHandled.
  assert.equal(control.count("pong"), FLOOD_WARM_UP + count + 2, "a pong was lost");
  assert.equal(control.count("error"), 0);
  await stopCanvas(canvas);
});

test("journal: a gesture with 102 downs and a wheel burst past 1e6 each get exactly one record from the real bridge, clamped to its ranges", async (t) => {
  // The real bridge, with its validation, journaling into a current session on the Canvas target.
  const python = (await execFileAsync("python3", ["-c", "import sys; print(sys.executable)"])).stdout.trim();
  const world = await makeWorld(t, { env: { PYTHONDONTWRITEBYTECODE: "1" } });
  const sessions = join(world.env.AUTONOM_HOME, "sessions");
  const artifacts = join(sessions, "s_canvas_clamp");
  await mkdir(artifacts, { recursive: true });
  await writeFile(join(sessions, "current.json"), JSON.stringify({
    schema_version: 2, session_id: "s_canvas_clamp", platform: "android", target_id: SERIAL, serial: SERIAL,
    artifacts_dir: artifacts,
  }));
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile,
    "--python", python, "--bridge", join(ROOT, "scripts/autonom_canvas_bridge.py")]);
  const auth = await login(canvas);
  const control = await pageSocket(canvas, auth, "/ws/control");
  const { device } = world;
  await waitFor(() => device.connected, 8000, "the fake device connection");
  await control.next((message) => message.json?.t === "state" && message.json.session === "streaming" &&
    message.json.width === device.size.width);
  // One gesture: a finger stays down while another goes down and up 101 times.
  control.send({ t: "touch", a: "down", id: 0, x: 0.5, y: 0.5 });
  for (let i = 0; i < 101; i += 1) {
    control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.4 });
    control.send({ t: "touch", a: "up", id: 1, x: 0.4, y: 0.4 });
  }
  control.send({ t: "touch", a: "up", id: 0, x: 0.5, y: 0.5 });
  // One wheel burst of 63,000 events of -16: past -1e6 in all.
  await control.flood(63_000, () => JSON.stringify({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -16 }));
  await allHandled(control);
  const journal = join(artifacts, "journal.ndjson");
  const entries = await waitFor(async () => {
    const text = await readFile(journal, "utf8").catch(() => "");
    const found = text.split("\n").filter(Boolean).map((line) => JSON.parse(line));
    return found.length >= 2 && found;
  }, 10_000, "the journal entries");
  await sleep(500);
  const all = (await readFile(journal, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.deepEqual(all.map((entry) => [entry.verb, entry.origin, entry.result?.via]),
    [["ui gesture", "human", "scrcpy"], ["ui scroll", "human", "scrcpy"]]);
  assert.equal(entries.length, 2);
  const detail = async (entry) => JSON.parse(await readFile(join(artifacts, entry.result.detail), "utf8"));
  const gesture = await detail(all[0]);
  const wheel = await detail(all[1]);
  assert.deepEqual([gesture.pointers, gesture.moves], [100, 0]);
  assert.deepEqual([wheel.events, wheel.dx, wheel.dy], [63_000, 0, -1_000_000]);
  assert.doesNotMatch(String((await status(canvas)).last_error ?? ""), /journal/);
  await stopCanvas(canvas);
});

// Steps the Canvas's wall clock (Date.now) back an hour on SIGUSR2, as a manual change or
// an NTP correction would, and says so on stderr.
const CLOCK_STEP = `data:text/javascript,${encodeURIComponent(`
const now = Date.now;
let offset = 0;
Date.now = () => now() + offset;
process.on("SIGUSR2", () => {
  offset -= 3_600_000;
  process.stderr.write("clock stepped back\\n");
});
`)}`;

test("journal: a wall clock stepped back during a gesture still gives the real bridge exactly one gesture record, with a duration of at least zero", async (t) => {
  // The real bridge, with its validation, journaling into a current session on the Canvas target.
  const python = (await execFileAsync("python3", ["-c", "import sys; print(sys.executable)"])).stdout.trim();
  const world = await makeWorld(t, { env: { PYTHONDONTWRITEBYTECODE: "1" } });
  const sessions = join(world.env.AUTONOM_HOME, "sessions");
  const artifacts = join(sessions, "s_canvas_clock");
  await mkdir(artifacts, { recursive: true });
  await writeFile(join(sessions, "current.json"), JSON.stringify({
    schema_version: 2, session_id: "s_canvas_clock", platform: "android", target_id: SERIAL, serial: SERIAL,
    artifacts_dir: artifacts,
  }));
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile,
    "--python", python, "--bridge", join(ROOT, "scripts/autonom_canvas_bridge.py")], { nodeArgs: ["--import", CLOCK_STEP] });
  const auth = await login(canvas);
  const control = await pageSocket(canvas, auth, "/ws/control");
  const { device } = world;
  await waitFor(() => device.connected, 8000, "the fake device connection");
  await control.next((message) => message.json?.t === "state" && message.json.session === "streaming" &&
    message.json.width === device.size.width);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  await waitFor(() => device.touches().length === 2, 3000, "the finger down and moved");
  // The wall clock jumps back an hour mid-gesture.
  canvas.child.kill("SIGUSR2");
  await waitFor(() => canvas.output().includes("clock stepped back"), 3000, "the clock step");
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.6 });
  const journal = join(artifacts, "journal.ndjson");
  await waitFor(async () => (await readFile(journal, "utf8").catch(() => "")).trim().length > 0, 10_000,
    "the gesture entry");
  await sleep(300);
  const entries = (await readFile(journal, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.deepEqual(entries.map((entry) => [entry.verb, entry.origin, entry.result?.via]),
    [["ui gesture", "human", "scrcpy"]]);
  const gesture = JSON.parse(await readFile(join(artifacts, entries[0].result.detail), "utf8"));
  assert.ok(gesture.duration_ms >= 0, `duration_ms ${gesture.duration_ms}`);
  assert.deepEqual([gesture.pointers, gesture.moves], [1, 1]);
  assert.doesNotMatch(String((await status(canvas)).last_error ?? ""), /journal/);
  await stopCanvas(canvas);
});

test("journal: repeated identical actions each get their own record, at once or while the bridge is slow", async (t) => {
  // Each record takes the bridge a while, so later ones always find earlier ones waiting.
  const { world, canvas, device, control } = await connectedControl(t, { env: { FAKE_BRIDGE_RECORD_MS: "40" } });
  for (let i = 0; i < 3; i += 1) control.send({ t: "system", op: "back" });
  for (let i = 0; i < 3; i += 1) {
    control.send({ t: "key", a: "down", code: 67 });
    control.send({ t: "key", a: "up", code: 67 });
  }
  for (let i = 0; i < 3; i += 1) {
    control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
    control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  }
  for (let i = 0; i < 3; i += 1) control.send({ t: "text", text: "same" });
  // A human pressing DPAD_DOWN four times, slower than the device but faster than the journal.
  for (let i = 0; i < 4; i += 1) {
    control.send({ t: "key", a: "down", code: 20 });
    control.send({ t: "key", a: "up", code: 20 });
    await sleep(25);
  }
  const expected = [
    ...Array(3).fill("system back"), ...Array(3).fill("key 67"), ...Array(3).fill("gesture"),
    ...Array(3).fill("text same"), ...Array(4).fill("key 20"),
  ];
  const records = await waitFor(async () => {
    const found = await journaled(world);
    return found.length >= expected.length && found;
  }, 5000, "one record per action");
  await sleep(300);
  assert.equal((await bridgeCalls(world, "record")).length, expected.length, "an action got more than one record");
  assert.deepEqual(records.map(({ payload }) => (payload.kind === "system" ? `system ${payload.op}`
    : payload.kind === "key" ? `key ${payload.key}`
      : payload.kind === "text" ? `text ${payload.text}` : payload.kind)), expected);
  for (const { payload } of records.filter((record) => record.payload.kind === "gesture")) {
    assert.deepEqual([payload.pointers, payload.moves, payload.start, payload.end], [1, 0, [540, 1200], [540, 1200]]);
  }
  assert.equal(device.count("back-or-screen-on"), 6);
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("journal: past 256 waiting records the Canvas drops records, counts them in /status and sends no other kind", async (t) => {
  const { world, canvas, auth, device } = await streamingCanvas(t, { device: { keep: false } });
  const hold = `${world.bridgeLog}.hold`;
  writeFileSync(hold, "");
  const open = async () => {
    const ws = await pageSocket(canvas, auth, "/ws/control");
    await ws.next((message) => message.json?.t === "state" && message.json.session === "streaming" &&
      message.json.width === device.size.width, 8000);
    return ws;
  };
  // Three pages each hold a finger down: lifting them later journals three gestures.
  const holders = [];
  for (let i = 0; i < 3; i += 1) {
    const ws = await open();
    ws.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
    holders.push(ws);
  }
  await waitFor(() => device.count("touch") === 3, 3000, "three fingers down");
  // Four pages flood actions: each stops being read at 64 unanswered records, and
  // together they fill the Canvas journal (256, of which 16 are with the bridge).
  const flooders = [];
  for (let i = 0; i < 4; i += 1) flooders.push(await open());
  for (const ws of flooders) {
    for (let i = 0; i < 200; i += 1) ws.send({ t: "system", op: "back" });
  }
  const applied = () => device.count("back-or-screen-on") / 2;
  await waitFor(() => applied() === 4 * 64, 5000, "the flood up to the journal bound");
  await sleep(200);
  assert.equal(applied(), 4 * 64, "a page was read past its journal bound");
  assert.equal((await bridgeCalls(world, "record")).length, 16, "the journal sent more than 16 records unanswered");
  const before = await status(canvas);
  assert.deepEqual([before.scrcpy.journal_pending, before.scrcpy.journal_dropped], [256, 0]);

  // An agent takes over: the three fingers are lifted, but the journal is full.
  const takeover = await fetch(`${canvas.origin}/control`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
    body: JSON.stringify({ mode: "takeover" }),
  });
  assert.equal(takeover.status, 200);
  await waitFor(() => device.count("touch") === 6, 3000, "the fingers lifted");
  const full = await status(canvas);
  assert.deepEqual([full.scrcpy.journal_pending, full.scrcpy.journal_dropped], [256, 3]);
  assert.match(full.last_error, /journal: 3 actions were not journaled/);
  rmSync(hold);
  const records = await waitFor(async () => {
    const found = await journaled(world, "system");
    return found.length === 4 * 64 && found;
  }, 10_000, "every waiting record");
  for (const record of records) assert.deepEqual(record.payload, { transport: "scrcpy", kind: "system", op: "back" });
  // The rest of each flood is read again, and refused: the agent owns control now.
  const refused = () => flooders.reduce((sum, ws) => sum + ws.json("error").length, 0);
  await waitFor(() => refused() === 4 * 200 - 4 * 64, 5000, "the rest of the floods refused");
  for (const ws of flooders) assert.match(ws.json("error")[0].message, /owned by agent/);
  await sleep(200);
  const all = await bridgeCalls(world, "record");
  assert.equal(all.length, 4 * 64, "a dropped or refused action was journaled");
  assert.ok(all.every((record) => RECORD_KINDS.includes(record.payload.kind)), "a record kind the bridge refuses");
  const after = await status(canvas);
  assert.deepEqual([after.scrcpy.journal_pending, after.scrcpy.journal_dropped], [0, 3]);
  await stopCanvas(canvas);
});

test("fanout: restart backoff doubles from 1 s to 10 s and resets after 30 s of streaming", () => {
  const delays = [];
  let backoff = 1000;
  for (let i = 0; i < 6; i += 1) {
    const { delay, next } = restartDelay(backoff, 500);
    delays.push(delay);
    backoff = next;
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 10_000, 10_000]);
  assert.deepEqual(restartDelay(10_000, HEALTHY_STREAM_MS), { delay: 1000, next: 2000 });
});

// --- Display presets (DISPLAY-001..008) ----------------------------------------------
// Against the fake adb's display (physical 1080x2400 @ 420, see FAKE_WM); no device.

const PRESETS = [
  { id: "small", label: "Small phone", width: 720, height: 1280, density: 320 },
  { id: "pixel-11", label: "Pixel 11", width: 1080, height: 2424, density: 420 },
  { id: "pixel-fold", label: "Pixel Fold (open)", width: 2208, height: 1840, density: 420 },
  { id: "tablet", label: "Tablet", width: 2560, height: 1600, density: 320 },
  { id: "default", label: "Device default" },
];
const PRESET_IDS = "small, pixel-11, pixel-fold, tablet, default";

/** The fake device's display overrides now, null where there is none. */
async function wmNow(world) {
  const saved = await readFile(world.wmState, "utf8").catch(() => null);
  if (saved) return JSON.parse(saved);
  const [size = "", density = ""] = (world.env.FAKE_WM_OVERRIDE ?? "").split("@");
  return { size: size || null, density: density ? Number(density) : null };
}

/** `wm` calls as "size", "density 320", ...; every one names the Canvas serial (I-1). */
function wmCallsIn(calls) {
  const wm = calls.filter((args) => args.includes("wm"));
  for (const args of wm) assert.deepEqual(args.slice(0, 4), ["-s", SERIAL, "shell", "wm"], JSON.stringify(args));
  return wm.map((args) => args.slice(4).join(" "));
}

async function wmCalls(world) {
  return wmCallsIn(await adbCalls(world));
}

/** The same, read at once: for a check made the moment the device reads a message. */
function wmCallsNow(world) {
  return wmCallsIn(readFileSync(world.adbLog, "utf8").trim().split("\n").filter(Boolean)
    .map((line) => JSON.parse(line)));
}

/** Only the calls that change the display; the others read it. */
function wmChanges(calls) {
  return calls.filter((call) => call.includes(" "));
}

/** POST /display as an API client, an agent unless `origin` says otherwise. */
async function postDisplay(canvas, preset, origin = "agent") {
  const response = await rawHttp(canvas.port, {
    method: "POST", path: "/display", body: JSON.stringify({ preset }),
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": origin },
  });
  return { status: response.status, body: JSON.parse(response.body) };
}

/** Ask for a preset on a control socket; resolves with its answer, a display or an error. */
function wsDisplay(ws, preset) {
  const answer = ws.reply((message) => message.t === "display" || (message.t === "error" && message.for === "display"));
  ws.send({ t: "display", preset });
  return answer;
}

/** Display journal records as "<origin> <preset> <W>x<H>@<density>", in bridge order. */
async function displayRecords(world) {
  return (await journaled(world, "display")).map(({ origin, payload }) =>
    `${origin} ${payload.preset} ${payload.width}x${payload.height}@${payload.density}`);
}

/** Read /stream.mjpeg in the background; `frames` collects each part's bytes. */
async function readMultipart(canvas) {
  const controller = new AbortController();
  const response = await fetch(`${canvas.origin}/stream.mjpeg`, {
    headers: { Authorization: `Bearer ${TOKEN}` }, signal: controller.signal,
  });
  const reader = response.body.getReader();
  const stream = { frames: [], close: () => controller.abort() };
  let pending = Buffer.alloc(0);
  (async () => {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      pending = Buffer.concat([pending, Buffer.from(value)]);
      for (;;) {
        const head = pending.indexOf("\r\n\r\n");
        if (head < 0) break;
        const length = Number(pending.toString("latin1", 0, head).match(/Content-Length: (\d+)/)?.[1]);
        if (!Number.isFinite(length) || pending.length < head + 4 + length + 2) break;
        stream.frames.push(Buffer.from(pending.subarray(head + 4, head + 4 + length)));
        pending = pending.subarray(head + 4 + length + 2);
      }
    }
  })().catch(() => {});
  return stream;
}

/** Width and height from a PNG header. */
function pngSize(frame) {
  return [frame.readUInt32BE(16), frame.readUInt32BE(20)];
}

// Answers the simulator list and screenshots like `xcrun simctl`, and logs every call.
const FAKE_XCRUN = String.raw`
import { appendFileSync, writeFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify(args) + "\n");
if (args[0] === "simctl" && args[1] === "list") {
  console.log(JSON.stringify({ devices: { "iOS 26.0": [{ udid: process.env.FAKE_UDID, state: "Booted", isAvailable: true }] } }));
} else if (args[0] === "simctl" && args[1] === "io") {
  // Like Xcode 27: the last argument is always a file path, and "-" is a file named "-".
  const write = () => {
    writeFileSync(args.at(-1), Buffer.from(process.env.FAKE_PNG, "base64"));
    console.log("Wrote screenshot to: " + args.at(-1));
  };
  if (process.env.FAKE_SCREENSHOT_DELAY_MS) setTimeout(write, Number(process.env.FAKE_SCREENSHOT_DELAY_MS));
  else write();
} else {
  console.error("unsupported fake xcrun command: " + args.join(" "));
  process.exitCode = 2;
}
`;

// Reads H.264 on stdin and writes a small JPEG every 50 ms until stdin ends or it is stopped.
const FAKE_FFMPEG = String.raw`
const frame = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 4, 0, 0, 0xff, 0xd9]);
process.stdin.resume();
process.stdin.on("end", () => process.exit(0));
setInterval(() => process.stdout.write(frame), 50);
`;

// /status and state messages show a `wm` reading at most this old (DISPLAY_READ_MAX_AGE_MS).
const DISPLAY_READ_MAX_AGE_MS = 2000;
// While a WebSocket client is connected the reading is renewed once it is this old.
const DISPLAY_WATCH_AGE_MS = 1500;

/** Until the Canvas has read the display it found when this page connected: the device default. */
async function settledDisplay(control) {
  await control.next((message) => message.json?.t === "state" && message.json.preset === "default");
}

/** The fake device's display overrides, read at once. */
function wmStateNow(world) {
  try {
    return JSON.parse(readFileSync(world.wmState, "utf8"));
  } catch {
    return { size: null, density: null };
  }
}

/** A /status display as [width, height, density, preset]. */
function displayValues({ width, height, density, preset }) {
  return [width, height, density, preset];
}

function agentHeaders() {
  return { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" };
}

/** Collect the display answers a control socket gets from now on. */
function displayAnswers(control) {
  const answers = [];
  control.on("message", (message) => {
    if (message.json?.t === "display" || message.json?.for === "display") answers.push(message.json);
  });
  return answers;
}

// How long GET /status may take during the display flood (STATUS_SAMPLE_MS for the others).
const DISPLAY_FLOOD_STATUS_MS = 60_000;

function supersededAnswer(preset) {
  return { t: "error", for: "display", superseded: true,
    message: `The display change to ${preset} was superseded by a later one` };
}

test("display: /status lists the five presets in order and reports the device default with its physical size and density", async (t) => {
  const { world, canvas, control } = await connectedControl(t);
  // A page's state messages carry the preset, read when it connected, without asking /status.
  const state = (await control.next((message) => message.json?.t === "state" && message.json.preset === "default")).json;
  assert.deepEqual([state.density, state.presets], [420, PRESETS]);
  const body = await status(canvas);
  assert.deepEqual(body.display, { width: 1080, height: 2400, density: 420, preset: "default", presets: PRESETS });
  assert.equal(body.last_error, null);
  // Only reads, each `wm size` then `wm density`, all with the Canvas serial.
  const calls = await wmCalls(world);
  assert.ok(calls.length >= 2 && calls.length % 2 === 0, JSON.stringify(calls));
  assert.ok(calls.every((call, index) => call === (index % 2 ? "density" : "size")), JSON.stringify(calls));
  await stopCanvas(canvas);
});

waitingTest("display: /status reads the display again once its reading is 2 s old, requests made together share one reading, and an override made outside this Canvas shows with its density and no preset", async (t) => {
  // No WebSocket client, so only /status reads the display.
  const { world, canvas } = await streamingCanvas(t);
  assert.equal((await status(canvas)).display.preset, "default");
  // Another Canvas, or `adb shell wm`, changes the device.
  await writeFile(world.wmState, JSON.stringify({ size: "1600x2560", density: 300 }));
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  const reads = (await wmCalls(world)).length;
  const answers = await Promise.all([status(canvas), status(canvas), status(canvas)]);
  for (const body of answers) assert.deepEqual(displayValues(body.display), [1600, 2560, 300, null]);
  assert.deepEqual((await wmCalls(world)).slice(reads), ["size", "density"]);
  // A reading younger than 2 s is answered again without asking the device.
  assert.deepEqual(displayValues((await status(canvas)).display), [1600, 2560, 300, null]);
  assert.equal((await wmCalls(world)).length, reads + 2);
  // An override this Canvas made is its preset while the values in effect match it.
  assert.equal((await postDisplay(canvas, "tablet")).body.display.preset, "tablet");
  assert.deepEqual(displayValues((await status(canvas)).display), [2560, 1600, 320, "tablet"]);
  await writeFile(world.wmState, JSON.stringify({ size: "2560x1600", density: 280 }));
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  assert.deepEqual(displayValues((await status(canvas)).display), [2560, 1600, 280, null]);
  // Reset from outside: the device default again.
  await writeFile(world.wmState, JSON.stringify({ size: null, density: null }));
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  assert.deepEqual(displayValues((await status(canvas)).display), [1080, 2400, 420, "default"]);
  // The stop puts back what the device had before this Canvas's first change.
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(await wmNow(world), { size: "1600x2560", density: 300 });
  assert.match(exit.stdout, /Display restored: 1600x2560 @ 300\n/);
});

waitingTest("display: a page that never asks /status gets state messages with the display read at most 2 s before: an outside change reaches it, a takeover after it shows it, and nothing reads the display once no page is connected", async (t) => {
  const { world, canvas, control } = await connectedControl(t);
  await settledDisplay(control);
  // Nobody asks /status from here on; the Canvas renews its reading while a socket is open.
  const changedAt = Date.now();
  await writeFile(world.wmState, JSON.stringify({ size: "1600x2560", density: 300 }));
  const outside = await control.until((message) => message.t === "state" && message.preset === null,
    DISPLAY_READ_MAX_AGE_MS + 1500);
  t.diagnostic(`the outside change reached the page ${Date.now() - changedAt} ms after it was made`);
  assert.equal(outside.density, 300);
  // A state message sent later for another reason shows the display as it is too.
  await sleep(DISPLAY_READ_MAX_AGE_MS + 500);
  const owned = control.until((message) => message.t === "state" && message.owner === "agent");
  await agentTakeover(canvas);
  const ownedState = await owned;
  assert.deepEqual([ownedState.preset, ownedState.density], [null, 300]);
  await writeFile(world.wmState, JSON.stringify({ size: null, density: null }));
  const reset = await control.until((message) => message.t === "state" && message.preset === "default",
    DISPLAY_READ_MAX_AGE_MS + 1500);
  assert.equal(reset.density, 420);
  // Renewed about every 1.5 s, not more often.
  const readings = async () => (await wmCalls(world)).filter((call) => call === "size").length;
  const before = await readings();
  await sleep(4600);
  const renewed = (await readings()) - before;
  assert.ok(renewed >= 2 && renewed <= 4, `${renewed} readings in 4.6 s`);
  // With no page connected nothing reads the display unasked.
  control.close();
  await control.closed;
  await sleep(500);
  const idle = await readings();
  await sleep(DISPLAY_WATCH_AGE_MS + 1000);
  assert.equal(await readings(), idle, "the display was read with no page connected");
  await stopCanvas(canvas);
});

waitingTest("display: a socket that connects once the last reading is over 2 s old never gets that reading in a state message: its first one leaves the display out until one fresh reading, which then reaches it, on the control and the video socket", async (t) => {
  // No socket yet, so only /status reads the display, and nothing renews that reading.
  const { world, canvas, auth } = await streamingCanvas(t);
  assert.equal((await status(canvas)).display.preset, "default");
  // Socket, the overrides another Canvas or `adb shell wm` makes meanwhile, and the
  // preset and density then in effect.
  const rounds = [
    ["/ws/control", { size: "1600x2560", density: 300 }, [null, 300]],
    ["/ws/video", { size: null, density: null }, ["default", 420]],
  ];
  for (const [path, overrides, expected] of rounds) {
    await writeFile(world.wmState, JSON.stringify(overrides));
    await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
    const reads = (await wmCalls(world)).length;
    const socket = await pageSocket(canvas, auth, path);
    await socket.next((message) => message.json?.t === "state" && "preset" in message.json,
      DISPLAY_READ_MAX_AGE_MS);
    const states = socket.json("state");
    for (const state of states.filter((message) => "preset" in message)) {
      assert.deepEqual([state.preset, state.density], expected, `${path} got a reading over 2 s old`);
    }
    assert.ok(!("preset" in states[0]) && !("density" in states[0]), JSON.stringify(states[0]));
    // One reading for the new socket, not one per state message.
    assert.deepEqual((await wmCalls(world)).slice(reads), ["size", "density"]);
    socket.close();
    await socket.closed;
  }
  await stopCanvas(canvas);
});

waitingTest("display: while a change runs, /status and state messages never name its preset before both its commands took effect", async (t) => {
  // Each wm command takes 1.5 s, so a /status reading falls between the two.
  const { world, canvas, control } = await connectedControl(t, { env: { FAKE_WM_SET_MS: "1500" } });
  await settledDisplay(control);
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  const tabletInEffect = () => {
    const now = wmStateNow(world);
    return now.size === "2560x1600" && now.density === 320;
  };
  const early = [];
  control.on("message", (message) => {
    if (message.json?.t === "state" && message.json.preset === "tablet" && !tabletInEffect()) early.push("state");
  });
  let answered = null;
  wsDisplay(control, "tablet").then((answer) => { answered = answer; });
  const seen = [];
  while (!answered) {
    // A new connection per poll: a reused fetch connection can stall for seconds here.
    const polled = await rawHttp(canvas.port, { path: "/status", headers: { Authorization: `Bearer ${TOKEN}` } });
    const { preset } = JSON.parse(polled.body).display;
    if (preset === "tablet" && !tabletInEffect()) early.push("status");
    seen.push(preset);
    await sleep(100);
  }
  t.diagnostic(`/status during the change: ${[...new Set(seen)].map(String).join(", ")}`);
  assert.deepEqual(early, [], "the preset was named before its commands took effect");
  assert.ok(seen.includes(null), "no /status read the display half changed");
  assert.equal(answered.display.preset, "tablet");
  assert.equal((await status(canvas)).display.preset, "tablet");
  await stopCanvas(canvas);
});

test("display: the owner switches to Tablet and back to the default over /ws/control, the video size follows without a restart, and each change is one journal record", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  await settledDisplay(control);
  const reads = (await wmCalls(world)).length;
  const tablet = await wsDisplay(control, "tablet");
  assert.deepEqual(tablet, { t: "display", ok: true, display: { preset: "tablet", width: 2560, height: 1600, density: 320 } });
  assert.deepEqual(await wmNow(world), { size: "2560x1600", density: 320 });
  // The preset's two commands; the readings around them may share the log with the
  // renewal that runs while a page is connected.
  assert.deepEqual(wmChanges((await wmCalls(world)).slice(reads)), ["size 2560x1600", "density 320"]);
  // scrcpy-server follows a wm size change with a new session at the new size (max 1280).
  device.sendSession(1280, 800);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280 &&
    message.json.preset === "tablet");

  const resets = (await wmCalls(world)).length;
  const back = await wsDisplay(control, "default");
  assert.deepEqual(back.display, { preset: "default", width: 1080, height: 2400, density: 420 });
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  assert.deepEqual(wmChanges((await wmCalls(world)).slice(resets)), ["size reset", "density reset"]);
  device.sendSession(570, 1280);
  await control.next((message) => message.json?.t === "state" && message.json.width === 570 &&
    message.json.preset === "default");

  const body = await status(canvas);
  assert.deepEqual([body.scrcpy.restarts, device.spawns.length, control.closeCode], [0, 1, null]);
  assert.deepEqual(body.display, { width: 1080, height: 2400, density: 420, preset: "default", presets: PRESETS });
  const records = await waitFor(async () => {
    const found = await journaled(world, "display");
    return found.length === 2 && found;
  }, 5000, "one record per change");
  assert.deepEqual(records.map(({ origin, payload }) => ({ origin, ...payload })), [
    { origin: "human", kind: "display", transport: "scrcpy", preset: "tablet", width: 2560, height: 1600, density: 320 },
    { origin: "human", kind: "display", transport: "scrcpy", preset: "default", width: 1080, height: 2400, density: 420 },
  ]);
  assert.match(canvas.output(), /Display: tablet 2560x1600 @ 320\n/);
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("display: an agent switches to Pixel 11 over POST /display, gets the size and density in effect, and the change is journaled as the agent's", async (t) => {
  const { world, canvas } = await streamingCanvas(t);
  const answer = await postDisplay(canvas, "pixel-11");
  assert.deepEqual(answer, { status: 200, body: { ok: true, display: { preset: "pixel-11", width: 1080, height: 2424, density: 420 } } });
  // 420 is the physical density, so Android keeps no density override.
  assert.deepEqual(await wmNow(world), { size: "1080x2424", density: null });
  assert.deepEqual(displayValues((await status(canvas)).display), [1080, 2424, 420, "pixel-11"]);
  const records = await waitFor(async () => {
    const found = await displayRecords(world);
    return found.length === 1 && found;
  }, 5000, "the display record");
  assert.deepEqual(records, ["agent pixel-11 1080x2424@420"]);
  await stopCanvas(canvas);
});

test("display: refused requests run no wm command and are not journaled: another owner, paused, unknown or malformed presets, no CSRF value, no token, a foreign Host", async (t) => {
  const { world, canvas, auth, control } = await connectedControl(t);
  await settledDisplay(control);
  const readings = async () => (await wmCalls(world)).filter((call) => call === "size").length;
  const readsBefore = await readings();
  const refusalsFrom = Date.now();
  await agentTakeover(canvas);
  await control.next((message) => message.json?.t === "state" && message.json.owner === "agent");
  const owned = await wsDisplay(control, "tablet");
  assert.deepEqual(owned, { t: "error", for: "display", message: "Canvas control is owned by agent" });
  assert.deepEqual(await postDisplay(canvas, "tablet", "human"), { status: 409, body: { error: "Canvas control is owned by agent" } });
  // The agent holds control: an unknown preset is refused naming the valid ids.
  const huge = await postDisplay(canvas, "huge");
  assert.deepEqual(huge, { status: 400, body: { error: `Unknown display preset; expected one of: ${PRESET_IDS}` } });
  for (const preset of [undefined, null, "", 3, true, "Tablet", "TABLET", "pixel_11", "restore", ["tablet"], { id: "tablet" }]) {
    assert.equal((await postDisplay(canvas, preset)).status, 400, JSON.stringify(preset));
  }
  const opened = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(opened.status, 101);
  const agent = opened.ws;
  await agent.next((message) => message.json?.t === "state");
  for (const preset of [7, "huge", null]) {
    const malformed = await wsDisplay(agent, preset);
    assert.deepEqual([malformed.t, malformed.message], ["error", `Unknown display preset; expected one of: ${PRESET_IDS}`]);
  }
  // Paused, nobody may change the display.
  const paused = control.reply((message) => message.t === "state" && message.owner === "shared" && message.paused);
  agent.send({ t: "control", mode: "release" });
  agent.send({ t: "control", mode: "pause" });
  await paused;
  assert.deepEqual(await postDisplay(canvas, "tablet"), { status: 409, body: { error: "Canvas input is paused" } });
  assert.equal((await wsDisplay(control, "tablet")).message, "Canvas input is paused");
  assert.equal((await wsDisplay(agent, "tablet")).message, "Canvas input is paused");
  const resumed = control.reply((message) => message.t === "state" && message.paused === false);
  agent.send({ t: "control", mode: "resume" });
  await resumed;
  // HTTP needs the page's CSRF value with its cookie, or the token, and the Canvas Host.
  const body = JSON.stringify({ preset: "tablet" });
  const json = { "Content-Type": "application/json" };
  const noCsrf = await rawHttp(canvas.port, { method: "POST", path: "/display", body, headers: { ...json, Cookie: auth.cookie } });
  assert.equal(noCsrf.status, 403);
  const wrongCsrf = await rawHttp(canvas.port, { method: "POST", path: "/display", body,
    headers: { ...json, Cookie: auth.cookie, "X-Autonom-CSRF": "wrong" } });
  assert.equal(wrongCsrf.status, 403);
  assert.equal((await rawHttp(canvas.port, { method: "POST", path: "/display", body, headers: json })).status, 401);
  const rebound = await rawHttp(canvas.port, { method: "POST", path: "/display", body,
    headers: { ...json, Authorization: `Bearer ${TOKEN}`, Host: "evil.test" } });
  assert.equal(rebound.status, 403);
  await sleep(200);
  assert.deepEqual(wmChanges(await wmCalls(world)), [], "a refused request changed the display");
  // Readings come only from the renewal while a page is connected, at most one per 1.5 s;
  // any of the 30-odd refused requests reading the display would have added one each.
  const renewals = Math.ceil((Date.now() - refusalsFrom) / DISPLAY_WATCH_AGE_MS) + 1;
  assert.ok((await readings()) - readsBefore <= renewals, "a refused request read the display");
  assert.deepEqual(await journaled(world, "display"), []);
  // With its CSRF value the page's own POST goes through.
  const page = await rawHttp(canvas.port, { method: "POST", path: "/display", body,
    headers: { ...json, Cookie: auth.cookie, "X-Autonom-CSRF": auth.csrf } });
  assert.equal(page.status, 200, page.body);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2560x1600", "density 320"]);
  agent.close();
  await stopCanvas(canvas);
});

test("display: iOS refuses display presets without running anything and its page has no Size picker", async (t) => {
  const udid = "FAKE-SIMULATOR-UDID";
  const world = await makeWorld(t, { env: { FAKE_UDID: udid } });
  const xcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(xcrun, `#!${process.execPath}\n${FAKE_XCRUN}`);
  await chmod(xcrun, 0o755);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", udid, "--simctl", xcrun]);
  assert.deepEqual(await postDisplay(canvas, "tablet"), { status: 400, body: { error: "Display presets are Android-only" } });
  assert.equal((await postDisplay(canvas, "huge")).status, 400);
  const body = await status(canvas);
  assert.equal(body.platform, "ios");
  assert.deepEqual(body.display, { width: 1080, height: 2400 });
  const html = await (await fetch(`${canvas.origin}/`)).text();
  assert.doesNotMatch(html, /id="display-preset"|id="display-menu"|data-preset="/, "the iOS page has a Size picker");
  assert.doesNotThrow(() => new Script(html.match(/<script>([\s\S]*)<\/script>/)[1]));
  // Only the simulator lookup at start ran.
  assert.deepEqual((await adbCalls(world)).map((args) => args.slice(0, 2).join(" ")), ["simctl list"]);
  await stopCanvas(canvas);
});

test("screen: iOS frames come from a simctl screenshot file, never from stdout, and the file is removed", async (t) => {
  const udid = "FAKE-SIMULATOR-UDID";
  const world = await makeWorld(t, { env: { FAKE_UDID: udid } });
  const xcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(xcrun, `#!${process.execPath}\n${FAKE_XCRUN}`);
  await chmod(xcrun, 0o755);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", udid, "--simctl", xcrun,
    "--transport", "screencap"]);
  // The stream answers only with its first frame, so a broken capture would hold fetch for minutes.
  let stream;
  try {
    stream = await Promise.race([readMultipart(canvas),
      sleep(10_000).then(() => { throw new Error("timed out waiting for an iOS frame"); })]);
    await waitFor(() => stream.frames.length >= 1, 10_000, "an iOS frame");
  } finally {
    stream?.close();
    await stopCanvas(canvas);
  }
  assert.deepEqual(stream.frames[0], Buffer.from(PNG_BASE64, "base64"));
  const shots = (await adbCalls(world)).filter((args) => args[0] === "simctl" && args[1] === "io");
  assert.ok(shots.length >= 1, "no screenshot ran");
  for (const args of shots) {
    // Xcode 27 writes "-" as a file named "-" in the working directory.
    assert.notEqual(args.at(-1), "-");
    assert.match(args.at(-1), /autonom-canvas-[^/]+\/frame-\d+\.png$/);
    assert.equal(existsSync(dirname(args.at(-1))), false, "the screenshot folder was left behind");
  }
  assert.equal(existsSync(join(ROOT, "-")), false, "a file named - was written");
});

test("screen: an iOS Canvas stopped during a screenshot leaves no screenshot folder behind", async (t) => {
  const udid = "FAKE-SIMULATOR-UDID";
  const world = await makeWorld(t, { env: { FAKE_UDID: udid, FAKE_SCREENSHOT_DELAY_MS: "1500" } });
  const temporary = join(world.directory, "tmp");
  await mkdir(temporary);
  world.env.TMPDIR = temporary;
  const xcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(xcrun, `#!${process.execPath}\n${FAKE_XCRUN}`);
  await chmod(xcrun, 0o755);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", udid, "--simctl", xcrun,
    "--transport", "screencap"]);
  // The stream answers with its first frame, which this slow screenshot holds back.
  const controller = new AbortController();
  fetch(`${canvas.origin}/stream.mjpeg`, { headers: { Authorization: `Bearer ${TOKEN}` }, signal: controller.signal })
    .catch(() => {});
  await waitFor(async () => (await adbCalls(world)).some((args) => args[1] === "io"), 10_000, "a screenshot");
  await stopCanvas(canvas);
  controller.abort();
  await sleep(2000);
  assert.deepEqual(readdirSync(temporary).filter((name) => name.startsWith("autonom-canvas-")), []);
});

test("display: a failed wm density names the command and the values in effect, sets no preset even once the values match it, is not journaled, and a later change and the restore at stop still work", async (t) => {
  const world = await makeWorld(t);
  const failing = join(world.directory, "density-fails");
  world.env.FAKE_WM_DENSITY_FAIL = failing;
  await writeFile(failing, "");
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile]);
  const failed = await postDisplay(canvas, "tablet");
  assert.deepEqual(failed, { status: 502, body: {
    error: "wm density 320 failed: Error: could not set the density; in effect: 2560x1600 @ 420" } });
  assert.deepEqual(await wmNow(world), { size: "2560x1600", density: null });
  // The size changed but not the density: an override that no preset stands for.
  assert.deepEqual(displayValues((await status(canvas)).display), [2560, 1600, 420, null]);
  // Even when the density then reaches Tablet's from outside, Tablet was never applied.
  await writeFile(world.wmState, JSON.stringify({ size: "2560x1600", density: 320 }));
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  assert.deepEqual(displayValues((await status(canvas)).display), [2560, 1600, 320, null]);
  assert.equal(canvas.child.exitCode, null);
  await rm(failing);
  assert.deepEqual(await postDisplay(canvas, "small"),
    { status: 200, body: { ok: true, display: { preset: "small", width: 720, height: 1280, density: 320 } } });
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  assert.deepEqual(wmChanges(await wmCalls(world)),
    ["size 2560x1600", "density 320", "size 720x1280", "density 320", "size reset", "density reset"]);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  // The change that failed is not journaled.
  assert.deepEqual(await displayRecords(world), ["agent small 720x1280@320", "system restore 1080x2400@420"]);
});

test("display: when wm cannot read the display a change is refused before any wm command, nothing is journaled, and the stop runs none", async (t) => {
  const world = await makeWorld(t);
  const failing = join(world.directory, "density-fails");
  world.env.FAKE_WM_DENSITY_FAIL = failing;
  await writeFile(failing, "read");
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile]);
  const unread = await status(canvas);
  assert.deepEqual(displayValues(unread.display), [1080, 2400, null, null]);
  assert.equal(unread.display.presets.length, 5);
  assert.equal(unread.last_error, "display: wm could not read the display: Error: could not read the density");
  assert.deepEqual(await postDisplay(canvas, "tablet"), { status: 502, body: {
    error: "wm could not read the display before changing it: Error: could not read the density" } });
  assert.deepEqual(wmChanges(await wmCalls(world)), []);
  // Once its failed reading is 2 s old, /status reads the display again.
  await rm(failing);
  await sleep(DISPLAY_READ_MAX_AGE_MS + 100);
  assert.deepEqual(displayValues((await status(canvas)).display), [1080, 2400, 420, "default"]);
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(wmChanges(await wmCalls(world)), [], "the stop changed a display this Canvas never changed");
  assert.doesNotMatch(exit.stdout, /Display restore/);
  assert.deepEqual(await journaled(world, "display"), []);
});

test("display: a restore whose wm density fails says so on stdout, still resets the size and is journaled", async (t) => {
  const world = await makeWorld(t);
  const failing = join(world.directory, "density-fails");
  world.env.FAKE_WM_DENSITY_FAIL = failing;
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  await writeFile(failing, "");
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.match(exit.stdout,
    /Display restore failed: wm density reset failed: Error: could not set the density; in effect: 1080x2400 @ 320\n/);
  assert.deepEqual(await wmNow(world), { size: null, density: 320 });
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2560x1600", "density 320", "size reset", "density reset"]);
  assert.deepEqual(await displayRecords(world), ["agent tablet 2560x1600@320", "system restore 1080x2400@320"]);
});

test("display: while Pixel Fold runs, Small then Tablet arrive: Small is answered superseded and runs no wm command, Tablet runs after Pixel Fold, and the display ends on Tablet", async (t) => {
  const { world, canvas, control } = await connectedControl(t, { env: { FAKE_WM_SET_MS: "300" } });
  await settledDisplay(control);
  const answers = displayAnswers(control);
  control.send({ t: "display", preset: "pixel-fold" });
  await waitFor(async () => (await wmCalls(world)).includes("size 2208x1840"), 5000, "Pixel Fold running");
  control.send({ t: "display", preset: "small" });
  control.send({ t: "display", preset: "tablet" });
  await waitFor(() => answers.length === 3, 10_000, "three answers");
  assert.deepEqual(answers, [
    supersededAnswer("small"),
    { t: "display", ok: true, display: { preset: "pixel-fold", width: 2208, height: 1840, density: 420 } },
    { t: "display", ok: true, display: { preset: "tablet", width: 2560, height: 1600, density: 320 } },
  ]);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2208x1840", "density 420", "size 2560x1600", "density 320"]);
  assert.deepEqual(await wmNow(world), { size: "2560x1600", density: 320 });
  assert.equal((await status(canvas)).display.preset, "tablet");
  assert.deepEqual(await displayRecords(world), ["human pixel-fold 2208x1840@420", "human tablet 2560x1600@320"]);
  await stopCanvas(canvas);
});

waitingTest("display: a newer change goes to the end of the queue: the waiting change it supersedes after an HTTP tap was queued runs nothing, and the newer one runs after that tap", async (t) => {
  const { world, canvas, control } = await connectedControl(t,
    { env: { FAKE_WM_SET_MS: "1000", FAKE_BRIDGE_INPUT_MS: "600" } });
  await settledDisplay(control);
  const answers = displayAnswers(control);
  control.send({ t: "display", preset: "pixel-fold" });
  await waitFor(async () => (await wmCalls(world)).includes("size 2208x1840"), 5000, "Pixel Fold running");
  control.send({ t: "display", preset: "small" });
  await sleep(250);
  const tap = rawHttp(canvas.port, { method: "POST", path: "/tap", headers: agentHeaders(), body: JSON.stringify({ x: 10, y: 10 }) });
  await sleep(250);
  control.send({ t: "display", preset: "tablet" });
  // Small was waiting with the tap queued after it: it is superseded at once.
  await waitFor(() => answers.length === 1, 3000, "the superseded answer");
  assert.deepEqual(answers[0], supersededAnswer("small"));
  // The tap runs after Pixel Fold and before Tablet.
  await waitFor(async () => (await bridgeCalls(world, "tap")).length === 1, 10_000, "the tap at the device");
  assert.deepEqual(wmChanges(wmCallsNow(world)), ["size 2208x1840", "density 420"]);
  assert.equal((await tap).status, 200);
  await waitFor(() => answers.length === 3, 10_000, "every answer");
  assert.deepEqual(answers.slice(1).map((answer) => answer.display?.preset), ["pixel-fold", "tablet"]);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2208x1840", "density 420", "size 2560x1600", "density 320"]);
  await stopCanvas(canvas);
});

test("display: of 12 POST /display sent at once one runs, one waits, and each of the others is answered 409 superseded without any wm command", async (t) => {
  const { world, canvas } = await streamingCanvas(t, { env: { FAKE_WM_SET_MS: "300" } });
  const asked = Array.from({ length: 12 }, (_, i) => PRESETS[i % 4].id);
  const answers = await Promise.all(asked.map((preset) => postDisplay(canvas, preset)));
  const applied = answers.filter((answer) => answer.status === 200);
  const superseded = answers.filter((answer) => answer.status !== 200);
  assert.equal(applied.length, 2, JSON.stringify(answers));
  for (const answer of superseded) {
    assert.equal(answer.status, 409);
    assert.equal(answer.body.superseded, true);
    assert.match(answer.body.error, /^The display change to (small|pixel-11|pixel-fold|tablet) was superseded by a later one$/);
  }
  // The commands of exactly the two applied changes, one change after the other.
  const commandsOf = (preset) => [`size ${preset.width}x${preset.height}`, `density ${preset.density}`];
  const changes = wmChanges(await wmCalls(world));
  assert.equal(changes.length, 4, JSON.stringify(changes));
  const ran = [changes.slice(0, 2), changes.slice(2)].map((pair) => pair.join(", ")).sort();
  const answered = applied.map(({ body }) =>
    commandsOf(PRESETS.find((preset) => preset.id === body.display.preset)).join(", ")).sort();
  assert.deepEqual(ran, answered);
  const last = PRESETS.find((preset) => commandsOf(preset).join(", ") === changes.slice(2).join(", "));
  assert.deepEqual(await wmNow(world), {
    size: `${last.width}x${last.height}`, density: last.density === 420 ? null : last.density });
  await stopCanvas(canvas);
});

test("display: input flood: 100k display messages on one connection keep Canvas memory bounded, each is answered applied or superseded, the last one asked for is applied last, and the restore at stop still runs", async (t) => {
  const { world, canvas, control } = await floodSetup(t);
  await settledDisplay(control);
  const count = 100_000;
  const total = FLOOD_WARM_UP + count;
  const others = [];
  control.on("message", (message) => {
    if (message.json?.t === "error" && !message.json.superseded) others.push(message.json.message);
  });
  // The last message, number total - 1, asks for Tablet.
  const message = (i) => JSON.stringify({ t: "display", preset: i % 2 ? "tablet" : "small" });
  await warmUp(control, message);
  // Each applied change runs adb several times, so on a loaded host /status, which reads
  // the display through adb too, may answer slowly during this authenticated flood: that
  // slowness is accepted, as long as it answers and memory stays bounded.
  const watched = await watchCanvas(canvas, async () => {
    await control.flood(count, (i) => message(FLOOD_WARM_UP + i));
    await allHandled(control);
  }, { statusTimeoutMs: DISPLAY_FLOOD_STATUS_MS });
  assertBounded(t, watched, `${count} display messages`);
  await waitFor(() => control.count("display") + control.count("error") === total, 20_000,
    "an answer to every display message");
  assert.deepEqual(others, [], "a display message was refused for another reason than a later one");
  const applied = control.count("display");
  t.diagnostic(`${applied} of ${total} display changes were applied, the others superseded`);
  assert.ok(applied >= 1 && applied <= 200, `${applied} display changes were applied`);
  const changes = wmChanges(await wmCalls(world));
  assert.equal(changes.length, 2 * applied);
  assert.deepEqual(changes.slice(-2), ["size 2560x1600", "density 320"]);
  assert.deepEqual(await wmNow(world), { size: "2560x1600", density: 320 });
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
});

test("display: a waiting change whose connection closes runs no wm command, and the change running completes", async (t) => {
  const { world, canvas, control } = await connectedControl(t, { env: { FAKE_WM_SET_MS: "300" } });
  await settledDisplay(control);
  control.send({ t: "display", preset: "small" });
  await waitFor(async () => (await wmCalls(world)).includes("size 720x1280"), 5000, "the first change running");
  // The close follows the request on the same socket, so the Canvas has it waiting by then.
  control.send({ t: "display", preset: "tablet" });
  control.close();
  await control.closed;
  await waitFor(async () => (await wmNow(world)).density === 320, 5000, "the first change done");
  await sleep(1000);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 720x1280", "density 320"]);
  assert.deepEqual(await wmNow(world), { size: "720x1280", density: 320 });
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 720x1280", "density 320", "size reset", "density reset"]);
});

test("display: SIGTERM during a 4 s swipe restores the display within the 3 s shutdown budget, writes the restore to the journal through a bridge of its own while the busy one holds its lines, and drops the taps and the display change queued behind the swipe", async (t) => {
  // The bridge handles one line at a time, as the real one does.
  const world = await makeWorld(t, { env: { FAKE_BRIDGE_INPUT_MS: "4000", FAKE_BRIDGE_IN_ORDER: "1" } });
  const canvas = await startCanvas(world, ["--transport", "screencap"]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  await waitFor(async () => (await displayRecords(world)).length === 1, 5000, "the change's record");
  // A swipe the device answers after 4 s, then taps and a display change queued behind it.
  const swipe = JSON.stringify({ x1: 10, y1: 10, x2: 10, y2: 500, duration: 4000 });
  const swiping = rawHttp(canvas.port, { method: "POST", path: "/swipe", headers: agentHeaders(), body: swipe })
    .catch(() => null);
  await waitFor(async () => (await bridgeCalls(world, "swipe")).length === 1, 5000, "the swipe at the device");
  const tap = JSON.stringify({ x: 10, y: 10 });
  const queued = [
    rawHttp(canvas.port, { method: "POST", path: "/tap", headers: agentHeaders(), body: tap }),
    rawHttp(canvas.port, { method: "POST", path: "/tap", headers: agentHeaders(), body: tap }),
    postDisplay(canvas, "pixel-fold"),
  ].map((request) => request.catch(() => null));
  await sleep(300);
  const started = Date.now();
  const exit = await stopCanvas(canvas);
  const elapsed = Date.now() - started;
  t.diagnostic(`the Canvas stopped ${elapsed} ms after SIGTERM`);
  assert.equal(exit.code, 0);
  assert.ok(elapsed < 3000, `the Canvas stopped ${elapsed} ms after SIGTERM`);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2560x1600", "density 320", "size reset", "density reset"]);
  assert.equal((await bridgeCalls(world, "tap")).length, 0, "a tap queued behind the swipe ran");
  // The shared bridge never got to the restore record behind the swipe; the other one wrote it.
  assert.deepEqual(await displayRecords(world), ["agent tablet 2560x1600@320", "system restore 1080x2400@420"]);
  await Promise.all([swiping, ...queued]);
});

test("display: a change still running at SIGTERM stops before its density command and the restore puts the size back", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_WM_SET_MS: "300" } });
  const canvas = await startCanvas(world, ["--transport", "screencap"]);
  const answer = postDisplay(canvas, "tablet").catch(() => null);
  await waitFor(async () => (await wmCalls(world)).includes("size 2560x1600"), 5000, "the size command running");
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 2560x1600", "size reset", "density reset"]);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.doesNotMatch(exit.stdout, /Display: tablet/);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  await answer;
});

test("display: a held finger and key are lifted on the device before the first wm command that changes the display and journaled before the change, and the lifted finger's moves are refused", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  await settledDisplay(control);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.pointersDown.size === 1 && device.keysDown.size === 1, 3000, "the finger and key down");
  // How many display-changing wm commands adb had run when the device read each up; the
  // readings of the renewal that runs while a page is connected may come at any time.
  const wmAtUp = [];
  device.on("control", (message) => {
    if (message.action === 1) wmAtUp.push([message.type, wmChanges(wmCallsNow(world)).length]);
  });
  assert.equal((await wsDisplay(control, "tablet")).ok, true);
  assert.deepEqual(wmAtUp.slice(0, 2), [["touch", 0], ["key", 0]]);
  assert.deepEqual([device.pointersDown.size, device.keysDown.size], [0, 0]);
  const refused = control.reply((message) => message.t === "error");
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  assert.match((await refused).message, /^Pointer 1 was lifted because the display size changed; put it down again$/);
  // Its up finds it lifted already; a new finger works.
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.6 });
  const downs = device.touches().filter((message) => message.action === 0).length;
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 2, x: 0.5, y: 0.5 });
  await waitFor(() => device.touches().filter((message) => message.action === 0).length === downs + 1, 3000, "a new finger");
  assert.equal(control.json("error").length, 1);
  // The lifted gesture and key are journaled before the change, as for a takeover.
  const records = await waitFor(async () => {
    const found = await journaled(world);
    return found.length >= 4 && found;
  }, 5000, "the journal records");
  assert.deepEqual(recordSummaries(records), ["human gesture", "human key 59", "human display", "human gesture"]);
  await stopCanvas(canvas);
});

test("display: a finger lifted while a paste waits for its ack still reaches the device before the first wm command that changes the display", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  await settledDisplay(control);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.pointersDown.size === 1 && device.count("set-clipboard") === 1, 3000,
    "the finger and the paste");
  const answer = wsDisplay(control, "tablet");
  await sleep(300);
  assert.deepEqual(wmChanges(wmCallsNow(world)), [], "wm changed the display while the finger's up waited behind the paste");
  assert.equal(device.pointersDown.size, 1);
  const wmAtUp = [];
  device.on("control", (message) => {
    if (message.type === "touch" && message.action === 1) wmAtUp.push(wmChanges(wmCallsNow(world)).length);
  });
  device.ack(clipboardWrites(device)[0].sequence);
  assert.equal((await answer).ok, true);
  assert.equal(wmAtUp[0], 0, "the display changed before the device read the finger's up");
  assert.equal(device.pointersDown.size, 0);
  assert.deepEqual(await wmNow(world), { size: "2560x1600", density: 320 });
  await stopCanvas(canvas);
});

test("display: on screenrecord each change restarts the shared capture once, the transport stays screenrecord and the journal names it", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_SCREENRECORD: "h264", FAKE_SCREENRECORD_HOLD: "1" } });
  const ffmpeg = join(world.directory, "fake-ffmpeg.mjs");
  await writeFile(ffmpeg, `#!${process.execPath}\n${FAKE_FFMPEG}`);
  await chmod(ffmpeg, 0o755);
  const canvas = await startCanvas(world, ["--transport", "screenrecord", "--ffmpeg", ffmpeg]);
  // Streaming captures, not the start-up probe, which has a time limit.
  const captures = async () => (await adbCalls(world))
    .filter((args) => args.includes("screenrecord") && !args.includes("--time-limit")).length;
  const stream = await readMultipart(canvas);
  await waitFor(() => stream.frames.length > 0, 5000, "frames from the first capture");
  assert.equal(await captures(), 1);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  await waitFor(async () => (await captures()) === 2, 5000, "the capture restarted");
  const seen = stream.frames.length;
  await waitFor(() => stream.frames.length > seen, 5000, "frames from the new capture");
  assert.equal((await postDisplay(canvas, "default")).status, 200);
  await waitFor(async () => (await captures()) === 3, 5000, "the capture restarted again");
  await sleep(800);
  assert.equal(await captures(), 3, "a change restarted the capture more than once");
  const body = await status(canvas);
  assert.equal(body.transport, "screenrecord");
  assert.doesNotMatch(String(body.last_error ?? ""), /accelerated stream failed/);
  assert.deepEqual(await displayRecords(world), ["agent tablet 2560x1600@320", "agent default 1080x2400@420"]);
  assert.ok((await journaled(world, "display")).every(({ payload }) => payload.transport === "screenrecord"));
  stream.close();
  await stopCanvas(canvas);
});

test("display: on screencap the frames after a change to Tablet have its size", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "screencap"]);
  const stream = await readMultipart(canvas);
  await waitFor(() => stream.frames.length > 0, 5000, "a first frame");
  assert.deepEqual(pngSize(stream.frames[0]), [1, 1]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  const from = stream.frames.length;
  const tablet = await waitFor(() => stream.frames.slice(from).find((frame) => pngSize(frame)[0] !== 1), 5000,
    "a frame with the new size");
  assert.deepEqual(pngSize(tablet), [2560, 1600]);
  assert.equal((await status(canvas)).transport, "screencap");
  stream.close();
  await stopCanvas(canvas);
});

test("display: a second multipart page that still has the old size taps the centre right after an agent applies Tablet and the device gets the Tablet centre; agent input without dw and dh is sent as given, and a malformed dw or dh is refused with nothing sent", async (t) => {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--transport", "screencap"]);
  const stream = await readMultipart(canvas);
  await waitFor(() => stream.frames.length > 0, 5000, "a first frame");
  const auth = await login(canvas);
  const page = (path, body) => rawHttp(canvas.port, { method: "POST", path, body: JSON.stringify(body), headers: {
    "Content-Type": "application/json", Cookie: auth.cookie, "X-Autonom-CSRF": auth.csrf, "X-Autonom-Origin": "human" } });
  const agent = (path, body) => rawHttp(canvas.port, { method: "POST", path, body: JSON.stringify(body), headers: agentHeaders() });
  // The size the page polled last: the device default.
  const known = (await status(canvas)).display;
  assert.deepEqual([known.width, known.height], [1080, 2400]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  // Before its next poll the page taps the centre it shows and drags down the middle.
  assert.equal((await page("/tap", { x: 540, y: 1200, dw: 1080, dh: 2400 })).status, 200);
  assert.equal((await page("/swipe", { x1: 540, y1: 1800, x2: 540, y2: 600, duration: 220, dw: 1080, dh: 2400 })).status, 200);
  // A page that knows the size in effect, and an agent that names no size, are sent as given.
  assert.equal((await page("/tap", { x: 100, y: 200, dw: 2560, dh: 1600 })).status, 200);
  assert.equal((await agent("/tap", { x: 540, y: 1200 })).status, 200);
  assert.equal((await agent("/swipe", { x1: 10, y1: 20, x2: 30, y2: 40, duration: 100 })).status, 200);
  const sent = async (op) => (await bridgeCalls(world, op)).map(({ origin, payload }) => [origin, payload]);
  assert.deepEqual(await sent("tap"), [["human", { x: 1280, y: 800 }], ["human", { x: 100, y: 200 }],
    ["agent", { x: 540, y: 1200 }]]);
  assert.deepEqual(await sent("swipe"), [["human", { x1: 1280, y1: 1200, x2: 1280, y2: 400, duration: 220 }],
    ["agent", { x1: 10, y1: 20, x2: 30, y2: 40, duration: 100 }]]);
  // Both or neither, each an integer from 1 to 10000; anything else is 400 and reaches no device.
  for (const size of [{ dw: 1080 }, { dh: 2400 }, { dw: 0, dh: 2400 }, { dw: -1080, dh: 2400 }, { dw: 1080.5, dh: 2400 },
    { dw: "1080", dh: 2400 }, { dw: 1080, dh: null }, { dw: 10_001, dh: 2400 }, { dw: 1080, dh: 1e9 }]) {
    for (const [path, body] of [["/tap", { x: 1, y: 1 }], ["/swipe", { x1: 1, y1: 1, x2: 2, y2: 2 }]]) {
      for (const send of [agent, page]) {
        const answer = await send(path, { ...body, ...size });
        assert.equal(answer.status, 400, `${path} ${JSON.stringify(size)}`);
        assert.equal(JSON.parse(answer.body).error, "dw and dh must both be integers from 1 to 10000");
      }
    }
  }
  assert.equal((await sent("tap")).length, 3);
  assert.equal((await sent("swipe")).length, 2);
  stream.close();
  await stopCanvas(canvas);
});

test("display: the multipart page sends the display size its taps, swipes and wheel swipes were computed for, none before it knows one, and moves a swipe's start to a size that changed during the drag", async (t) => {
  const { sandbox, run } = await loadPage(t);
  const posts = [];
  sandbox.performance = performance;
  sandbox.fetch = async (path, init) => {
    posts.push([path, JSON.parse(init.body)]);
    return { ok: true, json: async () => ({ ok: true }) };
  };
  // A 1080x2400 picture shown at half size.
  run(`view.transport="screencap";controlSocket=null;image.naturalWidth=1080;image.naturalHeight=2400;
    image.getBoundingClientRect=()=>({left:0,top:0,width:540,height:1200});image.setPointerCapture=()=>{};`);
  const drag = async (from, to, between = "") => {
    posts.length = 0;
    run(`legacyPointerDown({clientX:${from[0]},clientY:${from[1]},pointerId:1});${between}`);
    await run(`legacyPointerUp({clientX:${to[0]},clientY:${to[1]},pointerId:1})`);
    return posts.map(([path, body]) => [path, path === "/swipe" ? { ...body, duration: "any" } : body]);
  };
  assert.deepEqual(await drag([270, 600], [270, 600]), [["/tap", { x: 540, y: 1200 }]]);
  run("logicalDisplay={width:1080,height:2400}");
  assert.deepEqual(await drag([270, 600], [270, 600]), [["/tap", { x: 540, y: 1200, dw: 1080, dh: 2400 }]]);
  assert.deepEqual(await drag([270, 900], [270, 300]),
    [["/swipe", { x1: 540, y1: 1800, x2: 540, y2: 600, duration: "any", dw: 1080, dh: 2400 }]]);
  // The page polls a new size during the drag: the start goes to that size with the end.
  assert.deepEqual(await drag([270, 900], [270, 300], "logicalDisplay={width:2160,height:4800};"),
    [["/swipe", { x1: 1080, y1: 3600, x2: 1080, y2: 1200, duration: "any", dw: 2160, dh: 4800 }]]);
  posts.length = 0;
  await run("logicalDisplay={width:1080,height:2400};onWheel({preventDefault(){},deltaX:0,deltaY:100,deltaMode:0})");
  assert.deepEqual(posts, [["/swipe", { x1: 540, y1: 1320, x2: 540, y2: 600, duration: 220, dw: 1080, dh: 2400 }]]);
});

test("display: SIGTERM after Pixel Fold resets size and density before the process exits, and the restore is journaled as the system's", async (t) => {
  const { world, canvas } = await streamingCanvas(t);
  assert.equal((await postDisplay(canvas, "pixel-fold")).body.display.preset, "pixel-fold");
  assert.deepEqual(await wmNow(world), { size: "2208x1840", density: null });
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual((await wmCalls(world)).slice(-4), ["size reset", "density reset", "size", "density"]);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.deepEqual(await displayRecords(world), ["agent pixel-fold 2208x1840@420", "system restore 1080x2400@420"]);
});

test("display: a size and density override the device had before the Canvas comes back at stop", async (t) => {
  const { world, canvas } = await streamingCanvas(t, { env: { FAKE_WM_OVERRIDE: "1600x2560@300" } });
  // An override this Canvas did not make is no preset.
  assert.deepEqual(displayValues((await status(canvas)).display), [1600, 2560, 300, null]);
  assert.equal((await postDisplay(canvas, "small")).status, 200);
  assert.deepEqual(await wmNow(world), { size: "720x1280", density: 320 });
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(await wmNow(world), { size: "1600x2560", density: 300 });
  assert.deepEqual(wmChanges(await wmCalls(world)), ["size 720x1280", "density 320", "size 1600x2560", "density 300"]);
  assert.match(exit.stdout, /Display restored: 1600x2560 @ 300\n/);
});

test("display: a Canvas that changed nothing runs no wm command at stop and journals nothing", async (t) => {
  const { world, canvas } = await streamingCanvas(t);
  await status(canvas);
  // A refused request is no change either.
  assert.equal((await postDisplay(canvas, "huge")).status, 400);
  const before = await wmCalls(world);
  assert.deepEqual(before, ["size", "density"]);
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.deepEqual(await wmCalls(world), before);
  assert.doesNotMatch(exit.stdout, /Display restore/);
  assert.deepEqual(await journaled(world, "display"), []);
});

/** The Python that runs the real bridge, which writes no bytecode next to it. */
async function bridgePython() {
  return (await execFileAsync("python3", ["-c", "import sys; print(sys.executable)"],
    { env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } })).stdout.trim();
}

/** A current session on the Canvas target; returns its artifacts directory. */
async function displaySession(world) {
  const sessions = join(world.env.AUTONOM_HOME, "sessions");
  const artifacts = join(sessions, "s_canvas_display");
  await mkdir(artifacts, { recursive: true });
  await writeFile(join(sessions, "current.json"), JSON.stringify({
    schema_version: 2, session_id: "s_canvas_display", platform: "android", target_id: SERIAL, serial: SERIAL,
    artifacts_dir: artifacts,
  }));
  return artifacts;
}

/** The session's journal entries, [] before the first. */
async function journalEntries(artifacts) {
  const text = await readFile(join(artifacts, "journal.ndjson"), "utf8").catch(() => "");
  return text.split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

/** Each entry's preset, width, height, density and transport, from its detail file. */
async function displayDetails(artifacts, entries) {
  return await Promise.all(entries.map(async (entry) => {
    const detail = JSON.parse(await readFile(join(artifacts, entry.result.detail), "utf8"));
    return [detail.preset, detail.width, detail.height, detail.density, detail.transport];
  }));
}

test("display: journal: each applied change is one ui display entry and the restore at stop one system entry, from the real bridge", async (t) => {
  // The real bridge, with its validation, journaling into a current session on the Canvas target.
  const python = await bridgePython();
  const world = await makeWorld(t, { env: { PYTHONDONTWRITEBYTECODE: "1" } });
  const artifacts = await displaySession(world);
  const canvas = await startCanvas(world, ["--transport", "scrcpy", "--scrcpy-server", world.serverFile,
    "--python", python, "--bridge", join(ROOT, "scripts/autonom_canvas_bridge.py")]);
  const auth = await login(canvas);
  const control = await pageSocket(canvas, auth, "/ws/control");
  await control.next((message) => message.json?.t === "state");
  assert.equal((await wsDisplay(control, "small")).ok, true);
  assert.equal((await wsDisplay(control, "tablet")).ok, true);
  // Refused requests leave no entry.
  assert.equal((await postDisplay(canvas, "huge")).status, 400);
  assert.equal((await postDisplay(canvas, "tablet", "replay")).status, 200);
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  const entries = await journalEntries(artifacts);
  assert.deepEqual(entries.map((entry) => [entry.verb, entry.origin, entry.ok]), [
    ["ui display", "human", true], ["ui display", "human", true], ["ui display", "replay", true],
    ["ui display", "system", true]]);
  assert.deepEqual(await displayDetails(artifacts, entries), [
    ["small", 720, 1280, 320, "scrcpy"], ["tablet", 2560, 1600, 320, "scrcpy"], ["tablet", 2560, 1600, 320, "scrcpy"],
    ["restore", 1080, 2400, 420, "scrcpy"]]);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
});

test("display: journal: the restore at stop is one system entry while the real bridge is still busy with a 4 s swipe, and the stop keeps within its 3 s budget", async (t) => {
  // The real bridge handles one line at a time; the device holds its swipe for 4 s.
  const python = await bridgePython();
  const world = await makeWorld(t, { env: { PYTHONDONTWRITEBYTECODE: "1", FAKE_ADB_INPUT_MS: "4000" } });
  const artifacts = await displaySession(world);
  const canvas = await startCanvas(world, ["--transport", "screencap",
    "--python", python, "--bridge", join(ROOT, "scripts/autonom_canvas_bridge.py")]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  await waitFor(async () => (await journalEntries(artifacts)).length === 1, 5000, "the change's entry");
  const swipe = JSON.stringify({ x1: 10, y1: 10, x2: 10, y2: 500, duration: 4000 });
  const swiping = rawHttp(canvas.port, { method: "POST", path: "/swipe", headers: agentHeaders(), body: swipe })
    .catch(() => null);
  await waitFor(async () => (await adbCalls(world)).some((args) => args.includes("input") && args.includes("swipe")),
    5000, "the swipe at the device");
  await sleep(250);
  const started = Date.now();
  const exit = await stopCanvas(canvas);
  const elapsed = Date.now() - started;
  t.diagnostic(`the Canvas stopped ${elapsed} ms after SIGTERM`);
  assert.equal(exit.code, 0);
  assert.ok(elapsed < 3000, `the Canvas stopped ${elapsed} ms after SIGTERM`);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  const entries = await journalEntries(artifacts);
  assert.deepEqual(entries.map((entry) => [entry.verb, entry.origin, entry.ok]),
    [["ui display", "agent", true], ["ui display", "system", true]]);
  assert.deepEqual(await displayDetails(artifacts, entries),
    [["tablet", 2560, 1600, 320, "screencap"], ["restore", 1080, 2400, 420, "screencap"]]);
  await swiping;
});

test("display: journal: when no second bridge can start for the restore record behind a busy one, the stop still restores the display and exits 0 within its budget", async (t) => {
  const world = await makeWorld(t, { env: { FAKE_BRIDGE_INPUT_MS: "4000", FAKE_BRIDGE_IN_ORDER: "1" } });
  // The bridge's interpreter is gone by the stop, so only the bridge already running works.
  const python = join(world.directory, "bridge-python");
  await writeFile(python, `#!/bin/sh\nexec "${process.execPath}" "$@"\n`);
  await chmod(python, 0o755);
  const canvas = await startCanvas(world, ["--transport", "screencap", "--python", python]);
  assert.equal((await postDisplay(canvas, "tablet")).status, 200);
  await waitFor(async () => (await displayRecords(world)).length === 1, 5000, "the change's record");
  const swipe = JSON.stringify({ x1: 10, y1: 10, x2: 10, y2: 500, duration: 4000 });
  const swiping = rawHttp(canvas.port, { method: "POST", path: "/swipe", headers: agentHeaders(), body: swipe })
    .catch(() => null);
  await waitFor(async () => (await bridgeCalls(world, "swipe")).length === 1, 5000, "the swipe at the device");
  await rm(python);
  const started = Date.now();
  const exit = await stopCanvas(canvas);
  const elapsed = Date.now() - started;
  assert.equal(exit.code, 0, exit.stderr);
  assert.ok(elapsed < 3000, `the Canvas stopped ${elapsed} ms after SIGTERM`);
  assert.match(exit.stdout, /Display restored: 1080x2400 @ 420\n/);
  assert.deepEqual(await wmNow(world), { size: null, density: null });
  assert.deepEqual(await displayRecords(world), ["agent tablet 2560x1600@320"]);
  await swiping;
});

test("display: the Android page shows a Size picker with the five presets, sends a choice over the control socket or POST /display, and disables it without control", async (t) => {
  const { sandbox, sent, run, html } = await loadPage(t);
  assert.match(html, /<button type="button" class="popup" id="display-preset" aria-haspopup="listbox"[^>]* disabled>/,
    "the Android page has no Size picker");
  assert.deepEqual(presetOptions(html), [["small", "Small phone", "720 × 1280"], ["pixel-11", "Pixel 11", "1080 × 2424"],
    ["pixel-fold", "Pixel Fold (open)", "2208 × 1840"], ["tablet", "Tablet", "2560 × 1600"], ["default", "Device default", ""]]);
  const picker = () => JSON.parse(run("JSON.stringify([sizePicker.value,sizePicker.disabled])"));

  // Shown once the page knows the status; the preset in effect is selected.
  run("view.status={platform:\"android\"};view.preset=\"default\";syncPicker()");
  assert.deepEqual(picker(), ["default", false]);
  // On scrcpy a choice goes over the control socket; the picker waits for the answer.
  run("chooseDisplay(\"tablet\")");
  assert.deepEqual(sent.at(-1), { t: "display", preset: "tablet" });
  assert.equal(picker()[1], true);
  run("onControlMessage({t:\"display\",ok:true,display:{preset:\"tablet\",width:2560,height:1600,density:320}})");
  assert.deepEqual(picker(), ["tablet", false]);
  assert.deepEqual(JSON.parse(run("JSON.stringify(logicalDisplay)")), { width: 2560, height: 1600 });
  assert.deepEqual(JSON.parse(run("JSON.stringify(snapshot().display)")),
    { preset: "tablet", density: 320, picker: "tablet", pickerDisabled: false });
  // Another page's change arrives in a state message.
  const state = (fields) => run(`applyState(${JSON.stringify({ t: "state", owner: "shared", paused: false,
    session: "streaming", clients: null, preset: "small", density: 320, ...fields })})`);
  state({});
  assert.deepEqual(picker(), ["small", false]);
  // Disabled while another origin holds control or input is paused.
  state({ owner: "agent" });
  assert.equal(picker()[1], true);
  state({ paused: true });
  assert.equal(picker()[1], true);
  state({ owner: "human" });
  assert.equal(picker()[1], false);
  // A refused or superseded choice gives the picker back with the preset in effect.
  run("sizePicker.value=\"pixel-fold\";chooseDisplay(\"pixel-fold\")");
  run("onControlMessage({t:\"error\",for:\"display\",message:\"Canvas control is owned by agent\"})");
  assert.deepEqual(picker(), ["small", false]);
  assert.equal(run("view.note"), "Canvas control is owned by agent");
  run("sizePicker.value=\"tablet\";chooseDisplay(\"tablet\")");
  run(`onControlMessage(${JSON.stringify(supersededAnswer("tablet"))})`);
  assert.deepEqual(picker(), ["small", false]);
  // Without scrcpy it goes to POST /display as the human page, and the answer is shown.
  const posts = [];
  sandbox.fetch = async (path, init) => {
    posts.push([path, JSON.parse(init.body), init.headers["X-Autonom-Origin"]]);
    return { ok: true, json: async () => ({ ok: true, display: { preset: "pixel-fold", width: 2208, height: 1840, density: 420 } }) };
  };
  run("view.transport=\"screencap\";controlSocket=null");
  await run("chooseDisplay(\"pixel-fold\")");
  assert.deepEqual(posts, [["/display", { preset: "pixel-fold" }, "human"]]);
  assert.deepEqual(picker(), ["pixel-fold", false]);
  assert.equal(sent.filter((message) => message.t === "display").length, 3);
});

/** The Size menu's options as served: [preset id, label, size shown]. */
function presetOptions(html) {
  return [...html.matchAll(/<button[^>]* role="option" data-preset="([^"]+)"[^>]*>[\s\S]*?<span>([^<]*)<\/span><small[^>]*>([^<]*)<\/small><\/button>/g)]
    .map(([, id, label, size]) => [id, label, size]);
}

/** Every button of a served page: its attributes, its text, and the name assistive technology reads. */
function pageButtons(html) {
  const markup = html.replace(/<script>[\s\S]*?<\/script>/, "");
  return [...markup.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].map(([, rest, inner]) => {
    const attributes = Object.fromEntries([...rest.matchAll(/([^\s="'/]+)(?:="([^"]*)")?/g)]
      .map(([, name, value = ""]) => [name, value]));
    const text = inner.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
    return { attributes, text, name: attributes["aria-label"] ?? text };
  });
}

// Each control of the former page, by the attributes its handlers read, with its name now.
const FORMER_CONTROLS = [
  ["Back", { "data-key": "KEYCODE_BACK", "data-system": "back" }],
  ["Home", { "data-key": "KEYCODE_HOME", "data-system": "home" }],
  ["Recent apps", { "data-key": "KEYCODE_APP_SWITCH", "data-system": "app-switch" }],
  ["Up", { "data-key": "KEYCODE_DPAD_UP", "data-code": "19" }],
  ["Enter", { "data-key": "KEYCODE_ENTER", "data-code": "66" }],
  ["Down", { "data-key": "KEYCODE_DPAD_DOWN", "data-code": "20" }],
  ["Left", { "data-key": "KEYCODE_DPAD_LEFT", "data-code": "21" }],
  ["Delete", { "data-key": "KEYCODE_DEL", "data-code": "67" }],
  ["Right", { "data-key": "KEYCODE_DPAD_RIGHT", "data-code": "22" }],
  ["Wake screen", { "data-key": "KEYCODE_WAKEUP", "data-system": "wake" }],
  ["Power", { "data-key": "KEYCODE_POWER", "data-system": "power" }],
  ["Rotate", { "data-system": "rotate", "data-scrcpy": "" }],
  ["Volume down", { "data-system": "volume-down", "data-scrcpy": "" }],
  ["Volume up", { "data-system": "volume-up", "data-scrcpy": "" }],
  ["Notifications", { "data-system": "notifications", "data-scrcpy": "" }],
  ["Quick settings", { "data-system": "quick-settings", "data-scrcpy": "" }],
  ["Collapse panels", { "data-system": "collapse", "data-scrcpy": "" }],
  ["Copy device clipboard", { id: "clipboard", "data-scrcpy": "" }],
  ["Reconnect stream", { id: "refresh" }],
  ["Send", { id: "sendText" }],
];
const PAGE_IDS = ["video", "screen", "status", "text", "clipboard", "device", "refresh", "sendText"];

test("page: the Android page keeps its element ids, window.autonomCanvas and every former control with the attributes its handlers read, names every button, and marks the scrcpy-only ones", async (t) => {
  const { html } = await loadPage(t);
  for (const id of [...PAGE_IDS, "display-preset", "display-menu", "control-take", "control-pause", "inspector-toggle"]) {
    assert.equal(html.split(`id="${id}"`).length - 1, 1, `id ${id}`);
  }
  assert.match(html, /window\.autonomCanvas=Object\.freeze\(\{stats:snapshot,send:sendControl\}\)/);
  assert.match(html, /<canvas id="video" class="surface" tabindex="0" aria-label="Android device screen" hidden>/);
  assert.match(html, /<img id="screen" class="surface" tabindex="0" alt="Android device screen">/);
  assert.match(html, /<input id="text" [^>]*aria-label="Text to type">/);
  const buttons = pageButtons(html);
  for (const [name, attributes] of FORMER_CONTROLS) {
    const found = buttons.filter((button) => Object.entries(attributes).every(([key, value]) => button.attributes[key] === value));
    assert.equal(found.length, 1, name);
    assert.equal(found[0].name, name);
    assert.deepEqual(Object.keys(found[0].attributes).filter((key) => key.startsWith("data-")).sort(),
      Object.keys(attributes).filter((key) => key.startsWith("data-")).sort(), `${name} keeps exactly its data attributes`);
  }
  // Every button has a name; one without text is named by aria-label and shows it as its tooltip.
  for (const button of buttons) {
    assert.ok(button.name, JSON.stringify(button.attributes));
    assert.equal(button.attributes.type, "button", button.name);
    if (!button.text) assert.equal(button.attributes.title, button.attributes["aria-label"], button.name);
  }
  assert.deepEqual(buttons.filter((button) => "data-scrcpy" in button.attributes).map((button) => button.name),
    ["Rotate", "Volume down", "Volume up", "Notifications", "Quick settings", "Collapse panels", "Copy device clipboard"]);
  // The toolbar: wordmark, target with its live dot, the size menu with dimensions, control chip, inspector toggle.
  assert.match(html, /<b>Autonom<\/b>/);
  assert.match(html, /<i class="dot" id="live-dot" aria-hidden="true"><\/i><strong>fake-device-1<\/strong><span id="target-detail">Android · connecting<\/span>/);
  assert.match(html, /<span class="dims" id="display-dims"><\/span>/);
  assert.match(html, /<span class="chip" title="Who controls the device">[\s\S]*?<span id="control-chip">Shared<\/span>/);
  assert.match(html, /id="inspector-toggle" aria-pressed="true" aria-controls="inspector" title="Inspector" aria-label="Inspector"/);
  assert.match(html, /<div role="listbox" id="display-list" aria-label="Display size">/);
  assert.equal(presetOptions(html).length, 5);
  // The pill under the device: Back, Home, Recent apps | Rotate, Volume down, Volume up, Power.
  const dock = html.match(/<nav class="dock" aria-label="Device buttons">([\s\S]*?)<\/nav>/);
  assert.ok(dock, "the Android page has no pill of device buttons");
  assert.deepEqual(pageButtons(dock[1]).map((button) => button.name),
    ["Back", "Home", "Recent apps", "Rotate", "Volume down", "Volume up", "Power"]);
  // The inspector sections, in order.
  assert.deepEqual([...html.matchAll(/<section class="group"[^>]*>\s*<h2>([^<]+)<\/h2>/g)].map((match) => match[1]),
    ["Type", "Keys", "Device", "Control", "Stream"]);
  assert.doesNotMatch(html, /<section class="group" hidden>/, "an Android inspector section is hidden");
});

test("page: the page loads nothing from outside, has light and dark tokens with the approved accents, a phone layout with 44 px targets and the pill above the safe area, reduced motion, visible focus, and never builds DOM from strings", async (t) => {
  const { html } = await loadPage(t);
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  assert.doesNotMatch(html, /https?:\/\//, "an absolute URL is on the page");
  assert.doesNotMatch(html, /<script[^>]*\ssrc=|<link[^>]*stylesheet/i);
  assert.doesNotMatch(html.match(/<style>([\s\S]*)<\/style>/)[1], /@import|url\(/);
  for (const [, value] of html.matchAll(/\s(?:src|href)="([^"]*)"/g)) assert.match(value, /^data:/);
  assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
  assert.match(html, /<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">/);
  const light = html.match(/:root\{([\s\S]*?)\}/)[1];
  const dark = html.match(/@media \(prefers-color-scheme:dark\)\{:root\{([\s\S]*?)\}\}/);
  assert.ok(dark, "no dark color scheme block");
  assert.match(light, /color-scheme:light dark/);
  assert.match(light, /--accent-fill:#0071e3/);
  assert.match(dark[1], /--accent-fill:#0068d6/);
  assert.match(light, /--font:-apple-system,BlinkMacSystemFont/);
  assert.match(html, /:focus-visible\{outline:2px solid var\(--accent\)/);
  const phone = html.match(/@media \(max-width:760px\)\{([\s\S]*?)\n\}/);
  assert.ok(phone, "no phone layout");
  for (const rule of [".dock{position:fixed;bottom:calc(14px + env(safe-area-inset-bottom))", ".dock button{width:44px;height:44px}",
    ".popup,.option,.key,.btn,.field input,.row{height:44px}", ".linkbtn{min-height:44px}", ".diag summary{line-height:44px}",
    ".field input{font-size:16px}", "body{display:block;", "body.no-inspector .side{display:block}"]) {
    assert.ok(phone[1].includes(rule), rule);
  }
  const reduced = html.match(/@media \(prefers-reduced-motion:reduce\)\{(.*)\}/);
  assert.ok(reduced, "no reduced motion rule");
  for (const rule of ["transition:none!important", "animation:none!important", "button:active{transform:none!important}"]) {
    assert.ok(reduced[1].includes(rule), rule);
  }
});

test("page: the size menu fills an option only for keyboard focus (:focus-visible) and pointer hover, so a menu opened by pointer or touch shows the selection by its check mark alone", async (t) => {
  const { html } = await loadPage(t);
  const css = html.match(/<style>([\s\S]*)<\/style>/)[1];
  assert.match(css, /\n\.option:focus\{outline:none\}\n/);
  assert.match(css, /\n\.option:focus-visible\{background:var\(--accent-fill\);color:var\(--on-accent\)\}\n/);
  assert.match(css, /\n\.option:focus-visible small\{color:inherit\}\n/);
  // No rule for plain :focus, which the selected row has as soon as a pointer opens the menu, fills it.
  assert.doesNotMatch(css, /\.option:focus(?!-visible)[^{]*\{[^}]*(background|color)/);
  const hover = css.match(/@media \(hover:hover\)\{([\s\S]*?)\n\}/);
  assert.ok(hover, "no hover block");
  assert.ok(hover[1].includes(".option:hover{background:var(--accent-fill);color:var(--on-accent)}"));
  assert.ok(hover[1].includes(".option:hover small{color:inherit}"));
  // Hover fills only where a pointer hovers; elsewhere no option rule fills on hover.
  assert.doesNotMatch(css.replace(hover[0], ""), /\.option:hover/);
  assert.match(css, /\n\.option\[aria-selected=true\] \.check\{visibility:visible\}\n/);
  assert.match(css, /@media \(forced-colors:active\)\{\.option:focus-visible\{outline:2px solid CanvasText\}\}/);
});

test("page: the Size button shows a landscape device for Tablet and Pixel Fold, a portrait one for the phone presets, and the orientation of the size in effect for the device default or another size", async (t) => {
  const { run, html } = await loadPage(t);
  const button = html.match(/<button[^>]* id="display-preset"[^>]*>([\s\S]*?)<\/button>/)[1];
  assert.match(button, /<svg class="portrait" [^>]*><rect x="7" y="2\.5" width="10" height="19" rx="2\.5"\/>/);
  assert.match(button, /<svg class="landscape" [^>]*><rect x="2\.5" y="7" width="19" height="10" rx="2\.5"\/>/);
  const css = html.match(/<style>([\s\S]*)<\/style>/)[1];
  assert.ok(css.includes("\n.popup .landscape,.popup.wide .portrait{display:none}\n"));
  assert.ok(css.includes("\n.popup.wide .landscape{display:block}\n"));
  const wide = (preset, display = null) => run(`view.status={platform:"android"};view.preset=${JSON.stringify(preset)};` +
    `logicalDisplay=${JSON.stringify(display)};syncPicker();sizePicker.classList.contains("wide")`);
  assert.equal(wide("small"), false);
  assert.equal(wide("pixel-11", { width: 2560, height: 1600 }), false);
  assert.equal(wide("pixel-fold"), true);
  assert.equal(wide("tablet", { width: 1080, height: 2400 }), true);
  assert.equal(wide("default", { width: 1080, height: 2400 }), false);
  assert.equal(wide("default", { width: 2400, height: 1080 }), true);
  assert.equal(wide(null, { width: 1600, height: 1200 }), true);
  assert.equal(wide(null), false);
});

/** A CSS color token (#rgb, #rrggbb or rgba()) as [r, g, b, alpha]. */
function cssColor(value) {
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const pairs = hex[1].length === 3 ? [...hex[1]].map((digit) => digit + digit) : hex[1].match(/../g);
    return [...pairs.map((pair) => parseInt(pair, 16)), 1];
  }
  const rgba = value.match(/^rgba\(([^)]+)\)$/);
  assert.ok(rgba, `not a color: ${value}`);
  return rgba[1].split(",").map(Number);
}

function relativeLuminance([r, g, b]) {
  const [red, green, blue] = [r, g, b].map((value) => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(first, second) {
  const [high, low] = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
  return (high + 0.05) / (low + 0.05);
}

test("page: text, secondary text, links and filled-button text reach 4.5:1 against their backgrounds in light and in dark", async (t) => {
  const { html } = await loadPage(t);
  const tokens = (block) => Object.fromEntries([...block.matchAll(/--([\w-]+):([^;}]+)/g)].map(([, name, value]) => [name, value.trim()]));
  const light = tokens(html.match(/:root\{([\s\S]*?)\}/)[1]);
  const themes = { light, dark: { ...light, ...tokens(html.match(/@media \(prefers-color-scheme:dark\)\{:root\{([\s\S]*?)\}\}/)[1]) } };
  // [text, background, what a translucent background lies on]: the menu, notice and pill
  // material may lie over a black or a white device screen as well as the page.
  const pairs = [["text", "bg"], ["text", "surface"], ["text", "raised"], ["text", "fill-2", "surface"],
    ["text-2", "bg"], ["text-2", "surface"], ["text-2", "fill", "surface"], ["on-accent", "accent-fill"],
    ["accent", "surface"], ...["bg", "#000", "#fff"].flatMap((base) => [["text", "material", base], ["text-2", "material", base]])];
  for (const [theme, values] of Object.entries(themes)) {
    for (const [foreground, background, base] of pairs) {
      const [r, g, b, alpha] = cssColor(values[background]);
      const under = base ? cssColor(values[base] ?? base) : [0, 0, 0];
      const shown = [r, g, b].map((channel, index) => channel * alpha + under[index] * (1 - alpha));
      const ratio = contrastRatio(cssColor(values[foreground]), shown);
      assert.ok(ratio >= 4.5, `${theme}: ${foreground} on ${background}${base ? ` over ${base}` : ""} is ${ratio.toFixed(2)}:1`);
    }
  }
});

test("page: the size menu is a keyboard listbox: Enter opens it on the first preset, ArrowDown twice and Enter apply the third, and Escape, Tab, a click elsewhere or lost control close it without a change", async (t) => {
  const { sent, run, document } = await loadPage(t);
  const button = document.getElementById("display-preset");
  const menu = document.getElementById("display-menu");
  // A focused button is clicked by Enter and Space unless a handler took the key.
  const press = (key) => {
    const target = document.activeElement;
    const event = target.dispatch("keydown", { key });
    if (!event.defaultPrevented && (key === "Enter" || key === " ") && target.tagName === "BUTTON") target.click();
  };
  const focused = () => document.activeElement.dataset.preset ?? document.activeElement.id;
  const isOpen = () => menu.classList.contains("open") && button.getAttribute("aria-expanded") === "true";
  const selected = () => document.querySelectorAll("[data-preset]")
    .filter((option) => option.getAttribute("aria-selected") === "true").map((option) => option.dataset.preset);
  const displays = () => sent.filter((message) => message.t === "display").map((message) => message.preset);
  run("view.status={platform:\"android\"};view.preset=\"default\";syncPicker()");
  assert.equal(button.disabled, false);
  assert.equal(document.getElementById("display-label").textContent, "Device default");
  assert.deepEqual(selected(), ["default"]);

  button.focus();
  press("Enter");
  assert.ok(isOpen(), "Enter did not open the menu");
  assert.equal(focused(), "small");
  press("ArrowDown");
  press("ArrowDown");
  assert.equal(focused(), "pixel-fold");
  press("Enter");
  assert.deepEqual(displays(), ["pixel-fold"]);
  assert.equal(isOpen(), false);
  // The menu waits for the answer, which then shows the preset and its size. The disabled
  // button has lost the focus to the body; the answer gives it back to the button.
  assert.equal(button.disabled, true);
  assert.equal(document.activeElement, document.body);
  assert.equal(document.getElementById("display-label").textContent, "Pixel Fold (open)");
  run("onControlMessage({t:\"display\",ok:true,display:{preset:\"pixel-fold\",width:2208,height:1840,density:420}})");
  assert.equal(button.disabled, false);
  assert.equal(focused(), "display-preset", "the focus did not come back to the size button after the answer");
  assert.deepEqual(selected(), ["pixel-fold"]);
  run("render()");
  assert.equal(document.getElementById("display-dims").textContent, "2208 × 1840 · 420 dpi");

  // Escape closes without a change and gives the focus back.
  press("Enter");
  press("ArrowDown");
  press("Escape");
  assert.equal(isOpen(), false);
  assert.equal(focused(), "display-preset");
  // ArrowUp opens on the last option; Home and End move; Tab closes.
  press("ArrowUp");
  assert.equal(focused(), "default");
  press("Home");
  assert.equal(focused(), "small");
  press("End");
  assert.equal(focused(), "default");
  press("ArrowDown");
  assert.equal(focused(), "default", "the focus left the menu past its last option");
  press("Tab");
  assert.equal(isOpen(), false);
  // A pointer opens it on the preset in effect and follows the pointer; a click elsewhere closes it.
  button.dispatch("click", { detail: 1 });
  assert.ok(isOpen());
  assert.equal(focused(), "pixel-fold");
  const option = (id) => document.querySelectorAll("[data-preset]").find((item) => item.dataset.preset === id);
  option("small").dispatch("pointermove");
  assert.equal(focused(), "small");
  document.getElementById("caption").dispatch("pointerdown");
  assert.equal(isOpen(), false);
  assert.deepEqual(displays(), ["pixel-fold"]);
  // A click on a preset's label chooses it; choosing the preset in effect sends nothing.
  button.dispatch("click", { detail: 1 });
  document.elements.find((element) => element.parent === option("tablet") && element.tagName === "SPAN").dispatch("click", { detail: 1 });
  assert.deepEqual(displays(), ["pixel-fold", "tablet"]);
  // Focus that moved on while the change ran stays where it went.
  document.getElementById("text").focus();
  run("onControlMessage({t:\"display\",ok:true,display:{preset:\"tablet\",width:2560,height:1600,density:320}})");
  assert.equal(focused(), "text", "the answer took the focus back from the text box");
  button.dispatch("click", { detail: 1 });
  option("tablet").dispatch("click", { detail: 1 });
  assert.equal(isOpen(), false);
  assert.deepEqual(displays(), ["pixel-fold", "tablet"]);
  // Losing control closes the menu, which then cannot open.
  button.dispatch("click", { detail: 1 });
  run(`applyState(${JSON.stringify({ t: "state", owner: "agent", paused: false, session: "streaming", clients: null,
    preset: "tablet", density: 320 })})`);
  assert.equal(isOpen(), false);
  assert.equal(button.disabled, true);
  button.focus();
  press("Enter");
  press("ArrowDown");
  assert.equal(isOpen(), false);
  assert.deepEqual(displays(), ["pixel-fold", "tablet"]);
});

test("page: controls that cannot act are disabled, the Control buttons hand control over through the control socket or POST /control, and the toolbar, caption, notice and Stream details are filled as text", async (t) => {
  const { sandbox, sent, run, document } = await loadPage(t);
  const byId = (id) => document.getElementById(id);
  const text = (id) => byId(id).textContent;
  const inputs = [...document.querySelectorAll("[data-system],[data-code],[data-key]"), byId("text"), byId("sendText")];
  const disabled = () => [...new Set(inputs.map((control) => control.disabled))];
  const state = (fields) => run(`applyState(${JSON.stringify({ t: "state", owner: "shared", paused: false,
    session: "streaming", clients: { video: 1, control: 1 }, preset: "default", density: 420, ...fields })})`);
  // Until the page knows the status nothing can act.
  run("syncPicker()");
  assert.deepEqual(disabled(), [true]);
  assert.deepEqual([byId("control-take").disabled, byId("control-pause").disabled], [true, true]);
  run("view.status={platform:\"android\",display:{width:1080,height:2400,density:420,preset:\"default\"}};logicalDisplay=view.status.display");
  state({});
  assert.deepEqual(disabled(), [false]);
  assert.deepEqual([text("owner-name"), text("owner-detail"), text("control-chip")],
    ["Shared", "· people and agents can send input", "Shared"]);
  assert.equal(byId("owner-dot").dataset.state, "live");
  // Another owner: input is disabled, handoff and the device clipboard still act.
  state({ owner: "agent" });
  assert.deepEqual(disabled(), [true]);
  assert.deepEqual([byId("clipboard").disabled, byId("refresh").disabled, byId("control-take").disabled], [false, false, false]);
  assert.deepEqual([text("owner-name"), text("control-chip"), text("control-take")], ["Agent", "Agent", "Take control"]);
  assert.equal(byId("owner-dot").dataset.state, "warn");
  byId("control-take").click();
  assert.deepEqual(sent.at(-1), { t: "control", mode: "takeover" });
  state({ owner: "human" });
  assert.deepEqual(disabled(), [false]);
  assert.equal(text("control-take"), "Release");
  byId("control-take").click();
  assert.deepEqual(sent.at(-1), { t: "control", mode: "release" });
  byId("control-pause").click();
  assert.deepEqual(sent.at(-1), { t: "control", mode: "pause" });
  state({ paused: true });
  assert.deepEqual(disabled(), [true]);
  assert.deepEqual([text("control-pause"), text("owner-detail"), text("control-chip")],
    ["Resume input", "· input is paused", "Shared · Paused"]);
  byId("control-pause").click();
  assert.deepEqual(sent.at(-1), { t: "control", mode: "resume" });
  // Without scrcpy the handoff goes to POST /control as the human page, and the answer is shown.
  const posts = [];
  sandbox.fetch = async (path, init) => {
    posts.push([path, JSON.parse(init.body), init.headers["X-Autonom-Origin"]]);
    return { ok: true, json: async () => ({ ok: true, control_owner: "human", input_paused: false }) };
  };
  run("view.transport=\"screencap\";controlSocket=null");
  byId("control-take").click();
  await waitFor(() => text("owner-name") === "People", 2000, "the POST /control answer");
  assert.deepEqual(posts, [["/control", { mode: "takeover" }, "human"]]);
  assert.deepEqual(disabled(), [false]);

  // The toolbar, caption and Stream details on scrcpy with WebCodecs.
  run("view.transport=\"scrcpy\";view.mode=\"webcodecs\";view.width=570;view.height=1280;stats.codec=\"avc1.42C029\";" +
    "stats.renderTimes=[1,2,3];stats.framesRendered=3;view.rtt=12;render()");
  assert.deepEqual(["target-detail", "display-dims", "caption", "stream-transport", "stream-codec", "stream-frames",
    "stream-video", "stream-display", "stream-clients"].map(text),
  ["Android · scrcpy", "1080 × 2400 · 420 dpi", "3 fps · 12 ms · WebCodecs", "scrcpy · WebCodecs", "avc1.42C029",
    "3 fps · 0 dropped", "570 × 1280", "1080 × 2400 · 420 dpi", "1 video · 1 control"]);
  assert.equal(byId("live-dot").dataset.state, "live");
  assert.match(text("status"), /transport: scrcpy \(webcodecs\)/);
  assert.equal(text("display-default-size"), "", "the device default size was shown before it was read");
  run("rememberDefaultSize(view.status.display)");
  assert.equal(text("display-default-size"), "1080 × 2400");
  // A note with markup is shown as text, then leaves.
  const markup = "<img src=x onerror=alert(1)>Canvas control is owned by agent";
  run(`note(${JSON.stringify(markup)});render()`);
  assert.equal(text("notice"), markup);
  run("noteAt-=NOTICE_MS+1;render()");
  assert.equal(text("notice"), "");
  // The inspector toggle hides and shows the inspector.
  byId("inspector-toggle").click();
  assert.equal(document.body.classList.contains("no-inspector"), true);
  assert.deepEqual([byId("inspector-toggle").getAttribute("aria-pressed"), byId("inspector-toggle").title], ["false", "Show inspector"]);
  byId("inspector-toggle").click();
  assert.equal(document.body.classList.contains("no-inspector"), false);
  assert.equal(byId("inspector-toggle").getAttribute("aria-pressed"), "true");
});

test("page: the iOS page runs without the Size menu, keeps every element id, shows Home and Power, and keeps the Android buttons iOS refuses out of sight", async (t) => {
  const udid = "FAKE-SIMULATOR-UDID";
  const world = await makeWorld(t, { env: { FAKE_UDID: udid } });
  const xcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(xcrun, `#!${process.execPath}\n${FAKE_XCRUN}`);
  await chmod(xcrun, 0o755);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", udid, "--simctl", xcrun]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  await stopCanvas(canvas);
  for (const id of PAGE_IDS) assert.equal(html.split(`id="${id}"`).length - 1, 1, `id ${id}`);
  assert.doesNotMatch(html, /id="display-preset"|data-preset="/);
  assert.match(html, /<img id="screen" class="surface" tabindex="0" alt="iOS device screen">/);
  assert.match(html, /<nav class="dock" aria-label="Device buttons">/);
  const dock = html.match(/<nav class="dock"[^>]*>([\s\S]*?)<\/nav>/)[1];
  const shown = [...dock.matchAll(/<button [^>]*aria-label="([^"]+)"[^>]*>/g)]
    .filter((match) => !/ hidden[ >]/.test(match[0])).map((match) => match[1]);
  assert.deepEqual(shown, ["Home", "Power"]);
  assert.deepEqual([...html.matchAll(/<section class="group"( hidden)?>\s*<h2>([^<]+)<\/h2>/g)].map((match) => [match[2], Boolean(match[1])]),
    [["Type", false], ["Keys", true], ["Device", true], ["Control", false], ["Stream", false]]);
  const { run, document } = runPage(html);
  assert.equal(run("sizePicker"), null);
  run("view.transport=\"screencap\";controlSocket=null;view.status={platform:\"ios\",display:{width:1179,height:2556}};" +
    "logicalDisplay=view.status.display;render()");
  assert.equal(document.getElementById("target-detail").textContent, "iOS · screencap");
  assert.equal(document.getElementById("display-dims").textContent, "1179 × 2556");
  assert.equal(document.getElementById("sendText").disabled, false);
});

// ---------------------------------------------------------------------------
// iOS Simulator fast transport (idb): a fake idb_companion executable, the same kind the
// companion client tests use, extended with describe, get_orientation and HID, real
// simulator SPS/PPS, a periodic key frame, and files the test writes to steer it.
// ---------------------------------------------------------------------------

const IDB_MODULE_URL = new URL(
  "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs", import.meta.url).href;
const IOS_UDID = "3760A5A8-E59D-4AE6-B1AF-A626908D7B61";
// SPS and PPS of the real 1206x2622 simulator stream (avc1.640033).
const IOS_SPS = Buffer.from("27640033ac13143c04c0149e6a9a80868083c20108f8", "hex");
const IOS_PPS = Buffer.from("28ee3cb0", "hex");
const IOS_POINTS = { width: 402, height: 874 };

const FAKE_IDB_COMPANION = String.raw`
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import * as http2 from "node:http2";
import { GrpcMessageParser, decodeHidEvent, decodeVideoStreamRequest, encodeVideoStreamResponse, grpcFrame } from "${IDB_MODULE_URL}";

const args = process.argv.slice(2);
const arg = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const record = (event) => appendFileSync(process.env.FAKE_IDB_EVENTS, JSON.stringify({ pid: process.pid, ...event }) + "\n");
// A file named <FAKE_IDB_CONTROL>.<name> steers the fake while it runs.
const control = (name) => {
  const path = process.env.FAKE_IDB_CONTROL + "." + name;
  return existsSync(path) ? readFileSync(path, "utf8").trim() : null;
};
record({ type: "spawn", args });
if (process.env.FAKE_IDB_MODE === "crash") { process.stderr.write("cannot find target\n"); process.exit(3); }
process.on("SIGTERM", () => { record({ type: "term" }); process.exit(0); });

const varint = (value) => { const out = []; let v = value; while (v >= 0x80) { out.push((v & 0x7f) | 0x80); v = Math.floor(v / 128); } out.push(v); return Buffer.from(out); };
const tag = (field, wire) => varint(field * 8 + wire);
const len = (field, bytes) => Buffer.concat([tag(field, 2), varint(bytes.length), bytes]);
const uint = (field, value) => Buffer.concat([tag(field, 0), varint(value)]);
const dbl = (field, value) => { const b = Buffer.alloc(8); b.writeDoubleLE(value); return Buffer.concat([tag(field, 1), b]); };
const str = (field, text) => len(field, Buffer.from(text));
const ORIENTATIONS = ["UNKNOWN", "PORTRAIT", "PORTRAIT_UPSIDE_DOWN", "LANDSCAPE_LEFT", "LANDSCAPE_RIGHT"];
const SC = Buffer.from([0, 0, 0, 1]);
const SPS = Buffer.from("${IOS_SPS.toString("hex")}", "hex");
const PPS = Buffer.from("${IOS_PPS.toString("hex")}", "hex");
const KEY_EVERY = Number(process.env.FAKE_IDB_KEY_EVERY ?? 50);
const INTERVAL_MS = Number(process.env.FAKE_IDB_INTERVAL_MS ?? 10);
let seq = 0;
const frame = (key) => {
  const body = Buffer.alloc(5);
  body[0] = key ? 0x65 : 0x41;
  body.writeUInt32BE(seq++, 1);
  return key ? Buffer.concat([SC, SPS, SC, PPS, SC, body]) : Buffer.concat([SC, body]);
};
let calls = 0;
const server = http2.createServer();
server.on("session", (session) => session.on("error", () => {}));
server.on("stream", (stream, headers) => {
  const method = headers[":path"].split("/").pop();
  const parser = new GrpcMessageParser();
  stream.on("error", () => {});
  stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
  stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
  if (method === "video_stream") {
    const call = ++calls;
    let timer = null;
    let running = false;
    let sent = 0;
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        const request = decodeVideoStreamRequest(message);
        record({ type: request.type, call, fps: request.fps, format: request.format, keyFrameRate: request.keyFrameRate,
          quality: request.compressionQuality, avgBitrate: request.avgBitrate, scale: request.scaleFactor });
        if (request.type === "start") {
          running = true;
          timer = setInterval(() => {
            if (control("rst") === "now") {
              record({ type: "rst", call });
              clearInterval(timer);
              stream.close(http2.constants.NGHTTP2_INTERNAL_ERROR);
              return;
            }
            if (control("freeze")) return;
            stream.write(grpcFrame(encodeVideoStreamResponse({ data: frame(sent % KEY_EVERY === 0) })));
            sent += 1;
          }, INTERVAL_MS);
        } else if (request.type === "stop") {
          // A wedged companion: Stop arrives but is never answered, and the encoder runs on.
          if (control("ignore-stop")) continue;
          clearInterval(timer);
          running = false;
          stream.end();
        }
      }
    });
    // Like idb_companion: a call gone without Stop leaves its encoder running.
    stream.on("close", () => { if (running) record({ type: "leak", call }); clearInterval(timer); });
  } else if (method === "hid") {
    stream.on("data", (chunk) => { for (const message of parser.push(chunk)) record({ type: "hid", event: decodeHidEvent(message) }); });
    stream.on("end", () => stream.end());
  } else {
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        if (method === "describe") {
          const screen = Buffer.concat([uint(1, 1206), uint(2, 2622), dbl(3, 3), uint(4, ${IOS_POINTS.width}), uint(5, ${IOS_POINTS.height})]);
          stream.write(grpcFrame(len(1, Buffer.concat([str(1, arg("--udid")), str(2, "Autonom-Fast-Test"), len(3, screen), str(4, "Booted")]))));
        } else if (method === "get_orientation") {
          const name = control("orientation") ?? "PORTRAIT";
          stream.write(grpcFrame(uint(1, Math.max(0, ORIENTATIONS.indexOf(name)))));
        }
        stream.end();
      }
    });
  }
});
const socketPath = arg("--grpc-domain-sock");
server.listen(socketPath, () => process.stdout.write(JSON.stringify({ grpc_path: socketPath }) + "\n"));
`;

// simctl for the iOS fast tests: the Simulator's state comes from FAKE_SIM_STATE (Booted by default).
const FAKE_IOS_XCRUN = String.raw`
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify(args) + "\n");
if (args[0] === "simctl" && args[1] === "list") {
  const path = process.env.FAKE_SIM_STATE;
  const state = path && existsSync(path) ? readFileSync(path, "utf8").trim() : "Booted";
  console.log(JSON.stringify({ devices: { "iOS 27.0": [{ udid: process.env.FAKE_UDID, state, isAvailable: true }] } }));
} else if (args[0] === "simctl" && args[1] === "io") {
  writeFileSync(args.at(-1), Buffer.from(process.env.FAKE_PNG, "base64"));
} else if (args[0] === "simctl" && (args[1] === "pbcopy" || args[1] === "pbpaste")) {
  // The Simulator pasteboard is the file FAKE_PASTEBOARD; <it>.mode makes the command fail
  // ("fail"), or pbpaste print what a fresh Simulator prints ("no-items"); while <it>.hold
  // exists pbcopy waits. Each pbcopy is counted in <it>.copies, never logged with its text.
  const board = process.env.FAKE_PASTEBOARD;
  const mode = existsSync(board + ".mode") ? readFileSync(board + ".mode", "utf8").trim() : "";
  if (mode === "fail") {
    console.error("Unable to access the pasteboard");
    process.exit(4);
  }
  if (args[1] === "pbpaste") {
    if (mode === "no-items") process.stdout.write("There are no items on the device's pasteboard.\n");
    else if (existsSync(board)) process.stdout.write(readFileSync(board));
  } else {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => {
      const wait = () => {
        if (existsSync(board + ".hold")) { setTimeout(wait, 20); return; }
        writeFileSync(board, Buffer.concat(chunks));
        appendFileSync(board + ".copies", "1\n");
      };
      wait();
    });
  }
} else {
  console.error("unsupported fake xcrun command: " + args.join(" "));
  process.exitCode = 2;
}
`;

/** A world for an iOS Canvas: fake simctl, and a fake idb_companion on PATH unless `companion` is false. */
async function iosWorld(t, { env = {}, companion = true } = {}) {
  const world = await makeWorld(t, { env: { FAKE_UDID: IOS_UDID, ...env } });
  world.xcrun = join(world.directory, "fake-xcrun.mjs");
  await writeFile(world.xcrun, `#!${process.execPath}\n${FAKE_IOS_XCRUN}`);
  await chmod(world.xcrun, 0o755);
  world.companion = join(world.directory, "companion-bin", "idb_companion");
  await mkdir(dirname(world.companion));
  await writeFile(world.companion, `#!${process.execPath}\n${FAKE_IDB_COMPANION}`);
  await chmod(world.companion, 0o755);
  if (companion) await symlink(world.companion, join(world.bin, "idb_companion"));
  world.idbEvents = join(world.directory, "idb-events.jsonl");
  await writeFile(world.idbEvents, "");
  world.idbControl = join(world.directory, "idb-control");
  world.simState = join(world.directory, "sim-state");
  world.pasteboard = join(world.directory, "pasteboard");
  Object.assign(world.env, {
    FAKE_IDB_EVENTS: world.idbEvents, FAKE_IDB_CONTROL: world.idbControl, FAKE_SIM_STATE: world.simState,
    FAKE_PASTEBOARD: world.pasteboard,
  });
  // A companion a failed test left behind goes too, with the Canvas, before the folder is removed.
  world.children.push({
    exitCode: null, signalCode: null,
    kill: () => {
      for (const { pid } of idbEventsNow(world).filter((event) => event.type === "spawn")) {
        try { process.kill(pid, "SIGKILL"); } catch {}
      }
    },
  });
  return world;
}

function idbEventsNow(world) {
  return readFileSync(world.idbEvents, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

function idbControl(world, name, value) {
  const path = `${world.idbControl}.${name}`;
  if (value === null) rmSync(path, { force: true });
  else writeFileSync(path, value);
}

function hidEvents(world) {
  return idbEventsNow(world).filter((event) => event.type === "hid").map((event) => event.event);
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function iosCanvas(t, { args = [], env = {}, companion = true, nodeArgs = [] } = {}) {
  const world = await iosWorld(t, { env, companion });
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun, ...args],
    { nodeArgs });
  const auth = await login(canvas);
  return { world, canvas, auth };
}

/** An iOS Canvas on idb with a control socket whose session streams. */
async function iosControl(t, options) {
  const setup = await iosCanvas(t, options);
  const control = await pageSocket(setup.canvas, setup.auth, "/ws/control");
  await control.next((message) => message.json?.t === "state" && message.json.session === "streaming", 10_000);
  return { ...setup, control };
}

/** Wait until the HID stream has seen `count` events, then return them all. */
async function hidAtLeast(world, count) {
  await waitFor(() => hidEvents(world).length >= count, 5000, `${count} HID events`);
  return hidEvents(world);
}

const touchAt = (direction, x, y) => ({ touch: { x, y }, direction });

test("ios: auto picks idb when idb_companion is on PATH; its frames reach /ws/video as SESSION, CONFIG and PACKET, the Start asks for 60 fps, quality 0.5 and 2 s key frames", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t);
  assert.match(canvas.output(), /Transport: idb/);
  const before = await status(canvas);
  assert.equal(before.transport, "idb");
  assert.equal(before.fallback_reason, null);
  assert.equal(before.idb.source, "PATH");
  assert.equal(before.idb.session_state, "idle");
  assert.equal(idbEventsNow(world).length, 0, "the companion started before a page asked for video");
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 60, 10_000, "60 packets");
  const video = tab.video();
  assert.deepEqual(video.find((message) => message.kind === "session"), { kind: "session", width: 1206, height: 2622 });
  const config = video.find((message) => message.kind === "config");
  assert.ok(config.data.equals(Buffer.concat([Buffer.from([0, 0, 0, 1]), IOS_SPS, Buffer.from([0, 0, 0, 1]), IOS_PPS])));
  assert.ok(video.findIndex((message) => message.kind === "config") < video.findIndex((message) => message.kind === "packet"));
  const packets = tab.packets();
  assert.equal(packets[0].keyFrame, true);
  // The key frame goes without its SPS and PPS: they are in CONFIG.
  assert.deepEqual([...packets[0].data.subarray(0, 5)], [0, 0, 0, 1, 0x65]);
  assert.deepEqual(packets.map((packet) => packet.seq), packets.map((_, index) => index), "a frame was lost");
  assert.ok(packets.every((packet, index) => index === 0 || packet.pts >= packets[index - 1].pts));
  assert.equal(packets.filter((packet) => packet.keyFrame).length, Math.ceil(packets.length / 50));
  const events = idbEventsNow(world);
  const spawns = events.filter((event) => event.type === "spawn");
  assert.equal(spawns.length, 1);
  assert.deepEqual(spawns[0].args.slice(0, 2), ["--udid", IOS_UDID]);
  assert.ok(spawns[0].args.includes("--grpc-domain-sock"), "the companion listens on TCP");
  const starts = events.filter((event) => event.type === "start");
  assert.equal(starts.length, 1);
  assert.deepEqual({ ...starts[0], pid: undefined, call: undefined },
    { type: "start", pid: undefined, call: undefined, fps: 60, format: 0, keyFrameRate: 2, quality: 0.5, avgBitrate: 12_000_000, scale: 1 });
  const body = await status(canvas);
  assert.equal(body.idb.session_state, "streaming");
  assert.deepEqual(body.idb.points, IOS_POINTS);
  assert.equal(body.idb.orientation, "PORTRAIT");
  assert.equal(body.idb.companion_starts, 1);
  assert.equal(body.idb.streams_opened, 1);
  assert.ok(body.idb.packets >= 60);
  const state = tab.json("state").at(-1);
  assert.equal(state.transport, "idb");
  assert.equal(state.orientation, "PORTRAIT");
  assert.equal(state.rotation, 0);
  // A browser without WebCodecs still gets a picture: the multipart stream serves screenshots.
  const multipart = await readMultipart(canvas);
  try {
    await waitFor(() => multipart.frames.length >= 1, 10_000, "a multipart frame on the idb transport");
  } finally {
    multipart.close();
  }
  assert.deepEqual(multipart.frames[0], Buffer.from(PNG_BASE64, "base64"));
  assert.equal(idbEventsNow(world).filter((event) => event.type === "start").length, 1, "the multipart page opened a stream");
  await stopCanvas(canvas);
});

test("ios: tabs share one companion stream; a late joiner starts at the cached key frame and a tab that asks for one waits for the periodic key frame, never a second stream", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t, { env: { FAKE_IDB_KEY_EVERY: "100", FAKE_IDB_INTERVAL_MS: "10" } });
  const first = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => first.packets().length >= 30, 10_000, "the first tab's packets");
  const late = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => late.packets().length >= 5, 5000, "the late tab's packets");
  const latePackets = late.packets();
  assert.equal(latePackets[0].keyFrame, true, "the late tab did not start at a key frame");
  assert.equal(latePackets[0].seq, 0, "the late tab did not start at the cached key frame");
  assert.deepEqual(latePackets.map((packet) => packet.seq).slice(0, 5), [0, 1, 2, 3, 4]);
  assert.equal(late.video()[0].kind, "session");
  assert.equal(late.video()[1].kind, "config");
  // A page asks for a key frame (as after a decoder error): the next periodic one answers it.
  const control = await pageSocket(canvas, auth, "/ws/control");
  const keysBefore = (await status(canvas)).idb.key_frames;
  control.send({ t: "system", op: "keyframe" });
  await waitFor(async () => (await status(canvas)).idb.key_frames > keysBefore, 5000, "the periodic key frame");
  await sleep(300);
  const third = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => third.packets().length >= 3, 5000, "the third tab's packets");
  assert.equal(third.packets()[0].keyFrame, true);
  const events = idbEventsNow(world);
  assert.equal(events.filter((event) => event.type === "spawn").length, 1, "a second companion");
  assert.equal(events.filter((event) => event.type === "start").length, 1, "a second stream");
  assert.equal(events.filter((event) => event.type === "stop").length, 0);
  const body = await status(canvas);
  assert.equal(body.idb.video_clients, 3);
  assert.equal(body.idb.forced_key_frames, 0, "a key frame was forced although a periodic one came");
  await stopCanvas(canvas);
});

waitingTest("ios: a key frame asked for that does not come within 2.5 s is forced by Stop and Start on the same companion, at most once per 5 s", async (t) => {
  // A key frame only every 100 s: the periodic one never comes during the test.
  const { world, canvas, auth } = await iosCanvas(t, { env: { FAKE_IDB_KEY_EVERY: "10000", FAKE_IDB_INTERVAL_MS: "10" } });
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 10, 10_000, "packets");
  const control = await pageSocket(canvas, auth, "/ws/control");
  const asked = Date.now();
  control.send({ t: "system", op: "keyframe" });
  control.send({ t: "system", op: "keyframe" });
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "start").length === 2, 6000, "the forced restart");
  assert.ok(Date.now() - asked >= 2400, "the key frame was forced before the periodic one could come");
  const events = idbEventsNow(world);
  const pids = new Set(events.filter((event) => event.type === "spawn").map((event) => event.pid));
  assert.equal(pids.size, 1, "the forced key frame restarted the companion");
  // Stop on the old call went out before Start on the new one.
  assert.deepEqual(events.filter((event) => ["start", "stop"].includes(event.type)).map((event) => `${event.type} ${event.call}`),
    ["start 1", "stop 1", "start 2"]);
  await waitFor(() => tab.packets().filter((packet) => packet.keyFrame).length === 2, 3000, "the forced key frame");
  // Asked again at once: not forced again within 5 s.
  control.send({ t: "system", op: "keyframe" });
  await sleep(3500);
  assert.equal(idbEventsNow(world).filter((event) => event.type === "start").length, 2, "forced twice within 5 s");
  assert.equal((await status(canvas)).idb.forced_key_frames, 1);
  await stopCanvas(canvas);
});

test("ios: a forced restart whose Stop the companion does not answer replaces the companion before the next stream, never two encoders", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t, { env: { FAKE_IDB_KEY_EVERY: "10000", FAKE_IDB_INTERVAL_MS: "10" } });
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 10, 10_000, "packets");
  const pid = (await status(canvas)).idb.companion_pid;
  idbControl(world, "ignore-stop", "1");
  const control = await pageSocket(canvas, auth, "/ws/control");
  control.send({ t: "system", op: "keyframe" });
  await waitFor(() => idbEventsNow(world).some((event) => event.type === "term" && event.pid === pid), 12_000,
    "the companion whose Stop went unanswered to be ended");
  idbControl(world, "ignore-stop", null);
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "start").length === 2, 10_000,
    "a stream on a new companion");
  const events = idbEventsNow(world);
  // No Start went out on the old companion after its unanswered Stop ...
  assert.deepEqual(events.filter((event) => event.pid === pid && ["start", "stop"].includes(event.type))
    .map((event) => `${event.type} ${event.call}`), ["start 1", "stop 1"]);
  // ... and it was ended before the new one was spawned, so only one encoder ever ran.
  const spawns = events.filter((event) => event.type === "spawn");
  assert.equal(spawns.length, 2);
  assert.notEqual(spawns[1].pid, pid);
  assert.ok(events.findIndex((event) => event.type === "term" && event.pid === pid) < events.indexOf(spawns[1]),
    "the new companion started while the old encoder could still run");
  const starts = events.filter((event) => event.type === "start");
  assert.deepEqual(starts.map((event) => event.pid), [pid, spawns[1].pid]);
  assert.equal(alive(pid), false);
  const count = tab.packets().length;
  await waitFor(() => tab.packets().length >= count + 3, 10_000, "packets from the new companion");
  const body = await status(canvas);
  assert.equal(body.idb.session_state, "streaming");
  assert.equal(body.idb.companion_starts, 2);
  assert.equal(body.idb.streams_opened, 2);
  assert.equal(body.idb.companion_pid, spawns[1].pid);
  await stopCanvas(canvas);
});

waitingTest("ios: SIGTERM stops the stream with Stop and ends the companion; the last tab leaving does the same 15 s later", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t);
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 5, 10_000, "packets");
  const pid = (await status(canvas)).idb.companion_pid;
  assert.ok(alive(pid));
  tab.close();
  await tab.closed;
  await sleep(13_000);
  assert.ok(alive(pid), "the companion stopped before 15 s without clients");
  assert.equal(idbEventsNow(world).filter((event) => event.type === "stop").length, 0);
  await waitFor(() => !alive(pid), 5000, "the idle stop");
  let events = idbEventsNow(world);
  assert.deepEqual(events.filter((event) => ["start", "stop", "term", "leak"].includes(event.type)).map((event) => event.type),
    ["start", "stop", "term"]);
  assert.equal((await status(canvas)).idb.session_state, "idle");

  // A new tab starts a new companion; SIGTERM to the Canvas ends it with Stop first.
  const again = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => again.packets().length >= 5, 10_000, "packets after the idle stop");
  const second = (await status(canvas)).idb.companion_pid;
  assert.notEqual(second, pid);
  const exit = await stopCanvas(canvas);
  assert.equal(exit.code, 0);
  assert.equal(alive(second), false, "the companion outlived the Canvas");
  events = idbEventsNow(world).filter((event) => event.pid === second);
  assert.deepEqual(events.filter((event) => ["start", "stop", "term", "leak"].includes(event.type)).map((event) => event.type),
    ["start", "stop", "term"]);
  assert.ok(!idbEventsNow(world).some((event) => event.type === "leak"), "an encoder was left running");
});

test("ios: a companion that dies mid-stream is started again and the picture resumes on one stream", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t);
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 5, 10_000, "packets");
  const pid = (await status(canvas)).idb.companion_pid;
  process.kill(pid, "SIGKILL");
  const restarting = await tab.next((message) => message.json?.t === "state" && message.json.session === "restarting", 5000);
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "spawn").length === 2, 10_000, "a new companion");
  // Messages on the socket keep their order and the old stream had ended before the state
  // said restarting, so every packet after that message is from the new stream, however
  // early its first frame came.
  const packetsAfterRestart = () => {
    const at = tab.messages.indexOf(restarting);
    return tab.video.call({ messages: tab.messages.slice(at + 1) }).filter((message) => message.kind === "packet");
  };
  await waitFor(() => packetsAfterRestart().length >= 5, 10_000, "packets after the restart");
  const after = packetsAfterRestart();
  assert.equal(after[0].keyFrame, true, "the new stream did not start at a key frame");
  assert.equal(after[0].seq, 0, "the first packet after the restart is not the new stream's first frame");
  const body = await status(canvas);
  assert.equal(body.idb.session_state, "streaming");
  assert.equal(body.idb.companion_starts, 2);
  assert.ok(body.idb.restarts >= 1);
  assert.notEqual(body.idb.companion_pid, pid);
  const starts = idbEventsNow(world).filter((event) => event.type === "start");
  assert.deepEqual(starts.map((event) => event.pid), [pid, body.idb.companion_pid]);
  await stopCanvas(canvas);
});

test("ios: a stream dropped without Stop leaves the companion's encoder running, so that companion is ended and a new one streams", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t);
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 5, 10_000, "packets");
  const pid = (await status(canvas)).idb.companion_pid;
  idbControl(world, "rst", "now");
  await waitFor(() => idbEventsNow(world).some((event) => event.type === "leak" && event.pid === pid), 5000, "the dropped call");
  idbControl(world, "rst", null);
  await waitFor(() => idbEventsNow(world).some((event) => event.type === "term" && event.pid === pid), 10_000,
    "the leaking companion to be ended");
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "start").length === 2, 10_000, "a new stream");
  const second = idbEventsNow(world).filter((event) => event.type === "start")[1];
  assert.notEqual(second.pid, pid, "the new stream ran on the companion whose encoder leaked");
  assert.equal(alive(pid), false);
  const count = tab.packets().length;
  await waitFor(() => tab.packets().length >= count + 3, 10_000, "packets from the new companion");
  await stopCanvas(canvas);
});

waitingTest("ios: a Simulator that shuts down and boots again gets its stream restarted with Stop and Start on the same companion", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t);
  const tab = await pageSocket(canvas, auth, "/ws/video");
  await waitFor(() => tab.packets().length >= 5, 10_000, "packets");
  const pid = (await status(canvas)).idb.companion_pid;
  // Shut down: the stream freezes on its last picture.
  writeFileSync(world.simState, "Shutdown");
  idbControl(world, "freeze", "1");
  await tab.next((message) => message.json?.t === "state" && message.json.session === "restarting", 6000);
  writeFileSync(world.simState, "Booted");
  idbControl(world, "freeze", null);
  const bootedAt = Date.now();
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "start").length === 2, 8000, "the stream restart");
  await tab.next((message) => message.json?.t === "state" && message.json.session === "streaming" &&
    tab.messages.indexOf(message) > tab.messages.findIndex((item) => item.json?.session === "restarting"), 5000);
  assert.ok(Date.now() - bootedAt < 5000, "the picture took more than 5 s to resume after Booted");
  const events = idbEventsNow(world);
  assert.deepEqual(events.filter((event) => ["start", "stop"].includes(event.type)).map((event) => `${event.type} ${event.call}`),
    ["start 1", "stop 1", "start 2"]);
  assert.equal(new Set(events.filter((event) => event.type === "start").map((event) => event.pid)).size, 1);
  assert.equal(events.filter((event) => event.type === "spawn").length, 1);
  assert.equal((await status(canvas)).idb.companion_pid, pid);
  await stopCanvas(canvas);
});

test("ios: without idb_companion auto falls back to screencap and says why; --transport idb then refuses to start, and Android refuses idb", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t, { companion: false });
  const body = await status(canvas);
  assert.equal(body.transport, "screencap");
  assert.match(body.fallback_reason, /idb_companion was not found/);
  assert.equal(body.idb, null);
  const refused = await openSocket(canvas.port, `/ws/video?csrf=${encodeURIComponent(auth.csrf)}`, {
    headers: { Cookie: auth.cookie, Origin: canvas.origin },
  });
  assert.equal(refused.status, 409);
  assert.match(refused.body, /The idb transport is not active on this Canvas/);
  await stopCanvas(canvas);

  // The refusals are the CLI's JSON error objects with exit code 2, as `autonom canvas
  // serve` prints them when it refuses first.
  const refusal = (result) => {
    assert.equal(result.code, 2, result.stderr);
    return JSON.parse(result.stderr.trim().split("\n").at(-1));
  };
  const idbArgs = ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun, "--transport", "idb"];
  // AUTONOM_IDB_COMPANION is the remote companion of idb calls: neither a host:port nor a
  // file path there is the Canvas's binary.
  for (const remote of [null, "mac-farm-01:10882", world.companion]) {
    const env = remote ? { ...world.env, AUTONOM_IDB_COMPANION: remote } : world.env;
    const explicit = refusal(await startCanvas({ ...world, env }, idbArgs, { expectExit: true }));
    assert.deepEqual(explicit, {
      ok: false,
      error_code: "tool_missing",
      error: "--transport idb is unavailable: idb_companion was not found (--idb-companion, AUTONOM_IDB_COMPANION_BIN or PATH)",
      hint: "Install it with `brew install facebook/fb/idb-companion`, set AUTONOM_IDB_COMPANION_BIN to its path, " +
        "or pass --idb-companion PATH.",
      tool: "idb_companion",
      capability: "canvas.idb",
    }, String(remote));
  }
  const badVariable = refusal(await startCanvas({ ...world, env: { ...world.env, AUTONOM_IDB_COMPANION_BIN: world.directory } },
    idbArgs, { expectExit: true }));
  assert.equal(badVariable.error_code, "tool_missing");
  assert.equal(badVariable.error,
    `--transport idb is unavailable: AUTONOM_IDB_COMPANION_BIN is not an executable file: ${world.directory}`);
  for (const args of [["--transport", "idb"], ["--idb-companion", world.companion]]) {
    const android = refusal(await startCanvas(world, args, { expectExit: true }));
    assert.deepEqual([android.error_code, android.capability, android.error],
      ["unsupported_on_platform", "canvas.idb", "the idb transport mirrors iOS Simulators only"]);
  }

  // AUTONOM_IDB_COMPANION_BIN or --idb-companion selects it, the flag first; a set
  // AUTONOM_IDB_COMPANION is left alone.
  for (const [extra, env, source] of [
    [[], { AUTONOM_IDB_COMPANION_BIN: world.companion, AUTONOM_IDB_COMPANION: "mac-farm-01:10882" }, "AUTONOM_IDB_COMPANION_BIN"],
    [["--idb-companion", world.companion], { AUTONOM_IDB_COMPANION_BIN: world.directory }, "--idb-companion"],
  ]) {
    const configured = await startCanvas({ ...world, env: { ...world.env, ...env } },
      ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun, ...extra]);
    const configuredStatus = await status(configured);
    assert.equal(configuredStatus.transport, "idb");
    assert.equal(configuredStatus.idb.source, source);
    assert.equal(configuredStatus.idb.companion_path, world.companion);
    await stopCanvas(configured);
  }
  for (const remote of ["mac-farm-01:10882", world.companion]) {
    const ignored = await startCanvas({ ...world, env: { ...world.env, AUTONOM_IDB_COMPANION: remote } },
      ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun]);
    assert.equal((await status(ignored)).transport, "screencap", remote);
    await stopCanvas(ignored);
  }
  for (const path of [join(world.directory, "nothing-here"), world.directory]) {
    const missing = await startCanvas(world, ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun,
      "--idb-companion", path]);
    const missingStatus = await status(missing);
    assert.equal(missingStatus.transport, "screencap", path);
    assert.equal(missingStatus.fallback_reason, `--idb-companion is not an executable file: ${path}`);
    await stopCanvas(missing);
  }
});

test("ios: a companion that cannot start falls back to screencap once, closes the sockets and names the reason", async (t) => {
  const { world, canvas, auth } = await iosCanvas(t, { env: { FAKE_IDB_MODE: "crash" } });
  const tab = await pageSocket(canvas, auth, "/ws/video");
  const code = await tab.closed;
  assert.equal(code, 1000);
  const body = await status(canvas);
  assert.equal(body.transport, "screencap");
  assert.match(body.fallback_reason, /^idb failed to start: idb_companion exited before it was ready/);
  assert.match(body.fallback_reason, /cannot find target/);
  await sleep(1500);
  assert.equal(idbEventsNow(world).filter((event) => event.type === "spawn").length, 1, "the companion was retried after the fallback");
  await stopCanvas(canvas);
});

test("ios: a held finger moves live with repeated DOWN then UP in logical points; another connection's pointer is refused while the first keeps working; one gesture record names idb", async (t) => {
  const { world, canvas, auth, control } = await iosControl(t);
  const other = await pageSocket(canvas, auth, "/ws/control");
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.4 });
  await allHandled(control);
  const refusal = other.reply((message) => message.t === "error" && message.for === "touch");
  other.send({ t: "touch", a: "down", id: 2, x: 0.1, y: 0.1 });
  assert.match((await refusal).message, /one finger at a time/);
  // Its moves and up are ignored without more errors.
  other.send({ t: "touch", a: "move", id: 2, x: 0.2, y: 0.2 });
  other.send({ t: "touch", a: "up", id: 2, x: 0.2, y: 0.2 });
  await allHandled(other);
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.3 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.25 });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 4), [
    touchAt("down", 201, 437), touchAt("down", 201, 349.6), touchAt("down", 201, 262.2), touchAt("up", 201, 218.5),
  ]);
  assert.equal(other.json("error").length, 1, "the refused pointer's moves were answered");
  assert.equal(control.json("error").length, 0);
  await waitFor(async () => (await journaled(world, "gesture")).length === 1, 5000, "the gesture record");
  const [record] = await journaled(world, "gesture");
  assert.equal(record.origin, "human");
  assert.equal(record.payload.transport, "idb");
  assert.equal(record.payload.pointers, 1);
  assert.equal(record.payload.moves, 2);
  // Journal points are on the screen the companion described (402x874 points), as the bridge
  // measures iOS screens, and no bridge screen-size call (an accessibility dump) was needed.
  assert.deepEqual(record.payload.start, [201, 437]);
  assert.deepEqual(record.payload.end, [201, 219]);
  assert.deepEqual((await status(canvas)).display, { width: 402, height: 874 });
  assert.equal((await bridgeCalls(world, "screen-size")).length, 0, "the bridge measured the screen while streaming");
  await stopCanvas(canvas);
});

test("ios: the wheel is a short synthetic drag lifted once the wheel is still, Home and Power press HOME and LOCK, text goes through the bridge, and Android-only input is refused", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  // Wheel down twice: the finger goes down at the pointer and moves up 24 points per step.
  control.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  control.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 4), [
    touchAt("down", 201, 437), touchAt("down", 201, 413), touchAt("down", 201, 389), touchAt("up", 201, 389),
  ]);
  control.send({ t: "system", op: "home" });
  control.send({ t: "system", op: "power" });
  await allHandled(control);
  assert.deepEqual((await hidAtLeast(world, 8)).slice(4), [
    { button: "HOME", direction: "down" }, { button: "HOME", direction: "up" },
    { button: "LOCK", direction: "down" }, { button: "LOCK", direction: "up" },
  ]);
  control.send({ t: "text", text: "wifi" });
  control.send({ t: "text", text: "s3cret", sensitive: true });
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 2, 5000, "the bridge text calls");
  const texts = await bridgeCalls(world, "text");
  assert.deepEqual(texts.map((call) => call.payload), [
    { text: "wifi", sensitive: false, transport: "idb" }, { text: "s3cret", sensitive: true, transport: "idb" }]);
  assert.deepEqual(texts.map((call) => call.origin), ["human", "human"]);
  for (const [message, pattern] of [
    [{ t: "key", a: "down", code: 66 }, /Android keycodes have no iOS Simulator equivalent/],
    [{ t: "system", op: "back" }, /back is not available on the iOS Simulator/],
    [{ t: "system", op: "notifications" }, /notifications is not available/],
    [{ t: "display", preset: "tablet" }, /Display presets are Android-only/],
  ]) {
    const answer = control.reply((reply) => reply.t === "error");
    control.send(message);
    assert.match((await answer).message, pattern, JSON.stringify(message));
  }
  await allHandled(control);
  assert.equal(hidEvents(world).length, 8, "refused input reached the HID stream");
  await waitFor(async () => (await journaled(world)).length === 3, 5000, "the records");
  assert.deepEqual((await journaled(world)).map((record) => [record.payload.kind, record.payload.transport,
    record.payload.op ?? record.payload.events]), [["scroll", "idb", 2], ["system", "idb", "home"], ["system", "idb", "power"]]);
  const scroll = (await journaled(world, "scroll"))[0].payload;
  assert.deepEqual([scroll.dx, scroll.dy], [0, -2]);
  await stopCanvas(canvas);
});

/**
 * Split HID touch events into drags (DOWN ... UP) and check none could be a tap: each moves
 * past the iOS tap slop (about 10 points) from where it went down, and stays on the screen.
 */
function assertNoWheelTap(events, size = IOS_POINTS) {
  let drag = null;
  for (const event of events) {
    assert.ok(event.touch, `not a touch: ${JSON.stringify(event)}`);
    const { x, y } = event.touch;
    assert.ok(x >= 2 && x <= size.width - 2 && y >= 2 && y <= size.height - 2, `a wheel point left the screen: ${JSON.stringify(event)}`);
    if (!drag) {
      assert.equal(event.direction, "down", "an UP without a DOWN");
      drag = { start: event.touch, far: 0 };
    }
    drag.far = Math.max(drag.far, Math.hypot(x - drag.start.x, y - drag.start.y));
    if (event.direction === "up") {
      assert.ok(drag.far >= 16, `a wheel drag moved only ${drag.far} points, which iOS takes for a tap`);
      drag = null;
    }
  }
  assert.equal(drag, null, "a wheel drag was left down");
}

test("ios: the wheel never taps: tiny deltas still move past the tap slop, a pointer near an edge drags from an anchor inside the screen, the edge starts the drag over, and it waits while a finger is down", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "touch", a: "down", id: 7, x: 0.5, y: 0.5 });
  const wait = control.reply((message) => message.t === "error" && message.for === "scroll");
  control.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  assert.match((await wait).message, /A finger is down/);
  control.send({ t: "touch", a: "up", id: 7, x: 0.5, y: 0.5 });
  await allHandled(control);
  await hidAtLeast(world, 2);
  let records = 0;
  // One burst: its events, then its scroll record, which waits in the input queue behind its UP.
  const burst = async (messages, expected) => {
    const from = hidEvents(world).length;
    for (const message of messages) control.send({ t: "scroll", dx: 0, dy: 0, ...message });
    await allHandled(control);
    records += 1;
    await waitFor(async () => (await journaled(world, "scroll")).length === records, 5000, `scroll record ${records}`);
    const events = (await hidAtLeast(world, from + expected.length)).slice(from);
    assert.deepEqual(events, expected, JSON.stringify(messages));
    assertNoWheelTap(events);
  };
  // A gentle trackpad (3 px, 0.72 points): the first move still goes 16 points, then the
  // finger follows the deltas.
  await burst([{ x: 0.5, y: 0.5, dy: -0.03 }, { x: 0.5, y: 0.5, dy: -0.03 }], [
    touchAt("down", 201, 437), touchAt("down", 201, 421), touchAt("down", 201, 420.28), touchAt("up", 201, 420.28),
  ]);
  // A pointer 13 points from the top: the drag starts 32 points inside, lifts at the edge
  // (2 points inside) and starts over from there for each step.
  await burst([{ x: 0.5, y: 13 / 874, dy: -1 }, { x: 0.5, y: 13 / 874, dy: -1 }, { x: 0.5, y: 13 / 874, dy: -1 }], [
    touchAt("down", 201, 32), touchAt("down", 201, 8),
    touchAt("down", 201, 2), touchAt("up", 201, 2), touchAt("down", 201, 32), touchAt("down", 201, 8),
    touchAt("down", 201, 2), touchAt("up", 201, 2), touchAt("down", 201, 32), touchAt("down", 201, 8),
    touchAt("up", 201, 8),
  ]);
  // In the edge strip itself, sideways and at the bottom: the same, from the anchor inside.
  await burst([{ x: 0.002, y: 0.5, dx: 1 }], [touchAt("down", 32, 437), touchAt("down", 8, 437), touchAt("up", 8, 437)]);
  await burst([{ x: 0.5, y: 0.999, dy: 1 }], [touchAt("down", 201, 842), touchAt("down", 201, 866), touchAt("up", 201, 866)]);
  // A flick (16 steps, 384 points) from a row drags to the edge, never taps the row.
  await burst([{ x: 0.5, y: 0.3, dy: -16 }], [touchAt("down", 201, 262.2), touchAt("down", 201, 2), touchAt("up", 201, 2)]);
  // A burst that does not move (no delta) sends nothing, yet it is one scroll record.
  const before = hidEvents(world).length;
  await burst([{ x: 0.5, y: 0.5 }], []);
  await sleep(200);
  assert.equal(hidEvents(world).length, before, "a wheel event without a delta reached the Simulator");
  assertNoWheelTap(hidEvents(world).slice(2));
  await stopCanvas(canvas);
});

test("ios: rotation is reported in state messages and /status, and touches map to rotated logical points", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await allHandled(control);
  idbControl(world, "orientation", "LANDSCAPE_LEFT");
  const state = await control.until((message) => message.t === "state" && message.orientation === "LANDSCAPE_LEFT", 5000);
  assert.equal(state.rotation, 270);
  // The finger down in portrait was lifted by the rotation, and its moves are refused.
  const refused = control.reply((message) => message.t === "error" && message.for === "touch");
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.4 });
  assert.match((await refused).message, /was lifted because the Simulator rotated/);
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.25 });
  control.send({ t: "touch", a: "up", id: 2, x: 1, y: 1 });
  await allHandled(control);
  // HID writes are not awaited by the server, so the fake companion may log them a moment later.
  assert.deepEqual(await hidAtLeast(world, 4), [
    touchAt("down", 201, 437), touchAt("up", 201, 437), touchAt("down", 437, 100.5), touchAt("up", 874, 402),
  ]);
  const body = await status(canvas);
  assert.equal(body.idb.orientation, "LANDSCAPE_LEFT");
  assert.equal(body.idb.rotation, 270);
  idbControl(world, "orientation", "LANDSCAPE_RIGHT");
  assert.equal((await control.until((message) => message.t === "state" && message.orientation === "LANDSCAPE_RIGHT", 5000)).rotation, 90);
  idbControl(world, "orientation", "PORTRAIT_UPSIDE_DOWN");
  assert.equal((await control.until((message) => message.t === "state" && message.orientation === "PORTRAIT_UPSIDE_DOWN", 5000)).rotation, 180);
  // Face up keeps the orientation the screen is drawn in.
  idbControl(world, "orientation", "UNKNOWN");
  await sleep(1500);
  assert.equal((await status(canvas)).idb.orientation, "PORTRAIT_UPSIDE_DOWN");
  await stopCanvas(canvas);
});

test("ios: a takeover lifts the human finger on the Simulator and refuses human input; pause refuses everyone; nothing refused reaches the HID stream", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  const agent = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(agent.status, 101);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.45 });
  await allHandled(control);
  agent.ws.send({ t: "control", mode: "takeover" });
  await allHandled(agent.ws);
  await hidAtLeast(world, 3);
  assert.deepEqual(hidEvents(world).at(-1), touchAt("up", 201, 393.3), "the takeover did not lift the finger");
  const before = hidEvents(world).length;
  for (const message of [{ t: "touch", a: "move", id: 1, x: 0.5, y: 0.4 }, { t: "touch", a: "down", id: 3, x: 0.2, y: 0.2 },
    { t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: 1 }, { t: "system", op: "home" }, { t: "text", text: "no" }]) {
    const answer = control.reply((reply) => reply.t === "error");
    control.send(message);
    assert.equal((await answer).message, "Canvas control is owned by agent", JSON.stringify(message));
  }
  // The agent's input goes through.
  agent.ws.send({ t: "system", op: "home" });
  await allHandled(agent.ws);
  await hidAtLeast(world, before + 2);
  agent.ws.send({ t: "control", mode: "pause" });
  await allHandled(agent.ws);
  const paused = agent.ws.reply((reply) => reply.t === "error");
  agent.ws.send({ t: "system", op: "power" });
  assert.equal((await paused).message, "Canvas input is paused");
  await sleep(200);
  assert.equal(hidEvents(world).length, before + 2, "refused input reached the HID stream");
  assert.equal((await bridgeCalls(world, "text")).length, 0);
  // The takeover ends the human gesture and is journaled itself; pause is one more control record.
  await waitFor(async () => (await journaled(world)).length === 4, 5000, "the records");
  const kinds = (await journaled(world)).map((record) => `${record.origin} ${record.payload.kind} ${record.payload.transport}`).sort();
  assert.deepEqual(kinds, ["agent control idb", "agent control idb", "agent system idb", "human gesture idb"]);
  await stopCanvas(canvas);
});

/** Bridge input (text) waits while the hold is on: input that is still being typed. */
function holdBridgeInput(world, on) {
  const path = `${world.bridgeLog}.input-hold`;
  if (on) writeFileSync(path, "");
  else rmSync(path, { force: true });
}

/** Only the text `text` waits at the bridge while this hold is on. */
function holdBridgeText(world, text, on) {
  const path = `${world.bridgeLog}.input-hold.${text}`;
  if (on) writeFileSync(path, "");
  else rmSync(path, { force: true });
}

test("ios: touches, Home and their records sent after text wait until the bridge has typed it, as one device socket keeps the order on Android", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  holdBridgeInput(world, true);
  control.send({ t: "text", text: "wifi" });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "system", op: "home" });
  control.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  await allHandled(control);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the text call");
  // The text is still being typed: nothing sent after it may reach the Simulator, however
  // long it takes.
  await sleep(300);
  assert.deepEqual(hidEvents(world), [], "input sent after the text reached the Simulator before the text was typed");
  assert.deepEqual((await journaled(world)).length, 0, "a record of input sent after the text came before it");
  holdBridgeInput(world, false);
  assert.deepEqual(await hidAtLeast(world, 7), [
    touchAt("down", 201, 437), touchAt("up", 201, 437), { button: "HOME", direction: "down" }, { button: "HOME", direction: "up" },
    touchAt("down", 201, 437), touchAt("down", 201, 413), touchAt("up", 201, 413),
  ]);
  await waitFor(async () => (await journaled(world)).length === 3, 5000, "the records");
  const order = (await bridgeCalls(world)).filter((call) => call.op === "text" || call.op === "record")
    .map((call) => (call.op === "text" ? "text" : call.payload.kind));
  assert.deepEqual(order, ["text", "gesture", "system", "scroll"]);
  assert.equal(control.json("error").length, 0);
  await stopCanvas(canvas);
});

test("ios: input queued behind text when a takeover comes is neither typed nor sent nor journaled, each text says why, and the finger held down before it is lifted in order", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  const agent = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(agent.status, 101);
  // A finger down before the text reaches the Simulator at once.
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 1), [touchAt("down", 201, 437)]);
  holdBridgeInput(world, true);
  for (const text of ["w", "i", "f", "i"]) control.send({ t: "text", text });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.4 });
  control.send({ t: "system", op: "home" });
  await allHandled(control);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the first text call");
  agent.ws.send({ t: "control", mode: "takeover" });
  await allHandled(agent.ws);
  // The agent's input waits behind the human's and goes through.
  agent.ws.send({ t: "system", op: "power" });
  await allHandled(agent.ws);
  await sleep(200);
  assert.deepEqual(hidEvents(world), [touchAt("down", 201, 437)], "input reached the Simulator while text was being typed");
  holdBridgeInput(world, false);
  const textErrors = () => control.json("error").filter((message) => message.for === "text");
  await waitFor(() => textErrors().length === 3, 5000, "a refusal for each text not typed yet");
  for (const message of textErrors()) assert.equal(message.message, "Canvas control is owned by agent");
  // The move and Home queued before the takeover are dropped; the takeover's UP lifts the
  // finger where the Simulator has it, then the agent's Power goes out.
  assert.deepEqual(await hidAtLeast(world, 4), [
    touchAt("down", 201, 437), touchAt("up", 201, 437), { button: "LOCK", direction: "down" }, { button: "LOCK", direction: "up" },
  ]);
  await waitFor(async () => (await journaled(world)).length === 3, 5000, "the records");
  await sleep(300);
  assert.equal(hidEvents(world).length, 4, "human input sent before the takeover reached the Simulator after it");
  assert.equal((await bridgeCalls(world, "text")).length, 1, "text refused by its turn was typed");
  // Home never reached the Simulator, so it is not journaled, although the finger's DOWN was
  // written for the same connection; the finger's gesture is.
  assert.deepEqual((await journaled(world)).map((record) => `${record.origin} ${record.payload.kind}`),
    ["agent control", "human gesture", "agent system"]);
  await stopCanvas(canvas);
});

test("ios: a finger whose UP still waits behind text when the companion dies is lifted through the next companion, and the stale UP is not sent", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await allHandled(control);
  await hidAtLeast(world, 1);
  holdBridgeInput(world, true);
  control.send({ t: "text", text: "wifi" });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  await allHandled(control);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the text call");
  const pid = (await status(canvas)).idb.companion_pid;
  process.kill(pid, "SIGKILL");
  await control.until((message) => message.t === "state" && message.session === "restarting", 5000);
  await control.until((message) => message.t === "state" && message.session === "streaming", 10_000);
  const next = (await status(canvas)).idb.companion_pid;
  assert.notEqual(next, pid);
  holdBridgeInput(world, false);
  await waitFor(() => idbEventsNow(world).filter((event) => event.type === "hid").length >= 2, 5000, "the orphan UP");
  await waitFor(async () => (await journaled(world, "gesture")).length === 1, 5000, "the gesture record");
  await sleep(300);
  assert.deepEqual(idbEventsNow(world).filter((event) => event.type === "hid").map((event) => [event.pid, event.event]), [
    [pid, touchAt("down", 201, 437)], [next, touchAt("up", 201, 437)],
  ]);
  // Input after it goes to the new companion as before.
  control.send({ t: "touch", a: "down", id: 2, x: 0.25, y: 0.25 });
  control.send({ t: "touch", a: "up", id: 2, x: 0.25, y: 0.25 });
  await allHandled(control);
  assert.deepEqual((await hidAtLeast(world, 4)).slice(2), [touchAt("down", 100.5, 218.5), touchAt("up", 100.5, 218.5)]);
  await stopCanvas(canvas);
});

test("ios: a tap and Home queued behind text when the companion dies never reach a companion and are not journaled", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  holdBridgeInput(world, true);
  control.send({ t: "text", text: "wifi" });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "system", op: "home" });
  await allHandled(control);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the text call");
  const pid = (await status(canvas)).idb.companion_pid;
  process.kill(pid, "SIGKILL");
  await control.until((message) => message.t === "state" && message.session === "restarting", 5000);
  await control.until((message) => message.t === "state" && message.session === "streaming", 10_000);
  assert.notEqual((await status(canvas)).idb.companion_pid, pid);
  holdBridgeInput(world, false);
  // Input sent after the tap and Home goes out and is journaled: everything before it ran.
  control.send({ t: "system", op: "power" });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 2), [{ button: "LOCK", direction: "down" }, { button: "LOCK", direction: "up" }]);
  await waitFor(async () => (await journaled(world)).length >= 1, 5000, "the power record");
  await sleep(300);
  assert.equal(hidEvents(world).length, 2, "input of the lost companion reached the next one");
  assert.deepEqual((await journaled(world)).map((record) => `${record.payload.kind} ${record.payload.op ?? ""}`.trim()),
    ["system power"], "an action that never reached a companion was journaled");
  await stopCanvas(canvas);
});

test("ios: once a takeover refuses a gesture's DOWN, its queued move and UP are dropped after the release, and the gesture is not journaled", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  const agent = await openSocket(canvas.port, `/ws/control?token=${TOKEN}&origin=agent`);
  assert.equal(agent.status, 101);
  holdBridgeText(world, "a", true);
  holdBridgeText(world, "b", true);
  control.send({ t: "text", text: "a" });
  await allHandled(control);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the human text call");
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await allHandled(control);
  // The agent's text waits between the human's DOWN and the rest of the gesture.
  agent.ws.send({ t: "text", text: "b" });
  await allHandled(agent.ws);
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.4 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.4 });
  await allHandled(control);
  agent.ws.send({ t: "control", mode: "takeover" });
  await allHandled(agent.ws);
  // The human text ends; the DOWN's turn comes under the takeover and is refused; the agent's
  // text is being typed.
  holdBridgeText(world, "a", false);
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 2, 5000, "the agent text call");
  agent.ws.send({ t: "control", mode: "release" });
  await allHandled(agent.ws);
  holdBridgeText(world, "b", false);
  // Input after the gesture goes out: everything before it ran.
  control.send({ t: "system", op: "home" });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 2), [{ button: "HOME", direction: "down" }, { button: "HOME", direction: "up" }],
    "the move of a refused DOWN pressed where the user never pressed");
  await waitFor(async () => (await journaled(world, "system")).length === 1, 5000, "the home record");
  await sleep(300);
  assert.equal(hidEvents(world).length, 2);
  assert.equal((await journaled(world, "gesture")).length, 0, "a gesture that never reached the Simulator was journaled");
  assert.deepEqual((await journaled(world)).map((record) => `${record.origin} ${record.payload.kind}`),
    ["agent control", "agent control", "human system"]);
  await stopCanvas(canvas);
});

test("ios: volume-up and volume-down press VOLUME_UP and VOLUME_DOWN down then up on the HID stream, with one system record each", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "system", op: "volume-up" });
  control.send({ t: "system", op: "volume-down" });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 4), [
    { button: "VOLUME_UP", direction: "down" }, { button: "VOLUME_UP", direction: "up" },
    { button: "VOLUME_DOWN", direction: "down" }, { button: "VOLUME_DOWN", direction: "up" },
  ]);
  assert.equal(control.json("error").length, 0);
  await waitFor(async () => (await journaled(world, "system")).length === 2, 5000, "the system records");
  assert.deepEqual((await journaled(world, "system")).map((record) => [record.payload.op, record.payload.transport]),
    [["volume-up", "idb"], ["volume-down", "idb"]]);
  // POST /key with the Android volume names reaches the action bridge, which presses the
  // Simulator's volume buttons (tests/test_canvas_scrcpy.py BridgeIosButtonTests).
  for (const key of ["KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN"]) {
    const answer = await rawHttp(canvas.port, { method: "POST", path: "/key", headers: agentHeaders(), body: JSON.stringify({ key }) });
    assert.equal(answer.status, 200, answer.body);
  }
  assert.deepEqual((await bridgeCalls(world, "key")).map((call) => [call.payload.key, call.origin]),
    [["KEYCODE_VOLUME_UP", "agent"], ["KEYCODE_VOLUME_DOWN", "agent"]]);
  await stopCanvas(canvas);
});

/** A preload that makes the Canvas process see `process.platform` as `name`. */
function platformPreload(name) {
  return `data:text/javascript,${encodeURIComponent(`
import { syncBuiltinESMExports } from "node:module";
Object.defineProperty(process, "platform", { value: ${JSON.stringify(name)} });
syncBuiltinESMExports();
`)}`;
}

// The Simulator clipboard needs the Canvas on the Mac that runs the Simulator. The clipboard
// tests start the Canvas as if it ran on macOS, so they behave the same on a Linux test host.
const ON_MACOS = { nodeArgs: ["--import", platformPreload("darwin")] };
// The Canvas as if it ran on Linux: its Simulator (behind a remote companion) is on another Mac.
const NOT_MACOS = platformPreload("linux");

/** simctl clipboard calls the fake xcrun saw, as [command, udid]. */
async function simctlClipboardCalls(world) {
  return (await adbCalls(world)).filter((args) => args[0] === "simctl" && /^pb(copy|paste)$/.test(args[1]))
    .map((args) => args.slice(1));
}

function pasteboardCopies(world) {
  return existsSync(`${world.pasteboard}.copies`) ? readFileSync(`${world.pasteboard}.copies`, "utf8").split("\n").filter(Boolean).length : 0;
}

test("ios: clipboard-get reads the Simulator clipboard with simctl pbpaste for the Canvas UDID; empty and the no-items message read as no text, a failure is an error for clipboard-get, and the text reaches no log, journal or status", async (t) => {
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  const state = await control.next((message) => message.json?.t === "state" && "clipboard" in message.json);
  assert.equal(state.json.clipboard, true);
  const marker = "autonom-clip-test \u00fc\u65e5\u672c";
  writeFileSync(world.pasteboard, marker);
  let answer = control.reply((message) => message.t === "clipboard");
  control.send({ t: "clipboard-get" });
  assert.deepEqual(await answer, { t: "clipboard", text: marker });
  writeFileSync(world.pasteboard, "");
  answer = control.reply((message) => message.t === "clipboard");
  control.send({ t: "clipboard-get" });
  assert.deepEqual(await answer, { t: "clipboard", text: null });
  writeFileSync(`${world.pasteboard}.mode`, "no-items");
  answer = control.reply((message) => message.t === "clipboard");
  control.send({ t: "clipboard-get" });
  assert.deepEqual(await answer, { t: "clipboard", text: null });
  writeFileSync(`${world.pasteboard}.mode`, "fail");
  answer = control.reply((message) => message.t === "error");
  control.send({ t: "clipboard-get" });
  const failure = await answer;
  assert.equal(failure.for, "clipboard-get");
  assert.match(failure.message, /could not be read: simctl pbpaste exited with 4: Unable to access the pasteboard/);
  // Reading the clipboard is not input: it works under an agent's takeover, like on Android.
  rmSync(`${world.pasteboard}.mode`);
  writeFileSync(world.pasteboard, marker);
  await agentTakeover(canvas);
  answer = control.reply((message) => message.t === "clipboard");
  control.send({ t: "clipboard-get" });
  assert.equal((await answer).text, marker);
  assert.deepEqual(await simctlClipboardCalls(world), Array(5).fill(["pbpaste", IOS_UDID]));
  assert.equal(hidEvents(world).length, 0);
  const body = await status(canvas);
  await stopCanvas(canvas);
  assert.doesNotMatch(JSON.stringify(body) + canvas.output() + (await readFile(world.bridgeLog, "utf8")), /autonom-clip-test/);
});

test("ios: a paste sets the Simulator clipboard once with simctl pbcopy and, as plain ASCII up to 300 bytes, is typed once through the bridge, journaled as a paste; input sent after it waits until it is done", async (t) => {
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  writeFileSync(`${world.pasteboard}.hold`, "");
  const text = "autonom-clip-test\n~!";
  const done = control.reply((message) => message.t === "paste");
  control.send({ t: "paste", text });
  control.send({ t: "system", op: "home" });
  await waitFor(async () => (await simctlClipboardCalls(world)).length === 1, 5000, "the pbcopy");
  await sleep(200);
  assert.equal(hidEvents(world).length, 0, "Home went ahead of the paste");
  assert.equal((await bridgeCalls(world, "text")).length, 0, "typed before the clipboard was set");
  rmSync(`${world.pasteboard}.hold`);
  assert.deepEqual(await done, { t: "paste", typed: true });
  assert.equal(readFileSync(world.pasteboard, "utf8"), text);
  assert.equal(pasteboardCopies(world), 1);
  const typed = await bridgeCalls(world, "text");
  assert.deepEqual(typed.map((call) => [call.payload, call.origin]),
    [[{ text, sensitive: true, transport: "idb", paste: true }, "human"]]);
  assert.deepEqual(await hidAtLeast(world, 2), [{ button: "HOME", direction: "down" }, { button: "HOME", direction: "up" }]);
  // The bridge journals the typed paste itself; the Canvas sends no second record for it.
  await waitFor(async () => (await journaled(world, "system")).length === 1, 5000, "the Home record");
  assert.equal((await journaled(world, "paste")).length, 0);
  assert.deepEqual(await simctlClipboardCalls(world), [["pbcopy", IOS_UDID]]);
  await stopCanvas(canvas);
  assert.doesNotMatch(canvas.output(), /autonom-clip-test/);
});

test("ios: a paste longer than 300 bytes is only set on the Simulator clipboard, the reply says long-press Paste inserts it, and one paste record has its length only", async (t) => {
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  const text = "autonom-clip-test ".repeat(20);
  const done = control.reply((message) => message.t === "paste");
  control.send({ t: "paste", text });
  const answer = await done;
  assert.equal(answer.typed, false);
  assert.match(answer.message, /clipboard was set .* not typed: touch and hold a text field and choose Paste/);
  assert.equal(readFileSync(world.pasteboard, "utf8"), text);
  assert.equal(pasteboardCopies(world), 1);
  assert.equal((await bridgeCalls(world, "text")).length, 0, "long text was typed");
  await waitFor(async () => (await journaled(world, "paste")).length === 1, 5000, "the paste record");
  const [record] = await journaled(world, "paste");
  assert.deepEqual([record.payload.kind, record.payload.transport, record.payload.text_len, record.payload.text],
    ["paste", "idb", text.length, undefined]);
  await stopCanvas(canvas);
  assert.doesNotMatch(canvas.output(), /autonom-clip-test/);
});

test("ios: a short paste with characters idb cannot type is only set on the Simulator clipboard, never sent to the bridge, and the reply says long-press Paste inserts it", async (t) => {
  // idb's `ui text` has keys for printable ASCII and newline only ("No keycode found" for any
  // other character), so typing this text would fail after the clipboard was already set.
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  for (const text of ["autonom-clip-test \u00fc", "autonom-clip-test\t", "\u0430\u0431", "autonom \u{1f600}"]) {
    const done = control.reply((message) => message.t === "paste");
    control.send({ t: "paste", text });
    const answer = await done;
    assert.equal(answer.typed, false, JSON.stringify(text));
    assert.match(answer.message, /clipboard was set .* other than plain ASCII.* not typed: touch and hold a text field and choose Paste/);
    assert.equal(readFileSync(world.pasteboard, "utf8"), text);
  }
  assert.equal(pasteboardCopies(world), 4);
  assert.equal((await bridgeCalls(world, "text")).length, 0, "text idb cannot type was sent to the bridge");
  await waitFor(async () => (await journaled(world, "paste")).length === 4, 5000, "the paste records");
  const records = await journaled(world, "paste");
  assert.deepEqual(records.map((record) => [record.payload.text_len, record.payload.text]),
    [[19, undefined], [18, undefined], [2, undefined], [9, undefined]]);
  await stopCanvas(canvas);
  assert.doesNotMatch(canvas.output(), /autonom-clip-test/);
});

test("ios: a paste whose pbcopy fails, or that a takeover refuses by its turn, is answered with an error for paste and types nothing", async (t) => {
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  writeFileSync(`${world.pasteboard}.mode`, "fail");
  let answer = control.reply((message) => message.t === "error");
  control.send({ t: "paste", text: "autonom-clip-test" });
  const failure = await answer;
  assert.equal(failure.for, "paste");
  assert.match(failure.message, /could not be set: simctl pbcopy exited with 4/);
  rmSync(`${world.pasteboard}.mode`);
  // Queued behind held text, the paste's turn comes after an agent took control.
  holdBridgeText(world, "first", true);
  control.send({ t: "text", text: "first" });
  answer = control.reply((message) => message.t === "error" && message.for === "paste");
  control.send({ t: "paste", text: "autonom-clip-test" });
  await waitFor(async () => (await bridgeCalls(world, "text")).length === 1, 5000, "the held text");
  await agentTakeover(canvas);
  holdBridgeText(world, "first", false);
  assert.match((await answer).message, /owned by agent/);
  await allHandled(control);
  assert.equal(pasteboardCopies(world), 0);
  assert.deepEqual((await bridgeCalls(world, "text")).map((call) => call.payload.text), ["first"]);
  assert.equal((await journaled(world, "paste")).length, 0);
  await stopCanvas(canvas);
});

test("ios: an xcrun that cannot be started answers clipboard-get and paste with an error, and the Canvas keeps running", async (t) => {
  const { world, canvas, control } = await iosControl(t, ON_MACOS);
  const { rename } = await import("node:fs/promises");
  await rename(world.xcrun, `${world.xcrun}.gone`);
  t.after(() => rename(`${world.xcrun}.gone`, world.xcrun).catch(() => {}));
  for (const [message, pattern] of [
    [{ t: "clipboard-get" }, /could not be read: .*ENOENT/],
    [{ t: "paste", text: "autonom-clip-test" }, /could not be set: .*ENOENT/],
  ]) {
    const answer = control.reply((reply) => reply.t === "error" && reply.for === message.t);
    control.send(message);
    assert.match((await answer).message, pattern);
  }
  await allHandled(control);
  assert.equal((await bridgeCalls(world, "text")).length, 0);
  assert.equal(typeof (await status(canvas)).transport, "string");
  await rename(`${world.xcrun}.gone`, world.xcrun);
  await stopCanvas(canvas);
});

test("ios: a Canvas that does not run on macOS refuses paste and clipboard-get naming why, runs no simctl clipboard command, and its state says the clipboard is unavailable", async (t) => {
  const world = await iosWorld(t);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun],
    { nodeArgs: ["--import", NOT_MACOS] });
  const auth = await login(canvas);
  const control = await pageSocket(canvas, auth, "/ws/control");
  const state = await control.next((message) => message.json?.t === "state" && message.json.session === "streaming", 10_000);
  assert.equal(state.json.clipboard, false);
  for (const message of [{ t: "paste", text: "autonom-clip-test" }, { t: "clipboard-get" }]) {
    const answer = control.reply((reply) => reply.t === "error");
    control.send(message);
    const refusal = await answer;
    assert.equal(refusal.for, message.t);
    assert.match(refusal.message, /needs xcrun simctl on the Mac that runs the Simulator; this Canvas does not run on macOS/);
  }
  await allHandled(control);
  assert.deepEqual(await simctlClipboardCalls(world), []);
  assert.equal((await bridgeCalls(world, "text")).length, 0);
  await stopCanvas(canvas);
});

/** The HIDPinch the Canvas computes for two pointers given as 0..1 points on the 402x874 screen. */
function expectedPinch(start, end) {
  const point = ([x, y]) => ({ x: Math.round(x * IOS_POINTS.width * 100) / 100, y: Math.round(y * IOS_POINTS.height * 100) / 100 });
  const [a, b, c, d] = [...start, ...end].map(point);
  const half = (p, q) => Math.hypot(p.x - q.x, p.y - q.y) / 2;
  const center = { x: Math.round(((a.x + b.x) / 2) * 100) / 100, y: Math.round(((a.y + b.y) / 2) * 100) / 100 };
  const radius = Math.round(half(a, b) * 100) / 100;
  return { center, radius, scale: Math.round((half(c, d) / radius) * 10_000) / 10_000 };
}

test("ios: a mirrored pinch sends nothing before release, then one HIDPinch of center, radius, scale and the time it took; one gesture record with pointers 2; a tap after it goes over the HID stream as before", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  // As the page sends it: the mirror (-1000 - id) first, then the pointer.
  control.send({ t: "touch", a: "down", id: -1001, x: 0.6, y: 0.6 });
  control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.4 });
  for (const step of [0.35, 0.3]) {
    control.send({ t: "touch", a: "move", id: -1001, x: 1 - step, y: 1 - step });
    control.send({ t: "touch", a: "move", id: 1, x: step, y: step });
  }
  await allHandled(control);
  await sleep(150);
  assert.deepEqual(hidEvents(world), [], "a touch went out before the release");
  control.send({ t: "touch", a: "up", id: -1001, x: 0.7, y: 0.7 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.3, y: 0.3 });
  await allHandled(control);
  const [event] = await hidAtLeast(world, 1);
  const { duration, ...shape } = event.pinch;
  assert.deepEqual(shape, expectedPinch([[0.4, 0.4], [0.6, 0.6]], [[0.3, 0.3], [0.7, 0.7]]));
  assert.equal(shape.scale, 2);
  assert.ok(duration > 0 && duration <= 2, `duration ${duration}`);
  assert.equal(control.json("error").length, 0, "the second up was answered");
  // A tap afterwards is one finger, live, as before.
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 2, x: 0.5, y: 0.5 });
  await allHandled(control);
  assert.deepEqual((await hidAtLeast(world, 3)).slice(1), [touchAt("down", 201, 437), touchAt("up", 201, 437)]);
  await waitFor(async () => (await journaled(world, "gesture")).length === 2, 5000, "the gesture records");
  const records = await journaled(world, "gesture");
  assert.deepEqual(records.map((record) => [record.payload.pointers, record.payload.transport]), [[2, "idb"], [1, "idb"]]);
  assert.equal(records[0].payload.moves, 4);
  await stopCanvas(canvas);
});

test("ios: a second pointer of the same connection lifts the live finger and makes a pinch; a third pointer and another connection's pointer are refused with one finger at a time and leave the pinch as it was", async (t) => {
  const { world, canvas, auth, control } = await iosControl(t);
  const other = await pageSocket(canvas, auth, "/ws/control");
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.4 });
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.6 });
  await allHandled(control);
  assert.deepEqual(await hidAtLeast(world, 2), [touchAt("down", 201, 349.6), touchAt("up", 201, 349.6)]);
  let refusal = control.reply((message) => message.t === "error" && message.for === "touch");
  control.send({ t: "touch", a: "down", id: 3, x: 0.1, y: 0.1 });
  assert.match((await refusal).message, /one finger at a time/);
  refusal = other.reply((message) => message.t === "error" && message.for === "touch");
  other.send({ t: "touch", a: "down", id: 7, x: 0.9, y: 0.9 });
  assert.match((await refusal).message, /one finger at a time/);
  other.send({ t: "touch", a: "move", id: 7, x: 0.8, y: 0.8 });
  other.send({ t: "touch", a: "up", id: 7, x: 0.8, y: 0.8 });
  other.send({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -1 });
  control.send({ t: "touch", a: "move", id: 3, x: 0.2, y: 0.2 });
  control.send({ t: "touch", a: "up", id: 3, x: 0.2, y: 0.2 });
  await allHandled(other);
  control.send({ t: "touch", a: "move", id: 2, x: 0.5, y: 0.7 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.4 });
  control.send({ t: "touch", a: "up", id: 2, x: 0.5, y: 0.7 });
  await allHandled(control);
  const events = await hidAtLeast(world, 3);
  assert.equal(events.length, 3);
  const { duration, ...shape } = events[2].pinch;
  // Pointer 1 started where the live finger was when pointer 2 came.
  assert.deepEqual(shape, expectedPinch([[0.5, 0.4], [0.5, 0.6]], [[0.5, 0.4], [0.5, 0.7]]));
  assert.ok(duration >= 0 && duration <= 2);
  assert.equal(control.json("error").length, 1);
  assert.equal(other.json("error").filter((message) => message.for === "touch").length, 1);
  await stopCanvas(canvas);
});

test("ios: a pending pinch is dropped without a HID event or record when a takeover lifts it", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "touch", a: "down", id: -1001, x: 0.6, y: 0.6 });
  control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.4 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.3, y: 0.3 });
  await allHandled(control);
  await agentTakeover(canvas);
  control.send({ t: "touch", a: "up", id: 1, x: 0.3, y: 0.3 });
  await allHandled(control);
  await sleep(200);
  assert.deepEqual(hidEvents(world), []);
  assert.equal((await journaled(world, "gesture")).length, 0);
  await stopCanvas(canvas);
});

test("ios: a mirrored pinch puts no touch down or up on the HID stream, only one HIDPinch, while an ordinary tap still goes down live before its up is sent", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  // The page's Ctrl/Alt-drag: the mirrored finger (-1000 - id) comes first and marks the pair as a pinch.
  control.send({ t: "touch", a: "down", id: -1001, x: 0.6, y: 0.6 });
  await allHandled(control);
  await sleep(150);
  assert.deepEqual(hidEvents(world), [], "the mirrored finger went down on its own");
  control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.4 });
  control.send({ t: "touch", a: "move", id: -1001, x: 0.7, y: 0.7 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.3, y: 0.3 });
  control.send({ t: "touch", a: "up", id: -1001, x: 0.7, y: 0.7 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.3, y: 0.3 });
  await allHandled(control);
  await hidAtLeast(world, 1);
  await sleep(150);
  const events = hidEvents(world);
  assert.deepEqual(events.filter((event) => event.touch), [], "a touch down or up went out for the pinch");
  assert.equal(events.length, 1, JSON.stringify(events));
  assert.ok(events[0].pinch, "the one event is not a HIDPinch");
  assert.equal(control.json("error").length, 0);
  // An ordinary tap is live: its down reaches the HID stream while the finger is still held.
  control.send({ t: "touch", a: "down", id: 2, x: 0.5, y: 0.5 });
  assert.deepEqual((await hidAtLeast(world, 2)).slice(1), [touchAt("down", 201, 437)]);
  control.send({ t: "touch", a: "up", id: 2, x: 0.5, y: 0.5 });
  assert.deepEqual((await hidAtLeast(world, 3)).slice(1), [touchAt("down", 201, 437), touchAt("up", 201, 437)]);
  await stopCanvas(canvas);
});

waitingTest("ios: a pinch held longer than 2 s replays in 2 s, so a long hold cannot stall the HID stream", async (t) => {
  const { world, canvas, control } = await iosControl(t);
  control.send({ t: "touch", a: "down", id: -1001, x: 0.6, y: 0.5 });
  control.send({ t: "touch", a: "down", id: 1, x: 0.4, y: 0.5 });
  await allHandled(control);
  await sleep(2300);
  control.send({ t: "touch", a: "up", id: 1, x: 0.45, y: 0.5 });
  await allHandled(control);
  const [event] = await hidAtLeast(world, 1);
  assert.equal(event.pinch.duration, 2);
  assert.deepEqual(event.pinch.center, { x: 201, y: 437 });
  await stopCanvas(canvas);
});

test("ios: the page uses WebCodecs and the control socket on idb, turns the canvas for landscape, sends Home, Power and the volume buttons, a mirrored pinch with the mirror first and at most two fingers, typed characters as text, and long text or a paste as a paste once the clipboard is available", async (t) => {
  const world = await iosWorld(t);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  await stopCanvas(canvas);
  const { sandbox, sent, run, document } = runPage(html);
  run("view.transport=\"idb\";view.mode=\"webcodecs\";");
  assert.equal(run("fastActive()&&iosFast()&&!scrcpyActive()"), true);
  run("applyTransport()");
  assert.match(document.getElementById("text-hint").textContent, /Typed into the Simulator/);
  // Home and Power go over the control socket as system actions.
  const dock = document.elements.filter((element) => element.parent?.tagName === "NAV" && element.tagName === "BUTTON");
  const byLabel = (label) => dock.find((element) => element.getAttribute("aria-label") === label);
  for (const label of ["Home", "Power", "Volume down", "Volume up"]) assert.equal(byLabel(label).hidden, false, `${label} is hidden`);
  for (const label of ["Back", "Recent apps", "Rotate"]) assert.equal(byLabel(label).hidden, true, `${label} shows`);
  byLabel("Home").click();
  byLabel("Power").click();
  byLabel("Volume up").click();
  byLabel("Volume down").click();
  assert.deepEqual(sent, [{ t: "system", op: "home" }, { t: "system", op: "power" },
    { t: "system", op: "volume-up" }, { t: "system", op: "volume-down" }]);
  // Off idb (screencap) the volume buttons go out of sight again.
  run("const socket=controlSocket;socket.close=()=>{};view.transport=\"screencap\";applyTransport();" +
    "view.transport=\"idb\";view.mode=\"webcodecs\";controlSocket=socket");
  assert.equal(byLabel("Volume up").hidden, true);
  run("applyTransport()");
  assert.equal(byLabel("Volume up").hidden, false);
  // A landscape Simulator: the canvas holds the portrait picture turned upright.
  run("setVideoSize(1206,2622)");
  assert.deepEqual(JSON.parse(run("JSON.stringify([video.width,video.height])")), [1206, 2622]);
  run("applyState({t:\"state\",owner:\"shared\",paused:false,session:\"streaming\",clients:{video:1,control:1},transport:\"idb\",orientation:\"LANDSCAPE_LEFT\",rotation:270,width:1206,height:2622})");
  assert.deepEqual(JSON.parse(run("JSON.stringify([video.width,video.height,rotation])")), [2622, 1206, 270]);
  run("setRotation(0)");
  assert.deepEqual(JSON.parse(run("JSON.stringify([video.width,video.height])")), [1206, 2622]);
  // Ctrl-drag is a mirrored pinch whose mirror finger goes first; a third finger is refused here.
  sent.length = 0;
  const surfaceRect = { left: 0, top: 0, width: 1206, height: 2622 };
  run("video.getBoundingClientRect=()=>(" + JSON.stringify(surfaceRect) + ");video.setPointerCapture=()=>{};");
  const pointer = (pointerId, extra = {}) => ({ pointerId, pointerType: "mouse", button: 0, clientX: 300, clientY: 1311,
    pressure: 0.5, ctrlKey: true, preventDefault() {}, ...extra });
  sandbox.onPointerDown(pointer(1));
  sandbox.onPointerDown(pointer(2, { pointerType: "touch" }));
  sandbox.onPointerEnd(pointer(1), "up");
  const labels = () => sent.map((message) => `${message.t} ${message.a} ${message.id}`);
  assert.deepEqual(labels(), ["touch down -1001", "touch down 1", "touch up -1001", "touch up 1"]);
  const [mirror, first] = sent;
  assert.ok(Math.abs(mirror.x - (1 - first.x)) < 1e-9 && Math.abs(mirror.y - (1 - first.y)) < 1e-9, "the mirror is around the centre");
  assert.match(run("view.note"), /two fingers at most/);
  // Two plain fingers go out as two pointers; a third is refused.
  sent.length = 0;
  sandbox.onPointerDown(pointer(3, { pointerType: "touch", ctrlKey: false }));
  sandbox.onPointerDown(pointer(4, { pointerType: "touch", ctrlKey: false }));
  sandbox.onPointerDown(pointer(5, { pointerType: "touch", ctrlKey: false }));
  sandbox.onPointerEnd(pointer(3, { ctrlKey: false }), "up");
  sandbox.onPointerEnd(pointer(4, { ctrlKey: false }), "up");
  assert.deepEqual(labels(), ["touch down 3", "touch down 4", "touch up 3", "touch up 4"]);
  // Typed characters on the screen become text; shortcuts and keys without a character do not.
  sent.length = 0;
  const key = (code, value, extra = {}) => ({ code, key: value, repeat: false, shiftKey: false, ctrlKey: false,
    altKey: false, metaKey: false, preventDefault() {}, ...extra });
  sandbox.onKeyDown(key("KeyA", "a"));
  sandbox.onKeyDown(key("KeyQ", "\u0439"));
  sandbox.onKeyDown(key("KeyC", "c", { metaKey: true }));
  sandbox.onKeyDown(key("Enter", "Enter"));
  assert.deepEqual(sent, [{ t: "text", text: "a" }, { t: "text", text: "\u0439" }]);
  // The text box and a paste send text of at most 300 bytes.
  sent.length = 0;
  run("textInput.value=\"hello\"");
  await run("sendText()");
  run(`textInput.value=${JSON.stringify("x".repeat(301))}`);
  await run("sendText()");
  assert.deepEqual(sent, [{ t: "text", text: "hello" }]);
  assert.equal(run("textInput.value.length"), 301, "the text box lost text it did not send");
  sandbox.onPaste({ clipboardData: { getData: () => "pasted" }, preventDefault() {} });
  assert.deepEqual(sent.at(-1), { t: "text", text: "pasted" });
  // Once the state says the Simulator clipboard is reachable, long text and a paste are pastes.
  run("applyState({t:\"state\",owner:\"shared\",paused:false,session:\"streaming\",clients:{video:1,control:1},transport:\"idb\",orientation:\"PORTRAIT\",rotation:0,clipboard:true})");
  assert.match(document.getElementById("text-hint").textContent, /Plain ASCII up to 300 bytes is typed .* other text is set on its clipboard/);
  sent.length = 0;
  await run("sendText()");
  run("textInput.value=\"short\"");
  await run("sendText()");
  // Text idb cannot type (anything but printable ASCII and newline) goes as a paste too.
  run("textInput.value=\"caf\\u00e9\"");
  await run("sendText()");
  sandbox.onPaste({ clipboardData: { getData: () => "pasted" }, preventDefault() {} });
  assert.deepEqual(sent, [{ t: "paste", text: "x".repeat(301) }, { t: "text", text: "short" },
    { t: "paste", text: "caf\u00e9" }, { t: "paste", text: "pasted" }]);
  assert.equal(run("textInput.value"), "");
  run("onControlMessage({t:\"paste\",typed:false,message:\"The Simulator clipboard was set\"})");
  assert.equal(run("view.note"), "The Simulator clipboard was set");
  run("render()");
  assert.equal(document.getElementById("stream-transport").textContent, "idb · WebCodecs");
  assert.equal(run("transportLabel()"), "idb (webcodecs)");
  assert.equal(run("window.autonomCanvas.stats().rotation"), 0);
  // Without VideoDecoder (as in this sandbox) the idb page shows the multipart picture and keeps
  // its input on the control socket.
  run("view.mode=null;applyTransport()");
  assert.equal(run("view.mode"), "multipart");
  assert.equal(run("view.reason"), "VideoDecoder is not available in this browser");
  assert.equal(run("transportLabel()"), "idb (multipart: VideoDecoder is not available in this browser)");
  assert.equal(run("controlSocket!==null"), true);
});

/**
 * Checks the page of `html` on `transport`: decoded frames are shown in order, one per
 * animation frame, with at most two waiting; the oldest beyond that is closed and counted
 * as dropped, and frames still waiting are closed when the page leaves WebCodecs.
 */
function assertFramesShownInOrder(html, transport, size) {
  const { sandbox, run } = runPage(html);
  const frames = [];
  const animationFrames = [];
  const drawn = [];
  sandbox.frame = (id) => {
    const made = { id, displayWidth: size.width, displayHeight: size.height, closed: false, close() { this.closed = true; } };
    frames.push(made);
    return made;
  };
  sandbox.requestAnimationFrame = (callback) => animationFrames.push(callback);
  sandbox.drawn = drawn;
  sandbox.performance = performance;
  run("ctx.drawImage=(frame)=>drawn.push(frame.id);ctx.setTransform=()=>{};ctx.rotate=()=>{};");
  const refresh = () => animationFrames.splice(0).forEach((callback) => callback());
  const counts = () => JSON.parse(run("JSON.stringify([stats.framesDecoded,stats.framesRendered,stats.framesDropped])"));
  const closed = () => frames.filter((made) => made.closed).map((made) => made.id);

  run(`view.transport=${JSON.stringify(transport)};view.mode="webcodecs";`);
  // Two frames decoded between two refreshes are both shown, one per refresh, in order.
  run("onFrame(frame(1));onFrame(frame(2))");
  assert.equal(animationFrames.length, 1, "one animation frame is asked for at a time");
  refresh();
  assert.deepEqual(drawn, [1]);
  assert.equal(animationFrames.length, 1, "no animation frame was asked for the frame still waiting");
  refresh();
  assert.deepEqual(drawn, [1, 2]);
  assert.equal(animationFrames.length, 0, "an animation frame was asked for with nothing waiting");
  assert.deepEqual(counts(), [2, 2, 0]);
  assert.deepEqual(closed(), [1, 2], "a drawn frame was not closed");
  // A third frame before the refresh: the oldest is closed and counted as dropped.
  run("onFrame(frame(3));onFrame(frame(4));onFrame(frame(5))");
  assert.deepEqual(closed(), [1, 2, 3]);
  assert.equal(run("frameQueue.length"), 2);
  refresh();
  refresh();
  refresh();
  assert.deepEqual(drawn, [1, 2, 4, 5]);
  assert.equal(animationFrames.length, 0);
  assert.deepEqual(counts(), [5, 4, 1]);
  assert.equal(run("window.autonomCanvas.stats().framesRendered"), 4);
  assert.equal(run("window.autonomCanvas.stats().framesDropped"), 1);
  // A frame decoded while another waits for its refresh asks for no second animation frame.
  run("onFrame(frame(6))");
  assert.equal(animationFrames.length, 1);
  run("onFrame(frame(7))");
  assert.equal(animationFrames.length, 1, "a second animation frame was asked for while one was pending");
  refresh();
  refresh();
  assert.deepEqual(drawn, [1, 2, 4, 5, 6, 7]);
  assert.deepEqual(counts(), [7, 6, 1]);
  // Frames still waiting are closed when the page leaves WebCodecs.
  run("onFrame(frame(8));onFrame(frame(9));startMultipart(\"test\")");
  assert.deepEqual(closed(), [1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.equal(run("frameQueue.length"), 0);
  refresh();
  assert.deepEqual(drawn, [1, 2, 4, 5, 6, 7], "a closed frame was drawn");
  assert.deepEqual(counts(), [9, 6, 1], "frames closed by leaving WebCodecs were counted as dropped");
}

test("ios: the page shows decoded frames in order on idb with at most two waiting", async (t) => {
  const world = await iosWorld(t);
  const canvas = await startCanvas(world, ["--platform", "ios", "--target", IOS_UDID, "--simctl", world.xcrun]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  await stopCanvas(canvas);
  assertFramesShownInOrder(html, "idb", { width: 1206, height: 2622 });
});

test("page: the Android page shows decoded scrcpy frames in order with at most two waiting, not only the newest", async (t) => {
  const { html } = await loadPage(t);
  assert.doesNotMatch(html, /pendingFrame|presentInOrder/, "the newest-frame-only path is still in the page");
  assertFramesShownInOrder(html, "scrcpy", { width: 576, height: 1280 });
});

describe("tests that wait on Canvas timers, run side by side", { concurrency: true }, () => {
  for (const [name, fn] of waitingTests) test(name, fn);
});
