// ChatGPT probe device relay (contract section 4): createDeviceSource with fake ScrcpySession,
// IdbCompanion and resolver factories. The pure Canvas libraries (scrcpy-lib.mjs and the
// ios-idb-companion.mjs constants) are the real ones, imported read-only. No device commands run.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test, { describe } from "node:test";
import { fileURLToPath } from "node:url";

import { configAnnexB } from "../integrations/chatgpt/autonom-chatgpt-probe/media/h264-pcm.mjs";
import {
  ANDROID_BIT_RATE,
  ANDROID_MAX_FPS,
  ANDROID_MAX_SIZE,
  DESCRIBE_TIMEOUT_MS,
  IOS_FPS,
  createDeviceSource,
} from "../integrations/chatgpt/autonom-chatgpt-probe/media/device-source.mjs";
import { DEFAULT_VIDEO_OPTIONS } from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";
import { MOTION_ACTION, POINTER_ID, encodeTouch, h264CodecString } from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-lib.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPTS_DIR = join(ROOT, "plugins", "autonom", "skills", "android-emulator-browser", "scripts");
const ANDROID = "android:emulator-5584";
const UDID = "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5";
const IOS = `ios:${UDID}`;
const CONFIG = configAnnexB({ width: 96, height: 208 });
const CONFIG_B = configAnnexB({ width: 112, height: 240 });
const IDR = Buffer.from([0, 0, 0, 1, 0x65, 0x88, 0x84, 0x00]);
const DELTA = Buffer.from([0, 0, 0, 1, 0x41, 0x9a, 0x02, 0x03]);

function tick() {
  return new Promise((done) => setImmediate(done));
}

function errorCode(code, pattern) {
  return (error) => {
    assert.equal(error.code, code);
    if (pattern) assert.match(error.message, pattern);
    return true;
  };
}

// ---------- fakes ----------

function androidFakes(behaviour = {}) {
  const log = { resolved: [], sessions: [] };
  class FakeScrcpySession extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.sent = [];
      this.stops = 0;
      this.connected = true;
      this.sendResults = [...(behaviour.sendResults ?? [])];
      log.sessions.push(this);
    }

    async start() {
      if (behaviour.startError) throw behaviour.startError;
      if (behaviour.startGate) await behaviour.startGate;
      this.emit("state", "streaming");
    }

    send(buffer) {
      this.sent.push(Buffer.from(buffer));
      return this.sendResults.length ? this.sendResults.shift() : this.connected;
    }

    async stop() {
      this.stops += 1;
    }
  }
  const factories = {
    ScrcpySession: FakeScrcpySession,
    resolveScrcpyServer: async (args) => {
      log.resolved.push(args);
      return behaviour.resolved ?? { ok: true, source: "AUTONOM_SCRCPY_SERVER", serverPath: "/fake/scrcpy-server-v4.1", version: "4.1" };
    },
  };
  return { log, factories };
}

class FakeVideoStream {
  constructor(options) {
    this.options = options;
    this.queue = [];
    this.waiter = null;
    this.done = false;
    this.error = null;
    this.stops = 0;
  }

  push(frame) {
    if (this.waiter) {
      const { resolve: done } = this.waiter;
      this.waiter = null;
      done({ value: frame, done: false });
    } else {
      this.queue.push(frame);
    }
  }

  end() {
    this.done = true;
    if (this.waiter) {
      const { resolve: done } = this.waiter;
      this.waiter = null;
      done({ value: undefined, done: true });
    }
  }

  fail(error) {
    this.error = error;
    if (this.waiter) {
      const { reject } = this.waiter;
      this.waiter = null;
      reject(error);
    }
  }

  async stop() {
    this.stops += 1;
    this.end();
  }

  next() {
    if (this.queue.length) return Promise.resolve({ value: this.queue.shift(), done: false });
    if (this.error) return Promise.reject(this.error);
    if (this.done) return Promise.resolve({ value: undefined, done: true });
    return new Promise((done, reject) => {
      this.waiter = { resolve: done, reject };
    });
  }

  async return() {
    await this.stop();
    return { value: undefined, done: true };
  }

  [Symbol.asyncIterator]() {
    return this;
  }
}

function iosFakes(behaviour = {}) {
  const log = { resolved: [], companions: [], streams: [], describes: [], taps: [] };
  const client = {
    async openVideoStream(options) {
      if (behaviour.streamGate) await behaviour.streamGate;
      if (behaviour.streamError) throw behaviour.streamError;
      const stream = new FakeVideoStream(options);
      log.streams.push(stream);
      return stream;
    },
    async describe(options) {
      log.describes.push(options);
      if (behaviour.describeError) throw behaviour.describeError;
      return { screen: behaviour.screen ?? { width: 1179, height: 2556, density: 3, widthPoints: 393, heightPoints: 852 } };
    },
    hid() {
      return {
        async tap(x, y) {
          log.taps.push([x, y]);
          if (behaviour.tapError) throw behaviour.tapError;
        },
      };
    },
  };
  class FakeIdbCompanion extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.stops = 0;
      log.companions.push(this);
    }

    async start() {
      if (behaviour.startError) throw behaviour.startError;
      return client;
    }

    async stop() {
      this.stops += 1;
      return { code: 0, signal: null, exited: true };
    }
  }
  const factories = {
    IdbCompanion: FakeIdbCompanion,
    resolveIdbCompanionBinary: async (args) => {
      log.resolved.push(args);
      return behaviour.resolved ?? { available: true, path: "/fake/idb_companion", source: "AUTONOM_IDB_COMPANION_BIN", reason: null };
    },
  };
  return { log, factories };
}

function record(source) {
  const events = { config: [], frame: [], error: [], state: [] };
  for (const name of Object.keys(events)) source.on(name, (value) => events[name].push(value));
  return events;
}

// ---------- refusals ----------

describe("createDeviceSource refusals", () => {
  test("android:emulator-5554 is refused with device_not_allowed even with factories", () => {
    const { factories } = androidFakes();
    assert.throws(
      () => createDeviceSource({ spec: "android:emulator-5554", adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories }),
      errorCode("device_not_allowed", /emulator-5554/),
    );
  });

  test("invalid specs are refused with bad_source", () => {
    const bad = [
      undefined,
      null,
      42,
      "",
      "fixture",
      "android:",
      "android:has space",
      `android:${"a".repeat(129)}`,
      "ios:",
      "ios:1234",
      `ios:${"G".repeat(36)}`,
      `ios:${UDID}0`,
      "usb:emulator-5584",
      " android:emulator-5584",
    ];
    for (const spec of bad) {
      assert.throws(() => createDeviceSource({ spec, scriptsDir: SCRIPTS_DIR }), errorCode("bad_source", /android:<serial> or ios:<udid>/), String(spec));
    }
    assert.throws(() => createDeviceSource(), errorCode("bad_source"));
  });

  test("a missing scripts dir is refused with relay_unavailable", () => {
    assert.throws(() => createDeviceSource({ spec: ANDROID }), errorCode("relay_unavailable", /scripts directory/));
    assert.throws(() => createDeviceSource({ spec: IOS, scriptsDir: "" }), errorCode("relay_unavailable"));
  });

  test("a scripts dir without the Canvas modules fails start with relay_unavailable", async (t) => {
    const empty = mkdtempSync(join(tmpdir(), "probe-relay-empty-"));
    t.after(() => rmSync(empty, { recursive: true, force: true }));
    for (const spec of [ANDROID, IOS]) {
      const source = createDeviceSource({ spec, scriptsDir: empty });
      const events = record(source);
      await assert.rejects(source.start(), errorCode("relay_unavailable", /scrcpy-lib\.mjs could not be loaded/));
      assert.equal(source.state, "failed");
      assert.deepEqual(events.state, ["starting", "failed"]);
      await source.stop();
    }
  });
});

// ---------- Android ----------

describe("Android relay through ScrcpySession", () => {
  test("start builds one session with the contract options and resolves the server from the environment", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    assert.equal(log.resolved.length, 1);
    assert.equal(log.resolved[0].env, process.env);
    assert.equal(typeof log.resolved[0].findExecutable, "function");
    assert.equal(log.sessions.length, 1);
    assert.deepEqual(log.sessions[0].options, {
      adbPath: "/fake/adb",
      serial: "emulator-5584",
      serverPath: "/fake/scrcpy-server-v4.1",
      version: "4.1",
      maxSize: 1280,
      bitRate: 8_000_000,
      maxFps: 30,
    });
    assert.deepEqual([ANDROID_MAX_SIZE, ANDROID_BIT_RATE, ANDROID_MAX_FPS], [1280, 8_000_000, 30]);
    assert.deepEqual(events.state, ["starting", "streaming"]);
    assert.equal(source.spec, ANDROID);
    await source.stop();
  });

  test("a null adbPath falls back to adb on PATH", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: "android:R5CT1234ABC", adbPath: null, scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    assert.equal(log.sessions[0].options.adbPath, "adb");
    assert.equal(log.sessions[0].options.serial, "R5CT1234ABC");
    await source.stop();
  });

  test("config maps to the codec string and the session size; packets map to frames with key flags", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    const session = log.sessions[0];
    session.emit("session", { width: 1080, height: 2400 });
    session.emit("config", CONFIG);
    const before = Date.now();
    session.emit("packet", { keyFrame: true, pts: 0, data: IDR });
    session.emit("packet", { keyFrame: false, pts: 33_000, data: DELTA });
    session.emit("packet", { keyFrame: false, pts: 66_000 });
    assert.equal(events.config.length, 1);
    const config = events.config[0];
    assert.equal(config.codec, h264CodecString(CONFIG));
    assert.equal(config.codec, "avc1.42C01E");
    assert.deepEqual([config.width, config.height], [1080, 2400]);
    assert.ok(Buffer.isBuffer(config.annexB) && config.annexB.equals(CONFIG));
    assert.equal(events.frame.length, 2, "a packet without data is dropped");
    assert.deepEqual(events.frame.map((frame) => frame.key), [true, false]);
    assert.ok(events.frame[0].data.equals(IDR) && events.frame[1].data.equals(DELTA));
    for (const frame of events.frame) assert.ok(typeof frame.tMs === "number" && frame.tMs >= before && frame.tMs <= Date.now());
    // A rotation sends a new session size and a new config.
    session.emit("session", { width: 2400, height: 1080 });
    session.emit("config", CONFIG_B);
    assert.deepEqual([events.config[1].width, events.config[1].height], [2400, 1080]);
    assert.ok(events.config[1].annexB.equals(CONFIG_B));
    await source.stop();
  });

  test("a config without an SPS carries a null codec", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    log.sessions[0].emit("config", Buffer.from([0, 0, 0, 1, 0x68, 0xce, 0x38, 0x80]));
    assert.equal(events.config[0].codec, null);
    assert.equal(events.config[0].width, null);
    await source.stop();
  });

  test("tap sends one DOWN and one UP touch with scaled coordinates", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    const session = log.sessions[0];
    session.emit("session", { width: 1080, height: 2400 });
    assert.deepEqual(await source.tap({ x: 0.25, y: 0.5 }), { ok: true });
    const expected = (action, pressure, x, y) => encodeTouch({ action, pointerId: POINTER_ID.GENERIC_FINGER, x, y, width: 1080, height: 2400, pressure });
    assert.equal(session.sent.length, 2);
    assert.ok(session.sent[0].equals(expected(MOTION_ACTION.DOWN, 1, 270, 1200)));
    assert.ok(session.sent[1].equals(expected(MOTION_ACTION.UP, 0, 270, 1200)));
    // The far edge stays on the screen: 1.0 maps to the last pixel.
    assert.deepEqual(await source.tap({ x: 1, y: 0 }), { ok: true });
    assert.ok(session.sent[2].equals(expected(MOTION_ACTION.DOWN, 1, 1079, 0)));
    assert.ok(session.sent[3].equals(expected(MOTION_ACTION.UP, 0, 1079, 0)));
    await source.stop();
  });

  test("send false answers device_not_connected and nothing is retried", async () => {
    const { log, factories } = androidFakes({ sendResults: [false] });
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    const session = log.sessions[0];
    session.emit("session", { width: 1080, height: 2400 });
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), {
      ok: false,
      error_code: "device_not_connected",
      error: "The scrcpy control socket is not connected.",
    });
    assert.equal(session.sent.length, 1, "no UP after a refused DOWN and no retry");
    await source.stop();
  });

  test("an UP refused after the DOWN answers device_not_connected without a retry", async () => {
    const { log, factories } = androidFakes({ sendResults: [true, false] });
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    const session = log.sessions[0];
    session.emit("session", { width: 1080, height: 2400 });
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), {
      ok: false,
      error_code: "device_not_connected",
      error: "The scrcpy control socket closed before the touch ended.",
    });
    assert.equal(session.sent.length, 2);
    await source.stop();
  });

  test("tap before start, before the size is known and with bad coordinates is refused", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_not_ready", error: "The device stream is not running." });
    await source.start();
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_not_ready", error: "The device screen size is not known yet." });
    log.sessions[0].emit("session", { width: 1080, height: 2400 });
    for (const point of [{ x: -0.1, y: 0.5 }, { x: 0.5, y: 1.5 }, { x: "0.5", y: 0.5 }, { x: Number.NaN, y: 0 }, {}]) {
      assert.deepEqual(await source.tap(point), { ok: false, error_code: "bad_message", error: "x and y must be numbers from 0 to 1." });
    }
    assert.deepEqual(await source.tap(), { ok: false, error_code: "bad_message", error: "x and y must be numbers from 0 to 1." });
    assert.equal(log.sessions[0].sent.length, 0);
    await source.stop();
  });

  test("session errors surface as error events with device_stream_failed", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    const session = log.sessions[0];
    session.emit("error", new Error("scrcpy video socket closed"));
    session.emit("state", "restarting");
    session.emit("state", "streaming");
    assert.equal(events.error.length, 1);
    assert.equal(events.error[0].code, "device_stream_failed");
    assert.equal(events.error[0].message, "scrcpy video socket closed");
    assert.deepEqual(events.state, ["starting", "streaming", "restarting", "streaming"]);
    await source.stop();
  });

  test("an error with no listener does not throw", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    assert.doesNotThrow(() => log.sessions[0].emit("error", new Error("boom")));
    await source.stop();
  });

  test("stop stops the session once, is idempotent and ends input", async () => {
    const { log, factories } = androidFakes();
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    const session = log.sessions[0];
    session.emit("session", { width: 1080, height: 2400 });
    await Promise.all([source.stop(), source.stop()]);
    await source.stop();
    assert.equal(session.stops, 1);
    assert.equal(source.state, "stopped");
    assert.equal(events.state.at(-1), "stopped");
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_not_ready", error: "The device stream is not running." });
    assert.equal(session.sent.length, 0);
    session.emit("error", new Error("late"));
    assert.equal(events.error.length, 0, "errors after stop are not reported");
    await assert.rejects(source.start(), errorCode("source_failed", /stopped/));
  });

  test("an unusable scrcpy-server fails start with scrcpy_unavailable and creates no session", async () => {
    const { log, factories } = androidFakes({ resolved: { ok: false, source: null, reason: "no scrcpy-server found" } });
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await assert.rejects(source.start(), (error) => {
      assert.equal(error.code, "scrcpy_unavailable");
      assert.equal(error.message, "no scrcpy-server found");
      assert.match(error.extras.hint, /AUTONOM_SCRCPY_SERVER/);
      return true;
    });
    assert.equal(log.sessions.length, 0);
    assert.equal(source.state, "failed");
  });

  test("a session that fails to start rejects with device_start_failed and is stopped", async () => {
    const { log, factories } = androidFakes({ startError: new Error("adb: device offline") });
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    await assert.rejects(source.start(), errorCode("device_start_failed", /device offline/));
    assert.equal(log.sessions[0].stops, 1);
    assert.equal(source.state, "failed");
  });

  test("stop during start cancels the start and stops the session", async () => {
    let release;
    const startGate = new Promise((done) => {
      release = done;
    });
    const { log, factories } = androidFakes({ startGate });
    const source = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const starting = source.start();
    for (let i = 0; i < 50 && !log.sessions.length; i += 1) await tick();
    const stopping = source.stop();
    release();
    await assert.rejects(starting, errorCode("source_failed", /stopped while it started/));
    await stopping;
    assert.equal(log.sessions[0].stops, 1);
    assert.equal(source.state, "stopped");
  });
});

// ---------- iOS ----------

describe("iOS relay through IdbCompanion", () => {
  test("start resolves the companion, starts it and opens a 30 fps video stream", async () => {
    const { log, factories } = iosFakes();
    const source = createDeviceSource({ spec: IOS, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    assert.equal(log.resolved.length, 1);
    assert.equal(log.resolved[0].env, process.env);
    assert.equal(log.companions.length, 1);
    assert.deepEqual(log.companions[0].options, { udid: UDID, binary: "/fake/idb_companion" });
    assert.equal(log.streams.length, 1);
    assert.deepEqual(log.streams[0].options, { ...DEFAULT_VIDEO_OPTIONS, fps: 30 });
    assert.equal(IOS_FPS, 30);
    assert.deepEqual(events.state, ["starting", "streaming"]);
    await source.stop();
  });

  test("frames map to config on change and to frames with key flags", async () => {
    const { log, factories } = iosFakes();
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    const stream = log.streams[0];
    stream.push({ data: Buffer.concat([CONFIG, IDR]), key: true, config: CONFIG, configChanged: true, width: 1179, height: 2556, codec: "avc1.640033" });
    stream.push({ data: DELTA, key: false, config: null, configChanged: false, width: 1179, height: 2556, codec: "avc1.640033" });
    stream.push({ data: Buffer.concat([CONFIG, IDR]), key: true, config: CONFIG, configChanged: false, width: 1179, height: 2556, codec: "avc1.640033" });
    stream.push({ data: IDR, key: true, config: CONFIG_B, configChanged: true, width: 2556, height: 1179, codec: null });
    stream.push({ key: false });
    for (let i = 0; i < 20 && events.frame.length < 4; i += 1) await tick();
    assert.equal(events.config.length, 2, "config is emitted only when it changes");
    assert.deepEqual(
      events.config.map(({ codec, width, height }) => ({ codec, width, height })),
      [
        { codec: "avc1.640033", width: 1179, height: 2556 },
        { codec: h264CodecString(CONFIG_B), width: 2556, height: 1179 },
      ],
    );
    assert.ok(events.config[0].annexB.equals(CONFIG) && events.config[1].annexB.equals(CONFIG_B));
    assert.deepEqual(events.frame.map((frame) => frame.key), [true, false, true, true]);
    assert.ok(events.frame[1].data.equals(DELTA));
    for (const frame of events.frame) assert.equal(typeof frame.tMs, "number");
    await source.stop();
  });

  test("tap describes the screen once and taps in points", async () => {
    const { log, factories } = iosFakes();
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.25 }), { ok: true });
    assert.deepEqual(await source.tap({ x: 0.1, y: 0.9 }), { ok: true });
    assert.deepEqual(log.describes, [{ timeoutMs: DESCRIBE_TIMEOUT_MS }]);
    assert.equal(DESCRIBE_TIMEOUT_MS, 3000);
    assert.deepEqual(log.taps, [[Math.round(0.5 * 393), Math.round(0.25 * 852)], [Math.round(0.1 * 393), Math.round(0.9 * 852)]]);
    await source.stop();
  });

  test("tap falls back to pixels over density when points are missing", async () => {
    const { log, factories } = iosFakes({ screen: { width: 1179, height: 2556, density: 3 } });
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    await source.start();
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), { ok: true });
    assert.deepEqual(log.taps, [[Math.round(0.5 * 393), Math.round(0.5 * 852)]]);
    await source.stop();
  });

  test("tap refusals: not started, describe failure, no screen size, failed tap, bad coordinates", async () => {
    const idle = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: iosFakes().factories });
    assert.deepEqual(await idle.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_not_ready", error: "The device stream is not running." });

    const describeFails = iosFakes({ describeError: new Error("deadline exceeded") });
    const a = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: describeFails.factories });
    await a.start();
    assert.deepEqual(await a.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_describe_failed", error: "idb describe failed: deadline exceeded" });
    assert.deepEqual(describeFails.log.taps, []);
    await a.stop();

    const noSize = iosFakes({ screen: {} });
    const b = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: noSize.factories });
    await b.start();
    assert.deepEqual(await b.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_describe_failed", error: "idb describe gave no screen size." });
    await b.stop();

    const tapFails = iosFakes({ tapError: new Error("hid stream closed") });
    const c = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: tapFails.factories });
    await c.start();
    assert.deepEqual(await c.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "tap_failed", error: "idb tap failed: hid stream closed" });
    assert.equal(tapFails.log.taps.length, 1, "a failed tap is not retried");
    assert.deepEqual(await c.tap({ x: 2, y: 0.5 }), { ok: false, error_code: "bad_message", error: "x and y must be numbers from 0 to 1." });
    assert.equal(tapFails.log.taps.length, 1);
    await c.stop();
  });

  test("stream errors, stream end and a companion exit surface as error events", async () => {
    const failing = iosFakes();
    const a = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: failing.factories });
    const aEvents = record(a);
    await a.start();
    failing.log.streams[0].fail(new Error("grpc stream reset"));
    for (let i = 0; i < 20 && !aEvents.error.length; i += 1) await tick();
    assert.deepEqual(aEvents.error.map((error) => [error.code, error.message]), [["device_stream_failed", "grpc stream reset"]]);
    failing.log.companions[0].emit("exit", { code: 1, signal: null, expected: false });
    failing.log.companions[0].emit("exit", { code: 0, signal: null, expected: true });
    assert.deepEqual(aEvents.error.map((error) => error.code), ["device_stream_failed", "idb_companion_exited"]);
    await a.stop();

    const ending = iosFakes();
    const b = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: ending.factories });
    const bEvents = record(b);
    await b.start();
    ending.log.streams[0].end();
    for (let i = 0; i < 20 && !bEvents.error.length; i += 1) await tick();
    assert.deepEqual(bEvents.error.map((error) => [error.code, error.message]), [["device_stream_ended", "The iOS video stream ended."]]);
    await b.stop();
  });

  test("stop stops the video stream and the companion without an error event", async () => {
    const { log, factories } = iosFakes();
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    const events = record(source);
    await source.start();
    await Promise.all([source.stop(), source.stop()]);
    assert.equal(log.streams[0].stops, 1);
    assert.equal(log.companions[0].stops, 1);
    assert.equal(source.state, "stopped");
    assert.deepEqual(events.error, []);
    assert.deepEqual(await source.tap({ x: 0.5, y: 0.5 }), { ok: false, error_code: "device_not_ready", error: "The device stream is not running." });
  });

  test("an unavailable idb_companion fails start with idb_companion_unavailable", async () => {
    const { log, factories } = iosFakes({ resolved: { available: false, path: null, source: null, reason: "idb_companion was not found" } });
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    await assert.rejects(source.start(), (error) => {
      assert.equal(error.code, "idb_companion_unavailable");
      assert.equal(error.message, "idb_companion was not found");
      assert.match(error.extras.hint, /AUTONOM_IDB_COMPANION_BIN/);
      return true;
    });
    assert.equal(log.companions.length, 0);
  });

  test("a companion or stream that fails to start rejects with device_start_failed and stops the companion", async () => {
    const companionFails = iosFakes({ startError: new Error("companion not ready") });
    const a = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: companionFails.factories });
    await assert.rejects(a.start(), errorCode("device_start_failed", /companion not ready/));
    assert.equal(companionFails.log.companions[0].stops, 1);

    const streamFails = iosFakes({ streamError: new Error("video_stream refused") });
    const b = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: streamFails.factories });
    await assert.rejects(b.start(), errorCode("device_start_failed", /video_stream refused/));
    assert.equal(streamFails.log.companions[0].stops, 1);
    assert.equal(b.state, "failed");
  });

  test("stop during start cancels the start and stops the companion and stream", async () => {
    let release;
    const streamGate = new Promise((done) => {
      release = done;
    });
    const { log, factories } = iosFakes({ streamGate });
    const source = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories });
    const starting = source.start();
    for (let i = 0; i < 50 && !log.companions.length; i += 1) await tick();
    const stopping = source.stop();
    release();
    await assert.rejects(starting, errorCode("source_failed", /stopped while it started/));
    await stopping;
    assert.equal(log.companions[0].stops, 1);
    assert.equal(log.streams[0].stops, 1);
    assert.equal(source.state, "stopped");
  });
});

// ---------- no session store ----------

test("the relay creates no file under AUTONOM_HOME or XDG_STATE_HOME", async (t) => {
  const home = mkdtempSync(join(tmpdir(), "probe-relay-home-"));
  const state = mkdtempSync(join(tmpdir(), "probe-relay-state-"));
  const saved = { AUTONOM_HOME: process.env.AUTONOM_HOME, XDG_STATE_HOME: process.env.XDG_STATE_HOME };
  process.env.AUTONOM_HOME = home;
  process.env.XDG_STATE_HOME = state;
  t.after(() => {
    for (const [name, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    rmSync(home, { recursive: true, force: true });
    rmSync(state, { recursive: true, force: true });
  });
  const android = androidFakes();
  const a = createDeviceSource({ spec: ANDROID, adbPath: "/fake/adb", scriptsDir: SCRIPTS_DIR, factories: android.factories });
  a.on("error", () => {});
  await a.start();
  android.log.sessions[0].emit("session", { width: 1080, height: 2400 });
  android.log.sessions[0].emit("config", CONFIG);
  android.log.sessions[0].emit("packet", { keyFrame: true, data: IDR });
  assert.deepEqual(await a.tap({ x: 0.5, y: 0.5 }), { ok: true });
  await a.stop();
  const ios = iosFakes();
  const b = createDeviceSource({ spec: IOS, scriptsDir: SCRIPTS_DIR, factories: ios.factories });
  b.on("error", () => {});
  await b.start();
  ios.log.streams[0].push({ data: IDR, key: true, config: CONFIG, width: 96, height: 208, codec: null });
  assert.deepEqual(await b.tap({ x: 0.5, y: 0.5 }), { ok: true });
  await b.stop();
  assert.deepEqual(readdirSync(home), []);
  assert.deepEqual(readdirSync(state), []);
});
