/**
 * One idb_companion per iOS Simulator, owned by the Canvas, and a small gRPC client
 * for it over node:http2 with a hand-written protobuf codec for exactly the messages
 * the Canvas and the iOS device daemon need (idb.proto of fb-idb 1.6.5, package `idb`,
 * service `CompanionService`):
 *
 * - `video_stream`: VideoStreamRequest Start/Stop in, VideoStreamResponse payload out.
 *   One gRPC message carries one H.264 access unit; a keyframe comes with its SPS and
 *   PPS. The companion keeps encoding when a client goes away without an explicit
 *   Stop, so every close path here sends Stop while the call can still carry it, and a
 *   stream that ended without one reports `abnormal` so the owner restarts the
 *   companion.
 * - `hid`: a client stream of HIDEvent presses (touch down/up at logical points,
 *   HOME/LOCK buttons, keyboard usage codes), opened on first send or ahead of it
 *   (`openHid()`), with a `drop` event when the companion ends it. The companion serves
 *   one `hid` call at a time, so the call ends after a short idle window and the next
 *   event opens a new one: an idle call would block every other client's input. An
 *   ended call waits for the companion to serve it (up to a safety limit) rather than
 *   being cancelled, and events it could not confirm are reported (`lost`, flush()).
 * - `describe`, `get_orientation` and `accessibility_info` (backend, format, filter,
 *   keys, profile, frame coverage): unary queries.
 * - `accessibility_quiescence`: a server stream of StateChanged, TouchesCompleted,
 *   TargetChanged and TargetExited events.
 *
 * The companion listens on a private Unix domain socket by default: its TCP listener
 * binds every interface, not loopback only. It runs in its own process group and is
 * stopped with SIGTERM, then SIGKILL after a timeout; a process `exit` hook kills any
 * companion still alive. Node built-ins only.
 */
import { spawn as nodeSpawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { createWriteStream, mkdtempSync, rmSync } from "node:fs";
import { access, constants as fsConstants, stat } from "node:fs/promises";
import * as http2 from "node:http2";
import { connect as netConnect, createServer as netCreateServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, join, resolve as resolvePath } from "node:path";
import { performance } from "node:perf_hooks";
import { env as processEnv } from "node:process";

export const GRPC_SERVICE = "/idb.CompanionService";
export const IDB_INSTALL_HINT =
  "idb_companion was not found. Install it with `brew install facebook/fb/idb-companion` or pass its path.";

// The Canvas's own idb_companion binary. AUTONOM_IDB_COMPANION is not read here: it is the
// `host:port` of a remote companion that every idb call of the CLI is sent to.
export const IDB_CAPABILITY = "canvas.idb";
export const IDB_COMPANION_BIN_ENV = "AUTONOM_IDB_COMPANION_BIN";
export const IDB_COMPANION_HINT =
  "Install it with `brew install facebook/fb/idb-companion`, set AUTONOM_IDB_COMPANION_BIN to its path, " +
  "or pass --idb-companion PATH.";
// The structured error codes of the CLI (scripts/autonom_lib/errors.py) a refusal uses.
export const ERROR_CODE = Object.freeze({
  TOOL_MISSING: "tool_missing",
  UNSUPPORTED_ON_PLATFORM: "unsupported_on_platform",
});

/** A refusal with a stable `code`, printed as the CLI's JSON error object. */
export class StructuredError extends Error {
  constructor(code, message, { hint = null, ...extra } = {}) {
    super(message);
    this.code = code;
    this.hint = hint;
    this.extra = extra;
  }

  /** The same object `autonom` prints: ok, error_code, error, hint, then the extra fields. */
  toJSON() {
    return { ok: false, error_code: this.code, error: this.message, ...(this.hint ? { hint: this.hint } : {}), ...this.extra };
  }
}

/** `--transport idb` without a usable idb_companion: the same refusal `autonom canvas serve` makes. */
export function idbUnavailableError(reason) {
  return new StructuredError(ERROR_CODE.TOOL_MISSING, `--transport idb is unavailable: ${reason}`, {
    hint: IDB_COMPANION_HINT, tool: "idb_companion", capability: IDB_CAPABILITY,
  });
}

async function executableFile(path) {
  try {
    // A directory passes the execute check too: it must be a file.
    if (!(await stat(path)).isFile()) return false;
    await access(path, fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * The idb_companion binary of the Canvas: `flag` (--idb-companion), else
 * AUTONOM_IDB_COMPANION_BIN when set, else the first executable `idb_companion` file in
 * PATH. A flag or variable that names no executable file is reported, never skipped.
 * AUTONOM_IDB_COMPANION is never read. `_canvas_idb_lookup` in scripts/autonom.py
 * follows exactly these rules and reasons. Returns `{ available, path, source, reason }`.
 */
export async function resolveIdbCompanionBinary({ flag, env = processEnv } = {}) {
  const configured = flag !== undefined && flag !== null
    ? ["--idb-companion", String(flag)]
    : (env[IDB_COMPANION_BIN_ENV] ? [IDB_COMPANION_BIN_ENV, env[IDB_COMPANION_BIN_ENV]] : null);
  if (configured) {
    const [source, path] = configured;
    if (await executableFile(path)) return { available: true, path: resolvePath(path), source, reason: null };
    return { available: false, path, source, reason: `${source} is not an executable file: ${path}` };
  }
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    const candidate = join(directory, "idb_companion");
    if (await executableFile(candidate)) return { available: true, path: candidate, source: "PATH", reason: null };
  }
  return {
    available: false, path: null, source: null,
    reason: `idb_companion was not found (--idb-companion, ${IDB_COMPANION_BIN_ENV} or PATH)`,
  };
}

export const VIDEO_FORMAT = Object.freeze({ H264: 0, RBGA: 1, MJPEG: 2, MINICAP: 3, I420: 4 });
export const HID_DIRECTION = Object.freeze({ DOWN: 0, UP: 1 });
export const HID_BUTTON = Object.freeze({
  APPLE_PAY: 0,
  HOME: 1,
  LOCK: 2,
  SIDE_BUTTON: 3,
  SIRI: 4,
  PLAY_PAUSE: 5,
  VOLUME_UP: 6,
  VOLUME_DOWN: 7,
  EJECT: 8,
});
export const ORIENTATION_NAMES = Object.freeze([
  "UNKNOWN",
  "PORTRAIT",
  "PORTRAIT_UPSIDE_DOWN",
  "LANDSCAPE_LEFT",
  "LANDSCAPE_RIGHT",
  "FACE_UP",
  "FACE_DOWN",
]);

/** Start settings measured on Xcode 27 / idb-companion 1.6.4: 60 fps at full size. */
export const DEFAULT_VIDEO_OPTIONS = Object.freeze({
  fps: 60,
  format: "h264",
  compressionQuality: 0.5,
  scaleFactor: 1,
  keyFrameRate: 2,
});

export const READY_TIMEOUT_MS = 15_000;
export const STOP_TIMEOUT_MS = 3000;
export const KILL_WAIT_MS = 2000;
export const STREAM_STOP_TIMEOUT_MS = 2000;
export const UNARY_TIMEOUT_MS = 5000;
/**
 * idb_companion serves one `hid` call at a time, so an idle call blocks every other
 * client's input: it ends this long after its last event (see IdbHidStream).
 */
export const HID_IDLE_CLOSE_MS = 250;
/** The same window while a touch, button or key is held down (a long press). */
export const HID_HELD_IDLE_CLOSE_MS = 10_000;
/**
 * How long an ended (half-closed) HID call that carried events waits for the companion's
 * status. While another client holds the companion's one hid call (a long press, a long
 * `idb ui text`), the call is not served yet and its events are not read: cancelling it
 * would throw them away. So it waits this long, a safety limit, not a short timer; when
 * the limit is hit (or the session is lost) its events are reported as unconfirmed.
 */
export const HID_CLOSE_WAIT_MS = 60_000;
export const MAX_GRPC_MESSAGE_BYTES = 32 * 1024 * 1024;
/** Frames buffered before the HTTP/2 stream is paused (flow control then backs up). */
export const FRAME_QUEUE_HIGH_WATER = 8;
const LOG_TAIL_LINES = 200;
// sun_path is 104 bytes on macOS, including the terminating NUL.
const MAX_SOCKET_PATH_BYTES = 103;
const SOCKET_NAME = "companion.sock";

// ---------------------------------------------------------------------------
// Protobuf (proto3) wire format: just varint, 64-bit and length-delimited fields.
// ---------------------------------------------------------------------------

const WIRE = Object.freeze({ VARINT: 0, I64: 1, LEN: 2, I32: 5 });
const UINT64_MAX = (1n << 64n) - 1n;

function varint(value) {
  let n = typeof value === "bigint" ? value : BigInt(value);
  if (n < 0n || n > UINT64_MAX) throw new RangeError(`Varint ${value} is outside uint64.`);
  const out = [];
  while (n >= 0x80n) {
    out.push(Number((n & 0x7fn) | 0x80n));
    n >>= 7n;
  }
  out.push(Number(n));
  return Buffer.from(out);
}

function requireUint(value, name) {
  if (typeof value === "bigint") {
    if (value < 0n || value > UINT64_MAX) throw new RangeError(`${name} must be a uint64.`);
    return value;
  }
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new RangeError(`${name} must be a non-negative safe integer.`);
  }
  return value;
}

function requireNumber(value, name, { min = -Infinity, max = Infinity, exclusiveMin = false } = {}) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(`${name} must be a finite number.`);
  }
  if (exclusiveMin ? value <= min : value < min) throw new RangeError(`${name} is too small.`);
  if (value > max) throw new RangeError(`${name} is too large.`);
  return value;
}

/** Proto3 encoder that leaves out default (zero) scalars, like the reference library. */
class ProtoWriter {
  #parts = [];

  #tag(field, wire) {
    this.#parts.push(varint((field << 3) | wire));
  }

  uint(field, value) {
    if (value === undefined || value === 0 || value === 0n) return this;
    this.#tag(field, WIRE.VARINT);
    this.#parts.push(varint(value));
    return this;
  }

  bool(field, value) {
    return this.uint(field, value ? 1 : 0);
  }

  double(field, value) {
    if (value === undefined || Object.is(value, 0)) return this;
    this.#tag(field, WIRE.I64);
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(value);
    this.#parts.push(buf);
    return this;
  }

  bytes(field, value, { always = false } = {}) {
    if (value === undefined || (!always && value.length === 0)) return this;
    const buf = typeof value === "string" ? Buffer.from(value, "utf8") : Buffer.from(value);
    this.#tag(field, WIRE.LEN);
    this.#parts.push(varint(buf.length), buf);
    return this;
  }

  string(field, value) {
    return this.bytes(field, value);
  }

  /** Embedded messages are always written, even empty, so a oneof choice survives. */
  message(field, buf) {
    return this.bytes(field, buf, { always: true });
  }

  finish() {
    return Buffer.concat(this.#parts);
  }
}

function readVarint(buf, offset) {
  let result = 0n;
  let shift = 0n;
  for (let i = offset; i < buf.length; i += 1) {
    const byte = buf[i];
    result |= BigInt(byte & 0x7f) << shift;
    if ((byte & 0x80) === 0) {
      if (result > UINT64_MAX) throw new RangeError("Varint is outside uint64.");
      return [result, i + 1];
    }
    shift += 7n;
    if (shift > 63n) throw new RangeError("Varint is longer than 10 bytes.");
  }
  throw new RangeError("Truncated varint.");
}

/**
 * Splits one protobuf message into `{ field, wire, value }` records in wire order.
 * VARINT values are bigint, I64/I32 values and LEN values are Buffers (views).
 */
export function decodeProtoFields(input) {
  const buf = Buffer.isBuffer(input) ? input : Buffer.from(input);
  const fields = [];
  let offset = 0;
  while (offset < buf.length) {
    const [key, afterKey] = readVarint(buf, offset);
    const field = Number(key >> 3n);
    const wire = Number(key & 7n);
    if (field === 0) throw new RangeError("Protobuf field number 0 is invalid.");
    offset = afterKey;
    let value;
    switch (wire) {
      case WIRE.VARINT:
        [value, offset] = readVarint(buf, offset);
        break;
      case WIRE.I64:
        if (offset + 8 > buf.length) throw new RangeError("Truncated 64-bit field.");
        value = buf.subarray(offset, offset + 8);
        offset += 8;
        break;
      case WIRE.I32:
        if (offset + 4 > buf.length) throw new RangeError("Truncated 32-bit field.");
        value = buf.subarray(offset, offset + 4);
        offset += 4;
        break;
      case WIRE.LEN: {
        const [length, afterLength] = readVarint(buf, offset);
        const end = afterLength + Number(length);
        if (length > BigInt(buf.length) || end > buf.length) {
          throw new RangeError("Truncated length-delimited field.");
        }
        value = buf.subarray(afterLength, end);
        offset = end;
        break;
      }
      default:
        throw new RangeError(`Unsupported protobuf wire type ${wire}.`);
    }
    fields.push({ field, wire, value });
  }
  return fields;
}

/** Last value of each singular field (proto3 last-wins), keyed by field number. */
function lastFields(buf) {
  const map = new Map();
  for (const record of decodeProtoFields(buf)) map.set(record.field, record);
  return map;
}

function fieldUint(map, field) {
  const record = map.get(field);
  if (!record || record.wire !== WIRE.VARINT) return 0;
  const value = record.value;
  return value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : value;
}

function fieldDouble(map, field) {
  const record = map.get(field);
  if (!record || record.wire !== WIRE.I64) return 0;
  return record.value.readDoubleLE(0);
}

function fieldBytes(map, field) {
  const record = map.get(field);
  if (!record || record.wire !== WIRE.LEN) return null;
  return record.value;
}

function fieldString(map, field) {
  const value = fieldBytes(map, field);
  return value ? value.toString("utf8") : "";
}

function formatCode(format) {
  if (typeof format === "number") {
    if (!Object.values(VIDEO_FORMAT).includes(format)) throw new RangeError(`Unknown video format ${format}.`);
    return format;
  }
  const code = VIDEO_FORMAT[String(format).toUpperCase()];
  if (code === undefined) throw new RangeError(`Unknown video format ${format}.`);
  return code;
}

/**
 * Validated Start settings with defaults applied. Normalizing twice gives the same result
 * (a stream keeps normalized options and encodeVideoStreamStart normalizes them again), so
 * fps 0, frames on change only, stays 0 and is not replaced by the default; a missing or
 * null fps is the default.
 */
export function normalizeVideoOptions(options = {}) {
  const merged = { ...DEFAULT_VIDEO_OPTIONS, ...options };
  const out = { format: formatCode(merged.format) };
  if (merged.fps === undefined || merged.fps === null) {
    out.fps = DEFAULT_VIDEO_OPTIONS.fps;
  } else if (merged.fps === 0) {
    out.fps = 0;
  } else {
    if (!Number.isInteger(merged.fps) || merged.fps < 1 || merged.fps > 240) {
      throw new RangeError("fps must be an integer from 1 to 240, or 0 for frames on change only.");
    }
    out.fps = merged.fps;
  }
  if (merged.compressionQuality !== undefined) {
    out.compressionQuality = requireNumber(merged.compressionQuality, "compressionQuality", { min: 0, max: 1 });
  }
  out.scaleFactor = requireNumber(merged.scaleFactor ?? 1, "scaleFactor", { min: 0, max: 1, exclusiveMin: true });
  if (merged.avgBitrate !== undefined) {
    out.avgBitrate = requireNumber(merged.avgBitrate, "avgBitrate", { min: 0, max: 1e9, exclusiveMin: true });
  }
  if (merged.keyFrameRate !== undefined) {
    out.keyFrameRate = requireNumber(merged.keyFrameRate, "keyFrameRate", { min: 0, max: 3600 });
  }
  return out;
}

/**
 * VideoStreamRequest { start: Start { fps, format, compression_quality, scale_factor,
 * avg_bitrate, key_frame_rate } }
 */
export function encodeVideoStreamStart(options = {}) {
  const o = normalizeVideoOptions(options);
  const start = new ProtoWriter()
    .uint(2, o.fps)
    .uint(3, o.format)
    .double(4, o.compressionQuality)
    .double(5, o.scaleFactor)
    .double(6, o.avgBitrate)
    .double(7, o.keyFrameRate)
    .finish();
  return new ProtoWriter().message(1, start).finish();
}

/** VideoStreamRequest { stop: Stop {} } */
export function encodeVideoStreamStop() {
  return new ProtoWriter().message(2, Buffer.alloc(0)).finish();
}

/** Inverse of the two encoders above (used by test servers and diagnostics). */
export function decodeVideoStreamRequest(buf) {
  // `control` is a oneof: the last of start/stop on the wire wins.
  const choice = decodeProtoFields(buf)
    .filter((r) => r.wire === WIRE.LEN && (r.field === 1 || r.field === 2))
    .at(-1);
  if (choice?.field === 1) {
    const s = lastFields(choice.value);
    return {
      type: "start",
      filePath: fieldString(s, 1),
      fps: fieldUint(s, 2),
      format: fieldUint(s, 3),
      compressionQuality: fieldDouble(s, 4),
      scaleFactor: fieldDouble(s, 5),
      avgBitrate: fieldDouble(s, 6),
      keyFrameRate: fieldDouble(s, 7),
    };
  }
  if (choice?.field === 2) return { type: "stop" };
  return { type: "unknown" };
}

/** VideoStreamResponse: `{ data }` for a payload with bytes, `{ log }` for log output. */
export function decodeVideoStreamResponse(buf) {
  const records = decodeProtoFields(buf).filter((r) => r.wire === WIRE.LEN && (r.field === 1 || r.field === 2));
  const last = records.at(-1);
  if (!last) return { data: null, log: null };
  if (last.field === 1) return { data: null, log: Buffer.from(last.value) };
  const payload = lastFields(last.value);
  const data = fieldBytes(payload, 2);
  return { data: data ? Buffer.from(data) : null, log: null };
}

export function encodeVideoStreamResponse({ data, log } = {}) {
  if (log !== undefined) return new ProtoWriter().bytes(1, log, { always: true }).finish();
  const payload = new ProtoWriter().bytes(2, data ?? Buffer.alloc(0), { always: true }).finish();
  return new ProtoWriter().message(2, payload).finish();
}

function directionCode(direction) {
  if (direction === HID_DIRECTION.DOWN || direction === "down" || direction === "DOWN") return HID_DIRECTION.DOWN;
  if (direction === HID_DIRECTION.UP || direction === "up" || direction === "UP") return HID_DIRECTION.UP;
  throw new RangeError(`Unknown HID direction ${direction}.`);
}

function buttonCode(button) {
  if (typeof button === "number" && Object.values(HID_BUTTON).includes(button)) return button;
  const code = HID_BUTTON[String(button).toUpperCase()];
  if (code === undefined) throw new RangeError(`Unknown HID button ${button}.`);
  return code;
}

/**
 * HIDEvent { pinch: HIDPinch { center: Point, scale, duration, radius } } (HIDEvent field 4).
 * The companion puts two fingers on a horizontal line `radius` points either side of
 * `center`, moves them to `radius * scale` over `duration` seconds and lifts them.
 */
function encodeHidPinch(pinch) {
  const x = requireNumber(pinch.center?.x, "pinch.center.x", { min: 0 });
  const y = requireNumber(pinch.center?.y, "pinch.center.y", { min: 0 });
  const scale = requireNumber(pinch.scale, "pinch.scale", { min: 0, exclusiveMin: true });
  const duration = requireNumber(pinch.duration, "pinch.duration", { min: 0 });
  const radius = requireNumber(pinch.radius, "pinch.radius", { min: 0, exclusiveMin: true });
  const center = new ProtoWriter().double(1, x).double(2, y).finish();
  const body = new ProtoWriter().message(1, center).double(2, scale).double(3, duration).double(4, radius).finish();
  return new ProtoWriter().message(4, body).finish();
}

/**
 * HIDEvent { press: HIDPress { action: HIDPressAction { touch | button | key }, direction } }.
 * `event` is `{ touch: { x, y } }`, `{ button: "HOME" }` or `{ key: keycode }`, plus
 * `direction` ("down" or "up"). Touch points are logical points in the current
 * orientation. `{ pinch: { center: { x, y }, scale, duration, radius } }` is one canned
 * two-finger pinch instead (no direction).
 */
export function encodeHidEvent(event) {
  if (!event || typeof event !== "object") throw new TypeError("HID event must be an object.");
  if (event.pinch) return encodeHidPinch(event.pinch);
  const direction = directionCode(event.direction);
  let action;
  if (event.touch) {
    const x = requireNumber(event.touch.x, "touch.x", { min: 0 });
    const y = requireNumber(event.touch.y, "touch.y", { min: 0 });
    const point = new ProtoWriter().double(1, x).double(2, y).finish();
    const touch = new ProtoWriter().message(1, point).finish();
    action = new ProtoWriter().message(1, touch).finish();
  } else if (event.button !== undefined) {
    const button = new ProtoWriter().uint(1, buttonCode(event.button)).finish();
    action = new ProtoWriter().message(2, button).finish();
  } else if (event.key !== undefined) {
    const key = new ProtoWriter().uint(1, requireUint(event.key, "key")).finish();
    action = new ProtoWriter().message(3, key).finish();
  } else {
    throw new TypeError("HID event needs touch, button or key.");
  }
  const press = new ProtoWriter().message(1, action).uint(2, direction).finish();
  return new ProtoWriter().message(1, press).finish();
}

/** Inverse of encodeHidEvent for press and pinch events; null for any other HID event. */
export function decodeHidEvent(buf) {
  const event = lastFields(buf);
  const pinch = fieldBytes(event, 4);
  if (pinch && !event.has(1)) {
    const p = lastFields(pinch);
    const center = lastFields(fieldBytes(p, 1) ?? Buffer.alloc(0));
    return {
      pinch: {
        center: { x: fieldDouble(center, 1), y: fieldDouble(center, 2) },
        scale: fieldDouble(p, 2), duration: fieldDouble(p, 3), radius: fieldDouble(p, 4),
      },
    };
  }
  const press = fieldBytes(event, 1);
  if (!press) return null;
  const p = lastFields(press);
  const direction = fieldUint(p, 2) === HID_DIRECTION.UP ? "up" : "down";
  const action = fieldBytes(p, 1);
  if (!action) return { direction };
  const a = lastFields(action);
  if (a.has(1)) {
    const point = lastFields(fieldBytes(lastFields(fieldBytes(a, 1)), 1) ?? Buffer.alloc(0));
    return { touch: { x: fieldDouble(point, 1), y: fieldDouble(point, 2) }, direction };
  }
  if (a.has(2)) {
    const code = fieldUint(lastFields(fieldBytes(a, 2)), 1);
    const name = Object.keys(HID_BUTTON).find((k) => HID_BUTTON[k] === code) ?? code;
    return { button: name, direction };
  }
  if (a.has(3)) return { key: fieldUint(lastFields(fieldBytes(a, 3)), 1), direction };
  return { direction };
}

function decodeScreen(buf) {
  if (!buf) return null;
  const s = lastFields(buf);
  return {
    width: fieldUint(s, 1),
    height: fieldUint(s, 2),
    density: fieldDouble(s, 3),
    widthPoints: fieldUint(s, 4),
    heightPoints: fieldUint(s, 5),
  };
}

/** TargetDescriptionResponse.target_description, with screen dimensions and displays. */
export function decodeTargetDescriptionResponse(buf) {
  const description = fieldBytes(lastFields(buf), 1) ?? Buffer.alloc(0);
  const records = decodeProtoFields(description);
  const t = lastFields(description);
  const displays = records
    .filter((r) => r.field === 11 && r.wire === WIRE.LEN)
    .map((r) => {
      const d = lastFields(r.value);
      return {
        uniqueId: fieldString(d, 1),
        name: fieldString(d, 2),
        active: fieldUint(d, 3) !== 0,
        integrated: fieldUint(d, 4) !== 0,
        width: fieldUint(d, 5),
        height: fieldUint(d, 6),
        density: fieldDouble(d, 7),
      };
    });
  return {
    udid: fieldString(t, 1),
    name: fieldString(t, 2),
    screen: decodeScreen(fieldBytes(t, 3)),
    state: fieldString(t, 4),
    targetType: fieldString(t, 5),
    osVersion: fieldString(t, 6),
    architecture: fieldString(t, 7),
    displays,
  };
}

/** GetOrientationResponse.orientation as its enum name. */
export function decodeOrientationResponse(buf) {
  const code = fieldUint(lastFields(buf), 1);
  return ORIENTATION_NAMES[code] ?? "UNKNOWN";
}

// ---------------------------------------------------------------------------
// Accessibility reads and the quiet stream (idb.proto of fb-idb 1.6.5).
// ---------------------------------------------------------------------------

/** AccessibilityInfoRequest.Format. */
export const AX_FORMAT = Object.freeze({ LEGACY: 0, NESTED: 1, COMPLETE: 2 });
/** AccessibilityInfoRequest.Backend; UNSPECIFIED is the companion's historical default. */
export const AX_BACKEND = Object.freeze({ UNSPECIFIED: 0, AX: 1, AXBRIDGE: 2, AXBRIDGE_PERSISTENT: 3 });
/** AccessibilityInfoRequest.Filter. */
export const AX_FILTER = Object.freeze({ ALL: 0, INTERACTABLE: 1 });
/** AccessibilityQuiescenceResponse.State, by number. */
export const QUIESCENCE_STATE_NAMES = Object.freeze(["STATE_UNSPECIFIED", "BUSY", "SETTLING", "QUIET"]);
/** AccessibilityQuiescenceResponse.Signal, by number. */
export const QUIESCENCE_SIGNAL_NAMES = Object.freeze(["SIGNAL_UNSPECIFIED", "RUN_LOOP_IDLE", "ANIMATIONS_INACTIVE"]);
/** The four AccessibilityQuiescenceResponse events, by their oneof field number. */
const QUIESCENCE_EVENT_FIELDS = Object.freeze({
  2: "state_changed",
  3: "touches_completed",
  4: "target_changed",
  5: "target_exited",
});
/** Default deadline of one accessibility_info read: a full axbridge tree can take seconds. */
export const ACCESSIBILITY_INFO_TIMEOUT_MS = 10_000;
const UINT32_MAX = 2 ** 32 - 1;

/** An enum value by name (case-insensitive, with or without the proto prefix) or number. */
function enumCode(table, value, name, prefix) {
  if (typeof value === "number") {
    if (!Object.values(table).includes(value)) throw new RangeError(`Unknown ${name} ${value}.`);
    return value;
  }
  let key = String(value).toUpperCase();
  if (prefix && key.startsWith(prefix)) key = key.slice(prefix.length);
  const code = table[key];
  if (code === undefined) throw new RangeError(`Unknown ${name} ${value}.`);
  return code;
}

function enumName(table, code) {
  return Object.keys(table).find((key) => table[key] === code) ?? code;
}

/**
 * AccessibilityInfoRequest { format = 3, keys = 7, backend = 8, profile = 9,
 * collect_frame_coverage = 10, filter = 13 } for a whole-screen read. Every option is
 * optional; an unset one keeps the companion's historical behaviour (LEGACY format,
 * unspecified backend, every element, the default key set). Fields are written in
 * number order, so the bytes equal the reference library's.
 */
export function encodeAccessibilityInfoRequest(options = {}) {
  if (!options || typeof options !== "object") throw new TypeError("accessibility_info options must be an object.");
  const { backend, format, filter, keys, profile = false, collectFrameCoverage = false } = options;
  const formatCode = format === undefined || format === null ? 0 : enumCode(AX_FORMAT, format, "accessibility format");
  const backendCode = backend === undefined || backend === null
    ? 0 : enumCode(AX_BACKEND, backend, "accessibility backend", "BACKEND_");
  const filterCode = filter === undefined || filter === null ? 0 : enumCode(AX_FILTER, filter, "accessibility filter", "FILTER_");
  if (keys !== undefined && keys !== null && (!Array.isArray(keys) || keys.some((key) => typeof key !== "string" || !key))) {
    throw new TypeError("keys must be an array of non-empty strings.");
  }
  if (typeof profile !== "boolean") throw new TypeError("profile must be a boolean.");
  if (typeof collectFrameCoverage !== "boolean") throw new TypeError("collectFrameCoverage must be a boolean.");
  const writer = new ProtoWriter().uint(3, formatCode);
  for (const key of keys ?? []) writer.string(7, key);
  return writer
    .uint(8, backendCode)
    .bool(9, profile)
    .bool(10, collectFrameCoverage)
    .uint(13, filterCode)
    .finish();
}

/** Inverse of encodeAccessibilityInfoRequest (used by test servers), with enum names. */
export function decodeAccessibilityInfoRequest(buf) {
  const records = decodeProtoFields(buf);
  const f = lastFields(buf);
  return {
    format: enumName(AX_FORMAT, fieldUint(f, 3)),
    keys: records.filter((r) => r.field === 7 && r.wire === WIRE.LEN).map((r) => r.value.toString("utf8")),
    backend: enumName(AX_BACKEND, fieldUint(f, 8)),
    profile: fieldUint(f, 9) !== 0,
    collectFrameCoverage: fieldUint(f, 10) !== 0,
    filter: enumName(AX_FILTER, fieldUint(f, 13)),
  };
}

/** AccessibilityInfoResponse { json = 1 } (used by test servers). */
export function encodeAccessibilityInfoResponse(json) {
  return new ProtoWriter().string(1, String(json)).finish();
}

/** AccessibilityInfoResponse.json: the companion's JSON document as a string. */
export function decodeAccessibilityInfoResponse(buf) {
  return fieldString(lastFields(buf), 1);
}

function optionalUint32(value, name) {
  if (value === undefined || value === null) return undefined;
  if (!Number.isInteger(value) || value < 0 || value > UINT32_MAX) throw new RangeError(`${name} must be a uint32.`);
  return value;
}

/**
 * AccessibilityQuiescenceRequest { oneof target { pid = 1, bundle_id = 2 },
 * oneof busy_threshold { busy_threshold_ms = 3 }, oneof quiet_window { quiet_window_ms = 4 } }.
 * No target follows the frontmost application. Each tunable sits in its own oneof, so
 * a zero that is passed is written (an unset one takes the companion's default).
 */
export function encodeAccessibilityQuiescenceRequest(request = {}) {
  if (!request || typeof request !== "object") throw new TypeError("accessibility_quiescence request must be an object.");
  const { pid, bundleId, busyThresholdMs, quietWindowMs } = request;
  const hasPid = pid !== undefined && pid !== null;
  const hasBundle = bundleId !== undefined && bundleId !== null;
  if (hasPid && hasBundle) throw new TypeError("Pass pid or bundleId as the target, not both.");
  if (hasBundle && (typeof bundleId !== "string" || !bundleId)) throw new TypeError("bundleId must be a non-empty string.");
  const busy = optionalUint32(busyThresholdMs, "busyThresholdMs");
  const quiet = optionalUint32(quietWindowMs, "quietWindowMs");
  const parts = [];
  const scalar = (field, value) => parts.push(varint((field << 3) | WIRE.VARINT), varint(value));
  if (hasPid) scalar(1, requireUint(pid, "pid"));
  if (hasBundle) parts.push(new ProtoWriter().bytes(2, bundleId, { always: true }).finish());
  if (busy !== undefined) scalar(3, busy);
  if (quiet !== undefined) scalar(4, quiet);
  return Buffer.concat(parts);
}

/** Inverse of encodeAccessibilityQuiescenceRequest (used by test servers); unset fields are null. */
export function decodeAccessibilityQuiescenceRequest(buf) {
  const f = lastFields(buf);
  const present = (field) => (f.get(field)?.wire === WIRE.VARINT ? fieldUint(f, field) : null);
  // `target` is a oneof: the last of pid/bundle_id on the wire wins.
  const target = decodeProtoFields(buf).filter((r) => r.field === 1 || r.field === 2).at(-1);
  return {
    pid: target?.field === 1 && target.wire === WIRE.VARINT ? fieldUint(f, 1) : null,
    bundleId: target?.field === 2 && target.wire === WIRE.LEN ? target.value.toString("utf8") : null,
    busyThresholdMs: present(3),
    quietWindowMs: present(4),
  };
}

/** Repeated enum values, packed (proto3 default) or one per record. */
function repeatedUints(records, field) {
  const out = [];
  for (const record of records) {
    if (record.field !== field) continue;
    if (record.wire === WIRE.VARINT) {
      out.push(Number(record.value));
    } else if (record.wire === WIRE.LEN) {
      let offset = 0;
      while (offset < record.value.length) {
        const [value, next] = readVarint(record.value, offset);
        out.push(Number(value));
        offset = next;
      }
    }
  }
  return out;
}

/**
 * One AccessibilityQuiescenceResponse as
 * `{ type: "state_changed", pid, state, busySignals }`, `{ type: "touches_completed", pid }`,
 * `{ type: "target_changed", pid }` or `{ type: "target_exited", pid }`. A state or signal
 * newer than this client is reported by its number; an event newer than it is
 * `{ type: "unknown", pid, field }`, so a long-lived stream outlives a companion update.
 */
export function decodeAccessibilityQuiescenceResponse(buf) {
  const records = decodeProtoFields(buf);
  const f = lastFields(buf);
  const pid = fieldUint(f, 1);
  // `event` is a oneof: the last known choice on the wire wins.
  const event = records.filter((r) => QUIESCENCE_EVENT_FIELDS[r.field] && r.wire === WIRE.LEN).at(-1);
  if (!event) {
    const other = records.find((r) => r.field !== 1);
    return { type: "unknown", pid, field: other?.field ?? null };
  }
  const type = QUIESCENCE_EVENT_FIELDS[event.field];
  if (type !== "state_changed") return { type, pid };
  const stateRecords = decodeProtoFields(event.value);
  const code = fieldUint(lastFields(event.value), 1);
  return {
    type,
    pid,
    state: QUIESCENCE_STATE_NAMES[code] ?? code,
    busySignals: repeatedUints(stateRecords, 2).map((signal) => QUIESCENCE_SIGNAL_NAMES[signal] ?? signal),
  };
}

/** Inverse of decodeAccessibilityQuiescenceResponse for the four known events (test servers). */
export function encodeAccessibilityQuiescenceResponse(event) {
  if (!event || typeof event !== "object") throw new TypeError("Quiescence event must be an object.");
  const field = Number(Object.keys(QUIESCENCE_EVENT_FIELDS).find((key) => QUIESCENCE_EVENT_FIELDS[key] === event.type));
  if (!field) throw new RangeError(`Unknown quiescence event ${event.type}.`);
  let body = Buffer.alloc(0);
  if (event.type === "state_changed") {
    const state = typeof event.state === "number" ? event.state : QUIESCENCE_STATE_NAMES.indexOf(String(event.state));
    if (state < 0) throw new RangeError(`Unknown quiescence state ${event.state}.`);
    const signals = (event.busySignals ?? []).map((signal) => {
      const code = typeof signal === "number" ? signal : QUIESCENCE_SIGNAL_NAMES.indexOf(String(signal));
      if (code < 0) throw new RangeError(`Unknown quiescence signal ${signal}.`);
      return varint(code);
    });
    const writer = new ProtoWriter().uint(1, state);
    if (signals.length) writer.bytes(2, Buffer.concat(signals));
    body = writer.finish();
  }
  return new ProtoWriter().uint(1, requireUint(event.pid ?? 0, "pid")).message(field, body).finish();
}

// ---------------------------------------------------------------------------
// gRPC length-prefixed message framing.
// ---------------------------------------------------------------------------

export function grpcFrame(message) {
  const body = Buffer.from(message);
  const header = Buffer.alloc(5);
  header.writeUInt8(0, 0);
  header.writeUInt32BE(body.length, 1);
  return Buffer.concat([header, body]);
}

/** Reassembles gRPC messages from arbitrary DATA chunk boundaries. */
export class GrpcMessageParser {
  #chunks = [];
  #length = 0;
  #need = null;
  #maxBytes;

  constructor({ maxMessageBytes = MAX_GRPC_MESSAGE_BYTES } = {}) {
    this.#maxBytes = maxMessageBytes;
  }

  get pendingBytes() {
    return this.#length;
  }

  push(chunk) {
    if (chunk.length > 0) {
      this.#chunks.push(chunk);
      this.#length += chunk.length;
    }
    const messages = [];
    for (;;) {
      if (this.#need === null) {
        if (this.#length < 5) break;
        const head = this.#head();
        if (head[0] !== 0) throw new GrpcError(13, "Compressed gRPC messages are not supported.");
        const size = head.readUInt32BE(1);
        if (size > this.#maxBytes) throw new GrpcError(8, `gRPC message of ${size} bytes is too large.`);
        this.#need = 5 + size;
      }
      if (this.#length < this.#need) break;
      messages.push(this.#take(this.#need).subarray(5));
      this.#need = null;
    }
    return messages;
  }

  /** The 5-byte message prefix, copied across chunk boundaries only when it must be. */
  #head() {
    if (this.#chunks[0].length >= 5) return this.#chunks[0];
    const head = Buffer.alloc(5);
    let filled = 0;
    for (const chunk of this.#chunks) {
      filled += chunk.copy(head, filled, 0, Math.min(chunk.length, 5 - filled));
      if (filled === 5) break;
    }
    return head;
  }

  /** The next n bytes; joins only the chunks those bytes span. */
  #take(n) {
    this.#length -= n;
    const first = this.#chunks[0];
    if (first.length >= n) {
      if (first.length === n) this.#chunks.shift();
      else this.#chunks[0] = first.subarray(n);
      return first.subarray(0, n);
    }
    let covered = 0;
    let count = 0;
    while (covered < n) covered += this.#chunks[count++].length;
    const joined = Buffer.concat(this.#chunks.splice(0, count), covered);
    if (covered > n) this.#chunks.unshift(joined.subarray(n));
    return joined.subarray(0, n);
  }
}

export class GrpcError extends Error {
  constructor(code, details, { method } = {}) {
    super(`gRPC ${method ? `${method} ` : ""}failed with status ${code}${details ? `: ${details}` : ""}`);
    this.name = "GrpcError";
    this.code = code;
    this.details = details ?? "";
    this.method = method;
  }
}

function grpcStatusFrom(headers) {
  if (!headers || headers["grpc-status"] === undefined) return null;
  const code = Number(headers["grpc-status"]);
  let details = headers["grpc-message"] ?? "";
  try {
    details = decodeURIComponent(details);
  } catch {
    // Keep the raw text when it is not valid percent-encoding.
  }
  return { code: Number.isInteger(code) ? code : 2, details };
}

// ---------------------------------------------------------------------------
// H.264 Annex B helpers.
// ---------------------------------------------------------------------------

/** NAL unit payloads (without start codes) of an Annex B buffer. */
export function splitNalUnits(input) {
  const stream = Buffer.isBuffer(input) ? input : Buffer.from(input);
  const starts = [];
  for (let i = 0; i + 2 < stream.length; i += 1) {
    if (stream[i] === 0 && stream[i + 1] === 0 && stream[i + 2] === 1) {
      starts.push(i + 3);
      i += 2;
    }
  }
  return starts.map((start, index) => {
    let end = index + 1 < starts.length ? starts[index + 1] - 3 : stream.length;
    while (end > start && stream[end - 1] === 0) end -= 1;
    return stream.subarray(start, end);
  });
}

function rbsp(nal) {
  const out = [];
  let zeros = 0;
  for (let i = 0; i < nal.length; i += 1) {
    const byte = nal[i];
    if (zeros >= 2 && byte === 0x03) {
      zeros = 0;
      continue;
    }
    zeros = byte === 0 ? zeros + 1 : 0;
    out.push(byte);
  }
  return Buffer.from(out);
}

class BitReader {
  #buf;
  #bit = 0;

  constructor(buf) {
    this.#buf = buf;
  }

  bit() {
    const byte = this.#bit >> 3;
    if (byte >= this.#buf.length) throw new RangeError("SPS ends early.");
    const value = (this.#buf[byte] >> (7 - (this.#bit & 7))) & 1;
    this.#bit += 1;
    return value;
  }

  bits(n) {
    let value = 0;
    for (let i = 0; i < n; i += 1) value = value * 2 + this.bit();
    return value;
  }

  ue() {
    let zeros = 0;
    while (this.bit() === 0) {
      zeros += 1;
      if (zeros > 31) throw new RangeError("Exp-Golomb code is too long.");
    }
    return 2 ** zeros - 1 + this.bits(zeros);
  }

  se() {
    const k = this.ue();
    return k % 2 === 1 ? (k + 1) / 2 : -(k / 2);
  }
}

const HIGH_PROFILES = new Set([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135]);

function hexByte(value) {
  return value.toString(16).toUpperCase().padStart(2, "0");
}

/**
 * Picture size and codec string from one SPS NAL unit (with its one-byte header).
 * Applies frame cropping, so 1206x2622 comes out as such, not 1216x2624.
 */
export function parseSps(spsNal) {
  const nal = Buffer.isBuffer(spsNal) ? spsNal : Buffer.from(spsNal);
  if (nal.length < 4 || (nal[0] & 0x1f) !== 7) throw new RangeError("Not an SPS NAL unit.");
  const data = rbsp(nal.subarray(1));
  const profileIdc = data[0];
  const constraintFlags = data[1];
  const levelIdc = data[2];
  const r = new BitReader(data.subarray(3));
  r.ue(); // seq_parameter_set_id
  let chromaFormatIdc = 1;
  let separateColourPlane = 0;
  if (HIGH_PROFILES.has(profileIdc)) {
    chromaFormatIdc = r.ue();
    if (chromaFormatIdc > 3) throw new RangeError("Invalid chroma_format_idc.");
    if (chromaFormatIdc === 3) separateColourPlane = r.bit();
    r.ue(); // bit_depth_luma_minus8
    r.ue(); // bit_depth_chroma_minus8
    r.bit(); // qpprime_y_zero_transform_bypass_flag
    if (r.bit()) {
      const lists = chromaFormatIdc === 3 ? 12 : 8;
      for (let i = 0; i < lists; i += 1) {
        if (!r.bit()) continue;
        const size = i < 6 ? 16 : 64;
        let last = 8;
        let next = 8;
        for (let j = 0; j < size; j += 1) {
          if (next !== 0) next = (last + r.se() + 256) % 256;
          last = next === 0 ? last : next;
        }
      }
    }
  }
  r.ue(); // log2_max_frame_num_minus4
  const pocType = r.ue();
  if (pocType === 0) {
    r.ue(); // log2_max_pic_order_cnt_lsb_minus4
  } else if (pocType === 1) {
    r.bit(); // delta_pic_order_always_zero_flag
    r.se(); // offset_for_non_ref_pic
    r.se(); // offset_for_top_to_bottom_field
    const cycle = r.ue();
    if (cycle > 255) throw new RangeError("Invalid num_ref_frames_in_pic_order_cnt_cycle.");
    for (let i = 0; i < cycle; i += 1) r.se();
  }
  r.ue(); // max_num_ref_frames
  r.bit(); // gaps_in_frame_num_value_allowed_flag
  const widthMbs = r.ue() + 1;
  const heightMapUnits = r.ue() + 1;
  const frameMbsOnly = r.bit();
  if (!frameMbsOnly) r.bit(); // mb_adaptive_frame_field_flag
  r.bit(); // direct_8x8_inference_flag
  let crop = { left: 0, right: 0, top: 0, bottom: 0 };
  if (r.bit()) crop = { left: r.ue(), right: r.ue(), top: r.ue(), bottom: r.ue() };
  const chromaArrayType = separateColourPlane ? 0 : chromaFormatIdc;
  const subWidthC = chromaArrayType === 1 || chromaArrayType === 2 ? 2 : 1;
  const subHeightC = chromaArrayType === 1 ? 2 : 1;
  const cropUnitX = chromaArrayType === 0 ? 1 : subWidthC;
  const cropUnitY = (chromaArrayType === 0 ? 1 : subHeightC) * (2 - frameMbsOnly);
  const width = widthMbs * 16 - cropUnitX * (crop.left + crop.right);
  const height = (2 - frameMbsOnly) * heightMapUnits * 16 - cropUnitY * (crop.top + crop.bottom);
  if (width <= 0 || height <= 0) throw new RangeError("SPS cropping leaves no picture.");
  return {
    profileIdc,
    constraintFlags,
    levelIdc,
    chromaFormatIdc,
    frameMbsOnly: frameMbsOnly === 1,
    width,
    height,
    codec: `avc1.${hexByte(profileIdc)}${hexByte(constraintFlags)}${hexByte(levelIdc)}`,
  };
}

const START_CODE = Buffer.from([0, 0, 0, 1]);

/**
 * NAL layout of one access unit. Scanning stops at the first slice (VCL NAL), since
 * every slice of a picture has the same type and SPS/PPS always come before it.
 */
export function analyzeAccessUnit(input) {
  const data = Buffer.isBuffer(input) ? input : Buffer.from(input);
  const nalTypes = [];
  let sps = null;
  let pps = null;
  let i = 0;
  while (i + 3 < data.length) {
    if (data[i] !== 0 || data[i + 1] !== 0 || data[i + 2] !== 1) {
      i += 1;
      continue;
    }
    const start = i + 3;
    const type = data[start] & 0x1f;
    nalTypes.push(type);
    if (type >= 1 && type <= 5) break;
    let next = start + 1;
    while (next + 2 < data.length && !(data[next] === 0 && data[next + 1] === 0 && data[next + 2] === 1)) next += 1;
    let end = next + 2 < data.length ? next : data.length;
    while (end > start && data[end - 1] === 0) end -= 1;
    if (type === 7) sps = data.subarray(start, end);
    else if (type === 8) pps = data.subarray(start, end);
    i = next;
  }
  const config = sps && pps ? Buffer.concat([START_CODE, sps, START_CODE, pps]) : null;
  return { key: nalTypes.includes(5), nalTypes, sps, pps, config };
}

// ---------------------------------------------------------------------------
// gRPC client.
// ---------------------------------------------------------------------------

function abortError(signal) {
  const reason = signal?.reason;
  if (reason instanceof Error) return reason;
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

/**
 * Waits for `promise`, at most `ms`. The timer stays referenced on purpose: the
 * session is unref'd, and on Node 22 the socket teardown that ends a graceful
 * `session.close()` or a call does not keep the event loop alive by itself, so with an
 * unref'd timer the loop could drain first and leave the caller's await pending for
 * good. A referenced timer keeps the loop running until the wait settles, never longer
 * than `ms`.
 */
function withTimeout(promise, ms) {
  let timer;
  return Promise.race([
    promise.then((value) => ({ value, timedOut: false })),
    new Promise((resolve) => {
      timer = setTimeout(() => resolve({ timedOut: true }), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

/**
 * gRPC client for one companion endpoint: `{ socketPath }` (Unix domain socket) or
 * `{ host, port }`. The HTTP/2 session is opened on first use and reopened after it
 * closes.
 */
export class IdbCompanionClient {
  #endpoint;
  #session = null;
  #closed = false;
  #closing = null;
  #videoStreams = new Set();
  #quietStreams = new Set();
  #hid = null;
  #hidOptions;

  /**
   * @param {object} endpoint `{ socketPath }` or `{ host, port }`.
   * @param {object} [options] `{ hidIdleCloseMs, hidHeldIdleCloseMs, hidCloseWaitMs }` for the HID stream.
   */
  constructor(endpoint, { hidIdleCloseMs, hidHeldIdleCloseMs, hidCloseWaitMs } = {}) {
    if (!endpoint || (!endpoint.socketPath && !endpoint.port)) {
      throw new TypeError("Companion endpoint needs socketPath or port.");
    }
    this.#endpoint = { host: "127.0.0.1", ...endpoint };
    this.#hidOptions = {
      ...(hidIdleCloseMs !== undefined ? { idleCloseMs: hidIdleCloseMs } : {}),
      ...(hidHeldIdleCloseMs !== undefined ? { heldIdleCloseMs: hidHeldIdleCloseMs } : {}),
      ...(hidCloseWaitMs !== undefined ? { closeWaitMs: hidCloseWaitMs } : {}),
    };
  }

  get endpoint() {
    return { ...this.#endpoint };
  }

  /** True once close() has begun: no new calls are accepted from then on. */
  get closed() {
    return this.#closed || Boolean(this.#closing);
  }

  #assertOpen() {
    if (this.closed) throw new Error("Companion client is closed.");
  }

  #sessionOrConnect() {
    this.#assertOpen();
    if (this.#session && !this.#session.closed && !this.#session.destroyed) return this.#session;
    const { socketPath, host, port } = this.#endpoint;
    const authority = socketPath ? "http://localhost" : `http://${host.includes(":") ? `[${host}]` : host}:${port}`;
    const options = socketPath ? { createConnection: () => netConnect(socketPath) } : {};
    const session = http2.connect(authority, options);
    // Errors reach every open call through its own stream; this only keeps an
    // unhandled session error from crashing the process.
    session.on("error", () => {});
    session.on("close", () => {
      if (this.#session === session) this.#session = null;
    });
    session.unref?.();
    this.#session = session;
    return session;
  }

  /** Opens a raw gRPC call; the caller writes framed messages and reads data. */
  openCall(method, { timeoutMs } = {}) {
    const session = this.#sessionOrConnect();
    const headers = {
      ":method": "POST",
      ":path": `${GRPC_SERVICE}/${method}`,
      "content-type": "application/grpc",
      te: "trailers",
      "grpc-accept-encoding": "identity",
    };
    if (timeoutMs) headers["grpc-timeout"] = `${Math.max(1, Math.ceil(timeoutMs))}m`;
    return session.request(headers, { endStream: false });
  }

  async unary(method, requestBytes, { timeoutMs = UNARY_TIMEOUT_MS, signal } = {}) {
    if (signal?.aborted) throw abortError(signal);
    const call = this.openCall(method, { timeoutMs });
    const parser = new GrpcMessageParser();
    const messages = [];
    return new Promise((resolve, reject) => {
      let settled = false;
      let status = null;
      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        if (!call.destroyed) call.close(error ? http2.constants.NGHTTP2_CANCEL : undefined);
        if (error) reject(error);
        else resolve(value);
      };
      const onAbort = () => finish(abortError(signal));
      signal?.addEventListener("abort", onAbort, { once: true });
      // Referenced, as in withTimeout(): the session is unref'd, so an unref'd deadline could
      // let the event loop drain and drop the caller's await instead of rejecting it.
      const timer = setTimeout(() => finish(new GrpcError(4, "Deadline exceeded.", { method })), timeoutMs);
      call.on("response", (headers) => {
        status = grpcStatusFrom(headers) ?? status;
      });
      call.on("trailers", (trailers) => {
        status = grpcStatusFrom(trailers) ?? status;
      });
      call.on("data", (chunk) => {
        try {
          messages.push(...parser.push(chunk));
        } catch (error) {
          finish(error);
        }
      });
      call.on("error", (error) => finish(error));
      call.on("close", () => {
        if (!status) return finish(new GrpcError(14, "Call closed without a gRPC status.", { method }));
        if (status.code !== 0) return finish(new GrpcError(status.code, status.details, { method }));
        if (messages.length === 0) return finish(new GrpcError(13, "No response message.", { method }));
        finish(null, messages.at(-1));
      });
      call.end(grpcFrame(requestBytes));
    });
  }

  async describe(options) {
    const request = new ProtoWriter().bool(1, false).finish();
    return decodeTargetDescriptionResponse(await this.unary("describe", request, options));
  }

  async getOrientation(options) {
    return decodeOrientationResponse(await this.unary("get_orientation", Buffer.alloc(0), options));
  }

  /**
   * One `accessibility_info` read of the whole screen (see encodeAccessibilityInfoRequest
   * for `options`); resolves with the companion's JSON document as a string. A companion
   * without the method rejects with GrpcError code 12 (UNIMPLEMENTED), a read that
   * outlives `timeoutMs` with code 4.
   */
  async accessibilityInfo(options = {}, { timeoutMs = ACCESSIBILITY_INFO_TIMEOUT_MS, signal } = {}) {
    const request = encodeAccessibilityInfoRequest(options);
    return decodeAccessibilityInfoResponse(await this.unary("accessibility_info", request, { timeoutMs, signal }));
  }

  /**
   * Opens the server-streaming `accessibility_quiescence` call (see
   * encodeAccessibilityQuiescenceRequest for `request`); iterate the stream for events
   * and `cancel()` it when done. `timeoutMs` (none by default: the stream is meant to
   * stay open) ends it with GrpcError code 4.
   */
  async accessibilityQuiescence(request = {}, { timeoutMs, signal } = {}) {
    this.#assertOpen();
    const stream = new IdbQuiescenceStream(this, encodeAccessibilityQuiescenceRequest(request), { timeoutMs, signal });
    this.#quietStreams.add(stream);
    stream.closed.finally(() => this.#quietStreams.delete(stream));
    stream.start();
    return stream;
  }

  /** Starts a video stream; iterate it for frames and always `stop()` it. */
  async openVideoStream(options = {}, { signal } = {}) {
    this.#assertOpen();
    const stream = new IdbVideoStream(this, options, { signal });
    this.#videoStreams.add(stream);
    stream.closed.finally(() => this.#videoStreams.delete(stream));
    await stream.start();
    return stream;
  }

  /** The one HID stream of this client, opened on first send and ended when idle. */
  hid() {
    this.#assertOpen();
    this.#hid ??= new IdbHidStream(this, this.#hidOptions);
    return this.#hid;
  }

  /**
   * The same HID stream in kept-open mode: its call is opened now and resolves once the
   * request headers are out on a connected session, so the first event pays no connect
   * cost. See IdbHidStream.open().
   */
  async openHid({ timeoutMs } = {}) {
    const hid = this.hid();
    await hid.open({ timeoutMs });
    return hid;
  }

  /** Stops every video stream (sending Stop), cancels quiet streams, ends the HID stream and closes the session. */
  async close(options) {
    if (this.#closed) return;
    // Refuse new calls from the start, so a stream opened while the open ones are
    // being stopped cannot slip past Stop and be cut off with the session.
    this.#closing ??= this.#close(options);
    return this.#closing;
  }

  async #close({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    const pending = [...this.#videoStreams].map((s) => s.stop({ timeoutMs }));
    for (const stream of this.#quietStreams) stream.cancel();
    if (this.#hid) pending.push(this.#hid.close({ timeoutMs }));
    await Promise.allSettled(pending);
    this.#closed = true;
    const session = this.#session;
    this.#session = null;
    if (session && !session.destroyed) {
      const closed = new Promise((resolve) => session.close(resolve));
      if ((await withTimeout(closed, timeoutMs)).timedOut) session.destroy();
    }
  }
}

/**
 * One `video_stream` call (and its successors after `restart()`), read as an async
 * iterator of frames:
 *
 *   { data, key, nalTypes, config, configChanged, width, height, codec, pts, receivedAt }
 *
 * `data` is the Annex B access unit as received; `config` is SPS+PPS in Annex B when
 * this message carried them; `pts` is the arrival time in µs since the stream began.
 * Stop is sent on stop(), on return() from a for-await loop, on abort, and on errors
 * while the call can still carry it. `abnormal` is true when the stream ended without
 * Stop reaching the companion, or with a Stop the companion did not answer in time,
 * either of which may leave its encoder running: restart the companion then.
 */
export class IdbVideoStream {
  #client;
  #options;
  #signal;
  #call = null;
  #generation = 0;
  #stopSent = true;
  #queue = [];
  #waiter = null;
  #done = false;
  #error = null;
  #paused = false;
  #t0 = performance.now();
  #closedResolve;
  #stopping = null;
  #restarting = null;
  #errorThrown = false;
  #onAbort = null;
  abnormal = false;
  stats = { calls: 0, frames: 0, keyFrames: 0, bytes: 0, logs: 0, firstFrameMs: null };
  /** Latest SPS/PPS seen: `{ sps, pps, annexB, width, height, codec }` or null. */
  config = null;

  constructor(client, options = {}, { signal } = {}) {
    this.#client = client;
    this.#options = normalizeVideoOptions(options);
    this.#signal = signal;
    this.closed = new Promise((resolve) => {
      this.#closedResolve = resolve;
    });
  }

  get options() {
    return { ...this.#options };
  }

  get stopSent() {
    return this.#stopSent;
  }

  get done() {
    return this.#done;
  }

  async start() {
    if (this.#done) throw new Error("Video stream is closed.");
    if (this.#signal?.aborted) {
      this.#finish(abortError(this.#signal), { abnormal: false });
      throw abortError(this.#signal);
    }
    if (this.#signal && !this.#onAbort) {
      this.#onAbort = () => {
        this.#error ??= abortError(this.#signal);
        this.stop().catch(() => {});
      };
      this.#signal.addEventListener("abort", this.#onAbort, { once: true });
    }
    try {
      this.#openCall();
    } catch (error) {
      this.#finish(error, { abnormal: false });
      throw error;
    }
  }

  #openCall() {
    const generation = ++this.#generation;
    const call = this.#client.openCall("video_stream");
    const parser = new GrpcMessageParser();
    let status = null;
    this.#call = call;
    this.#stopSent = false;
    this.stats.calls += 1;
    const current = () => generation === this.#generation && !this.#done;
    call.on("response", (headers) => {
      status = grpcStatusFrom(headers) ?? status;
    });
    call.on("trailers", (trailers) => {
      status = grpcStatusFrom(trailers) ?? status;
    });
    call.on("data", (chunk) => {
      // Frames that arrive while Stop drains are dropped: the reader asked to stop.
      if (!current() || this.#stopping) return;
      let messages;
      try {
        messages = parser.push(chunk);
      } catch (error) {
        this.#fail(error);
        return;
      }
      for (const message of messages) {
        try {
          this.#onMessage(message);
        } catch (error) {
          this.#fail(error);
          return;
        }
      }
    });
    call.on("error", (error) => {
      if (current() && !this.#stopping) this.#fail(error);
    });
    const ended = () => {
      if (!current() || this.#stopping) return;
      // The companion ended the call by itself, so its encoder is done too. Without a
      // status the transport dropped, and the encoder may still be running.
      this.#call = null;
      if (!call.destroyed) call.close();
      if (!status) {
        const lost = new GrpcError(14, "Video stream ended without a gRPC status (connection lost).", {
          method: "video_stream",
        });
        this.#finish(lost, { abnormal: true });
      } else if (status.code !== 0) {
        this.#finish(new GrpcError(status.code, status.details, { method: "video_stream" }), { abnormal: false });
      } else {
        this.#finish(null, { abnormal: false });
      }
    };
    // The response side ends once trailers arrive; 'close' alone would wait for our
    // request side too, which stays open until Stop.
    call.on("end", ended);
    call.on("close", ended);
    call.write(grpcFrame(encodeVideoStreamStart(this.#options)));
  }

  #onMessage(message) {
    const { data, log } = decodeVideoStreamResponse(message);
    if (log) {
      this.stats.logs += 1;
      return;
    }
    if (!data || data.length === 0) return;
    const receivedAt = performance.now();
    let frame;
    if (this.#options.format === VIDEO_FORMAT.H264) {
      const unit = analyzeAccessUnit(data);
      let configChanged = false;
      if (unit.config && (!this.config || !unit.config.equals(this.config.annexB))) {
        let info = null;
        try {
          info = parseSps(unit.sps);
        } catch {
          info = null;
        }
        this.config = {
          sps: Buffer.from(unit.sps),
          pps: Buffer.from(unit.pps),
          annexB: unit.config,
          width: info?.width ?? null,
          height: info?.height ?? null,
          codec: info?.codec ?? null,
        };
        configChanged = true;
      }
      frame = {
        data,
        key: unit.key,
        nalTypes: unit.nalTypes,
        config: unit.config,
        configChanged,
        width: this.config?.width ?? null,
        height: this.config?.height ?? null,
        codec: this.config?.codec ?? null,
      };
    } else {
      frame = {
        data,
        key: true,
        nalTypes: [],
        config: null,
        configChanged: false,
        width: null,
        height: null,
        codec: null,
      };
    }
    frame.pts = Math.round((receivedAt - this.#t0) * 1000);
    frame.receivedAt = receivedAt;
    this.stats.frames += 1;
    this.stats.bytes += data.length;
    if (frame.key) this.stats.keyFrames += 1;
    this.stats.firstFrameMs ??= Math.round(receivedAt - this.#t0);
    this.#push(frame);
  }

  #push(frame) {
    if (this.#waiter) {
      const { resolve } = this.#waiter;
      this.#waiter = null;
      resolve({ value: frame, done: false });
      return;
    }
    this.#queue.push(frame);
    if (this.#queue.length >= FRAME_QUEUE_HIGH_WATER && !this.#paused && this.#call) {
      this.#paused = true;
      this.#call.pause();
    }
  }

  #fail(error) {
    if (this.#done) return;
    this.#error ??= error;
    // An error on our side (bad message, decode failure) leaves the call open: Stop it.
    this.stop().catch(() => {});
  }

  #finish(error, { abnormal }) {
    if (this.#done) return;
    this.#done = true;
    if (error) this.#error ??= error;
    if (abnormal) this.abnormal = true;
    if (this.#onAbort) this.#signal?.removeEventListener("abort", this.#onAbort);
    if (this.#waiter) {
      const { resolve, reject } = this.#waiter;
      this.#waiter = null;
      if (this.#error && this.#queue.length === 0) reject(this.#error);
      else resolve({ value: undefined, done: true });
    }
    this.#closedResolve({ abnormal: this.abnormal, stopSent: this.#stopSent, error: this.#error });
  }

  /**
   * Sends Stop on the current call, ends its request side and waits (bounded) for the
   * companion to finish it. Returns `{ stopSent, acknowledged }`.
   */
  async #stopCall(timeoutMs) {
    const call = this.#call;
    this.#call = null;
    if (!call) return { stopSent: this.#stopSent, acknowledged: true };
    const ended = new Promise((resolve) => {
      if (call.closed || call.destroyed) resolve();
      else call.once("close", resolve);
    });
    // Keep reading so the companion is not blocked by flow control while it drains.
    if (this.#paused) {
      this.#paused = false;
      call.resume();
    }
    if (!this.#stopSent && call.writable && !call.destroyed) {
      const written = new Promise((resolve) => {
        call.write(grpcFrame(encodeVideoStreamStop()), (error) => resolve(!error));
      });
      const { value, timedOut } = await withTimeout(written, timeoutMs);
      if (!timedOut && value) this.#stopSent = true;
      if (!call.destroyed && call.writable) call.end();
    }
    const { timedOut } = await withTimeout(ended, timeoutMs);
    if (!call.destroyed) call.close(http2.constants.NGHTTP2_CANCEL);
    return { stopSent: this.#stopSent, acknowledged: !timedOut };
  }

  /** Stops the stream for good. Safe to call more than once and from any path. */
  async stop({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    if (this.#restarting) await this.#restarting.catch(() => {});
    if (this.#stopping) return this.#stopping;
    if (this.#done && !this.#call) {
      return { stopSent: this.#stopSent, abnormal: this.abnormal, error: this.#error };
    }
    this.#stopping = (async () => {
      // A Stop written but not answered by ending the call in time may leave the encoder
      // running, as in restart(): abnormal, so the owner replaces the companion.
      const { stopSent, acknowledged } = await this.#stopCall(timeoutMs);
      this.#finish(null, { abnormal: !stopSent || !acknowledged });
      return { stopSent, abnormal: this.abnormal, error: this.#error };
    })();
    return this.#stopping;
  }

  /**
   * Stop on the current call, then a fresh Start on a new one; the iterator keeps
   * going. A new start brings SPS, PPS and an IDR within about 0.15 s, which is the
   * only way to get a keyframe on demand.
   *
   * A Stop that could not be written, or that the companion did not answer by ending
   * the call in time, may leave the old encoder running. No new Start goes out on that
   * companion then: the stream ends at once as abnormal (restart() rejects), so its
   * owner replaces the companion before it opens the next stream, never two encoders.
   *
   * With `options`, the new Start uses them (normalized like the constructor's) instead of
   * the ones given before: how a Canvas changes the frame rate or scale of a running stream.
   */
  async restart({ timeoutMs = STREAM_STOP_TIMEOUT_MS, options } = {}) {
    if (this.#done || this.#stopping || this.#restarting) throw new Error("Video stream is closed or restarting.");
    // Validated before anything stops: bad options leave the running stream alone.
    const nextOptions = options === undefined ? null : normalizeVideoOptions(options);
    // Events of the old call are stale from here on.
    this.#generation += 1;
    this.#restarting = (async () => {
      const { stopSent, acknowledged } = await this.#stopCall(timeoutMs);
      if (!stopSent || !acknowledged) {
        throw new Error(`Stop was not ${stopSent ? "answered" : "written"} on the old video call; ` +
          "its encoder may still run, so the companion must be restarted.");
      }
      this.#paused = false;
      if (nextOptions) this.#options = nextOptions;
      this.#openCall();
    })();
    try {
      await this.#restarting;
    } catch (error) {
      this.#finish(error, { abnormal: true });
      throw error;
    } finally {
      this.#restarting = null;
    }
  }

  next() {
    if (this.#queue.length > 0) {
      const value = this.#queue.shift();
      if (this.#paused && this.#queue.length < FRAME_QUEUE_HIGH_WATER / 2 && this.#call) {
        this.#paused = false;
        this.#call.resume();
      }
      return Promise.resolve({ value, done: false });
    }
    if (this.#done) {
      if (this.#error && !this.#errorThrown) {
        this.#errorThrown = true;
        return Promise.reject(this.#error);
      }
      return Promise.resolve({ value: undefined, done: true });
    }
    if (this.#waiter) return Promise.reject(new Error("Only one pending read is allowed."));
    return new Promise((resolve, reject) => {
      this.#waiter = {
        resolve,
        reject: (error) => {
          this.#errorThrown = true;
          reject(error);
        },
      };
    });
  }

  async return() {
    await this.stop();
    this.#queue.length = 0;
    return { value: undefined, done: true };
  }

  async throw(error) {
    this.#error ??= error;
    await this.stop();
    throw error;
  }

  [Symbol.asyncIterator]() {
    return this;
  }
}

/** Events queued on a quiet stream before its HTTP/2 stream is paused (flow control then backs up). */
const QUIET_QUEUE_HIGH_WATER = 256;

/**
 * One server-streaming `accessibility_quiescence` call, read as an async iterator of
 * decoded events (see decodeAccessibilityQuiescenceResponse). The request goes out with
 * the request side ended, as the reference client sends it. The iterator ends when the
 * companion finishes the call with status OK (after `target_exited`, for one), rejects
 * with a GrpcError for any other status (12 for a companion without the method), for a
 * dropped call (14) or at the deadline (4), and ends quietly after `cancel()`.
 *
 * `started` resolves once the companion answered the call with headers or a first event,
 * and rejects when it ends with an error before that; `closed` always resolves with
 * `{ error, status, cancelled }`.
 */
export class IdbQuiescenceStream {
  #client;
  #request;
  #timeoutMs;
  #signal;
  #onAbort = null;
  #call = null;
  #timer = null;
  #queue = [];
  #waiter = null;
  #paused = false;
  #done = false;
  #error = null;
  #errorThrown = false;
  #cancelled = false;
  #status = null;
  #startedResolve;
  #startedReject;
  #startedSettled = false;
  #closedResolve;
  events = 0;

  constructor(client, requestBytes, { timeoutMs, signal } = {}) {
    if (timeoutMs !== undefined && timeoutMs !== null) requireNumber(timeoutMs, "timeoutMs", { min: 0, exclusiveMin: true });
    this.#client = client;
    this.#request = Buffer.from(requestBytes);
    this.#timeoutMs = timeoutMs ?? null;
    this.#signal = signal;
    this.started = new Promise((resolve, reject) => {
      this.#startedResolve = resolve;
      this.#startedReject = reject;
    });
    // A caller that only iterates never awaits `started`: its rejection is reported there.
    this.started.catch(() => {});
    this.closed = new Promise((resolve) => {
      this.#closedResolve = resolve;
    });
  }

  get done() {
    return this.#done;
  }

  /** The final gRPC status `{ code, details }`, once the companion sent one. */
  get status() {
    return this.#status ? { ...this.#status } : null;
  }

  start() {
    if (this.#call || this.#done) throw new Error("Quiet stream already started or closed.");
    if (this.#signal?.aborted) {
      this.#finish(abortError(this.#signal));
      return;
    }
    if (this.#signal) {
      this.#onAbort = () => this.#fail(abortError(this.#signal));
      this.#signal.addEventListener("abort", this.#onAbort, { once: true });
    }
    let call;
    try {
      call = this.#client.openCall("accessibility_quiescence", { timeoutMs: this.#timeoutMs ?? undefined });
    } catch (error) {
      this.#finish(error);
      return;
    }
    this.#call = call;
    if (this.#timeoutMs) {
      // Referenced, as for unary(): the session is unref'd.
      this.#timer = setTimeout(
        () => this.#fail(new GrpcError(4, "Deadline exceeded.", { method: "accessibility_quiescence" })),
        this.#timeoutMs,
      );
    }
    const parser = new GrpcMessageParser();
    call.on("response", (headers) => {
      this.#status = grpcStatusFrom(headers) ?? this.#status;
      if (!this.#status || this.#status.code === 0) this.#markStarted();
    });
    call.on("trailers", (trailers) => {
      this.#status = grpcStatusFrom(trailers) ?? this.#status;
    });
    call.on("data", (chunk) => {
      if (this.#done) return;
      let messages;
      try {
        messages = parser.push(chunk);
      } catch (error) {
        this.#fail(error);
        return;
      }
      for (const message of messages) {
        let event;
        try {
          event = decodeAccessibilityQuiescenceResponse(message);
        } catch (error) {
          this.#fail(error);
          return;
        }
        this.events += 1;
        this.#markStarted();
        this.#push(event);
      }
    });
    call.on("error", (error) => this.#fail(error));
    const ended = () => {
      if (this.#done) return;
      const status = this.#status;
      if (!status) {
        this.#finish(new GrpcError(14, "Quiet stream ended without a gRPC status (connection lost).", {
          method: "accessibility_quiescence",
        }));
      } else if (status.code !== 0) {
        this.#finish(new GrpcError(status.code, status.details, { method: "accessibility_quiescence" }));
      } else {
        this.#finish(null);
      }
    };
    call.on("end", ended);
    call.on("close", ended);
    call.end(grpcFrame(this.#request));
  }

  #markStarted() {
    if (this.#startedSettled) return;
    this.#startedSettled = true;
    this.#startedResolve(true);
  }

  #push(event) {
    if (this.#waiter) {
      const { resolve } = this.#waiter;
      this.#waiter = null;
      resolve({ value: event, done: false });
      return;
    }
    this.#queue.push(event);
    if (this.#queue.length >= QUIET_QUEUE_HIGH_WATER && !this.#paused && this.#call) {
      this.#paused = true;
      this.#call.pause();
    }
  }

  /** An error on our side or a deadline: the call is cancelled, the iterator rejects. */
  #fail(error) {
    if (this.#done) return;
    this.#finish(error);
  }

  #finish(error) {
    if (this.#done) return;
    this.#done = true;
    clearTimeout(this.#timer);
    if (this.#onAbort) this.#signal?.removeEventListener("abort", this.#onAbort);
    if (error) this.#error ??= error;
    const call = this.#call;
    this.#call = null;
    // Cancel a call still running on our side (error, deadline, cancel()); one the
    // companion finished with status OK is just released.
    if (call && !call.destroyed) call.close(this.#error || this.#cancelled ? http2.constants.NGHTTP2_CANCEL : undefined);
    if (!this.#startedSettled) {
      this.#startedSettled = true;
      if (this.#error) this.#startedReject(this.#error);
      else this.#startedResolve(!this.#cancelled);
    }
    if (this.#waiter) {
      const { resolve, reject } = this.#waiter;
      this.#waiter = null;
      if (this.#error && this.#queue.length === 0) reject(this.#error);
      else resolve({ value: undefined, done: true });
    }
    this.#closedResolve({ error: this.#error, status: this.status, cancelled: this.#cancelled });
  }

  /** Cancels the call (RST_STREAM CANCEL); the iterator ends without an error. Safe to repeat. */
  cancel() {
    if (this.#done) return;
    this.#cancelled = true;
    this.#finish(null);
  }

  next() {
    if (this.#queue.length > 0) {
      const value = this.#queue.shift();
      if (this.#paused && this.#queue.length < QUIET_QUEUE_HIGH_WATER / 2 && this.#call) {
        this.#paused = false;
        this.#call.resume();
      }
      return Promise.resolve({ value, done: false });
    }
    if (this.#done) {
      if (this.#error && !this.#errorThrown) {
        this.#errorThrown = true;
        return Promise.reject(this.#error);
      }
      return Promise.resolve({ value: undefined, done: true });
    }
    if (this.#waiter) return Promise.reject(new Error("Only one pending read is allowed."));
    return new Promise((resolve, reject) => {
      this.#waiter = {
        resolve,
        reject: (error) => {
          this.#errorThrown = true;
          reject(error);
        },
      };
    });
  }

  async return() {
    this.cancel();
    this.#queue.length = 0;
    return { value: undefined, done: true };
  }

  async throw(error) {
    this.cancel();
    throw error;
  }

  [Symbol.asyncIterator]() {
    return this;
  }
}

/**
 * Events of ended HID calls that the companion did not confirm with its OK status: the
 * call was cancelled at the safety limit (`closeWaitMs`) or by close(), failed, or its
 * session was lost before the companion served it. They may not have been performed.
 */
export class HidUnconfirmedError extends Error {
  constructor(events, cause = null) {
    const what = events === 1 ? "1 HID event was" : `${events} HID events were`;
    super(`${what} sent but not confirmed by idb_companion${cause?.message ? ` (${cause.message})` : ""}; `
      + `${events === 1 ? "it" : "they"} may not have been performed.`);
    this.name = "HidUnconfirmedError";
    this.code = "hid_unconfirmed";
    this.events = events;
    this.cause = cause;
  }
}

/**
 * The `hid` client stream. Events go out on one open call; after the call ends or
 * fails, the next event opens a new one. Touch points are logical points.
 *
 * idb_companion serves one `hid` call at a time: while one call is open, every other
 * client's `hid` call (another gRPC session, or an `idb ui text`/`idb ui key` process)
 * waits until it ends, and none of its events are read meanwhile. So a call never stays
 * open while idle: `idleCloseMs` (default HID_IDLE_CLOSE_MS) after the last event was
 * written, with no press held down, the stream ends the call cleanly (half-close, no
 * `drop`), and the next send or open() opens a new one. While a touch, button or key is
 * held down the window is `heldIdleCloseMs` (default HID_HELD_IDLE_CLOSE_MS), so a down
 * and its up stay on one call unless the press is left without an event that long.
 * Events sent while an ended call is still closing wait for that call to close and then
 * go out on the new call in send order. `idleCloseMs: 0` keeps a call open until close()
 * (the old behaviour).
 *
 * An ended call that carried events waits for the companion's status for as long as the
 * companion takes to serve it, up to `closeWaitMs` (default HID_CLOSE_WAIT_MS): while
 * another client holds the companion's call, this call's events are written but not read
 * yet, and cancelling it would throw them away although send() already resolved. A call
 * that closes without status OK after it carried events (cancelled at the limit or by
 * close(), failed, or its session lost) emits `lost` with `{ call, events, error }`;
 * `lostEvents` counts them. flush() ends the current call at once and resolves when the
 * companion confirmed every event sent so far, or rejects with HidUnconfirmedError.
 * cancel() gives up at once on every call not confirmed yet (its owner's own deadline):
 * they are cancelled, their events reported lost, sends still waiting are refused, and
 * the stream stays usable.
 *
 * Kept-open mode (`open()`, or `client.openHid()`): the call is opened before any event,
 * so the first event pays no connect cost; it too ends after the idle window. When the
 * companion ends or drops an open call other than through close(), flush() or an idle
 * close, the stream emits `drop` with `{ error, status, call }` (error null for a clean
 * status OK) so its owner learns of it at once rather than on the next send; the next
 * send or open() opens a new call. An idle close (and a flush) emits `idle_close` with
 * `{ call }`.
 */
export class IdbHidStream extends EventEmitter {
  #client;
  #call = null;
  #closed = false;
  #idleCloseMs;
  #heldIdleCloseMs;
  #closeWaitMs;
  #idleTimer = null;
  /** Bumped by cancel(): a send queued before it is refused, never written later. */
  #generation = 0;
  /** Writes handed to the current call whose callback has not run yet. */
  #writing = 0;
  /** Sends waiting for an ended call to finish closing. */
  #queued = 0;
  /** Resolves once the call being ended has closed (or was cancelled); null when none. */
  #ending = null;
  /** The call being ended, until it closes. */
  #endingCall = null;
  #tail = Promise.resolve();
  /** The last write handed to a call, settled either way. */
  #lastWrite = Promise.resolve();
  /** Records of calls that carried events and have not closed yet. */
  #unsettled = new Set();
  /** Events lost since the last flush(), and the cause of the latest loss. */
  #lostUnreported = 0;
  #lostError = null;
  /** Presses held down: "touch", "button:<name>", "key:<code>". */
  #held = new Set();
  lastError = null;
  sent = 0;
  /** Calls opened so far. */
  calls = 0;
  /** Calls this stream ended because they were idle (or flushed). */
  idleCloses = 0;
  /** Events on calls that closed without the companion's OK status (see `lost`). */
  lostEvents = 0;
  /** True once open() asked for kept-open mode. */
  keepOpen = false;

  /**
   * @param {object} client An IdbCompanionClient (anything with `openCall("hid")`).
   * @param {object} [options]
   * @param {number} [options.idleCloseMs] Idle window before the call ends; 0 = never.
   * @param {number} [options.heldIdleCloseMs] The same window while a press is held down.
   * @param {number} [options.closeWaitMs] Safety limit an ended call with events waits for its status.
   */
  constructor(client, {
    idleCloseMs = HID_IDLE_CLOSE_MS, heldIdleCloseMs = HID_HELD_IDLE_CLOSE_MS, closeWaitMs = HID_CLOSE_WAIT_MS,
  } = {}) {
    super();
    this.#client = client;
    this.#idleCloseMs = nonNegativeMs(idleCloseMs, "idleCloseMs");
    this.#heldIdleCloseMs = Math.max(this.#idleCloseMs, nonNegativeMs(heldIdleCloseMs, "heldIdleCloseMs"));
    this.#closeWaitMs = nonNegativeMs(closeWaitMs, "closeWaitMs");
  }

  /** True while a call is open for writing. */
  get isOpen() {
    const call = this.#call;
    return Boolean(call) && !call.destroyed && call.writable;
  }

  /** The idle window in ms (0: calls stay open until close()). */
  get idleCloseMs() {
    return this.#idleCloseMs;
  }

  /** The safety limit an ended call with events waits for the companion's status. */
  get closeWaitMs() {
    return this.#closeWaitMs;
  }

  /** Events sent on calls that have not been confirmed or lost yet. */
  get unconfirmed() {
    let events = 0;
    for (const record of this.#unsettled) events += record.events;
    return events;
  }

  #open() {
    const call = this.#client.openCall("hid");
    const number = ++this.calls;
    const record = { number, events: 0, status: null, error: null };
    record.settled = new Promise((resolve) => {
      record.resolve = resolve;
    });
    call.hidRecord = record;
    let callError = null;
    call.on("error", (error) => {
      callError ??= error;
      this.lastError = error;
      if (this.#call === call) this.#forget(call);
    });
    call.on("response", (headers) => {
      record.status = grpcStatusFrom(headers) ?? record.status;
      const status = record.status;
      if (status && status.code !== 0) this.lastError = new GrpcError(status.code, status.details, { method: "hid" });
    });
    call.on("trailers", (trailers) => {
      record.status = grpcStatusFrom(trailers) ?? record.status;
      const status = record.status;
      if (status && status.code !== 0) this.lastError = new GrpcError(status.code, status.details, { method: "hid" });
    });
    call.on("close", () => {
      if (this.#call === call) this.#forget(call);
      if (this.#endingCall === call) this.#endingCall = null;
      // close(), flush() and an idle close end the call on purpose; only an end the owner
      // did not ask for is a drop.
      if (!this.#closed && !call.idleClosed) {
        const status = record.status;
        let error = callError;
        if (!error && !status) error = new GrpcError(14, "HID stream ended without a gRPC status (connection lost).", { method: "hid" });
        if (!error && status.code !== 0) error = new GrpcError(status.code, status.details, { method: "hid" });
        this.emit("drop", { error, status: status ? { ...status } : null, call: number });
      }
      this.#settle(record, callError);
    });
    call.on("end", () => {
      // The companion finished the call; the next event opens a new one.
      if (this.#call === call) this.#forget(call);
      if (!call.destroyed && call.writable) call.end();
    });
    // HIDResponse is empty; drain it so the call can close.
    call.on("data", () => {});
    this.#call = call;
    this.#writing = 0;
    return call;
  }

  /**
   * A call closed: its events are confirmed by status OK (the companion reads the call to
   * its end before it answers), and lost otherwise.
   */
  #settle(record, callError) {
    this.#unsettled.delete(record);
    const confirmed = record.status?.code === 0 && !record.error;
    let error = null;
    if (!confirmed && record.events > 0) {
      const status = record.status;
      error = record.error ?? callError
        ?? (status && status.code !== 0 ? new GrpcError(status.code, status.details, { method: "hid" }) : null)
        ?? new GrpcError(14, "HID call closed without a gRPC status (connection lost).", { method: "hid" });
      if (record.error) this.lastError = record.error;
      else this.lastError ??= error;
      this.lostEvents += record.events;
      this.#lostUnreported += record.events;
      this.#lostError = error;
      this.emit("lost", { call: record.number, events: record.events, error });
    }
    record.resolve({ confirmed: confirmed || record.events === 0, events: record.events, error });
  }

  /** The current call is gone: nothing to idle-close any more. */
  #forget(call) {
    if (this.#call !== call) return;
    this.#call = null;
    this.#writing = 0;
    this.#clearIdle();
  }

  #clearIdle() {
    clearTimeout(this.#idleTimer);
    this.#idleTimer = null;
  }

  /** Arms the idle close once nothing is being written or waiting to be. */
  #armIdle() {
    this.#clearIdle();
    if (!this.#idleCloseMs || this.#closed || this.#writing > 0 || this.#queued > 0) return;
    const call = this.#call;
    if (!call || call.destroyed || !call.writable) return;
    const ms = this.#held.size > 0 ? this.#heldIdleCloseMs : this.#idleCloseMs;
    this.#idleTimer = setTimeout(() => {
      this.#idleTimer = null;
      if (this.#call !== call || this.#writing > 0 || this.#queued > 0 || this.#closed) return;
      this.#idleClose(call);
    }, ms);
    this.#idleTimer.unref?.();
  }

  /**
   * Ends a call cleanly: half-close, then wait for the companion's status. Sends that
   * arrive meanwhile wait for the close, so the companion, which serves one call at a
   * time, gets every event in send order. A call that carried events is never cancelled
   * on a short timer: it may not have been served yet (another client holds the
   * companion's call), and a cancel would throw its events away. It waits up to
   * `closeWaitMs`; at that limit it is cancelled and its events reported as lost.
   */
  #idleClose(call) {
    this.#forget(call);
    call.idleClosed = true;
    this.idleCloses += 1;
    // A press still held after its (longer) window was left without its up: the next
    // call starts with nothing held, so it gets the short window again.
    this.#held.clear();
    const record = call.hidRecord;
    const closed = new Promise((resolve) => {
      if (call.destroyed) resolve();
      else call.once("close", resolve);
    });
    if (call.writable) call.end();
    // With no event on it a cancel loses nothing: the short stop timeout is enough.
    const limit = record && record.events > 0 ? this.#closeWaitMs : STREAM_STOP_TIMEOUT_MS;
    const ending = withTimeout(closed, limit).then(async ({ timedOut }) => {
      if (!timedOut || call.destroyed) return;
      if (record && record.events > 0) {
        record.error ??= new GrpcError(4, `the HID call was not served within ${limit} ms `
          + "(another client may hold the companion's hid call), so it was cancelled", { method: "hid" });
      }
      call.close(http2.constants.NGHTTP2_CANCEL);
      await withTimeout(closed, STREAM_STOP_TIMEOUT_MS);
    });
    this.#endingCall = call;
    this.#ending = ending;
    this.#tail = ending;
    ending.then(() => {
      if (this.#ending === ending) this.#ending = null;
    });
    // Last, so a send from a listener already waits for this call to close.
    this.emit("idle_close", { call: record?.number ?? this.calls });
  }

  /**
   * Kept-open mode: opens the call now when none is open, and resolves once its request
   * headers are out on a connected session (the stream has its id). Rejects when the
   * call fails or closes first, and with GrpcError code 4 when that takes longer than
   * `timeoutMs` (the call is cancelled then). Calling it on an open call is a no-op.
   */
  async open({ timeoutMs = UNARY_TIMEOUT_MS } = {}) {
    if (this.#closed) throw new Error("HID stream is closed.");
    this.keepOpen = true;
    // An ended call still closing, or sends waiting for it: open after them.
    while (this.#ending || this.#queued > 0) await this.#tail;
    if (this.#closed) throw new Error("HID stream is closed.");
    let call = this.#call;
    if (!call || call.destroyed || !call.writable) call = this.#open();
    if (!call.pending) {
      if (this.#writing === 0) this.#armIdle();
      return;
    }
    let cleanup = () => {};
    const ready = new Promise((resolve, reject) => {
      const onReady = () => {
        cleanup();
        resolve();
      };
      const onGone = (error) => {
        cleanup();
        reject(error ?? this.lastError ?? new GrpcError(14, "HID stream closed before it was open.", { method: "hid" }));
      };
      const onError = (error) => onGone(error);
      const onClose = () => onGone(null);
      cleanup = () => {
        call.off("ready", onReady);
        call.off("error", onError);
        call.off("close", onClose);
      };
      call.once("ready", onReady);
      call.once("error", onError);
      call.once("close", onClose);
    });
    ready.catch(() => {});
    const { timedOut } = await withTimeout(ready, timeoutMs);
    if (timedOut) {
      cleanup();
      if (this.#call === call) this.#forget(call);
      if (!call.destroyed) call.close(http2.constants.NGHTTP2_CANCEL);
      throw new GrpcError(4, "HID stream was not open within the deadline.", { method: "hid" });
    }
    // Opened with no event: it must not stay open while idle either.
    if (this.#call === call && this.#writing === 0) this.#armIdle();
  }

  /** Sends one HID event (see encodeHidEvent); resolves once it is written. */
  send(event) {
    if (this.#closed) return Promise.reject(new Error("HID stream is closed."));
    let frame;
    try {
      frame = grpcFrame(encodeHidEvent(event));
    } catch (error) {
      return Promise.reject(error);
    }
    this.#clearIdle();
    this.#track(event);
    if (!this.#ending && this.#queued === 0) return this.#write(frame);
    // An ended call is closing: go out on the next call, after every earlier send.
    this.#queued += 1;
    const generation = this.#generation;
    const run = this.#tail.then(() => {
      this.#queued -= 1;
      if (generation !== this.#generation) throw new Error("HID send cancelled before it was written.");
      return this.#write(frame);
    });
    this.#tail = run.then(() => {}, () => {});
    return run;
  }

  /**
   * Ends the current call now, as an idle close does (a press still held is let go of),
   * and resolves once the companion confirmed every event sent so far with status OK:
   * the companion reads a call to its end before it answers, so they were performed.
   * While another client holds the companion's hid call this waits, up to `closeWaitMs`
   * per call. Rejects with HidUnconfirmedError when events sent since the last flush()
   * were lost (the limit was hit, a call failed, the session was lost, or close() cut a
   * call short); the count is then reset.
   */
  async flush() {
    // Every send handed over so far is written first.
    for (;;) {
      if (this.#queued > 0) await this.#tail;
      else if (this.#writing > 0) await this.#lastWrite;
      else break;
    }
    const call = this.#call;
    if (call && !call.destroyed && call.writable) {
      this.#clearIdle();
      this.#idleClose(call);
    }
    await Promise.all([...this.#unsettled].map((record) => record.settled));
    const lost = this.#lostUnreported;
    if (lost > 0) {
      const error = new HidUnconfirmedError(lost, this.#lostError);
      this.#lostUnreported = 0;
      this.#lostError = null;
      throw error;
    }
  }

  /** Remembers which presses are held down, for the idle window. */
  #track(event) {
    const direction = event?.direction;
    if (direction === undefined || direction === null) return;
    let key = null;
    if (event.touch) key = "touch";
    else if (event.button !== undefined && event.button !== null) key = `button:${String(event.button).toUpperCase()}`;
    else if (event.key !== undefined && event.key !== null) key = `key:${event.key}`;
    if (!key) return;
    const down = direction === HID_DIRECTION.DOWN || String(direction).toLowerCase() === "down";
    if (down) this.#held.add(key);
    else this.#held.delete(key);
  }

  #write(frame) {
    if (this.#closed) return Promise.reject(new Error("HID stream is closed."));
    let call;
    try {
      call = this.#call;
      if (!call || call.destroyed || !call.writable) call = this.#open();
    } catch (error) {
      return Promise.reject(error);
    }
    this.#clearIdle();
    this.#writing += 1;
    // Unconfirmed from the moment the call has it, until the call's status.
    const record = call.hidRecord;
    if (record) {
      record.events += 1;
      this.#unsettled.add(record);
    }
    const written = new Promise((resolve, reject) => {
      call.write(frame, (error) => {
        if (this.#call === call) this.#writing = Math.max(0, this.#writing - 1);
        if (error) {
          // The sender learns of this one from the rejection.
          if (record) record.events = Math.max(0, record.events - 1);
          this.lastError = error;
          if (this.#call === call) this.#forget(call);
          reject(error);
        } else {
          this.sent += 1;
          if (this.#call === call && this.#writing === 0) this.#armIdle();
          resolve();
        }
      });
    });
    this.#lastWrite = written.then(() => {}, () => {});
    return written;
  }

  touchDown(x, y) {
    return this.send({ touch: { x, y }, direction: "down" });
  }

  /** A held finger moves by repeating DOWN at the new point. */
  touchMove(x, y) {
    return this.touchDown(x, y);
  }

  touchUp(x, y) {
    return this.send({ touch: { x, y }, direction: "up" });
  }

  async tap(x, y) {
    await this.touchDown(x, y);
    await this.touchUp(x, y);
  }

  buttonDown(button) {
    return this.send({ button, direction: "down" });
  }

  buttonUp(button) {
    return this.send({ button, direction: "up" });
  }

  async pressButton(button) {
    await this.buttonDown(button);
    await this.buttonUp(button);
  }

  keyDown(keycode) {
    return this.send({ key: keycode, direction: "down" });
  }

  /** One canned two-finger pinch: `{ center: { x, y }, scale, duration, radius }` in points and seconds. */
  pinch(pinch) {
    return this.send({ pinch });
  }

  keyUp(keycode) {
    return this.send({ key: keycode, direction: "up" });
  }

  /**
   * Refuses new sends, ends the open call and the one still being ended (an idle close or
   * a flush in progress), and waits up to `timeoutMs` for their status; a call not done by
   * then is cancelled, and its events, if any, are reported as lost.
   */
  async close({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    this.#closed = true;
    this.#clearIdle();
    const calls = [this.#endingCall, this.#call].filter((call) => call && !call.destroyed);
    const ending = this.#ending;
    this.#call = null;
    this.#writing = 0;
    await Promise.all(calls.map((call) => this.#endCall(call, timeoutMs)));
    if (ending) await ending;
  }

  /**
   * Gives up now on every call not confirmed yet, the open one and the one being ended:
   * each is cancelled (a call the companion has not read yet is then never performed)
   * and its events reported lost (`lost`, flush() rejects with HidUnconfirmedError);
   * sends still waiting for an ended call are refused. Unlike close() the stream stays
   * usable: the next send opens a new call. Resolves once those calls closed.
   */
  async cancel(reason = "its owner's deadline passed") {
    this.#generation += 1;
    this.#clearIdle();
    this.#held.clear();
    const current = this.#call;
    if (current) {
      // Ended on purpose: not a drop.
      current.idleClosed = true;
      this.#forget(current);
    }
    const calls = [this.#endingCall, current].filter((call) => call && !call.destroyed);
    const ending = this.#ending;
    await Promise.all(calls.map((call) => this.#endCall(call, 0, reason)));
    if (ending) await ending;
  }

  async #endCall(call, timeoutMs, reason = null) {
    const ended = new Promise((resolve) => {
      if (call.destroyed) resolve();
      else call.once("close", resolve);
    });
    if (call.writable) call.end();
    const { timedOut } = await withTimeout(ended, timeoutMs);
    if (!timedOut || call.destroyed) return;
    const record = call.hidRecord;
    if (record && record.events > 0) {
      record.error ??= new GrpcError(4, reason
        ? `the HID call was cancelled before idb_companion confirmed it: ${reason}`
        : `the HID stream was closed before idb_companion confirmed the call within ${timeoutMs} ms, `
          + "so it was cancelled", { method: "hid" });
    }
    call.close(http2.constants.NGHTTP2_CANCEL);
    await withTimeout(ended, STREAM_STOP_TIMEOUT_MS);
  }
}

function nonNegativeMs(value, name) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 0) throw new RangeError(`${name} must be a non-negative number of milliseconds.`);
  return ms;
}

// ---------------------------------------------------------------------------
// Companion process.
// ---------------------------------------------------------------------------

const LIVE_COMPANIONS = new Set();
let exitHookInstalled = false;

function killGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
    return true;
  } catch (error) {
    // ESRCH: gone. EPERM on macOS: the group only has a zombie left.
    if (error.code === "ESRCH" || error.code === "EPERM") return false;
    throw error;
  }
}

/** Kills every companion this process still owns. Synchronous, for exit paths. */
export function reapAllCompanionsSync() {
  for (const companion of LIVE_COMPANIONS) companion.killSync();
}

function installExitHook() {
  if (exitHookInstalled) return;
  exitHookInstalled = true;
  process.once("exit", reapAllCompanionsSync);
}

/** A free TCP port on loopback (the companion itself still binds every interface). */
export function freeLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = netCreateServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const UDID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;

// idb_companion needs the host's tool paths, home, temporary directory and locale,
// not the coding agent's API keys or dynamic-loader overrides. Keep this an exact
// allowlist: accepting a whole prefix (including LC_*) could carry unrelated secrets.
const COMPANION_ENV_KEYS = Object.freeze([
  "PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "DEVELOPER_DIR",
]);

/** A fresh environment for the local companion, including when the caller supplies one. */
export function companionEnvironment(source = processEnv) {
  return Object.fromEntries(COMPANION_ENV_KEYS
    .filter((key) => typeof source[key] === "string")
    .map((key) => [key, source[key]]));
}

/**
 * Owns one `idb_companion --udid <udid>` process. Emits `log` (line) and `exit`
 * ({ code, signal, expected }) events.
 */
export class IdbCompanion extends EventEmitter {
  #options;
  #child = null;
  #exited = null; // { promise, done, code, signal, reaped, expected }, per spawned child
  #client = null;
  #dir = null;
  #endpoint = null;
  #logTail = [];
  #logStream = null;
  #starting = null;
  #stopping = null;

  /**
   * @param {object} options
   * @param {string} options.udid Simulator UDID.
   * @param {string} [options.binary] idb_companion path (default from PATH).
   * @param {"socket"|"tcp"} [options.transport] Unix socket (default) or TCP port.
   * @param {number} [options.port] TCP port; a free one is picked when omitted.
   * @param {string} [options.socketDir] Parent for the private socket directory.
   * @param {string} [options.only] `--only` filter (default "simulator"; null omits).
   * @param {string} [options.logPath] File to append the companion's output to.
   * @param {number} [options.readyTimeoutMs] @param {number} [options.stopTimeoutMs]
   * @param {Function} [options.spawn] child_process.spawn replacement for tests.
   */
  constructor(options) {
    super();
    if (!options || !UDID_PATTERN.test(options.udid ?? "")) throw new TypeError("A valid simulator UDID is required.");
    const transport = options.transport ?? "socket";
    if (transport !== "socket" && transport !== "tcp") throw new RangeError(`Unknown transport ${transport}.`);
    this.#options = {
      binary: "idb_companion",
      only: "simulator",
      readyTimeoutMs: READY_TIMEOUT_MS,
      stopTimeoutMs: STOP_TIMEOUT_MS,
      spawn: nodeSpawn,
      env: companionEnvironment(),
      ...options,
      transport,
    };
  }

  get udid() {
    return this.#options.udid;
  }

  get pid() {
    return this.#child?.pid ?? null;
  }

  get running() {
    return Boolean(this.#child) && !this.#exited.done;
  }

  get endpoint() {
    return this.#endpoint ? { ...this.#endpoint } : null;
  }

  get client() {
    return this.#client;
  }

  logTail() {
    return [...this.#logTail];
  }

  #log(line) {
    this.#logTail.push(line);
    if (this.#logTail.length > LOG_TAIL_LINES) this.#logTail.shift();
    this.#logStream?.write(`${line}\n`);
    this.emit("log", line);
  }

  /** Spawns the companion and resolves with its client once it is listening. */
  async start() {
    if (this.#stopping) await this.#stopping;
    if (this.#starting) return this.#starting;
    if (this.running && this.#client) return this.#client;
    this.#starting = this.#start().finally(() => {
      this.#starting = null;
    });
    return this.#starting;
  }

  async #start() {
    const o = this.#options;
    const args = ["--udid", o.udid];
    if (o.transport === "socket") {
      this.#dir = mkdtempSync(join(o.socketDir ?? tmpdir(), "autonom-idb-"));
      const socketPath = join(this.#dir, SOCKET_NAME);
      if (Buffer.byteLength(socketPath) > MAX_SOCKET_PATH_BYTES) {
        this.#removeDir();
        throw new RangeError(`Socket path is longer than ${MAX_SOCKET_PATH_BYTES} bytes: ${socketPath}`);
      }
      args.push("--grpc-domain-sock", socketPath);
      this.#endpoint = { socketPath };
    } else {
      const port = o.port ?? (await freeLoopbackPort());
      args.push("--grpc-port", String(port));
      this.#endpoint = { host: "127.0.0.1", port };
    }
    if (o.only) args.push("--only", o.only);
    if (o.logPath && !this.#logStream) {
      this.#logStream = createWriteStream(o.logPath, { flags: "a" });
      this.#logStream.on("error", () => {});
    }
    const child = o.spawn(o.binary, args, {
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: companionEnvironment(o.env),
    });
    this.#child = child;
    let markExited;
    const exited = new Promise((resolve) => {
      markExited = resolve;
    });
    // `reaped`: the final group SIGKILL went out. The group id must never be
    // signalled after that, because the OS may hand it to an unrelated process.
    // `expected`: this child was asked to go (stop, restart, killSync). Kept per
    // child so a late exit of an old child is never mistaken for the current one.
    this.#exited = { promise: exited, done: false, code: null, signal: null, reaped: false, expected: false };
    const exitState = this.#exited;
    LIVE_COMPANIONS.add(this);
    installExitHook();

    let readyResolve;
    let readyReject;
    const ready = new Promise((resolve, reject) => {
      readyResolve = resolve;
      readyReject = reject;
    });
    const lines = (stream, onLine) => {
      let rest = "";
      stream.setEncoding("utf8");
      stream.on("data", (chunk) => {
        rest += chunk;
        let index;
        while ((index = rest.indexOf("\n")) >= 0) {
          onLine(rest.slice(0, index).replace(/\r$/, ""));
          rest = rest.slice(index + 1);
        }
        if (rest.length > 64 * 1024) {
          onLine(rest);
          rest = "";
        }
      });
      stream.on("end", () => {
        if (rest) onLine(rest);
      });
    };
    lines(child.stdout, (line) => {
      this.#log(line);
      const text = line.trim();
      if (!text.startsWith("{")) return;
      try {
        const info = JSON.parse(text);
        if (info.grpc_path || info.grpc_port || info.grpc_swift_port) readyResolve(info);
      } catch {
        // Not the readiness line.
      }
    });
    lines(child.stderr, (line) => this.#log(line));
    child.once("error", (error) => {
      const wrapped = error.code === "ENOENT" ? new Error(IDB_INSTALL_HINT, { cause: error }) : error;
      readyReject(wrapped);
      if (!exitState.done) {
        exitState.done = true;
        if (this.#child === child) LIVE_COMPANIONS.delete(this);
        markExited();
      }
    });
    child.once("exit", (code, signal) => {
      exitState.done = true;
      exitState.code = code;
      exitState.signal = signal;
      markExited();
      const expected = exitState.expected;
      const tail = this.#logTail.slice(-5).join(" | ");
      readyReject(new Error(`idb_companion exited before it was ready (code ${code}, signal ${signal}). ${tail}`));
      // A child that was already replaced (killSync, then restart before its exit
      // arrived) must not touch the bookkeeping or events of the new companion.
      if (this.#child !== child) return;
      LIVE_COMPANIONS.delete(this);
      if (!expected) {
        // Helpers left in its process group go too; the group id cannot be reused
        // while any member is alive.
        if (child.pid && !exitState.reaped) killGroup(child.pid, "SIGKILL");
        exitState.reaped = true;
        this.#client?.close({ timeoutMs: 100 }).catch(() => {});
        this.#removeDir();
      }
      this.emit("exit", { code, signal, expected });
    });

    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const tail = this.#logTail.slice(-5).join(" | ");
        reject(new Error(`idb_companion was not ready within ${o.readyTimeoutMs} ms. ${tail}`));
      }, o.readyTimeoutMs);
      timer.unref?.();
    });
    try {
      await Promise.race([ready, timeout]);
    } catch (error) {
      clearTimeout(timer);
      await this.#terminate();
      throw error;
    }
    clearTimeout(timer);
    this.#client = new IdbCompanionClient(this.#endpoint);
    return this.#client;
  }

  async #terminate() {
    const child = this.#child;
    const exitState = this.#exited;
    if (!child) return;
    exitState.expected = true;
    if (!exitState.done && child.pid) {
      if (!exitState.reaped) {
        killGroup(child.pid, "SIGTERM");
        const { timedOut } = await withTimeout(exitState.promise, this.#options.stopTimeoutMs);
        if (timedOut) killGroup(child.pid, "SIGKILL");
      }
      // Also after killSync(): its SIGKILL is out, but the exit must land before a
      // restart() spawns the next companion.
      if (!exitState.done) await withTimeout(exitState.promise, KILL_WAIT_MS);
    }
    // The companion is gone; anything left in its group (helpers) goes too. Only
    // once: a later stop() or restart() must not signal a group id that is free.
    if (child.pid && !exitState.reaped) killGroup(child.pid, "SIGKILL");
    exitState.reaped = true;
    LIVE_COMPANIONS.delete(this);
    this.#removeDir();
  }

  #removeDir() {
    if (!this.#dir) return;
    rmSync(this.#dir, { recursive: true, force: true });
    this.#dir = null;
  }

  /**
   * Stops every stream (Stop is sent), closes the client, then SIGTERM to the process
   * group and SIGKILL after `stopTimeoutMs`. Returns how the process ended.
   */
  async stop() {
    if (this.#starting) await this.#starting.catch(() => {});
    if (this.#stopping) return this.#stopping;
    this.#stopping = (async () => {
      const client = this.#client;
      this.#client = null;
      if (client) await client.close({ timeoutMs: STREAM_STOP_TIMEOUT_MS }).catch(() => {});
      await this.#terminate();
      const state = this.#exited;
      this.#logStream?.end();
      this.#logStream = null;
      return { code: state?.code ?? null, signal: state?.signal ?? null, exited: state?.done ?? true };
    })().finally(() => {
      this.#stopping = null;
    });
    return this.#stopping;
  }

  /** Stop, then start a fresh companion; resolves with the new client. */
  async restart() {
    await this.stop();
    return this.start();
  }

  /** Synchronous SIGKILL of the process group, for process exit. */
  killSync() {
    const child = this.#child;
    if (child?.pid && !this.#exited?.done && !this.#exited?.reaped) {
      this.#exited.expected = true;
      killGroup(child.pid, "SIGKILL");
      this.#exited.reaped = true;
    }
    LIVE_COMPANIONS.delete(this);
    try {
      this.#removeDir();
    } catch {
      // Best effort on exit.
    }
  }
}
