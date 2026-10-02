import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FPS,
  STREAM_KEYCODES,
  encodeAdbText,
  extractJpegFrames,
  generateToken,
  isAllowedHost,
  isCanvasOrigin,
  isSafeKeyCode,
  normalizeCoordinate,
  parseArgs,
  parseControlMessage,
  parseCookies,
  parseWmSize,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs";

test("parseArgs returns secure defaults and explicit overrides", () => {
  const defaults = parseArgs([]);
  assert.equal(defaults.fps, DEFAULT_FPS);
  assert.equal(defaults.transport, "auto");
  assert.equal(defaults.noAuth, false);

  const values = parseArgs([
    "--serial", "emulator-5554",
    "--port", "8080",
    "--fps", "30",
    "--max-size", "1920",
    "--bit-rate", "12000000",
    "--transport", "screenrecord",
    "--no-auth",
  ]);
  assert.equal(values.serial, "emulator-5554");
  assert.equal(values.port, 8080);
  assert.equal(values.fps, 30);
  assert.equal(values.maxSize, 1920);
  assert.equal(values.bitRate, 12_000_000);
  assert.equal(values.transport, "screenrecord");
  assert.equal(values.noAuth, true);
});

test("parseArgs rejects unsafe ranges and unknown modes", () => {
  assert.throws(() => parseArgs(["--fps", "0"]), /between 1 and 60/);
  assert.equal(parseArgs(["--port", "0"]).port, 0);
  assert.throws(() => parseArgs(["--port", "70000"]), /0 to 65535/);
  assert.throws(() => parseArgs(["--transport", "magic"]), /auto, scrcpy, screenrecord, or screencap/);
});

test("parseArgs accepts the scrcpy transport and its server options", () => {
  const values = parseArgs([
    "--transport", "scrcpy",
    "--scrcpy-server", "/tmp/scrcpy-server",
    "--scrcpy-version", "4.1",
  ]);
  assert.equal(values.transport, "scrcpy");
  assert.equal(values.scrcpyServer, "/tmp/scrcpy-server");
  assert.equal(values.scrcpyVersion, "4.1");
  assert.equal(values.fpsExplicit, undefined);
  assert.equal(parseArgs(["--fps", "15"]).fpsExplicit, true);
  assert.throws(() => parseArgs(["--scrcpy-version", "4.1"]), /--scrcpy-server/);
  assert.throws(() => parseArgs(["--scrcpy-server", "x", "--scrcpy-version", "four"]), /look like 4\.1/);
});

test("tokens, keycodes, and conservative text encoding", () => {
  assert.ok(generateToken().length >= 24);
  assert.equal(isSafeKeyCode("KEYCODE_BACK"), true);
  assert.equal(isSafeKeyCode("KEYCODE_UNKNOWN_INJECTION"), false);
  assert.equal(encodeAdbText("hello world"), "hello%sworld");
  assert.throws(() => encodeAdbText("こんにちは"), /conservative ASCII/);
});

test("wm size parsing prefers the active override", () => {
  assert.deepEqual(parseWmSize("Physical size: 1080x2400\nOverride size: 720x1600"), {
    width: 720,
    height: 1600,
  });
  assert.equal(parseWmSize("n/a"), null);
});

test("JPEG extraction preserves partial frames across chunks", () => {
  const first = Buffer.from([0x00, 0xff, 0xd8, 0x01, 0x02]);
  const parsedFirst = extractJpegFrames(first);
  assert.equal(parsedFirst.frames.length, 0);
  assert.deepEqual([...parsedFirst.carry], [0xff, 0xd8, 0x01, 0x02]);

  const second = Buffer.from([0x03, 0xff, 0xd9, 0xff, 0xd8, 0x04, 0xff, 0xd9, 0x99]);
  const parsedSecond = extractJpegFrames(second, parsedFirst.carry);
  assert.equal(parsedSecond.frames.length, 2);
  assert.deepEqual([...parsedSecond.frames[0]], [0xff, 0xd8, 0x01, 0x02, 0x03, 0xff, 0xd9]);
  assert.deepEqual([...parsedSecond.frames[1]], [0xff, 0xd8, 0x04, 0xff, 0xd9]);
});

test("coordinate normalization rounds and clamps", () => {
  assert.equal(normalizeCoordinate(12.6, "x"), 13);
  assert.equal(normalizeCoordinate(-20, "x"), 0);
  assert.equal(normalizeCoordinate(200000, "x"), 100000);
  assert.throws(() => normalizeCoordinate("not-a-number", "x"), /must be a number/);
});

test("Host and Origin checks accept only this Canvas", () => {
  assert.equal(isAllowedHost("127.0.0.1:3277", 3277), true);
  assert.equal(isAllowedHost("LOCALHOST:3277", 3277), true);
  assert.equal(isAllowedHost("127.0.0.1:3278", 3277), false);
  assert.equal(isAllowedHost("evil.test:3277", 3277), false);
  assert.equal(isAllowedHost(undefined, 3277), false);
  assert.equal(isCanvasOrigin("http://127.0.0.1:3277", 3277), true);
  assert.equal(isCanvasOrigin("http://localhost:3277", 3277), true);
  assert.equal(isCanvasOrigin("https://127.0.0.1:3277", 3277), false);
  assert.equal(isCanvasOrigin("http://evil.test", 3277), false);
  assert.equal(isCanvasOrigin("null", 3277), false);
  assert.deepEqual({ ...parseCookies("a=1; autonom_session=abc=; b") }, { a: "1", autonom_session: "abc=" });
  assert.equal(parseCookies("__proto__=x").toString, undefined);
});

test("control messages are validated strictly and keep only known fields", () => {
  assert.deepEqual(
    parseControlMessage(JSON.stringify({ t: "touch", a: "move", id: 3, x: 0, y: 1, p: 0.5, extra: true })),
    { t: "touch", a: "move", id: 3, x: 0, y: 1, p: 0.5 });
  assert.deepEqual(parseControlMessage(JSON.stringify({ t: "key", a: "down", code: 29 })),
    { t: "key", a: "down", code: 29, meta: 0, repeat: 0 });
  assert.deepEqual(parseControlMessage(JSON.stringify({ t: "text", text: "Café ✓" })),
    { t: "text", text: "Café ✓", sensitive: false });
  assert.deepEqual(parseControlMessage(JSON.stringify({ t: "system", op: "quick-settings" })),
    { t: "system", op: "quick-settings" });
  assert.deepEqual(parseControlMessage(JSON.stringify({ t: "scroll", x: 0.5, y: 0.5, dx: -16, dy: 16 })),
    { t: "scroll", x: 0.5, y: 0.5, dx: -16, dy: 16 });
  // The allowlist is the browser key map plus the existing system keys.
  assert.ok(STREAM_KEYCODES.has(29) && STREAM_KEYCODES.has(4) && STREAM_KEYCODES.has(224));
  assert.equal(STREAM_KEYCODES.has(24), false);

  const rejects = [
    ["{", null], ["[1]", null], [JSON.stringify({ t: "fly" }), "fly"],
    [JSON.stringify({ t: "touch", a: "down", id: 1, x: Number.MAX_VALUE, y: 0 }), "touch"],
    [JSON.stringify({ t: "touch", a: "down", id: 1, x: 0.5 }), "touch"],
    [JSON.stringify({ t: "scroll", x: 0.5, y: 0.5, dx: 0, dy: -17 }), "scroll"],
    [JSON.stringify({ t: "key", a: "down", code: 24 }), "key"],
    [JSON.stringify({ t: "key", a: "down", code: 29, meta: 0x800000 }), "key"],
    [JSON.stringify({ t: "text", text: "é".repeat(151) }), "text"],
    [JSON.stringify({ t: "paste", text: "x".repeat(64 * 1024 + 1) }), "paste"],
    [JSON.stringify({ t: "control", mode: "steal" }), "control"],
  ];
  for (const [raw, type] of rejects) {
    assert.throws(() => parseControlMessage(raw), (error) => error.for === type, raw.slice(0, 60));
  }
});
