#!/usr/bin/env node
/**
 * Live acceptance of concurrent sessions and the shared control lease (run ws-p1p2-control,
 * contract section 8). Not part of `node --test tests/*.test.mjs`: it starts this checkout's
 * real `autonom` CLI and Canvas and drives real test devices.
 *
 *   node tests/live/control_split_live.mjs --evidence-dir DIR            (base phase)
 *   node tests/live/control_split_live.mjs --evidence-dir DIR --ios      (iOS phase)
 *   node tests/live/control_split_live.mjs --evidence-dir DIR --legacy   (single-device Canvas)
 *     [--adb PATH] [--xcrun PATH] [--python PATH] [--keep-home]
 *   node tests/live/control_split_live.mjs --dry-run [--ios | --legacy]  (prints the plan only)
 *   node tests/live/control_split_live.mjs --self-test                   (pure helpers only)
 *
 * Allowed devices only: AVD Autonom_Split_API36 on emulator-5584, AVD Autonom_Split2_API36 on
 * emulator-5586 and the Simulator Autonom-Split-Test (45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5).
 * Every target goes through guard() before a session, a Canvas or a device command sees it;
 * any other target, emulator-5554 above all, is refused, and an emulator on an allowed port
 * must answer `adb emu avd name` with its allowed AVD. Each run gets its own temporary
 * AUTONOM_HOME; every Canvas listens on --port 0 with --control-idle 20; no host OS setting
 * is changed. A device that is not running is booted with `autonom devices boot` and shut
 * down at the end only when this run booted it.
 *
 * Base (control-base.json): sessions A on emulator-5584 (--label live-a) and B on
 * emulator-5586 (--alongside --label live-b) and `session list`; a workspace Canvas attaches
 * emulator-5584 (reusing A); one generated Settings flow (launchApp, then 30 alternating
 * swipes each followed by waitForSettled) runs on both devices at once; once A has finished
 * 3 steps one human tap goes to the emulator-5584 tile through the page route (no Take over
 * click); flow A must end `interrupted` (control_held, control_lost) while flow B passes; the
 * CLI is refused, a second human tap is admitted, Hand back frees the device and a short
 * flow on A passes again; then idle release, an explicit Take over over an agent lease, a
 * dropped WebSocket client whose lease ends, an agent lease on emulator-5586, and the
 * evidence of each session naming only its own device.
 *
 * --ios (control-ios.json): a session on the Simulator, a workspace Canvas attaching it, a
 * human touch over /ws/control (idb) auto-acquires; the CLI is refused; Hand back; the CLI is
 * admitted. --legacy (control-legacy.json): `canvas serve --serial emulator-5584` with a
 * session on emulator-5584: /status keeps every 0.35.1 key, a page tap creates the lease, the
 * CLI is refused, Hand back, the CLI is admitted.
 *
 * Ctrl-C or SIGTERM: no new device work starts, running children are ended, the phase is
 * given 30 s to settle, then cleanup runs (emulators whose boot was cut short included).
 *
 * Evidence: {ok, phase, started_at, finished_at, devices, checks: {<name>: {ok, ...}}} plus a
 * `steps` log; never a token, cookie or bearer value. Exits 0 only when every check of the
 * phase is ok, 1 otherwise, 2 on bad arguments.
 */
import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import {
  CONTROL_GENERATION_STALE, CONTROL_HELD, leasePath, leaseState,
} from "../../plugins/autonom/skills/android-emulator-browser/scripts/canvas-control.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const AUTONOM = join(ROOT, "scripts/autonom.py");
const REFUSED_SERIAL = "emulator-5554";
// The only targets this script may touch (contract section 8).
const DEVICES = Object.freeze({
  a: Object.freeze({ platform: "android", target: "emulator-5584", name: "Autonom_Split_API36", avd: "Autonom_Split_API36", port: 5584 }),
  b: Object.freeze({ platform: "android", target: "emulator-5586", name: "Autonom_Split2_API36", avd: "Autonom_Split2_API36", port: 5586 }),
  ios: Object.freeze({ platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "Autonom-Split-Test" }),
});
const PHASE_DEVICES = Object.freeze({ base: ["a", "b"], ios: ["ios"], legacy: ["a"] });
const PHASE_FILES = Object.freeze({ base: "control-base.json", ios: "control-ios.json", legacy: "control-legacy.json" });
const CHECK_NAMES = Object.freeze({
  base: Object.freeze(["session_list", "flow_a_interrupted", "flow_b_passed", "no_agent_after_tap", "cli_refused",
    "human_tap_admitted", "hand_back_free", "rerun_passes", "idle_release", "takeover_explicit",
    "dropped_client_expired", "agent_lease_5586", "evidence_per_target"]),
  ios: Object.freeze(["auto_acquire_idb", "cli_refused", "hand_back", "cli_admitted"]),
  legacy: Object.freeze(["status_keys_superset", "page_tap_lease", "cli_refused", "hand_back", "cli_admitted"]),
});
// The /status keys of a single-device Canvas at 0.35.1 (9acc89c `sendStatus`).
const LEGACY_STATUS_KEYS = Object.freeze(["platform", "serial", "display", "transport", "requested_transport",
  "fallback_reason", "ffmpeg", "screenrecord_h264", "screenrecord_max_size", "direct_h264_url", "frames_sent",
  "last_frame_at", "stream_clients", "uptime_seconds", "last_error", "control_owner", "input_paused", "scrcpy",
  "idb", "tools"]);
// Flow commands that change the device (flow/executor.py MUTATING_COMMANDS).
const MUTATING_COMMANDS = Object.freeze(new Set(["launchApp", "stopApp", "clearState", "openLink", "tapOn",
  "longPressOn", "doubleTapOn", "setOrientation", "inputText", "pasteText", "eraseText", "pressKey", "back", "swipe",
  "scroll", "scrollUntilVisible", "setLocation", "setPermissions", "addMedia"]));
// CLI verbs that change a device (contract 5.3 MUTATING and BY ACTION), for the journal window check.
const MUTATING_VERB_PREFIXES = Object.freeze(["devices shutdown", "session launch", "session force-stop",
  "session clear", "session uninstall", "ui tap", "ui swipe", "ui pinch", "ui rotate", "ui shake", "ui type",
  "ui key", "ui accessibility", "open", "permissions", "location set", "location clear", "media add",
  "network attach", "network detach", "network stop", "metrics memory warn", "simulator"]);
const CONTROL_IDLE_S = 20;
const IDLE_WAIT_MS = 25_000;
const DROPPED_LIMIT_MS = 45_000;
const FLOW_APP = "com.android.settings";
const FLOW_SWIPES = 30;
const RERUN_SWIPES = 2;
const STEPS_BEFORE_TAP = 3;
const FLOW_TIMEOUT_MS = 900_000;
const ATTACH_WAIT_MS = 90_000;
// A fresh control socket starts the device stream when none runs; input waits for it, as the page does.
const STREAM_READY_MS = 60_000;
const TAP_POINT = Object.freeze({ x: 10, y: 10 });
const TOUCH_POINT = Object.freeze({ x: 0.5, y: 0.03 });
const liveSecrets = new Set();

const USAGE = `usage: control_split_live.mjs --evidence-dir DIR [--ios | --legacy]
  [--adb PATH] [--xcrun PATH] [--python PATH] [--keep-home]
       control_split_live.mjs --dry-run [--ios | --legacy]
       control_split_live.mjs --self-test`;

class UsageError extends Error {}

/** The run's options from argv, or a UsageError. One phase per run. */
function parseCli(argv) {
  let values;
  try {
    ({ values } = parseArgs({
      args: argv,
      strict: true,
      allowPositionals: false,
      options: {
        "evidence-dir": { type: "string" },
        ios: { type: "boolean", default: false },
        legacy: { type: "boolean", default: false },
        "dry-run": { type: "boolean", default: false },
        "self-test": { type: "boolean", default: false },
        "keep-home": { type: "boolean", default: false },
        adb: { type: "string", default: "adb" },
        xcrun: { type: "string", default: "xcrun" },
        python: { type: "string", default: "python3" },
        help: { type: "boolean", short: "h", default: false },
      },
    }));
  } catch (error) {
    throw new UsageError(error.message);
  }
  if (values.ios && values.legacy) throw new UsageError("--ios and --legacy are separate runs; pass one");
  const phase = values.ios ? "ios" : values.legacy ? "legacy" : "base";
  const options = {
    phase, evidenceDir: values["evidence-dir"] ?? null, dryRun: values["dry-run"], selfTest: values["self-test"],
    keepHome: values["keep-home"], adb: values.adb, xcrun: values.xcrun, python: values.python, help: values.help,
  };
  if (options.evidenceDir === "") throw new UsageError("--evidence-dir needs a directory");
  if (!options.help && !options.selfTest && !options.dryRun && !options.evidenceDir) {
    throw new UsageError("--evidence-dir is required");
  }
  return options;
}

// --- pure helpers (checked by --self-test) --------------------------------------------------

class StoppedError extends Error {}

/**
 * The run's stop switch. Once `stop()` ran (a signal, or the end of the phase) no new child
 * process, Canvas request or socket starts unless it is cleanup's own (`check(true)`);
 * pending sleeps end at once and in-flight requests are aborted through `signal`.
 */
function createStopper() {
  const controller = new AbortController();
  const wakers = new Set();
  const stopper = {
    get stopping() { return controller.signal.aborted; },
    signal: controller.signal,
    stop() {
      if (controller.signal.aborted) return;
      controller.abort(new StoppedError("the run is stopping"));
      for (const wake of wakers) wake();
      wakers.clear();
    },
    /** Throws StoppedError once stopping, except for cleanup's own work. */
    check(cleanupCall = false) {
      if (controller.signal.aborted && !cleanupCall) {
        throw new StoppedError("the run is stopping; no new device work starts");
      }
    },
    /** A sleep that ends early when the run stops. */
    sleep(milliseconds) {
      if (controller.signal.aborted) return Promise.resolve();
      return new Promise((resolvePromise) => {
        const done = () => {
          clearTimeout(timer);
          wakers.delete(done);
          resolvePromise();
        };
        const timer = setTimeout(done, milliseconds);
        wakers.add(done);
      });
    },
  };
  return stopper;
}

/** The AVD name from `adb emu avd name` output (`<name>\r\nOK`), or null. */
function avdNameFrom(output) {
  for (const raw of String(output ?? "").split(/\r?\n/)) {
    const line = raw.trim();
    if (line && line !== "OK" && line !== "KO" && !line.startsWith("KO:") && /^[A-Za-z0-9._-]+$/.test(line)) return line;
  }
  return null;
}

/** The allowed device for (platform, target), or an Error naming why it is refused. */
function allowedTarget(platform, target) {
  if (target === REFUSED_SERIAL) return new Error(`${REFUSED_SERIAL} is never used by this check`);
  const found = Object.values(DEVICES).find((device) => device.platform === platform && device.target === target);
  return found ?? new Error(`${platform}:${target ?? "(none)"} is not one of the allowed test devices`);
}

/** allowedTarget, throwing: every session, Canvas and device command goes through it. */
function guard(platform, target) {
  const found = allowedTarget(platform, target);
  if (found instanceof Error) throw found;
  return found;
}

/** The target flags of the CLI for one allowed device. */
function targetFlags(device, tools = {}) {
  guard(device.platform, device.target);
  if (device.platform === "android") {
    return ["--serial", device.target, ...(tools.adb && tools.adb !== "adb" ? ["--adb", tools.adb] : [])];
  }
  return ["--platform", "ios", "--udid", device.target,
    ...(tools.xcrun && tools.xcrun !== "xcrun" ? ["--simctl", tools.xcrun] : [])];
}

/** The Settings flow of the base phase: launchApp, then alternating swipes, each settled. */
function flowYaml(swipes, name = "Control split") {
  const lines = ["schema: autonom.dev/flow/v1", `appId: ${FLOW_APP}`, `name: ${name}`, "---", "- launchApp"];
  for (let i = 0; i < swipes; i += 1) {
    lines.push("- swipe:", `    direction: ${i % 2 === 0 ? "up" : "down"}`);
    lines.push("- waitForSettled:", "    timeoutMs: 3000");
  }
  return `${lines.join("\n")}\n`;
}

/** The Canvas page URL (with its #token) from the CLI's output, or null. */
function previewUrl(output) {
  const match = String(output).match(/Preview at (http:\/\/127\.0\.0\.1:(\d+)\/(?:#token=\S+)?)/);
  return match ? { url: match[1], port: Number(match[2]) } : null;
}

/** The device id of a workspace Canvas (`<platform>~<target>`) and its route prefix. */
function deviceId(device) {
  return `${device.platform}~${device.target}`;
}

function devicePath(id) {
  return `/d/${encodeURIComponent(id)}`;
}

/** A random Canvas token that never starts with "-" (argparse would read it as an option). */
function liveToken(random = randomBytes) {
  for (;;) {
    const token = random(18).toString("base64url");
    if (!token.startsWith("-")) return token;
  }
}

/** `text` with this run's tokens, `token=` values and Bearer values taken out. */
function redactSecrets(text, secrets = liveSecrets) {
  let out = String(text ?? "");
  for (const secret of secrets) if (secret) out = out.split(secret).join("<redacted>");
  return out
    .replace(/([#?&;]token=)[^&#\s"'<>\\]*/gi, "$1<redacted>")
    .replace(/(Bearer\s+)[^\s"'\\]+/gi, "$1<redacted>")
    .replace(/(autonom_session=)[^;\s"'\\]+/gi, "$1<redacted>");
}

function errorText(error, max = 300) {
  return redactSecrets(String(error?.message ?? error)).slice(0, max);
}

/** Lines of an NDJSON text, parsed; broken lines are skipped. */
function parseNdjson(text) {
  const out = [];
  for (const line of String(text ?? "").split("\n")) {
    if (!line.trim()) continue;
    try { out.push(JSON.parse(line)); } catch { /* a partial last line */ }
  }
  return out;
}

function eventMs(event) {
  return Date.parse(event?.wall_time ?? event?.timestamp ?? "");
}

/** How many steps of a run finished (any status). */
function finishedSteps(events) {
  return (events ?? []).filter((event) => event?.kind === "flow.step.finished").length;
}

/**
 * The flow side of `no_agent_after_tap`: after the grant time T at most one mutating step
 * finishes (the one admitted before T, RISK-002); the run has one `flow.step.refused` after T
 * and only `flow.run.finished` follows it; no other mutating step starts after T.
 */
function flowAfterTap(events, grantMs) {
  const list = (events ?? []).filter((event) => event && typeof event.kind === "string");
  const after = list.filter((event) => eventMs(event) > grantMs);
  const mutatingFinished = after.filter((event) => event.kind === "flow.step.finished" &&
    MUTATING_COMMANDS.has(event.payload?.command) && event.payload?.status !== "skipped");
  const refusedAt = list.findIndex((event) => event.kind === "flow.step.refused");
  const refused = refusedAt >= 0 ? list[refusedAt] : null;
  const tail = refusedAt >= 0 ? list.slice(refusedAt + 1).map((event) => event.kind) : null;
  const allowedStarts = new Set([refused?.payload?.step_index,
    ...mutatingFinished.map((event) => event.payload?.step_index)]);
  const strayStarts = after.filter((event) => event.kind === "flow.step.started" &&
    MUTATING_COMMANDS.has(event.payload?.command) && !allowedStarts.has(event.payload?.step_index));
  const ok = mutatingFinished.length <= 1 && refused !== null && eventMs(refused) > grantMs &&
    Array.isArray(tail) && tail.length === 1 && tail[0] === "flow.run.finished" && strayStarts.length === 0;
  return {
    ok,
    mutating_finished_after_grant: mutatingFinished.map((event) => ({ step_index: event.payload?.step_index,
      command: event.payload?.command })),
    refused: refused ? { step_index: refused.payload?.step_index ?? null, command: refused.payload?.command ?? null,
      error_code: refused.payload?.error_code ?? null, after_grant: eventMs(refused) > grantMs } : null,
    after_refused: tail,
    stray_mutating_starts: strayStarts.length,
  };
}

/** A journal entry is a device mutation made by an agent and admitted (ok). */
function agentMutation(entry) {
  if (!entry || entry.ok !== true) return false;
  if (entry.kind === "flow_step") return MUTATING_COMMANDS.has(entry.command) && entry.status === "passed";
  if (entry.kind !== "action" || (entry.origin ?? "agent") !== "agent") return false;
  const verb = String(entry.verb ?? "");
  return MUTATING_VERB_PREFIXES.some((prefix) => verb === prefix || verb.startsWith(`${prefix} `));
}

/** The highest journal `seq` in `entries` (0 for none). */
function maxSeq(entries) {
  return (entries ?? []).reduce((max, entry) => (Number.isInteger(entry?.seq) ? Math.max(max, entry.seq) : max), 0);
}

/**
 * The journal side of `no_agent_after_tap`: between the first human `ui tap` entry at or after
 * the grant and the Hand back, no admitted agent action mutates the device; a flow step
 * journaled there may be the one admitted before T (at most one).
 *
 * The Hand back of step 7 is `POST /control`, which journals nothing (only a `control` message
 * over /ws/control writes a `ui control` record). So the window ends at `handBackSeq`, the
 * highest seq of A's journal read right after the Hand back answered: every entry up to it
 * was written before or during the Hand back. A human `ui control` record inside the window,
 * when one exists, ends it earlier.
 */
function journalWindow(entries, grantMs, handBackSeq = null) {
  const list = (entries ?? []).filter((entry) => entry && Number.isInteger(entry.seq));
  // Journal `ts` has second precision: the window starts at the human tap's own entry.
  const graceMs = Math.floor(grantMs / 1000) * 1000;
  const tap = list.find((entry) => entry.kind === "action" && entry.origin === "human" && entry.verb === "ui tap" &&
    entry.ok === true && Date.parse(entry.ts) >= graceMs);
  if (!tap) return { ok: false, reason: "human_tap_not_journaled" };
  const record = list.find((entry) => entry.seq > tap.seq && entry.kind === "action" && entry.origin === "human" &&
    entry.verb === "ui control" && (!Number.isInteger(handBackSeq) || entry.seq <= handBackSeq));
  const bounded = Number.isInteger(handBackSeq) && handBackSeq >= tap.seq;
  // `end` is exclusive: the record itself, or the first entry after the Hand back answered.
  const end = record ? record.seq : bounded ? handBackSeq + 1 : null;
  if (end === null) {
    return { ok: false, from_seq: tap.seq, to_seq: null, reason: "hand_back_not_bounded" };
  }
  const inside = list.filter((entry) => entry.seq > tap.seq && entry.seq < end);
  const agentActions = inside.filter((entry) => entry.kind === "action" && agentMutation(entry));
  const flowSteps = inside.filter((entry) => entry.kind === "flow_step" && agentMutation(entry));
  return {
    ok: agentActions.length === 0 && flowSteps.length <= 1,
    from_seq: tap.seq, to_seq: end - 1, bounded_by: record ? "control_record" : "hand_back_answer",
    agent_mutations: agentActions.map((entry) => ({ seq: entry.seq, verb: entry.verb })),
    flow_steps_ok: flowSteps.length,
  };
}

/** A lease reading that relies on the human lease still held: void once the idle window passed. */
function idleWindow(sinceHumanMs, idleS = CONTROL_IDLE_S) {
  const seconds = Math.round(sinceHumanMs / 100) / 10;
  if (!Number.isFinite(sinceHumanMs) || sinceHumanMs >= idleS * 1000) {
    return { ok: false, since_human_s: Number.isFinite(seconds) ? seconds : null, reason: "idle_window_missed" };
  }
  return { ok: true, since_human_s: seconds };
}

/** Both sessions of the base run are listed live with their labels, `current` only on A. */
function sessionListOk(json, ids) {
  const sessions = Array.isArray(json?.sessions) ? json.sessions : [];
  const a = sessions.find((entry) => entry?.session_id === ids.a);
  const b = sessions.find((entry) => entry?.session_id === ids.b);
  return Boolean(a && b) && a.live === true && b.live === true && a.label === "live-a" && b.label === "live-b" &&
    a.current === true && b.current === false && a.target_id === DEVICES.a.target && b.target_id === DEVICES.b.target &&
    sessions.filter((entry) => entry?.current === true).length === 1;
}

/** Flow A ended interrupted by the human lease: exit 1, `interrupted`, control_held, control_lost. */
function interruptedOk(result) {
  const json = result?.json ?? {};
  return result?.code === 1 && json.status === "interrupted" && json.failure?.error_code === CONTROL_HELD &&
    json.failure?.failure_class === "control_lost";
}

/** A CLI answer refused with `code`: exit 2 and that error_code on stderr. */
function refusedWith(result, code) {
  return result?.code === 2 && result?.error?.error_code === code;
}

/** Detail file names of one session: `NNNN_<slug>.json`, every index used once. */
function detailNamesUnique(names) {
  const indexes = (names ?? []).map((name) => /^(\d+)_.*\.json$/.exec(name)?.[1]).filter(Boolean);
  return indexes.length === (names ?? []).length && new Set(indexes).size === indexes.length;
}

/** Every target id a text names, among the allowed devices and the refused serial. */
function targetsNamed(text) {
  const known = [...Object.values(DEVICES).map((device) => device.target), REFUSED_SERIAL];
  return known.filter((target) => String(text ?? "").includes(target));
}

/**
 * One session's evidence names only its own device: every journal entry and action detail
 * that names a known target names `own`, and nothing names another one.
 */
function evidenceNamesOnly(texts, own) {
  const foreign = [];
  for (const [where, text] of texts) {
    for (const target of targetsNamed(text)) if (target !== own) foreign.push({ where, target });
  }
  return { ok: foreign.length === 0, foreign: foreign.slice(0, 20) };
}

/**
 * Whether the device stream behind a control socket takes input: the last `state` message
 * names the session `streaming`, the signal the Canvas page waits for before its first input.
 */
function streamReady(messages) {
  const last = (messages ?? []).findLast((message) => message?.t === "state");
  return last?.session === "streaming";
}

/** The keys of `body` missing from the 0.35.1 /status set (none for a superset). */
function missingStatusKeys(body) {
  return LEGACY_STATUS_KEYS.filter((key) => !Object.hasOwn(body ?? {}, key));
}

/** The phase report is ok only when every expected check exists and is ok. */
function phaseOk(phase, checks) {
  return CHECK_NAMES[phase].every((name) => checks?.[name]?.ok === true);
}

/** Checks never reached are reported failed, never left out. */
function fillMissing(phase, checks) {
  for (const name of CHECK_NAMES[phase]) {
    if (!checks[name]) checks[name] = { ok: false, reason: "not_reached" };
  }
  return checks;
}

/** The plan printed by --dry-run: no device command, nothing written. */
function dryRunPlan(options) {
  const phase = options.phase;
  return {
    dry_run: true,
    phase,
    devices: PHASE_DEVICES[phase].map((key) => DEVICES[key]),
    refused: [REFUSED_SERIAL, "any target not listed above",
      "an emulator on an allowed port whose console names another AVD"],
    home: "a temporary AUTONOM_HOME passed to every child process (removed unless --keep-home)",
    canvas: phase === "legacy"
      ? `autonom canvas serve --serial ${DEVICES.a.target} --port 0 --control-idle ${CONTROL_IDLE_S}`
      : `autonom canvas serve --port 0 --control-idle ${CONTROL_IDLE_S} (workspace)`,
    checks: CHECK_NAMES[phase],
    flow: phase === "base" ? { app: FLOW_APP, swipes: FLOW_SWIPES, settled_after_each: true,
      human_tap_after_finished_steps: STEPS_BEFORE_TAP } : null,
    evidence: options.evidenceDir ? join(resolve(options.evidenceDir), PHASE_FILES[phase])
      : `<evidence dir>/${PHASE_FILES[phase]}`,
    cleanup: ["Canvas stopped", "agent leases released", "sessions stopped",
      "devices booted by this run shut down", "temporary AUTONOM_HOME removed (unless --keep-home)"],
  };
}

function selfTest() {
  // Targets: the three allowed devices pass; emulator-5554 and everything else is refused.
  assert.equal(allowedTarget("android", "emulator-5584").avd, "Autonom_Split_API36");
  assert.equal(allowedTarget("android", "emulator-5586").port, 5586);
  assert.equal(allowedTarget("ios", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5").name, "Autonom-Split-Test");
  assert.match(allowedTarget("android", "emulator-5554").message, /never used/);
  assert.match(allowedTarget("ios", "emulator-5554").message, /never used/);
  for (const [platform, target] of [["android", "emulator-5580"], ["android", "emulator-5582"], ["ios", "emulator-5584"],
    ["android", "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5"], ["ios", "3760A5A8-E59D-4AE6-B1AF-A626908D7B61"],
    ["android", undefined], ["android", "127.0.0.1:5555"]]) {
    assert.ok(allowedTarget(platform, target) instanceof Error, `${platform}:${target}`);
  }
  assert.throws(() => guard("android", "emulator-5554"), /never used/);
  assert.throws(() => guard("android", "R5CT1234"), /not one of the allowed/);
  assert.throws(() => targetFlags({ platform: "android", target: "emulator-5554" }), /never used/);
  assert.throws(() => targetFlags({ platform: "android", target: "emulator-5580" }), /not one of the allowed/);
  assert.deepEqual(targetFlags(DEVICES.a), ["--serial", "emulator-5584"]);
  assert.deepEqual(targetFlags(DEVICES.b, { adb: "/opt/adb" }), ["--serial", "emulator-5586", "--adb", "/opt/adb"]);
  assert.deepEqual(targetFlags(DEVICES.ios), ["--platform", "ios", "--udid", DEVICES.ios.target]);

  // Arguments: one phase per run, --evidence-dir unless dry-run or self-test.
  assert.equal(parseCli(["--evidence-dir", "/tmp/x"]).phase, "base");
  assert.equal(parseCli(["--evidence-dir", "/tmp/x", "--ios"]).phase, "ios");
  assert.equal(parseCli(["--evidence-dir", "/tmp/x", "--legacy"]).phase, "legacy");
  assert.equal(parseCli(["--dry-run"]).dryRun, true);
  assert.equal(parseCli(["--dry-run", "--legacy"]).phase, "legacy");
  assert.equal(parseCli(["--self-test"]).selfTest, true);
  assert.throws(() => parseCli([]), /--evidence-dir is required/);
  assert.throws(() => parseCli(["--evidence-dir", "/tmp/x", "--ios", "--legacy"]), /separate runs/);
  assert.throws(() => parseCli(["--evidence-dir", "/tmp/x", "--serial", "emulator-5554"]), UsageError);
  assert.throws(() => parseCli(["--evidence-dir", "/tmp/x", "emulator-5554"]), UsageError);
  assert.throws(() => parseCli(["--evidence-dir", ""]), /needs a directory/);
  assert.throws(() => parseCli(["--evidence-dir"]), UsageError);

  // The dry-run plan names only allowed devices and writes nowhere.
  for (const phase of ["base", "ios", "legacy"]) {
    const plan = dryRunPlan({ phase, evidenceDir: null });
    assert.equal(plan.dry_run, true);
    assert.ok(plan.devices.every((device) => !(allowedTarget(device.platform, device.target) instanceof Error)));
    assert.ok(!JSON.stringify(plan.devices).includes(REFUSED_SERIAL));
    assert.deepEqual(plan.checks, CHECK_NAMES[phase]);
    assert.ok(plan.evidence.endsWith(PHASE_FILES[phase]));
  }
  assert.match(dryRunPlan({ phase: "legacy" }).canvas, /--serial emulator-5584 --port 0 --control-idle 20/);

  // The generated flow: launchApp, then N alternating swipes each followed by waitForSettled.
  const yaml = flowYaml(FLOW_SWIPES);
  assert.ok(yaml.startsWith("schema: autonom.dev/flow/v1\nappId: com.android.settings\n"));
  assert.equal((yaml.match(/^- swipe:/gm) ?? []).length, FLOW_SWIPES);
  assert.equal((yaml.match(/^- waitForSettled:/gm) ?? []).length, FLOW_SWIPES);
  assert.equal((yaml.match(/direction: up/g) ?? []).length, FLOW_SWIPES / 2);
  assert.equal((yaml.match(/direction: down/g) ?? []).length, FLOW_SWIPES / 2);
  assert.match(flowYaml(1), /- launchApp\n- swipe:\n {4}direction: up\n- waitForSettled:\n {4}timeoutMs: 3000\n$/);

  assert.deepEqual(previewUrl("ready\nPreview at http://127.0.0.1:51234/#token=abc\n"),
    { url: "http://127.0.0.1:51234/#token=abc", port: 51234 });
  assert.equal(previewUrl("Preview at http://example.com:1/"), null);
  assert.equal(deviceId(DEVICES.a), "android~emulator-5584");
  assert.equal(devicePath("android~emulator-5584"), "/d/android~emulator-5584");

  let draws = 0;
  const dashFirst = (n) => Buffer.alloc(n, draws++ === 0 ? 0xf8 : 0x41);
  assert.ok(!liveToken(dashFirst).startsWith("-") && draws === 2);
  const token = "AbC-_123tokenValue";
  const secrets = new Set([token]);
  for (const text of [`http://127.0.0.1:1/#token=${token}`, `Authorization: Bearer ${token}`, `the ${token} leaked`,
    `ws://127.0.0.1:2/d/x/ws/control?token=${token}&origin=human`, `cookie autonom_session=${token}`]) {
    assert.ok(!redactSecrets(text, secrets).includes(token), text);
  }
  assert.equal(redactSecrets("Cookie: autonom_session=abc; x=1", new Set()), "Cookie: autonom_session=<redacted>; x=1");

  assert.deepEqual(parseNdjson('{"a":1}\n\n{"b":2}\n{"c"'), [{ a: 1 }, { b: 2 }]);

  // Flow events around the grant time T.
  const T = Date.parse("2026-10-10T14:00:10.000Z");
  const at = (offsetMs) => new Date(T + offsetMs).toISOString();
  const ev = (kind, offsetMs, payload = {}) => ({ kind, wall_time: at(offsetMs), payload });
  const before = [
    ev("flow.run.started", -9000),
    ev("flow.step.started", -8000, { step_index: 1, command: "launchApp" }),
    ev("flow.step.finished", -7000, { step_index: 1, command: "launchApp", status: "passed" }),
    ev("flow.step.started", -6000, { step_index: 2, command: "swipe" }),
    ev("flow.step.finished", -5000, { step_index: 2, command: "swipe", status: "passed" }),
    ev("flow.step.started", -4000, { step_index: 3, command: "waitForSettled" }),
    ev("flow.step.finished", -3000, { step_index: 3, command: "waitForSettled", status: "passed" }),
    ev("flow.step.started", -100, { step_index: 4, command: "swipe" }),
  ];
  assert.equal(finishedSteps(before), 3);
  const clean = [...before,
    ev("flow.step.finished", 200, { step_index: 4, command: "swipe", status: "passed" }),
    ev("flow.step.started", 300, { step_index: 5, command: "waitForSettled" }),
    ev("flow.step.finished", 900, { step_index: 5, command: "waitForSettled", status: "passed" }),
    ev("flow.step.started", 1000, { step_index: 6, command: "swipe" }),
    ev("flow.step.refused", 1001, { step_index: 6, command: "swipe", error_code: "control_held" }),
    ev("flow.run.finished", 1100, { status: "interrupted" })];
  assert.equal(flowAfterTap(clean, T).ok, true);
  assert.equal(flowAfterTap(clean, T).mutating_finished_after_grant.length, 1, "RISK-002: one admitted before T");
  const twoAfter = [...before,
    ev("flow.step.finished", 200, { step_index: 4, command: "swipe", status: "passed" }),
    ev("flow.step.started", 300, { step_index: 5, command: "swipe" }),
    ev("flow.step.finished", 600, { step_index: 5, command: "swipe", status: "passed" }),
    ev("flow.step.started", 700, { step_index: 6, command: "swipe" }),
    ev("flow.step.refused", 701, { step_index: 6, command: "swipe", error_code: "control_held" }),
    ev("flow.run.finished", 800, { status: "interrupted" })];
  assert.equal(flowAfterTap(twoAfter, T).ok, false, "a second mutation after T fails");
  assert.equal(flowAfterTap(before, T).ok, false, "no refusal fails");
  const extraTail = [...clean.slice(0, -1), ev("flow.step.started", 1050, { step_index: 7, command: "swipe" }),
    ev("flow.run.finished", 1100, { status: "interrupted" })];
  assert.equal(flowAfterTap(extraTail, T).ok, false, "only flow.run.finished follows the refusal");
  const refusedBefore = [...before.slice(0, -1), ev("flow.step.refused", -50, { step_index: 4, command: "swipe" }),
    ev("flow.run.finished", -40, {})];
  assert.equal(flowAfterTap(refusedBefore, T).ok, false, "a refusal before the grant is not this tap's");

  // Journal window between the human tap and the Hand back.
  const tsAt = (offsetMs) => new Date(Math.floor((T + offsetMs) / 1000) * 1000).toISOString().replace(".000Z", "Z");
  const journal = [
    { seq: 1, ts: tsAt(-9000), kind: "action", verb: "ui tap", origin: "agent", ok: true },
    { seq: 2, ts: tsAt(0), kind: "action", verb: "ui tap", origin: "human", ok: true },
    { seq: 3, ts: tsAt(300), kind: "flow_step", verb: "flow run", command: "swipe", status: "passed", ok: true },
    { seq: 4, ts: tsAt(500), kind: "action", verb: "control status", origin: "agent", ok: true },
    { seq: 5, ts: tsAt(900), kind: "action", verb: "ui tap", origin: "agent", ok: false, error_code: "control_held" },
    { seq: 6, ts: tsAt(1000), kind: "action", verb: "flow run", origin: "agent", ok: false },
    { seq: 7, ts: tsAt(2000), kind: "action", verb: "ui tap", origin: "human", ok: true },
    { seq: 8, ts: tsAt(3000), kind: "action", verb: "ui control", origin: "human", ok: true },
    { seq: 9, ts: tsAt(9000), kind: "action", verb: "ui tap", origin: "agent", ok: true },
  ];
  const window = journalWindow(journal, T);
  assert.equal(window.ok, true, JSON.stringify(window));
  assert.deepEqual([window.from_seq, window.to_seq, window.bounded_by], [2, 7, "control_record"]);
  const leaked = journal.map((entry) => (entry.seq === 4 ? { ...entry, verb: "ui swipe" } : entry));
  assert.equal(journalWindow(leaked, T).ok, false, "an agent swipe inside the window fails");
  // The live Hand back is POST /control: no `ui control` record, the window ends at the seq
  // read right after the Hand back answered (7 here); the agent tap at seq 9 lies outside.
  const httpHandBack = journal.filter((entry) => entry.seq !== 8);
  const byAnswer = journalWindow(httpHandBack, T, 7);
  assert.equal(byAnswer.ok, true, JSON.stringify(byAnswer));
  assert.deepEqual([byAnswer.from_seq, byAnswer.to_seq, byAnswer.bounded_by], [2, 7, "hand_back_answer"]);
  assert.equal(journalWindow(httpHandBack, T, maxSeq(httpHandBack)).ok, false, "seq 9 inside a later bound fails");
  assert.equal(journalWindow(httpHandBack, T).reason, "hand_back_not_bounded", "no record and no bound fails");
  assert.equal(journalWindow(httpHandBack, T, 1).reason, "hand_back_not_bounded", "a bound before the tap fails");
  assert.equal(journalWindow(leaked.filter((entry) => entry.seq !== 8), T, 7).ok, false, "a swipe before the bound fails");
  assert.equal(journalWindow(journal, T, 7).bounded_by, "hand_back_answer", "a record after the bound is not used");
  assert.equal(maxSeq(journal), 9);
  assert.equal(maxSeq([]), 0);
  assert.equal(journalWindow(journal.filter((entry) => entry.origin !== "human"), T).reason, "human_tap_not_journaled");
  assert.equal(agentMutation({ kind: "action", verb: "simulator battery", origin: "agent", ok: true }), true);
  assert.equal(agentMutation({ kind: "action", verb: "ui tree", origin: "agent", ok: true }), false);
  assert.equal(agentMutation({ kind: "action", verb: "ui tap", origin: "human", ok: true }), false);
  assert.equal(agentMutation({ kind: "flow_step", command: "waitForSettled", status: "passed", ok: true }), false);

  // Lease readings that rely on a held human lease are void past the idle window.
  assert.deepEqual(idleWindow(4200), { ok: true, since_human_s: 4.2 });
  assert.deepEqual(idleWindow(20_000), { ok: false, since_human_s: 20, reason: "idle_window_missed" });
  assert.equal(idleWindow(Number.NaN).reason, "idle_window_missed");

  const ids = { a: "s_a", b: "s_b" };
  const listed = { sessions: [
    { session_id: "s_a", target_id: "emulator-5584", label: "live-a", current: true, live: true },
    { session_id: "s_b", target_id: "emulator-5586", label: "live-b", current: false, live: true }] };
  assert.equal(sessionListOk(listed, ids), true);
  assert.equal(sessionListOk({ sessions: [listed.sessions[0], { ...listed.sessions[1], current: true }] }, ids), false);
  assert.equal(sessionListOk({ sessions: [listed.sessions[0], { ...listed.sessions[1], label: null }] }, ids), false);
  assert.equal(sessionListOk({ sessions: [listed.sessions[0]] }, ids), false);
  assert.equal(sessionListOk(null, ids), false);

  assert.equal(interruptedOk({ code: 1, json: { status: "interrupted",
    failure: { error_code: "control_held", failure_class: "control_lost" } } }), true);
  assert.equal(interruptedOk({ code: 1, json: { status: "failed",
    failure: { error_code: "control_held", failure_class: "control_lost" } } }), false);
  assert.equal(interruptedOk({ code: 0, json: { status: "passed" } }), false);
  assert.equal(refusedWith({ code: 2, error: { error_code: "control_held" } }, CONTROL_HELD), true);
  assert.equal(refusedWith({ code: 2, error: { error_code: "control_held" } }, CONTROL_GENERATION_STALE), false);
  assert.equal(refusedWith({ code: 0, error: null }, CONTROL_HELD), false);

  assert.equal(detailNamesUnique(["0001_ui-tap.json", "0002_canvas-tap.json"]), true);
  assert.equal(detailNamesUnique(["0001_ui-tap.json", "0001_canvas-tap.json"]), false);
  assert.equal(detailNamesUnique(["notes.json"]), false);
  assert.equal(detailNamesUnique([]), true);
  assert.deepEqual(targetsNamed('{"serial":"emulator-5584","x":"emulator-5586"}'), ["emulator-5584", "emulator-5586"]);
  assert.equal(evidenceNamesOnly([["j", '{"target_id":"emulator-5584"}'], ["d", "{}"]], "emulator-5584").ok, true);
  assert.equal(evidenceNamesOnly([["j", '{"target_id":"emulator-5586"}']], "emulator-5584").ok, false);
  assert.equal(evidenceNamesOnly([["j", "emulator-5554"]], "emulator-5584").ok, false);

  assert.equal(streamReady([]), false);
  assert.equal(streamReady([{ t: "state", session: "starting" }]), false);
  assert.equal(streamReady([{ t: "state", session: "starting" }, { t: "pong" }, { t: "state", session: "streaming" }]), true);
  assert.equal(streamReady([{ t: "state", session: "streaming" }, { t: "state", session: "restarting" }]), false);
  assert.equal(streamReady([{ t: "error", session: "streaming" }]), false);

  assert.deepEqual(missingStatusKeys(Object.fromEntries(LEGACY_STATUS_KEYS.map((key) => [key, null]))), []);
  assert.deepEqual(missingStatusKeys({ ...Object.fromEntries(LEGACY_STATUS_KEYS.map((key) => [key, 1])), lease: {} }), []);
  assert.deepEqual(missingStatusKeys({ platform: "android" }), LEGACY_STATUS_KEYS.slice(1));

  const checks = fillMissing("ios", { auto_acquire_idb: { ok: true } });
  assert.deepEqual(checks.cli_refused, { ok: false, reason: "not_reached" });
  assert.equal(phaseOk("ios", checks), false);
  assert.equal(phaseOk("ios", Object.fromEntries(CHECK_NAMES.ios.map((name) => [name, { ok: true }]))), true);
  assert.equal(phaseOk("legacy", {}), false);

  // The lease helpers this script reads with: the same path rule as the CLI.
  assert.ok(leasePath({ AUTONOM_HOME: "/tmp/h" }, "android", "emulator-5584").endsWith("/control/android__emulator-5584.json"));
  assert.equal(leaseState(null, 0), "none");

  // The AVD behind a serial, from `adb emu avd name`.
  assert.equal(avdNameFrom("Autonom_Split_API36\r\nOK\r\n"), "Autonom_Split_API36");
  assert.equal(avdNameFrom("OK\n"), null);
  assert.equal(avdNameFrom("KO: unknown command\n"), null);
  assert.equal(avdNameFrom(""), null);
  assert.equal(avdNameFrom("error: device offline\n"), null);
}

/** The stop switch: no new work after stop, sleeps end early, cleanup's own work still runs. */
async function selfTestStopper() {
  const stopper = createStopper();
  assert.equal(stopper.stopping, false);
  stopper.check();
  const started = Date.now();
  const sleeping = stopper.sleep(5000);
  stopper.stop();
  await sleeping;
  assert.ok(Date.now() - started < 1000, "a sleep ends when the run stops");
  assert.equal(stopper.stopping, true);
  assert.ok(stopper.signal.aborted);
  assert.throws(() => stopper.check(), StoppedError);
  stopper.check(true);
  const after = Date.now();
  await stopper.sleep(5000);
  assert.ok(Date.now() - after < 100, "a sleep after stop does not wait");
  stopper.stop();
}

// --- entry ----------------------------------------------------------------------------------

let options;
try {
  options = parseCli(process.argv.slice(2));
} catch (error) {
  if (!(error instanceof UsageError)) throw error;
  console.error(`control_split_live: ${error.message}`);
  console.error(USAGE);
  process.exit(2);
}
if (options.help) {
  console.log(USAGE);
  process.exit(0);
}
if (options.selfTest) {
  selfTest();
  await selfTestStopper();
  console.log("ok: control_split_live self-test");
  process.exit(0);
}
if (options.dryRun) {
  console.log(JSON.stringify(dryRunPlan(options), null, 2));
  process.exit(0);
}
// Every device of the phase passes the allowlist before anything starts.
for (const key of PHASE_DEVICES[options.phase]) guard(DEVICES[key].platform, DEVICES[key].target);

const tools = { adb: options.adb, xcrun: options.xcrun };
const outDir = resolve(options.evidenceDir);
await mkdir(outDir, { recursive: true });
const autonomHome = await mkdtemp(join(tmpdir(), "autonom-control-live-home-"));
const liveEnv = { ...process.env, AUTONOM_HOME: autonomHome };
delete liveEnv.XDG_STATE_HOME;
delete liveEnv.AUTONOM_CONTROL_HOLDER;
delete liveEnv.AUTONOM_CONTROL_GENERATION;
delete liveEnv.AUTONOM_TEST_MODE;
delete liveEnv.AUTONOM_CONTROL_NOW_MS;
delete liveEnv.AUTONOM_CANVAS_HUMAN_TTL_S;

const report = {
  ok: false, phase: options.phase, started_at: new Date().toISOString(), finished_at: null,
  devices: PHASE_DEVICES[options.phase].map((key) => ({ ...DEVICES[key] })), checks: {}, steps: [],
};
// emulatorsBooted / simulatorsBooted: {device, confirmed, interrupted}. An entry is added
// before `devices boot` starts, so a boot cut short (a signal, a timeout) is still shut down
// when its device turns up; `confirmed` once the boot answered that it started the device.
const state = {
  canvases: new Set(), startingCanvases: new Set(), sockets: new Set(), sessions: [], emulatorsBooted: [],
  simulatorsBooted: [], agentLeases: [], lastHumanAt: Number.NaN, evidenceWarnings: 0, children: new Set(),
};
const stopper = createStopper();

async function writeReport() {
  const file = join(outDir, PHASE_FILES[options.phase]);
  await writeFile(file, `${redactSecrets(JSON.stringify(report, null, 2))}\n`);
  return file;
}

function note(name, ok, detail = {}) {
  const entry = { name, ok: Boolean(ok), at: new Date().toISOString(), ...detail };
  report.steps.push(entry);
  console.error(`${entry.at} ${entry.ok ? "ok" : "FAILED"} ${options.phase}: ${name}`);
  writeReport().catch(() => {});
  return entry.ok;
}

function check(name, ok, observed = {}) {
  report.checks[name] = { ok: Boolean(ok), ...observed };
  console.error(`${new Date().toISOString()} ${ok ? "ok" : "FAILED"} check ${name}`);
  writeReport().catch(() => {});
  return Boolean(ok);
}

// --- process helpers ------------------------------------------------------------------------

/** A sleep of the phase: it ends early once the run stops. */
function sleep(milliseconds) {
  return stopper.sleep(milliseconds);
}

/** A sleep that always waits (cleanup and the final wait for the phase). */
function rawSleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

/**
 * One child process; tracked so cleanup can end a flow still running after a failure. Once
 * the run stops only cleanup's own commands (`cleanup: true`) start; any other call rejects
 * with StoppedError, so a phase cut short by a signal ends instead of driving a device on.
 */
function run(file, argv, { timeout = 60_000, env = liveEnv, cleanup = false } = {}) {
  try {
    stopper.check(cleanup);
  } catch (error) {
    return Promise.reject(error);
  }
  return new Promise((resolvePromise) => {
    const child = execFile(file, argv, { timeout, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env, cwd: ROOT },
      (error, stdout, stderr) => {
        state.children.delete(child);
        resolvePromise({ code: error ? (error.code ?? 1) : 0, signal: error?.signal ?? null,
          stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
      });
    state.children.add(child);
  });
}

/** adb on one allowed emulator. */
async function adb(target, argv, runOptions) {
  guard("android", target);
  return await run(options.adb, ["-s", target, ...argv], runOptions);
}

/** The Autonom CLI in this run's AUTONOM_HOME; JSON stdout and the stderr envelope parsed. */
async function autonom(argv, runOptions = {}) {
  const result = await run(options.python, [AUTONOM, ...argv], { timeout: 240_000, ...runOptions });
  try { result.json = JSON.parse(result.stdout); } catch { result.json = null; }
  try { result.error = JSON.parse(result.stderr.trim().split("\n").pop() || "null"); } catch { result.error = null; }
  if (result.stdout.includes("evidence_write_failed") || result.stderr.includes("evidence_write_failed")) {
    state.evidenceWarnings += 1;
  }
  return result;
}

async function waitFor(predicate, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    stopper.check();
    const value = await predicate();
    if (value || Date.now() >= deadline) return value;
    await sleep(intervalMs);
  }
}

// --- devices --------------------------------------------------------------------------------

async function androidOnline(device, runOptions) {
  return (await adb(device.target, ["get-state"], runOptions)).stdout.trim() === "device";
}

/** The AVD the emulator on an allowed serial runs, from its console, or null. */
async function runningAvd(device, runOptions) {
  return avdNameFrom((await adb(device.target, ["emu", "avd", "name"], { timeout: 15_000, ...runOptions })).stdout);
}

/**
 * The emulator on the serial must run the allowed AVD (contract section 8): another AVD that
 * happens to listen on emulator-5584/5586 is refused before any session or device command.
 */
async function verifyAvd(device) {
  let avd = null;
  for (let attempt = 0; attempt < 3 && avd === null; attempt += 1) {
    if (attempt > 0) await sleep(1000);
    avd = await runningAvd(device);
  }
  note(`${device.target} runs ${device.avd}`, avd === device.avd, { avd });
  if (avd !== device.avd) {
    throw new Error(`${device.target} runs AVD ${avd ?? "(console did not answer)"}, not ${device.avd}; refusing it`);
  }
}

/** An emulator of an allowed AVD on its fixed port; booted here when it was not running. */
async function ensureEmulator(device) {
  guard(device.platform, device.target);
  if (await androidOnline(device)) {
    await verifyAvd(device);
    return false;
  }
  // Recorded before the boot starts: an emulator launched by a boot that is cut short keeps
  // running in its own session, and cleanup shuts it down once its console names our AVD.
  const entry = { device, confirmed: false, interrupted: false };
  state.emulatorsBooted.push(entry);
  let booted;
  try {
    booted = await autonom(["devices", "boot", "--avd", device.avd, "--port", String(device.port), "--timeout", "180",
      ...(options.adb !== "adb" ? ["--adb", options.adb] : [])], { timeout: 260_000 });
  } catch (error) {
    entry.interrupted = true;
    throw error;
  }
  if (stopper.stopping || booted.signal) entry.interrupted = true;
  if (booted.code !== 0 || booted.json?.serial !== device.target) {
    throw new Error(`devices boot ${device.avd} failed: ${errorText(booted.error?.error ?? booted.stderr, 200)}`);
  }
  if (booted.json.already_running) state.emulatorsBooted.splice(state.emulatorsBooted.indexOf(entry), 1);
  else entry.confirmed = true;
  await verifyAvd(device);
  return true;
}

/** The simctl entry of the allowed Simulator, or null. */
async function simulatorEntry(device, runOptions) {
  const listed = await run(options.xcrun, ["simctl", "list", "devices", "--json"], runOptions);
  let parsed = {};
  try { parsed = JSON.parse(listed.stdout || "{}"); } catch { parsed = {}; }
  return Object.values(parsed.devices ?? {}).flat().find((item) => item.udid === device.target) ?? null;
}

/** The allowed Simulator in state Booted; booted with `autonom devices boot` when it was Shutdown. */
async function ensureSimulator(device) {
  guard(device.platform, device.target);
  const found = await simulatorEntry(device);
  if (!found) throw new Error(`${device.name} is not listed by simctl`);
  if (found.name !== device.name) throw new Error(`${device.target} is named ${found.name}; refusing it`);
  if (found.state === "Booted") return false;
  if (found.state !== "Shutdown") throw new Error(`${device.name} is ${found.state}; it must be Booted or Shutdown`);
  // It was Shutdown: whatever state it reaches from here on is this run's boot.
  const entry = { device, confirmed: false, interrupted: false };
  state.simulatorsBooted.push(entry);
  let booted;
  try {
    booted = await autonom(["devices", "boot", ...targetFlags(device, tools)], { timeout: 320_000 });
  } catch (error) {
    entry.interrupted = true;
    throw error;
  }
  if (stopper.stopping || booted.signal) entry.interrupted = true;
  if (booted.code !== 0) throw new Error(`devices boot ${device.name} failed: ${errorText(booted.error?.error ?? booted.stderr, 200)}`);
  entry.confirmed = true;
  return true;
}

// --- sessions, journal, leases --------------------------------------------------------------

async function startSession(device, label, extra = []) {
  const started = await autonom([...targetFlags(device, tools), "session", "start", ...extra, "--label", label]);
  const id = started.json?.session?.session_id ?? null;
  if (id) state.sessions.push(id);
  note(`session ${label} on ${device.target}`, started.code === 0 && id,
    { session_id: id, error_code: started.error?.error_code ?? null });
  if (!id) throw new Error(`session start ${label} failed: ${errorText(started.error?.error ?? started.stderr, 200)}`);
  return id;
}

async function sessionRecord(sessionId) {
  const shown = await autonom(["--session-id", sessionId, "session", "show"]);
  return shown.json?.session ?? null;
}

const artifactDirs = new Map();

/** A session's artifacts directory, read once (polling must not spawn a CLI each time). */
async function artifactsDir(sessionId) {
  if (!artifactDirs.has(sessionId)) {
    const record = await sessionRecord(sessionId);
    if (!record?.artifacts_dir) return null;
    artifactDirs.set(sessionId, record.artifacts_dir);
  }
  return artifactDirs.get(sessionId);
}

async function journalOf(sessionId) {
  const read = await autonom(["--session-id", sessionId, "journal", "--max", "5000"]);
  return Array.isArray(read.json?.entries) ? read.json.entries : [];
}

/** Every action detail of a session: [{name, text, json}]. */
async function detailsOf(sessionId) {
  const root = await artifactsDir(sessionId);
  const directory = root ? join(root, "actions") : null;
  if (!directory || !existsSync(directory)) return [];
  const names = (await readdir(directory)).filter((name) => name.endsWith(".json")).sort();
  const out = [];
  for (const name of names) {
    const text = await readFile(join(directory, name), "utf8");
    let json = null;
    try { json = JSON.parse(text); } catch { json = null; }
    out.push({ name, text, json });
  }
  return out;
}

/** The lease file of a target, read directly (the CLI's status has no acquired_at). */
function leaseFile(device) {
  guard(device.platform, device.target);
  const path = leasePath(liveEnv, device.platform, device.target);
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

async function controlStatus(device) {
  const status = await autonom([...targetFlags(device, tools), "control", "status"]);
  return { code: status.code, lease: status.json?.lease ?? null, error_code: status.error?.error_code ?? null };
}

function leaseView(lease) {
  return lease ? { state: lease.state ?? null, generation: lease.generation ?? null,
    holder: lease.holder ? { kind: lease.holder.kind, id: lease.holder.id, label: lease.holder.label } : null } : null;
}

async function uiTap(device, extra = []) {
  return await autonom([...extra, ...targetFlags(device, tools), "ui", "tap", "--x", String(TAP_POINT.x),
    "--y", String(TAP_POINT.y)]);
}

// --- the Canvas -----------------------------------------------------------------------------

/** `autonom canvas serve --port 0 --control-idle 20`, workspace unless `extra` names a target. */
async function startCanvas(extra = []) {
  stopper.check();
  const token = liveToken();
  liveSecrets.add(token);
  const argv = [AUTONOM, ...(options.adb !== "adb" ? ["--adb", options.adb] : []),
    ...(options.xcrun !== "xcrun" ? ["--simctl", options.xcrun] : []),
    "canvas", "serve", ...extra, "--port", "0", `--token=${token}`, "--control-idle", String(CONTROL_IDLE_S)];
  const child = spawn(options.python, argv, { cwd: ROOT, env: liveEnv, stdio: ["ignore", "pipe", "pipe"] });
  // Until it printed its URL (and `canvas stop` can reach it) cleanup ends it with a signal.
  state.startingCanvases.add(child);
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk.toString(); });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const exited = new Promise((resolvePromise) => child.once("exit", (code, signal) => resolvePromise(code ?? signal)));
  const running = () => child.exitCode === null && child.signalCode === null;
  let found;
  try {
    found = await new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => reject(new Error("the Canvas did not print its URL within 60 s")), 60_000);
      child.stdout.on("data", () => {
        const match = previewUrl(output);
        if (!match) return;
        clearTimeout(timer);
        resolvePromise(match);
      });
      exited.then((code) => {
        clearTimeout(timer);
        reject(new Error(`the Canvas exited ${code} before it started: ${errorText(output, 300)}`));
      });
    });
  } catch (error) {
    if (running()) child.kill("SIGTERM");
    await Promise.race([exited, rawSleep(8000)]);
    state.startingCanvases.delete(child);
    throw error;
  }
  const origin = `http://127.0.0.1:${found.port}`;
  /** One HTTP request; `as` is the X-Autonom-Origin (human = the page's own input). */
  const request = async (method, path, body, { as = "agent", timeoutMs = 120_000 } = {}) => {
    stopper.check();
    const headers = { Authorization: `Bearer ${token}`, "X-Autonom-Origin": as };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await fetch(`${origin}${path}`, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.any([AbortSignal.timeout(timeoutMs), stopper.signal]),
      });
      let payload = null;
      try { payload = await response.json(); } catch { payload = null; }
      if (JSON.stringify(payload ?? {}).includes("evidence_write_failed")) state.evidenceWarnings += 1;
      return { status: response.status, body: payload ?? {} };
    } catch (error) {
      return { status: 0, body: {}, error: errorText(error, 200) };
    }
  };
  const canvas = {
    port: found.port, token, running, exited, request,
    stop: async () => {
      const stopped = await autonom(["canvas", "stop", "--port", String(found.port)], { timeout: 60_000, cleanup: true });
      const code = await Promise.race([exited, rawSleep(20_000).then(() => "timeout")]);
      if (code === "timeout" && running()) {
        child.kill("SIGTERM");
        await Promise.race([exited, rawSleep(8000)]);
      }
      state.canvases.delete(canvas);
      return { ok: stopped.code === 0 && !running(), stop_code: stopped.code };
    },
  };
  state.canvases.add(canvas);
  state.startingCanvases.delete(child);
  return canvas;
}

async function attach(canvas, device) {
  guard(device.platform, device.target);
  const attached = await autonom(["canvas", "attach", "--port", String(canvas.port), ...targetFlags(device, tools)],
    { timeout: 180_000 });
  return { code: attached.code, session: attached.json?.device?.session ?? null,
    error_code: attached.error?.error_code ?? null };
}

async function waitLive(canvas, id) {
  return await waitFor(async () => {
    const devices = (await canvas.request("GET", "/api/devices")).body?.devices ?? [];
    const summary = devices.find((item) => item.id === id) ?? null;
    return summary?.state === "live" ? summary : null;
  }, ATTACH_WAIT_MS, 1000);
}

/** One human tap through the page route of `base` ("" for the single-device Canvas). */
async function humanTap(canvas, base) {
  const answer = await canvas.request("POST", `${base}/tap`, { ...TAP_POINT }, { as: "human" });
  if (answer.status === 200) state.lastHumanAt = Date.now();
  return answer;
}

async function controlMode(canvas, base, mode) {
  const answer = await canvas.request("POST", `${base}/control`, { mode }, { as: "human" });
  if (answer.status === 200 && mode === "takeover") state.lastHumanAt = Date.now();
  return answer;
}

/** A human control socket; collects every message; `send` is a no-op once closed. */
async function humanSocket(canvas, base) {
  stopper.check();
  const ws = new WebSocket(`ws://127.0.0.1:${canvas.port}${base}/ws/control?token=${encodeURIComponent(canvas.token)}&origin=human`);
  const messages = [];
  ws.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    try { messages.push(JSON.parse(event.data)); } catch { /* not JSON */ }
  });
  const opened = await Promise.race([
    new Promise((resolvePromise) => {
      ws.addEventListener("open", () => resolvePromise(true));
      ws.addEventListener("error", () => resolvePromise(false));
    }),
    sleep(10_000).then(() => false),
  ]);
  state.sockets.add(ws);
  return {
    opened, messages,
    // Resolves true once the stream takes input (bounded); a closed socket never turns ready.
    ready: async (timeoutMs = STREAM_READY_MS) => opened && Boolean(await waitFor(
      () => streamReady(messages) || ws.readyState !== WebSocket.OPEN, timeoutMs, 100)) && streamReady(messages),
    errors: () => messages.filter((message) => message?.t === "error")
      .map((message) => ({ message: errorText(message.message, 160), code: message.code ?? null })),
    send: (message) => {
      if (!stopper.stopping && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
    },
    close: () => {
      state.sockets.delete(ws);
      try { ws.close(); } catch { /* closed */ }
    },
  };
}

/** One human touch (down, up) over a control socket. */
async function humanTouch(socket) {
  socket.send({ t: "touch", a: "down", id: 1, x: TOUCH_POINT.x, y: TOUCH_POINT.y });
  await sleep(80);
  socket.send({ t: "touch", a: "up", id: 1, x: TOUCH_POINT.x, y: TOUCH_POINT.y });
  state.lastHumanAt = Date.now();
}

function sinceHuman() {
  return idleWindow(Date.now() - state.lastHumanAt);
}

function isCanvasHolder(lease) {
  return lease?.holder?.kind === "human" && /^canvas:\d+:\d+$/.test(String(lease?.holder?.id ?? ""));
}

// --- flows ----------------------------------------------------------------------------------

async function flowFile(name, swipes) {
  const directory = join(autonomHome, "live-flows");
  await mkdir(directory, { recursive: true });
  const path = join(directory, `${name}.yaml`);
  await writeFile(path, flowYaml(swipes, name));
  return path;
}

async function runDirs(sessionId) {
  const root = await artifactsDir(sessionId);
  const directory = root ? join(root, "flows") : null;
  if (!directory || !existsSync(directory)) return { directory, names: [] };
  return { directory, names: await readdir(directory) };
}

/** The events of the flow run that appeared in `sessionId` after `known` was listed. */
async function newRunEvents(sessionId, known) {
  const { directory, names } = await runDirs(sessionId);
  const fresh = names.filter((name) => !known.has(name));
  for (const name of fresh) {
    const path = join(directory, name, "events.ndjson");
    if (!existsSync(path)) continue;
    return { path, events: parseNdjson(await readFile(path, "utf8")) };
  }
  return null;
}

async function flowRun(device, path) {
  return await autonom([...targetFlags(device, tools), "flow", "run", path], { timeout: FLOW_TIMEOUT_MS });
}

function flowView(result) {
  return { code: result?.code ?? null, status: result?.json?.status ?? null,
    failure: result?.json?.failure ? { error_code: result.json.failure.error_code ?? null,
      failure_class: result.json.failure.failure_class ?? null, step_index: result.json.failure.step_index ?? null,
      command: result.json.failure.command ?? null } : null,
    error_code: result?.error?.error_code ?? null };
}

// --- phases ---------------------------------------------------------------------------------

async function basePhase() {
  const A = DEVICES.a;
  const B = DEVICES.b;
  const tileA = devicePath(deviceId(A));
  await ensureEmulator(A);
  await ensureEmulator(B);

  // 1. Sessions A and B, listed with labels; current only on A.
  const idA = await startSession(A, "live-a");
  const idB = await startSession(B, "live-b", ["--alongside"]);
  const listed = await autonom(["session", "list"]);
  check("session_list", listed.code === 0 && sessionListOk(listed.json, { a: idA, b: idB }), {
    count: listed.json?.count ?? null,
    sessions: (listed.json?.sessions ?? []).map((entry) => ({ session_id: entry.session_id, target_id: entry.target_id,
      label: entry.label, current: entry.current, live: entry.live })),
  });

  // 2. A workspace Canvas attaches emulator-5584 and reuses A.
  const canvas = await startCanvas();
  const attached = await attach(canvas, A);
  note("Canvas attaches emulator-5584 reusing A", attached.code === 0 && attached.session?.id === idA &&
    attached.session?.reused === true, { session: attached.session, error_code: attached.error_code });
  const live = await waitLive(canvas, deviceId(A));
  note("emulator-5584 live in the Canvas", Boolean(live), { transport: live?.transport ?? null });
  if (!live) throw new Error("emulator-5584 did not turn live in the Canvas");

  // 3. The same flow on both devices at once.
  const flowPath = await flowFile("control-split", FLOW_SWIPES);
  const knownA = new Set((await runDirs(idA)).names);
  const flowA = flowRun(A, flowPath);
  const flowB = flowRun(B, flowPath);

  // 4. Once A finished 3 steps: one human tap on the emulator-5584 tile (no Take over click).
  const progress = await waitFor(async () => {
    const found = await newRunEvents(idA, knownA);
    return found && finishedSteps(found.events) >= STEPS_BEFORE_TAP ? found : null;
  }, 240_000, 250);
  if (!progress) throw new Error(`flow A did not finish ${STEPS_BEFORE_TAP} steps within 240 s`);
  const firstTap = await humanTap(canvas, tileA);
  note("human tap on the emulator-5584 tile", firstTap.status === 200,
    { http: firstTap.status, error_code: firstTap.body?.error_code ?? null, body_keys: Object.keys(firstTap.body ?? {}) });

  // 5. Flow A ends interrupted; the lease names the human at generation 1 (grant time T).
  const resultA = await flowA;
  const timingA = sinceHuman();
  const statusA = await controlStatus(A);
  const fileA = leaseFile(A);
  const grantMs = Date.parse(fileA?.acquired_at ?? "");
  check("flow_a_interrupted", interruptedOk(resultA) && statusA.lease?.holder?.kind === "human" &&
    statusA.lease?.generation === 1 && Number.isFinite(grantMs) && timingA.ok, {
    flow: flowView(resultA), lease: leaseView(statusA.lease), acquired_at: fileA?.acquired_at ?? null, ...timingA,
  });

  // 6. The CLI is refused; a second human tap is admitted and journaled as human in A.
  const refused = await uiTap(A);
  const timingRefused = sinceHuman();
  const journalAfterRefusal = await journalOf(idA);
  const refusedEntry = [...journalAfterRefusal].reverse().find((entry) => entry.verb === "ui tap" && entry.origin === "agent");
  check("cli_refused", refusedWith(refused, CONTROL_HELD) && refusedEntry?.ok === false &&
    refusedEntry?.error_code === CONTROL_HELD && timingRefused.ok, {
    code: refused.code, error_code: refused.error?.error_code ?? null, holder: refused.error?.holder ?? null,
    generation: refused.error?.generation ?? null, journal: refusedEntry ? { seq: refusedEntry.seq, ok: refusedEntry.ok,
      error_code: refusedEntry.error_code ?? null } : null, ...timingRefused,
  });
  const humansBefore = journalAfterRefusal.filter((entry) => entry.verb === "ui tap" && entry.origin === "human").length;
  const timingSecond = sinceHuman();
  const secondTap = await humanTap(canvas, tileA);
  await sleep(1000);
  const humansAfter = (await journalOf(idA)).filter((entry) => entry.verb === "ui tap" && entry.origin === "human").length;
  const afterSecond = leaseFile(A);
  check("human_tap_admitted", secondTap.status === 200 && humansAfter === humansBefore + 1 && timingSecond.ok &&
    afterSecond?.generation === 1 && isCanvasHolder(afterSecond), {
    http: secondTap.status, human_taps_journaled: [humansBefore, humansAfter], generation: afterSecond?.generation ?? null,
    ...timingSecond,
  });

  // 7. Hand back: free at generation 1.
  const timingHandBack = sinceHuman();
  const handBack = await controlMode(canvas, tileA, "release");
  // POST /control journals nothing, so the journal window of no_agent_after_tap ends at the
  // highest seq of A's journal once the Hand back answered (read before anything else runs on A).
  const handBackSeq = handBack.status === 200 ? maxSeq(await journalOf(idA)) : null;
  const freeA = await controlStatus(A);
  check("hand_back_free", handBack.status === 200 && handBack.body?.lease?.ended_by === "released" &&
    freeA.lease?.state === "free" && freeA.lease?.generation === 1 && timingHandBack.ok, {
    http: handBack.status, ended_by: handBack.body?.lease?.ended_by ?? null, lease: leaseView(freeA.lease), ...timingHandBack,
  });

  // no_agent_after_tap: the flow's events and A's journal between T and the Hand back.
  const runA = await newRunEvents(idA, knownA);
  const flowSide = Number.isFinite(grantMs) ? flowAfterTap(runA?.events ?? [], grantMs) : { ok: false, reason: "no_grant_time" };
  const journalSide = Number.isFinite(grantMs) ? journalWindow(await journalOf(idA), grantMs, handBackSeq)
    : { ok: false, reason: "no_grant_time" };
  check("no_agent_after_tap", flowSide.ok && journalSide.ok, { grant_at: fileA?.acquired_at ?? null,
    flow: flowSide, journal: journalSide, journal_verbs_checked: "agent device mutations (contract 5.3 MUTATING, BY ACTION)" });

  // Flow B ran beside it untouched.
  const resultB = await flowB;
  check("flow_b_passed", resultB.code === 0 && resultB.json?.status === "passed", { flow: flowView(resultB) });

  // A short flow on A passes again.
  const rerunPath = await flowFile("control-split-rerun", RERUN_SWIPES);
  const rerun = await flowRun(A, rerunPath);
  check("rerun_passes", rerun.code === 0 && rerun.json?.status === "passed", { flow: flowView(rerun) });

  // 8. Idle release: one human tap (generation 2), then no input for 25 s.
  const journalBeforeIdle = await journalOf(idA);
  const lastSeq = journalBeforeIdle.reduce((max, entry) => Math.max(max, entry.seq ?? 0), 0);
  const idleTap = await humanTap(canvas, tileA);
  await sleep(500);
  const heldIdle = leaseFile(A);
  await sleep(IDLE_WAIT_MS);
  const idleStatus = await controlStatus(A);
  const rootA = await artifactsDir(idA);
  const idleEntries = (await journalOf(idA)).filter((entry) => entry.seq > lastSeq && entry.verb === "ui control" &&
    entry.origin === "human");
  const idleModes = [];
  for (const entry of idleEntries) {
    const path = entry.result?.detail && rootA ? join(rootA, entry.result.detail) : null;
    try { idleModes.push(JSON.parse(await readFile(path, "utf8")).mode ?? null); } catch { idleModes.push(null); }
  }
  check("idle_release", idleTap.status === 200 && heldIdle?.generation === 2 && isCanvasHolder(heldIdle) &&
    idleStatus.lease?.state === "free" && idleStatus.lease?.generation === 2 && idleModes.includes("idle"), {
    http: idleTap.status, held_generation: heldIdle?.generation ?? null, lease: leaseView(idleStatus.lease),
    control_records: idleModes, waited_s: IDLE_WAIT_MS / 1000,
  });

  // 9. Explicit Take over of an agent lease.
  const agentAcquire = await autonom([...targetFlags(A, tools), "control", "acquire", "--holder", "agent:live"]);
  if (agentAcquire.code === 0) state.agentLeases.push({ device: A, holder: "agent:live" });
  const agentGeneration = agentAcquire.json?.generation ?? null;
  const refusedTap = await canvas.request("POST", `${tileA}/tap`, { ...TAP_POINT }, { as: "human" });
  const stillAgent = leaseFile(A);
  const takeover = await controlMode(canvas, tileA, "takeover");
  const stale = await uiTap(A, ["--control-holder", "agent:live", "--control-generation", String(agentGeneration ?? 3)]);
  const takeoverHandBack = await controlMode(canvas, tileA, "release");
  const afterTakeover = await controlStatus(A);
  check("takeover_explicit", agentAcquire.code === 0 && agentGeneration === 3 && refusedTap.status === 409 &&
    refusedTap.body?.error_code === CONTROL_HELD && stillAgent?.holder?.id === "agent:live" &&
    stillAgent?.generation === 3 && takeover.status === 200 && takeover.body?.lease?.generation === 4 &&
    takeover.body?.lease?.mine === true && takeover.body?.lease?.took_over_from?.id === "agent:live" &&
    refusedWith(stale, CONTROL_GENERATION_STALE) && takeoverHandBack.status === 200 &&
    afterTakeover.lease?.state === "free", {
    agent_generation: agentGeneration, human_tap: { http: refusedTap.status, error_code: refusedTap.body?.error_code ?? null },
    lease_after_refused_tap: leaseView(stillAgent ? { ...stillAgent, state: leaseState(stillAgent, Date.now()) } : null),
    takeover: { http: takeover.status, generation: takeover.body?.lease?.generation ?? null,
      took_over_from: takeover.body?.lease?.took_over_from ?? null },
    stale_claim: { code: stale.code, error_code: stale.error?.error_code ?? null },
    hand_back: { http: takeoverHandBack.status }, lease: leaseView(afterTakeover.lease),
  });
  if (takeover.status === 200) state.agentLeases = state.agentLeases.filter((item) => item.holder !== "agent:live");

  // 10. A dropped human WebSocket client: its lease ends within 45 s.
  const socket = await humanSocket(canvas, tileA);
  const wsReady = await socket.ready();
  if (wsReady) await humanTouch(socket);
  await sleep(2000);
  const wsLease = leaseFile(A);
  const wsErrors = socket.errors();
  socket.close();
  const closedAt = Date.now();
  const ended = await waitFor(async () => {
    const status = await controlStatus(A);
    return ["expired", "free"].includes(status.lease?.state) ? status : null;
  }, DROPPED_LIMIT_MS, 1000);
  check("dropped_client_expired", socket.opened && wsReady && isCanvasHolder(wsLease) && wsLease?.generation === 5 &&
    wsErrors.length === 0 && Boolean(ended), {
    opened: socket.opened, stream_ready: wsReady, acquired_generation: wsLease?.generation ?? null, socket_errors: wsErrors,
    lease: leaseView(ended?.lease ?? null), ended_after_s: ended ? Math.round((Date.now() - closedAt) / 1000) : null,
    transport: live?.transport ?? null,
  });

  // 11. An agent lease on emulator-5586: claimed admitted, unclaimed refused, released.
  const acquireB = await autonom([...targetFlags(B, tools), "control", "acquire", "--holder", "agent:b"]);
  if (acquireB.code === 0) state.agentLeases.push({ device: B, holder: "agent:b" });
  const generationB = acquireB.json?.generation ?? null;
  const claimed = await uiTap(B, ["--control-holder", "agent:b", "--control-generation", String(generationB ?? 1)]);
  const unclaimed = await uiTap(B);
  const releaseB = await autonom([...targetFlags(B, tools), "control", "release", "--holder", "agent:b"]);
  if (releaseB.code === 0) state.agentLeases = state.agentLeases.filter((item) => item.holder !== "agent:b");
  check("agent_lease_5586", acquireB.code === 0 && Number.isInteger(generationB) && claimed.code === 0 &&
    claimed.json?.control?.generation === generationB && refusedWith(unclaimed, CONTROL_HELD) &&
    releaseB.code === 0 && releaseB.json?.released === true, {
    generation: generationB, claimed: { code: claimed.code, control: claimed.json?.control ?? null },
    unclaimed: { code: unclaimed.code, error_code: unclaimed.error?.error_code ?? null },
    released: releaseB.json?.released ?? null,
  });

  // 12. Evidence: each session names only its own device; unique detail names; no evidence warning.
  const evidence = {};
  let evidenceOk = true;
  for (const [key, id, device] of [["a", idA, A], ["b", idB, B]]) {
    const journal = await journalOf(id);
    const details = await detailsOf(id);
    const named = evidenceNamesOnly([...journal.map((entry) => [`journal#${entry.seq}`, JSON.stringify(entry)]),
      ...details.map((detail) => [`actions/${detail.name}`, detail.text])], device.target);
    const unique = detailNamesUnique(details.map((detail) => detail.name));
    const warned = [...journal.map((entry) => JSON.stringify(entry)), ...details.map((detail) => detail.text)]
      .some((text) => text.includes("evidence_write_failed"));
    evidence[key] = { session_id: id, journal_entries: journal.length, details: details.length,
      foreign: named.foreign, unique_detail_names: unique, evidence_write_failed: warned };
    evidenceOk = evidenceOk && named.ok && unique && !warned && journal.length > 0 && details.length > 0;
  }
  check("evidence_per_target", evidenceOk && state.evidenceWarnings === 0,
    { ...evidence, evidence_write_failed_in_answers: state.evidenceWarnings });
}

async function iosPhase() {
  const S = DEVICES.ios;
  const tile = devicePath(deviceId(S));
  await ensureSimulator(S);
  const id = await startSession(S, "live-ios");
  const canvas = await startCanvas();
  const attached = await attach(canvas, S);
  note("Canvas attaches the Simulator reusing its session", attached.code === 0 && attached.session?.id === id &&
    attached.session?.reused === true, { session: attached.session, error_code: attached.error_code });
  const live = await waitLive(canvas, deviceId(S));
  if (!live) throw new Error("the Simulator did not turn live in the Canvas");
  const status = (await canvas.request("GET", `${tile}/status`)).body ?? {};

  // A human touch over /ws/control (idb) auto-acquires and is admitted.
  const before = (await journalOf(id)).filter((entry) => entry.origin === "human").length;
  const socket = await humanSocket(canvas, tile);
  const touchReady = await socket.ready();
  if (touchReady) await humanTouch(socket);
  await sleep(2500);
  const lease = leaseFile(S);
  const after = (await journalOf(id)).filter((entry) => entry.origin === "human").length;
  const touchErrors = socket.errors();
  check("auto_acquire_idb", status.transport === "idb" && socket.opened && touchReady && isCanvasHolder(lease) &&
    touchErrors.length === 0 && after > before, {
    transport: status.transport ?? null, stream_ready: touchReady, generation: lease?.generation ?? null,
    socket_errors: touchErrors,
    human_entries: [before, after],
  });

  const timing = sinceHuman();
  const refused = await uiTap(S);
  check("cli_refused", refusedWith(refused, CONTROL_HELD) && timing.ok,
    { code: refused.code, error_code: refused.error?.error_code ?? null, ...timing });
  const handBack = await controlMode(canvas, tile, "release");
  socket.close();
  const free = await controlStatus(S);
  check("hand_back", handBack.status === 200 && free.lease?.state === "free",
    { http: handBack.status, lease: leaseView(free.lease) });
  const admitted = await uiTap(S);
  check("cli_admitted", admitted.code === 0, { code: admitted.code, error_code: admitted.error?.error_code ?? null });
}

async function legacyPhase() {
  const A = DEVICES.a;
  await ensureEmulator(A);
  await startSession(A, "live-legacy");
  const canvas = await startCanvas(targetFlags(A, tools));
  const status = await canvas.request("GET", "/status");
  const missing = missingStatusKeys(status.body);
  check("status_keys_superset", status.status === 200 && missing.length === 0 &&
    Object.hasOwn(status.body, "lease") && Object.hasOwn(status.body, "session_stopped"),
  { http: status.status, missing, lease_state: status.body?.lease?.state ?? null });

  const tap = await humanTap(canvas, "");
  await sleep(500);
  const lease = leaseFile(A);
  check("page_tap_lease", tap.status === 200 && isCanvasHolder(lease) && lease?.generation === 1,
    { http: tap.status, generation: lease?.generation ?? null, holder_kind: lease?.holder?.kind ?? null });
  const timing = sinceHuman();
  const refused = await uiTap(A);
  check("cli_refused", refusedWith(refused, CONTROL_HELD) && timing.ok,
    { code: refused.code, error_code: refused.error?.error_code ?? null, ...timing });
  const handBack = await controlMode(canvas, "", "release");
  const free = await controlStatus(A);
  check("hand_back", handBack.status === 200 && free.lease?.state === "free",
    { http: handBack.status, lease: leaseView(free.lease) });
  const admitted = await uiTap(A);
  check("cli_admitted", admitted.code === 0, { code: admitted.code, error_code: admitted.error?.error_code ?? null });
}

// --- run ------------------------------------------------------------------------------------

/** SIGTERM, then wait (bounded) for the exit of every child still tracked. */
async function endChildren(record) {
  for (const child of [...state.children]) {
    await record(`child ${child.pid} ended`, async () => {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      await Promise.race([new Promise((done) => child.once("exit", done)), rawSleep(10_000)]);
      return { ok: child.exitCode !== null || child.signalCode !== null };
    });
  }
}

/** Shut down an emulator this run started, or whose boot it started and cut short. */
async function shutDownEmulator(entry) {
  const { device } = entry;
  if (!entry.confirmed) {
    // A boot cut short may have left an emulator starting in its own session: wait (bounded)
    // for its console, and shut it down only when it names the allowed AVD.
    let avd = null;
    const deadline = Date.now() + (entry.interrupted ? 60_000 : 5000);
    for (;;) {
      avd = await runningAvd(device, { cleanup: true });
      if (avd !== null || Date.now() >= deadline) break;
      await rawSleep(2000);
    }
    if (avd === null) return { ok: !entry.interrupted, skipped: "no emulator answered on the port", interrupted: entry.interrupted };
    if (avd !== device.avd) return { ok: true, skipped: `the port runs ${avd}, not this run's boot` };
  }
  const result = await autonom([...targetFlags(device, tools), "devices", "shutdown"], { timeout: 120_000, cleanup: true });
  return { ok: result.code === 0, confirmed: entry.confirmed };
}

/** Shut down the Simulator this run booted (it was Shutdown before), unless it is again. */
async function shutDownSimulator(entry) {
  const found = await simulatorEntry(entry.device, { cleanup: true });
  if (!entry.confirmed && (!found || found.state === "Shutdown")) return { ok: true, skipped: "not booted" };
  const result = await autonom([...targetFlags(entry.device, tools), "devices", "shutdown"],
    { timeout: 120_000, cleanup: true });
  return { ok: result.code === 0, confirmed: entry.confirmed };
}

async function cleanup() {
  const steps = [];
  const record = async (name, work) => {
    try {
      const result = await work();
      steps.push({ name, ok: typeof result === "object" && result !== null ? Boolean(result.ok) : Boolean(result),
        ...(typeof result === "object" && result !== null ? result : {}) });
    } catch (error) {
      steps.push({ name, ok: false, error: errorText(error, 200) });
    }
  };
  // A flow still running after a failed step is ended first, so nothing drives a device during
  // cleanup; the stop switch is already on, so the phase starts nothing new after this.
  await endChildren(record);
  for (const ws of [...state.sockets]) {
    state.sockets.delete(ws);
    try { ws.close(); } catch { /* closed */ }
  }
  for (const child of [...state.startingCanvases]) {
    await record(`starting Canvas ${child.pid} ended`, async () => {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      await Promise.race([new Promise((done) => child.once("exit", done)), rawSleep(10_000)]);
      state.startingCanvases.delete(child);
      return { ok: child.exitCode !== null || child.signalCode !== null };
    });
  }
  for (const lease of [...state.agentLeases]) {
    await record(`release ${lease.holder}`, async () => {
      const released = await autonom([...targetFlags(lease.device, tools), "control", "release", "--holder", lease.holder],
        { cleanup: true });
      return { ok: released.code === 0 || released.error?.error_code === "control_not_held" };
    });
  }
  for (const canvas of [...state.canvases]) await record(`Canvas ${canvas.port} stopped`, () => canvas.stop());
  for (const id of [...state.sessions].reverse()) {
    await record(`session ${id} stopped`, async () => {
      const stopped = await autonom(["--session-id", id, "session", "stop"], { timeout: 120_000, cleanup: true });
      return { ok: stopped.code === 0 || stopped.error?.error_code === "session_stopped" };
    });
  }
  for (const entry of state.emulatorsBooted) {
    await record(`${entry.device.name} shut down`, () => shutDownEmulator(entry));
  }
  for (const entry of state.simulatorsBooted) {
    await record(`${entry.device.name} shut down`, () => shutDownSimulator(entry));
  }
  // Nothing of this run may outlive it: a child started by cleanup itself and still running ends here.
  await endChildren(record);
  if (!options.keepHome) {
    await record("temporary AUTONOM_HOME removed", async () => {
      await rm(autonomHome, { recursive: true, force: true });
      return { ok: !existsSync(autonomHome) };
    });
  } else {
    steps.push({ name: "temporary AUTONOM_HOME kept", ok: true, path: autonomHome });
  }
  return steps;
}

const PHASE_SETTLE_MS = 30_000;
let finishing = null;
let phaseDone = Promise.resolve();

/**
 * Cleanup, the final report and the exit code; runs once (normal end or a signal). The stop
 * switch goes on first, the running children are ended, and the phase is given time to
 * settle (each of its next steps now fails with StoppedError) before cleanup takes stock, so
 * no work of the phase starts behind cleanup's back.
 */
function finish(reason = null) {
  finishing ??= (async () => {
    stopper.stop();
    if (reason) {
      note("run stopped", false, { reason });
      for (const child of [...state.children]) {
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
      }
      const settled = await Promise.race([phaseDone.then(() => true), rawSleep(PHASE_SETTLE_MS).then(() => false)]);
      if (!settled) note("phase still busy after the stop", false, { waited_s: PHASE_SETTLE_MS / 1000 });
    }
    report.cleanup = await cleanup();
    fillMissing(options.phase, report.checks);
    report.ok = reason === null && phaseOk(options.phase, report.checks);
    report.finished_at = new Date().toISOString();
    try {
      const written = await writeReport();
      await stat(written);
      console.error(`evidence: ${written}`);
    } catch (error) {
      console.error(`evidence could not be written: ${errorText(error, 200)}`);
    }
    process.exit(report.ok ? 0 : 1);
  })();
  return finishing;
}

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => { finish(signal); });

const work = { base: basePhase, ios: iosPhase, legacy: legacyPhase }[options.phase];
phaseDone = (async () => {
  try {
    await work();
  } catch (error) {
    note(error instanceof StoppedError ? "phase stopped" : "phase error", false, { error: errorText(error, 400) });
  }
})();
await phaseDone;
await finish();
