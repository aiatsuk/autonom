import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FPS,
  encodeAdbText,
  extractJpegFrames,
  generateToken,
  isSafeKeyCode,
  normalizeCoordinate,
  parseArgs,
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
  assert.throws(() => parseArgs(["--transport", "magic"]), /auto, screenrecord, or screencap/);
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
