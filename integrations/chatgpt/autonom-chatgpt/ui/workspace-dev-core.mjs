// Autonom development workspace page: the diagnostics page of the bridge media path (contract section 6).
// The pure helpers at the top have no imports and no DOM access, so Node tests import this file directly;
// the page view at the bottom runs only when a document exists. The server inlines this file into
// workspace-dev.html as a module script, so it must never contain a closing script tag.
// The page talks to the adapter only through the MCP Apps host bridge (postMessage JSON-RPC). It uses no
// network API of the browser.

export const WORKSPACE_CORE_VERSION = "0.1.0";
export const DIAGNOSTICS_SCHEMA_VERSION = 1;
export const UI_PROTOCOL_VERSION = "2026-01-26";
export const DIAG_RUN_ID_PATTERN = /^diag-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$/;
export const HOST_ID_PATTERN = /^h_[a-z2-7]{26}$/;
export const SESSION_ID_PATTERN = /^s_[A-Za-z0-9]{1,64}$/;
export const STREAM_ID_PATTERN = /^st_[0-9a-f]{16}$/;
export const PROFILES = Object.freeze(["focus", "thumb"]);
export const CHECK_STATUSES = Object.freeze(["pass", "fail", "not_exercised"]);
export const SYSTEM_BUTTONS = Object.freeze(["home", "back"]);
export const AGENT_HAS_CONTROL = "The agent has control - Pause to take over";

/** The six page checks of contract section 6, in report order. */
export const CHECKS = Object.freeze([
  Object.freeze({ id: "bridge.list_sessions", label: "Sessions through the bridge" }),
  Object.freeze({ id: "media.focus", label: "Focus stream decoded" }),
  Object.freeze({ id: "media.thumb", label: "Thumb stream decoded" }),
  Object.freeze({ id: "input.tap", label: "Tap on the picture" }),
  Object.freeze({ id: "control.pause", label: "Pause (take over)" }),
  Object.freeze({ id: "control.resume", label: "Resume (hand back)" }),
]);
export const CHECK_IDS = Object.freeze(CHECKS.map((check) => check.id));

/** Frames a focus stream, and key frames a thumb stream, must paint for the media checks to pass. */
export const FOCUS_PASS_FRAMES = 10;
export const THUMB_PASS_FRAMES = 2;

/** Error codes after which the pull loop stops instead of trying again. */
export const FATAL_STREAM_CODES = Object.freeze(["stream_not_found", "media_unavailable", "session_stopped", "session_not_found",
  "host_not_found", "host_unavailable"]);

// ------------------------------------------------------------------------------------------ small helpers

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function int(value) {
  return Number.isInteger(value) ? value : null;
}

function nonNegInt(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function num(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value, max = 400) {
  return typeof value === "string" ? value.slice(0, max) : null;
}

export function round1(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? Math.round(n * 10) / 10 : null;
}

export function readConfig(doc) {
  const element = doc && doc.getElementById ? doc.getElementById("workspace-config") : null;
  if (!element) return null;
  try {
    const value = JSON.parse(element.textContent || "null");
    return isObject(value) ? value : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------------------------------- run id

export function defaultRandomHex(length) {
  const bytes = new Uint8Array(Math.ceil(length / 2));
  if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, length);
}

/** `diag-YYYYMMDD-HHMMSS-xxxx` in UTC (the diagnostics run id pattern of contract section 0). */
export function makeRunId(nowMs, randomHex = defaultRandomHex) {
  const d = new Date(Number.isFinite(nowMs) ? nowMs : Date.now());
  const p = (n, w = 2) => String(n).padStart(w, "0");
  const stamp = `${p(d.getUTCFullYear(), 4)}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}-` +
    `${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`;
  let suffix = String(randomHex(4) || "").toLowerCase();
  if (!/^[0-9a-f]{4}$/.test(suffix)) suffix = defaultRandomHex(4);
  return `diag-${stamp}-${suffix}`;
}

// ------------------------------------------------------------------------------------------- numbers

function finite(values) {
  return (Array.isArray(values) ? values : []).filter((v) => typeof v === "number" && Number.isFinite(v));
}

/** Nearest-rank percentile (p in 0..100) of the finite numbers in `values`; null when there are none. */
export function percentile(values, p) {
  const list = finite(values).sort((a, b) => a - b);
  if (!list.length) return null;
  const q = Math.min(100, Math.max(0, Number.isFinite(p) ? p : 50));
  const rank = Math.ceil((q / 100) * list.length);
  return list[Math.min(list.length - 1, Math.max(0, rank - 1))];
}

/**
 * Sliding-window rate counter. `tick(now)` records one event; `rate(now)` is events per second over the
 * last `windowMs` (over the time since the first event while the window is not full yet).
 */
export function fpsCounter(windowMs = 1000) {
  const span = Number.isFinite(windowMs) && windowMs > 0 ? windowMs : 1000;
  const times = [];
  let total = 0;
  let first = null;
  const trim = (now) => {
    while (times.length && times[0] <= now - span) times.shift();
  };
  return {
    tick(now) {
      if (!Number.isFinite(now)) return;
      if (first === null) first = now;
      times.push(now);
      total += 1;
      trim(now);
    },
    rate(now) {
      if (!Number.isFinite(now) || first === null) return 0;
      trim(now);
      const elapsed = Math.min(span, now - first);
      if (elapsed <= 0) return 0;
      return (times.length * 1000) / elapsed;
    },
    get total() {
      return total;
    },
    reset() {
      times.length = 0;
      total = 0;
      first = null;
    },
  };
}

// ------------------------------------------------------------------------------------------- bytes

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64_INDEX = (() => {
  const table = new Int16Array(128).fill(-1);
  for (let i = 0; i < B64.length; i++) table[B64.charCodeAt(i)] = i;
  table["-".charCodeAt(0)] = 62;
  table["_".charCodeAt(0)] = 63;
  return table;
})();

/** Decodes standard or URL-safe base64 (padding optional); null for anything else. */
export function base64ToBytes(b64) {
  if (typeof b64 !== "string") return null;
  const text = b64.replace(/=+$/, "");
  if (text.length % 4 === 1) return null;
  const out = new Uint8Array(Math.floor((text.length * 3) / 4));
  let bits = 0;
  let value = 0;
  let o = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    const v = code < 128 ? B64_INDEX[code] : -1;
    if (v < 0) return null;
    value = ((value << 6) | v) & 0xffffff;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (value >> bits) & 0xff;
    }
  }
  return o === out.length ? out : out.subarray(0, o);
}

export function sameBytes(a, b) {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * The WebCodecs codec string from the first H.264 SPS in an Annex B buffer (`avc1.PPCCLL`), or null.
 * The same logic as the Canvas page's `codecFromSps` (emulation-prevention bytes skipped).
 */
export function codecFromSps(data) {
  if (!(data instanceof Uint8Array)) return null;
  for (let i = 0; i + 3 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1 && (data[i + 3] & 31) === 7) {
      const out = [];
      let zeros = 0;
      for (let j = i + 3; j < data.length && out.length < 4; j++) {
        const b = data[j];
        if (zeros >= 2 && b === 3) {
          zeros = 0;
          continue;
        }
        zeros = b === 0 ? zeros + 1 : 0;
        out.push(b);
      }
      if (out.length < 4) return null;
      return "avc1." + [out[1], out[2], out[3]].map((b) => b.toString(16).toUpperCase().padStart(2, "0")).join("");
    }
  }
  return null;
}

// ------------------------------------------------------------------------------------- tool results

/** Normalizes a CallToolResult into {ok, isError, sc, meta, errorCode, error, hint}. */
export function toolOutcome(result) {
  const r = isObject(result) ? result : {};
  let sc = isObject(r.structuredContent) ? r.structuredContent : null;
  if (!sc && Array.isArray(r.content) && r.content[0] && typeof r.content[0].text === "string") {
    try {
      const parsed = JSON.parse(r.content[0].text);
      if (isObject(parsed)) sc = parsed;
    } catch {
      sc = null;
    }
  }
  const meta = isObject(r._meta) ? r._meta : {};
  const ok = Boolean(sc && sc.ok === true && r.isError !== true);
  let errorCode = null;
  if (!ok) errorCode = sc && typeof sc.error_code === "string" ? sc.error_code : "malformed_result";
  return {
    ok,
    isError: !ok,
    sc: sc || {},
    meta,
    errorCode,
    error: sc ? str(sc.error) : "The tool result has no structured content",
    hint: sc ? str(sc.hint) : null,
  };
}

/** The lease view (contract 3.6) of a tool result's structured content, or null. */
export function controlView(sc) {
  const c = isObject(sc) && isObject(sc.control) ? sc.control : null;
  if (!c) return null;
  const holder = isObject(c.holder) ? { kind: str(c.holder.kind, 40), id: str(c.holder.id, 200), label: str(c.holder.label, 200) } : null;
  return {
    state: str(c.state, 20),
    holder,
    generation: int(c.generation),
    role: c.role === "agent" || c.role === "human" ? c.role : null,
    mine: c.mine === true,
  };
}

/** True when a refused input should tell the person that the agent holds the device. */
export function agentHasControl(outcome) {
  if (!outcome || outcome.ok) return false;
  const view = controlView(outcome.sc);
  return Boolean(view && view.role === "agent");
}

function streamView(value) {
  if (!isObject(value)) return null;
  return {
    stream_id: typeof value.stream_id === "string" && STREAM_ID_PATTERN.test(value.stream_id) ? value.stream_id : null,
    profile: PROFILES.includes(value.profile) ? value.profile : null,
    state: str(value.state, 40),
    width: nonNegInt(value.width),
    height: nonNegInt(value.height),
    codec: str(value.codec, 40),
    epoch: int(value.epoch),
    buffer_bytes: nonNegInt(value.buffer_bytes),
    error_code: str(value.error_code, 80),
  };
}

/** Reads an `open_stream` result: {ok, errorCode, error, hint, stream}. */
export function parseOpenStreamResult(result) {
  const outcome = toolOutcome(result);
  const stream = streamView(outcome.sc.stream);
  const ok = outcome.ok && Boolean(stream && stream.stream_id);
  return {
    ok,
    errorCode: ok ? null : outcome.errorCode || "malformed_result",
    error: outcome.error,
    hint: outcome.hint,
    stream,
  };
}

/**
 * Reads a `get_frames` result (contract 5.3): structured `stream`, `frames`, `bytes`, `first_seq`,
 * `last_seq`, `resync`, `dropped`, `server_ms` and `_meta["autonom/frames"]` `{config_b64, frames: [{seq,
 * key, pts, data_b64}]}`. Frames come back sorted by `seq` with their bytes decoded; invalid entries are
 * counted in `invalid` and skipped.
 */
export function parseFramesResult(result) {
  const outcome = toolOutcome(result);
  const sc = outcome.sc;
  const stream = streamView(sc.stream);
  const payload = isObject(outcome.meta["autonom/frames"]) ? outcome.meta["autonom/frames"] : {};
  let config = null;
  let invalid = 0;
  if (typeof payload.config_b64 === "string" && payload.config_b64) {
    config = base64ToBytes(payload.config_b64);
    if (!config || !config.length) {
      config = null;
      invalid += 1;
    }
  }
  const frames = [];
  const seen = new Set();
  for (const entry of Array.isArray(payload.frames) ? payload.frames : []) {
    const seq = isObject(entry) ? entry.seq : null;
    const data = isObject(entry) ? base64ToBytes(entry.data_b64) : null;
    if (!Number.isInteger(seq) || seq < 1 || seen.has(seq) || !data || !data.length) {
      invalid += 1;
      continue;
    }
    seen.add(seq);
    const pts = num(entry.pts);
    frames.push({ seq, key: entry.key === true, pts: pts === null ? 0 : pts, data });
  }
  frames.sort((a, b) => a.seq - b.seq);
  const bytes = frames.reduce((sum, f) => sum + f.data.length, 0);
  const lastSeq = nonNegInt(sc.last_seq) ?? (frames.length ? frames[frames.length - 1].seq : null);
  const firstSeq = nonNegInt(sc.first_seq) ?? (frames.length ? frames[0].seq : null);
  const count = nonNegInt(sc.frames);
  return {
    ok: outcome.ok,
    errorCode: outcome.errorCode,
    error: outcome.ok ? null : outcome.error,
    hint: outcome.ok ? null : outcome.hint,
    stream,
    epoch: stream && stream.epoch !== null ? stream.epoch : int(sc.epoch),
    width: nonNegInt(sc.width) ?? (stream ? stream.width : null),
    height: nonNegInt(sc.height) ?? (stream ? stream.height : null),
    codec: (stream && stream.codec) || null,
    resync: sc.resync === true,
    dropped: nonNegInt(sc.dropped) ?? 0,
    serverMs: num(sc.server_ms),
    bytes: nonNegInt(sc.bytes) ?? bytes,
    firstSeq,
    lastSeq,
    config,
    frames,
    invalid,
    countMismatch: count !== null && count !== frames.length,
  };
}

/**
 * Turns parsed frames into WebCodecs chunk descriptors in Annex B form, as the Canvas page decodes them:
 * the latest config (SPS/PPS) is prepended to every key frame; delta frames pass through.
 * Returns [{seq, type: "key"|"delta", timestamp, data}] (timestamp: a non-negative integer).
 */
export function annexBChunks(frames, config) {
  const head = config instanceof Uint8Array && config.length ? config : null;
  const out = [];
  for (const frame of Array.isArray(frames) ? frames : []) {
    if (!frame || !(frame.data instanceof Uint8Array) || !frame.data.length) continue;
    let data = frame.data;
    if (frame.key && head) {
      data = new Uint8Array(head.length + frame.data.length);
      data.set(head);
      data.set(frame.data, head.length);
    }
    const ts = Number.isFinite(frame.pts) ? Math.max(0, Math.round(frame.pts)) : 0;
    out.push({ seq: frame.seq, type: frame.key ? "key" : "delta", timestamp: ts, data });
  }
  return out;
}

/**
 * The pull state after one `get_frames` answer: `afterSeq` and `epoch` for the next call, the config to
 * prepend, `reconfigure` (a resync, a new epoch or a changed config needs a fresh decoder setup) and
 * `resynced` (a resync after the first pull). Failed answers leave the state as it was.
 *
 * The server repeats `resync: true` with no frames until a key frame is buffered (for example after a new
 * session message from a Canvas Display menu change). Such an empty answer only marks the resync as pending
 * (`pendingResync`, with `pendingCounts` telling whether it came after the first pull): the decoder is not
 * reset and nothing is counted until the answer that delivers frames, so one resync event counts once.
 */
export function nextPullState(previous, parsed) {
  const prev = isObject(previous) ? previous : {};
  const next = {
    afterSeq: nonNegInt(prev.afterSeq) ?? 0,
    epoch: int(prev.epoch),
    config: prev.config instanceof Uint8Array ? prev.config : null,
    reconfigure: false,
    resynced: false,
    pendingResync: prev.pendingResync === true,
    pendingCounts: prev.pendingResync === true && prev.pendingCounts === true,
  };
  if (!parsed || !parsed.ok) return next;
  const started = next.afterSeq > 0 || next.epoch !== null;
  const epochChanged = Number.isInteger(parsed.epoch) && next.epoch !== null && parsed.epoch !== next.epoch;
  if (parsed.resync || epochChanged) {
    if (!next.pendingResync) next.pendingCounts = started;
    next.pendingResync = true;
  }
  const hasFrames = Array.isArray(parsed.frames) && parsed.frames.length > 0;
  if (next.pendingResync && hasFrames) {
    next.reconfigure = true;
    next.resynced = next.pendingCounts;
    next.pendingResync = false;
    next.pendingCounts = false;
  }
  if (Number.isInteger(parsed.epoch)) next.epoch = parsed.epoch;
  if (parsed.config && !sameBytes(parsed.config, next.config)) {
    next.config = parsed.config;
    next.reconfigure = true;
  }
  if (Number.isInteger(parsed.lastSeq) && (parsed.resync || epochChanged || parsed.lastSeq > next.afterSeq)) {
    next.afterSeq = parsed.lastSeq;
  }
  return next;
}

/** Arguments of the next `get_frames` call. */
export function framesArgs(streamId, state, { waitMs = 250, maxBytes } = {}) {
  const after = state && Number.isInteger(state.afterSeq) && state.afterSeq > 0 ? state.afterSeq : 0;
  const args = { stream_id: streamId, after_seq: after, wait_ms: waitMs };
  if (state && Number.isInteger(state.epoch)) args.epoch = state.epoch;
  if (Number.isInteger(maxBytes)) args.max_bytes = maxBytes;
  return args;
}

// ------------------------------------------------------------------------------------------- input

/**
 * Maps a pointer position to normalized device coordinates (0..1) over the picture drawn into `rect`
 * (a bounding rectangle {left, top, width, height}). With the video size the picture is taken to be
 * letterboxed as `object-fit: contain`. Returns null outside the picture or for unusable input.
 */
export function normalizePoint(clientX, clientY, rect, videoWidth, videoHeight) {
  if (!isObject(rect) || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return null;
  const left = Number(rect.left);
  const top = Number(rect.top);
  const width = Number(rect.width);
  const height = Number(rect.height);
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null;
  let drawnW = width;
  let drawnH = height;
  if (Number.isFinite(videoWidth) && Number.isFinite(videoHeight) && videoWidth > 0 && videoHeight > 0) {
    const scale = Math.min(width / videoWidth, height / videoHeight);
    drawnW = videoWidth * scale;
    drawnH = videoHeight * scale;
  }
  const x = (clientX - (left + (width - drawnW) / 2)) / drawnW;
  const y = (clientY - (top + (height - drawnH) / 2)) / drawnH;
  const eps = 1e-9;
  if (x < -eps || y < -eps || x > 1 + eps || y > 1 + eps) return null;
  const clamp = (v) => Math.round(Math.min(1, Math.max(0, v)) * 100000) / 100000;
  return { x: clamp(x), y: clamp(y) };
}

export function tapEvent(point) {
  return { op: "tap", x: point.x, y: point.y };
}

export function systemEvent(button) {
  if (!SYSTEM_BUTTONS.includes(button)) throw new TypeError(`Unknown system button ${button}`);
  return { op: "system", button };
}

// ---------------------------------------------------------------------------------------- measurements

const SAMPLE_CAP = 5000;

export function pushCapped(list, value, cap = SAMPLE_CAP) {
  if (list.length >= cap) list.shift();
  list.push(value);
}

/** A per-profile measurement record the pull loop fills (the readouts of contract section 6). */
export function createMeter(profile, startedAt) {
  return {
    profile, started_at: Number.isFinite(startedAt) ? startedAt : null, ended_at: null,
    calls: 0, call_errors: 0, call_ms: [], frames_received: 0, frames_painted: 0, key_frames: 0,
    bytes: 0, largest_packet: 0, key_frame_bytes: 0, resyncs: 0, dropped_server: 0, dropped_decoder: 0,
    decoder_errors: 0, frame_age_ms: [], gaps: 0, codec: null, width: null, height: null, error_code: null,
  };
}

/**
 * Records one finished `get_frames` call (its parsed answer, its duration and the `after_seq` it was made
 * with) into `meter`. `resynced` is the same flag of `nextPullState` for this answer: a resync counts once,
 * on the answer that delivers its frames and only after the first pull, never on the empty `resync: true`
 * answers before the key frame. Without the flag, an answer with `resync: true`, frames and a non-zero
 * `after_seq` counts. A focus answer that skips sequence numbers without a resync counts as a gap.
 */
export function recordPull(meter, parsed, callMs, afterSeqBefore, resynced) {
  meter.calls += 1;
  if (Number.isFinite(callMs)) pushCapped(meter.call_ms, Math.max(0, callMs));
  if (!parsed || !parsed.ok) {
    meter.call_errors += 1;
    if (parsed && parsed.errorCode) meter.error_code = parsed.errorCode;
    return meter;
  }
  const before = Number.isInteger(afterSeqBefore) ? afterSeqBefore : 0;
  const counted = typeof resynced === "boolean" ? resynced : parsed.resync && before > 0 && parsed.frames.length > 0;
  if (counted) meter.resyncs += 1;
  meter.dropped_server += parsed.dropped || 0;
  if (!parsed.resync && !counted && meter.profile === "focus" && parsed.frames.length && before > 0 && parsed.frames[0].seq > before + 1) {
    meter.gaps += 1;
  }
  for (const frame of parsed.frames) {
    meter.frames_received += 1;
    meter.bytes += frame.data.length;
    meter.largest_packet = Math.max(meter.largest_packet, frame.data.length);
    if (frame.key) {
      meter.key_frames += 1;
      meter.key_frame_bytes = Math.max(meter.key_frame_bytes, frame.data.length);
    }
  }
  if (parsed.codec) meter.codec = parsed.codec;
  if (parsed.width) meter.width = parsed.width;
  if (parsed.height) meter.height = parsed.height;
  return meter;
}

/**
 * The measurement summary for the readouts and the diagnostics: frames/s painted, median and p95 call ms,
 * bytes/s, frame age (pull answer to paint, median and p95), resyncs, drops, packet sizes.
 */
export function summarizeMeter(meter, nowMs) {
  if (!meter) return null;
  const end = Number.isFinite(meter.ended_at) ? meter.ended_at : nowMs;
  const seconds = Number.isFinite(meter.started_at) && Number.isFinite(end) ? Math.max(0, (end - meter.started_at) / 1000) : 0;
  const per = (n) => (seconds > 0 ? round1(n / seconds) : null);
  return {
    profile: meter.profile,
    duration_s: round1(seconds),
    calls: meter.calls,
    call_errors: meter.call_errors,
    call_ms_median: round1(percentile(meter.call_ms, 50)),
    call_ms_p95: round1(percentile(meter.call_ms, 95)),
    frames_received: meter.frames_received,
    frames_painted: meter.frames_painted,
    frames_per_s: per(meter.frames_painted),
    bytes: meter.bytes,
    bytes_per_s: per(meter.bytes),
    largest_packet: meter.largest_packet,
    key_frames: meter.key_frames,
    key_frame_bytes: meter.key_frame_bytes,
    frame_age_ms_median: round1(percentile(meter.frame_age_ms, 50)),
    frame_age_ms_p95: round1(percentile(meter.frame_age_ms, 95)),
    resyncs: meter.resyncs,
    dropped: meter.dropped_server,
    dropped_decoder: meter.dropped_decoder,
    decoder_errors: meter.decoder_errors,
    gaps: meter.gaps,
    codec: meter.codec,
    width: meter.width,
    height: meter.height,
    error_code: meter.error_code,
  };
}

// ---------------------------------------------------------------------------------------- checks

export function emptyChecks() {
  const out = {};
  for (const id of CHECK_IDS) out[id] = { status: "not_exercised", detail: "Not exercised on this page" };
  return out;
}

/** Sets one check; unknown ids and statuses throw. Returns the record. */
export function setCheck(checks, id, status, detail = "") {
  if (!CHECK_IDS.includes(id)) throw new RangeError(`Unknown check ${id}`);
  if (!CHECK_STATUSES.includes(status)) throw new RangeError(`Unknown status ${status}`);
  checks[id] = { status, detail: String(detail || "").slice(0, 400) };
  return checks[id];
}

/** The media check for a profile's meter: pass, fail (the stream or decoder failed) or not exercised. */
export function mediaCheck(meter) {
  if (!meter) return { status: "not_exercised", detail: "No stream was opened with this profile" };
  const need = meter.profile === "thumb" ? THUMB_PASS_FRAMES : FOCUS_PASS_FRAMES;
  const painted = meter.frames_painted;
  if (painted >= need) return { status: "pass", detail: `${painted} frames decoded and painted` };
  if (meter.error_code || meter.decoder_errors) {
    return { status: "fail", detail: `${painted} frames painted; ${meter.error_code || "decoder_error"}` };
  }
  return { status: "not_exercised", detail: `${painted} of ${need} frames painted so far` };
}

// ---------------------------------------------------------------------------------------- diagnostics

const ABSOLUTE_PATH = /(^|[\s"'`(=:,;[{<>|])(\/(?:Users|home|private|var|tmp|opt|etc|Volumes|root|Library|System|mnt|usr|srv|run)(?![A-Za-z0-9])|\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+|~\/|[A-Za-z]:[\\/])/;
const SECRET_HINT = /bearer[\s.]|token=|authorization:|access_token|refresh_token|client_secret|api[_-]?key/i;
const LONG_RANDOM = /[A-Za-z0-9+/_-]{32,}/g;
const UUID = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;

/** True when `text` looks like it carries a token or a local absolute path (the diagnostics must not). */
export function looksSecretOrPath(text) {
  if (typeof text !== "string") return false;
  if (/(^|[^A-Za-z])file:/i.test(text)) return true;
  if (SECRET_HINT.test(text) || ABSOLUTE_PATH.test(text)) return true;
  for (const run of text.match(LONG_RANDOM) || []) {
    if (UUID.test(run)) continue; // device ids such as Simulator UDIDs are not secrets
    if (/[0-9]/.test(run) && /[A-Za-z]/.test(run)) return true;
  }
  return false;
}

/** Deep copy of a JSON value without the strings (and keys) that look like tokens or paths: {value, removed}. */
export function dropSecrets(value) {
  let removed = 0;
  const walk = (v, depth) => {
    if (depth > 12) {
      removed += 1;
      return undefined;
    }
    if (typeof v === "string") {
      if (looksSecretOrPath(v)) {
        removed += 1;
        return undefined;
      }
      return v.slice(0, 2000);
    }
    if (typeof v === "number") return Number.isFinite(v) ? v : null;
    if (typeof v === "boolean" || v === null) return v;
    if (Array.isArray(v)) {
      const out = [];
      for (const item of v.slice(0, 500)) {
        const inner = walk(item, depth + 1);
        if (inner !== undefined) out.push(inner);
      }
      return out;
    }
    if (isObject(v)) {
      const out = {};
      for (const [key, inner] of Object.entries(v)) {
        if (looksSecretOrPath(key)) {
          removed += 1;
          continue;
        }
        const kept = walk(inner, depth + 1);
        if (kept !== undefined) out[key] = kept;
      }
      return out;
    }
    return undefined;
  };
  return { value: walk(value, 0), removed };
}

/**
 * The `save_diagnostics` results (schema 1): run id, page and adapter versions, environment, host context,
 * measurements per profile and the six checks as [{id, status, detail}] in CHECK_IDS order, plus a summary
 * count. Strings that look like tokens or absolute paths are dropped (counted in `removed_strings`); ids are
 * kept only when they match their patterns.
 */
export function buildDiagnostics({ runId, nowMs, adapterVersion, environment, host, session, measurements, checks } = {}) {
  const created = Number.isFinite(nowMs) ? nowMs : Date.now();
  const id = typeof runId === "string" && DIAG_RUN_ID_PATTERN.test(runId) ? runId : makeRunId(created);
  const source = isObject(checks) ? checks : {};
  const h = isObject(host) ? host : {};
  const s = isObject(session) ? session : {};
  const m = isObject(measurements) ? measurements : {};
  const raw = {
    environment: isObject(environment) ? environment : {},
    host: {
      name: str(h.name, 80), version: str(h.version, 80), protocol: str(h.protocol, 40),
      entry: ["global", "thread", "unknown"].includes(h.entry) ? h.entry : "unknown",
      display_mode: str(h.display_mode, 40), theme: h.theme === "light" || h.theme === "dark" ? h.theme : null,
    },
    measurements: { focus: isObject(m.focus) ? m.focus : null, thumb: isObject(m.thumb) ? m.thumb : null },
    details: CHECK_IDS.map((checkId) => (isObject(source[checkId]) && typeof source[checkId].detail === "string"
      ? source[checkId].detail.slice(0, 400) : "")),
  };
  const { value, removed } = dropSecrets(raw);
  // dropSecrets removes array items, so details are cleaned one by one to keep their positions.
  const checkList = CHECK_IDS.map((checkId, index) => {
    const record = isObject(source[checkId]) ? source[checkId] : {};
    const detail = raw.details[index];
    return {
      id: checkId,
      status: CHECK_STATUSES.includes(record.status) ? record.status : "not_exercised",
      detail: looksSecretOrPath(detail) ? "" : detail,
    };
  });
  const counts = { pass: 0, fail: 0, not_exercised: 0 };
  for (const check of checkList) counts[check.status] += 1;
  return {
    schema_version: DIAGNOSTICS_SCHEMA_VERSION,
    run_id: id,
    created_at: new Date(created).toISOString().replace(/\.\d{3}Z$/, "Z"),
    page_version: WORKSPACE_CORE_VERSION,
    adapter_version: typeof adapterVersion === "string" && /^[0-9A-Za-z.+-]{1,40}$/.test(adapterVersion) ? adapterVersion : null,
    host_id: typeof h.host_id === "string" && HOST_ID_PATTERN.test(h.host_id) ? h.host_id : null,
    session: typeof s.session_id === "string" && SESSION_ID_PATTERN.test(s.session_id) ? {
      session_id: s.session_id,
      platform: s.platform === "android" || s.platform === "ios" ? s.platform : null,
    } : null,
    environment: value.environment,
    host: value.host,
    measurements: value.measurements,
    checks: checkList,
    summary: counts,
    removed_strings: removed,
  };
}

// ---------------------------------------------------------------------------------------- bridge payloads

export function initializeParams(version = WORKSPACE_CORE_VERSION) {
  return {
    appInfo: { name: "autonom-workspace-dev", version },
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
    protocolVersion: UI_PROTOCOL_VERSION,
  };
}

/** Entry point from the host context, when the host reports one; else "unknown". */
export function detectEntry(hostContext) {
  const ctx = isObject(hostContext) ? hostContext : {};
  const candidates = [ctx.entrypoint, ctx.entryPoint, ctx.entry, ctx["openai/entrypoint"],
    ctx.toolInfo && ctx.toolInfo.entrypoint, ctx.location];
  for (const value of candidates) {
    const text = typeof value === "string" ? value : value && typeof value.type === "string" ? value.type : "";
    if (/^(global|sidebar)$/i.test(text)) return "global";
    if (/^(thread|conversation|chat)$/i.test(text)) return "thread";
  }
  return "unknown";
}

/** Session entries of a `list_sessions` (or `open_workspace`) result with valid ids: {ok, sessions}. */
export function readSessions(result) {
  const { ok, sc } = toolOutcome(result);
  let source = [];
  if (Array.isArray(sc.sessions)) source = sc.sessions;
  else if (isObject(sc.workspace) && Array.isArray(sc.workspace.sessions)) source = sc.workspace.sessions;
  const sessions = [];
  for (const entry of source) {
    if (!isObject(entry) || typeof entry.session_id !== "string" || !SESSION_ID_PATTERN.test(entry.session_id)) continue;
    if (typeof entry.host_id !== "string" || !HOST_ID_PATTERN.test(entry.host_id)) continue;
    sessions.push({
      host_id: entry.host_id, session_id: entry.session_id,
      platform: entry.platform === "android" || entry.platform === "ios" ? entry.platform : null,
      target_id: str(entry.target_id, 128), label: str(entry.label, 64), live: entry.live !== false,
    });
  }
  return { ok, sessions };
}

/** The local host id of a `list_hosts` or `open_workspace` result, or null. */
export function readHostId(result) {
  const { sc } = toolOutcome(result);
  const workspace = isObject(sc.workspace) ? sc.workspace : {};
  const candidates = [sc.host_id, workspace.host_id];
  const hosts = Array.isArray(sc.hosts) ? sc.hosts : Array.isArray(workspace.hosts) ? workspace.hosts : [];
  for (const h of hosts) if (isObject(h) && h.kind === "local") candidates.push(h.host_id);
  for (const value of candidates) if (typeof value === "string" && HOST_ID_PATTERN.test(value)) return value;
  return null;
}

// ---------------------------------------------------------------------------------------- bridge client

export class BridgeError extends Error {
  constructor(code, message, data = null) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.data = data;
  }
}

/**
 * JSON-RPC 2.0 over postMessage (MCP Apps), reduced from the probe's client. `post(message)` sends to the
 * parent; `addListener(handler)` subscribes to message events. Only events whose source is `parent` and
 * whose data has jsonrpc "2.0" are processed.
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

// ======================================================================================== page view
// Everything below runs only in the page (a document exists). The page never sees the Canvas token: it
// only reads tool results that come through the host bridge.

/**
 * Starts the page view on `doc` and `win` (the browser's document and window; tests pass fakes). Returns
 * a handle with `stop()` that ends the readout timer, the frame loop and the bridge.
 */
export function startPage(doc, win) {
  const $ = (id) => doc.getElementById(id);
  const config = readConfig(doc) || {};
  const now = () => (win.performance && typeof win.performance.now === "function" ? win.performance.now() : Date.now());
  const S = {
    bridge: null, toolPath: null, init: {}, hostContext: {}, theme: null,
    hostId: null, adapterVersion: typeof config.adapter_version === "string" ? config.adapter_version : null,
    sessions: [], session: null, stream: null, loop: null, streamGen: 0, decoder: null, codec: null,
    waitingKey: true, pull: { afterSeq: 0, epoch: null, config: null }, meters: { focus: null, thumb: null },
    meter: null, fps: fpsCounter(1000), inflight: new Map(), checks: emptyChecks(), saving: false,
  };

  // ------------------------------------------------------------------ rendering
  function text(id, value) {
    const el = $(id);
    if (el) el.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
  }
  function notice(message, kind = "info") {
    const el = $("notice");
    if (!el) return;
    el.textContent = message || "";
    el.dataset.kind = kind;
    el.hidden = !message;
  }
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") doc.documentElement.dataset.theme = theme;
    else delete doc.documentElement.dataset.theme;
  }
  function applyHostContext(ctx) {
    if (!isObject(ctx)) return;
    S.hostContext = { ...S.hostContext, ...ctx };
    if (ctx.theme === "light" || ctx.theme === "dark") {
      S.theme = ctx.theme;
      applyTheme(ctx.theme);
    }
    renderHeader();
  }
  function renderHeader() {
    text("adapter-version", S.adapterVersion);
    text("host-id", S.hostId);
    const entry = detectEntry(S.hostContext);
    text("entry", entry === "global" ? "Sidebar (global)" : entry === "thread" ? "Conversation (thread)" : "Not reported");
  }
  function renderSessions() {
    const list = $("sessions");
    if (list) {
      list.replaceChildren();
      if (!S.sessions.length) {
        const empty = doc.createElement("li");
        empty.className = "empty";
        empty.textContent = "No live sessions. Ask the chat to start one, then refresh.";
        list.append(empty);
      }
      for (const entry of S.sessions) {
        const item = doc.createElement("li");
        const button = doc.createElement("button");
        button.type = "button";
        button.className = "session";
        button.setAttribute("aria-pressed", String(Boolean(S.session && S.session.session_id === entry.session_id)));
        button.textContent = `${entry.label || entry.target_id || entry.session_id} - ${entry.platform || "?"} ${entry.target_id || ""} (${entry.session_id})`;
        button.addEventListener("click", () => chooseSession(entry));
        item.append(button);
        list.append(item);
      }
    }
    const chosen = Boolean(S.session);
    for (const id of ["start-focus", "start-thumb", "pause", "resume", "home", "back"]) {
      const el = $(id);
      if (el) el.disabled = !chosen;
    }
    const stop = $("stop-stream");
    if (stop) stop.disabled = !S.stream;
    text("session-chosen", S.session ? `${S.session.session_id} (${S.session.platform || "?"} ${S.session.target_id || ""})` : null);
    text("stream-state", S.stream ? `${S.stream.profile} ${S.stream.stream_id}` : "No stream");
  }
  function renderChecks() {
    const list = $("checks");
    if (!list) return;
    list.replaceChildren();
    for (const check of CHECKS) {
      const record = S.checks[check.id];
      const item = doc.createElement("li");
      item.dataset.status = record.status;
      const name = doc.createElement("b");
      name.textContent = check.label;
      const status = doc.createElement("span");
      status.className = "status";
      status.textContent = record.status.replace("_", " ");
      const detail = doc.createElement("span");
      detail.className = "detail";
      detail.textContent = record.detail;
      item.append(name, status, detail);
      list.append(item);
    }
  }
  function renderReadouts() {
    const meter = S.meter;
    const t = now();
    const summary = meter ? summarizeMeter(meter, t) : null;
    text("ro-profile", meter ? meter.profile : null);
    text("ro-fps", meter && S.stream ? round1(S.fps.rate(t)) : summary ? summary.frames_per_s : null);
    text("ro-call", summary && summary.call_ms_median !== null ? `${summary.call_ms_median} / ${summary.call_ms_p95}` : null);
    text("ro-bytes", summary && summary.bytes_per_s !== null ? Math.round(summary.bytes_per_s) : null);
    text("ro-age", summary && summary.frame_age_ms_median !== null ? `${summary.frame_age_ms_median} / ${summary.frame_age_ms_p95}` : null);
    text("ro-resyncs", summary ? summary.resyncs : null);
    text("ro-dropped", summary ? `${summary.dropped} server, ${summary.dropped_decoder} decoder` : null);
    text("ro-frames", summary ? `${summary.frames_painted} painted of ${summary.frames_received}` : null);
    text("ro-codec", summary ? summary.codec || S.codec : null);
  }
  function updateMediaChecks() {
    for (const profile of PROFILES) {
      const result = mediaCheck(S.meters[profile]);
      setCheck(S.checks, `media.${profile}`, result.status, result.detail);
    }
    renderChecks();
  }

  // ------------------------------------------------------------------ tool path
  async function callTool(name, args = {}, timeoutMs = 45000) {
    if (S.toolPath === "bridge") return S.bridge.request("tools/call", { name, arguments: args }, { timeoutMs });
    const openai = win.openai;
    if (S.toolPath === "openai" && openai && typeof openai.callTool === "function") {
      let timer;
      try {
        return await Promise.race([
          openai.callTool(name, args),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new BridgeError("bridge_timeout", `${name}: no answer`)), timeoutMs); }),
        ]);
      } finally {
        clearTimeout(timer);
      }
    }
    throw new BridgeError("bridge_error", "No tool path: the host bridge did not answer ui/initialize");
  }
  function failText(outcome) {
    return `${outcome.errorCode || "error"}${outcome.error ? `: ${outcome.error}` : ""}${outcome.hint ? ` (${outcome.hint})` : ""}`;
  }
  function errorText(error) {
    return `${(error && error.code) || "bridge_error"}: ${(error && error.message) || ""}`;
  }

  // ------------------------------------------------------------------ sessions
  async function refreshSessions() {
    try {
      const hostId = readHostId(await callTool("list_hosts", {}));
      if (hostId) S.hostId = hostId;
    } catch {
      // Every session entry carries the host id as well.
    }
    let result;
    try {
      result = await callTool("list_sessions", {});
    } catch (error) {
      setCheck(S.checks, "bridge.list_sessions", "fail", errorText(error));
      renderChecks();
      notice(`list_sessions did not answer: ${errorText(error)}`, "error");
      return;
    }
    const { ok, sessions } = readSessions(result);
    if (!ok) {
      const outcome = toolOutcome(result);
      setCheck(S.checks, "bridge.list_sessions", "fail", failText(outcome));
      notice(`list_sessions failed: ${failText(outcome)}`, "error");
    } else {
      S.sessions = sessions.filter((s) => s.live);
      if (!S.hostId && S.sessions[0]) S.hostId = S.sessions[0].host_id;
      if (S.toolPath === "bridge") setCheck(S.checks, "bridge.list_sessions", "pass", `${S.sessions.length} live sessions listed`);
      else setCheck(S.checks, "bridge.list_sessions", "not_exercised", "Listed through window.openai, not the MCP Apps bridge");
      if (S.session && !S.sessions.some((s) => s.session_id === S.session.session_id)) {
        await stopStream();
        S.session = null;
      }
      if (!S.session && S.sessions.length === 1) S.session = S.sessions[0];
    }
    renderHeader();
    renderSessions();
    renderChecks();
  }
  async function chooseSession(entry) {
    if (S.session && S.session.session_id !== entry.session_id) {
      // Set the new session first so a stream that is still opening for the old one is dropped.
      S.session = entry;
      await stopStream();
    }
    S.session = entry;
    renderSessions();
  }
  function ref() {
    return { host_id: S.session.host_id, session_id: S.session.session_id };
  }

  // ------------------------------------------------------------------ decoder
  function closeDecoder() {
    if (S.decoder && S.decoder.state !== "closed") {
      try { S.decoder.close(); } catch { /* already closed */ }
    }
    S.decoder = null;
    S.codec = null;
    S.inflight.clear();
  }
  function onFrame(frame) {
    try {
      const canvas = $("screen");
      if (canvas) {
        const w = frame.displayWidth || frame.codedWidth;
        const h = frame.displayHeight || frame.codedHeight;
        if (canvas.width !== w || canvas.height !== h) {
          canvas.width = w;
          canvas.height = h;
        }
        canvas.getContext("2d").drawImage(frame, 0, 0, w, h);
      }
      if (S.meter) {
        S.meter.frames_painted += 1;
        const arrived = S.inflight.get(frame.timestamp);
        if (arrived !== undefined) {
          pushCapped(S.meter.frame_age_ms, Math.max(0, now() - arrived));
          S.inflight.delete(frame.timestamp);
        }
      }
      S.fps.tick(now());
    } finally {
      frame.close();
    }
  }
  function onDecoderError(error) {
    if (S.meter) S.meter.decoder_errors += 1;
    notice(`The video decoder failed: ${error && error.message ? error.message : "error"}. Waiting for the next key frame.`, "error");
    closeDecoder();
    S.waitingKey = true;
  }
  async function configureDecoder(configBytes) {
    const codec = codecFromSps(configBytes);
    if (!codec) {
      notice("The stream config has no H.264 SPS; nothing can be decoded.", "error");
      return false;
    }
    if (typeof win.VideoDecoder !== "function" || typeof win.EncodedVideoChunk !== "function") {
      notice("WebCodecs VideoDecoder is not available in this view.", "error");
      if (S.meter) S.meter.error_code = "webcodecs_unavailable";
      return false;
    }
    try {
      const support = await win.VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true });
      if (!support || !support.supported) {
        notice(`VideoDecoder rejects ${codec}.`, "error");
        if (S.meter) S.meter.error_code = "codec_unsupported";
        return false;
      }
      if (!S.decoder || S.decoder.state === "closed") S.decoder = new win.VideoDecoder({ output: onFrame, error: onDecoderError });
      S.decoder.configure({ codec, optimizeForLatency: true });
      S.codec = codec;
      S.waitingKey = true;
      return true;
    } catch (error) {
      notice(`VideoDecoder setup failed: ${error && error.message ? error.message : "error"}`, "error");
      if (S.meter) S.meter.decoder_errors += 1;
      return false;
    }
  }
  function decodeChunks(chunks, arrivedAt) {
    const meter = S.meter;
    for (const chunk of chunks) {
      if (!S.decoder || S.decoder.state !== "configured") {
        if (meter) meter.dropped_decoder += 1;
        S.waitingKey = true;
        continue;
      }
      if (S.waitingKey && chunk.type !== "key") {
        if (meter) meter.dropped_decoder += 1;
        continue;
      }
      if (chunk.type !== "key" && S.decoder.decodeQueueSize > 30) {
        if (meter) meter.dropped_decoder += 1;
        S.waitingKey = true;
        continue;
      }
      try {
        S.inflight.set(chunk.timestamp, arrivedAt);
        if (S.inflight.size > 120) S.inflight.delete(S.inflight.keys().next().value);
        S.decoder.decode(new win.EncodedVideoChunk({ type: chunk.type, timestamp: chunk.timestamp, data: chunk.data }));
        S.waitingKey = false;
      } catch (error) {
        onDecoderError(error);
        return;
      }
    }
  }

  // ------------------------------------------------------------------ stream and pull loop
  // Every Start, Stop, session change and teardown takes a new stream generation. A start whose
  // generation or session is no longer current when open_stream answers closes what it opened and
  // changes nothing, so only the latest request can own S.stream and the pull loop.
  function streamCurrent(gen, sessionId) {
    return gen === S.streamGen && Boolean(S.session) && S.session.session_id === sessionId;
  }
  async function startStream(profile) {
    if (!S.session) return;
    const gen = ++S.streamGen;
    const sessionRef = ref();
    await teardownStream();
    if (!streamCurrent(gen, sessionRef.session_id)) return;
    notice("");
    const meter = createMeter(profile, now());
    let opened;
    try {
      opened = parseOpenStreamResult(await callTool("open_stream", { ...sessionRef, profile }));
    } catch (error) {
      opened = { ok: false, errorCode: (error && error.code) || "bridge_error", error: (error && error.message) || "", hint: null };
    }
    if (!streamCurrent(gen, sessionRef.session_id)) {
      if (opened.ok && opened.stream && opened.stream.stream_id) {
        callTool("close_stream", { stream_id: opened.stream.stream_id }, 10000).catch(() => { /* the stream closes when idle */ });
      }
      return;
    }
    S.meters[profile] = meter;
    S.meter = meter;
    S.fps.reset();
    if (!opened.ok) {
      meter.error_code = opened.errorCode;
      meter.ended_at = now();
      notice(`open_stream failed: ${failText(opened)}`, "error");
      updateMediaChecks();
      renderReadouts();
      return;
    }
    S.stream = opened.stream;
    S.pull = { afterSeq: 0, epoch: null, config: null };
    closeDecoder();
    const token = {};
    S.loop = token;
    renderSessions();
    pullLoop(token, opened.stream.stream_id, profile, meter).catch((error) => {
      notice(`The frame loop stopped: ${error && error.message ? error.message : "error"}`, "error");
    });
  }
  function endStream(meter, message) {
    notice(message, "error");
    meter.ended_at = now();
    S.loop = null;
    S.stream = null;
    closeDecoder();
    updateMediaChecks();
    renderSessions();
  }
  async function pullLoop(token, streamId, profile, meter) {
    let failures = 0;
    while (S.loop === token) {
      const args = framesArgs(streamId, S.pull, { waitMs: profile === "thumb" ? 1000 : 250 });
      const started = now();
      let parsed;
      try {
        parsed = parseFramesResult(await callTool("get_frames", args, 15000));
      } catch (error) {
        parsed = { ok: false, errorCode: (error && error.code) || "bridge_error", error: (error && error.message) || "", hint: null, frames: [] };
      }
      const arrived = now();
      if (S.loop !== token) break;
      const next = nextPullState(S.pull, parsed);
      recordPull(meter, parsed, arrived - started, S.pull.afterSeq, next.resynced);
      if (!parsed.ok) {
        failures += 1;
        if (FATAL_STREAM_CODES.includes(parsed.errorCode) || failures >= 5) {
          endStream(meter, `The stream ended: ${failText(parsed)}. Start a new stream.`);
          break;
        }
        await new Promise((resolve) => win.setTimeout(resolve, 500));
        continue;
      }
      failures = 0;
      if (parsed.stream && parsed.stream.state === "failed") {
        meter.error_code = parsed.stream.error_code || "media_unavailable";
        endStream(meter, "The stream failed on the server. Start a new stream.");
        break;
      }
      S.pull = {
        afterSeq: next.afterSeq, epoch: next.epoch, config: next.config,
        pendingResync: next.pendingResync, pendingCounts: next.pendingCounts,
      };
      if ((next.reconfigure || !S.decoder) && next.config) {
        closeDecoder();
        const configured = await configureDecoder(next.config);
        if (S.loop !== token) break;
        if (!configured && ["webcodecs_unavailable", "codec_unsupported"].includes(meter.error_code)) {
          endStream(meter, `This view cannot decode the stream (${meter.error_code}).`);
          break;
        }
      }
      if (parsed.frames.length && S.pull.config) {
        decodeChunks(annexBChunks(parsed.frames, S.pull.config), arrived);
        // A thumb gets one key frame at a time: flush so the decoder paints it now.
        if (profile === "thumb" && S.decoder && S.decoder.state === "configured") S.decoder.flush().catch(() => {});
      }
      renderReadouts();
      updateMediaChecks();
    }
  }
  function stopStream() {
    S.streamGen += 1;
    return teardownStream();
  }
  async function teardownStream() {
    const stream = S.stream;
    S.loop = null;
    S.stream = null;
    if (S.meter && S.meter.ended_at === null) S.meter.ended_at = now();
    if (S.decoder && S.decoder.state === "configured") {
      try { await S.decoder.flush(); } catch { /* closed meanwhile */ }
    }
    closeDecoder();
    if (stream && stream.stream_id) {
      try { await callTool("close_stream", { stream_id: stream.stream_id }, 10000); } catch { /* the stream closes when idle */ }
    }
    if (S.meter) updateMediaChecks();
    renderReadouts();
    renderSessions();
  }

  // ------------------------------------------------------------------ input and control
  async function sendInput(event, checkId) {
    if (!S.session) return;
    let outcome;
    try {
      outcome = toolOutcome(await callTool("send_input", { ...ref(), event }, 20000));
    } catch (error) {
      notice(`send_input did not answer: ${errorText(error)}`, "error");
      if (checkId && S.checks[checkId].status !== "pass") setCheck(S.checks, checkId, "fail", errorText(error));
      renderChecks();
      return;
    }
    if (outcome.ok) {
      notice("");
      if (checkId) setCheck(S.checks, checkId, "pass", `${event.op} completed`);
    } else if (agentHasControl(outcome)) {
      notice(AGENT_HAS_CONTROL, "warn");
    } else {
      notice(`Input refused: ${failText(outcome)}`, "error");
      if (checkId && S.checks[checkId].status !== "pass") setCheck(S.checks, checkId, "fail", failText(outcome));
    }
    renderChecks();
  }
  async function control(tool, checkId) {
    if (!S.session) return;
    let outcome;
    try {
      outcome = toolOutcome(await callTool(tool, ref(), 20000));
    } catch (error) {
      setCheck(S.checks, checkId, "fail", errorText(error));
      notice(`${tool} did not answer: ${errorText(error)}`, "error");
      renderChecks();
      return;
    }
    if (outcome.ok) {
      const view = controlView(outcome.sc);
      setCheck(S.checks, checkId, "pass", view ? `lease ${view.state || "?"}, generation ${view.generation ?? "?"}` : "completed");
      notice(tool === "pause_session" ? "Paused: you have control of the device." : "Resumed: the agent may act again.");
    } else {
      setCheck(S.checks, checkId, "fail", failText(outcome));
      notice(`${tool} failed: ${failText(outcome)}`, "error");
    }
    renderChecks();
  }

  // ------------------------------------------------------------------ diagnostics
  function environment() {
    const nav = win.navigator || {};
    return {
      user_agent: String(nav.userAgent || "").slice(0, 200),
      language: typeof nav.language === "string" ? nav.language : null,
      webcodecs: typeof win.VideoDecoder === "function" && typeof win.EncodedVideoChunk === "function",
      device_pixel_ratio: Number.isFinite(win.devicePixelRatio) ? win.devicePixelRatio : null,
      tool_path: S.toolPath,
    };
  }
  async function saveDiagnostics() {
    if (S.saving) return;
    S.saving = true;
    const t = now();
    const info = isObject(S.init.hostInfo) ? S.init.hostInfo : {};
    const results = buildDiagnostics({
      runId: makeRunId(Date.now()),
      nowMs: Date.now(),
      adapterVersion: S.adapterVersion,
      environment: environment(),
      host: {
        name: info.name, version: info.version, protocol: S.init.protocolVersion,
        entry: detectEntry(S.hostContext), display_mode: S.hostContext.displayMode, theme: S.theme, host_id: S.hostId,
      },
      session: S.session,
      measurements: { focus: summarizeMeter(S.meters.focus, t), thumb: summarizeMeter(S.meters.thumb, t) },
      checks: S.checks,
    });
    try {
      const outcome = toolOutcome(await callTool("save_diagnostics", { results }, 20000));
      if (outcome.ok) {
        text("saved", `${results.run_id} (${isObject(outcome.sc.saved) ? outcome.sc.saved.bytes : "?"} bytes)`);
        notice("");
      } else {
        notice(`save_diagnostics failed: ${failText(outcome)}`, "error");
      }
    } catch (error) {
      notice(`save_diagnostics did not answer: ${errorText(error)}`, "error");
    } finally {
      S.saving = false;
    }
  }

  // ------------------------------------------------------------------ wiring
  function on(id, handler) {
    const el = $(id);
    if (el) el.addEventListener("click", handler);
  }
  on("refresh", () => refreshSessions());
  on("start-focus", () => startStream("focus"));
  on("start-thumb", () => startStream("thumb"));
  on("stop-stream", () => stopStream());
  on("home", () => sendInput(systemEvent("home"), null));
  on("back", () => sendInput(systemEvent("back"), null));
  on("pause", () => control("pause_session", "control.pause"));
  on("resume", () => control("resume_session", "control.resume"));
  on("save", () => saveDiagnostics());
  const canvas = $("screen");
  if (canvas) {
    canvas.addEventListener("click", (event) => {
      if (!S.session || !S.stream || !canvas.width || !canvas.height) return;
      const point = normalizePoint(event.clientX, event.clientY, canvas.getBoundingClientRect(), canvas.width, canvas.height);
      if (point) sendInput(tapEvent(point), "input.tap");
    });
  }
  const readoutTimer = win.setInterval(renderReadouts, 1000);

  async function boot() {
    renderHeader();
    renderSessions();
    renderChecks();
    renderReadouts();
    if (win.parent && win.parent !== win) {
      const bridge = new BridgeClient({
        post: (message) => win.parent.postMessage(message, "*"),
        addListener: (handler) => win.addEventListener("message", handler),
        parent: win.parent,
        timeoutMs: 8000,
      });
      S.bridge = bridge;
      bridge.onNotification("ui/notifications/tool-input", () => {});
      bridge.onNotification("ui/notifications/tool-result", (params) => {
        const hostId = readHostId(params);
        if (hostId) S.hostId = hostId;
        const { ok, sessions } = readSessions(params);
        if (ok && sessions.length && !S.sessions.length) {
          S.sessions = sessions.filter((s) => s.live);
          renderSessions();
        }
        renderHeader();
      });
      bridge.onNotification("ui/notifications/host-context-changed", (params) => applyHostContext(params));
      bridge.onRequest("ui/resource-teardown", async () => {
        await stopStream();
        return {};
      });
      try {
        const result = await bridge.request("ui/initialize", initializeParams(WORKSPACE_CORE_VERSION), { timeoutMs: 8000 });
        S.init = isObject(result) ? result : {};
        S.toolPath = "bridge";
        bridge.notify("ui/notifications/initialized", {});
        applyHostContext(S.init.hostContext || {});
      } catch {
        S.init = {};
      }
    }
    if (!S.toolPath && win.openai && typeof win.openai.callTool === "function") {
      S.toolPath = "openai";
      if (win.openai.theme === "light" || win.openai.theme === "dark") applyTheme(win.openai.theme);
    }
    if (!S.toolPath) {
      notice("Not running inside an MCP host: the bridge is not available.", "info");
      return;
    }
    await refreshSessions();
  }
  const booted = boot();
  return {
    booted,
    state: S,
    stop() {
      win.clearInterval(readoutTimer);
      S.streamGen += 1;
      S.loop = null;
      if (S.bridge) S.bridge.close();
    },
  };
}

if (typeof document !== "undefined" && typeof window !== "undefined" && document.getElementById("workspace")) {
  startPage(document, window);
}
