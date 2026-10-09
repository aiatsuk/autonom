/**
 * The Canvas activity log and tool health (contract section 5.4).
 *
 * `createCommandLog()` keeps the last COMMAND_LOG_MAX commands of a Canvas in memory: every
 * tools op and device command, with its duration, result and error. Nothing is written to
 * disk. An entry never holds typed text, payloads, mock bodies or tokens: its summary is cut
 * to 120 characters with every URL reduced to its scheme and host, and its error to 300.
 *
 * `activityApiRoute` is a `hooks.apiRoutes` entry for GET /api/activity, POST
 * /api/activity/clear and GET /api/health. Health comes from the primary device's tools
 * process (`health` op); with no device attached, from a short-lived
 * `python3 -m autonom_lib.tool_health` run (cached 60 s), so it works with zero devices.
 */
import { spawn } from "node:child_process";
import { resolve } from "node:path";

export const COMMAND_LOG_MAX = 1000;
export const ACTIVITY_READ_DEFAULT_LIMIT = 200;
export const ACTIVITY_READ_MAX_LIMIT = 500;
export const SUMMARY_MAX = 120;
export const ERROR_MAX = 300;
export const HEALTH_TIMEOUT_MS = 30_000;
export const HEALTH_CACHE_MS = 60_000;
export const ENTRY_KINDS = Object.freeze(["tools", "input", "control", "display", "device", "session", "boot",
  "workspace", "stream"]);

const KINDS = new Set(ENTRY_KINDS);
const DEVICE_ID = /^(android|ios)~[A-Za-z0-9._:-]{1,128}$/;
const NAME = /^[A-Za-z0-9._:/-]{1,64}$/;
const ORIGIN = /^[a-z]{1,16}$/;
const CODE = /^[a-z0-9_]{1,64}$/;
// A URL anywhere in a text: scheme, "://" and everything up to the next space.
const URL_IN_TEXT = /\b([A-Za-z][A-Za-z0-9+.-]{0,31}):\/\/([^\s]*)/g;
// Control characters (C0, DEL, C1) never reach an entry.
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/g;

/** `scheme://host` of a URL (no user, port, path, query or fragment). */
export function urlOrigin(text) {
  try {
    const parsed = new URL(String(text));
    const scheme = parsed.protocol.replace(/:$/, "");
    return parsed.hostname ? `${scheme}://${parsed.hostname}` : `${scheme}:`;
  } catch {
    const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(String(text));
    return scheme ? `${scheme[1]}:` : "";
  }
}

function hostOf(rest) {
  // rest: what follows "scheme://"; the host ends at / ? # and loses any user info.
  const authority = rest.split(/[/?#]/, 1)[0];
  const host = authority.slice(authority.lastIndexOf("@") + 1);
  const bracket = host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":", 1)[0];
  return bracket.toLowerCase();
}

/** A text safe for an entry: one line, URLs as scheme+host, at most `limit` characters. */
export function redactText(value, limit = SUMMARY_MAX) {
  if (value === null || value === undefined) return null;
  const text = String(value)
    .replace(CONTROL, " ")
    .replace(URL_IN_TEXT, (_match, scheme, rest) => `${scheme}://${hostOf(rest)}`)
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return null;
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function cleanName(value) {
  const text = String(value ?? "").slice(0, 64);
  return NAME.test(text) ? text : text.replace(/[^A-Za-z0-9._:/-]/g, "_") || "unknown";
}

/**
 * The command log of one Canvas: a ring of at most `max` entries, each with a seq.
 * `now()` returns epoch milliseconds (for `at` and `duration_ms`).
 */
export function createCommandLog({ max = COMMAND_LOG_MAX, now = () => Date.now() } = {}) {
  const entries = [];
  let seq = 0;
  // Seqs up to this one were cleared on purpose: never counted as dropped.
  let clearedThrough = 0;

  function begin({ device = null, kind = "tools", name, origin = null, summary = null } = {}) {
    const started = now();
    seq += 1;
    const entry = {
      seq,
      at: new Date(started).toISOString(),
      device: typeof device === "string" && DEVICE_ID.test(device) ? device : null,
      kind: KINDS.has(kind) ? kind : "device",
      name: cleanName(name),
      origin: typeof origin === "string" && ORIGIN.test(origin) ? origin : null,
      summary: redactText(summary, SUMMARY_MAX),
      ok: null,
      duration_ms: null,
      error_code: null,
      error: null,
      exit_code: null,
    };
    entries.push(entry);
    while (entries.length > max) entries.shift();
    let ended = false;
    return {
      entry,
      end({ ok, errorCode = null, error = null, exitCode = null } = {}) {
        if (ended) return entry;
        ended = true;
        entry.ok = Boolean(ok);
        entry.duration_ms = Math.max(0, Math.round(now() - started));
        entry.error_code = typeof errorCode === "string" && CODE.test(errorCode) ? errorCode : (errorCode ? "unknown" : null);
        entry.error = entry.ok ? null : redactText(error, ERROR_MAX);
        entry.exit_code = Number.isInteger(exitCode) ? exitCode : null;
        return entry;
      },
    };
  }

  /**
   * Entries with seq > `after` (oldest first), at most `limit`, optionally only failures and
   * only one device. `next` is the cursor for the next read: past the last entry returned, but
   * held before the first one still running, so a running entry is sent again once it ends.
   * `dropped` counts entries in (after, next] that left the ring before they were read.
   * `seen` is the highest seq a reader has already been past (the `seen` of its earlier
   * reads): entries up to it were shown, so a running entry that held the cursor back and
   * then left the ring never makes them count as dropped. Each read returns its own `seen`.
   */
  function list({ after = 0, limit = ACTIVITY_READ_DEFAULT_LIMIT, failures = false, device = null, seen = 0 } = {}) {
    const from = after > seq ? 0 : Math.max(0, after);
    const shown = Number.isInteger(seen) && seen > 0 && seen <= seq && after <= seq ? Math.max(seen, from) : from;
    const out = [];
    let index = entries.findIndex((entry) => entry.seq > from);
    if (index < 0) index = entries.length;
    let last = from;
    let cut = false;
    for (; index < entries.length; index += 1) {
      const entry = entries[index];
      if (out.length >= limit) {
        cut = true;
        break;
      }
      last = entry.seq;
      if (failures && entry.ok !== false) continue;
      if (device && entry.device !== device) continue;
      out.push({ ...entry });
    }
    let next = cut ? last : seq;
    const running = entries.find((entry) => entry.seq > from && entry.seq <= next && entry.ok === null &&
      (!device || entry.device === device));
    if (running) next = running.seq - 1;
    const oldest = entries.length ? entries[0].seq : seq + 1;
    const lowest = Math.max(shown, clearedThrough);
    const dropped = Math.max(0, Math.min(oldest - 1, next) - lowest);
    return { entries: out, next, dropped, total: entries.length, seen: Math.max(shown, last) };
  }

  function clear() {
    const count = entries.length;
    entries.length = 0;
    clearedThrough = seq;
    return count;
  }

  return { begin, list, clear, get size() { return entries.length; } };
}

function badRequest(error) {
  return { status: 400, body: { ok: false, error, error_code: "invalid_value", hint: null } };
}

function readInteger(raw, name, minimum, maximum, fallback) {
  if (raw === null) return { value: fallback };
  if (!/^\d{1,15}$/.test(raw)) return { error: badRequest(`${name} must be an integer from ${minimum} to ${maximum}`) };
  const value = Number(raw);
  if (value < minimum || value > maximum) return { error: badRequest(`${name} must be an integer from ${minimum} to ${maximum}`) };
  return { value };
}

/** Run `python3 -m autonom_lib.tool_health` in `scriptsDir`; resolves {checked_at, tools}. */
export function runToolHealth({ python = process.env.PYTHON ?? "python3", scriptsDir, env = process.env,
  timeoutMs = HEALTH_TIMEOUT_MS, refresh = false } = {}) {
  return new Promise((resolvePromise, reject) => {
    let child;
    try {
      child = spawn(python, ["-m", "autonom_lib.tool_health", ...(refresh ? ["--refresh"] : [])],
        { cwd: scriptsDir, env, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      reject(error);
      return;
    }
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch { /* gone */ }
      reject(new Error(`the tool check did not answer within ${Math.round(timeoutMs / 1000)} s`));
    }, timeoutMs);
    timer.unref?.();
    child.stdout.on("data", (chunk) => { if (stdout.length < 1_000_000) stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr = (stderr + chunk).slice(-2000); });
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => {
      clearTimeout(timer);
      try {
        const parsed = JSON.parse(stdout.trim().split("\n").pop() || "");
        if (code === 0 && parsed && Array.isArray(parsed.tools)) {
          resolvePromise({ checked_at: parsed.checked_at ?? null, tools: parsed.tools });
          return;
        }
      } catch { /* reported below */ }
      reject(new Error(`the tool check failed (exit ${code})${stderr.trim() ? `: ${stderr.trim().split("\n").pop()}` : ""}`));
    });
  });
}

const DEFAULT_CANVAS_FACTS = Object.freeze({
  scrcpy: { available: false, path: null, version: null, source: null, reason: null },
  idb_companion: { available: false, path: null, source: null, reason: null },
  ffmpeg: null,
});

function primaryTools(registry) {
  try {
    const primary = registry?.primary?.();
    return primary?.context?.tools ?? primary?.tools ?? null;
  } catch {
    return null;
  }
}

/**
 * The /api/activity and /api/health routes. Options (all for tests and wiring):
 * `runHealth({refresh})` replaces the python run; `canvasFacts(ctx)` gives the server's own
 * scrcpy, idb_companion and ffmpeg resolution (else `ctx.canvasFacts` or
 * `ctx.server.canvasFacts`, else "not resolved"); `now()` is the cache clock.
 */
export function createActivityApiRoute({
  python,
  scriptsDir = resolve(import.meta.dirname, "../../../../../scripts"),
  env = process.env,
  runHealth = null,
  canvasFacts = null,
  cacheMs = HEALTH_CACHE_MS,
  now = () => Date.now(),
} = {}) {
  let cached = null;
  let cachedAt = -Infinity;
  let pending = null;
  const fallbackHealth = (refresh) => {
    if (!refresh && cached && now() - cachedAt < cacheMs) return Promise.resolve(cached);
    if (pending) return pending;
    const run = runHealth ? runHealth({ refresh }) : runToolHealth({ python, scriptsDir, env, refresh });
    pending = Promise.resolve(run).then((value) => {
      cached = value;
      cachedAt = now();
      return value;
    }).finally(() => { pending = null; });
    return pending;
  };

  async function facts(ctx) {
    const source = canvasFacts ?? ctx.canvasFacts ?? ctx.server?.canvasFacts ?? null;
    try {
      const value = typeof source === "function" ? await source(ctx) : source;
      if (value && typeof value === "object") return { ...DEFAULT_CANVAS_FACTS, ...value };
    } catch { /* not resolved */ }
    return { ...DEFAULT_CANVAS_FACTS };
  }

  async function health(ctx) {
    const refresh = ctx.url.searchParams.get("refresh") === "1";
    const tools = primaryTools(ctx.registry);
    let checked = null;
    let error = null;
    if (tools && typeof tools.call === "function") {
      const answer = await tools.call("health", { refresh }, "system");
      if (answer?.status === 200 && Array.isArray(answer.body?.result?.tools)) checked = answer.body.result;
      else error = answer?.body?.error ?? "the device tools did not answer";
    }
    if (!checked) {
      try {
        checked = await fallbackHealth(refresh);
        error = null;
      } catch (failure) {
        error = error ?? failure.message;
      }
    }
    const body = {
      ok: true,
      checked_at: checked?.checked_at ?? null,
      tools: checked?.tools ?? [],
      canvas: await facts(ctx),
    };
    if (!checked) body.error = redactText(error, ERROR_MAX);
    return { status: 200, body };
  }

  return async function activityApiRoute(ctx) {
    const { method, url } = ctx;
    const path = url?.pathname;
    if (path !== "/api/activity" && path !== "/api/activity/clear" && path !== "/api/health") return null;
    const log = ctx.commandLog;
    if (path === "/api/activity" && method === "GET") {
      const after = readInteger(url.searchParams.get("after"), "after", 0, Number.MAX_SAFE_INTEGER, 0);
      if (after.error) return after.error;
      const seen = readInteger(url.searchParams.get("seen"), "seen", 0, Number.MAX_SAFE_INTEGER, 0);
      if (seen.error) return seen.error;
      const limit = readInteger(url.searchParams.get("limit"), "limit", 1, ACTIVITY_READ_MAX_LIMIT,
        ACTIVITY_READ_DEFAULT_LIMIT);
      if (limit.error) return limit.error;
      const failures = url.searchParams.get("failures");
      if (failures !== null && failures !== "1" && failures !== "0") return badRequest("failures must be 1 or 0");
      const device = url.searchParams.get("device");
      if (device !== null && device !== "" && !DEVICE_ID.test(device)) {
        return badRequest("device must be a device id such as android~emulator-5580");
      }
      if (!log) return { status: 200, body: { ok: true, entries: [], next: 0, dropped: 0, total: 0, seen: 0 } };
      const page = log.list({ after: after.value, limit: limit.value, failures: failures === "1", device: device || null,
        seen: seen.value });
      return { status: 200, body: { ok: true, ...page } };
    }
    if (path === "/api/activity/clear" && method === "POST") {
      return { status: 200, body: { ok: true, cleared: log ? log.clear() : 0 } };
    }
    if (path === "/api/health" && method === "GET") return health(ctx);
    return { status: 405, body: { ok: false, error: "Method not allowed", error_code: "invalid_value", hint: null } };
  };
}

/** The `hooks.apiRoutes` entry with the default python run (`PYTHON`, else python3). */
export const activityApiRoute = createActivityApiRoute();
