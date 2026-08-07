#!/usr/bin/env node
/**
 * Shared helpers for the Android emulator browser bridge.
 * Exports are part of the test and server contracts — keep names stable.
 */
import { randomBytes } from "node:crypto";

export const DEFAULT_PORT = 3277;
export const DEFAULT_FPS = 15;
export const DEFAULT_MAX_SIZE = 1280;
export const DEFAULT_BIT_RATE = 8_000_000;
export const MAX_BODY_BYTES = 64 * 1024;

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

const TRANSPORTS = new Set(["auto", "screenrecord", "screencap"]);
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
      case "--adb":
        options.adb = requireFlagValue(argv, ++i, flag);
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
    throw new Error("--transport must be auto, screenrecord, or screencap.");
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
