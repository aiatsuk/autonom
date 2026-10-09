import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_BIT_RATE,
  DEFAULT_FPS,
  DEFAULT_MAX_SIZE,
  SOFTWARE_ENCODER_MAX_SIZE,
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
  screenrecordMaxSize,
  streamMaxSize,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs";

test("parseArgs returns secure defaults and explicit overrides", () => {
  const defaults = parseArgs([]);
  assert.equal(defaults.fps, DEFAULT_FPS);
  assert.equal(defaults.transport, "auto");
  assert.equal(defaults.noAuth, false);
  // 12 Mbit/s keeps the bits per frame at 60 fps that 8 Mbit/s gave at about 36 fps.
  assert.equal(DEFAULT_BIT_RATE, 12_000_000);
  assert.equal(defaults.bitRate, 12_000_000);

  const values = parseArgs([
    "--serial", "emulator-5554",
    "--port", "8080",
    "--fps", "30",
    "--max-size", "1920",
    "--bit-rate", "16000000",
    "--transport", "screenrecord",
    "--no-auth",
  ]);
  assert.equal(values.serial, "emulator-5554");
  assert.equal(values.port, 8080);
  assert.equal(values.fps, 30);
  assert.equal(values.maxSize, 1920);
  assert.equal(values.bitRate, 16_000_000);
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

test("parseArgs takes --tools and --autonom overrides for the panel tools and the log feed", () => {
  const defaults = parseArgs([]);
  assert.equal(defaults.tools, undefined);
  assert.equal(defaults.autonom, undefined);
  const values = parseArgs(["--tools", "/tmp/fake-tools.mjs", "--autonom", "/tmp/fake-autonom.mjs"]);
  assert.equal(values.tools, "/tmp/fake-tools.mjs");
  assert.equal(values.autonom, "/tmp/fake-autonom.mjs");
  assert.throws(() => parseArgs(["--tools"]), /Pass a value after --tools/);
  assert.throws(() => parseArgs(["--autonom", "--no-auth"]), /Pass a value after --autonom/);
});

test("parseArgs marks an explicit --max-size and accepts 0 as native size", () => {
  const defaults = parseArgs([]);
  assert.equal(defaults.maxSize, DEFAULT_MAX_SIZE);
  assert.equal(DEFAULT_MAX_SIZE, 1280);
  assert.equal(defaults.maxSizeExplicit, undefined);
  assert.deepEqual([parseArgs(["--max-size", "1280"]).maxSize, parseArgs(["--max-size", "1280"]).maxSizeExplicit],
    [1280, true]);
  assert.deepEqual([parseArgs(["--max-size", "0"]).maxSize, parseArgs(["--max-size", "0"]).maxSizeExplicit], [0, true]);
  assert.equal(parseArgs(["--max-size", "320"]).maxSize, 320);
  assert.equal(parseArgs(["--max-size", "4096"]).maxSize, 4096);
  for (const value of ["319", "4097", "-1", "1.5", "native", ""]) {
    assert.throws(() => parseArgs(["--max-size", value]), /--max-size/, value);
  }
  assert.throws(() => parseArgs(["--max-size", "100"]), /0 \(native size\) or an integer from 320 to 4096/);
});

test("streamMaxSize follows the encoder only without --max-size", () => {
  const software = { encoder: "software", name: "c2.android.avc.encoder" };
  const hardware = { encoder: "hardware", name: "c2.qti.avc.encoder" };
  const defaults = parseArgs([]);
  assert.equal(SOFTWARE_ENCODER_MAX_SIZE, 2048);
  assert.deepEqual(streamMaxSize(defaults, software),
    { maxSize: 2048, source: "encoder", encoder: "software", encoderName: "c2.android.avc.encoder" });
  assert.deepEqual(streamMaxSize(defaults, hardware),
    { maxSize: 0, source: "encoder", encoder: "hardware", encoderName: "c2.qti.avc.encoder" });
  // A failed probe, or one whose answer says nothing usable, keeps the old default.
  for (const probe of [null, undefined, {}, { encoder: "unknown", name: "x" }]) {
    assert.deepEqual(streamMaxSize(defaults, probe),
      { maxSize: 1280, source: "default", encoder: null, encoderName: null });
  }
  // An explicit size wins over any encoder, 0 included.
  assert.deepEqual(streamMaxSize(parseArgs(["--max-size", "1024"]), hardware),
    { maxSize: 1024, source: "explicit", encoder: "hardware", encoderName: "c2.qti.avc.encoder" });
  assert.deepEqual(streamMaxSize(parseArgs(["--max-size", "0"])),
    { maxSize: 0, source: "explicit", encoder: null, encoderName: null });
  assert.deepEqual(streamMaxSize(parseArgs(["--max-size", "2048"]), software).maxSize, 2048);
});

test("screenrecordMaxSize never passes 0 to the screenrecord path", () => {
  assert.equal(screenrecordMaxSize(parseArgs([])), 1280);
  assert.equal(screenrecordMaxSize(parseArgs(["--max-size", "720"])), 720);
  assert.equal(screenrecordMaxSize(parseArgs(["--max-size", "0"])), 4096);
});

test("tokens, keycodes, and conservative text encoding", () => {
  assert.ok(generateToken().length >= 24);
  for (let i = 0; i < 500; i += 1) assert.ok(!generateToken(3).startsWith("-"));
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

// Workspace and multi-device options (contract 3.5); appended, the tests above are unchanged.
test("parseArgs: no device flags is the single-device Canvas, as before", () => {
  const options = parseArgs([]);
  assert.equal(options.mode, "single");
  assert.deepEqual(options.devices, []);
  assert.equal(options.workspace, undefined);
  assert.equal(parseArgs(["--serial", "emulator-5580"]).mode, "single");
});

test("parseArgs: one --device stands for --platform and --target", () => {
  const android = parseArgs(["--device", "android:127.0.0.1:5555"]);
  assert.equal(android.mode, "single");
  assert.equal(android.platform, "android");
  assert.equal(android.target, "127.0.0.1:5555");
  const ios = parseArgs(["--device", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"]);
  assert.equal(ios.platform, "ios");
  assert.equal(ios.target, "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5");
  assert.throws(() => parseArgs(["--platform", "ios", "--device", "android:x"]), /does not match/);
});

test("parseArgs: --workspace, --split or two --device select the workspace", () => {
  const two = parseArgs(["--device", "android:emulator-5580", "--device", "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"]);
  assert.equal(two.mode, "workspace");
  assert.equal(two.workspace, "default");
  assert.deepEqual(two.devices, [{ platform: "android", target: "emulator-5580" },
    { platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5" }]);
  assert.equal(parseArgs(["--split"]).workspace, "default");
  const named = parseArgs(["--workspace", "qa.run-1", "--ephemeral", "--device", "android:emulator-5580"]);
  assert.equal(named.mode, "workspace");
  assert.equal(named.workspace, "qa.run-1");
  assert.equal(named.ephemeral, true);
  assert.deepEqual(named.devices, [{ platform: "android", target: "emulator-5580" }]);
  assert.equal(named.target, undefined, "a workspace --device is not the single target");
});

test("parseArgs: device flags are validated", () => {
  for (const value of ["emulator-5580", "windows:x", "android:", "android:a b", `android:${"x".repeat(129)}`]) {
    assert.throws(() => parseArgs(["--device", value]), /--device must be/, value);
  }
  assert.throws(() => parseArgs(["--device", "android:a", "--device", "android:a"]), /same device twice/);
  const nine = Array.from({ length: 9 }, (_, i) => ["--device", `android:emulator-${5554 + 2 * i}`]).flat();
  assert.throws(() => parseArgs(nine), /at most 8/);
  assert.throws(() => parseArgs(["--serial", "x", "--device", "android:y"]), /cannot be combined/);
  assert.throws(() => parseArgs(["--target", "x", "--device", "android:y"]), /cannot be combined/);
  assert.throws(() => parseArgs(["--serial", "x", "--split"]), /single-device Canvas/);
  for (const name of ["", "a/b", "x".repeat(41), "a b"]) {
    assert.throws(() => parseArgs(["--workspace", name]), name === "" ? /Pass a value|--workspace/ : /--workspace must be/);
  }
  assert.throws(() => parseArgs(["--device"]), /Pass a value after --device/);
});

test("parseArgs: --bootable, --shutdown-booted, --install-root and --captures-dir", () => {
  const options = parseArgs(["--split", "--bootable", "avd:Autonom_Split2_API36@5586", "--bootable", "avd:Pixel",
    "--bootable", "simulator:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", "--shutdown-booted",
    "--install-root", "/src/app/build", "--install-root", "/src/other", "--captures-dir", "/tmp/captures"]);
  assert.deepEqual(options.bootable, [
    { kind: "avd", name: "Autonom_Split2_API36", port: 5586, udid: null },
    { kind: "avd", name: "Pixel", port: null, udid: null },
    { kind: "simulator", name: null, port: null, udid: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5" },
  ]);
  assert.equal(options.shutdownBooted, true);
  assert.deepEqual(options.installRoots, ["/src/app/build", "/src/other"]);
  assert.equal(options.capturesDir, "/tmp/captures");
  assert.deepEqual(parseArgs([]).installRoots, []);
  // The single-device Canvas takes install roots and the captures folder too.
  assert.deepEqual(parseArgs(["--serial", "x", "--install-root", "/a"]).installRoots, ["/a"]);
  for (const value of ["avd:", "avd:a@5555", "avd:a@5684", "avd:a@55860", "avd:a b", "avd:a@5586@1",
    "simulator:nope", "floppy:x"]) {
    assert.throws(() => parseArgs(["--split", "--bootable", value]), /--bootable/, value);
  }
  const nine = Array.from({ length: 9 }, (_, i) => ["--bootable", `avd:A${i}`]).flat();
  assert.throws(() => parseArgs(["--split", ...nine]), /at most 8/);
  assert.throws(() => parseArgs(["--bootable", "avd:Pixel"]), /needs a workspace/);
  assert.throws(() => parseArgs(["--shutdown-booted"]), /needs a workspace/);
  assert.throws(() => parseArgs(["--ephemeral"]), /needs a workspace/);
});

test("a token starting with a dash passes as --token=VALUE", () => {
  // Before: "--token=..." was an unknown flag, and "--token" "--x" was refused as missing.
  assert.equal(parseArgs(["--token=--dash-token_1"]).token, "--dash-token_1");
  assert.equal(parseArgs(["--token=-d"]).token, "-d");
  assert.equal(parseArgs(["--token=a=b"]).token, "a=b");
  assert.equal(parseArgs(["--token", "plain"]).token, "plain");
  assert.throws(() => parseArgs(["--token="]), /Pass a value after --token/);
});
