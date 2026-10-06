/**
 * Pure scrcpy v4.1 wire helpers for the Canvas scrcpy transport (no I/O).
 * Layouts follow scrcpy v4.1 Streamer.java, ControlMessageReader.java,
 * DeviceMessageWriter.java and app/tests/test_control_msg_serialize.c.
 * The scrcpy protocol has no cross-version compatibility, so every layout here
 * is valid only for SCRCPY_PROTOCOL_VERSION. Exports are part of the test and
 * server contracts — keep names stable.
 */

export const SCRCPY_PROTOCOL_VERSION = "4.1";

export const VIDEO_CODEC_ID_H264 = 0x68323634;
export const DEVICE_NAME_FIELD_LENGTH = 64;
export const VIDEO_HEADER_LENGTH = 12;

export const CONTROL_MESSAGE_TYPE = Object.freeze({
  INJECT_KEYCODE: 0,
  INJECT_TEXT: 1,
  INJECT_TOUCH_EVENT: 2,
  INJECT_SCROLL_EVENT: 3,
  BACK_OR_SCREEN_ON: 4,
  EXPAND_NOTIFICATION_PANEL: 5,
  EXPAND_SETTINGS_PANEL: 6,
  COLLAPSE_PANELS: 7,
  GET_CLIPBOARD: 8,
  SET_CLIPBOARD: 9,
  SET_DISPLAY_POWER: 10,
  ROTATE_DEVICE: 11,
  RESET_VIDEO: 17,
});

export const DEVICE_MESSAGE_TYPE = Object.freeze({
  CLIPBOARD: 0,
  ACK_CLIPBOARD: 1,
  UHID_OUTPUT: 2,
});

export const KEY_ACTION = Object.freeze({ DOWN: 0, UP: 1 });
export const MOTION_ACTION = Object.freeze({ DOWN: 0, UP: 1, MOVE: 2, CANCEL: 3 });
export const MOTION_BUTTON_PRIMARY = 1;
export const POINTER_ID = Object.freeze({ MOUSE: -1, GENERIC_FINGER: -2 });
export const COPY_KEY = Object.freeze({ NONE: 0, COPY: 1, CUT: 2 });

const CONTROL_MESSAGE_MAX_SIZE = 1 << 18;
export const INJECT_TEXT_MAX_BYTES = 300;
// type (1) + sequence (8) + paste flag (1) + length (4)
export const CLIPBOARD_TEXT_MAX_BYTES = CONTROL_MESSAGE_MAX_SIZE - 14;
export const DEVICE_MESSAGE_MAX_SIZE = 1 << 18;
// type (1) + length (4)
export const DEVICE_CLIPBOARD_TEXT_MAX_BYTES = DEVICE_MESSAGE_MAX_SIZE - 5;

const EMPTY_MESSAGE_TYPES = new Set([
  CONTROL_MESSAGE_TYPE.EXPAND_NOTIFICATION_PANEL,
  CONTROL_MESSAGE_TYPE.EXPAND_SETTINGS_PANEL,
  CONTROL_MESSAGE_TYPE.COLLAPSE_PANELS,
  CONTROL_MESSAGE_TYPE.ROTATE_DEVICE,
  CONTROL_MESSAGE_TYPE.RESET_VIDEO,
]);

const PTS_MASK = (1n << 61n) - 1n;
const INT32_MIN = -0x8000_0000;
const INT32_MAX = 0x7fff_ffff;
const INT64_MIN = -(1n << 63n);
const UINT64_MAX = (1n << 64n) - 1n;

function asBuffer(value, name) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  throw new TypeError(`${name} must be a Buffer or Uint8Array.`);
}

function requireInt(value, name, minimum, maximum) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be an integer from ${minimum} to ${maximum}.`);
  }
  return value;
}

function requireFinite(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new RangeError(`${name} must be a finite number.`);
  }
  return value;
}

function requireUint64(value, name, minimum) {
  let big;
  if (typeof value === "bigint") big = value;
  else if (Number.isSafeInteger(value)) big = BigInt(value);
  else throw new RangeError(`${name} must be a safe integer or a BigInt.`);
  if (big < minimum || big > UINT64_MAX) {
    throw new RangeError(`${name} does not fit in 64 bits.`);
  }
  // scrcpy's C client stores ids as uint64 and the server reads a Java long; both
  // agree on the two's complement bit pattern.
  return BigInt.asUintN(64, big);
}

function utf8(text, name, maximumBytes) {
  if (typeof text !== "string") throw new TypeError(`${name} must be a string.`);
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length > maximumBytes) {
    throw new RangeError(`${name} is ${bytes.length} UTF-8 bytes; the limit is ${maximumBytes}.`);
  }
  return bytes;
}

// sc_float_to_u16fp() in scrcpy: truncation of f * 2^16 in float, 1.0 saturates to 0xffff.
function floatToU16FixedPoint(value) {
  const scaled = Math.trunc(Math.fround(value) * 0x10000);
  return scaled >= 0xffff ? 0xffff : scaled;
}

// sc_float_to_i16fp() in scrcpy: truncation of f * 2^15 in float, 1.0 saturates to 0x7fff.
function floatToI16FixedPoint(value) {
  const scaled = Math.trunc(Math.fround(value) * 0x8000);
  return scaled >= 0x7fff ? 0x7fff : scaled;
}

function scrollToFixedPoint(value, name) {
  requireFinite(value, name);
  // The wire range is [-16, 16]; scrcpy normalises to [-1, 1] and clamps before encoding.
  const normalized = Math.min(1, Math.max(-1, Math.fround(value) / 16));
  return floatToI16FixedPoint(normalized);
}

function writePosition(buffer, offset, { x, y, width, height }) {
  buffer.writeInt32BE(requireInt(x, "x", INT32_MIN, INT32_MAX), offset);
  buffer.writeInt32BE(requireInt(y, "y", INT32_MIN, INT32_MAX), offset + 4);
  buffer.writeUInt16BE(requireInt(width, "width", 0, 0xffff), offset + 8);
  buffer.writeUInt16BE(requireInt(height, "height", 0, 0xffff), offset + 10);
  return offset + 12;
}

/**
 * Parse the 12-byte header that precedes every video socket message.
 * Session headers carry the capture size; media headers carry flags, PTS and the
 * payload size. PTS is returned as a Number: MediaCodec timestamps are microseconds
 * since capture start, far below 2^53.
 */
export function parseVideoHeader(buf12) {
  const header = asBuffer(buf12, "header");
  if (header.length < VIDEO_HEADER_LENGTH) {
    throw new RangeError(`A video header needs ${VIDEO_HEADER_LENGTH} bytes, got ${header.length}.`);
  }
  if (header[0] & 0x80) {
    return {
      kind: "session",
      width: header.readUInt32BE(4),
      height: header.readUInt32BE(8),
      clientResized: (header[3] & 0x01) !== 0,
    };
  }
  const ptsAndFlags = header.readBigUInt64BE(0);
  return {
    kind: "media",
    config: (ptsAndFlags & (1n << 62n)) !== 0n,
    keyFrame: (ptsAndFlags & (1n << 61n)) !== 0n,
    pts: Number(ptsAndFlags & PTS_MASK),
    size: header.readUInt32BE(8),
  };
}

export function encodeKeycode({ action, keycode, repeat = 0, metaState = 0 }) {
  const buffer = Buffer.alloc(14);
  buffer.writeUInt8(CONTROL_MESSAGE_TYPE.INJECT_KEYCODE, 0);
  buffer.writeUInt8(requireInt(action, "action", 0, 0xff), 1);
  buffer.writeInt32BE(requireInt(keycode, "keycode", 0, INT32_MAX), 2);
  buffer.writeInt32BE(requireInt(repeat, "repeat", 0, INT32_MAX), 6);
  buffer.writeInt32BE(requireInt(metaState, "metaState", 0, INT32_MAX), 10);
  return buffer;
}

export function encodeText(text) {
  const bytes = utf8(text, "text", INJECT_TEXT_MAX_BYTES);
  const buffer = Buffer.alloc(5 + bytes.length);
  buffer.writeUInt8(CONTROL_MESSAGE_TYPE.INJECT_TEXT, 0);
  buffer.writeUInt32BE(bytes.length, 1);
  bytes.copy(buffer, 5);
  return buffer;
}

export function encodeTouch({
  action,
  pointerId,
  x,
  y,
  width,
  height,
  pressure,
  actionButton = 0,
  buttons = 0,
}) {
  requireFinite(pressure, "pressure");
  if (pressure < 0 || pressure > 1) throw new RangeError("pressure must be from 0 to 1.");
  const buffer = Buffer.alloc(32);
  buffer.writeUInt8(CONTROL_MESSAGE_TYPE.INJECT_TOUCH_EVENT, 0);
  buffer.writeUInt8(requireInt(action, "action", 0, 0xff), 1);
  buffer.writeBigUInt64BE(requireUint64(pointerId, "pointerId", INT64_MIN), 2);
  let offset = writePosition(buffer, 10, { x, y, width, height });
  buffer.writeUInt16BE(floatToU16FixedPoint(pressure), offset);
  offset += 2;
  buffer.writeInt32BE(requireInt(actionButton, "actionButton", INT32_MIN, INT32_MAX), offset);
  buffer.writeInt32BE(requireInt(buttons, "buttons", INT32_MIN, INT32_MAX), offset + 4);
  return buffer;
}

export function encodeScroll({ x, y, width, height, hScroll, vScroll, buttons = 0 }) {
  const buffer = Buffer.alloc(21);
  buffer.writeUInt8(CONTROL_MESSAGE_TYPE.INJECT_SCROLL_EVENT, 0);
  const offset = writePosition(buffer, 1, { x, y, width, height });
  buffer.writeInt16BE(scrollToFixedPoint(hScroll, "hScroll"), offset);
  buffer.writeInt16BE(scrollToFixedPoint(vScroll, "vScroll"), offset + 2);
  buffer.writeInt32BE(requireInt(buttons, "buttons", INT32_MIN, INT32_MAX), offset + 4);
  return buffer;
}

export function encodeBackOrScreenOn(action) {
  return Buffer.from([CONTROL_MESSAGE_TYPE.BACK_OR_SCREEN_ON, requireInt(action, "action", 0, 0xff)]);
}

export function encodeGetClipboard(copyKey = COPY_KEY.NONE) {
  return Buffer.from([CONTROL_MESSAGE_TYPE.GET_CLIPBOARD, requireInt(copyKey, "copyKey", 0, 2)]);
}

export function encodeSetClipboard({ sequence = 0, paste = false, text }) {
  const bytes = utf8(text, "text", CLIPBOARD_TEXT_MAX_BYTES);
  if (typeof paste !== "boolean") throw new TypeError("paste must be a boolean.");
  const buffer = Buffer.alloc(14 + bytes.length);
  buffer.writeUInt8(CONTROL_MESSAGE_TYPE.SET_CLIPBOARD, 0);
  buffer.writeBigUInt64BE(requireUint64(sequence, "sequence", 0n), 1);
  buffer.writeUInt8(paste ? 1 : 0, 9);
  buffer.writeUInt32BE(bytes.length, 10);
  bytes.copy(buffer, 14);
  return buffer;
}

export function encodeEmpty(type) {
  if (!EMPTY_MESSAGE_TYPES.has(type)) {
    throw new RangeError(`Control message type ${type} is not an empty message the Canvas sends.`);
  }
  return Buffer.from([type]);
}

/**
 * Incremental parser for device messages read from the control socket.
 * push() returns every complete message and keeps a partial tail for the next chunk.
 * A malformed stream throws; the caller must drop the control connection.
 */
export class DeviceMessageParser {
  #pending = Buffer.alloc(0);

  push(chunk) {
    const bytes = asBuffer(chunk, "chunk");
    this.#pending = this.#pending.length ? Buffer.concat([this.#pending, bytes]) : bytes;
    const messages = [];
    for (;;) {
      const parsed = this.#next();
      if (!parsed) break;
      messages.push(parsed.message);
      this.#pending = this.#pending.subarray(parsed.length);
    }
    return messages;
  }

  get pendingBytes() {
    return this.#pending.length;
  }

  #next() {
    const data = this.#pending;
    if (data.length < 1) return null;
    const type = data[0];
    switch (type) {
      case DEVICE_MESSAGE_TYPE.CLIPBOARD: {
        if (data.length < 5) return null;
        const size = data.readUInt32BE(1);
        if (size > DEVICE_CLIPBOARD_TEXT_MAX_BYTES) {
          throw new RangeError(`Device clipboard message of ${size} bytes exceeds the protocol limit.`);
        }
        if (data.length < 5 + size) return null;
        return {
          length: 5 + size,
          message: { type: "clipboard", text: data.toString("utf8", 5, 5 + size) },
        };
      }
      case DEVICE_MESSAGE_TYPE.ACK_CLIPBOARD:
        if (data.length < 9) return null;
        return { length: 9, message: { type: "ack-clipboard", sequence: data.readBigUInt64BE(1) } };
      case DEVICE_MESSAGE_TYPE.UHID_OUTPUT: {
        if (data.length < 5) return null;
        const size = data.readUInt16BE(3);
        if (data.length < 5 + size) return null;
        return {
          length: 5 + size,
          message: {
            type: "uhid-output",
            id: data.readUInt16BE(1),
            data: Buffer.from(data.subarray(5, 5 + size)),
          },
        };
      }
      default:
        throw new RangeError(`Unknown device message type ${type}.`);
    }
  }
}

function nalUnits(stream) {
  const starts = [];
  for (let i = 0; i + 2 < stream.length; i += 1) {
    if (stream[i] === 0 && stream[i + 1] === 0 && stream[i + 2] === 1) {
      starts.push(i + 3);
      i += 2;
    }
  }
  return starts.map((start, index) => {
    let end = index + 1 < starts.length ? starts[index + 1] - 3 : stream.length;
    // A four-byte start code leaves one zero byte at the end of the previous unit.
    while (end > start && stream[end - 1] === 0) end -= 1;
    return stream.subarray(start, end);
  });
}

function withoutEmulationPrevention(nal, wanted) {
  const out = [];
  let zeros = 0;
  for (let i = 0; i < nal.length && out.length < wanted; i += 1) {
    const byte = nal[i];
    if (zeros >= 2 && byte === 0x03) {
      zeros = 0;
      continue;
    }
    zeros = byte === 0 ? zeros + 1 : 0;
    out.push(byte);
  }
  return out;
}

function hexByte(value) {
  return value.toString(16).toUpperCase().padStart(2, "0");
}

/**
 * WebCodecs codec string `avc1.PPCCLL` (profile_idc, constraint flags, level_idc)
 * from the first SPS NAL unit of an Annex B config packet; null without an SPS.
 */
export function h264CodecString(configPacket) {
  const stream = asBuffer(configPacket, "configPacket");
  const sps = nalUnits(stream).find((nal) => nal.length > 0 && (nal[0] & 0x1f) === 7);
  if (!sps) return null;
  const header = withoutEmulationPrevention(sps, 4);
  if (header.length < 4) return null;
  return `avc1.${hexByte(header[1])}${hexByte(header[2])}${hexByte(header[3])}`;
}

const VERSION_NUMBER = String.raw`(\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?)`;
const VERSION_OUTPUT = new RegExp(String.raw`^\s*scrcpy\s+v?${VERSION_NUMBER}(?=\s|$)`, "m");
const SERVER_FILE = new RegExp(String.raw`^scrcpy-server-v${VERSION_NUMBER}(?:\.jar)?$`);

/**
 * Version from `scrcpy --version` output ("scrcpy 4.1 <https://...>") or from a
 * release file name such as `scrcpy-server-v4.1`; null when neither is present.
 * The full version string is kept so a pre-release never passes as 4.1.
 */
export function parseScrcpyVersion(text) {
  if (typeof text !== "string") return null;
  const output = VERSION_OUTPUT.exec(text);
  if (output) return output[1];
  const name = text.trim().split(/[\\/]/).pop();
  const file = SERVER_FILE.exec(name);
  return file ? file[1] : null;
}

// One video encoder line of scrcpy's `list_encoders=true` output, for example
// "    --video-codec=h264 --video-encoder=c2.android.avc.encoder   (sw)". Android 10 and
// later add "(hw)" or "(sw)", then "[vendor]" and "(alias for NAME)" where they apply.
const ENCODER_LINE = /^\s*--video-codec=(\S+)\s+--video-encoder=(\S+)(.*)$/;

/**
 * The H.264 video encoders scrcpy-server lists with `list_encoders=true`, in its order:
 * [{name, hardware, alias}]. `hardware` is true for "(hw)", false for "(sw)" and null
 * when the line says neither (Android 9 and older); `alias` is true for an alias line.
 */
export function parseH264Encoders(text) {
  const encoders = [];
  for (const line of String(text ?? "").split(/\r?\n/)) {
    const match = ENCODER_LINE.exec(line);
    if (!match || match[1] !== "h264") continue;
    const details = match[3];
    const hardware = /\(hw\)/.test(details) ? true : /\(sw\)/.test(details) ? false : null;
    encoders.push({ name: match[2], hardware, alias: /\(alias for /.test(details) });
  }
  return encoders;
}

/**
 * What a list of H.264 encoders offers: {encoder: "hardware", name} when one of them is
 * "(hw)", {encoder: "software", name} when every one is "(sw)", null when there is none
 * or a line says neither (the Canvas then cannot tell). `name` is the first encoder of
 * that kind that is not an alias.
 */
export function h264EncoderKind(encoders) {
  if (!Array.isArray(encoders) || !encoders.length) return null;
  const first = (hardware) => {
    const matching = encoders.filter((encoder) => encoder.hardware === hardware);
    return (matching.find((encoder) => !encoder.alias) ?? matching[0]).name;
  };
  if (encoders.some((encoder) => encoder.hardware === true)) return { encoder: "hardware", name: first(true) };
  if (encoders.every((encoder) => encoder.hardware === false)) return { encoder: "software", name: first(false) };
  return null;
}

function letterKeys() {
  const keys = {};
  for (let i = 0; i < 26; i += 1) keys[`Key${String.fromCharCode(65 + i)}`] = 29 + i;
  return keys;
}

function digitKeys() {
  const keys = {};
  for (let i = 0; i < 10; i += 1) keys[`Digit${i}`] = 7 + i;
  return keys;
}

function functionKeys() {
  const keys = {};
  for (let i = 1; i <= 12; i += 1) keys[`F${i}`] = 130 + i;
  return keys;
}

/**
 * KeyboardEvent.code → Android AKEYCODE_* value (scrcpy v4.1 keycodes.h).
 * Numpad digits and the numpad decimal point are left out on purpose: Android
 * reads them as Insert/End/arrows/Delete unless the event carries META_NUM_LOCK_ON,
 * which metaStateFor() cannot know, so the page sends them as text instead.
 * CapsLock is left out because macOS browsers report it as a toggle (keydown when
 * it turns on, keyup when it turns off), which would leave the device key half pressed.
 */
export const ANDROID_KEYCODES = Object.freeze({
  ...letterKeys(),
  ...digitKeys(),
  ...functionKeys(),
  Space: 62,
  Enter: 66,
  NumpadEnter: 160,
  Tab: 61,
  Backspace: 67,
  Delete: 112,
  Escape: 111,
  Insert: 124,
  ArrowUp: 19,
  ArrowDown: 20,
  ArrowLeft: 21,
  ArrowRight: 22,
  Home: 122,
  End: 123,
  PageUp: 92,
  PageDown: 93,
  Comma: 55,
  Period: 56,
  Slash: 76,
  Semicolon: 74,
  Quote: 75,
  BracketLeft: 71,
  BracketRight: 72,
  Backslash: 73,
  Minus: 69,
  Equal: 70,
  Backquote: 68,
  NumpadDivide: 154,
  NumpadMultiply: 155,
  NumpadSubtract: 156,
  NumpadAdd: 157,
  ShiftLeft: 59,
  ShiftRight: 60,
  ControlLeft: 113,
  ControlRight: 114,
  AltLeft: 57,
  AltRight: 58,
  MetaLeft: 117,
  MetaRight: 118,
});

function usLetterChars() {
  const chars = {};
  for (let i = 0; i < 26; i += 1) {
    const letter = String.fromCharCode(97 + i);
    chars[`Key${letter.toUpperCase()}`] = [letter, letter.toUpperCase()];
  }
  return chars;
}

function usDigitChars() {
  const chars = {};
  for (let i = 0; i < 10; i += 1) chars[`Digit${i}`] = [String(i), ")!@#$%^&*("[i]];
  return chars;
}

/**
 * KeyboardEvent.code → what a US layout types there, without and with Shift. The page
 * compares it with KeyboardEvent.key to tell a key of another layout from a US one.
 */
export const US_KEY_CHARS = Object.freeze({
  ...usLetterChars(),
  ...usDigitChars(),
  Space: [" ", " "],
  Comma: [",", "<"],
  Period: [".", ">"],
  Slash: ["/", "?"],
  Semicolon: [";", ":"],
  Quote: ["'", "\""],
  BracketLeft: ["[", "{"],
  BracketRight: ["]", "}"],
  Backslash: ["\\", "|"],
  Minus: ["-", "_"],
  Equal: ["=", "+"],
  Backquote: ["`", "~"],
  NumpadDivide: ["/", "/"],
  NumpadMultiply: ["*", "*"],
  NumpadSubtract: ["-", "-"],
  NumpadAdd: ["+", "+"],
});

/**
 * What one browser key press sends on the scrcpy transport: `{ code }`, an Android keycode
 * pressed and released, `{ text }`, or null for nothing. Keycodes stand for US key
 * positions, so a printable key is sent as its character when it types something else
 * than a US layout would there (Shift considered), or sits where the keycode table has no
 * entry: other layouts (AZERTY, QWERTZ, Cyrillic, ...), the AltGr layer (Ctrl+Alt, or
 * the AltGraph modifier) and the macOS Option layer (Alt alone) type their own
 * characters. Plain Ctrl and Meta shortcuts, and keys that type what US would, keep
 * their keycodes, except Ctrl+V and Cmd+V, which send nothing so that the browser's own
 * paste event carries the clipboard. A dead key (and a keydown that only feeds an input
 * method while it composes) sends nothing: the composed character comes with the next
 * keydown. Pure, with its tables passed in, because the page script gets its source.
 */
export function keyInputFor(event, keycodes, usChars) {
  const key = event.key;
  if (key === "Dead" || (event.isComposing && (key === "Process" || key === "Unidentified"))) return null;
  const code = keycodes[event.code];
  const altGraph = Boolean(event.ctrlKey && event.altKey) ||
    (typeof event.getModifierState === "function" && event.getModifierState("AltGraph"));
  const shortcut = Boolean(event.metaKey) || (Boolean(event.ctrlKey) && !altGraph);
  if (shortcut && event.code === "KeyV") return null;
  if ([...key].length === 1 && !shortcut) {
    const us = usChars[event.code];
    if (code === undefined || !us || us[event.shiftKey ? 1 : 0] !== key) return { text: key };
  }
  return code === undefined ? null : { code };
}

export const META_STATE = Object.freeze({
  SHIFT_ON: 0x01,
  ALT_ON: 0x02,
  ALT_LEFT_ON: 0x10,
  SHIFT_LEFT_ON: 0x40,
  CTRL_ON: 0x1000,
  CTRL_LEFT_ON: 0x2000,
  META_ON: 0x10000,
  META_LEFT_ON: 0x20000,
});

/**
 * Android meta state for browser modifier flags. Browsers do not say which side
 * is held, so the left-side bit is set together with the generic bit, as scrcpy does.
 */
export function metaStateFor({ shiftKey = false, ctrlKey = false, altKey = false, metaKey = false } = {}) {
  let state = 0;
  if (shiftKey) state |= META_STATE.SHIFT_ON | META_STATE.SHIFT_LEFT_ON;
  if (ctrlKey) state |= META_STATE.CTRL_ON | META_STATE.CTRL_LEFT_ON;
  if (altKey) state |= META_STATE.ALT_ON | META_STATE.ALT_LEFT_ON;
  if (metaKey) state |= META_STATE.META_ON | META_STATE.META_LEFT_ON;
  return state;
}
