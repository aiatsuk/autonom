// The Canvas's iOS session on the device daemon (run ios-canvas-unify, CU-01..CU-04): video only
// on change, sized to its tiles, stopped when nobody watches, boot state and orientation pushed.
// An in-process fake companion (a real gRPC session over a Unix socket) sends one key frame after
// each Start on a still screen, as idb's variable frame rate does, and a fake daemon answers the
// socket protocol. No browser, device or process is involved; every wait comes from a constant.
import assert from "node:assert/strict";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import * as http2 from "node:http2";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { performance } from "node:perf_hooks";
import test from "node:test";
import { setTimeout as sleep } from "node:timers/promises";

import { IosFastSession, daemonBootState } from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-device-server.mjs";
import {
  IDB_ON_CHANGE_KEY_FRAME_MS,
  IDB_OWN_READ_MS,
  IDB_RETRY_MIN_MS,
  IDB_WATCH_SETTLE_MS,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-runtime.mjs";
import { IOSD_SCHEMA, daemonPaths } from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-device-daemon.mjs";
import {
  DEFAULT_VIDEO_OPTIONS,
  GrpcMessageParser,
  STREAM_STOP_TIMEOUT_MS,
  decodeVideoStreamRequest,
  encodeVideoStreamResponse,
  grpcFrame,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";

const UDID = "3760A5A8-E59D-4AE6-B1AF-A626908D7B61";
// SPS and PPS of the real 1206x2622 simulator stream.
const SPS = Buffer.from("27640033ac13143c04c0149e6a9a80868083c20108f8", "hex");
const PPS = Buffer.from("28ee3cb0", "hex");
const CODE = Buffer.from([0, 0, 0, 1]);
const ORIENTATIONS = ["UNKNOWN", "PORTRAIT", "PORTRAIT_UPSIDE_DOWN", "LANDSCAPE_LEFT", "LANDSCAPE_RIGHT"];
/** A tile that needs half of the picture on a 2x screen (CU-02-S01), and one that needs all of it. */
const HALF_TILE = { width: 300, height: 652, dpr: 2 };
const FULL_TILE = { width: 402, height: 874, dpr: 3 };
/** What a stream may take after the settle time, and what a viewer may wait for a frame (INV-2). */
const SLACK_MS = 400;
const FIRST_FRAME_MS = 1000;

const varint = (value) => {
  const out = [];
  let rest = value;
  while (rest >= 0x80) { out.push((rest & 0x7f) | 0x80); rest = Math.floor(rest / 128); }
  out.push(rest);
  return Buffer.from(out);
};
const tag = (field, wire) => varint(field * 8 + wire);
const len = (field, bytes) => Buffer.concat([tag(field, 2), varint(bytes.length), bytes]);
const uint = (field, value) => Buffer.concat([tag(field, 0), varint(value)]);
const str = (field, text) => len(field, Buffer.from(text));
const double = (field, value) => {
  const bytes = Buffer.alloc(8);
  bytes.writeDoubleLE(value);
  return Buffer.concat([tag(field, 1), bytes]);
};

async function until(check, milliseconds, what) {
  const deadline = performance.now() + milliseconds;
  for (;;) {
    if (check()) return;
    assert.ok(performance.now() < deadline, `${what}: not within ${milliseconds} ms`);
    await sleep(5);
  }
}

/** One simulator's companion: a still screen unless `damage()` says otherwise. */
class FakeCompanion {
  socketPath;
  /** Every Start: { at, fps, scale, call }; `stops` counts answered Stops. */
  starts = [];
  stops = 0;
  /** How long a Stop goes unanswered. */
  stopDelayMs = 0;
  describes = 0;
  orientationReads = 0;
  state = "Booted";
  orientation = "PORTRAIT";
  #server = http2.createServer();
  #sessions = new Set();
  #calls = new Set();
  #sequence = 0;

  constructor(socketPath) {
    this.socketPath = socketPath;
    this.#server.on("session", (session) => {
      this.#sessions.add(session);
      session.on("error", () => {});
      session.on("close", () => this.#sessions.delete(session));
    });
    this.#server.on("stream", (stream, headers) => this.#onCall(stream, headers[":path"].split("/").pop()));
  }

  /** The Starts that had no Stop yet: never more than one encoder. */
  get running() {
    return this.#calls.size;
  }

  async listen() {
    await new Promise((resolvePromise, reject) => {
      this.#server.once("error", reject);
      this.#server.listen(this.socketPath, resolvePromise);
    });
    return this;
  }

  #frame(key) {
    const body = Buffer.alloc(5);
    body[0] = key ? 0x65 : 0x41;
    body.writeUInt32BE(this.#sequence++, 1);
    const data = key ? Buffer.concat([CODE, SPS, CODE, PPS, CODE, body]) : Buffer.concat([CODE, body]);
    return grpcFrame(encodeVideoStreamResponse({ data }));
  }

  /** The screen changed: one frame on every open call. */
  damage() {
    for (const stream of this.#calls) stream.write(this.#frame(false));
  }

  /** Every open video call ends, no Stop asked for it. */
  endVideo() {
    for (const stream of this.#calls) stream.end();
  }

  #onCall(stream, method) {
    const parser = new GrpcMessageParser();
    stream.on("error", () => {});
    stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
    stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
    stream.on("data", (chunk) => {
      for (const message of parser.push(chunk)) {
        if (method === "video_stream") this.#onVideo(stream, decodeVideoStreamRequest(message));
        else if (method === "describe") {
          this.describes += 1;
          const screen = Buffer.concat([uint(1, 1206), uint(2, 2622), double(3, 3), uint(4, 402), uint(5, 874)]);
          stream.write(grpcFrame(len(1, Buffer.concat([str(1, UDID), str(2, "Fake"), len(3, screen), str(4, this.state)]))));
          stream.end();
        } else if (method === "get_orientation") {
          this.orientationReads += 1;
          stream.write(grpcFrame(uint(1, ORIENTATIONS.indexOf(this.orientation))));
          stream.end();
        }
      }
    });
    stream.on("close", () => this.#calls.delete(stream));
  }

  #onVideo(stream, request) {
    if (request.type === "start") {
      this.starts.push({ at: performance.now(), fps: request.fps, scale: request.scaleFactor });
      this.#calls.add(stream);
      // Stop and Start bring SPS, PPS and an IDR soon after, even on a still screen.
      setTimeout(() => { if (this.#calls.has(stream)) stream.write(this.#frame(true)); }, 20);
    } else if (request.type === "stop") {
      this.stops += 1;
      this.#calls.delete(stream);
      if (this.stopDelayMs) setTimeout(() => stream.end(), this.stopDelayMs).unref();
      else stream.end();
    }
  }

  async close() {
    for (const session of this.#sessions) session.destroy();
    await new Promise((resolvePromise) => this.#server.close(resolvePromise));
  }
}

/** The socket protocol of the device daemon, in this process: its registry names this process. */
class FakeDaemon {
  ops = [];
  /** The `watching` of each `subscribe`, in order. */
  subscribes = [];
  refuseSubscribe = false;
  state = "serving";
  booted = true;
  companion;
  #server;
  #connections = new Set();
  #subscribers = new Set();

  constructor(world, companion) {
    this.world = world;
    this.companion = companion;
    this.socket = join(world.sockets, `d-${world.daemons.push(this)}.sock`);
  }

  async start() {
    this.#server = createNetServer((socket) => this.#onConnection(socket));
    await new Promise((resolvePromise, reject) => {
      this.#server.once("error", reject);
      this.#server.listen(this.socket, resolvePromise);
    });
    const { registry } = daemonPaths(this.world.home, UDID);
    mkdirSync(dirname(registry), { recursive: true });
    writeFileSync(registry, JSON.stringify({
      schema: IOSD_SCHEMA, udid: UDID, pid: process.pid, socket: this.socket, state: this.state,
      companion: { pid: process.pid, socket: this.companion.socketPath, owned: true },
    }));
    return this;
  }

  /** A state event, as the daemon writes one on every change. */
  publishState({ state = this.state, booted = this.booted, target = booted === null ? null : booted ? "Booted" : "Shutdown" } = {}) {
    this.state = state;
    this.booted = booted;
    this.#publish({ event: "state", udid: UDID, state, booted, target_state: target });
  }

  publishOrientation(orientation) {
    this.#publish({ event: "orientation", udid: UDID, orientation });
  }

  #publish(event) {
    for (const socket of this.#subscribers) socket.write(`${JSON.stringify(event)}\n`);
  }

  /** Every connection drops; the listening socket stays, as for a daemon that took over. */
  dropConnections() {
    for (const socket of this.#connections) socket.destroy();
  }

  #onConnection(socket) {
    this.#connections.add(socket);
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("error", () => {});
    socket.on("close", () => {
      this.#connections.delete(socket);
      this.#subscribers.delete(socket);
    });
    socket.on("data", (chunk) => {
      buffer += chunk;
      for (let index = buffer.indexOf("\n"); index >= 0; index = buffer.indexOf("\n")) {
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (line.trim()) this.#handle(socket, JSON.parse(line));
      }
    });
  }

  #handle(socket, request) {
    const write = (message) => socket.write(`${JSON.stringify(message)}\n`);
    this.ops.push(request.op);
    if (request.op === "subscribe") {
      this.subscribes.push(request.watching);
      if (this.refuseSubscribe) {
        write({ id: request.id, ok: false, error: { code: "invalid_request", message: "Unknown op.", stage: "before_send" } });
        return;
      }
      this.#subscribers.add(socket);
      write({ id: request.id, ok: true, subscribed: true, watching: request.watching });
      write({ event: "state", udid: UDID, state: this.state, booted: this.booted, target_state: this.booted ? "Booted" : "Shutdown" });
      return;
    }
    write(request.op === "detach" ? { id: request.id, ok: true }
      : { id: request.id, ok: true, socket: this.companion.socketPath, pid: process.pid, owned: true });
  }

  async close() {
    for (const socket of this.#connections) socket.destroy();
    await new Promise((resolvePromise) => this.#server.close(resolvePromise));
  }
}

async function makeWorld(t, { subscribe = true } = {}) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-unify-"));
  // Unix socket paths stay short: their own folder directly under the temporary directory.
  const sockets = await mkdtemp(join(tmpdir(), "acu-"));
  const world = { directory, sockets, home: join(directory, "home"), daemons: [], sessions: [] };
  world.companion = await new FakeCompanion(join(sockets, "c.sock")).listen();
  world.daemon = await new FakeDaemon(world, world.companion).start();
  world.daemon.refuseSubscribe = !subscribe;
  t.after(async () => {
    await Promise.allSettled(world.sessions.map((session) => session.stop()));
    await Promise.allSettled(world.daemons.map((daemon) => daemon.close()));
    await world.companion.close();
    rmSync(directory, { recursive: true, force: true });
    rmSync(sockets, { recursive: true, force: true });
  });
  return world;
}

function makeSession(world, { fps = 0, tiles = [null] } = {}) {
  const session = new IosFastSession({
    udid: UDID, binary: join(world.directory, "no-companion"),
    video: { ...DEFAULT_VIDEO_OPTIONS, fps },
    daemon: { stateBase: world.home, env: { AUTONOM_HOME: world.home } },
  });
  world.sessions.push(session);
  session.keys = [];
  session.packets = 0;
  session.states = [];
  session.orientations = [];
  session.errors = [];
  session.stopsSeen = 0;
  session.bootedEvents = [];
  session.on("packet", ({ keyFrame }) => {
    session.packets += 1;
    if (keyFrame) session.keys.push(performance.now());
  });
  session.on("state", (name) => session.states.push(name));
  session.on("orientation", (name) => session.orientations.push(name));
  session.on("error", (error) => session.errors.push(error.message));
  session.on("video-stopped", () => { session.stopsSeen += 1; });
  session.on("booted", (booted) => session.bootedEvents.push(booted));
  if (tiles) session.setWatchers(tiles);
  return session;
}

/** A session streaming on the world's companion, its first frame shown, its events subscribed. */
async function streaming(world, options) {
  const session = makeSession(world, options);
  await session.start();
  await until(() => session.stats.daemonSubscribed || world.daemon.refuseSubscribe, 1000, "the subscription");
  return session;
}

test("CU-01: without a choice the stream asks for frames on change, and a still screen sends its key frame and no more", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  assert.equal(world.companion.starts[0].fps, 0);
  assert.equal(world.companion.starts[0].scale, 1);
  await sleep(1100);
  assert.equal(session.packets, 1, "frames on a still screen");
  assert.equal(session.stats.framesPerSecond, 0);
  assert.equal(world.companion.starts.length, 1);
  // A change on the screen is one frame.
  world.companion.damage();
  await until(() => session.packets === 2, 500, "the frame of a change");
  assert.equal(session.stats.framesPerSecond, 1);
});

test("CU-01: a Display choice of a fixed rate is honored in place, and so is going back to On change", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  assert.equal(await session.setVideo({ fps: 60 }), true);
  await until(() => world.companion.starts.length === 2, 500, "the Start with 60 fps");
  assert.equal(world.companion.starts[1].fps, 60);
  assert.equal(world.companion.stops, 1);
  assert.equal(await session.setVideo({ fps: 30 }), true);
  assert.equal(await session.setVideo({ fps: 0 }), true);
  await until(() => world.companion.starts.length === 4, 500, "the Starts of both changes");
  assert.deepEqual(world.companion.starts.map(({ fps }) => fps), [0, 60, 30, 0]);
  assert.equal(world.companion.running, 1, "never two encoders");
});

test("setVideo: settings the stream already runs with restart nothing (a focus change on an iOS tile costs no Stop)", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  assert.equal(await session.setVideo({ fps: 0, scaleFactor: 1 }), true);
  assert.equal(world.companion.starts.length, 1);
  assert.equal(session.stats.streamRestarts, 0);
});

test("setVideo: during a restart, settings that need no change wait for it and restart nothing more", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  world.companion.stopDelayMs = 100;
  session.requestKeyFrame();
  await until(() => session.stats.streamRestarts === 1, world.companion.stopDelayMs / 2, "the forced key frame");
  assert.equal(await session.setVideo({ fps: 0, scaleFactor: 1 }), true, "a focus change would count as failed");
  assert.equal(session.stats.streamRestarts, 1);
  await until(() => world.companion.starts.length === 2, 500, "the Start of the key frame");
});

test("setVideo: a choice made during a restart runs right after it, and every answer says so", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  const answers = [session.setVideo({ fps: 60 }), session.setVideo({ fps: 30 }), session.setVideo({ fps: 30 })];
  assert.deepEqual(await Promise.all(answers), [true, true, true], "the Display choice would count as failed");
  await until(() => world.companion.starts.length === 3, 500, "the Start with the last choice");
  assert.deepEqual(world.companion.starts.map(({ fps }) => fps), [0, 60, 30]);
  assert.equal(world.companion.running, 1, "never two encoders");
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.equal(world.companion.starts.length, 3, "a restart too many");
});

test("setVideo: with the stream lost and the next one on its way the answer is true, and that stream opens with it", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  world.companion.endVideo();
  await until(() => session.state === "restarting", 500, "the lost stream");
  assert.equal(await session.setVideo({ fps: 0, scaleFactor: 1 }), true, "a focus change would count as failed");
  assert.equal(await session.setVideo({ fps: 15 }), true, "the Display choice would count as failed");
  await until(() => world.companion.starts.length === 2, IDB_RETRY_MIN_MS + SLACK_MS, "the next stream");
  assert.equal(world.companion.starts[1].fps, 15);
  assert.equal(session.stats.streamRestarts, 0);
});

test("setVideo: false when a session stopped for good or its own restart failed", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  // Stop goes unanswered past the time a stream gives it.
  world.companion.stopDelayMs = STREAM_STOP_TIMEOUT_MS * 2;
  assert.equal(await session.setVideo({ fps: 30 }), false);
  await session.stop();
  assert.equal(await session.setVideo({ fps: 15 }), false);
});

test("CU-01-S02: on change a key frame asked for comes at once by Stop and Start, at most once per interval", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  const asked = performance.now();
  session.requestKeyFrame();
  await until(() => session.keys.length === 2, FIRST_FRAME_MS, "the forced key frame");
  assert.ok(session.keys[1] - asked < FIRST_FRAME_MS);
  assert.equal(session.stats.forcedKeyFrames, 1);
  // Asked again at once: the next Start waits for the interval, a joiner never more than that.
  session.requestKeyFrame();
  session.requestKeyFrame();
  await sleep(IDB_ON_CHANGE_KEY_FRAME_MS / 2);
  assert.equal(world.companion.starts.length, 2, "forced twice within the interval");
  await until(() => world.companion.starts.length === 3, IDB_ON_CHANGE_KEY_FRAME_MS, "the next forced key frame");
  await until(() => session.keys.length === 3, FIRST_FRAME_MS, "its frame");
  assert.ok(world.companion.starts[2].at - world.companion.starts[1].at >= IDB_ON_CHANGE_KEY_FRAME_MS - 5);
  assert.equal(world.companion.running, 1);
});

test("CU-01: a key frame asked for while a stream opens is the one that stream starts with", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  // The Stop and Start of a new setting run: the frame they bring answers this request too.
  const restarted = session.setVideo({ fps: 30 });
  session.requestKeyFrame();
  await restarted;
  await until(() => session.keys.length === 2, FIRST_FRAME_MS, "the frame of the new Start");
  await sleep(IDB_ON_CHANGE_KEY_FRAME_MS / 4);
  assert.equal(world.companion.starts.length, 2, "a second restart for the same frame");
});

test("CU-02-S01: the stream opens at the scale its tile needs and follows the largest tile in place", async (t) => {
  const world = await makeWorld(t);
  const session = makeSession(world, { tiles: [HALF_TILE] });
  await session.start();
  assert.equal(world.companion.starts[0].scale, 0.5, "the first stream opens at the tile's size");
  assert.equal(session.scaleFactor, 0.5);
  // A second, larger tile decides; it going away brings the smaller scale back.
  session.setWatchers([HALF_TILE, FULL_TILE]);
  await until(() => world.companion.starts.length === 2, IDB_WATCH_SETTLE_MS + SLACK_MS, "full size");
  assert.equal(world.companion.starts[1].scale, 1);
  session.setWatchers([HALF_TILE]);
  await until(() => world.companion.starts.length === 3, IDB_WATCH_SETTLE_MS * 2 + SLACK_MS, "half size again");
  assert.equal(world.companion.starts[2].scale, 0.5);
  assert.equal(world.companion.running, 1);
  assert.equal(world.daemon.ops.filter((op) => op === "companion").length, 0, "the same companion streams on");
});

test("CU-02: a resize burst is one restart after it settled, and small resizes inside a step are none", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world, { tiles: [FULL_TILE] });
  const before = world.companion.starts.length;
  // Inside a step: nothing restarts.
  for (let width = 400; width < 410; width += 1) {
    session.setWatchers([{ width, height: 870, dpr: 3 }]);
    await sleep(IDB_WATCH_SETTLE_MS / 25);
  }
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.equal(world.companion.starts.length, before, "a restart inside a step");
  // Dragging the split: the size keeps changing, the stream restarts once it stops changing.
  let last = 0;
  for (let width = 400; width >= 240; width -= 16) {
    session.setWatchers([{ width, height: Math.round(width * 2.17), dpr: 2 }]);
    last = performance.now();
    await sleep(IDB_WATCH_SETTLE_MS / 10);
  }
  await until(() => world.companion.starts.length === before + 1, IDB_WATCH_SETTLE_MS + SLACK_MS, "the restart after the burst");
  const start = world.companion.starts.at(-1);
  assert.equal(start.scale, 0.5, "the last size counts");
  assert.ok(start.at - last >= IDB_WATCH_SETTLE_MS - 5, `restarted ${start.at - last} ms after the last change`);
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.equal(world.companion.starts.length, before + 1, "one restart for one burst");
});

test("CU-02: a new scale goes out at most once per settle time, however soon the next change comes", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world, { tiles: [FULL_TILE] });
  await session.setVideo({ fps: 30 });
  session.setWatchers([HALF_TILE]);
  await until(() => world.companion.starts.at(-1).scale === 0.5, IDB_WATCH_SETTLE_MS * 2 + SLACK_MS, "half size");
  session.setWatchers([FULL_TILE]);
  await until(() => world.companion.starts.at(-1).scale === 1, IDB_WATCH_SETTLE_MS * 2 + SLACK_MS, "full size");
  const { starts } = world.companion;
  for (let index = 2; index < starts.length; index += 1) {
    assert.ok(starts[index].at - starts[index - 1].at >= IDB_WATCH_SETTLE_MS - 5, "two scales within the settle time");
  }
});

test("Half is the most the tiles get: the smaller of the choice and what they need", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world, { tiles: [FULL_TILE] });
  assert.equal(await session.setVideo({ scaleFactor: 0.5 }), true);
  await until(() => world.companion.starts.at(-1).scale === 0.5, 500, "Half");
  session.setWatchers([{ width: 80, height: 174, dpr: 2 }]);
  await until(() => world.companion.starts.at(-1).scale === 0.25, IDB_WATCH_SETTLE_MS * 2 + SLACK_MS, "a quarter");
  await session.setVideo({ scaleFactor: 1 });
  assert.equal(session.scaleFactor, 0.25, "full size is no reason to go above what the tile needs");
});

test("CU-03-S01: a hidden page stops the video within 2 s, input stays up, and showing it brings a key frame within 1 s", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  const hidden = performance.now();
  session.setWatchers([]);
  await until(() => world.companion.stops === 1, 2000, "the Stop");
  assert.ok(performance.now() - hidden >= IDB_WATCH_SETTLE_MS - 5, "the Stop did not wait for the page to settle");
  await until(() => session.stats.videoStopped, 500, "the session to stop");
  assert.equal(world.companion.running, 0);
  assert.equal(session.state, "streaming", "nothing failed");
  assert.equal(session.connected, true, "input goes on while nobody watches");
  assert.ok(!session.states.includes("restarting"));
  assert.equal(session.stats.restarts, 0);
  assert.equal(session.stopsSeen, 1);
  await session.hid({ touch: { x: 1, y: 2 }, direction: "down" }).catch(() => {});
  const keys = session.keys.length;
  const shown = performance.now();
  session.setWatchers([null]);
  await until(() => session.keys.length === keys + 1, FIRST_FRAME_MS, "the key frame after showing");
  assert.ok(world.companion.starts.at(-1).at - shown < IDB_WATCH_SETTLE_MS, "showing does not wait to settle");
  assert.equal(session.stats.videoStopped, false);
  assert.deepEqual(session.errors, []);
});

test("CU-03: flaps inside the settle time stop nothing; a tile back during the Stop coalesces into one new stream", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  for (let flap = 0; flap < 6; flap += 1) {
    session.setWatchers([]);
    await sleep(IDB_WATCH_SETTLE_MS / 5);
    session.setWatchers([null]);
    await sleep(IDB_WATCH_SETTLE_MS / 10);
  }
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.equal(world.companion.stops, 0, "a flap stopped the stream");
  assert.equal(world.companion.starts.length, 1);
  // Hidden for good, then back the moment Stop went out.
  session.setWatchers([]);
  await until(() => world.companion.stops === 1, IDB_WATCH_SETTLE_MS + SLACK_MS, "the Stop");
  session.setWatchers([null]);
  await until(() => world.companion.starts.length === 2, FIRST_FRAME_MS, "the new stream");
  await until(() => session.keys.length === 2, FIRST_FRAME_MS, "its key frame");
  assert.equal(world.companion.running, 1);
  assert.equal(session.state, "streaming");
  assert.equal(session.stats.videoStopped, false);
});

test("CU-03: nobody watching from the start still brings the first frame (the control socket needs a session), then the stream stops", async (t) => {
  const world = await makeWorld(t);
  const session = makeSession(world, { tiles: [] });
  await session.start();
  assert.equal(session.connected, true);
  assert.equal(world.daemon.subscribes[0], false, "the daemon does not poll for nobody");
  await until(() => world.companion.stops === 1, IDB_WATCH_SETTLE_MS + SLACK_MS, "the Stop");
  assert.equal(session.connected, true);
});

test("CU-03: while the stream is stopped a new setting waits for the next stream, which counts as applied", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  const starts = world.companion.starts.length;
  assert.equal(await session.setVideo({ fps: 15 }), true, "the choice would count as failed");
  assert.equal(world.companion.starts.length, starts);
  session.setWatchers([HALF_TILE]);
  await until(() => world.companion.starts.length === starts + 1, FIRST_FRAME_MS, "the next stream");
  assert.equal(world.companion.starts.at(-1).fps, 15);
  assert.equal(world.companion.starts.at(-1).scale, 0.5, "it opens at once at the tile's size, not one restart later");
  assert.equal(session.stats.streamRestarts, 0);
});

test("CU-03: a key frame asked for while the stream is stopped forces nothing", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  session.requestKeyFrame();
  await sleep(IDB_ON_CHANGE_KEY_FRAME_MS / 2);
  assert.equal(session.stats.forcedKeyFrames, 0);
  assert.equal(world.companion.starts.length, 1);
});

test("CU-04-S02: a shutdown event marks the Simulator down within the event, without any read; Booted restarts the stream", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  const describes = world.companion.describes;
  world.daemon.publishState({ booted: false });
  await until(() => session.booted === false, 500, "the shutdown");
  assert.equal(session.state, "restarting");
  assert.equal(session.connected, false, "no input while it is down");
  assert.equal(session.stats.targetState, "Shutdown");
  assert.equal(world.companion.describes, describes, "the event is all it takes");
  world.daemon.publishState({ booted: true });
  await until(() => session.booted && session.state === "streaming", FIRST_FRAME_MS, "the picture back");
  assert.equal(world.companion.starts.length, 2);
  assert.equal(world.companion.stops, 1);
  assert.equal(world.companion.running, 1);
});

test("CU-04-S02: a shutdown while the session already restarts is told without a state change, and so is Booted", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  world.companion.endVideo();
  await until(() => session.state === "restarting", 500, "the lost stream");
  const states = session.states.length;
  world.daemon.publishState({ booted: false });
  await until(() => session.bootedEvents.length === 1, IDB_RETRY_MIN_MS / 2, "the shutdown told");
  assert.deepEqual(session.bootedEvents, [false]);
  assert.equal(session.states.length, states, "the state stays what it was");
  assert.equal(session.state, "restarting");
  world.daemon.publishState({ booted: true });
  await until(() => session.state === "streaming", IDB_RETRY_MIN_MS + FIRST_FRAME_MS, "the picture back");
  assert.deepEqual(session.bootedEvents, [false, true]);
});

test("CU-04-S02: a Simulator already down while the first stream opens is told too", async (t) => {
  const world = await makeWorld(t);
  world.daemon.booted = false;
  const session = makeSession(world);
  const started = session.start().catch(() => {});
  await until(() => session.bootedEvents.length === 1, FIRST_FRAME_MS, "the shutdown told");
  assert.deepEqual(session.bootedEvents, [false]);
  assert.equal(session.state, "starting");
  await session.stop();
  await started;
});

test("CU-04: nothing known (a companion restart) keeps the last boot state; orientation events reach the session", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  world.daemon.publishState({ state: "companion_restarting", booted: null });
  await until(() => session.stats.targetState === null, 500, "the event");
  assert.equal(session.booted, true);
  assert.equal(session.state, "streaming");
  world.daemon.publishState({ state: "serving", booted: true });
  world.daemon.publishOrientation("LANDSCAPE_LEFT");
  await until(() => session.orientation === "LANDSCAPE_LEFT", 500, "the orientation");
  assert.deepEqual(session.orientations, ["LANDSCAPE_LEFT"]);
  // Face up and unknown keep the orientation the screen is drawn in, and a repeat is no change.
  world.daemon.publishOrientation("LANDSCAPE_LEFT");
  world.daemon.publishOrientation("UNKNOWN");
  world.daemon.publishOrientation("PORTRAIT_UPSIDE_DOWN");
  await until(() => session.orientation === "PORTRAIT_UPSIDE_DOWN", 500, "the next orientation");
  assert.deepEqual(session.orientations, ["LANDSCAPE_LEFT", "PORTRAIT_UPSIDE_DOWN"]);
});

test("CU-04: a listener that throws on an event ends neither the events nor the Canvas", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.prependOnceListener("orientation", () => { throw new Error("listener failed"); });
  world.daemon.publishOrientation("LANDSCAPE_RIGHT");
  await until(() => session.errors.includes("listener failed"), 500, "the reported error");
  world.daemon.publishOrientation("PORTRAIT");
  await until(() => session.orientation === "PORTRAIT", 500, "the next event");
});

test("CU-04: a Simulator that goes down and comes back while the stream is stopped ends up stopped and streaming again", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  world.daemon.publishState({ booted: false });
  await until(() => session.state === "restarting", 500, "down");
  assert.equal(world.companion.starts.length, 1);
  world.daemon.publishState({ booted: true });
  await until(() => session.state === "streaming", FIRST_FRAME_MS, "input back");
  assert.equal(session.connected, true);
  assert.equal(world.companion.starts.length, 2, "one stream brought the session back");
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "stopped again");
  assert.equal(world.companion.running, 0);
});

test("CU-04: a companion replaced while the stream is stopped brings the session to the new one, input included", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  const old = world.companion;
  const next = await new FakeCompanion(join(world.sockets, "c2.sock")).listen();
  t.after(() => next.close());
  world.daemon.publishState({ state: "companion_restarting", booted: null });
  world.daemon.companion = next;
  world.daemon.publishState({ state: "serving", booted: true });
  await until(() => next.starts.length === 1, FIRST_FRAME_MS, "a stream on the new companion");
  assert.equal(old.starts.length, 1);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "stopped again");
  assert.equal(session.connected, true);
  // Watching flips that resend the current state are no companion restart.
  session.setWatchers([null]);
  await until(() => next.starts.length === 2, FIRST_FRAME_MS, "shown again");
  await sleep(IDB_WATCH_SETTLE_MS / 2);
  assert.equal(next.starts.length, 2);
});

test("CU-04: the daemon polls only while a tile shows the device: watching follows the tiles, and stops settle first", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  assert.deepEqual(world.daemon.subscribes, [true]);
  // A flap never reaches the daemon.
  session.setWatchers([]);
  await sleep(IDB_WATCH_SETTLE_MS / 5);
  session.setWatchers([null]);
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.deepEqual(world.daemon.subscribes, [true]);
  session.setWatchers([]);
  await until(() => world.daemon.subscribes.length === 2, IDB_WATCH_SETTLE_MS + SLACK_MS, "unwatched");
  assert.deepEqual(world.daemon.subscribes, [true, false]);
  session.setWatchers([null]);
  await until(() => world.daemon.subscribes.length === 3, 200, "watched, at once");
  assert.deepEqual(world.daemon.subscribes, [true, false, true]);
  assert.equal(session.stats.ownReads, 0, "with events the session asks its companion nothing on a timer");
});

test("CU-04: subscribed, the session sends no read of its own, however long it streams", async (t) => {
  const world = await makeWorld(t);
  await streaming(world);
  const { describes, orientationReads } = world.companion;
  await sleep(IDB_OWN_READ_MS * 3);
  assert.equal(world.companion.describes, describes);
  assert.equal(world.companion.orientationReads, orientationReads);
});

test("CU-04: a lost daemon connection subscribes again with what is true now", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  world.daemon.dropConnections();
  await until(() => world.daemon.subscribes.length === 3, 5000, "the new subscription");
  assert.deepEqual(world.daemon.subscribes, [true, false, false]);
  await until(() => session.stats.daemonSubscribed, 500, "the reply");
  assert.equal(session.stats.daemonLosses, 1);
  world.daemon.publishState({ booted: false });
  await until(() => session.booted === false, 500, "events on the new connection");
});

test("CU-04: a shutdown event from a connection the session left is ignored", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  await session.stop();
  world.daemon.publishState({ booted: false });
  await sleep(50);
  assert.equal(session.booted, true);
});

test("a daemon from before events (subscribe refused): the session reads its own companion over gRPC while a tile shows the device, and stops reading when none does", async (t) => {
  const world = await makeWorld(t, { subscribe: false });
  const session = await streaming(world);
  assert.equal(session.stats.daemonSubscribed, false);
  const read = world.companion.describes;
  await until(() => world.companion.describes >= read + 2, IDB_OWN_READ_MS * 3, "two reads");
  world.companion.orientation = "LANDSCAPE_LEFT";
  await until(() => session.orientation === "LANDSCAPE_LEFT", IDB_OWN_READ_MS * 2, "the orientation");
  world.companion.state = "Shutdown";
  await until(() => session.booted === false, IDB_OWN_READ_MS * 2, "the shutdown");
  assert.equal(session.state, "restarting");
  assert.equal(session.stats.targetState, "Shutdown");
  world.companion.state = "Booted";
  await until(() => session.booted && session.state === "streaming", IDB_OWN_READ_MS * 2 + FIRST_FRAME_MS, "the stream back");
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  const reads = session.stats.ownReads;
  await sleep(IDB_OWN_READ_MS * 2 + SLACK_MS);
  assert.ok(session.stats.ownReads <= reads + 1, "a read for nobody");
});

test("the session's own reads go at the pace of one per interval, and an answer that fails keeps what is known", async (t) => {
  const world = await makeWorld(t, { subscribe: false });
  const session = await streaming(world);
  const started = performance.now();
  const before = session.stats.ownReads;
  await sleep(IDB_OWN_READ_MS * 3 + SLACK_MS);
  const reads = session.stats.ownReads - before;
  assert.ok(reads >= 2 && reads <= (performance.now() - started) / IDB_OWN_READ_MS + 1, `${reads} reads`);
  world.companion.state = "";
  await sleep(IDB_OWN_READ_MS + SLACK_MS);
  assert.equal(session.booted, true, "a state of nothing says nothing");
  assert.equal(session.state, "streaming");
});

test("stop() ends a stopped stream's wait and every timer, and later reports change nothing", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  session.setWatchers([]);
  await until(() => session.stats.videoStopped, IDB_WATCH_SETTLE_MS + SLACK_MS, "the stop");
  await session.stop();
  assert.equal(session.state, "stopped");
  assert.equal(session.stats.videoStopped, false);
  session.setWatchers([null]);
  await sleep(IDB_WATCH_SETTLE_MS + SLACK_MS);
  assert.equal(world.companion.starts.length, 1);
  assert.equal(world.daemon.ops.at(-1), "detach");
});

test("a report that changes nothing starts no timer: the stop stays due when the first report said so", async (t) => {
  const world = await makeWorld(t);
  const session = await streaming(world);
  const hidden = performance.now();
  session.setWatchers([]);
  for (let index = 0; index < 8; index += 1) {
    await sleep(IDB_WATCH_SETTLE_MS / 10);
    session.setWatchers([]);
  }
  await until(() => world.companion.stops === 1, IDB_WATCH_SETTLE_MS + SLACK_MS, "the Stop");
  assert.ok(performance.now() - hidden < IDB_WATCH_SETTLE_MS + SLACK_MS * 2);
});

test("daemonBootState: the workspace poll skips simctl only while the session follows the daemon's events", async (t) => {
  assert.equal(daemonBootState(undefined), null, "no context");
  assert.equal(daemonBootState({ session: null }), null, "no session");
  assert.equal(daemonBootState({ session: { booted: true, stats: { daemonSubscribed: true } } }), null,
    "not an iOS session");
  const world = await makeWorld(t);
  const session = await streaming(world);
  assert.equal(daemonBootState({ session }), true);
  world.daemon.publishState({ booted: false });
  await until(() => session.booted === false, 500, "the shutdown");
  assert.equal(daemonBootState({ session }), false);
  world.daemon.publishState({ state: "serving", booted: true });
  await until(() => session.booted === true, 500, "booted again");
  assert.equal(daemonBootState({ session }), true);
  world.daemon.publishState({ state: "companion_restarting", booted: null });
  await until(() => session.stats.daemonState === "companion_restarting", 500, "the restart event");
  assert.equal(session.booted, true, "the session keeps its last boot state");
  assert.equal(daemonBootState({ session }), null, "but the workspace asks simctl while the companion is lost");
  world.daemon.publishState({ state: "serving", booted: null, target_state: null });
  await until(() => session.stats.daemonState === "serving", 500, "serving again");
  assert.equal(daemonBootState({ session }), null, "serving again, nothing known of the Simulator yet");
  const old = await makeWorld(t, { subscribe: false });
  const own = await streaming(old);
  assert.equal(daemonBootState({ session: own }), null, "a daemon from before events: simctl is asked");
});
