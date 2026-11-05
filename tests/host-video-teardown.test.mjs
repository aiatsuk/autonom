// A host capture whose stop rejects (a failed cutout restore) must not stop the
// scrcpy session's teardown: the forward is removed, the server ended, and the
// session restarts on the Android encoder.
import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { Duplex, PassThrough } from "node:stream";
import { ScrcpySession } from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs";

test("a host stop that rejects still tears down and restarts the session", { timeout: 20_000 }, async () => {
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error);
  process.on("unhandledRejection", onUnhandled);
  const commands = [], adbCalls = [];
  let host, attemptConnections = 0, disabled = 0;
  const metadata = Buffer.concat([Buffer.alloc(64), Buffer.from([0x68, 0x32, 0x36, 0x34])]);
  const spawn = (_file, args) => {
    commands.push(args.at(-1)); attemptConnections = 0;
    const child = new EventEmitter(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.kill = () => {}; return child;
  };
  const connect = () => {
    const first = attemptConnections++ === 0;
    const socket = new Duplex({ read() {}, write(_b, _e, done) { done(); } });
    socket.setNoDelay = () => {};
    queueMicrotask(() => {
      socket.emit("connect");
      if (first) socket.push(Buffer.concat([Buffer.from([0]),
        commands.at(-1).includes("video=false") ? Buffer.alloc(64) : metadata]));
    });
    return socket;
  };
  const session = new ScrcpySession({
    adbPath: "fake", serial: "emulator-5556", serverPath: "fake", maxSize: 1280, maxFps: 60, bitRate: 12000000,
    exec: async (_file, args) => {
      adbCalls.push(args.slice(2).join(" "));
      return { stdout: args.includes("forward") && !args.includes("--remove") ? "43210" : "" };
    },
    spawn, connect,
    onHostDisabled: () => { disabled += 1; },
    hostFactory: async () => () => {
      const stdout = new PassThrough();
      host = { stdout, stats: {}, requestKeyFrame() {}, abort() {},
        async stop() { stdout.destroy(); throw new Error("restore failed"); } };
      queueMicrotask(() => stdout.write(metadata));
      return host;
    },
  });
  session.on("error", () => {});
  try {
    await session.start();
    assert.equal(session.stats.videoBackend, "videotoolbox");
    host.stdout.destroy();
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline && !(session.stats.restarts >= 1 && session.state === "streaming")) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(session.state, "streaming");
    assert.ok(session.stats.restarts >= 1);
    assert.ok(adbCalls.some((call) => call.includes("forward --remove")), "forward removed");
    assert.ok(adbCalls.some((call) => call.includes("pkill")), "server ended");
    assert.equal(disabled, 1);
    assert.equal(commands.at(-1).includes("video=false"), false, "restarted on the Android encoder");
    assert.deepEqual(unhandled, []);
  } finally {
    await session.stop().catch(() => {});
    process.off("unhandledRejection", onUnhandled);
  }
});
