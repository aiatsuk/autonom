/**
 * The host-wide control lease as the Canvas reads it (contract section 6.1).
 *
 * One lease file per target lives in the machine state store,
 * `<state base>/control/<platform>__<safe target>.json`. Only
 * `scripts/autonom_lib/control.py` writes it (the Canvas reaches it through the bridge ops
 * `control-acquire`, `control-release` and `control-renew`); this module only reads it, with
 * the same names, the same validity rule and the same admission table as the Python side, so
 * both answer every case of `tests/fixtures/control_cases.json` alike.
 *
 * Pure functions and small file helpers only: no timers, no child processes.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const LEASE_POLL_MS = 250;
// Liveness: a Canvas that is gone stops renewing, so its lease ends within this long.
export const HUMAN_LEASE_TTL_S = 30;
export const LEASE_RENEW_MS = 10_000;
// Idle release after the last human input (DEC-014), and its range for --control-idle.
export const DEFAULT_CONTROL_IDLE_S = 120;
export const MIN_CONTROL_IDLE_S = 15;
export const MAX_CONTROL_IDLE_S = 3600;
// Human input waits at most this long for an automatic acquire to answer.
export const AUTO_ACQUIRE_WAIT_MS = 2_000;

export const LEASE_SCHEMA = 1;
export const HOLDER_KINDS = Object.freeze(["human", "agent"]);
export const CONTROL_CAPABILITY = "control.lease";
export const CONTROL_HELD = "control_held";
export const CONTROL_GENERATION_STALE = "control_generation_stale";
export const CONTROL_LEASE_EXPIRED = "control_lease_expired";
export const CONTROL_NOT_HELD = "control_not_held";
export const CONTROL_CODES = Object.freeze([CONTROL_HELD, CONTROL_GENERATION_STALE, CONTROL_LEASE_EXPIRED,
  CONTROL_NOT_HELD]);
export const SESSION_CODES = Object.freeze(["session_stopped", "session_target_mismatch", "session_not_found"]);
// Bridge and tools error codes the Canvas answers with HTTP 409 and their `error_code`.
export const LEASE_ERROR_CODES = Object.freeze([...CONTROL_CODES, ...SESSION_CODES]);

const UNSAFE_CHARACTERS = /[^A-Za-z0-9._-]/gu;
const SESSION_ID = /^s_[A-Za-z0-9]+$/;
const PLATFORMS = new Set(["android", "ios"]);

const HINTS = Object.freeze({
  [CONTROL_HELD]: (target) => `Wait until 'autonom control status --target ${target}' shows it free, or ask ` +
    "the holder to release it; a human can take over from the Canvas.",
  [CONTROL_GENERATION_STALE]: (target, generation) => `Your lease was replaced (generation ${generation} now); ` +
    "acquire it again before driving the device; never replay an input whose outcome you did not see.",
  [CONTROL_LEASE_EXPIRED]: () => "Renew before the expiry ('autonom control renew') or acquire it again.",
  [CONTROL_NOT_HELD]: () => "Acquire the lease first ('autonom control acquire --holder <id>') or drop the " +
    "control claim (--control-holder / AUTONOM_CONTROL_HOLDER).",
});

/** $AUTONOM_HOME, else $XDG_STATE_HOME/autonom, else ~/.local/state/autonom (canvas-workspace.mjs). */
export function stateBase(env = process.env) {
  if (env.AUTONOM_HOME) return env.AUTONOM_HOME;
  if (env.XDG_STATE_HOME) return join(env.XDG_STATE_HOME, "autonom");
  return join(env.HOME || homedir(), ".local", "state", "autonom");
}

/** The session store: $AUTONOM_HOME/sessions, else ~/.autonom/sessions (session.sessions_home). */
export function sessionsHome(env = process.env) {
  const base = env.AUTONOM_HOME ? env.AUTONOM_HOME : join(env.HOME || homedir(), ".autonom");
  return join(base, "sessions");
}

/** Code points of `text` from the start, at most `count` (Python string slicing). */
function headCodePoints(text, count) {
  return Array.from(text).slice(0, count).join("");
}

/**
 * The target id as a file name part, exactly `session._safe_target`: characters outside
 * [A-Za-z0-9._-] become "_", cut to 128; an id that had to change carries a hash of the
 * exact id, so `127.0.0.1:5555` and `127.0.0.1_5555` never share a file.
 */
export function safeTarget(targetId) {
  const raw = String(targetId);
  const safe = headCodePoints(raw.replace(UNSAFE_CHARACTERS, "_"), 128);
  if (safe === raw) return safe;
  const digest = createHash("sha256").update(raw, "utf8").digest("hex").slice(0, 12);
  return `${headCodePoints(safe, 115)}-${digest}`;
}

function platformPart(platform) {
  return headCodePoints(String(platform || "android").replace(UNSAFE_CHARACTERS, "_"), 16);
}

/** `<state base>/control/<platform>__<safe target>.json`; nothing is created. */
export function leasePath(env, platform, targetId) {
  if (!PLATFORMS.has(platform)) throw new TypeError("the control lease platform must be 'android' or 'ios'");
  if (typeof targetId !== "string" || targetId === "") throw new TypeError("the control lease needs a target id");
  return join(stateBase(env), "control", `${platformPart(platform)}__${safeTarget(targetId)}.json`);
}

/** `<sessions home>/<id>/session.json`, or null for an id that is not a session id. */
export function sessionRecordPath(env, sessionId) {
  if (typeof sessionId !== "string" || !SESSION_ID.test(sessionId)) return null;
  return join(sessionsHome(env), sessionId, "session.json");
}

const isInteger = (value) => Number.isSafeInteger(value);

function validHolder(holder) {
  return Boolean(holder) && typeof holder === "object" && HOLDER_KINDS.includes(holder.kind) &&
    typeof holder.id === "string" && holder.id !== "" && typeof holder.label === "string";
}

/** The schema 1 shape check admission and status rely on (control.py `_valid_lease`). */
export function validLease(lease) {
  if (!lease || typeof lease !== "object" || Array.isArray(lease) || lease.schema !== LEASE_SCHEMA) return false;
  if (!isInteger(lease.generation) || lease.generation < 0) return false;
  if (typeof lease.platform !== "string" || typeof lease.target_id !== "string") return false;
  if (lease.holder === null || lease.holder === undefined) return true;
  return validHolder(lease.holder) && isInteger(lease.expires_at_ms);
}

/** "none" (no lease or never granted), "free" (released), "held" (live) or "expired". */
export function leaseState(lease, nowMs) {
  if (!validLease(lease)) return "none";
  if (lease.holder === null || lease.holder === undefined) return lease.generation === 0 ? "none" : "free";
  return nowMs < lease.expires_at_ms ? "held" : "expired";
}

function claimOf(claim) {
  if (!claim || typeof claim !== "object") return null;
  const generation = claim.generation === undefined ? null : claim.generation;
  return { holder_id: claim.holder_id, generation };
}

function refusalOf(code, lease, claim) {
  const target = lease?.target_id || "the target";
  const generation = lease ? lease.generation : null;
  const holder = lease?.holder ? { ...lease.holder } : null;
  const claimed = claim ? claim.holder_id : null;
  let message;
  if (code === CONTROL_HELD && holder) {
    message = `${target} is controlled by ${holder.kind} '${holder.label}' (generation ${generation})`;
  } else if (code === CONTROL_GENERATION_STALE) {
    message = `the control claim of '${claimed}' on ${target} is stale: generation ${claim?.generation ?? null} ` +
      `given, the lease is at generation ${generation}`;
  } else if (code === CONTROL_LEASE_EXPIRED) {
    message = `the control lease of '${claimed}' on ${target} expired at ${lease?.expires_at ?? null}`;
  } else if (code === CONTROL_HELD) {
    message = `${target} is controlled by another holder`;
  } else if (claimed !== null) {
    message = `'${claimed}' does not hold the control lease of ${target}`;
  } else {
    message = `nobody holds the control lease of ${target}`;
  }
  const result = {
    code, message, hint: HINTS[code](target, generation),
    platform: lease?.platform ?? null, target_id: lease?.target_id ?? null, capability: CONTROL_CAPABILITY,
    generation, holder, expires_at: holder ? (lease.expires_at ?? null) : null,
  };
  if (claim) {
    result.claimed_holder = claim.holder_id;
    result.claimed_generation = claim.generation;
  }
  return result;
}

/**
 * The admission rule (contract 2.4): null when admitted, else the refusal
 * `{code, message, hint, holder, generation, ...}`. The first matching row decides.
 */
export function admission(lease, claim, nowMs) {
  const valid = validLease(lease) ? lease : null;
  const state = leaseState(valid, nowMs);
  const given = claimOf(claim);
  const refuse = (code) => refusalOf(code, valid, given);
  if (state === "none") return given === null ? null : refuse(CONTROL_NOT_HELD); // rows 1, 2
  if (given === null) return state === "held" ? refuse(CONTROL_HELD) : null; // rows 3, 4
  const same = valid.holder?.id === given.holder_id;
  const generationMatches = given.generation === null || given.generation === valid.generation;
  if (state === "held" && same) return generationMatches ? null : refuse(CONTROL_GENERATION_STALE); // rows 5, 6
  if (given.generation !== null && given.generation < valid.generation) return refuse(CONTROL_GENERATION_STALE); // 7
  if (state === "held") return refuse(CONTROL_HELD); // row 8
  if (state === "expired" && same && generationMatches) return refuse(CONTROL_LEASE_EXPIRED); // row 9
  return refuse(CONTROL_NOT_HELD); // row 10
}

/**
 * A cached JSON file: `.get()` answers `{value, changed}` and reads the file again only when
 * its (mtimeMs, ctimeMs, size, ino) changed. A missing file and invalid JSON are value null.
 */
export function createFileCache(path, { readFileSync = fs.readFileSync, statSync = fs.statSync } = {}) {
  let key;
  let value = null;
  return {
    path,
    get() {
      let stat = null;
      try {
        stat = statSync(path);
      } catch {
        stat = null;
      }
      const next = stat ? `${stat.mtimeMs}:${stat.ctimeMs ?? ""}:${stat.size}:${stat.ino}` : "missing";
      if (next === key) return { value, changed: false };
      key = next;
      value = null;
      if (stat) {
        try {
          value = JSON.parse(readFileSync(path, "utf8"));
        } catch {
          value = null;
        }
      }
      return { value, changed: true };
    },
    /** Forget what was read, so the next get() reads the file again. */
    reset() {
      key = undefined;
      value = null;
    },
  };
}

/** "live", "stopped", "mismatch" (another target) or "missing" (no readable record). */
export function sessionStatus(record, { platform, target }) {
  if (!record || typeof record !== "object" || Array.isArray(record)) return "missing";
  if (record.stopped_at) return "stopped";
  const recordTarget = record.target_id || record.serial;
  const recordPlatform = record.platform || "android";
  if (recordTarget !== target || recordPlatform !== platform) return "mismatch";
  return "live";
}

/** The --control-idle value: an integer number of seconds from 15 to 3600, else an Error. */
export function parseControlIdle(value) {
  const text = String(value ?? "");
  const seconds = /^\d{1,7}$/.test(text) ? Number(text) : Number.NaN;
  if (!Number.isInteger(seconds) || seconds < MIN_CONTROL_IDLE_S || seconds > MAX_CONTROL_IDLE_S) {
    throw new Error(`--control-idle must be an integer from ${MIN_CONTROL_IDLE_S} to ${MAX_CONTROL_IDLE_S} seconds.`);
  }
  return seconds;
}

/** The lease of `platform`/`target` from a cached read: null when missing, invalid or another target's. */
export function leaseFor(value, { platform, target }) {
  if (!validLease(value) || value.platform !== platform || value.target_id !== target) return null;
  return value;
}

/** The page's words for a holder: "agent 'worker' (generation 3)". */
export function describeHolder(holder, generation) {
  if (!holder) return "nobody";
  return `${holder.kind} '${holder.label}' (generation ${generation})`;
}
