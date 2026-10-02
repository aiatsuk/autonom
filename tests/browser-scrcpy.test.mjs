// End-to-end tests of the Canvas scrcpy transport against fakes only: a fake adb
// script, an in-process fake scrcpy device behind the fake forward port, and a
// fake journal bridge. No real adb device, emulator or browser is involved.
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { rmSync, writeFileSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import { Script, createContext } from "node:vm";

import {
  HEALTHY_STREAM_MS,
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

const FAKE_ADB = String.raw`
import { appendFileSync } from "node:fs";
import { connect } from "node:net";

const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_ADB_LOG, JSON.stringify(args) + "\n");
const rest = args[0] === "-s" ? args.slice(2) : args;
const [command, ...tail] = rest;
const PNG = Buffer.from(process.env.FAKE_PNG, "base64");

if (command === "get-state") {
  console.log("device");
} else if (command === "exec-out" && tail[0] === "screencap") {
  process.stdout.write(PNG);
} else if (command === "exec-out" && tail[0] === "screenrecord") {
  if (process.env.FAKE_SCREENRECORD === "h264" && tail.includes("--output-format=h264")) {
    process.stdout.write(Buffer.from([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x29, 0, 0, 0, 1, 0x68, 0xce]));
  } else {
    console.error("screenrecord: unrecognized option '--output-format=h264'");
    process.exitCode = 1;
  }
} else if (command === "shell" && tail.join(" ") === "screenrecord --help") {
  // Android 16 (API 36) help text: the hidden --output-format option is not listed.
  console.log("Usage: screenrecord [options] <filename>\n--size WIDTHxHEIGHT\n--bit-rate RATE\n--time-limit TIME");
} else if (command === "shell" && tail.join(" ") === "wm size") {
  console.log("Physical size: 1080x2400");
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
  // nothing to do
} else {
  console.error("unsupported fake adb command: " + rest.join(" "));
  process.exitCode = 2;
}
`;

const FAKE_BRIDGE = String.raw`
import { appendFileSync, existsSync } from "node:fs";
import { createInterface } from "node:readline";

// While this file exists, record answers wait: a journal slower than its input.
const HOLD = process.env.FAKE_BRIDGE_LOG + ".hold";
// A slow disk: each record is answered this long after it arrives.
const RECORD_MS = Number(process.env.FAKE_BRIDGE_RECORD_MS ?? 0);
const held = [];
const answer = (id, result) => process.stdout.write(JSON.stringify({ id, ok: true, result }) + "\n");
setInterval(() => {
  while (held.length && !existsSync(HOLD)) answer(...held.shift());
}, 20).unref();

createInterface({ input: process.stdin }).on("line", (line) => {
  const message = JSON.parse(line);
  appendFileSync(process.env.FAKE_BRIDGE_LOG, line + "\n");
  let result = { ok: true };
  if (message.op === "screen-size") result = { ok: true, display: { width: 1080, height: 2400 } };
  if (message.op === "record") result = { ok: true, recorded: message.payload.kind, via: "scrcpy" };
  if (message.op === "record" && (held.length || existsSync(HOLD))) held.push([message.id, result]);
  else if (message.op === "record" && RECORD_MS) setTimeout(() => answer(message.id, result), RECORD_MS);
  else answer(message.id, result);
});
`;

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
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
    directory, bin, adbLog, bridgeLog, fakeAdb, fakeBridge, serverFile, device: fake, children: [],
    env: {
      // Only the temporary bin directory: no real adb, scrcpy or ffmpeg can be found.
      PATH: bin,
      HOME: directory,
      AUTONOM_HOME: join(directory, "autonom-home"),
      FAKE_ADB_LOG: adbLog,
      FAKE_BRIDGE_LOG: bridgeLog,
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

async function pageSocket(canvas, auth, path) {
  const opened = await openSocket(canvas.port, `${path}?csrf=${encodeURIComponent(auth.csrf)}`, {
    headers: { Cookie: auth.cookie, Origin: canvas.origin },
  });
  assert.equal(opened.status, 101, opened.body);
  return opened.ws;
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

/**
 * Run `work` while sampling the Canvas process: its resident memory, and GET /status,
 * which must keep answering. Returns the peak memory growth over the start.
 */
async function watchCanvas(canvas, work) {
  const before = await residentBytes(canvas.child.pid);
  let peak = before;
  const latencies = [];
  let done = false;
  const sample = async () => {
    peak = Math.max(peak, await residentBytes(canvas.child.pid));
    const started = Date.now();
    await statusWithin(canvas, 10_000);
    return Date.now() - started;
  };
  const sampling = (async () => {
    while (!done) {
      latencies.push(await sample());
      await sleep(100);
    }
  })();
  try {
    await work();
  } finally {
    done = true;
    await sampling;
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

/** Messages on one connection are handled in order, so this pong means all before it were. */
async function allHandled(ws, timeoutMs = 20_000) {
  const ts = -Date.now();
  const pong = ws.until((message) => message.t === "pong" && message.ts === ts, timeoutMs);
  ws.send({ t: "ping", ts });
  await pong;
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
const RECORD_KINDS = ["gesture", "scroll", "key", "text", "paste", "system", "control"];

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

  const spawns = (await adbCalls(world)).filter((args) => args[2] === "shell" && args[3]?.includes("app_process"));
  assert.equal(spawns.length, 1, "more than one scrcpy-server was started");
  assert.equal(device.spawns.length, 1);
  assert.match(spawns[0][3], /^CLASSPATH=\/data\/local\/tmp\/autonom-scrcpy-4\.1\.jar app_process \/ com\.genymobile\.scrcpy\.Server 4\.1 scid=[0-9a-f]{8} /);
  for (const option of ["tunnel_forward=true", "audio=false", "control=true", "cleanup=true", "max_size=1280",
    "video_bit_rate=8000000", "max_fps=60", "video_codec=h264", "clipboard_autosync=false"]) {
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

test("fanout: the session restarts after the device server dies and stops when idle", async (t) => {
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
  assert.ok(!calls.some((args) => args[3]?.includes("app_process")), "a server was started after the stop");
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
  assert.equal(count((args) => args[3]?.includes("app_process")), 1);
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

test("input: touches map to the current video size and every interruption lifts the pointer", async (t) => {
  const { canvas, auth, device, control } = await connectedControl(t);
  const touch = (a, id, x, y, extra = {}) => control.send({ t: "touch", a, id, x, y, ...extra });
  touch("down", 1, 0.5, 0.5);
  touch("move", 1, 0.25, 0.75);
  await waitFor(() => device.touches().length === 2, 3000, "down and move");
  let [down, move] = device.touches();
  assert.deepEqual([down.action, down.x, down.y, down.width, down.height, down.pressure],
    [0, 285, 640, 570, 1280, 0xffff]);
  assert.deepEqual([move.action, move.x, move.y], [2, 142, 960]);
  assert.equal(move.pointerId, down.pointerId);

  // Rotation with the finger down: scrcpy-server ignores events made for the old size, so
  // the Canvas lifts the finger itself with the new size and refuses its later moves.
  device.sendSession(1280, 570);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280);
  await waitFor(() => device.touches().length === 3, 3000, "the finger lifted with the new size");
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
  await waitFor(() => device.touches().length === 5, 3000, "a gesture after the rotation");
  const [rotated, up] = device.touches().slice(3);
  assert.deepEqual([rotated.action, rotated.x, rotated.y, rotated.width, rotated.height], [0, 640, 285, 1280, 570]);
  assert.deepEqual([up.action, up.pressure], [1, 0]);
  assert.equal(control.json("error").length, 1);

  // Multitouch: two pointers, distinct device ids, both lifted.
  touch("down", 5, 0.2, 0.2);
  touch("down", 6, 0.8, 0.8);
  touch("up", 5, 0.2, 0.2);
  touch("cancel", 6, 0.8, 0.8);
  await waitFor(() => device.touches().length === 10, 3000, "multitouch");
  const multi = device.touches().slice(5);
  assert.notEqual(multi[0].pointerId, multi[1].pointerId);
  assert.deepEqual(multi.map((message) => message.action), [0, 0, 1, 3, 1]);

  // Close mid-drag: the Canvas sends the up itself.
  const other = await pageSocket(canvas, auth, "/ws/control");
  await other.next((message) => message.json?.t === "state");
  other.send({ t: "touch", a: "down", id: 9, x: 0.1, y: 0.1 });
  await waitFor(() => device.touches().length === 11, 3000, "second client down");
  other.close();
  await waitFor(() => device.touches().length === 12, 3000, "up after disconnect");
  assert.equal(device.touches()[11].action, 1);
  assert.equal(device.touches()[11].pointerId, device.touches()[10].pointerId);

  // Pause mid-drag through the WebSocket.
  touch("down", 1, 0.5, 0.5);
  await waitFor(() => device.touches().length === 13, 3000, "down before pause");
  control.send({ t: "control", mode: "pause" });
  await waitFor(() => device.touches().length === 14, 3000, "up after pause");
  assert.equal(device.touches()[13].action, 1);
  const paused = control.reply((message) => message.t === "error");
  touch("move", 1, 0.5, 0.6);
  assert.match((await paused).message, /paused/);
  control.send({ t: "control", mode: "resume" });
  await control.next((message) => message.json?.t === "state" && message.json.paused === false);

  // An agent takes over through HTTP mid-drag: the human pointer is lifted and refused.
  touch("down", 1, 0.5, 0.5);
  await waitFor(() => device.touches().length === 15, 3000, "down before takeover");
  const takeover = await fetch(`${canvas.origin}/control`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Autonom-Origin": "agent" },
    body: JSON.stringify({ mode: "takeover" }),
  });
  assert.equal(takeover.status, 200);
  await waitFor(() => device.touches().length === 16, 3000, "up after takeover");
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
  await waitFor(() => device.messages.filter((message) => message.keycode === 3).length === 2, 3000,
    "agent input");
  agent.ws.send({ t: "control", mode: "release" });
  await control.next((message) => message.json?.t === "state" && message.json.owner === "shared");

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
  await waitFor(() => device.messages.length === count + 13, 3000, "control messages");
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
  await waitFor(() => gets() === 2, 3000, "the agent request");
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
  await waitFor(() => device.messages.length === 2, 3000, "the held pointer and key");
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
  await waitFor(() => device.count("text") === (answered + 1) * perRound, 5000, "every text on the device");
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
  await waitFor(() => device.touches().length === 4, 3000, "input after the device read again");
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
  await waitFor(() => device.count("set-clipboard") === 1, 3000, "the paste");
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
  await waitFor(() => device.messages.length === 6, 3000, "the held input");
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
    await waitFor(() => device.messages.length === before + messages.length, 5000, "the burst on the device");
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
  await waitFor(() => device.messages.length === 4, 5000, "the burst on the device");
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

test("input: a burst of 300 one-character Unicode texts gets 300 clipboard pastes in order and 300 journal records, none dropped", async (t) => {
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
  await waitFor(() => device.messages.length === 2, 5000, "the input after the paste");
  const waited = device.arrivals[1] - device.arrivals[0];
  assert.ok(waited >= 950 && waited < 3000, `input followed an unacknowledged paste after ${waited} ms`);
  assert.deepEqual(deviceLabels(device.messages), ["paste q", "text after"]);
  assert.match((await status(canvas)).last_error, /did not acknowledge/);
  // Nothing is held any more: input without the clipboard goes straight on.
  const sentAt = Date.now();
  control.send({ t: "text", text: "more" });
  await waitFor(() => device.messages.length === 3, 3000, "input after the timeout");
  assert.ok(device.arrivals[2] - sentAt < 500, "input after the timeout was held");
  assert.deepEqual(control.json("error"), []);
  assert.equal((await stopCanvas(canvas)).code, 0);
});

test("input: a device server restart while input waits for a clipboard ack refuses that input at once and holds nothing after it", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "paste", text: "é" });
  await waitFor(() => device.count("set-clipboard") === 1, 3000, "the paste");
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
  const refused = await waitFor(() => control.json("error").length >= 3 && control.json("error"), 3000,
    "the held input refused");
  assert.ok(Date.now() - restartedAt < 500, "held input waited past the restart");
  assert.deepEqual(refused.map((error) => error.for), ["key", "key", "text"]);
  for (const error of refused) assert.match(error.message, /not ready/);
  await streaming;
  await waitFor(() => device.connected, 5000, "the new device server");
  const before = device.messages.length;
  const sentAt = Date.now();
  control.send({ t: "text", text: "c" });
  await waitFor(() => device.messages.length === before + 1, 3000, "input after the restart");
  assert.ok(device.arrivals[before] - sentAt < 500, "input after the restart was held");
  assert.deepEqual(deviceLabels(device.messages), ["paste é", "text c"]);
  assert.equal((await stopCanvas(canvas)).code, 0);
});

test("input: a takeover while input waits for a clipboard ack lifts the pointer after the paste and refuses the input held behind it", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { device: { autoAck: false } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "paste", text: "ü" });
  await waitFor(() => device.messages.length === 2, 3000, "the pointer and the paste");
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
  const refused = waitFor(() => control.json("error").length === 2 && control.json("error"), 3000,
    "the held input refused");
  device.ack(device.messages[1].sequence);
  await waitFor(() => device.messages.length === 3, 3000, "the lifted pointer");
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
  }, 5000, "the journal records");
  assert.deepEqual(recordSummaries(records), ["human paste", "human gesture"]);
  assert.deepEqual([records[1].payload.pointers, records[1].payload.moves], [1, 0]);
  await stopCanvas(canvas);
});

test("input: a rotation lifts a finger still down with the new size, sends an up made just before it again, and refuses the lifted finger's moves", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t, { device: { dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "move", id: 1, x: 0.5, y: 0.6 });
  await waitFor(() => device.touches().length === 2, 3000, "the finger down");
  const [down] = device.touches();
  assert.deepEqual([...device.pointersDown], [down.pointerId]);
  // The device rotates with the finger down: events made for the old size would be ignored.
  device.sendSession(1280, 570);
  await waitFor(() => device.pointersDown.size === 0, 3000, "the finger lifted on the device");
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
  await waitFor(() => device.pointersDown.size === 1, 3000, "the second finger down");
  device.resize(570, 1280);
  control.send({ t: "touch", a: "up", id: 2, x: 0.2, y: 0.2 });
  await waitFor(() => device.ignored.length === 1, 3000, "the up made for the old size");
  assert.equal(device.pointersDown.size, 1);
  device.sendSession();
  await waitFor(() => device.pointersDown.size === 0, 3000, "the up sent again with the new size");
  const resent = device.touches().at(-1);
  assert.deepEqual([resent.action, resent.x, resent.y, resent.width, resent.height], [1, 114, 256, 570, 1280]);

  const records = await waitFor(async () => {
    const found = await journaled(world, "gesture");
    return found.length === 2 && found;
  }, 5000, "one record per gesture");
  assert.deepEqual(records.map(({ payload }) => [payload.pointers, payload.moves]), [[1, 1], [1, 0]]);
  assert.equal(control.json("error").length, 1);
  await stopCanvas(canvas);
});

test("input: an up written late, behind a paste, counts as recent at a rotation from when it was written", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { autoAck: false, dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => device.pointersDown.size === 1, 3000, "the finger down");
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, 3000, "the paste");
  // The device rotates; its server says so only later.
  device.resize(1280, 570);
  // An agent takes over: the finger is lifted, but its up waits behind the paste, which
  // the device never acknowledges, so for 1 s, and then goes out made for the old size.
  await agentTakeover(canvas);
  // The up was made before the takeover answered, and written no later than it arrived.
  const madeBy = Date.now();
  await waitFor(() => device.ignored.length === 1, 3000, "the up, written after the paste for the old size");
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
  await waitFor(() => device.pointersDown.size === 0, 3000, "the up sent again with the new size");
  const resent = device.touches().at(-1);
  assert.deepEqual([resent.action, resent.width, resent.height], [1, 1280, 570]);
  await stopCanvas(canvas);
});

test("input: an up written more than a second before the video size changes is not sent again", async (t) => {
  const { canvas, device, control } = await connectedControl(t, { device: { dropStaleSize: true } });
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  await waitFor(() => device.touches().length === 2 && device.pointersDown.size === 0, 3000, "a tap");
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
  await waitFor(() => device.pointersDown.size === 1, 3000, "the finger down");
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, 3000, "the paste");
  const [paste] = device.messages.filter((message) => message.type === "set-clipboard");
  await agentTakeover(canvas);
  // The device rotates, and says so, while the finger's up still waits behind the paste.
  device.sendSession(1280, 570);
  await control.next((message) => message.json?.t === "state" && message.json.width === 1280);
  assert.equal(device.touches().length, 1, "the up went out before the paste was acknowledged");
  device.ack(paste.sequence);
  await waitFor(() => device.pointersDown.size === 0, 3000, "the up sent again with the new size");
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
  await waitFor(() => device.pointersDown.size === 1 && device.keysDown.size === 1, 3000, "the finger and key down");
  const [down] = device.touches();
  control.send({ t: "paste", text: "x" });
  await waitFor(() => device.count("set-clipboard") === 1, 3000, "the paste");
  // The takeover lifts them, but their up and key-up wait behind the paste...
  const pasted = device.controlBytes;
  await agentTakeover(canvas);
  await expectNoDeviceWrite(device, pasted, 150);
  // ...when the device server dies, before any ack.
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.pointersDown.size === 0 && device.keysDown.size === 0, 5000,
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
  await waitFor(() => device.pointersDown.size === 1 && device.keysDown.size === 1, 3000, "the finger and key down");
  // A stuck Controller: the up and key-up are written but never read by this server.
  device.running.control.pause();
  control.send({ t: "touch", a: "up", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "up", code: 59 });
  await sleep(150);
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.pointersDown.size === 0 && device.keysDown.size === 0, 5000,
    "the finger and key lifted through the restarted server");
  assert.deepEqual(control.json("error"), []);
  await stopCanvas(canvas);
});

test("input: a finger and a key held when the device server dies are lifted through the restarted server once its video size is known", async (t) => {
  const { world, canvas, device, control } = await connectedControl(t);
  control.send({ t: "touch", a: "down", id: 1, x: 0.5, y: 0.5 });
  control.send({ t: "key", a: "down", code: 59 });
  await waitFor(() => device.messages.length === 2, 3000, "the held finger and key");
  const [down] = device.touches();
  const restarting = control.reply((message) => message.t === "state" && message.session === "restarting");
  device.crash();
  await restarting;
  const before = device.messages.length;
  await control.reply((message) => message.t === "state" && message.session === "streaming", 8000);
  await waitFor(() => device.messages.length === before + 2, 5000, "the lifts through the restarted server");
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
  await waitFor(() => device.messages.length === before + 4, 3000, "a new gesture");
  await waitFor(async () => (await journaled(world)).length >= 3, 5000, "the journal records");
  await sleep(300);
  // The interrupted gesture and key are journaled once, when the server died.
  assert.deepEqual(recordSummaries(await journaled(world)), ["human gesture", "human key 59", "human gesture"]);
  await stopCanvas(canvas);
});

/**
 * The Canvas page script, run in a node:vm sandbox with just enough of a browser to load
 * (its sign-in never answers), on the scrcpy transport with an open control socket.
 * `sent` collects the control messages it sends; `run` evaluates code inside the page.
 */
async function loadPage(t) {
  const world = await makeWorld(t);
  const canvas = await startCanvas(world, ["--scrcpy-server", world.serverFile]);
  const html = await (await fetch(`${canvas.origin}/`)).text();
  await stopCanvas(canvas);
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const element = () => ({ addEventListener() {}, getContext: () => ({}), style: {}, dataset: {} });
  const sent = [];
  const sandbox = {
    location: { hash: "", pathname: "/", search: "", protocol: "http:", host: "127.0.0.1:1" },
    history: { replaceState() {} },
    document: { getElementById: element, querySelectorAll: () => [] },
    fetch: () => new Promise(() => {}),
    URLSearchParams, TextEncoder, setTimeout, clearTimeout, setInterval, clearInterval,
    record: (text) => sent.push(JSON.parse(text)),
  };
  sandbox.window = sandbox;
  const page = createContext(sandbox);
  new Script(script).runInContext(page);
  const run = (code) => new Script(code).runInContext(page);
  run("view.transport=\"scrcpy\";controlSocket={readyState:1,send:record};");
  return { sandbox, sent, run };
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
    await waitFor(() => clipboardWrites(device).length === cycle, 5000, `paste ${cycle}`);
    // While this paste waits: 600 pings, the next paste, and one message after it, which
    // stays held behind that paste when the rest has been handled.
    sendBurst(control, [...Array.from({ length: 600 }, (_, i) => ({ t: "ping", ts: cycle * 1000 + i })),
      { t: "paste", text: "\u00e9" }, { t: "ping", ts: -cycle }]);
  }
  await waitFor(() => clipboardWrites(device).length === cycles + 1, 5000, "the last paste");
  await waitFor(() => control.json("pong").some((message) => message.ts === -cycles), 5000, "the last held message");
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
  for (const ws of [deaf, blind]) {
    ws.socket.resume();
    await quiet(ws);
    assert.equal(ws.json("state").at(-1).paused, false, "a client that fell behind missed the current state");
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
