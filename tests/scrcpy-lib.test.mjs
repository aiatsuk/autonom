import assert from "node:assert/strict";
import test from "node:test";

import {
  ANDROID_KEYCODES,
  CLIPBOARD_TEXT_MAX_BYTES,
  CONTROL_MESSAGE_TYPE,
  COPY_KEY,
  DEVICE_CLIPBOARD_TEXT_MAX_BYTES,
  DEVICE_MESSAGE_MAX_SIZE,
  DeviceMessageParser,
  INJECT_TEXT_MAX_BYTES,
  KEY_ACTION,
  MOTION_ACTION,
  MOTION_BUTTON_PRIMARY,
  SCRCPY_PROTOCOL_VERSION,
  VIDEO_CODEC_ID_H264,
  encodeBackOrScreenOn,
  encodeEmpty,
  encodeGetClipboard,
  encodeKeycode,
  encodeScroll,
  encodeSetClipboard,
  encodeText,
  encodeTouch,
  h264CodecString,
  metaStateFor,
  parseScrcpyVersion,
  parseVideoHeader,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-lib.mjs";

// Byte vectors below are copied from scrcpy v4.1 app/tests/test_control_msg_serialize.c,
// app/tests/test_device_msg_deserialize.c and server ControlMessageReaderTest.java.
const AKEYCODE_ENTER = 66;
const AMETA_SHIFT_ON = 0x01;
const AMETA_SHIFT_LEFT_ON = 0x40;

function bytes(...values) {
  return Buffer.from(values);
}

function ascii(text) {
  return [...Buffer.from(text, "ascii")];
}

test("protocol: version and wire constants match scrcpy v4.1", () => {
  assert.equal(SCRCPY_PROTOCOL_VERSION, "4.1");
  // doc/develop.md: "h264" (0x68323634)
  assert.equal(VIDEO_CODEC_ID_H264, 0x68323634);
  assert.equal(Buffer.from([0x68, 0x32, 0x36, 0x34]).toString("ascii"), "h264");
  // ControlMessageReader.java: INJECT_TEXT_MAX_LENGTH and MESSAGE_MAX_SIZE - 14
  assert.equal(INJECT_TEXT_MAX_BYTES, 300);
  assert.equal(CLIPBOARD_TEXT_MAX_BYTES, (1 << 18) - 14);
  // DeviceMessageWriter.java: MESSAGE_MAX_SIZE - 5
  assert.equal(DEVICE_CLIPBOARD_TEXT_MAX_BYTES, (1 << 18) - 5);
  assert.equal(CONTROL_MESSAGE_TYPE.RESET_VIDEO, 17);
  assert.equal(CONTROL_MESSAGE_TYPE.ROTATE_DEVICE, 11);
});

test("protocol: session header carries the capture size and client-resized flag", () => {
  // Streamer.writeSessionMeta: int flags (bit 31 set), int width, int height.
  const session = bytes(0x80, 0, 0, 0, 0, 0, 0x02, 0x3a, 0, 0, 0x05, 0x00);
  assert.deepEqual(parseVideoHeader(session), {
    kind: "session",
    width: 570,
    height: 1280,
    clientResized: false,
  });
  const resized = bytes(0x80, 0, 0, 0x01, 0, 0, 0x05, 0x00, 0, 0, 0x02, 0x3a);
  assert.deepEqual(parseVideoHeader(resized), {
    kind: "session",
    width: 1280,
    height: 570,
    clientResized: true,
  });
});

test("protocol: media header splits config, key frame, PTS and size", () => {
  // Streamer.writeFrameMeta: config packets carry only PACKET_FLAG_CONFIG (1 << 62).
  const config = bytes(0x40, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x1e);
  assert.deepEqual(parseVideoHeader(config), {
    kind: "media",
    config: true,
    keyFrame: false,
    pts: 0,
    size: 30,
  });

  const keyHeader = Buffer.alloc(12);
  keyHeader.writeBigUInt64BE((1n << 61n) | 1_234_567n, 0);
  keyHeader.writeUInt32BE(40_960, 8);
  assert.deepEqual(parseVideoHeader(keyHeader), {
    kind: "media",
    config: false,
    keyFrame: true,
    pts: 1_234_567,
    size: 40_960,
  });

  const deltaHeader = Buffer.alloc(12);
  deltaHeader.writeBigUInt64BE(9_007_199_254_740_991n, 0);
  deltaHeader.writeUInt32BE(0xffff_ffff, 8);
  const delta = parseVideoHeader(new Uint8Array(deltaHeader));
  assert.equal(delta.kind, "media");
  assert.equal(delta.config, false);
  assert.equal(delta.keyFrame, false);
  assert.equal(delta.pts, Number.MAX_SAFE_INTEGER);
  assert.equal(delta.size, 0xffff_ffff);
});

test("protocol: video header parsing rejects short or non-binary input", () => {
  assert.throws(() => parseVideoHeader(Buffer.alloc(11)), /needs 12 bytes/);
  assert.throws(() => parseVideoHeader("not bytes"), /Buffer or Uint8Array/);
});

test("protocol: keycode message equals test_serialize_inject_keycode", () => {
  const encoded = encodeKeycode({
    action: KEY_ACTION.UP,
    keycode: AKEYCODE_ENTER,
    repeat: 5,
    metaState: AMETA_SHIFT_ON | AMETA_SHIFT_LEFT_ON,
  });
  assert.equal(encoded.length, 14);
  assert.deepEqual(
    encoded,
    bytes(
      CONTROL_MESSAGE_TYPE.INJECT_KEYCODE,
      0x01, // AKEY_EVENT_ACTION_UP
      0x00, 0x00, 0x00, 0x42, // AKEYCODE_ENTER
      0x00, 0x00, 0x00, 0x05, // repeat
      0x00, 0x00, 0x00, 0x41, // AMETA_SHIFT_ON | AMETA_SHIFT_LEFT_ON
    ),
  );
  assert.deepEqual(
    encodeKeycode({ action: KEY_ACTION.DOWN, keycode: 29 }),
    bytes(0, 0, 0, 0, 0, 29, 0, 0, 0, 0, 0, 0, 0, 0),
  );
  assert.throws(() => encodeKeycode({ action: 0, keycode: -1 }), /keycode/);
  assert.throws(() => encodeKeycode({ action: 0, keycode: 1.5 }), /keycode/);
  assert.throws(() => encodeKeycode({ action: 256, keycode: 66 }), /action/);
});

test("protocol: text message equals test_serialize_inject_text and its long variant", () => {
  const encoded = encodeText("hello, world!");
  assert.equal(encoded.length, 18);
  assert.deepEqual(
    encoded,
    bytes(
      CONTROL_MESSAGE_TYPE.INJECT_TEXT,
      0x00, 0x00, 0x00, 0x0d, // text length
      ...ascii("hello, world!"),
    ),
  );

  const long = encodeText("a".repeat(INJECT_TEXT_MAX_BYTES));
  assert.equal(long.length, 5 + 300);
  const expected = Buffer.alloc(5 + 300, "a");
  expected[0] = CONTROL_MESSAGE_TYPE.INJECT_TEXT;
  expected[1] = 0x00;
  expected[2] = 0x00;
  expected[3] = 0x01;
  expected[4] = 0x2c; // text length (32 bits)
  assert.deepEqual(long, expected);

  assert.throws(() => encodeText("a".repeat(301)), /limit is 300/);
  // 100 three-byte characters are 300 bytes; one more character crosses the limit.
  assert.equal(encodeText("✓".repeat(100)).length, 305);
  assert.throws(() => encodeText("✓".repeat(101)), /303 UTF-8 bytes/);
  assert.throws(() => encodeText(42), /must be a string/);
});

test("protocol: Unicode text is length-prefixed UTF-8", () => {
  const text = "Café ✓ naïve";
  const encoded = encodeText(text);
  const utf8 = Buffer.from(text, "utf8");
  assert.equal(utf8.length, 16);
  assert.equal(encoded.readUInt32BE(1), 16);
  assert.deepEqual(encoded.subarray(5), utf8);
  // ControlMessageReaderTest.testParseTextEvent uses "testé".
  assert.deepEqual(encodeText("testé"), bytes(1, 0, 0, 0, 6, ...Buffer.from("testé", "utf8")));
});

test("protocol: touch message equals test_serialize_inject_touch_event", () => {
  const encoded = encodeTouch({
    action: MOTION_ACTION.DOWN,
    pointerId: 0x1234567887654321n,
    x: 100,
    y: 200,
    width: 1080,
    height: 1920,
    pressure: 1.0,
    actionButton: MOTION_BUTTON_PRIMARY,
    buttons: MOTION_BUTTON_PRIMARY,
  });
  assert.equal(encoded.length, 32);
  assert.deepEqual(
    encoded,
    bytes(
      CONTROL_MESSAGE_TYPE.INJECT_TOUCH_EVENT,
      0x00, // AKEY_EVENT_ACTION_DOWN
      0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21, // pointer id
      0x00, 0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0xc8, // 100 200
      0x04, 0x38, 0x07, 0x80, // 1080 1920
      0xff, 0xff, // pressure
      0x00, 0x00, 0x00, 0x01, // AMOTION_EVENT_BUTTON_PRIMARY (action button)
      0x00, 0x00, 0x00, 0x01, // AMOTION_EVENT_BUTTON_PRIMARY (buttons)
    ),
  );
});

test("protocol: touch pointer ids, pressure fixed point and validation", () => {
  // ControlMessageReaderTest.testParseTouchEvent writes pointerId -42 as a Java long.
  const negative = encodeTouch({
    action: MOTION_ACTION.DOWN,
    pointerId: -42,
    x: 100,
    y: 200,
    width: 1080,
    height: 1920,
    pressure: 1,
  });
  assert.equal(negative.readBigInt64BE(2), -42n);
  assert.deepEqual(negative.subarray(2, 10), bytes(0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xd6));

  const generic = encodeTouch({ action: MOTION_ACTION.UP, pointerId: -2, x: 285, y: 640, width: 570, height: 1280, pressure: 0 });
  assert.equal(generic[1], 1);
  assert.equal(generic.readBigInt64BE(2), -2n);
  assert.equal(generic.readInt32BE(10), 285);
  assert.equal(generic.readInt32BE(14), 640);
  assert.equal(generic.readUInt16BE(18), 570);
  assert.equal(generic.readUInt16BE(20), 1280);
  assert.equal(generic.readUInt16BE(22), 0);
  assert.equal(generic.readInt32BE(24), 0);
  assert.equal(generic.readInt32BE(28), 0);

  const half = encodeTouch({ action: MOTION_ACTION.MOVE, pointerId: 3, x: 1, y: 2, width: 3, height: 4, pressure: 0.5 });
  assert.equal(half.readUInt16BE(22), 0x8000);
  assert.equal(half.readBigInt64BE(2), 3n);

  const valid = { action: 0, pointerId: 0, x: 0, y: 0, width: 1, height: 1, pressure: 1 };
  assert.throws(() => encodeTouch({ ...valid, x: Number.NaN }), /x must be an integer/);
  assert.throws(() => encodeTouch({ ...valid, pressure: 1.01 }), /pressure/);
  assert.throws(() => encodeTouch({ ...valid, pressure: Number.NaN }), /pressure/);
  assert.throws(() => encodeTouch({ ...valid, width: 65_536 }), /width/);
  assert.throws(() => encodeTouch({ ...valid, pointerId: 2 ** 60 }), /pointerId/);
  assert.throws(() => encodeTouch({ ...valid, pointerId: 1n << 64n }), /pointerId/);
});

test("protocol: scroll message equals test_serialize_inject_scroll_event", () => {
  const encoded = encodeScroll({
    x: 260,
    y: 1026,
    width: 1080,
    height: 1920,
    hScroll: 16,
    vScroll: -16,
    buttons: 1,
  });
  assert.equal(encoded.length, 21);
  assert.deepEqual(
    encoded,
    bytes(
      CONTROL_MESSAGE_TYPE.INJECT_SCROLL_EVENT,
      0x00, 0x00, 0x01, 0x04, 0x00, 0x00, 0x04, 0x02, // 260 1026
      0x04, 0x38, 0x07, 0x80, // 1080 1920
      0x7f, 0xff, // 16 (float encoded as i16 in the range [-16, 16])
      0x80, 0x00, // -16 (float encoded as i16 in the range [-16, 16])
      0x00, 0x00, 0x00, 0x01, // 1
    ),
  );
});

test("protocol: scroll values clamp to [-16, 16] and decode back under server rules", () => {
  // Binary.i16FixedPointToFloat() then * 16, as in ControlMessageReader.parseInjectScrollEvent.
  const decode = (raw) => (raw === 0x7fff ? 1 : raw / 0x8000) * 16;
  const base = { x: 1, y: 1, width: 10, height: 10 };
  const clamped = encodeScroll({ ...base, hScroll: 100, vScroll: -100 });
  assert.equal(clamped.readInt16BE(13), 0x7fff);
  assert.equal(clamped.readInt16BE(15), -0x8000);
  assert.equal(clamped.readInt32BE(17), 0);

  const small = encodeScroll({ ...base, hScroll: 1, vScroll: -2.5 });
  assert.equal(small.readInt16BE(13), 2048);
  assert.equal(small.readInt16BE(15), -5120);
  assert.equal(decode(small.readInt16BE(13)), 1);
  assert.equal(decode(small.readInt16BE(15)), -2.5);
  // ControlMessageReaderTest.testParseScrollEvent: 0 and 0x8000 decode to 0 and -16.
  assert.equal(decode(0), 0);
  assert.equal(decode(-0x8000), -16);

  assert.throws(() => encodeScroll({ ...base, hScroll: Number.NaN, vScroll: 0 }), /hScroll/);
  assert.throws(() => encodeScroll({ ...base, hScroll: 0, vScroll: Infinity }), /vScroll/);
});

test("protocol: back-or-screen-on, get-clipboard and empty messages equal scrcpy vectors", () => {
  // test_serialize_back_or_screen_on
  assert.deepEqual(encodeBackOrScreenOn(KEY_ACTION.UP), bytes(CONTROL_MESSAGE_TYPE.BACK_OR_SCREEN_ON, 0x01));
  // test_serialize_get_clipboard with SC_COPY_KEY_COPY
  assert.deepEqual(encodeGetClipboard(COPY_KEY.COPY), bytes(CONTROL_MESSAGE_TYPE.GET_CLIPBOARD, 1));
  assert.deepEqual(encodeGetClipboard(), bytes(8, 0));
  // test_serialize_expand_notification_panel, expand_settings_panel, collapse_panels,
  // rotate_device and reset_video are one type byte each.
  assert.deepEqual(encodeEmpty(CONTROL_MESSAGE_TYPE.EXPAND_NOTIFICATION_PANEL), bytes(5));
  assert.deepEqual(encodeEmpty(CONTROL_MESSAGE_TYPE.EXPAND_SETTINGS_PANEL), bytes(6));
  assert.deepEqual(encodeEmpty(CONTROL_MESSAGE_TYPE.COLLAPSE_PANELS), bytes(7));
  assert.deepEqual(encodeEmpty(CONTROL_MESSAGE_TYPE.ROTATE_DEVICE), bytes(11));
  assert.deepEqual(encodeEmpty(CONTROL_MESSAGE_TYPE.RESET_VIDEO), bytes(17));
  assert.throws(() => encodeEmpty(2), /not an empty message/);
  assert.throws(() => encodeEmpty(15), /not an empty message/);
  assert.throws(() => encodeGetClipboard(3), /copyKey/);
});

test("protocol: set-clipboard message equals test_serialize_set_clipboard", () => {
  const encoded = encodeSetClipboard({
    sequence: 0x0102030405060708n,
    paste: true,
    text: "hello, world!",
  });
  assert.equal(encoded.length, 27);
  assert.deepEqual(
    encoded,
    bytes(
      CONTROL_MESSAGE_TYPE.SET_CLIPBOARD,
      0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, // sequence
      1, // paste
      0x00, 0x00, 0x00, 0x0d, // text length
      ...ascii("hello, world!"),
    ),
  );
  const unicode = encodeSetClipboard({ text: "naïve ✓" });
  assert.equal(unicode.readBigUInt64BE(1), 0n);
  assert.equal(unicode[9], 0);
  assert.equal(unicode.toString("utf8", 14), "naïve ✓");
});

test("protocol: set-clipboard long variant fills SC_CONTROL_MSG_MAX_SIZE exactly", () => {
  const encoded = encodeSetClipboard({
    sequence: 0x0102030405060708n,
    paste: true,
    text: "a".repeat(CLIPBOARD_TEXT_MAX_BYTES),
  });
  assert.equal(encoded.length, 1 << 18);
  const expected = Buffer.alloc(1 << 18, "a");
  bytes(
    CONTROL_MESSAGE_TYPE.SET_CLIPBOARD,
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, // sequence
    1, // paste
    CLIPBOARD_TEXT_MAX_BYTES >>> 24,
    (CLIPBOARD_TEXT_MAX_BYTES >> 16) & 0xff,
    (CLIPBOARD_TEXT_MAX_BYTES >> 8) & 0xff,
    CLIPBOARD_TEXT_MAX_BYTES & 0xff,
  ).copy(expected, 0);
  assert.deepEqual(encoded, expected);
  assert.throws(() => encodeSetClipboard({ text: "a".repeat(CLIPBOARD_TEXT_MAX_BYTES + 1) }), /limit/);
  assert.throws(() => encodeSetClipboard({ text: "x", sequence: -1 }), /sequence/);
  assert.throws(() => encodeSetClipboard({ text: "x", paste: 1 }), /paste/);
});

test("protocol: device messages equal test_device_msg_deserialize vectors", () => {
  const parser = new DeviceMessageParser();
  // test_deserialize_clipboard
  assert.deepEqual(parser.push(bytes(0, 0x00, 0x00, 0x00, 0x03, 0x41, 0x42, 0x43)), [
    { type: "clipboard", text: "ABC" },
  ]);
  // test_deserialize_ack_set_clipboard
  assert.deepEqual(parser.push(bytes(1, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08)), [
    { type: "ack-clipboard", sequence: 0x0102030405060708n },
  ]);
  // test_deserialize_uhid_output
  assert.deepEqual(parser.push(bytes(2, 0, 42, 0, 5, 0x01, 0x02, 0x03, 0x04, 0x05)), [
    { type: "uhid-output", id: 42, data: bytes(1, 2, 3, 4, 5) },
  ]);
  assert.equal(parser.pendingBytes, 0);
});

test("protocol: device message parser reassembles messages split across chunks", () => {
  const text = "Café ✓ naïve";
  const utf8 = Buffer.from(text, "utf8");
  const header = Buffer.alloc(5);
  header[0] = 0;
  header.writeUInt32BE(utf8.length, 1);
  const stream = Buffer.concat([
    header,
    utf8,
    bytes(1, 0, 0, 0, 0, 0, 0, 0, 7),
    bytes(0, 0, 0, 0, 0),
  ]);

  const parser = new DeviceMessageParser();
  const messages = [];
  for (const byte of stream) messages.push(...parser.push(bytes(byte)));
  assert.deepEqual(messages, [
    { type: "clipboard", text },
    { type: "ack-clipboard", sequence: 7n },
    { type: "clipboard", text: "" },
  ]);

  const together = new DeviceMessageParser().push(stream);
  assert.deepEqual(together, messages);
});

test("protocol: device message parser accepts the largest clipboard and refuses corrupt streams", () => {
  // test_deserialize_clipboard_big: a DEVICE_MSG_MAX_SIZE message.
  const big = Buffer.alloc(DEVICE_MESSAGE_MAX_SIZE, "a");
  big[0] = 0;
  big.writeUInt32BE(DEVICE_CLIPBOARD_TEXT_MAX_BYTES, 1);
  const parser = new DeviceMessageParser();
  assert.deepEqual(parser.push(big.subarray(0, 1000)), []);
  const [message] = parser.push(big.subarray(1000));
  assert.equal(message.type, "clipboard");
  assert.equal(message.text.length, DEVICE_CLIPBOARD_TEXT_MAX_BYTES);
  assert.equal(message.text[0], "a");

  const oversize = Buffer.alloc(5);
  oversize.writeUInt32BE(DEVICE_CLIPBOARD_TEXT_MAX_BYTES + 1, 1);
  assert.throws(() => new DeviceMessageParser().push(oversize), /exceeds the protocol limit/);
  assert.throws(() => new DeviceMessageParser().push(bytes(9)), /Unknown device message type 9/);
});

test("protocol: H.264 codec string comes from the first SPS of the config packet", () => {
  const sps = [0x67, 0x42, 0xc0, 0x29, 0x8d, 0x68, 0x0b, 0x41, 0x6c];
  const pps = [0x68, 0xce, 0x06, 0xe2];
  const config = bytes(0, 0, 0, 1, ...sps, 0, 0, 0, 1, ...pps);
  assert.equal(h264CodecString(config), "avc1.42C029");

  // Three-byte start codes, PPS first, then a High profile SPS.
  const high = bytes(0, 0, 1, ...pps, 0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0xac, 0xd9);
  assert.equal(h264CodecString(high), "avc1.64001F");

  // An emulation prevention byte (00 00 03) inside the header is removed before reading it.
  const escaped = bytes(0, 0, 0, 1, 0x67, 0x00, 0x00, 0x03, 0x1f, 0xff);
  assert.equal(h264CodecString(escaped), "avc1.00001F");

  assert.equal(h264CodecString(bytes(0, 0, 0, 1, ...pps)), null);
  assert.equal(h264CodecString(bytes(0, 0, 0, 1, 0x67, 0x42)), null);
  assert.equal(h264CodecString(Buffer.alloc(0)), null);
});

test("protocol: scrcpy version comes from --version output or a server file name", () => {
  const output = [
    "scrcpy 4.1 <https://github.com/Genymobile/scrcpy>",
    "",
    "Dependencies (compiled / linked):",
    " - SDL: 2.30.9 / 2.30.9",
    " - libavcodec: 61.19.100 / 61.19.100",
  ].join("\n");
  assert.equal(parseScrcpyVersion(output), "4.1");
  assert.equal(parseScrcpyVersion("scrcpy 3.3.1 <https://github.com/Genymobile/scrcpy>\n"), "3.3.1");
  assert.equal(parseScrcpyVersion("scrcpy 4.1-rc1 <https://github.com/Genymobile/scrcpy>"), "4.1-rc1");
  assert.equal(parseScrcpyVersion("/tmp/canvas/scrcpy-server-v4.1"), "4.1");
  assert.equal(parseScrcpyVersion("scrcpy-server-v3.3"), "3.3");
  assert.equal(parseScrcpyVersion("C:\\tools\\scrcpy-server-v4.1.jar"), "4.1");
  assert.equal(parseScrcpyVersion("/opt/homebrew/share/scrcpy/scrcpy-server"), null);
  assert.equal(parseScrcpyVersion("scrcpy-server-v4.1.bak"), null);
  assert.equal(parseScrcpyVersion(""), null);
  assert.equal(parseScrcpyVersion(undefined), null);
});

test("protocol: keyboard map sends Android keycodes from keycodes.h", () => {
  const expected = {
    KeyA: 29, KeyM: 41, KeyZ: 54,
    Digit0: 7, Digit5: 12, Digit9: 16,
    NumpadEnter: 160, NumpadDivide: 154, NumpadMultiply: 155, NumpadSubtract: 156, NumpadAdd: 157,
    Enter: 66, Tab: 61, Backspace: 67, Delete: 112, Escape: 111, Space: 62,
    ArrowUp: 19, ArrowDown: 20, ArrowLeft: 21, ArrowRight: 22,
    Home: 122, End: 123, PageUp: 92, PageDown: 93,
    F1: 131, F12: 142,
    Comma: 55, Period: 56, Slash: 76, Semicolon: 74, Quote: 75,
    BracketLeft: 71, BracketRight: 72, Backslash: 73, Minus: 69, Equal: 70, Backquote: 68,
    ShiftLeft: 59, ShiftRight: 60, ControlLeft: 113, ControlRight: 114,
    AltLeft: 57, AltRight: 58, MetaLeft: 117, MetaRight: 118,
  };
  for (const [code, keycode] of Object.entries(expected)) {
    assert.equal(ANDROID_KEYCODES[code], keycode, code);
  }
  assert.equal(Object.isFrozen(ANDROID_KEYCODES), true);
  for (const value of Object.values(ANDROID_KEYCODES)) {
    assert.ok(Number.isInteger(value) && value > 0 && value < 300, String(value));
  }
  assert.equal(ANDROID_KEYCODES.IntlRo, undefined);
  // keycodes.h: AKEYCODE_NUMPAD_0..9 and NUMPAD_DOT fall back to navigation keys
  // without META_NUM_LOCK_ON, so these codes travel as text instead.
  for (const code of ["Numpad0", "Numpad5", "Numpad9", "NumpadDecimal", "CapsLock"]) {
    assert.equal(ANDROID_KEYCODES[code], undefined, code);
  }
  // Every key CANVAS-007 names is mapped.
  const required = [
    ...Array.from({ length: 26 }, (_, i) => `Key${String.fromCharCode(65 + i)}`),
    ...Array.from({ length: 10 }, (_, i) => `Digit${i}`),
    ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`),
    "Enter", "Tab", "Backspace", "Delete", "Escape",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown",
    "ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "MetaLeft", "MetaRight",
  ];
  for (const code of required) assert.ok(Number.isInteger(ANDROID_KEYCODES[code]), code);
});

test("protocol: meta state sets Android META_* bits for browser modifiers", () => {
  assert.equal(metaStateFor({}), 0);
  assert.equal(metaStateFor(), 0);
  // Same value as the test_serialize_inject_keycode vector.
  assert.equal(metaStateFor({ shiftKey: true }), AMETA_SHIFT_ON | AMETA_SHIFT_LEFT_ON);
  assert.equal(metaStateFor({ ctrlKey: true }), 0x1000 | 0x2000);
  assert.equal(metaStateFor({ altKey: true }), 0x02 | 0x10);
  assert.equal(metaStateFor({ metaKey: true }), 0x10000 | 0x20000);
  assert.equal(
    metaStateFor({ shiftKey: true, ctrlKey: true, altKey: true, metaKey: true }),
    0x41 | 0x3000 | 0x12 | 0x30000,
  );
});
