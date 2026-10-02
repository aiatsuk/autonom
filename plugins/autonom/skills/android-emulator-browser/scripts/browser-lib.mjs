#!/usr/bin/env node
/**
 * Shared helpers for the Android emulator browser bridge.
 * Exports are part of the test and server contracts — keep names stable.
 */
import { randomBytes } from "node:crypto";

import { ANDROID_KEYCODES } from "./scrcpy-lib.mjs";

export const DEFAULT_PORT = 3277;
export const DEFAULT_FPS = 15;
// scrcpy only sends frames when the screen changes, so its cap can be higher than
// the multipart default; an explicit --fps still applies to it.
export const DEFAULT_SCRCPY_MAX_FPS = 60;
export const DEFAULT_MAX_SIZE = 1280;
export const DEFAULT_BIT_RATE = 8_000_000;
export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_CONTROL_MESSAGE_BYTES = 64 * 1024;
export const MAX_POINTERS = 10;

const ALLOWED_KEYCODES = new Set([
  "KEYCODE_BACK",
  "KEYCODE_HOME",
  "KEYCODE_ENTER",
  "KEYCODE_DEL",
  "KEYCODE_TAB",
  "KEYCODE_DPAD_UP",
  "KEYCODE_DPAD_DOWN",
  "KEYCODE_DPAD_LEFT",
  "KEYCODE_DPAD_RIGHT",
  "KEYCODE_DPAD_CENTER",
  "KEYCODE_WAKEUP",
  "KEYCODE_POWER",
  "KEYCODE_APP_SWITCH",
  "KEYCODE_ESCAPE",
  "KEYCODE_MOVE_HOME",
  "KEYCODE_MOVE_END",
]);

// Android keycodes of the KEYCODE_* names above, for the scrcpy control channel.
const SYSTEM_KEYCODES = Object.freeze({
  KEYCODE_HOME: 3,
  KEYCODE_BACK: 4,
  KEYCODE_DPAD_UP: 19,
  KEYCODE_DPAD_DOWN: 20,
  KEYCODE_DPAD_LEFT: 21,
  KEYCODE_DPAD_RIGHT: 22,
  KEYCODE_DPAD_CENTER: 23,
  KEYCODE_POWER: 26,
  KEYCODE_TAB: 61,
  KEYCODE_ENTER: 66,
  KEYCODE_DEL: 67,
  KEYCODE_ESCAPE: 111,
  KEYCODE_MOVE_HOME: 122,
  KEYCODE_MOVE_END: 123,
  KEYCODE_APP_SWITCH: 187,
  KEYCODE_WAKEUP: 224,
});

export const STREAM_KEYCODES = new Set([
  ...Object.values(ANDROID_KEYCODES),
  ...Object.values(SYSTEM_KEYCODES),
]);

// Every Android modifier and lock bit (AMETA_* in scrcpy input.h); anything else is refused.
const META_MASK = 0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0x20 | 0x40 | 0x80 |
  0x1000 | 0x2000 | 0x4000 | 0x10000 | 0x20000 | 0x40000 | 0x100000 | 0x200000 | 0x400000;

export const SYSTEM_OPS = Object.freeze([
  "back", "home", "app-switch", "power", "volume-up", "volume-down", "wake",
  "notifications", "quick-settings", "collapse", "rotate", "keyframe",
]);
export const CONTROL_MODES = Object.freeze(["pause", "resume", "takeover", "release"]);
export const ORIGINS = Object.freeze(["human", "agent", "replay", "system"]);

const TRANSPORTS = new Set(["auto", "scrcpy", "screenrecord", "screencap"]);
const VERSION_TEXT = /^\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?$/;
const ASCII_TEXT = /^[A-Za-z0-9 ._@:/,+\-=!?]*$/;
const SOI = Buffer.from([0xff, 0xd8]);
const EOI = Buffer.from([0xff, 0xd9]);

export function generateToken(bytes = 24) {
  return randomBytes(bytes).toString("base64url");
}

export function clamp(value, minimum, maximum) {
  if (value < minimum) return minimum;
  if (value > maximum) return maximum;
  return value;
}

function requireFlagValue(argv, index, flag) {
  const value = argv[index];
  if (value == null || value.startsWith("--")) {
    throw new Error(`Pass a value after ${flag}.`);
  }
  return value;
}

function asInt(flag, raw) {
  const n = Number(raw);
  if (!Number.isInteger(n)) {
    throw new Error(`${flag} must be an integer.`);
  }
  return n;
}

export function parseArgs(argv) {
  const options = {
    platform: "android",
    port: DEFAULT_PORT,
    fps: DEFAULT_FPS,
    maxSize: DEFAULT_MAX_SIZE,
    bitRate: DEFAULT_BIT_RATE,
    transport: "auto",
    noAuth: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    switch (flag) {
      case "--help":
      case "-h":
        options.help = true;
        break;
      case "--serial":
      case "-s":
        options.serial = requireFlagValue(argv, ++i, flag);
        break;
      case "--target":
        options.target = requireFlagValue(argv, ++i, flag);
        break;
      case "--platform":
        options.platform = requireFlagValue(argv, ++i, flag);
        break;
      case "--adb":
        options.adb = requireFlagValue(argv, ++i, flag);
        break;
      case "--simctl":
        options.simctl = requireFlagValue(argv, ++i, flag);
        break;
      case "--idb":
        options.idb = requireFlagValue(argv, ++i, flag);
        break;
      case "--ffmpeg":
        options.ffmpeg = requireFlagValue(argv, ++i, flag);
        break;
      case "--port":
      case "-p":
        options.port = Number(requireFlagValue(argv, ++i, flag));
        break;
      case "--fps":
        options.fps = Number(requireFlagValue(argv, ++i, flag));
        options.fpsExplicit = true;
        break;
      case "--max-size":
        options.maxSize = asInt(flag, requireFlagValue(argv, ++i, flag));
        break;
      case "--bit-rate":
        options.bitRate = asInt(flag, requireFlagValue(argv, ++i, flag));
        break;
      case "--transport":
        options.transport = requireFlagValue(argv, ++i, flag);
        break;
      case "--token":
        options.token = requireFlagValue(argv, ++i, flag);
        break;
      case "--python":
        options.python = requireFlagValue(argv, ++i, flag);
        break;
      case "--bridge":
        options.bridge = requireFlagValue(argv, ++i, flag);
        break;
      case "--scrcpy-server":
        options.scrcpyServer = requireFlagValue(argv, ++i, flag);
        break;
      case "--scrcpy-version":
        options.scrcpyVersion = requireFlagValue(argv, ++i, flag);
        break;
      case "--no-auth":
        options.noAuth = true;
        break;
      default:
        throw new Error(`Unknown argument: ${flag}`);
    }
  }

  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) {
    throw new Error("--port must be an integer from 0 to 65535 (0 selects an available port).");
  }
  if (!["android", "ios"].includes(options.platform)) {
    throw new Error("--platform must be android or ios.");
  }
  if (!Number.isFinite(options.fps) || options.fps < 1 || options.fps > 60) {
    throw new Error("--fps must be between 1 and 60.");
  }
  if (!Number.isInteger(options.maxSize) || options.maxSize < 320 || options.maxSize > 4096) {
    throw new Error("--max-size must be an integer from 320 to 4096.");
  }
  if (
    !Number.isInteger(options.bitRate) ||
    options.bitRate < 100_000 ||
    options.bitRate > 100_000_000
  ) {
    throw new Error("--bit-rate must be an integer from 100000 to 100000000.");
  }
  if (!TRANSPORTS.has(options.transport)) {
    throw new Error("--transport must be auto, scrcpy, screenrecord, or screencap.");
  }
  if (options.scrcpyVersion !== undefined) {
    if (!VERSION_TEXT.test(options.scrcpyVersion)) {
      throw new Error("--scrcpy-version must look like 4.1.");
    }
    if (options.scrcpyServer === undefined) {
      throw new Error("--scrcpy-version names the version of --scrcpy-server; pass both.");
    }
  }
  return options;
}

export function isSafeKeyCode(value) {
  return ALLOWED_KEYCODES.has(value);
}

export function encodeAdbText(value) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("Text must be a non-empty string.");
  }
  if (value.length > 1000) {
    throw new Error("Text is too long (maximum 1000 characters). ");
  }
  if (!ASCII_TEXT.test(value)) {
    throw new Error(
      "Browser text entry intentionally supports conservative ASCII only. Use the device keyboard or a semantics-based test for other characters.",
    );
  }
  return value.replaceAll("%", "%25").replaceAll(" ", "%s");
}

export function parseWmSize(value) {
  const re = /(?:Physical|Override) size:\s*(\d+)x(\d+)/g;
  let last = null;
  let match;
  while ((match = re.exec(String(value))) !== null) {
    last = { width: Number(match[1]), height: Number(match[2]) };
  }
  return last;
}

export function extractJpegFrames(chunk, carry = Buffer.alloc(0)) {
  let buffer = carry.length ? Buffer.concat([carry, chunk], carry.length + chunk.length) : chunk;
  const frames = [];

  for (;;) {
    if (buffer.length < 4) break;
    const start = buffer.indexOf(SOI);
    if (start < 0) {
      // Keep last byte in case SOI is split across chunks.
      buffer = buffer.subarray(Math.max(0, buffer.length - 1));
      break;
    }
    const end = buffer.indexOf(EOI, start + 2);
    if (end < 0) {
      buffer = buffer.subarray(start);
      break;
    }
    frames.push(buffer.subarray(start, end + 2));
    buffer = buffer.subarray(end + 2);
  }

  return { frames, carry: buffer };
}

export function normalizeCoordinate(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    throw new Error(`${name} must be a number.`);
  }
  return Math.round(clamp(number, 0, 100_000));
}

/** True for the Host header of a request addressed to this Canvas (DNS-rebinding guard). */
export function isAllowedHost(host, port) {
  const value = String(host ?? "").toLowerCase();
  return value === `127.0.0.1:${port}` || value === `localhost:${port}`;
}

/** True for an Origin header of a page served by this Canvas. */
export function isCanvasOrigin(origin, port) {
  const value = String(origin ?? "").toLowerCase();
  return value === `http://127.0.0.1:${port}` || value === `http://localhost:${port}`;
}

export function parseCookies(header) {
  const cookies = Object.create(null);
  for (const part of String(header ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0) cookies[part.slice(0, index).trim()] = part.slice(index + 1).trim();
  }
  return cookies;
}

class ControlMessageError extends Error {
  constructor(message, type) {
    super(message);
    this.for = type;
  }
}

function fail(type, message) {
  throw new ControlMessageError(message, type);
}

function unit(message, name) {
  const value = message[name];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    fail(message.t, `${name} must be a number from 0 to 1.`);
  }
  return value;
}

function bounded(message, name, minimum, maximum) {
  const value = message[name];
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    fail(message.t, `${name} must be a number from ${minimum} to ${maximum}.`);
  }
  return value;
}

function integer(message, name, minimum, maximum, fallback) {
  const value = message[name] ?? fallback;
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    fail(message.t, `${name} must be an integer from ${minimum} to ${maximum}.`);
  }
  return value;
}

function choice(message, name, allowed) {
  const value = message[name];
  if (!allowed.includes(value)) fail(message.t, `${name} must be one of: ${allowed.join(", ")}.`);
  return value;
}

function utf8Text(message, name, maximumBytes) {
  const value = message[name];
  if (typeof value !== "string" || value.length === 0) fail(message.t, `${name} must be a non-empty string.`);
  if (Buffer.byteLength(value, "utf8") > maximumBytes) {
    fail(message.t, `${name} is longer than ${maximumBytes} UTF-8 bytes.`);
  }
  return value;
}

const TOUCH_ACTIONS = ["down", "move", "up", "cancel"];
const KEY_ACTIONS = ["down", "up"];
const INT32_MAX = 0x7fff_ffff;

/**
 * Parse and strictly validate one control WebSocket message. Returns a normalized
 * copy with only the known fields; throws an error whose `for` names the message type.
 */
export function parseControlMessage(raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    fail(null, "Control messages must be JSON.");
  }
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    fail(null, "A control message must be a JSON object.");
  }
  const type = typeof message.t === "string" ? message.t : null;
  switch (type) {
    case "touch": {
      const action = choice(message, "a", TOUCH_ACTIONS);
      const result = {
        t: type,
        a: action,
        id: integer(message, "id", -INT32_MAX, INT32_MAX),
        x: unit(message, "x"),
        y: unit(message, "y"),
      };
      if (message.p !== undefined) result.p = unit(message, "p");
      return result;
    }
    case "scroll":
      return {
        t: type,
        x: unit(message, "x"),
        y: unit(message, "y"),
        dx: bounded(message, "dx", -16, 16),
        dy: bounded(message, "dy", -16, 16),
      };
    case "key": {
      const code = integer(message, "code", 0, INT32_MAX);
      if (!STREAM_KEYCODES.has(code)) fail(type, `Android keycode ${code} is not allowed.`);
      const meta = integer(message, "meta", 0, INT32_MAX, 0);
      if (meta & ~META_MASK) fail(type, "meta has bits that are not Android modifier flags.");
      return {
        t: type,
        a: choice(message, "a", KEY_ACTIONS),
        code,
        meta,
        repeat: integer(message, "repeat", 0, 1_000_000, 0),
      };
    }
    case "text": {
      if (message.sensitive !== undefined && typeof message.sensitive !== "boolean") {
        fail(type, "sensitive must be true or false.");
      }
      return { t: type, text: utf8Text(message, "text", 300), sensitive: message.sensitive === true };
    }
    case "paste":
      return { t: type, text: utf8Text(message, "text", MAX_CONTROL_MESSAGE_BYTES) };
    case "clipboard-get":
      return { t: type };
    case "system":
      return { t: type, op: choice(message, "op", SYSTEM_OPS) };
    case "control":
      return { t: type, mode: choice(message, "mode", CONTROL_MODES) };
    case "ping":
      return { t: type, ts: bounded(message, "ts", -Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER) };
    default:
      return fail(type, "Unknown control message type.");
  }
}
