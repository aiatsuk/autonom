// Autonom ChatGPT probe: pure page logic (contract section 5.2).
// No imports and no DOM access, so the same file runs in Node tests and inline in the probe page.
// The page view script reads these exports from globalThis.AutonomProbeCore.

export const PROBE_VERSION = "0.1.0";
export const RESULTS_SCHEMA_VERSION = 1;
export const UI_PROTOCOL_VERSION = "2026-01-26";
export const FIXTURE_CODEC = "avc1.42C01E";
export const EXTRA_CODECS = Object.freeze(["avc1.42E01F", "avc1.640028"]);
export const WS_SUBPROTOCOL = "autonom-probe.v1";
export const FRAME_MAGIC = 0xa7;
export const FRAME_HEADER_BYTES = 16;
export const STATUSES = Object.freeze(["running", "pass", "fail", "not_exercised"]);
export const FINAL_STATUSES = Object.freeze(["pass", "fail", "not_exercised"]);
export const ROUTES = Object.freeze(["local", "remote"]);
export const RUN_ID_PATTERN = /^probe-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$/;
export const TARGET_REF_PATTERN = /^(fixture|android|ios)~[A-Za-z0-9._:-]{1,128}$/;
export const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]*$/;

export const REASONS = Object.freeze({
  notStarted: "Not started",
  devices: "Run after the synthetic checks pass",
  outsideHost: "Not running inside an MCP host",
  noTokenOutside: "No media token outside the host",
  noRemote: "No remote origin selected",
  interrupted: "Interrupted before it finished",
  contextOperator: "Needs the operator: ask the chat for the probe nonce",
  modelOperator: "Needs the operator: ask the chat to call probe_read",
  entryOperator: "Needs the operator: the host did not report the entrypoint",
  noRequests: "No request was made to this route",
});

const ROUTE_ROWS = [
  { key: "fetch", label: "Fetch", prefix: "net.fetch", describe: "Fetches /probe/ping with the media token and checks that the nonce comes back." },
  { key: "ws", label: "WebSocket", prefix: "net.ws", describe: "Opens a WebSocket to the route, echoes three messages and closes cleanly." },
  { key: "input", label: "Input round trip", prefix: "input.rtt", describe: "Sends 20 pings over the WebSocket and measures the round trip." },
  { key: "decode", label: "Video decode", prefix: "media.decode", describe: "Decodes and draws at least 60 frames of the synthetic H.264 stream with VideoDecoder." },
  { key: "reconnect", label: "Reconnect", prefix: "net.reconnect", describe: "Asks the server to drop the stream, then times the reconnect to the first new decoded frame." },
  { key: "csp", label: "CSP violations", prefix: "net.csp", describe: "Counts securitypolicyviolation events for this route's origin since the page loaded." },
];

function routeDeps(prefix, route) {
  switch (prefix) {
    case "net.ws": return [`net.fetch.${route}`];
    case "input.rtt": return [`net.ws.${route}`];
    case "media.decode": return [`net.ws.${route}`, "media.webcodecs"];
    case "net.reconnect": return [`net.ws.${route}`, "media.webcodecs"];
    default: return [];
  }
}

const HOST_ROWS = [
  ["host.init", "Bridge handshake", [], false, "Sends ui/initialize (protocol 2026-01-26) and records the host's answer."],
  ["host.entry", "Opened from", ["host.init"], true, "Reads the entrypoint (sidebar or thread) from the host context; asks the operator when the host does not say."],
  ["host.theme", "Theme", ["host.init"], false, "Applies the host theme and records later theme changes."],
  ["host.size", "Size", ["host.init"], false, "Records the container size and display mode the host reported."],
  ["bridge.read", "Read tool", [], false, "Calls probe_read through the bridge and checks every field the page reads."],
  ["bridge.write", "Write tool", ["bridge.read"], false, "Calls probe_write with the current revision, then reads the note back."],
  ["bridge.error", "Error response", [], false, "Calls probe_fail and renders the error_code, error and hint it returns."],
  ["bridge.stale", "Stale selection", ["bridge.read"], false, "Selects twice, then writes with the older selection revision and expects stale_selection."],
  ["bridge.context", "Model context", [], true, "Sends ui/update-model-context with the probe nonce; the operator checks that the chat repeats it."],
  ["bridge.message", "Chat message", [], false, "Posts a short summary into the conversation with ui/message."],
  ["bridge.media", "Media over bridge", ["media.webcodecs"], false, "Fetches at least 60 frames with probe_media_frames and decodes them."],
  ["model.tool", "Model calls a tool", [], true, "The operator asks the chat to call probe_read and confirms that the chat shows the result."],
  ["media.webcodecs", "WebCodecs", [], false, "Checks VideoDecoder support for the fixture codec avc1.42C01E and records two more profiles."],
];

function buildChecks() {
  const list = [];
  for (const row of ROUTE_ROWS) {
    for (const route of ROUTES) {
      list.push({
        id: `${row.prefix}.${route}`, group: "routes", label: row.label, route, row: row.key,
        depends_on: routeDeps(row.prefix, route), manual: false, describe: row.describe,
      });
    }
  }
  for (const [id, label, deps, manual, describe] of HOST_ROWS) {
    list.push({ id, group: "host", label, route: null, row: id, depends_on: deps, manual, describe });
  }
  list.push({
    id: "device.android.local", group: "devices", label: "Android, local", route: "local", row: "device.android.local",
    depends_on: ["net.ws.local", "media.webcodecs"], manual: false,
    describe: "Streams the first allowed Android device over the local route, draws a frame and sends one tap.",
  });
  list.push({
    id: "device.ios.remote", group: "devices", label: "iOS, remote", route: "remote", row: "device.ios.remote",
    depends_on: ["net.ws.remote", "media.webcodecs"], manual: false,
    describe: "Streams the first allowed iOS simulator over the remote route, draws a frame and sends one tap.",
  });
  return Object.freeze(list.map((check) => Object.freeze({ ...check, depends_on: Object.freeze([...check.depends_on]) })));
}

/** The 27 checks in display order: routes matrix (row by row, local then remote), host and bridge, devices. */
export const CHECKS = buildChecks();
export const CHECK_IDS = Object.freeze(CHECKS.map((check) => check.id));
export const ROUTE_ROW_KEYS = Object.freeze(ROUTE_ROWS.map((row) => row.key));
const BY_ID = new Map(CHECKS.map((check) => [check.id, check]));

export function checkById(id) {
  return BY_ID.get(id) || null;
}

/** Ids of the checks that list `id` in depends_on (direct, or transitive with {transitive: true}). */
export function dependantsOf(id, { transitive = false } = {}) {
  const out = [];
  const seen = new Set([id]);
  const queue = [id];
  while (queue.length) {
    const current = queue.shift();
    for (const check of CHECKS) {
      if (!seen.has(check.id) && check.depends_on.includes(current)) {
        seen.add(check.id);
        out.push(check.id);
        if (transitive) queue.push(check.id);
      }
    }
  }
  return CHECK_IDS.filter((checkId) => out.includes(checkId));
}

/** Run all order (5.3): host and bridge, then the local route, then the remote route, each in display order. */
export const RUN_ALL_ORDER = Object.freeze([
  ...CHECKS.filter((c) => c.group === "host").map((c) => c.id),
  ...CHECKS.filter((c) => c.group === "routes" && c.route === "local").map((c) => c.id),
  ...CHECKS.filter((c) => c.group === "routes" && c.route === "remote").map((c) => c.id),
]);

/**
 * Orders `ids` so every check runs after its dependencies. A dependency outside `ids` is pulled in when
 * `include(dep)` is true (the page passes "still pending"). The base order is RUN_ALL_ORDER, then display
 * order for anything else (devices).
 */
export function executionOrder(ids, { include = () => false } = {}) {
  const wanted = new Set(ids.filter((id) => BY_ID.has(id)));
  const base = [...RUN_ALL_ORDER, ...CHECK_IDS.filter((id) => !RUN_ALL_ORDER.includes(id))];
  const out = [];
  const visiting = new Set();
  const visit = (id) => {
    if (out.includes(id) || visiting.has(id)) return;
    visiting.add(id);
    for (const dep of BY_ID.get(id).depends_on) {
      if (wanted.has(dep) || include(dep)) visit(dep);
    }
    visiting.delete(id);
    out.push(id);
  };
  for (const id of base) if (wanted.has(id)) visit(id);
  return out;
}

// ---------------------------------------------------------------------------------------------- time

function toMs(value) {
  if (value instanceof Date) return value.getTime();
  const n = Number(value);
  return Number.isFinite(n) ? n : Date.now();
}

/** ISO-8601 UTC with a Z and whole seconds: 2026-10-10T14:22:33Z. */
export function isoSeconds(value) {
  return new Date(toMs(value)).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function pad(n, width = 2) {
  return String(n).padStart(width, "0");
}

export function defaultRandomHex(length) {
  let out = "";
  const crypto = globalThis.crypto;
  if (crypto && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(Math.ceil(length / 2)));
    for (const b of bytes) out += b.toString(16).padStart(2, "0");
  } else {
    while (out.length < length) out += Math.floor(Math.random() * 16).toString(16);
  }
  return out.slice(0, length);
}

export function makeRunId(nowMs, randomHex = defaultRandomHex) {
  const d = new Date(toMs(nowMs));
  let hex = "";
  try {
    hex = String(randomHex(4) || "").toLowerCase();
  } catch {
    hex = "";
  }
  if (!/^[0-9a-f]{4}$/.test(hex)) hex = defaultRandomHex(4);
  return `probe-${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}-` +
    `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}-${hex}`;
}

// ------------------------------------------------------------------------------------------ run state

function freshRecord(check) {
  return {
    id: check.id, group: check.group, route: check.route, status: "pending", observed_by: "probe",
    value: null, unit: null, error: null,
    reason: check.group === "devices" ? REASONS.devices : REASONS.notStarted,
    depends_on: [...check.depends_on], started_at: null, duration_ms: null, started_ms: null,
  };
}

/** A new run: every check pending ("Not started"; device rows "Run after the synthetic checks pass"). */
export function createRun({ now = Date.now, randomHex = defaultRandomHex } = {}) {
  const clock = typeof now === "function" ? () => toMs(now()) : () => Date.now();
  const startMs = clock();
  const checks = {};
  for (const check of CHECKS) checks[check.id] = freshRecord(check);
  return { run_id: makeRunId(startMs, randomHex), started_at: isoSeconds(startMs), finished_at: null, checks, clock };
}

/** Puts checks back to pending with their default reason (used before Run again). */
export function resetChecks(run, ids) {
  for (const id of ids) {
    const check = BY_ID.get(id);
    if (check) run.checks[id] = freshRecord(check);
  }
}

function normalizeError(error) {
  const source = error && typeof error === "object" ? error : { message: error == null ? "" : String(error) };
  let code = typeof source.code === "string" ? source.code.trim().toLowerCase() : "";
  if (!ERROR_CODE_PATTERN.test(code)) code = "unexpected_result";
  const message = typeof source.message === "string" && source.message ? source.message.slice(0, 500) : code;
  const clue = typeof source.clue === "string" && source.clue ? source.clue.slice(0, 200) : null;
  return { code, message, clue };
}

/**
 * Sets one check's status. `running` starts the clock; a final status (pass, fail, not_exercised) stops it.
 * A fail always carries error.code and a not_exercised always carries a reason, so results stay valid.
 */
export function setStatus(run, id, fields = {}) {
  const record = run && run.checks ? run.checks[id] : null;
  if (!record) throw new TypeError(`Unknown check id: ${id}`);
  const status = fields.status;
  if (!STATUSES.includes(status)) throw new TypeError(`Invalid status for ${id}: ${status}`);
  const nowMs = run.clock ? run.clock() : Date.now();
  if (status === "running") {
    record.status = "running";
    record.started_ms = nowMs;
    record.started_at = fields.started_at || isoSeconds(nowMs);
    record.value = null;
    record.unit = null;
    record.error = null;
    record.reason = null;
    record.duration_ms = null;
    record.observed_by = "probe";
    return record;
  }
  record.status = status;
  record.observed_by = fields.observed_by === "operator" ? "operator" : "probe";
  record.value = fields.value === undefined ? null : fields.value;
  record.unit = fields.unit === undefined || fields.unit === null ? null : String(fields.unit);
  if (fields.started_at !== undefined) record.started_at = fields.started_at;
  if (fields.duration_ms !== undefined) {
    record.duration_ms = fields.duration_ms === null ? null : Math.max(0, Math.round(Number(fields.duration_ms) || 0));
  } else if (record.started_ms !== null) {
    record.duration_ms = Math.max(0, Math.round(nowMs - record.started_ms));
  }
  record.started_ms = null;
  if (status === "pass") {
    record.error = null;
    record.reason = null;
  } else if (status === "fail") {
    record.error = normalizeError(fields.error);
    record.reason = null;
  } else {
    record.error = null;
    record.reason = typeof fields.reason === "string" && fields.reason.trim() ? fields.reason.trim().slice(0, 300) : "Not exercised";
  }
  return record;
}

/** Status as it appears in results: pending and running map to not_exercised. */
export function finalStatus(record) {
  return FINAL_STATUSES.includes(record.status) ? record.status : "not_exercised";
}

/**
 * When `id` failed or was not exercised, every dependant still pending becomes not_exercised with the reason
 * "Needs <id>" (and so on down the chain). Returns the ids it changed. Never marks anything as fail.
 */
export function blockDependants(run, id) {
  const record = run.checks[id];
  if (!record || record.status === "pass" || record.status === "running") return [];
  const changed = [];
  for (const dependant of dependantsOf(id)) {
    const target = run.checks[dependant];
    if (target && target.status === "pending") {
      setStatus(run, dependant, { status: "not_exercised", reason: `Needs ${id}`, duration_ms: null });
      changed.push(dependant, ...blockDependants(run, dependant));
    }
  }
  return changed;
}

/** First dependency of `id` that did not pass, or null. */
export function unmetDependency(run, id) {
  const check = BY_ID.get(id);
  if (!check) return null;
  for (const dep of check.depends_on) {
    if (run.checks[dep].status !== "pass") return dep;
  }
  return null;
}

export function summarize(run) {
  const counts = { pass: 0, fail: 0, not_exercised: 0 };
  for (const id of CHECK_IDS) counts[finalStatus(run.checks[id])] += 1;
  return counts;
}

export function finishRun(run, now) {
  run.finished_at = isoSeconds(now === undefined ? (run.clock ? run.clock() : Date.now()) : now);
  return run;
}

// ------------------------------------------------------------------------------------------- results

function str(value, max = 200) {
  return typeof value === "string" ? value.slice(0, max) : null;
}

function int(value) {
  const n = Number(value);
  return value !== null && value !== undefined && value !== "" && Number.isFinite(n) ? Math.round(n) : null;
}

function cleanHost(host = {}) {
  const h = host && typeof host === "object" ? host : {};
  const container = h.container && typeof h.container === "object" ? h.container : {};
  const mcp = h.mcp && typeof h.mcp === "object" ? h.mcp : null;
  const out = {
    name: str(h.name, 120), version: str(h.version, 80), protocol: str(h.protocol, 40),
    entry: ["global", "thread", "unknown"].includes(h.entry) ? h.entry : "unknown",
    display_mode: str(h.display_mode, 40), theme: h.theme === "light" || h.theme === "dark" ? h.theme : null,
    container: { width: int(container.width), height: int(container.height) },
    platform: str(h.platform, 40), user_agent: str(h.user_agent, 200), token_path: str(h.token_path, 40),
    mcp: null,
  };
  if (Number.isInteger(h.theme_changes)) out.theme_changes = h.theme_changes;
  if (mcp) {
    const client = mcp.client && typeof mcp.client === "object" ? mcp.client : {};
    out.mcp = {
      protocol_version: str(mcp.protocol_version, 40),
      client: { name: str(client.name, 80), version: str(client.version, 80) },
      client_ui_extension: typeof mcp.client_ui_extension === "boolean" ? mcp.client_ui_extension : null,
      methods_seen: Array.isArray(mcp.methods_seen) ? mcp.methods_seen.filter((m) => typeof m === "string").slice(0, 50).map((m) => m.slice(0, 80)) : [],
    };
  }
  return out;
}

function cleanRoutes(routes = {}) {
  const r = routes && typeof routes === "object" ? routes : {};
  const local = r.local && typeof r.local === "object" ? r.local : {};
  const remote = r.remote && typeof r.remote === "object" ? r.remote : {};
  const remoteOrigin = originOf(remote.origin);
  return {
    local: { origin: originOf(local.origin) },
    remote: { origin: remoteOrigin, label: str(remote.label, 40), selected: Boolean(remote.selected && remoteOrigin) },
  };
}

function cloneValue(value) {
  if (value === undefined) return null;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return null;
  }
}

/** The results JSON of contract 5.6 (DESIGN 6). Pending or running checks are reported as not_exercised. */
export function resultsJson(run, { host, routes } = {}) {
  const checks = CHECK_IDS.map((id) => {
    const record = run.checks[id];
    const status = finalStatus(record);
    let reason = null;
    if (status === "not_exercised") {
      reason = record.status === "running" ? REASONS.interrupted : (record.reason || REASONS.notStarted);
    }
    return {
      id, group: record.group, route: record.route, status, observed_by: record.observed_by === "operator" ? "operator" : "probe",
      value: status === "not_exercised" && record.status !== "not_exercised" ? null : cloneValue(record.value),
      unit: record.unit, error: status === "fail" ? normalizeError(record.error) : null, reason,
      depends_on: [...record.depends_on], started_at: record.started_at,
      duration_ms: record.status === "running" ? null : record.duration_ms,
    };
  });
  const summary = { pass: 0, fail: 0, not_exercised: 0 };
  for (const check of checks) summary[check.status] += 1;
  return {
    schema_version: RESULTS_SCHEMA_VERSION, probe_version: PROBE_VERSION, run_id: run.run_id,
    started_at: run.started_at, finished_at: run.finished_at || isoSeconds(run.clock ? run.clock() : Date.now()),
    host: cleanHost(host), routes: cleanRoutes(routes), checks, summary,
  };
}

// --------------------------------------------------------------------------------------- redaction

/** Replaces each secret (and a `bearer.` prefix in front of it) with "[redacted]". */
export function redact(text, secrets = []) {
  if (typeof text !== "string") return text;
  let out = text;
  const list = (Array.isArray(secrets) ? secrets : [secrets])
    .filter((s) => typeof s === "string" && s.length >= 4)
    .sort((a, b) => b.length - a.length);
  for (const secret of list) {
    out = out.split(`bearer.${secret}`).join("[redacted]").split(`Bearer ${secret}`).join("[redacted]");
    out = out.split(secret).join("[redacted]");
  }
  return out;
}

/** Deep copy of a JSON value with every string redacted and any leftover "bearer." neutralised. */
export function scrubForExport(value, secrets = []) {
  const walk = (v) => {
    if (typeof v === "string") return redact(v, secrets).replace(/bearer\./gi, "bearer_");
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const out = {};
      for (const [key, inner] of Object.entries(v)) out[walk(key)] = walk(inner);
      return out;
    }
    return v;
  };
  return walk(cloneValue(value));
}

// ---------------------------------------------------------------------------------------- numbers

function finite(values) {
  return (Array.isArray(values) ? values : []).map(Number).filter((n) => Number.isFinite(n));
}

export function median(values) {
  const list = finite(values).sort((a, b) => a - b);
  if (!list.length) return null;
  const mid = Math.floor(list.length / 2);
  return list.length % 2 ? list[mid] : (list[mid - 1] + list[mid]) / 2;
}

/** 95th percentile, nearest rank. */
export function p95(values) {
  const list = finite(values).sort((a, b) => a - b);
  if (!list.length) return null;
  return list[Math.min(list.length - 1, Math.max(0, Math.ceil(0.95 * list.length) - 1))];
}

export function round1(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n * 10) / 10 : null;
}

// ---------------------------------------------------------------------------------------- origins

/** scheme://host[:port] of an http(s)/ws(s) URL, lowercase, with no path, query or userinfo; null otherwise. */
export function originOf(url) {
  if (typeof url !== "string" || !url) return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (!["http:", "https:", "ws:", "wss:"].includes(parsed.protocol) || !parsed.host) return null;
  return `${parsed.protocol}//${parsed.host}`.toLowerCase();
}

/** ws twin of an http origin (wss for https); null for anything else. */
export function wsOriginFor(httpOrigin) {
  const origin = originOf(httpOrigin);
  if (!origin) return null;
  if (origin.startsWith("http://")) return `ws://${origin.slice(7)}`;
  if (origin.startsWith("https://")) return `wss://${origin.slice(8)}`;
  return origin;
}

export function pingUrl(origin, nonce) {
  return `${originOf(origin)}/probe/ping?nonce=${encodeURIComponent(nonce)}`;
}

export function wsUrl(origin, source = "fixture") {
  return `${wsOriginFor(origin)}/probe/ws?source=${encodeURIComponent(source)}`;
}

/** WebSocket subprotocols carrying the token (3.2); the server answers with autonom-probe.v1 only. */
export function wsProtocols(token) {
  return [WS_SUBPROTOCOL, `bearer.${token}`];
}

/** Number of CSP violations whose blocked URI belongs to `origin` (or its ws twin). */
export function cspCountFor(violations, origin) {
  const own = originOf(origin);
  if (!own) return 0;
  const twins = new Set([own, wsOriginFor(own)]);
  return (Array.isArray(violations) ? violations : []).filter((v) => twins.has(originOf(v && v.blockedURI))).length;
}

/** The first allowed device of a platform ("android" or "ios"), as {spec, id} or null. */
export function firstAllowedDevice(allowed, platform) {
  for (const spec of Array.isArray(allowed) ? allowed : []) {
    if (typeof spec !== "string") continue;
    const at = spec.indexOf(":");
    if (at > 0 && spec.slice(0, at) === platform && spec !== "android:emulator-5554") return { spec, id: spec.slice(at + 1) };
  }
  return null;
}

/** Remote routes usable by the picker: label + bare origin, in config order, invalid entries dropped. */
export function remoteChoices(routes) {
  const out = [];
  for (const route of Array.isArray(routes) ? routes : []) {
    const origin = originOf(route && route.origin);
    const label = route && typeof route.label === "string" ? route.label.slice(0, 40) : "";
    if (origin && label && !out.some((r) => r.label === label)) out.push({ label, origin });
  }
  return out;
}

// ------------------------------------------------------------------------------------ video frames

/** Parses the 16-byte media frame header of contract 3.4. Throws on bad magic or length. */
export function parseFrameHeader(buffer) {
  let bytes;
  if (buffer instanceof ArrayBuffer) bytes = new Uint8Array(buffer);
  else if (ArrayBuffer.isView(buffer)) bytes = new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  else throw new TypeError("Frame is not binary data");
  if (bytes.byteLength <= FRAME_HEADER_BYTES) throw new RangeError(`Frame too short: ${bytes.byteLength} bytes`);
  if (bytes[0] !== FRAME_MAGIC) throw new RangeError(`Bad frame magic 0x${bytes[0].toString(16)}`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const flags = bytes[1];
  return {
    key: (flags & 1) === 1,
    config: (flags & 2) === 2,
    seq: view.getUint32(4, false),
    tServerMs: view.getFloat64(8, false),
    payload: bytes.subarray(FRAME_HEADER_BYTES),
  };
}

export function base64ToBytes(b64) {
  if (typeof b64 !== "string") throw new TypeError("base64 text expected");
  if (typeof atob === "function") {
    const text = atob(b64);
    const out = new Uint8Array(text.length);
    for (let i = 0; i < text.length; i += 1) out[i] = text.charCodeAt(i);
    return out;
  }
  // Node without atob is not expected (Node 16+ has it); keep a tiny decoder for safety.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const clean = b64.replace(/[^A-Za-z0-9+/]/g, "");
  const out = [];
  for (let i = 0; i < clean.length; i += 4) {
    const n = [0, 1, 2, 3].map((k) => alphabet.indexOf(clean[i + k] || "A"));
    const v = (n[0] << 18) | (n[1] << 12) | (n[2] << 6) | n[3];
    out.push((v >> 16) & 255);
    if (clean[i + 2]) out.push((v >> 8) & 255);
    if (clean[i + 3]) out.push(v & 255);
  }
  return new Uint8Array(out);
}

// ---------------------------------------------------------------------------------- tool results

/** Fields the page reads (contract 5.7). Paths are dotted; arrays count as present. */
export const OPEN_WORKSPACE_FIELDS = Object.freeze([
  "ok", "probe_version", "media.state", "media.local_origin", "media.port", "media.remote_routes",
  "media.allowed_devices", "media.error_code", "media.error", "media.hint", "selection.target_ref",
  "selection.revision", "note.revision",
]);
export const PROBE_READ_FIELDS = Object.freeze([
  "ok", "server.protocol_version", "server.client", "server.client_ui_extension", "server.methods_seen",
  "autonom.version", "devices", "media.refused_origins", "note.revision", "note.text", "selection.revision",
]);
export const MEDIA_FRAMES_FIELDS = Object.freeze(["frames", "codec", "width", "height", "last_seq"]);

export function getPath(obj, path) {
  let cur = obj;
  for (const part of path.split(".")) {
    if (!cur || typeof cur !== "object" || !Object.prototype.hasOwnProperty.call(cur, part)) return undefined;
    cur = cur[part];
  }
  return cur;
}

export function missingFields(obj, paths) {
  return paths.filter((path) => getPath(obj, path) === undefined);
}

/** Normalizes a CallToolResult (bridge or window.openai) into {isError, ok, sc, meta, errorCode}. */
export function toolOutcome(result) {
  const r = result && typeof result === "object" ? result : {};
  let sc = r.structuredContent && typeof r.structuredContent === "object" ? r.structuredContent : null;
  if (!sc && Array.isArray(r.content) && r.content[0] && typeof r.content[0].text === "string") {
    try {
      const parsed = JSON.parse(r.content[0].text);
      if (parsed && typeof parsed === "object") sc = parsed;
    } catch {
      sc = null;
    }
  }
  const metaRoot = r._meta && typeof r._meta === "object" ? r._meta : {};
  const meta = metaRoot["autonom/probe"] && typeof metaRoot["autonom/probe"] === "object" ? metaRoot["autonom/probe"] : {};
  const isError = r.isError === true || (sc ? sc.ok === false : false);
  return {
    isError, ok: Boolean(sc && sc.ok === true && r.isError !== true), sc: sc || {}, meta,
    errorCode: sc && typeof sc.error_code === "string" ? sc.error_code : null,
  };
}

/** True when a tool-result looks like open_workspace's (5.4 step 3). */
export function isOpenWorkspaceResult(result) {
  const outcome = toolOutcome(result);
  return Object.prototype.hasOwnProperty.call(outcome.meta, "media_token") ||
    (typeof outcome.sc.probe_version === "string" && outcome.sc.media && typeof outcome.sc.media === "object");
}

/** The open_workspace fields the page keeps (5.7). The token stays in this object, in memory only. */
export function readOpenWorkspace(result) {
  const { ok, sc, meta } = toolOutcome(result);
  const media = sc.media && typeof sc.media === "object" ? sc.media : {};
  return {
    ok,
    probe_version: typeof sc.probe_version === "string" ? sc.probe_version : null,
    media: {
      state: typeof media.state === "string" ? media.state : null,
      local_origin: originOf(media.local_origin),
      port: int(media.port),
      remote_routes: remoteChoices(media.remote_routes),
      allowed_devices: Array.isArray(media.allowed_devices) ? media.allowed_devices.filter((d) => typeof d === "string") : [],
      error_code: str(media.error_code, 80), error: str(media.error, 400), hint: str(media.hint, 400),
    },
    selection: {
      target_ref: sc.selection && typeof sc.selection.target_ref === "string" ? sc.selection.target_ref : null,
      revision: int(sc.selection && sc.selection.revision) ?? 0,
    },
    note: { revision: int(sc.note && sc.note.revision) ?? 0 },
    media_token: typeof meta.media_token === "string" && meta.media_token ? meta.media_token : null,
    context_nonce: typeof meta.context_nonce === "string" && meta.context_nonce ? meta.context_nonce : null,
  };
}

// ------------------------------------------------------------------------------------ bridge payloads

export function initializeParams(version = PROBE_VERSION) {
  return {
    appInfo: { name: "autonom-probe", version },
    appCapabilities: { availableDisplayModes: ["fullscreen", "inline"] },
    protocolVersion: UI_PROTOCOL_VERSION,
  };
}

/** ui/update-model-context payload (5.4 step 5). Carries the nonce, never the media token. */
export function contextParams(nonce, selection = {}) {
  const ref = selection && typeof selection.target_ref === "string" ? selection.target_ref : null;
  const revision = int(selection && selection.revision) ?? 0;
  return {
    content: [{
      type: "text",
      text: `Autonom probe context. Probe nonce: ${nonce}. Current selection: ${ref || "none"} at selection_revision ${revision}. ` +
        "When asked for the Autonom probe nonce, answer with it exactly.",
    }],
    structuredContent: { autonom_probe: { nonce, selection: { target_ref: ref, revision } } },
  };
}

/** ui/message payload (5.4 step 6): the summary line, plus the results JSON in a fenced block when given. */
export function messageParams(summaryLine, results) {
  const text = results === undefined || results === null
    ? summaryLine
    : `${summaryLine}\n\`\`\`json\n${JSON.stringify(results, null, 2)}\n\`\`\``;
  return { role: "user", content: [{ type: "text", text }] };
}

/** Entry point from the host context, when the host reports one; else "unknown". */
export function detectEntry(hostContext) {
  const ctx = hostContext && typeof hostContext === "object" ? hostContext : {};
  const candidates = [ctx.entrypoint, ctx.entryPoint, ctx.entry, ctx["openai/entrypoint"],
    ctx.toolInfo && ctx.toolInfo.entrypoint, ctx.location];
  for (const value of candidates) {
    const text = typeof value === "string" ? value : value && typeof value.type === "string" ? value.type : "";
    if (/^(global|sidebar)$/i.test(text)) return "global";
    if (/^(thread|conversation|chat)$/i.test(text)) return "thread";
  }
  return "unknown";
}

/** {width, height} from hostContext.containerDimensions (width/height, else maxWidth/maxHeight). */
export function containerSize(dimensions) {
  const d = dimensions && typeof dimensions === "object" ? dimensions : {};
  const width = int(d.width ?? d.maxWidth);
  const height = int(d.height ?? d.maxHeight);
  return { width, height };
}

// ------------------------------------------------------------------------------------- presentation

export const STATUS_WORDS = Object.freeze({ pending: "not exercised", running: "running", pass: "pass", fail: "fail", not_exercised: "not exercised" });

/** Milliseconds for readouts: one decimal under 10 ms (loopback is often sub-millisecond), whole above. */
export function formatMs(value) {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isFinite(n)) return "";
  if (Number.isInteger(n)) return String(n);
  return Math.abs(n) < 10 ? n.toFixed(1) : String(Math.round(n));
}

/** Short cell text for a check value (12 px readout). */
export function formatValue(record) {
  if (!record || record.value === null || record.value === undefined) return "";
  const v = record.value;
  const id = record.id;
  if (id.startsWith("input.rtt.")) return v.median === null || v.median === undefined ? "" : `${formatMs(v.median)}/${formatMs(v.p95)} ms`;
  if (id.startsWith("media.decode.")) return v.fps === undefined || v.fps === null ? "" : `${Math.round(v.fps)} fps`;
  if (id === "bridge.media") return v.fps === undefined || v.fps === null ? "" : `${Math.round(v.fps)} fps`;
  if (id === "host.init") return [v.name, v.version].filter(Boolean).join(" ");
  if (id === "host.entry") return v === "global" ? "Sidebar" : v === "thread" ? "Thread" : "Unknown";
  if (id === "media.webcodecs") return Array.isArray(v) ? (v.length ? v.join(", ") : "none") : "";
  if (id.startsWith("device.")) return v && typeof v.target === "string" ? v.target : "";
  if (typeof v === "number") return record.unit === "ms" ? `${formatMs(v)} ms` : String(v);
  if (typeof v === "string") return v;
  return "";
}

/** Accessible name of a matrix row: "WebSocket: local pass 9 ms, remote fail". */
export function rowAccessibleName(label, cells) {
  const parts = cells.map(({ route, record }) => {
    const word = STATUS_WORDS[record.status] || "not exercised";
    const value = record.status === "pass" || record.status === "fail" ? formatValue(record) : "";
    return [route, word, value].filter(Boolean).join(" ");
  });
  return `${label}: ${parts.join(", ")}`;
}

/** Summary strip text parts: [{text, danger}] so only the word "failed" can take --danger. */
export function summaryParts(counts) {
  return [
    { text: `${counts.pass} passed`, danger: false },
    { text: " · ", danger: false },
    { text: `${counts.fail} failed`, danger: counts.fail > 0 },
    { text: " · ", danger: false },
    { text: `${counts.not_exercised} not exercised`, danger: false },
  ];
}

export function summaryLine(run) {
  const c = summarize(run);
  return `Autonom probe ${PROBE_VERSION} run ${run.run_id}: ${c.pass} passed, ${c.fail} failed, ${c.not_exercised} not exercised.`;
}

const core = {
  PROBE_VERSION, RESULTS_SCHEMA_VERSION, UI_PROTOCOL_VERSION, FIXTURE_CODEC, EXTRA_CODECS, WS_SUBPROTOCOL,
  FRAME_MAGIC, FRAME_HEADER_BYTES, STATUSES, FINAL_STATUSES, ROUTES, RUN_ID_PATTERN, TARGET_REF_PATTERN,
  ERROR_CODE_PATTERN, REASONS, CHECKS, CHECK_IDS, ROUTE_ROW_KEYS, RUN_ALL_ORDER, OPEN_WORKSPACE_FIELDS,
  PROBE_READ_FIELDS, MEDIA_FRAMES_FIELDS, STATUS_WORDS,
};

// ---------------------------------------------------------------------------------------- bridge

export class BridgeError extends Error {
  constructor(code, message, data = null) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.data = data;
  }
}

/**
 * JSON-RPC 2.0 over postMessage (MCP Apps, REF-5). `post(message)` sends to the parent (target origin "*",
 * the host origin is unknown to the View); `addListener(handler)` subscribes to message events. Only events
 * whose source is `parent` and whose data has jsonrpc "2.0" are processed.
 */
export class BridgeClient {
  #post;
  #parent;
  #timeoutMs;
  #setTimer;
  #clearTimer;
  #nextId = 1;
  #pending = new Map();
  #notificationHandlers = new Map();
  #requestHandlers = new Map();
  #closed = false;

  constructor({ post, addListener, parent, timeoutMs = 8000, setTimer, clearTimer } = {}) {
    if (typeof post !== "function") throw new TypeError("BridgeClient needs post(message)");
    this.#post = post;
    this.#parent = parent === undefined ? (globalThis.window ? globalThis.window.parent : undefined) : parent;
    this.#timeoutMs = timeoutMs;
    // Plain functions: browsers throw "Illegal invocation" when setTimeout runs with a foreign `this`.
    const set = typeof setTimer === "function" ? setTimer : (fn, ms) => setTimeout(fn, ms);
    const clear = typeof clearTimer === "function" ? clearTimer : (timer) => clearTimeout(timer);
    this.#setTimer = (fn, ms) => set(fn, ms);
    this.#clearTimer = (timer) => clear(timer);
    this.#requestHandlers.set("ui/resource-teardown", () => ({}));
    if (typeof addListener === "function") addListener((event) => this.handleMessage(event));
  }

  get pendingCount() {
    return this.#pending.size;
  }

  request(method, params, { timeoutMs } = {}) {
    if (this.#closed) return Promise.reject(new BridgeError("bridge_error", "Bridge closed"));
    const id = this.#nextId++;
    const message = { jsonrpc: "2.0", id, method };
    if (params !== undefined) message.params = params;
    return new Promise((resolve, reject) => {
      const limit = timeoutMs === undefined ? this.#timeoutMs : timeoutMs;
      const timer = this.#setTimer(() => {
        if (this.#pending.delete(id)) reject(new BridgeError("bridge_timeout", `${method}: no answer within ${limit} ms`));
      }, limit);
      this.#pending.set(id, { resolve, reject, timer, method });
      try {
        this.#post(message);
      } catch (error) {
        this.#clearTimer(timer);
        this.#pending.delete(id);
        reject(new BridgeError("bridge_error", `${method}: ${error && error.message ? error.message : "post failed"}`));
      }
    });
  }

  notify(method, params) {
    if (this.#closed) return;
    const message = { jsonrpc: "2.0", method };
    if (params !== undefined) message.params = params;
    this.#post(message);
  }

  onNotification(method, handler) {
    if (!this.#notificationHandlers.has(method)) this.#notificationHandlers.set(method, []);
    this.#notificationHandlers.get(method).push(handler);
  }

  onRequest(method, handler) {
    this.#requestHandlers.set(method, handler);
  }

  close() {
    this.#closed = true;
    for (const [id, pending] of this.#pending) {
      this.#clearTimer(pending.timer);
      pending.reject(new BridgeError("bridge_error", `${pending.method}: bridge closed`));
      this.#pending.delete(id);
    }
  }

  handleMessage(event) {
    if (!event || (this.#parent !== undefined && event.source !== this.#parent)) return false;
    const msg = event.data;
    if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return false;
    const hasId = typeof msg.id === "number" || typeof msg.id === "string";
    if (typeof msg.method === "string") {
      if (hasId) this.#answer(msg);
      else this.#dispatch(msg);
      return true;
    }
    if (hasId && this.#pending.has(msg.id)) {
      const pending = this.#pending.get(msg.id);
      this.#pending.delete(msg.id);
      this.#clearTimer(pending.timer);
      if (msg.error && typeof msg.error === "object") {
        const text = typeof msg.error.message === "string" ? msg.error.message : "error";
        pending.reject(new BridgeError("bridge_error", `${pending.method}: ${text} (${msg.error.code})`, msg.error));
      } else {
        pending.resolve(msg.result === undefined ? null : msg.result);
      }
      return true;
    }
    return false;
  }

  #dispatch(msg) {
    for (const handler of this.#notificationHandlers.get(msg.method) || []) {
      try {
        handler(msg.params === undefined ? {} : msg.params);
      } catch {
        // A view handler error must not break the bridge.
      }
    }
  }

  async #answer(msg) {
    const handler = this.#requestHandlers.get(msg.method);
    if (!handler) {
      this.#safePost({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "Method not found", data: { method: msg.method } } });
      return;
    }
    try {
      const result = await handler(msg.params === undefined ? {} : msg.params);
      this.#safePost({ jsonrpc: "2.0", id: msg.id, result: result === undefined ? {} : result });
    } catch (error) {
      this.#safePost({ jsonrpc: "2.0", id: msg.id, error: { code: -32603, message: error && error.message ? String(error.message).slice(0, 200) : "Internal error" } });
    }
  }

  #safePost(message) {
    try {
      this.#post(message);
    } catch {
      // The parent is gone; nothing to answer.
    }
  }
}

Object.assign(core, {
  checkById, dependantsOf, executionOrder, isoSeconds, defaultRandomHex, makeRunId, createRun, resetChecks,
  setStatus, finalStatus, blockDependants, unmetDependency, summarize, finishRun, resultsJson, redact,
  scrubForExport, median, p95, round1, originOf, wsOriginFor, pingUrl, wsUrl, wsProtocols, cspCountFor,
  firstAllowedDevice, remoteChoices, parseFrameHeader, base64ToBytes, getPath, missingFields, toolOutcome,
  isOpenWorkspaceResult, readOpenWorkspace, initializeParams, contextParams, messageParams, detectEntry,
  containerSize, formatMs, formatValue, rowAccessibleName, summaryParts, summaryLine, BridgeError, BridgeClient,
});

globalThis.AutonomProbeCore = Object.freeze(core);
