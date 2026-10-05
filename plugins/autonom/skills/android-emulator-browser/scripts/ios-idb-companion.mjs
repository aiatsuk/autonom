/**
 * One idb_companion per iOS Simulator, owned by the Canvas, and a small gRPC client
 * for it over node:http2 with a hand-written protobuf codec for exactly the messages
 * the Canvas needs (idb.proto, package `idb`, service `CompanionService`):
 *
 * - `video_stream`: VideoStreamRequest Start/Stop in, VideoStreamResponse payload out.
 *   One gRPC message carries one H.264 access unit; a keyframe comes with its SPS and
 *   PPS. The companion keeps encoding when a client goes away without an explicit
 *   Stop, so every close path here sends Stop while the call can still carry it, and a
 *   stream that ended without one reports `abnormal` so the owner restarts the
 *   companion.
 * - `hid`: one long client stream of HIDEvent presses (touch down/up at logical
 *   points, HOME/LOCK buttons, keyboard usage codes).
 * - `describe` and `get_orientation`: unary queries.
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

/** Validated Start settings with defaults applied. */
export function normalizeVideoOptions(options = {}) {
  const merged = { ...DEFAULT_VIDEO_OPTIONS, ...options };
  const out = { format: formatCode(merged.format) };
  if (merged.fps !== undefined && merged.fps !== null && merged.fps !== 0) {
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
 * HIDEvent { press: HIDPress { action: HIDPressAction { touch | button | key }, direction } }.
 * `event` is `{ touch: { x, y } }`, `{ button: "HOME" }` or `{ key: keycode }`, plus
 * `direction` ("down" or "up"). Touch points are logical points in the current
 * orientation.
 */
export function encodeHidEvent(event) {
  if (!event || typeof event !== "object") throw new TypeError("HID event must be an object.");
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

/** Inverse of encodeHidEvent for press events; null for any other HID event. */
export function decodeHidEvent(buf) {
  const press = fieldBytes(lastFields(buf), 1);
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
  #hid = null;

  constructor(endpoint) {
    if (!endpoint || (!endpoint.socketPath && !endpoint.port)) {
      throw new TypeError("Companion endpoint needs socketPath or port.");
    }
    this.#endpoint = { host: "127.0.0.1", ...endpoint };
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
      const timer = setTimeout(() => finish(new GrpcError(4, "Deadline exceeded.", { method })), timeoutMs);
      timer.unref?.();
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

  /** Starts a video stream; iterate it for frames and always `stop()` it. */
  async openVideoStream(options = {}, { signal } = {}) {
    this.#assertOpen();
    const stream = new IdbVideoStream(this, options, { signal });
    this.#videoStreams.add(stream);
    stream.closed.finally(() => this.#videoStreams.delete(stream));
    await stream.start();
    return stream;
  }

  /** The one HID stream of this client, opened on first send. */
  hid() {
    this.#assertOpen();
    this.#hid ??= new IdbHidStream(this);
    return this.#hid;
  }

  /** Stops every video stream (sending Stop), ends the HID stream and closes the session. */
  async close(options) {
    if (this.#closed) return;
    // Refuse new calls from the start, so a stream opened while the open ones are
    // being stopped cannot slip past Stop and be cut off with the session.
    this.#closing ??= this.#close(options);
    return this.#closing;
  }

  async #close({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    const pending = [...this.#videoStreams].map((s) => s.stop({ timeoutMs }));
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
 * Stop reaching the companion, which leaves its encoder running: restart the
 * companion then.
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
      const { stopSent } = await this.#stopCall(timeoutMs);
      this.#finish(null, { abnormal: !stopSent });
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
   */
  async restart({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    if (this.#done || this.#stopping || this.#restarting) throw new Error("Video stream is closed or restarting.");
    // Events of the old call are stale from here on.
    this.#generation += 1;
    this.#restarting = (async () => {
      const { stopSent, acknowledged } = await this.#stopCall(timeoutMs);
      if (!stopSent || !acknowledged) {
        throw new Error(`Stop was not ${stopSent ? "answered" : "written"} on the old video call; ` +
          "its encoder may still run, so the companion must be restarted.");
      }
      this.#paused = false;
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

/**
 * The `hid` client stream. Events go out on one open call; after the call ends or
 * fails, the next event opens a new one. Touch points are logical points.
 */
export class IdbHidStream {
  #client;
  #call = null;
  #closed = false;
  lastError = null;
  sent = 0;

  constructor(client) {
    this.#client = client;
  }

  #open() {
    const call = this.#client.openCall("hid");
    call.on("error", (error) => {
      this.lastError = error;
      if (this.#call === call) this.#call = null;
    });
    call.on("trailers", (trailers) => {
      const status = grpcStatusFrom(trailers);
      if (status && status.code !== 0) this.lastError = new GrpcError(status.code, status.details, { method: "hid" });
    });
    call.on("close", () => {
      if (this.#call === call) this.#call = null;
    });
    call.on("end", () => {
      // The companion finished the call; the next event opens a new one.
      if (this.#call === call) this.#call = null;
      if (!call.destroyed && call.writable) call.end();
    });
    // HIDResponse is empty; drain it so the call can close.
    call.on("data", () => {});
    this.#call = call;
    return call;
  }

  /** Sends one HID event (see encodeHidEvent); resolves once it is written. */
  send(event) {
    if (this.#closed) return Promise.reject(new Error("HID stream is closed."));
    let frame;
    let call;
    try {
      frame = grpcFrame(encodeHidEvent(event));
      call = this.#call;
      if (!call || call.destroyed || !call.writable) call = this.#open();
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      call.write(frame, (error) => {
        if (error) {
          this.lastError = error;
          if (this.#call === call) this.#call = null;
          reject(error);
        } else {
          this.sent += 1;
          resolve();
        }
      });
    });
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

  keyUp(keycode) {
    return this.send({ key: keycode, direction: "up" });
  }

  async close({ timeoutMs = STREAM_STOP_TIMEOUT_MS } = {}) {
    this.#closed = true;
    const call = this.#call;
    this.#call = null;
    if (!call || call.destroyed) return;
    const ended = new Promise((resolve) => call.once("close", resolve));
    if (call.writable) call.end();
    const { timedOut } = await withTimeout(ended, timeoutMs);
    if (timedOut && !call.destroyed) call.close(http2.constants.NGHTTP2_CANCEL);
  }
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
      env: process.env,
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
      env: o.env,
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
