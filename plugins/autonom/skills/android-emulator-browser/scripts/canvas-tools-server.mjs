/**
 * The Canvas tools: the client of the tools process (scripts/autonom_canvas_tools.py), the
 * device log feed (`autonom logs follow --source device`) and the /tools/* routes.
 *
 * The tools process is separate from the input bridge, so slow panel work (an attach can
 * take 20 s) never delays device input or the stream. One NDJSON request per line on its
 * stdin, one reply per line on its stdout. A call waits at most TOOLS_CALL_TIMEOUT_MS; a
 * process that is gone is restarted by the next call, at most once per
 * TOOLS_RESTART_INTERVAL_MS.
 *
 * The log feed is one child process per Canvas. Its entries live only in a bounded ring
 * buffer in memory: log text is never journaled or written to disk here. An optional `match`
 * text narrows the feed at its source (`logs follow --grep`), so a device that logs faster
 * than a reader can read (a booted iOS Simulator logs thousands of lines a second) cannot
 * push the lines asked for out of the ring.
 */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { resolve } from "node:path";

export const TOOLS_CALL_TIMEOUT_MS = 60_000;
export const TOOLS_RESTART_INTERVAL_MS = 10_000;
export const LOG_IDLE_STOP_MS = 60_000;
export const LOG_MAX_ENTRIES = 2000;
export const LOG_MAX_BYTES = 1024 * 1024;
export const LOG_READ_DEFAULT_LIMIT = 200;
export const LOG_READ_MAX_LIMIT = 500;
// A single log line longer than this is dropped (counted in `dropped`), so one runaway
// line can never hold more memory than the ring buffer allows.
export const LOG_MAX_LINE_BYTES = 256 * 1024;
// A child that ignores SIGTERM this long is killed.
export const CHILD_KILL_GRACE_MS = 1000;
// After a child exits, its last stdout lines (a reply, the eof line) may still be in the
// pipe: they are read until its stdout closes, or this long when something else holds it.
const EXIT_DRAIN_MS = 500;

export const TOOLS_READ_OPS = Object.freeze([
  "context",
  "permissions.list",
  "location.get",
  "network.status",
  "network.requests",
  "network.request",
  "mocks.list",
]);
export const TOOLS_MUTATING_OPS = Object.freeze([
  "permissions.set",
  "location.set",
  "location.clear",
  "simulate",
  "network.start",
  "network.attach",
  "network.detach",
  "network.stop",
  "mocks.add",
  "mocks.update",
  "mocks.enable",
  "mocks.disable",
  "mocks.remove",
  "mocks.clear",
]);
export const TOOLS_OPS = Object.freeze([...TOOLS_READ_OPS, ...TOOLS_MUTATING_OPS]);

const READ_OPS = new Set(TOOLS_READ_OPS);
const ALL_OPS = new Set(TOOLS_OPS);

const STATUS_400 = new Set([
  "flow_command_invalid", "invalid_value", "invalid_coordinates", "unknown_privacy_service",
  "invalid_simulator_action", "consent_required", "consent_declined", "selector_required",
]);
const STATUS_404 = new Set(["mock_not_found", "flow_not_found", "app_not_installed"]);
const STATUS_409 = new Set([
  "unsupported_on_platform", "unsupported_capability", "no_active_session",
  "session_target_mismatch", "emulator_only", "proxy_not_running",
  "physical_device_attach_unsupported",
]);

const PACKAGE_NAME = /^[A-Za-z0-9_.]{1,255}$/;
// The feed's `match`: plain printable ASCII text, found case-insensitively in a line.
const MATCH_TEXT = /^[\x20-\x7E]{1,200}$/;

/**
 * `text` as a regular expression that matches it literally, for `logs follow --grep` (a
 * Python regular expression): every character but letters, digits and `_` is escaped.
 */
export function literalPattern(text) {
  return String(text).replace(/[^A-Za-z0-9_]/g, (character) => `\\${character}`);
}

/** The HTTP status of a failed tools call, by its lowercase error code. */
export function toolsErrorStatus(code) {
  if (STATUS_400.has(code)) return 400;
  if (STATUS_404.has(code)) return 404;
  if (STATUS_409.has(code)) return 409;
  return 502;
}

export function isToolsOp(op) {
  return typeof op === "string" && ALL_OPS.has(op);
}

export function isMutatingToolsOp(op) {
  return isToolsOp(op) && !READ_OPS.has(op);
}

function failure(status, errorCode, error, hint = null, capability = null) {
  return { status, body: { ok: false, error, error_code: errorCode, hint, capability } };
}

const optionalText = (value) => (typeof value === "string" && value !== "" ? value : null);

/** The HTTP answer for one reply line of the tools process. */
export function toolsReplyResponse(message) {
  if (message.ok === true) {
    const result = message.result && typeof message.result === "object" ? message.result : {};
    return { status: 200, body: { ok: true, result } };
  }
  const code = optionalText(message.error_code) ?? "backend_failed";
  return failure(toolsErrorStatus(code), code,
    optionalText(message.error) ?? "The Canvas tools call failed",
    optionalText(message.hint), optionalText(message.capability));
}

/** Calls `onGone` once, when the child's stdout has closed after its exit (or soon after it). */
function whenGone(child, onGone) {
  let done = false;
  let timer = null;
  const finish = () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    onGone(child.exitCode, child.signalCode);
  };
  child.on("close", finish);
  child.on("exit", () => {
    timer = setTimeout(finish, EXIT_DRAIN_MS);
    timer.unref?.();
  });
}

function alive(child) {
  // A spawn that failed (no python) has no pid.
  return Boolean(child) && child.pid !== undefined && child.exitCode === null && child.signalCode === null;
}

/** SIGTERM now (the whole process group when `group`), SIGKILL after the grace period; resolves at exit. */
function terminate(child, { group = false, graceMs = CHILD_KILL_GRACE_MS } = {}) {
  if (!child) return Promise.resolve();
  const signal = (name) => {
    try {
      if (group && child.pid) process.kill(-child.pid, name);
      else child.kill(name);
    } catch {
      try { child.kill(name); } catch { /* already gone */ }
    }
  };
  // The group may hold grandchildren (adb logcat) after the leader is gone.
  if (!alive(child)) {
    if (group && child.pid) signal("SIGTERM");
    return Promise.resolve();
  }
  return new Promise((resolvePromise) => {
    const timer = setTimeout(() => {
      signal("SIGKILL");
    }, graceMs);
    child.once("exit", () => {
      clearTimeout(timer);
      // Grandchildren left in the group are told to stop too.
      if (group) signal("SIGKILL");
      resolvePromise();
    });
    signal("SIGTERM");
  });
}

/**
 * The client of the tools process. `spawnTools()` returns a new child process speaking the
 * NDJSON protocol of the contract; `call` never rejects, it resolves {status, body}.
 */
export class ToolsClient {
  constructor({
    spawnTools,
    timeoutMs = TOOLS_CALL_TIMEOUT_MS,
    restartIntervalMs = TOOLS_RESTART_INTERVAL_MS,
    now = () => performance.now(),
  }) {
    this.spawnTools = spawnTools;
    this.timeoutMs = timeoutMs;
    this.restartIntervalMs = restartIntervalMs;
    this.now = now;
    this.child = null;
    this.pending = new Map();
    this.nextId = 0;
    this.lastRestartAt = -Infinity;
    this.restarts = 0;
    this.started = false;
    this.closed = false;
  }

  start() {
    this.started = true;
    let child;
    try {
      child = this.spawnTools();
    } catch {
      this.child = null;
      return;
    }
    this.child = child;
    // A spawn that failed (no python) may never emit exit.
    child.on("error", () => { if (child.pid === undefined) this.lost(child); });
    child.stdin?.on("error", () => {});
    child.stderr?.on("data", () => {});
    createInterface({ input: child.stdout }).on("line", (line) => this.onLine(child, line));
    whenGone(child, () => this.lost(child));
  }

  available() {
    return !this.closed && alive(this.child);
  }

  onLine(child, line) {
    // A reply from a child that already exited (or was replaced by a restart while its last
    // lines drained) still answers its own call: the waiter is matched by id and child.
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (!message || typeof message !== "object") return;
    const waiter = this.pending.get(message.id);
    // A reply to a call that timed out (or that never was) is dropped.
    if (!waiter || waiter.child !== child) return;
    this.pending.delete(message.id);
    clearTimeout(waiter.timer);
    waiter.resolve(toolsReplyResponse(message));
  }

  lost(child) {
    for (const [id, waiter] of this.pending) {
      if (waiter.child !== child) continue;
      this.pending.delete(id);
      clearTimeout(waiter.timer);
      waiter.resolve(this.unavailable());
    }
  }

  unavailable() {
    return failure(502, "tools_unavailable", this.closed
      ? "The Canvas is stopping"
      : "The Canvas tools process is not running",
    this.closed ? null : "The next tools call restarts it (at most once every 10 s).");
  }

  /** Restart a tools process that is gone, at most once per restart interval. */
  ensureRunning() {
    if (this.closed) return false;
    if (alive(this.child)) return true;
    if (!this.started) {
      this.start();
      return alive(this.child);
    }
    const now = this.now();
    if (now - this.lastRestartAt < this.restartIntervalMs) return false;
    this.lastRestartAt = now;
    this.restarts += 1;
    this.start();
    return alive(this.child);
  }

  call(op, payload, origin) {
    if (!this.ensureRunning()) return Promise.resolve(this.unavailable());
    const child = this.child;
    const id = ++this.nextId;
    return new Promise((resolvePromise) => {
      const timer = setTimeout(() => {
        // The process is not killed for one slow call; its late reply is dropped.
        if (this.pending.get(id)?.child !== child) return;
        this.pending.delete(id);
        resolvePromise(failure(504, "timeout",
          `The Canvas tools call ${op} did not answer within ${Math.round(this.timeoutMs / 1000)} s`));
      }, this.timeoutMs);
      this.pending.set(id, { child, resolve: resolvePromise, timer });
      try {
        child.stdin.write(`${JSON.stringify({ id, op, payload, origin })}\n`);
      } catch {
        // The exit handler answers it.
      }
    });
  }

  async close() {
    this.closed = true;
    const child = this.child;
    for (const [id, waiter] of this.pending) {
      this.pending.delete(id);
      clearTimeout(waiter.timer);
      waiter.resolve(this.unavailable());
    }
    await terminate(child);
  }
}

/**
 * The device log feed: one `logs follow --source device` child at a time, its NDJSON
 * entries in a ring buffer of the last LOG_MAX_ENTRIES entries and LOG_MAX_BYTES bytes.
 * `spawnFollow(package, match)` starts the child (`match`: only lines holding that text, or
 * null). `checkStart(package)` resolves null when the feed may start, or the reason it may not.
 */
export class LogFeed {
  constructor({
    spawnFollow,
    checkStart = async () => null,
    idleMs = LOG_IDLE_STOP_MS,
    maxEntries = LOG_MAX_ENTRIES,
    maxBytes = LOG_MAX_BYTES,
    maxLineBytes = LOG_MAX_LINE_BYTES,
  }) {
    this.spawnFollow = spawnFollow;
    this.checkStart = checkStart;
    this.idleMs = idleMs;
    this.maxEntries = maxEntries;
    this.maxBytes = maxBytes;
    this.maxLineBytes = maxLineBytes;
    this.run = null;
    this.package = null;
    this.match = null;
    this.error = null;
    this.entries = [];
    this.bytes = 0;
    this.seq = 0;
    // Lines up to this seq were cleared on purpose (a feed restart): never counted as dropped.
    this.clearedThrough = 0;
    this.idleTimer = null;
    this.closed = false;
    // Each set() is a new generation: a start still checking its precondition gives way
    // to any later set(), so a stop never waits for it and is never undone by it.
    this.generation = 0;
  }

  running() {
    return this.run !== null && !this.run.ended;
  }

  status() {
    return { running: this.running(), package: this.package };
  }

  /** POST /tools/logs: start, restart (package or match change) or stop the feed. */
  set(active, packageName = null, match = null) {
    this.generation += 1;
    return this.apply(this.generation, active, packageName, match);
  }

  async apply(generation, active, packageName, match = null) {
    if (!active || this.closed) {
      await this.stop();
      if (generation === this.generation) this.error = null;
      return this.snapshot();
    }
    if (this.running() && this.package === packageName && this.match === match) {
      this.watched();
      return this.snapshot();
    }
    await this.stop();
    if (this.closed || generation !== this.generation) return this.snapshot();
    this.clear();
    this.package = packageName;
    this.match = match;
    this.error = null;
    const refused = await this.checkStart(packageName);
    if (this.closed || generation !== this.generation) return this.snapshot();
    if (refused) {
      this.error = refused;
      return this.snapshot();
    }
    this.start(packageName, match);
    return this.snapshot();
  }

  snapshot() {
    return { ok: true, running: this.running(), package: this.package, match: this.match, error: this.error };
  }

  clear() {
    this.entries = [];
    this.bytes = 0;
    this.clearedThrough = this.seq;
  }

  start(packageName, match = null) {
    let child;
    try {
      child = this.spawnFollow(packageName, match);
    } catch (error) {
      this.error = `The device log feed could not start: ${error.message}`;
      return;
    }
    const run = { child, ended: false, stopping: false, stderr: "" };
    this.run = run;
    child.on("error", (error) => {
      if (child.pid === undefined) this.ended(run, `The device log feed could not start: ${error.message}`);
    });
    child.stdin?.on?.("error", () => {});
    child.stderr?.on("data", (chunk) => { run.stderr = (run.stderr + chunk.toString()).slice(-2000); });
    let buffer = Buffer.alloc(0);
    let skipping = false;
    child.stdout.on("data", (chunk) => {
      if (run !== this.run) return;
      buffer = buffer.length ? Buffer.concat([buffer, chunk]) : chunk;
      for (;;) {
        const index = buffer.indexOf(10);
        if (index < 0) break;
        const line = buffer.subarray(0, index);
        buffer = buffer.subarray(index + 1);
        if (skipping) {
          skipping = false;
          continue;
        }
        this.onLine(run, line);
      }
      if (buffer.length > this.maxLineBytes) {
        // The rest of this line is skipped, and the line counted as dropped.
        buffer = Buffer.alloc(0);
        if (!skipping) this.dropOne();
        skipping = true;
      }
    });
    whenGone(child, (code, signal) => {
      const detail = lastLine(run.stderr);
      this.ended(run, `The device log feed exited (${signal ?? `code ${code}`})${detail ? `: ${detail}` : ""}`);
    });
    this.watched();
  }

  onLine(run, raw) {
    if (run.ended) return;
    if (raw.length > this.maxLineBytes) {
      this.dropOne();
      return;
    }
    let entry;
    try { entry = JSON.parse(raw.toString("utf8")); } catch { return; }
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return;
    if (entry.kind === "eof") {
      const reason = typeof entry.reason === "string" ? entry.reason : "unknown";
      this.ended(run, `The device log feed ended: ${reason}`);
      return;
    }
    this.seq += 1;
    this.entries.push({ ...entry, seq: this.seq, size: raw.length });
    this.bytes += raw.length;
    while (this.entries.length > this.maxEntries || this.bytes > this.maxBytes) {
      const evicted = this.entries.shift();
      this.bytes -= evicted.size;
    }
  }

  /** A line that never entered the buffer still takes a seq, so readers see it as dropped. */
  dropOne() {
    this.seq += 1;
  }

  ended(run, reason) {
    if (run.ended) return;
    run.ended = true;
    if (run !== this.run) return;
    clearTimeout(this.idleTimer);
    this.idleTimer = null;
    if (!run.stopping) this.error = reason;
    // The group may still hold the device log reader the CLI started.
    terminate(run.child, { group: true });
  }

  watched() {
    clearTimeout(this.idleTimer);
    this.idleTimer = null;
    if (!this.running()) return;
    this.idleTimer = setTimeout(() => {
      // Nobody read the feed for the idle period: it stops, as asked by no one.
      this.set(false).catch(() => {});
    }, this.idleMs);
    this.idleTimer.unref?.();
  }

  /**
   * GET /tools/logs: entries after `after`, at most `limit`. Reading marks the feed as watched.
   *
   * `dropped` counts each lost seq once per cursor: only the seqs in (after, next] that are
   * not in the ring (evicted by the bound, or a line that never fit), never those cleared on
   * purpose. When the page is not cut short by `limit`, `next` is the newest seq, so lost
   * seqs past the last returned line are counted now and the next read starts past them.
   */
  read(after = 0, limit = LOG_READ_DEFAULT_LIMIT) {
    this.watched();
    // A cursor from before a Canvas restart is ahead of every seq here: read from the start.
    const from = after > this.seq ? 0 : after;
    const lines = [];
    const first = this.entries.findIndex((entry) => entry.seq > from);
    const start = first < 0 ? this.entries.length : first;
    let i = start;
    for (; i < this.entries.length && lines.length < limit; i += 1) {
      const { size: _size, ...entry } = this.entries[i];
      lines.push(entry);
    }
    const cut = i < this.entries.length;
    const next = cut ? (lines.length ? lines[lines.length - 1].seq : from) : this.seq;
    const lowest = Math.max(from, this.clearedThrough);
    let kept = 0;
    for (const entry of this.entries) {
      if (entry.seq > lowest && entry.seq <= next) kept += 1;
    }
    const dropped = Math.max(0, next - lowest - kept);
    return {
      ok: true,
      running: this.running(),
      package: this.package,
      lines,
      next,
      dropped,
      error: this.error,
    };
  }

  async stop() {
    clearTimeout(this.idleTimer);
    this.idleTimer = null;
    const run = this.run;
    if (!run) return;
    run.stopping = true;
    if (!run.ended) {
      run.ended = true;
      await terminate(run.child, { group: true });
    }
  }

  async close() {
    this.closed = true;
    await this.stop();
  }
}

function lastLine(text) {
  const lines = String(text ?? "").trim().split("\n").filter(Boolean);
  if (!lines.length) return "";
  let line = lines[lines.length - 1];
  try {
    const parsed = JSON.parse(line);
    if (parsed && typeof parsed.error === "string") line = parsed.error;
    else if (parsed?.error && typeof parsed.error.message === "string") line = parsed.error.message;
  } catch { /* plain text */ }
  return line.slice(0, 300);
}

/**
 * The tools of one Canvas: the tools client, the log feed and their status. Both child
 * processes start from `python`; `toolsPath` and `autonomPath` override the scripts.
 */
export function createCanvasTools({
  python,
  toolsPath = null,
  autonomPath = null,
  platform,
  target,
  tool,
  env = process.env,
  timeoutMs,
  restartIntervalMs,
  idleMs,
  maxEntries,
  maxBytes,
}) {
  const repoScripts = resolve(import.meta.dirname, "../../../../../scripts");
  const toolsScript = toolsPath ?? resolve(repoScripts, "autonom_canvas_tools.py");
  const autonomScript = autonomPath ?? resolve(repoScripts, "autonom.py");
  const client = new ToolsClient({
    spawnTools: () => spawn(python, [toolsScript, "--platform", platform, "--target", target,
      "--tool", tool], { stdio: ["pipe", "pipe", "pipe"], env }),
    timeoutMs,
    restartIntervalMs,
  });
  const targetFlag = platform === "ios" ? "--udid" : "--serial";
  const toolFlag = platform === "ios" ? "--simctl" : "--adb";
  const feed = new LogFeed({
    spawnFollow: (packageName, match = null) => spawn(python, [autonomScript, "--platform", platform, targetFlag, target,
      toolFlag, tool, "logs", "follow", "--source", "device",
      ...(packageName ? ["--package", packageName] : []),
      ...(match ? ["--grep", literalPattern(match)] : [])],
    // Its own process group, so the device log reader it starts stops with it.
    { stdio: ["ignore", "pipe", "pipe"], env, detached: true }),
    // `logs follow --source device` on iOS follows the current session's stream file
    // without checking its device: only a session on this Simulator may feed this Canvas.
    checkStart: platform !== "ios" ? undefined : async () => {
      const answer = await client.call("context", {}, "system");
      if (answer.status === 200 && answer.body.result?.session) return null;
      const why = answer.status === 200 ? "" : ` (${answer.body.error})`;
      return `No Autonom session with a log stream is on this Simulator${why}. Start one with ` +
        `autonom session start --platform ios --udid ${target} --log-stream`;
    },
    idleMs,
    maxEntries,
    maxBytes,
  });
  client.start();
  return {
    client,
    feed,
    call: (op, payload, origin) => client.call(op, payload, origin),
    status: () => ({ available: client.available(), logs: feed.status() }),
    async close() {
      await Promise.all([client.close(), feed.close()]);
    },
  };
}

function badRequest(errorCode, error) {
  return failure(400, errorCode, error);
}

function readInteger(raw, name, minimum, maximum, fallback) {
  if (raw === null) return fallback;
  if (!/^\d{1,15}$/.test(raw)) throw badRequest("invalid_value", `${name} must be an integer from ${minimum} to ${maximum}`);
  const value = Number(raw);
  if (value < minimum || value > maximum) {
    throw badRequest("invalid_value", `${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}

/**
 * Answer one /tools/* request: resolves {status, body}. The caller has already checked
 * authorization and CSRF. `readBody()` reads the JSON body (with the Canvas body limit);
 * `refusal(origin)` is the Canvas control rule, a message when input is refused.
 */
export async function handleToolsRoute(tools, { method, url, origin, readBody, refusal }) {
  try {
    if (method === "POST" && url.pathname === "/tools/call") {
      const body = await readBody();
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        return badRequest("invalid_value", "The body must be a JSON object");
      }
      if (!isToolsOp(body.op)) {
        return badRequest("flow_command_invalid",
          `Unknown tools op ${JSON.stringify(String(body.op ?? ""))}; valid ops: ${TOOLS_OPS.join(", ")}`);
      }
      const payload = body.payload ?? {};
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return badRequest("invalid_value", "payload must be a JSON object");
      }
      if (isMutatingToolsOp(body.op)) {
        const refused = refusal(origin);
        if (refused) return failure(403, "control_refused", refused);
      }
      return await tools.call(body.op, payload, origin);
    }
    if (method === "GET" && url.pathname === "/tools/logs") {
      const after = readInteger(url.searchParams.get("after"), "after", 0, Number.MAX_SAFE_INTEGER, 0);
      const limit = readInteger(url.searchParams.get("limit"), "limit", 1, LOG_READ_MAX_LIMIT, LOG_READ_DEFAULT_LIMIT);
      return { status: 200, body: tools.feed.read(after, limit) };
    }
    if (method === "POST" && url.pathname === "/tools/logs") {
      const body = await readBody();
      if (!body || typeof body !== "object" || Array.isArray(body) || typeof body.active !== "boolean") {
        return badRequest("invalid_value", "active must be true or false");
      }
      const packageName = body.package ?? null;
      if (packageName !== null && (typeof packageName !== "string" || !PACKAGE_NAME.test(packageName))) {
        return badRequest("invalid_value", "package must be a package or bundle id (letters, digits, _ and .) or null");
      }
      const match = body.match ?? null;
      if (match !== null && (typeof match !== "string" || !MATCH_TEXT.test(match))) {
        return badRequest("invalid_value", "match must be 1 to 200 printable ASCII characters or null");
      }
      // Starting or stopping the feed is not a device mutation: no control check.
      return { status: 200, body: await tools.feed.set(body.active, packageName, match) };
    }
    return { status: 404, body: { error: "Not found" } };
  } catch (error) {
    if (error && typeof error.status === "number" && error.body) return error;
    throw error;
  }
}
