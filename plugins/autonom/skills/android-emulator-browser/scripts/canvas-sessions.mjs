// Automatic Autonom sessions of a workspace Canvas: attaching a device starts its own
// session alongside the primary one (or reuses the live session on that target), and
// detaching ends only the sessions the Canvas started. Every call goes through the CLI
// (`autonom.py session start|stop`), so the session store has one writer rule.
import { spawn } from "node:child_process";

import { pidAlive } from "./canvas-workspace.mjs";

export const SESSION_CLI_TIMEOUT_MS = 60_000;
const OUTPUT_MAX_BYTES = 4 * 1024 * 1024;

/** The last JSON object in a block of text: the whole text, else its last line that parses. */
export function parseCliJson(text) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {}
  const lines = trimmed.split(/\r?\n/).reverse();
  for (const line of lines) {
    try {
      const value = JSON.parse(line);
      if (value && typeof value === "object") return value;
    } catch {}
  }
  return null;
}

/**
 * Run one CLI command (argv[0] is the program) and read its JSON answer: stdout on exit 0,
 * else the error object it printed on stderr. Never rejects; a timeout kills it.
 */
export function runCliJson(argv, { timeoutMs = SESSION_CLI_TIMEOUT_MS, env = process.env } = {}) {
  return new Promise((resolvePromise) => {
    let child;
    try {
      child = spawn(argv[0], argv.slice(1), { env, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      resolvePromise({ code: null, json: null, error: error.message });
      return;
    }
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const keep = (text, chunk) => (text.length < OUTPUT_MAX_BYTES ? text + chunk.toString() : text);
    child.stdout.on("data", (chunk) => { stdout = keep(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = keep(stderr, chunk); });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    timer.unref?.();
    child.once("error", (error) => {
      clearTimeout(timer);
      resolvePromise({ code: null, json: null, error: error.message });
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (timedOut) {
        resolvePromise({ code: null, json: null, error: `timed out after ${Math.round(timeoutMs / 1000)} s` });
        return;
      }
      const json = code === 0 ? parseCliJson(stdout) : (parseCliJson(stderr) ?? parseCliJson(stdout));
      resolvePromise({ code, json, error: json ? null : (stderr.trim().slice(-300) || null) });
    });
  });
}

/** Start a command that outlives the Canvas: its own process group, no pipes, not awaited. */
export function spawnDetachedCli(argv, { env = process.env } = {}) {
  try {
    const child = spawn(argv[0], argv.slice(1), { env, stdio: "ignore", detached: true });
    child.on("error", () => {});
    child.unref();
    return true;
  } catch {
    return false;
  }
}

function errorOf(result, fallback) {
  const json = result?.json;
  if (json && typeof json === "object" && json.ok === false) {
    return { error_code: String(json.error_code ?? "backend_failed"), error: String(json.error ?? fallback) };
  }
  return { error_code: "backend_failed", error: String(result?.error ?? fallback).slice(0, 300) };
}

/**
 * The session manager of one Canvas. `port()` is the Canvas port (known after listen);
 * `run(argv, {timeoutMs})` runs a CLI command and answers {code, json}.
 */
export function createSessionManager({
  python, autonomPath, env = process.env, port, pid = process.pid,
  run = (argv, options) => runCliJson(argv, { env, ...options }),
  spawnDetached = (argv) => spawnDetachedCli(argv, { env }),
  isAlive = pidAlive,
  timeoutMs = SESSION_CLI_TIMEOUT_MS,
}) {
  const base = () => [python, autonomPath];

  const targetArgs = ({ platform, target, tool, idb }) => (platform === "ios"
    ? ["--platform", "ios", "--udid", target, ...(tool ? ["--simctl", tool] : []), ...(idb ? ["--idb", idb] : [])]
    : ["--platform", "android", "--serial", target, ...(tool ? ["--adb", tool] : [])]);

  const stopArgv = (sessionId) => [...base(), "session", "stop", "--session-id", sessionId];
  // Releases still running, by session id: an ensure that meets one of them waits for it.
  const releases = new Map();

  const stopSession = async (sessionId) => {
    let result;
    try {
      result = await run(stopArgv(sessionId), { timeoutMs });
    } catch (error) {
      result = { code: null, json: null, error: error?.message ?? String(error) };
    }
    if (result?.code === 0) return { stopped: true, error: null };
    return { stopped: false, error: errorOf(result, "session stop failed") };
  };

  return {
    /**
     * A session for this device: started, reused or adopted. Never rejects.
     *
     * A live session another Canvas started, whose process is gone, is never adopted as is:
     * that Canvas started a detached `session stop` for it at its own stop, which may run
     * (or still be tearing down) after this Canvas took it over. It is stopped here first
     * (joining that stop when it runs) and a fresh session is started. A session of this
     * Canvas whose release is still running is waited for the same way.
     */
    async ensure({ platform, target, tool, idb = null }) {
      const argv = [
        ...base(), ...targetArgs({ platform, target, tool, idb }),
        "session", "start", "--alongside", "--started-by", `canvas:${port()}:${pid}`,
        ...(platform === "ios" ? ["--log-stream"] : []),
      ];
      const start = async () => {
        try {
          return await run(argv, { timeoutMs });
        } catch (error) {
          return { code: null, json: null, error: error?.message ?? String(error) };
        }
      };
      let result = await start();
      let settled = null;
      for (let attempt = 0; ; attempt += 1) {
        const json = result?.json;
        if (result?.code === 0 && typeof json?.session?.session_id === "string") {
          return { session_id: json.session.session_id, started_by_canvas: true, reused: false, error: null };
        }
        if (!(json?.error_code === "session_already_active" && typeof json.session_id === "string")) break;
        const owner = json.started_by;
        const canvas = owner && owner.kind === "canvas" && Number.isInteger(owner.pid);
        const mine = canvas && owner.pid === pid;
        const orphaned = canvas && !mine && !isAlive(owner.pid);
        const releasing = mine ? releases.get(json.session_id) : null;
        if (!orphaned && !releasing) {
          return { session_id: json.session_id, started_by_canvas: Boolean(mine), reused: true, error: null };
        }
        if (attempt > 0) {
          // Still live after its stop: never hand out a session another stop may end.
          return { session_id: null, started_by_canvas: false, reused: false,
            error: settled?.error ?? errorOf(result, "the previous Canvas's session did not stop") };
        }
        settled = releasing ? await releasing : await stopSession(json.session_id);
        result = await start();
      }
      return { session_id: null, started_by_canvas: false, reused: false, error: errorOf(result, "session start failed") };
    },

    /** Stop a session the Canvas started; anything else is left alone. Never rejects. */
    async release(link) {
      if (!link?.started_by_canvas || !link.session_id) return { stopped: false, error: null };
      const sessionId = link.session_id;
      const stopping = releases.get(sessionId) ?? stopSession(sessionId);
      releases.set(sessionId, stopping);
      try {
        return await stopping;
      } finally {
        if (releases.get(sessionId) === stopping) releases.delete(sessionId);
      }
    },

    /** At Canvas stop: start the stops of its own sessions and do not wait for them. */
    releaseDetached(links) {
      const started = [];
      for (const link of links ?? []) {
        if (!link?.started_by_canvas || !link.session_id) continue;
        if (spawnDetached(stopArgv(link.session_id))) started.push(link.session_id);
      }
      return started;
    },
  };
}
