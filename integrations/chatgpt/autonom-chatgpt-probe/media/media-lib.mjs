/**
 * Pure helpers of the Autonom ChatGPT probe media endpoint: argument parsing, the security
 * gate (Host, Origin, token), CORS, query and message validation, frame headers and rate
 * limits. Nothing here opens sockets or reads the environment, so all of it is unit-tested.
 */
import { createHash, timingSafeEqual } from "node:crypto";

export const PROBE_VERSION = "0.1.0";
export const MEDIA_SCHEMA = "autonom.chatgpt-probe.media/v1";
export const SUBPROTOCOL = "autonom-probe.v1";
export const BEARER_PROTOCOL_PREFIX = "bearer.";
export const FRAME_MAGIC = 0xa7;
export const FRAME_HEADER_BYTES = 16;
export const FLAG_KEY = 0x01;
export const FLAG_CONFIG = 0x02;
export const MAX_SOCKETS = 8;
export const MAX_MESSAGE_BYTES = 65536;
export const MAX_REFUSED_ORIGINS = 20;
export const ORIGIN_RECORD_CHARS = 200;
export const RATE_LIMIT_PER_SECOND = 200;
export const BACKPRESSURE_HIGH_BYTES = 4 * 1024 * 1024;
export const BACKPRESSURE_LOW_BYTES = 1024 * 1024;
export const DEFAULT_FPS = 30;
export const ECHO_PAYLOAD_MAX = 256;
export const DROP_CLOSE_CODE = 4001;
export const DROP_CLOSE_REASON = "probe drop";
export const FORBIDDEN_DEVICE = "android:emulator-5554";
export const INTERNAL_ROUTES = Object.freeze(["/probe/status", "/probe/frames"]);
export const PROBE_ROUTES = Object.freeze(["/probe/ping", "/probe/status", "/probe/frames", "/probe/ws"]);

const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,}$/;
const NONCE_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const ANDROID_SPEC = /^android:([A-Za-z0-9._:-]{1,128})$/;
const IOS_SPEC = /^ios:([0-9A-Fa-f-]{36})$/;
const HOST_PART = "(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*|\\[[0-9a-f:.]+\\])";
const ORIGIN_PATTERN = new RegExp(`^(https?)://(${HOST_PART})(?::([0-9]{1,5}))?$`);
const WILDCARD_PATTERN = /^https:\/\/\*\.([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)(?::([0-9]{1,5}))?$/;
const LABEL_PATTERN = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
const HOST_PORT_PATTERN = new RegExp(`^${HOST_PART}:[0-9]{1,5}$`);

/** An error carrying an Autonom-style lowercase error code. */
export class ProbeError extends Error {
  constructor(code, message, extras = {}) {
    super(message);
    this.code = code;
    this.extras = extras;
  }
}

function validPort(text) {
  if (text === undefined) return true;
  const port = Number(text);
  return Number.isInteger(port) && port >= 1 && port <= 65535;
}

/**
 * Parse one Origin header value. Returns the lowercase origin (`scheme://host[:port]`) or null
 * for `null`, `file://`, paths, userinfo, other schemes or anything malformed.
 */
export function normalizeOrigin(value) {
  if (typeof value !== "string") return null;
  const text = value.trim().toLowerCase();
  const match = ORIGIN_PATTERN.exec(text);
  if (!match || !validPort(match[3])) return null;
  return text;
}

/** Parse one `--allow-origin` value: an exact origin or a one-label `https://*.suffix` wildcard. */
export function parseAllowOrigin(value) {
  const text = String(value ?? "").trim().toLowerCase();
  const wildcard = WILDCARD_PATTERN.exec(text);
  if (wildcard && validPort(wildcard[2])) {
    return { kind: "wildcard", value: text, suffix: wildcard[1], port: wildcard[2] ?? null };
  }
  const exact = normalizeOrigin(text);
  if (exact) return { kind: "exact", value: exact };
  throw new ProbeError("invalid_arguments", `Not an allowed origin form: ${String(value).slice(0, 80)}`, {
    field: "--allow-origin",
    problem: "use scheme://host[:port] or https://*.suffix",
  });
}

/** True when `origin` (raw header value) matches an allowlist entry from parseAllowOrigin. */
export function originAllowed(origin, allowlist) {
  const normalized = normalizeOrigin(origin);
  if (!normalized) return false;
  const match = ORIGIN_PATTERN.exec(normalized);
  const [, scheme, host, port] = match;
  for (const entry of allowlist) {
    if (entry.kind === "exact" && entry.value === normalized) return true;
    if (entry.kind === "wildcard" && scheme === "https" && (port ?? null) === entry.port) {
      const tail = `.${entry.suffix}`;
      if (host.endsWith(tail) && LABEL_PATTERN.test(host.slice(0, -tail.length))) return true;
    }
  }
  return false;
}

/** Parse one `--allow-host` value (`host:port`, lowercase). */
export function parseAllowHost(value) {
  const text = String(value ?? "").trim().toLowerCase();
  if (!HOST_PORT_PATTERN.test(text) || !validPort(text.slice(text.lastIndexOf(":") + 1))) {
    throw new ProbeError("invalid_arguments", `Not a host:port value: ${String(value).slice(0, 80)}`, {
      field: "--allow-host",
      problem: "use host:port",
    });
  }
  return text;
}

/** Host header check against loopback names on the listening port plus `--allow-host` values. */
export function hostAllowed(hostHeader, port, allowHosts = []) {
  if (typeof hostHeader !== "string" || !hostHeader) return false;
  const host = hostHeader.trim().toLowerCase();
  if (host === `127.0.0.1:${port}` || host === `localhost:${port}`) return true;
  return allowHosts.includes(host);
}

/** Parse a device spec (`android:<serial>` or `ios:<udid>`); null when malformed. */
export function parseDeviceSpec(spec) {
  if (typeof spec !== "string") return null;
  let match = ANDROID_SPEC.exec(spec);
  if (match) return { kind: "android", id: spec, serial: match[1] };
  match = IOS_SPEC.exec(spec);
  if (match) return { kind: "ios", id: spec, udid: match[1] };
  return null;
}

/** Parse a `source` query value: `fixture` or a device spec; null when malformed. */
export function parseSource(value) {
  if (value === "fixture") return { kind: "fixture", id: "fixture" };
  return parseDeviceSpec(value);
}

/** Compare two secrets in constant time (SHA-256 digests are always equal length). */
export function tokensEqual(given, expected) {
  if (typeof given !== "string" || typeof expected !== "string" || !expected) return false;
  const a = createHash("sha256").update(given, "utf8").digest();
  const b = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(a, b) && given.length === expected.length;
}

export function validToken(token) {
  return typeof token === "string" && TOKEN_PATTERN.test(token);
}

/** Token from `Authorization: Bearer <token>`, or null. */
export function bearerFromAuthorization(header) {
  if (typeof header !== "string") return null;
  const match = /^Bearer[ ]+(\S+)\s*$/i.exec(header.trim());
  return match ? match[1] : null;
}

/** Offered WebSocket subprotocols: whether `autonom-probe.v1` is offered and the bearer token. */
export function protocolsFromHeader(header) {
  const offered = String(header ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const bearer = offered.find((item) => item.startsWith(BEARER_PROTOCOL_PREFIX));
  return {
    offersProbe: offered.includes(SUBPROTOCOL),
    token: bearer ? bearer.slice(BEARER_PROTOCOL_PREFIX.length) : null,
  };
}

/** Bounded record of refused origins: max 20 distinct, each with `count` and `last_at`. */
export function createRefusedOriginLog({ max = MAX_REFUSED_ORIGINS, now = () => new Date() } = {}) {
  const entries = new Map();
  return {
    record(origin) {
      const key = String(origin ?? "").slice(0, ORIGIN_RECORD_CHARS);
      const at = now().toISOString().replace(/\.\d{3}Z$/, "Z");
      const entry = entries.get(key);
      if (entry) {
        entry.count += 1;
        entry.last_at = at;
      } else if (entries.size < max) {
        entries.set(key, { origin: key, count: 1, last_at: at });
      }
    },
    list() {
      return [...entries.values()].map((entry) => ({ ...entry }));
    },
  };
}

export const ERROR_TEXT = Object.freeze({
  host_not_allowed: "The Host header is not a loopback name on this port or an allowed host.",
  origin_not_allowed: "The Origin is not in the probe allowlist.",
  origin_required: "This route needs an Origin header.",
  unauthorized: "Missing or wrong probe token.",
  preflight_not_allowed: "The preflight asks for a method or header the probe does not allow.",
  not_found: "Unknown probe route.",
  method_not_allowed: "Only GET and OPTIONS are allowed.",
  bad_nonce: "nonce must be 1-64 characters of A-Z, a-z, 0-9, _ or -.",
  bad_query: "count must be an integer 1-30 and after_seq an integer >= -1.",
  bad_source: "source must be fixture, android:<serial> or ios:<udid>.",
  device_not_allowed: "The device is not in the probe allowlist.",
  too_many_sockets: "The probe already has 8 open sockets.",
  relay_unavailable: "The device relay is not installed in this probe.",
  upgrade_required: "This route is a WebSocket; send an Upgrade request.",
  bad_message: "The message is not a valid probe client message.",
  rate_limited: "More than 200 messages per second; extra messages are dropped.",
  not_supported: "The message is not supported for this source.",
  source_failed: "The video source failed.",
  server_stopping: "The probe media endpoint is stopping.",
  internal_error: "The probe media endpoint hit an internal error.",
});

export const ERROR_HINT = Object.freeze({
  host_not_allowed: "Connect to 127.0.0.1 or localhost on the probe port, or add the host with --allow-host.",
  origin_not_allowed: "Add the origin to allow_origins in the probe config and restart the probe.",
  origin_required: "Call this route from the probe page.",
  unauthorized: "Use the media token from open_workspace.",
  device_not_allowed: "Add the device with install_probe.py --allow-device.",
  relay_unavailable: "Install the device relay (media/device-source.mjs).",
});

/** The JSON body of a refusal: `{ok:false, error_code, error[, hint]}`. Never holds a token. */
export function errorBody(code, extras = {}) {
  const body = { ok: false, error_code: code, error: ERROR_TEXT[code] ?? code };
  if (ERROR_HINT[code]) body.hint = ERROR_HINT[code];
  return { ...body, ...extras };
}

/** Headers of every JSON response; CORS headers only for an allowed origin. */
export function jsonHeaders(allowedOrigin = null, extra = {}) {
  const headers = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  };
  if (allowedOrigin) {
    headers["Access-Control-Allow-Origin"] = allowedOrigin;
    headers.Vary = "Origin";
  }
  return { ...headers, ...extra };
}

function isProbePath(path) {
  return path === "/probe" || path.startsWith("/probe/");
}

/**
 * The security gate (contract 3.2) for one request or upgrade. Returns
 * `{ok:true, origin}` (origin = allowed origin or null) or
 * `{ok:false, status, code, origin, cors}` where `cors` is the origin to echo, if any.
 */
export function gateRequest({ method, path, headers, upgrade = false, port, token, allowHosts = [], allowOrigins = [], refused = null }) {
  const host = headers.host;
  if (!hostAllowed(host, port, allowHosts)) {
    return { ok: false, status: upgrade ? 403 : 421, code: "host_not_allowed", cors: null };
  }
  const rawOrigin = headers.origin;
  let origin = null;
  if (rawOrigin !== undefined) {
    if (!originAllowed(rawOrigin, allowOrigins)) {
      refused?.record(rawOrigin);
      return { ok: false, status: 403, code: "origin_not_allowed", cors: null };
    }
    origin = normalizeOrigin(rawOrigin);
  } else if (upgrade || method !== "GET" || !INTERNAL_ROUTES.includes(path)) {
    return { ok: false, status: 403, code: "origin_required", cors: null };
  }
  if (method === "OPTIONS" && !upgrade) {
    if (!isProbePath(path)) return { ok: false, status: 404, code: "not_found", cors: origin };
    return preflight(headers, origin);
  }
  const given = upgrade ? protocolsFromHeader(headers["sec-websocket-protocol"]) : null;
  const presented = upgrade ? (given.offersProbe ? given.token : null) : bearerFromAuthorization(headers.authorization);
  if (!tokensEqual(presented ?? "", token)) {
    return { ok: false, status: 401, code: "unauthorized", cors: origin };
  }
  return { ok: true, origin };
}

function preflight(headers, origin) {
  const method = String(headers["access-control-request-method"] ?? "");
  const requested = String(headers["access-control-request-headers"] ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  if (method !== "GET" || requested.some((name) => name !== "authorization")) {
    return { ok: false, status: 403, code: "preflight_not_allowed", cors: null };
  }
  const answer = {
    "Access-Control-Allow-Origin": origin,
    Vary: "Origin",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Max-Age": "60",
  };
  if (String(headers["access-control-request-private-network"] ?? "").toLowerCase() === "true") {
    answer["Access-Control-Allow-Private-Network"] = "true";
  }
  return { ok: true, preflight: true, origin, headers: answer };
}

export function validNonce(value) {
  return typeof value === "string" && NONCE_PATTERN.test(value);
}

function strictInteger(text) {
  if (typeof text !== "string" || !/^-?[0-9]{1,10}$/.test(text)) return null;
  return Number(text);
}

/** `count` (1-30) and `after_seq` (>= -1) of `/probe/frames`, or null when invalid. */
export function parseFramesQuery(params) {
  const count = strictInteger(params.get("count") ?? "10");
  const afterSeq = strictInteger(params.get("after_seq") ?? "-1");
  if (count === null || count < 1 || count > 30) return null;
  if (afterSeq === null || afterSeq < -1 || afterSeq > 0xffffffff - 31) return null;
  return { count, afterSeq };
}

/** 16-byte binary frame header (contract 3.4). */
export function encodeFrameHeader({ key, config, seq, tServerMs }) {
  const header = Buffer.alloc(FRAME_HEADER_BYTES);
  header[0] = FRAME_MAGIC;
  header[1] = (key ? FLAG_KEY : 0) | (config ? FLAG_CONFIG : 0);
  header.writeUInt32BE(seq >>> 0, 4);
  header.writeDoubleBE(tServerMs, 8);
  return header;
}

export function parseFrameHeader(buffer) {
  const data = Buffer.from(buffer);
  if (data.length < FRAME_HEADER_BYTES || data[0] !== FRAME_MAGIC) throw new RangeError("bad frame header");
  return {
    key: Boolean(data[1] & FLAG_KEY),
    config: Boolean(data[1] & FLAG_CONFIG),
    seq: data.readUInt32BE(4),
    tServerMs: data.readDoubleBE(8),
    payload: data.subarray(FRAME_HEADER_BYTES),
  };
}

function isUnit(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

/** Validate one client text message. Returns `{ok:true, message}` or `{ok:false}`. */
export function parseClientMessage(text) {
  let message;
  try {
    message = JSON.parse(text);
  } catch {
    return { ok: false };
  }
  if (!message || typeof message !== "object" || Array.isArray(message) || typeof message.type !== "string") return { ok: false };
  switch (message.type) {
    case "start":
      if (!Number.isInteger(message.fps) || message.fps < 1 || message.fps > 60) return { ok: false };
      return { ok: true, message: { type: "start", fps: message.fps } };
    case "stop":
    case "drop":
      return { ok: true, message: { type: message.type } };
    case "echo":
      if (typeof message.payload !== "string" || message.payload.length > ECHO_PAYLOAD_MAX) return { ok: false };
      return { ok: true, message: { type: "echo", payload: message.payload } };
    case "ping":
      if (!Number.isInteger(message.seq) || typeof message.t_client !== "number" || !Number.isFinite(message.t_client)) return { ok: false };
      return { ok: true, message: { type: "ping", seq: message.seq, t_client: message.t_client } };
    case "tap":
      if (!Number.isInteger(message.seq) || !isUnit(message.x) || !isUnit(message.y)) return { ok: false };
      return { ok: true, message: { type: "tap", seq: message.seq, x: message.x, y: message.y } };
    default:
      return { ok: false };
  }
}

/**
 * Per-socket limiter: at most `limit` messages per one-second window. `check()` answers
 * `"ok"`, `"notify"` (drop and send one rate_limited this window) or `"drop"`.
 */
export function createRateLimiter({ limit = RATE_LIMIT_PER_SECOND, windowMs = 1000, now = () => Date.now() } = {}) {
  let windowStart = -Infinity;
  let count = 0;
  let notified = false;
  return {
    check() {
      const t = now();
      if (t - windowStart >= windowMs) {
        windowStart = t;
        count = 0;
        notified = false;
      }
      count += 1;
      if (count <= limit) return "ok";
      if (notified) return "drop";
      notified = true;
      return "notify";
    },
  };
}

/** Backpressure state: skip frames above 4 MiB buffered until below 1 MiB, then wait for a key frame. */
export function createBackpressure({ high = BACKPRESSURE_HIGH_BYTES, low = BACKPRESSURE_LOW_BYTES } = {}) {
  let congested = false;
  let needKey = false;
  return {
    /** True when the frame may be sent now. */
    admit(buffered, key) {
      if (congested && buffered < low) congested = false;
      if (!congested && buffered > high) congested = true;
      if (congested) {
        needKey = true;
        return false;
      }
      if (needKey && !key) return false;
      needKey = false;
      return true;
    },
    get congested() {
      return congested;
    },
  };
}

const ARG_SPEC = Object.freeze({
  "--port": "single",
  "--autonom-root": "single",
  "--adb": "single",
  "--fps": "single",
  "--allow-origin": "multi",
  "--allow-host": "multi",
  "--allow-device": "multi",
  "--self-test": "flag",
});

/** Parse the CLI argument list (without node and the script). Throws ProbeError(invalid_arguments). */
export function parseArgs(argv) {
  const out = { port: null, autonomRoot: null, adbPath: null, fps: DEFAULT_FPS, allowOrigins: [], allowHosts: [], allowDevices: [], selfTest: false };
  const bad = (field, problem) => new ProbeError("invalid_arguments", `Invalid ${field}: ${problem}`, { field, problem });
  for (let i = 0; i < argv.length; i += 1) {
    const name = argv[i];
    const kind = ARG_SPEC[name];
    if (!kind) throw bad(String(name).slice(0, 40), "unknown argument");
    if (kind === "flag") {
      out.selfTest = true;
      continue;
    }
    const value = argv[i + 1];
    if (value === undefined) throw bad(name, "missing value");
    i += 1;
    switch (name) {
      case "--port": {
        const port = strictInteger(value);
        if (port === null || port < 0 || port > 65535) throw bad(name, "must be an integer 0-65535");
        out.port = port;
        break;
      }
      case "--fps": {
        const fps = strictInteger(value);
        if (fps === null || fps < 1 || fps > 60) throw bad(name, "must be an integer 1-60");
        out.fps = fps;
        break;
      }
      case "--autonom-root":
        out.autonomRoot = value;
        break;
      case "--adb":
        out.adbPath = value;
        break;
      case "--allow-origin":
        out.allowOrigins.push(parseAllowOrigin(value).value);
        break;
      case "--allow-host":
        out.allowHosts.push(parseAllowHost(value));
        break;
      case "--allow-device":
        if (!parseDeviceSpec(value)) throw bad(name, "must be android:<serial> or ios:<udid>");
        if (value === FORBIDDEN_DEVICE) throw new ProbeError("device_not_allowed", "android:emulator-5554 is never allowed", { field: name });
        out.allowDevices.push(value);
        break;
      default:
        break;
    }
  }
  if (!out.selfTest) {
    if (out.port === null) throw bad("--port", "required");
    if (!out.autonomRoot) throw bad("--autonom-root", "required");
  }
  return out;
}

/** Whether a device source may be opened: in the allowlist and never emulator-5554. */
export function deviceAllowed(spec, allowDevices) {
  return spec !== FORBIDDEN_DEVICE && allowDevices.includes(spec);
}

/** Pure-helper assertions for `--self-test`. Returns the list of failed check names. */
export function selfTest() {
  const failures = [];
  const check = (name, condition) => {
    if (!condition) failures.push(name);
  };
  const allow = [parseAllowOrigin("https://web-sandbox.oaiusercontent.com"), parseAllowOrigin("https://*.sandbox.example.com")];
  check("origin exact", originAllowed("https://web-sandbox.oaiusercontent.com", allow));
  check("origin case", originAllowed("HTTPS://Web-Sandbox.OaiUserContent.com", allow));
  check("origin port differs", !originAllowed("https://web-sandbox.oaiusercontent.com:8443", allow));
  check("origin wildcard one label", originAllowed("https://abc.sandbox.example.com", allow));
  check("origin wildcard two labels", !originAllowed("https://a.b.sandbox.example.com", allow));
  check("origin wildcard bare suffix", !originAllowed("https://sandbox.example.com", allow));
  check("origin wildcard http", !originAllowed("http://abc.sandbox.example.com", allow));
  check("origin null", !originAllowed("null", allow));
  check("origin file", !originAllowed("file://", allow));
  check("origin path", !originAllowed("https://web-sandbox.oaiusercontent.com/x", allow));
  check("host loopback", hostAllowed("127.0.0.1:47811", 47811) && hostAllowed("LOCALHOST:47811", 47811));
  check("host rebinding", !hostAllowed("attacker.example:47811", 47811));
  check("host allow", hostAllowed("127.0.0.1:47812", 47811, ["127.0.0.1:47812"]));
  check("token equal", tokensEqual("a".repeat(32), "a".repeat(32)));
  check("token differs", !tokensEqual("a".repeat(32), "b".repeat(32)) && !tokensEqual("a", "a".repeat(32)));
  check("bearer", bearerFromAuthorization("Bearer abc") === "abc" && bearerFromAuthorization("Basic abc") === null);
  const protocols = protocolsFromHeader("autonom-probe.v1, bearer.xyz");
  check("subprotocol", protocols.offersProbe && protocols.token === "xyz");
  check("device spec", parseDeviceSpec("android:emulator-5584")?.kind === "android" && parseDeviceSpec("ios:bad") === null);
  check("device forbidden", !deviceAllowed(FORBIDDEN_DEVICE, [FORBIDDEN_DEVICE]));
  const header = parseFrameHeader(encodeFrameHeader({ key: true, config: true, seq: 7, tServerMs: 12.5 }));
  check("frame header", header.key && header.config && header.seq === 7 && header.tServerMs === 12.5);
  check("message echo", parseClientMessage('{"type":"echo","payload":"x"}').ok && !parseClientMessage("[1]").ok);
  check("message tap", !parseClientMessage('{"type":"tap","seq":1,"x":2,"y":0}').ok);
  check("nonce", validNonce("abc_-1") && !validNonce("") && !validNonce("a b"));
  check("frames query", parseFramesQuery(new URLSearchParams("count=31")) === null && parseFramesQuery(new URLSearchParams("count=3&after_seq=-1"))?.count === 3);
  let t = 0;
  const limiter = createRateLimiter({ limit: 2, now: () => t });
  check("rate limit", limiter.check() === "ok" && limiter.check() === "ok" && limiter.check() === "notify" && limiter.check() === "drop");
  t = 1000;
  check("rate window", limiter.check() === "ok");
  const log = createRefusedOriginLog({ max: 2 });
  ["a", "b", "c", "a"].forEach((origin) => log.record(origin));
  check("refused cap", log.list().length === 2 && log.list()[0].count === 2);
  return failures;
}
