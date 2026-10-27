import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { spawn } from "node:child_process";
import test from "node:test";
import { performAndroid, performIOS, parseCompanionEndpoint } from "../scripts/pinch_bridge.mjs";
import { decodeHidEvent } from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";

const request = { platform: "android", target: "emulator-5554", adb: "adb", x: 200, y: 400,
  width: 400, height: 800, radius: 80, scale: 2, duration: 0.1 };

class FakeSession extends EventEmitter {
  constructor({ failAt = 0, failStart = false } = {}) {
    super(); this.failAt = failAt; this.failStart = failStart; this.events = []; this.stopped = false;
  }
  async start() { if (this.failStart) throw new Error("startup failed"); }
  send(bytes, done) {
    if (this.events.length === this.failAt && this.failAt) return false;
    this.events.push({ action: bytes.readUInt8(1), id: Number(bytes.readBigUInt64BE(2)),
      x: bytes.readInt32BE(10), y: bytes.readInt32BE(14),
      width: bytes.readUInt16BE(18), height: bytes.readUInt16BE(20) });
    queueMicrotask(done);
    return true;
  }
  async stop() { this.stopped = true; }
}

function androidDeps(session) {
  return { resolveServer: async () => ({ ok: true, serverPath: "/server", version: "4.1" }),
    createSession: (options) => { assert.equal(options.controlOnly, true); return session; },
    delay: async () => {} };
}

test("Android emits a real pair of simultaneous fingers and scales their separation", async () => {
  for (const scale of [2, 0.5]) {
    const session = new FakeSession();
    assert.equal(await performAndroid({ ...request, scale }, androidDeps(session)), "scrcpy");
    assert.deepEqual(session.events.slice(0, 2).map(({ action, id, x }) => [action, id, x]),
      [[0, 1, 120], [0, 2, 280]]);
    assert.deepEqual(session.events.slice(-2).map(({ action, id, x }) => [action, id, x]),
      [[1, 2, 200 + 80 * scale], [1, 1, 200 - 80 * scale]]);
    assert.ok(session.events.slice(2, -2).every(({ action, y, width, height }) =>
      action === 2 && y === 400 && width === 400 && height === 800));
    assert.equal(session.stopped, true);
  }
});

test("Android failures clean up and do not replay the gesture", async () => {
  const session = new FakeSession({ failAt: 3 });
  await assert.rejects(performAndroid(request, androidDeps(session)),
    (error) => error.code === "ui_action_uncertain" && error.extra.sent === true);
  assert.equal(session.events.length, 3);
  assert.equal(session.stopped, true);
  const before = new FakeSession({ failStart: true });
  await assert.rejects(performAndroid(request, androidDeps(before)),
    (error) => error.code === "backend_failed" && error.extra.sent === false);
  assert.equal(before.stopped, true);
});

test("Android missing scrcpy is a structured refusal before starting anything", async () => {
  await assert.rejects(performAndroid(request, { resolveServer: async () => ({ ok: false, reason: "missing" }) }),
    (error) => error.code === "tool_missing" && error.extra.capability === "ui.pinch");
});

test("iOS sends exactly one HIDPinch with every supplied field", async () => {
  const sent = [];
  const client = { describe: async () => ({ udid: "SIMULATOR" }),
    unary: async (method, bytes) => sent.push([method, decodeHidEvent(bytes)]) };
  const ios = { ...request, platform: "ios", target: "SIMULATOR", scale: 0.5 };
  assert.equal(await performIOS(ios, { client }), "idb");
  assert.deepEqual(sent, [["hid", { pinch: { center: { x: 200, y: 400 }, scale: 0.5,
    duration: 0.1, radius: 80 } }]]);
});

test("iOS checks companion target and reports a lost response as uncertain", async () => {
  let calls = 0;
  const wrong = { describe: async () => ({ udid: "OTHER" }), unary: async () => { calls += 1; } };
  await assert.rejects(performIOS({ ...request, target: "SIMULATOR" }, { client: wrong }),
    (error) => error.code === "session_target_mismatch" && error.extra.sent === false);
  assert.equal(calls, 0);
  const lost = { describe: async () => ({ udid: "SIMULATOR" }), unary: async () => { calls += 1; throw Error("lost"); } };
  await assert.rejects(performIOS({ ...request, target: "SIMULATOR" }, { client: lost }),
    (error) => error.code === "ui_action_uncertain" && error.extra.sent === true);
  assert.equal(calls, 1);
});

test("iOS accepts explicit standard ports and IPv6 endpoints", () => {
  assert.deepEqual(parseCompanionEndpoint("localhost:80"), { host: "localhost", port: 80 });
  assert.deepEqual(parseCompanionEndpoint("[::1]:10882"), { host: "::1", port: 10882 });
  for (const bad of ["host", "host:0", "host:65536", "user@host:80", "host/path:80"])
    assert.throws(() => parseCompanionEndpoint(bad), (error) => error.code === "invalid_value");
});

test("interrupted bridge reaps its live transport before exiting", async (t) => {
  const moduleUrl = new URL("../scripts/pinch_bridge.mjs", import.meta.url).href;
  const source = `
    import { EventEmitter } from 'node:events';
    import { spawn } from 'node:child_process';
    import { installTerminationHandlers, performAndroid, writeResult } from ${JSON.stringify(moduleUrl)};
    const owned = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {stdio:'ignore'});
    let rejectStart;
    class Session extends EventEmitter {
      async start() { process.stdout.write('ready ' + owned.pid + '\\n'); await new Promise((_, reject) => { rejectStart = reject; }); }
      stop() {
        if (!this.stopping) this.stopping = (async () => {
          rejectStart(Error('stopped while starting'));
          owned.kill('SIGTERM'); await new Promise(r => owned.once('exit', r));
        })();
        return this.stopping;
      }
    }
    installTerminationHandlers();
    try { await performAndroid({}, {resolveServer:async () => ({ok:true}), createSession:() => new Session()}); }
    catch (error) { writeResult({ok:false,error_code:error.code,error:error.message}, process.stderr); }
  `;
  const child = spawn(process.execPath, ["--input-type=module", "-e", source]);
  let ownedPid;
  t.after(() => { child.kill("SIGKILL"); if (ownedPid) { try { process.kill(ownedPid, "SIGKILL"); } catch {} } });
  const ready = await new Promise((resolve, reject) => {
    child.stdout.once("data", (data) => resolve(data.toString()));
    child.once("error", reject);
    child.once("exit", () => reject(new Error("bridge exited before startup")));
  });
  ownedPid = Number(ready.trim().split(" ")[1]);
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const exited = new Promise((resolve) => child.once("close", resolve));
  child.kill("SIGTERM");
  assert.equal(await exited, 2);
  assert.equal(JSON.parse(stderr).cleanup_confirmed, true);
  assert.throws(() => process.kill(ownedPid, 0));
  ownedPid = null;
});

test("control-only scrcpy owns one socket and reports forward or JAR cleanup failures", async () => {
  const { ScrcpySession } = await import("../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs");
  for (const failure of ["forward", "jar"]) {
    const calls = [];
    const timeouts = [];
    let connects = 0;
    const exec = async (_file, args, options) => {
      calls.push(args);
      if (args.includes("forward") || args.includes("rm")) timeouts.push(options.timeout);
      if (args.includes("tcp:0")) return { stdout: "12345\n" };
      if (failure === "forward" && args.includes("--remove")) throw Error("forward removal failed");
      if (failure === "jar" && args.includes("rm")) throw Error("jar removal failed");
      return { stdout: "" };
    };
    const spawn = (_file, args) => {
      calls.push(args);
      const child = new EventEmitter();
      child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
      child.kill = () => { queueMicrotask(() => child.emit("exit", 0, null)); };
      return child;
    };
    const connect = () => {
      connects += 1;
      const socket = new EventEmitter();
      Object.assign(socket, { destroyed: false, writableLength: 0, pause() {}, resume() {},
        setNoDelay() {}, destroy() { this.destroyed = true; this.emit("close"); },
        write(_bytes, done) { done?.(); return true; } });
      setImmediate(() => { socket.emit("connect"); setImmediate(() => socket.emit("data", Buffer.from([0]))); });
      return socket;
    };
    const session = new ScrcpySession({ adbPath: "adb", serial: "emulator-5554", serverPath: "/server",
      controlOnly: true, exec, spawn, connect });
    await session.start();
    assert.equal(connects, 1);
    assert.ok(calls.some((args) => args.some((arg) => typeof arg === "string" && arg.includes("video=false"))));
    assert.ok(calls.some((args) => args.some((arg) => typeof arg === "string" && arg.includes("autonom-pinch-"))));
    await assert.rejects(session.stop(), /scrcpy cleanup failed/);
    assert.ok(timeouts.every((timeout) => timeout === 10000));
  }
});

test("iOS HIDPinch crosses a real gRPC stream and waits for acknowledgement", async (t) => {
  const http2 = await import("node:http2");
  const { IdbCompanionClient, GrpcMessageParser, grpcFrame } = await import("../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs");
  const server = http2.createServer();
  const events = [];
  server.on("stream", (stream, headers) => {
    const parser = new GrpcMessageParser();
    stream.on("data", (chunk) => { for (const message of parser.push(chunk)) events.push([headers[":path"], message]); });
    stream.on("end", () => {
      stream.respond({ ":status": 200, "content-type": "application/grpc" }, { waitForTrailers: true });
      stream.on("wantTrailers", () => stream.sendTrailers({ "grpc-status": "0" }));
      // TargetDescriptionResponse { target_description { udid: "SIMULATOR" } }.
      const description = Buffer.from([0x0a, 0x0b, 0x0a, 0x09, ...Buffer.from("SIMULATOR")]);
      stream.end(grpcFrame(headers[":path"].endsWith("/describe") ? description : Buffer.alloc(0)));
    });
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const client = new IdbCompanionClient({ host: "127.0.0.1", port: server.address().port });
  t.after(async () => { await client.close(); await new Promise((resolve) => server.close(resolve)); });
  assert.equal(await performIOS({ ...request, target: "SIMULATOR" }, { client }), "idb");
  assert.equal(events.length, 2);
  assert.ok(events[1][0].endsWith("/hid"));
  assert.equal(decodeHidEvent(events[1][1]).pinch.scale, 2);
});

test("cancelling a pending scrcpy push waits for it before removing the JAR", async () => {
  const { ScrcpySession } = await import("../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs");
  const calls = [];
  let finishPush;
  const exec = async (_file, args) => {
    if (args.includes("push")) {
      calls.push("push-start");
      await new Promise((resolve) => { finishPush = resolve; });
      calls.push("push-finished");
    } else if (args.includes("rm")) calls.push("remove");
    return { stdout: "" };
  };
  const session = new ScrcpySession({ adbPath: "adb", serial: "emulator-5554", serverPath: "/server",
    controlOnly: true, exec });
  const start = session.start().catch(() => {});
  const stop = session.stop();
  assert.deepEqual(calls, ["push-start"]);
  finishPush();
  await Promise.all([start, stop]);
  assert.deepEqual(calls, ["push-start", "push-finished", "remove"]);
});

test("failed forward discovery makes control-only cleanup unconfirmed", async () => {
  const { ScrcpySession } = await import("../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs");
  const calls = [];
  const exec = async (_file, args) => {
    calls.push(args);
    if (args.includes("tcp:0")) throw Error("forward timed out");
    if (args.includes("--list")) throw Error("forward list failed");
    return { stdout: "" };
  };
  const session = new ScrcpySession({ adbPath: "adb", serial: "emulator-5554", serverPath: "/server",
    controlOnly: true, exec });
  await assert.rejects(session.start(), /forward timed out/);
  await assert.rejects(session.stop(), /scrcpy cleanup failed: forward list failed/);
  assert.ok(calls.some((args) => args.includes("rm")), "JAR cleanup was still attempted");
});
