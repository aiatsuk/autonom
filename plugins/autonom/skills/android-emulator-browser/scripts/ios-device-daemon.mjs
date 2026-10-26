#!/usr/bin/env node
/**
 * The iOS device daemon: one Node process per simulator UDID and Autonom state base that
 * owns the connection to the simulator's one `idb_companion` and serves the CLI and the
 * Canvas over a private Unix socket.
 *
 *   node ios-device-daemon.mjs --udid <UDID> --state-base <dir> [--idle-ms <ms>]
 *
 * - Registry: `<state base>/iosd/<UDID>.json` (schema `autonom-iosd/v1`) names the daemon
 *   pid, its socket, the companion it uses, the Xcode/CoreSimulator/companion versions and
 *   its start time. `<UDID>.daemon.lock` (O_EXCL, holds the pid) keeps a second daemon
 *   for the same UDID from starting; `<UDID>.lock` is the clients' start lock (the CLI and
 *   ensureDaemon() take it before they start a daemon).
 * - Socket: `$TMPDIR/autonom-iosd-<12 hex>/d.sock` in a 0700 directory, at most 103 bytes
 *   (macOS sun_path); a longer one refuses the start with `daemon_start_failed`.
 * - Companion: the one idb's state file (`/tmp/idb/state`, or AUTONOM_IDB_STATE_FILE)
 *   lists as live for the UDID is used as it is (not owned); otherwise the daemon runs
 *   `idb connect <UDID>` under the companion environment allowlist, so every `idb` process
 *   reuses the same companion, and marks it owned. Only an owned companion is stopped:
 *   `idb disconnect <UDID>`, SIGTERM, SIGKILL after 3 s. A lost companion is replaced
 *   through `idb connect` with the Canvas's backoff (0.5 s doubling to 5 s, reset after
 *   10 s of stable running); an input that was in flight is never sent again.
 * - One gRPC session and one `accessibility_quiescence` stream stay open. The `hid` stream
 *   is opened at start and primed with one harmless key-up before the first input, but
 *   its call is not kept open: the companion serves one `hid` call at a time, so an open
 *   call would block `idb ui text`, idb's HID fallbacks and the Canvas. The call ends
 *   HID_IDLE_CLOSE_MS after priming, and right after each input, whose reply waits for
 *   the companion's status on that call: only then were its events read and performed.
 *   While another client holds the companion's call the input waits (later inputs queue
 *   behind it, in order), but never past its deadline: DEFAULT_HID_WAIT_MS
 *   (AUTONOM_IOSD_HID_WAIT_MS for tests) plus the input's own duration, counted from the
 *   moment the request arrived. That is shorter than the CLI's shortest wait for an
 *   input's reply (DAEMON_INPUT_BUDGET_S in ios_idb.py, minus INPUT_REPLY_SLACK_MS), so
 *   the CLI always gets the answer and nothing is performed after it gave up. At the
 *   deadline an input not yet begun is refused (`before_send`, nothing sent); one whose
 *   events were sent but not confirmed has its calls cancelled (never performed later)
 *   and fails with `input_unconfirmed`, stage `after_send`, as does one whose events were
 *   lost with the session. The next input opens a new call on the same session (no new
 *   priming; priming runs again only on a new companion). An `act` replies within its
 *   `timeout_ms` plus ACT_REPLY_MARGIN_MS (inside the CLI's ACT_MARGIN_S).
 * - Warm-up: a new quiet stream reports no touch until it sent its first event, so the
 *   daemon serves a new companion only once that event came and the priming call was
 *   confirmed (QUIET_WARM_MS at most), a touch that finds the stream reopened waits for
 *   it the same way, and the first touch on each stream, and the first after each
 *   target change the stream reports (an app launched or relaunched), gets
 *   FIRST_TOUCH_CONFIRM_MS. A quiet stream silent too long after a BUSY report
 *   (QUIET_STUCK_MS) is only suspected: STUCK_SILENT_PROBES fresh probe streams in a
 *   row that stay silent too (QUIET_STUCK_PROBE_MS each) prove the companion stuck, and
 *   one the daemon owns is then replaced, while idle or before the next touch.
 * - Protocol: one JSON object per line `{id, op, ...}`, one reply line `{id, ok, ...}`;
 *   failures carry `error: {code, message, stage}` with stage `before_send` or
 *   `after_send`. Inputs run one at a time in arrival order; reads run beside them.
 *   `attach` and `detach` of one connection are applied in the order they arrive.
 * - Stop: every request already read is answered; a request that arrives while the
 *   daemon stops gets `daemon_unreachable` with stage `before_send`. Before it closes a
 *   connection the daemon writes one closing line `{id: null, ok: false, closing: true,
 *   error}`; it runs no request read after that line, so a client may treat every
 *   request still waiting then as `before_send`.
 * - Idle stop after 5 minutes without a request while no Canvas is attached
 *   (AUTONOM_IOSD_IDLE_MS shortens it for tests, minimum 1000 ms).
 * - A companion older than 1.6.4, or one without `accessibility_quiescence`, puts the
 *   daemon in state `unsupported`: every request then fails with `companion_too_old`.
 *
 * The log (`<state base>/iosd/<UDID>.log`) records ops, timings and lifecycle events,
 * never tree contents or typed text. The daemon never boots, reboots, erases or shuts
 * down a simulator. Node built-ins only.
 */
import { execFile as nodeExecFile, spawn as nodeSpawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  appendFileSync, chmodSync, closeSync, existsSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync,
  renameSync, rmSync, statSync, truncateSync, unlinkSync, writeFileSync, writeSync,
} from "node:fs";
import { connect as netConnect, createServer as netCreateServer } from "node:net";
import { cpus, homedir, loadavg, tmpdir } from "node:os";
import { basename, delimiter, dirname, join, resolve as resolvePath } from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

import { IDB_RETRY_MAX_MS, IDB_RETRY_MIN_MS, IDB_STABLE_MS } from "./canvas-runtime.mjs";
import {
  GrpcError, HID_BUTTON, HidUnconfirmedError, IdbCompanionClient, STREAM_STOP_TIMEOUT_MS, companionEnvironment,
} from "./ios-idb-companion.mjs";

export const IOSD_SCHEMA = "autonom-iosd/v1";
export const DAEMON_SCRIPT = fileURLToPath(import.meta.url);

// ---------------------------------------------------------------------------
// Constants (each one is pinned by a unit test).
// ---------------------------------------------------------------------------

/** DEC-002: five idle minutes without a request and without an attached Canvas. */
export const DEFAULT_IDLE_MS = 300_000;
/** The shortest idle limit AUTONOM_IOSD_IDLE_MS or --idle-ms may set (tests). */
export const MIN_IDLE_MS = 1000;
export const IDLE_ENV = "AUTONOM_IOSD_IDLE_MS";
/**
 * The deadline of one input, from the moment its request arrived (queue, companion
 * restart and the wait for another client's hid call all count), plus the input's own
 * duration: DEFAULT_HID_WAIT_MS, set by AUTONOM_IOSD_HID_WAIT_MS for tests, never below
 * MIN_HID_WAIT_MS. It pairs with the CLI's DAEMON_INPUT_BUDGET_S (ios_idb.py, 20 s): the
 * daemon's reply, at most INPUT_REPLY_SLACK_MS after the deadline (the cancel's own stop
 * timeout), reaches the CLI before the CLI stops waiting; a test pins
 * DEFAULT_HID_WAIT_MS + INPUT_REPLY_SLACK_MS < DAEMON_INPUT_BUDGET_S.
 */
export const DEFAULT_HID_WAIT_MS = 15_000;
export const HID_WAIT_ENV = "AUTONOM_IOSD_HID_WAIT_MS";
export const MIN_HID_WAIT_MS = 100;
/** How long after its deadline an input's reply may still take: the cancel's stop timeout and a margin. */
export const INPUT_REPLY_SLACK_MS = STREAM_STOP_TIMEOUT_MS + 1000;
/**
 * An `act` replies by its timeout_ms (and the input's duration) plus this, from arrival:
 * the input's deadline, the confirmation and grace windows, the cause check and the read
 * fit in it, and what is not done by then is cut. It pairs with the CLI's ACT_MARGIN_S
 * (25 s): ACT_REPLY_MARGIN_MS + INPUT_REPLY_SLACK_MS < ACT_MARGIN_S.
 */
export const ACT_REPLY_MARGIN_MS = 20_000;
/** sun_path is 104 bytes on macOS, including the terminating NUL. */
export const MAX_SOCKET_PATH_BYTES = 103;
/** One request or reply line; a 3000-node tree is about 1.5 MB. */
export const MAX_LINE_BYTES = 4 * 1024 * 1024;
/** The first companion with `accessibility_quiescence` and the read backend field. */
export const MIN_COMPANION_VERSION = Object.freeze([1, 6, 4]);
/** ID-10 settle values: wait for touches_completed, then a grace period unless busy. */
export const CONFIRM_MS = 500;
export const GRACE_MS = 300;
/**
 * The confirmation window of the first touch on a quiet stream (a new companion, or a
 * stream opened again) and of the first touch after the stream's target changed (an app
 * launched, relaunched or brought to the front). Measured on a new companion, the app
 * reported that touch up to about 0.65 s after the companion's status; measured after a
 * `simctl terminate` + `launch` of Settings on a stream that stayed open, the first touch
 * in the new process was reported 0.36-0.67 s after its write at load 4 (later ones in
 * 66-108 ms), so CONFIRM_MS expired just before it came, and sooner under load. Only an
 * unreported touch waits this long; a reported one is confirmed the moment its
 * touches_completed arrives. Used when the request does not set confirm_ms.
 */
export const FIRST_TOUCH_CONFIRM_MS = 1500;
/** The quiet window the open quiet stream is asked for. */
export const QUIET_WINDOW_MS = 250;
/** Run 1's default --observe-timeout-ms: the whole settle stays inside it. */
export const OBSERVE_TIMEOUT_MS = 3000;
/** Requests that arrive while the companion is being replaced wait this long. */
export const RESTART_WAIT_MS = 6000;
/** After `idb disconnect`, SIGTERM; SIGKILL when the companion outlives this. */
export const COMPANION_STOP_MS = 3000;
/** How often the companion pid is checked. */
export const COMPANION_POLL_MS = 250;
/** How long the start waits for the quiet stream to answer before it trusts it. */
export const QUIET_PROBE_MS = 2000;
/**
 * A newly opened quiet stream reports touches only once it has delivered its first event
 * (on a new companion about 0.4 s after the call opened; a touch made before that comes
 * late or never). The start, and a touch that finds the stream just (re)opened, wait for
 * that first event this long at most; past it they go on, and an unreported touch stays
 * unconfirmed.
 */
export const QUIET_WARM_MS = 2000;
/**
 * A stuck quiet stream: idb_companion 1.6.4 sometimes ends its reports with one BUSY event
 * whose pending signals include RUN_LOOP_IDLE and then sends nothing but target changes:
 * no touches_completed, no SETTLING or QUIET, on that stream, on a new stream and on a new
 * gRPC session alike, until the companion is replaced (a new companion reported the same
 * app's touches at once). Measured: on a freshly booted Simulator at load 50 it happened
 * on the Home screen in 3 of 7 boots, and every touch in Settings launched later went
 * unconfirmed; on a running one, after about six Settings relaunches, and in long runs of
 * Settings push/Back taps about once a minute. But a healthy companion idling on the Home
 * screen also sends BUSY with RUN_LOOP_IDLE pending and then nothing for minutes, and
 * reports the next touch at once (REV-055). So silence for QUIET_STUCK_MS after such a
 * report is only a suspicion. Only a state report (SETTLING, QUIET, BUSY) or a
 * touches_completed ends it: the target changes a stuck companion still sends (an app
 * launched, relaunched or brought to the front) do not, since a new app that reports
 * normally sends a state report soon after; nor does another BUSY with RUN_LOOP_IDLE
 * pending (a stuck companion sends one for each app launched on it). A suspicion is
 * checked with a probe: a new quiet stream on the same session, which on a healthy
 * companion sends a state report other than that BUSY within QUIET_STUCK_PROBE_MS and on
 * a stuck one sends none (or only that BUSY). A probe that gets one (or the open stream
 * reporting during it) keeps the companion (logged `quiet_probe` ok). A healthy
 * companion's probe stream too stays silent for the whole QUIET_STUCK_PROBE_MS at times
 * (REV-056: 4 of 83 live probes, more under load), so one silent probe is not the proof:
 * STUCK_SILENT_PROBES silent probes in a row, each on its own new stream and each begun
 * within STUCK_PROBE_FRESH_MS of the last one's answer, are, and a companion the daemon
 * owns is then replaced (at most once per STUCK_RESTART_INTERVAL_MS, a gap that doubles
 * from the third replacement in a row with no sign of health, see STUCK_BACKOFF_MAX_FACTOR;
 * never while the host is overloaded, see HOST_LOAD_FACTOR). The daemon probes
 * while no request runs (a first probe at most once per STUCK_RESTART_INTERVAL_MS, again
 * after it while the silence lasts; the next probe of a silent one at once) and before a
 * touch that finds the stream suspected, inside the touch's deadline, so the touch is
 * reported. A companion it does not own is left as it is (logged `quiet_stuck`); its
 * touches stay unconfirmed, never confirmed.
 */
export const QUIET_STUCK_MS = 3000;
export const QUIET_STUCK_SIGNAL = "RUN_LOOP_IDLE";
/**
 * How long a stuck-suspicion probe waits for the new stream's first state report other
 * than a BUSY with QUIET_STUCK_SIGNAL pending (or a touches_completed). Measured on
 * idb_companion 1.6.4 (iOS 27.0 Simulator): a healthy companion's new stream sent SETTLING
 * first, 0.37-0.62 s after the call opened at load 9-18 (up to 1.47 s at load 29 in an
 * earlier round); a stuck one, at load 40-50, sent nothing in 2 s, or one BUSY with
 * RUN_LOOP_IDLE pending after 0.88-1.43 s and then nothing (its touches went
 * unconfirmed). Every probe gets the whole QUIET_STUCK_PROBE_MS: a shorter one would be a
 * weaker proof.
 */
export const QUIET_STUCK_PROBE_MS = 2000;
/**
 * Silent probes in a row that prove a companion stuck (REV-056). A healthy companion's
 * probe stream stayed silent in 4 of 83 live probes (about 5 %), so one silent probe
 * would replace a healthy companion idling for minutes (re-probed every
 * STUCK_RESTART_INTERVAL_MS) within 5 minutes about 40 % of the time; two in a row make
 * that about 0.25 % per check. Before a touch the probes still needed are run only when
 * they (QUIET_STUCK_PROBE_MS each) and a replacement (RESTART_WAIT_MS) all fit in what
 * the input's deadline leaves after its duration and ARM_WAIT_SPARE_MS; otherwise the
 * touch is sent on the current companion and reported as it is (unconfirmed when the
 * companion is stuck), and the idle check carries on after it. With the default deadline
 * (15 s) the whole check fits: 2 x 2 s + 6 s + 1 s.
 */
export const STUCK_SILENT_PROBES = 2;
/** A probe's answer stands this long for the next check of the same suspicion (idle or touch). */
export const STUCK_PROBE_FRESH_MS = 3000;
export const STUCK_RESTART_INTERVAL_MS = 30_000;
/** Tests only: a shorter STUCK_RESTART_INTERVAL_MS, never below MIN_STUCK_INTERVAL_MS. */
export const STUCK_INTERVAL_ENV = "AUTONOM_IOSD_STUCK_INTERVAL_MS";
export const MIN_STUCK_INTERVAL_MS = 1000;
/**
 * Backoff of stuck replacements (r523 live run: seven in a row, one every ~34 s, while the
 * host's 1-minute load was 200-500 on 10 CPUs; each new companion's first report was the
 * same BUSY with RUN_LOOP_IDLE pending). A replacement that follows the last one with no
 * sign in between that the new companion was healthy (a report other than that BUSY on its
 * stream once it serves, a confirmed touch, or a probe that answered) is counted in a row:
 * the first and the second replacement need the interval, each one after that twice the
 * gap the last one needed: 30 s, 30 s, 60 s, 120 s, ... up to STUCK_BACKOFF_MAX_FACTOR
 * times the interval (10 minutes by default). Any sign of health resets it to the interval;
 * a target change and that BUSY itself are no sign of health.
 */
export const STUCK_BACKOFF_MAX_FACTOR = 20;
/**
 * Host-load guard: while the host's 1-minute load average is above HOST_LOAD_FACTOR per
 * CPU, a suspected stuck companion is neither probed nor replaced (logged `quiet_suspect`,
 * reason `host_load`), and the check comes back after HOST_LOAD_RECHECK_MS (or the
 * interval, if shorter). Under that load a healthy companion answers probes too slowly to
 * tell from a stuck one, and each replacement starts a new idb_companion on a host
 * that is already short of time: in the r523 live run every replacement in the loop came at
 * a 1-minute load of 20-50 per CPU (205-510 on 10 CPUs). The stuck companions a
 * replacement did fix were seen at 1-minute loads of 17.6 and 27.6 on 10 CPUs (about 2-3
 * per CPU), and a healthy companion's probe stream sent its first event after 1.47 s,
 * against the 2 s probe limit, at 29. No live run covered 60-100 on 10 CPUs, and false
 * silent probes likely rise well before 100, so the limit is 6 per CPU (60 on 10 CPUs, the
 * live script's --max-load). Touches still go to the current companion, once, and are
 * reported as they are.
 */
export const HOST_LOAD_FACTOR = 6;
export const HOST_LOAD_RECHECK_MS = 10_000;
/** Tests only: a file whose first number is used as the host's 1-minute load (read on each check). */
export const HOST_LOAD_FILE_ENV = "AUTONOM_IOSD_LOAD_FILE";
/** Of an input's deadline, what the wait for a cold quiet stream always leaves for the input. */
export const ARM_WAIT_SPARE_MS = 1000;
/** One retry of an "unlit display" read, after this pause (ID-13). */
export const UNLIT_RETRY_MS = 300;
/** ID-09: an `ax` read covering less than this share of the rows below the status bar is incomplete. */
export const COVERAGE_THRESHOLD = 0.5;
/** Points at the top of the screen left out of the coverage (status bar, Dynamic Island). */
export const STATUS_BAR_PT = 60;
/** An element at least this share of the screen area is a container, not content. */
export const CONTAINER_AREA_SHARE = 0.9;
/** Frontmost apps whose content is web content: read with `axbridge`. */
export const WEB_CONTENT_BUNDLES = Object.freeze(["com.apple.mobilesafari"]);
/** The same apps by the label of their Application element (the legacy read has no bundle id). */
export const WEB_CONTENT_APP_NAMES = Object.freeze(["Safari"]);
/** Element types and roles that mark a web view inside an app. */
export const WEB_VIEW_TYPES = Object.freeze(["WebView", "WebArea", "WebKitWebView"]);
export const WEB_VIEW_ROLES = Object.freeze(["AXWebArea"]);
/** ID-11: one key-up of Left Shift (HID usage 225) warms the input path; it types nothing. */
export const PRIMING_KEYCODE = 225;
/** ID-12: the CoreDevice flag that, set with no Device Hub running, swallows HID input. */
export const DTUHIDD_FLAG = "com.apple.coredevice.dtuhidd.active";
export const DEVICE_HUB_PROCESS = /(^|\/)Device ?Hub$/i;
/** The display a read hit is not lit yet (foldable or a display change in progress). */
export const UNLIT_MESSAGE = /display that is not lit yet/i;
export const NO_TRANSLATION_MESSAGE = /no translation object/i;
/** Wait for a client's start lock, and for a starting daemon's registry. */
export const LOCK_TIMEOUT_MS = 20_000;
export const START_TIMEOUT_MS = 40_000;
const IDB_CONNECT_TIMEOUT_MS = 30_000;
const IDB_DISCONNECT_TIMEOUT_MS = 10_000;
const TOOL_TIMEOUT_MS = 5000;
const LOG_MAX_BYTES = 1024 * 1024;
const TIMINGS_KEPT = 20;
const UDID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;
const IDB_STATE_FILE = "/tmp/idb/state";
const CORESIMULATOR_PLIST = "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/Info.plist";

export const INPUT_OPS = Object.freeze(["tap", "swipe", "button", "key"]);
export const OPS = Object.freeze([
  "tree", "tap", "swipe", "button", "key", "quiet", "act", "companion", "attach", "detach", "status", "stop",
]);
export const STAGE = Object.freeze({ BEFORE_SEND: "before_send", AFTER_SEND: "after_send" });
export const CODE = Object.freeze({
  INVALID_REQUEST: "invalid_request",
  COMPANION_TOO_OLD: "companion_too_old",
  DISPLAY_NOT_LIT: "ios_display_not_lit",
  DAEMON_UNREACHABLE: "daemon_unreachable",
  DAEMON_START_FAILED: "daemon_start_failed",
  BACKEND_FAILED: "backend_failed",
  INPUT_UNCONFIRMED: "input_unconfirmed",
  INPUT_BLOCKED: "ios_input_blocked",
});
/** The hint of an input whose HID call idb_companion never confirmed (an error, not a warning). */
export const HID_UNCONFIRMED_HINT =
  "The input was sent once, but idb_companion did not confirm it: another client held the simulator's input "
  + "(idb ui text, a Canvas press) past the safety limit, or the companion connection was lost. "
  + "Check the screen before you repeat it.";
export const HINT = Object.freeze({
  [CODE.DISPLAY_NOT_LIT]:
    "The simulator's display is not lit yet: a foldable device or a display change is in progress. "
    + "Wait for the display to settle, or use a device type with one fixed display.",
  [CODE.INPUT_UNCONFIRMED]:
    "The input was sent once, but the app did not report the touch within the confirmation window. "
    + "Check the screen before you repeat it; a tap on empty space may also go unconfirmed.",
  [CODE.INPUT_BLOCKED]:
    `Input is not reaching the simulator: ${DTUHIDD_FLAG} is set and no Device Hub is running. `
    + "Reboot the simulator (xcrun simctl shutdown <UDID> && xcrun simctl boot <UDID>), then retry.",
  [CODE.COMPANION_TOO_OLD]:
    "Upgrade idb_companion to 1.6.4 or newer: brew update && brew upgrade idb-companion (and pipx upgrade fb-idb).",
});

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

/** A reply-shaped error that knows whether the device may have seen the request. */
export class DaemonError extends Error {
  constructor(code, message, { stage = STAGE.BEFORE_SEND, hint = HINT[code] ?? null, ...extra } = {}) {
    super(message);
    this.name = "DaemonError";
    this.code = code;
    this.stage = stage;
    this.hint = hint;
    this.extra = extra;
  }

  toJSON() {
    return { code: this.code, message: this.message, stage: this.stage, ...(this.hint ? { hint: this.hint } : {}), ...this.extra };
  }
}

/** `$AUTONOM_HOME`, else `~/.autonom`: the state base the sessions use. */
export function defaultStateBase(env = process.env) {
  return env.AUTONOM_HOME ? resolvePath(env.AUTONOM_HOME) : join(homedir(), ".autonom");
}

/** Where the daemon of `udid` keeps its registry, locks and log. */
export function daemonPaths(stateBase, udid) {
  const dir = join(stateBase, "iosd");
  return {
    dir,
    registry: join(dir, `${udid}.json`),
    daemonLock: join(dir, `${udid}.daemon.lock`),
    startLock: join(dir, `${udid}.lock`),
    log: join(dir, `${udid}.log`),
  };
}

/**
 * `$TMPDIR/autonom-iosd-<first 12 hex of sha256(state base + UDID)>/d.sock`, with its
 * byte length and whether it fits sun_path.
 */
export function socketPathFor(stateBase, udid, { tmp = tmpdir() } = {}) {
  const hash = createHash("sha256").update(`${stateBase}${udid}`).digest("hex").slice(0, 12);
  const dir = join(tmp, `autonom-iosd-${hash}`);
  const path = join(dir, "d.sock");
  const bytes = Buffer.byteLength(path);
  return { dir, path, bytes, fits: bytes <= MAX_SOCKET_PATH_BYTES };
}

/** The idle limit: --idle-ms, else AUTONOM_IOSD_IDLE_MS, else 5 minutes; never below 1 s. */
export function idleLimit({ flag, env = process.env } = {}) {
  const raw = flag ?? env[IDLE_ENV];
  if (raw === undefined || raw === null || raw === "") return DEFAULT_IDLE_MS;
  const value = Number(raw);
  if (!Number.isFinite(value)) return DEFAULT_IDLE_MS;
  return Math.max(MIN_IDLE_MS, Math.round(value));
}

/** An input's deadline: AUTONOM_IOSD_HID_WAIT_MS, else DEFAULT_HID_WAIT_MS; never below 100 ms. */
export function hidWaitLimit(env = process.env) {
  const raw = env[HID_WAIT_ENV];
  if (raw === undefined || raw === null || raw === "") return DEFAULT_HID_WAIT_MS;
  const value = Number(raw);
  if (!Number.isFinite(value)) return DEFAULT_HID_WAIT_MS;
  return Math.max(MIN_HID_WAIT_MS, Math.round(value));
}

/** The least time between two stuck-companion replacements: AUTONOM_IOSD_STUCK_INTERVAL_MS, else 30 s; never below 1 s. */
export function stuckRestartInterval(env = process.env) {
  const raw = env[STUCK_INTERVAL_ENV];
  if (raw === undefined || raw === null || raw === "") return STUCK_RESTART_INTERVAL_MS;
  const value = Number(raw);
  if (!Number.isFinite(value)) return STUCK_RESTART_INTERVAL_MS;
  return Math.max(MIN_STUCK_INTERVAL_MS, Math.round(value));
}

/**
 * The least gap before the next stuck replacement after `streak` replacements in a row with
 * no sign of health in between (see STUCK_BACKOFF_MAX_FACTOR): the interval for 0 or 1,
 * doubling with each one more, at most STUCK_BACKOFF_MAX_FACTOR times the interval.
 */
export function stuckRestartGap(interval, streak) {
  const doublings = Math.max(0, Math.min(30, Math.floor(Number(streak) || 0) - 1));
  return Math.min(interval * STUCK_BACKOFF_MAX_FACTOR, interval * 2 ** doublings);
}

/**
 * The host's load against HOST_LOAD_FACTOR per CPU: { load_1m, cpus, load_limit,
 * overloaded }. The 1-minute load is os.loadavg()[0], or (tests only) the first number in
 * the file AUTONOM_IOSD_LOAD_FILE names; an unreadable file falls back to os.loadavg().
 */
export function hostLoad(env = process.env, { loadavg: readLoad = loadavg, cpuCount = () => cpus().length } = {}) {
  let load = null;
  const file = env[HOST_LOAD_FILE_ENV];
  if (file) {
    try {
      const value = Number.parseFloat(readFileSync(file, "utf8"));
      if (Number.isFinite(value)) load = value;
    } catch {
      // Unreadable: the real load.
    }
  }
  if (load === null) load = Number(readLoad()?.[0]) || 0;
  const count = Math.max(1, Number(cpuCount()) || 1);
  const limit = HOST_LOAD_FACTOR * count;
  return { load_1m: Math.round(load * 100) / 100, cpus: count, load_limit: limit, overloaded: load > limit };
}

/** How long a validated input itself takes (a long press, a swipe), in ms. */
export function inputDurationMs(input) {
  if ((input?.op === "tap" || input?.op === "swipe") && Number.isFinite(input.duration) && input.duration > 0) {
    return Math.round(input.duration * 1000);
  }
  return 0;
}

/** `[major, minor, patch]` of the first x.y[.z] in `text`, or null. */
export function parseVersion(text) {
  const match = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(String(text ?? ""));
  return match ? [Number(match[1]), Number(match[2]), Number(match[3] ?? 0)] : null;
}

/** True when `version` (a string or triple) is at least `minimum`; null when unknown. */
export function versionAtLeast(version, minimum = MIN_COMPANION_VERSION) {
  const parsed = Array.isArray(version) ? version : parseVersion(version);
  if (!parsed) return null;
  for (let i = 0; i < 3; i += 1) {
    if ((parsed[i] ?? 0) !== minimum[i]) return (parsed[i] ?? 0) > minimum[i];
  }
  return true;
}

/**
 * The version `idb_companion --version` printed: its JSON `version`, else the first
 * x.y.z in its output, else the one in the binary's resolved path (Homebrew keeps
 * `Cellar/idb-companion/<version>/`; 1.6.4 prints only its build date).
 */
export function companionVersionFrom(stdout, realBinaryPath = null) {
  for (const line of String(stdout ?? "").split("\n")) {
    const text = line.trim();
    if (!text.startsWith("{")) continue;
    try {
      const info = JSON.parse(text);
      if (info && typeof info.version === "string" && parseVersion(info.version)) return info.version;
    } catch {
      // Not the JSON line.
    }
  }
  const firstLine = String(stdout ?? "").split("\n").find((line) => line.trim() && !line.trim().startsWith("{"));
  const fromText = /\b\d+\.\d+\.\d+\b/.exec(firstLine ?? "");
  if (fromText) return fromText[0];
  const fromPath = /idb-companion\/(\d+\.\d+(?:\.\d+)?)\//.exec(realBinaryPath ?? "");
  return fromPath ? fromPath[1] : null;
}

/** The value `notifyutil -g <name>` printed (`<name> <value>`), or null. */
export function parseNotifyValue(stdout, name = DTUHIDD_FLAG) {
  for (const line of String(stdout ?? "").split("\n")) {
    const parts = line.trim().split(/\s+/);
    if (parts.length >= 2 && parts[0] === name && /^-?\d+$/.test(parts.at(-1))) return Number(parts.at(-1));
    if (parts.length === 1 && /^-?\d+$/.test(parts[0])) return Number(parts[0]);
  }
  return null;
}

/** True when a `ps -axo comm=` listing has a Device Hub process. */
export function deviceHubIn(listing) {
  return String(listing ?? "").split("\n").some((line) => DEVICE_HUB_PROCESS.test(line.trim()));
}

/** The cause of an unconfirmed input from the dtuhidd flag and the Device Hub check (ID-12). */
export function inputWarning({ flag, deviceHub }) {
  const code = flag === 1 && deviceHub === false ? CODE.INPUT_BLOCKED : CODE.INPUT_UNCONFIRMED;
  return { code, hint: HINT[code], dtuhidd_active: flag, device_hub: deviceHub };
}

function frameOf(element) {
  const frame = element?.frame;
  if (!frame || typeof frame !== "object") return null;
  const values = [frame.x, frame.y, frame.width, frame.height].map(Number);
  if (values.some((value) => !Number.isFinite(value))) return null;
  const [x, y, width, height] = values;
  return width > 0 && height > 0 ? { x, y, width, height } : null;
}

/**
 * The share of the rows below the status bar that at least one content element covers,
 * from the frames of a legacy (describe-all) read: 0..1, or null when there is no screen
 * to measure. The Application and Window elements and full-screen containers are left
 * out: they cover everything whether or not their content was read.
 */
export function frameCoverage(elements, { statusBarPt = STATUS_BAR_PT } = {}) {
  if (!Array.isArray(elements)) return null;
  const app = elements.find((element) => element?.type === "Application" && frameOf(element));
  const frames = elements.map(frameOf).filter(Boolean);
  if (!frames.length) return null;
  const screen = app ? frameOf(app) : {
    x: Math.min(...frames.map((f) => f.x)), y: Math.min(...frames.map((f) => f.y)),
    width: Math.max(...frames.map((f) => f.x + f.width)) - Math.min(...frames.map((f) => f.x)),
    height: Math.max(...frames.map((f) => f.y + f.height)) - Math.min(...frames.map((f) => f.y)),
  };
  const top = screen.y + Math.min(statusBarPt, screen.height / 2);
  const bottom = screen.y + screen.height;
  if (bottom <= top) return null;
  const spans = [];
  for (const element of elements) {
    if (element?.type === "Application" || element?.type === "Window") continue;
    const frame = frameOf(element);
    if (!frame) continue;
    if (frame.width * frame.height >= CONTAINER_AREA_SHARE * screen.width * screen.height) continue;
    const start = Math.max(top, frame.y);
    const end = Math.min(bottom, frame.y + frame.height);
    if (end > start) spans.push([start, end]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  let covered = 0;
  let reach = top;
  for (const [start, end] of spans) {
    if (end <= reach) continue;
    covered += end - Math.max(start, reach);
    reach = end;
  }
  return Math.round((covered / (bottom - top)) * 1000) / 1000;
}

function bundleOf(app) {
  for (const key of ["bundle_id", "bundleId", "bundle_identifier", "AXBundleIdentifier"]) {
    if (typeof app?.[key] === "string" && app[key]) return app[key];
  }
  return null;
}

/**
 * ID-09 rule after an `ax` read: read again with `axbridge` (interactable filter) when
 * the read covers too little of the screen, the frontmost app is on the web-content
 * list, or the tree holds a web view. Returns `{ axbridge, reason, coverage }`.
 */
export function chooseReadBackend(elements) {
  const coverage = frameCoverage(elements);
  if (!Array.isArray(elements)) return { axbridge: false, reason: null, coverage };
  const app = elements.find((element) => element?.type === "Application");
  const bundle = bundleOf(app);
  if ((bundle && WEB_CONTENT_BUNDLES.includes(bundle)) || WEB_CONTENT_APP_NAMES.includes(app?.AXLabel)) {
    return { axbridge: true, reason: "web_content_app", coverage };
  }
  const webView = elements.some((element) => WEB_VIEW_TYPES.includes(element?.type) || WEB_VIEW_ROLES.includes(element?.role));
  if (webView) return { axbridge: true, reason: "web_view", coverage };
  if (coverage !== null && coverage < COVERAGE_THRESHOLD) return { axbridge: true, reason: "low_coverage", coverage };
  return { axbridge: false, reason: null, coverage };
}

function finiteNumber(value, name, { min = 0 } = {}) {
  const number = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  if (typeof number !== "number" || !Number.isFinite(number) || number < min) {
    throw new DaemonError(CODE.INVALID_REQUEST, `${name} must be a number of at least ${min}.`);
  }
  return number;
}

function optionalMs(value, name, fallback) {
  if (value === undefined || value === null) return fallback;
  return Math.round(finiteNumber(value, name));
}

/** A validated input `{ op, ... }` (tap, swipe, button or key) or an invalid_request error. */
export function validateInput(request) {
  if (!request || typeof request !== "object") throw new DaemonError(CODE.INVALID_REQUEST, "input must be an object.");
  const { op } = request;
  if (op === "tap") {
    const input = { op, x: finiteNumber(request.x, "x"), y: finiteNumber(request.y, "y") };
    if (request.duration !== undefined && request.duration !== null) input.duration = finiteNumber(request.duration, "duration");
    return input;
  }
  if (op === "swipe") {
    return {
      op,
      x1: finiteNumber(request.x1, "x1"), y1: finiteNumber(request.y1, "y1"),
      x2: finiteNumber(request.x2, "x2"), y2: finiteNumber(request.y2, "y2"),
      duration: request.duration === undefined || request.duration === null ? 0.5 : finiteNumber(request.duration, "duration"),
    };
  }
  if (op === "button") {
    const name = String(request.name ?? "").toUpperCase();
    if (!(name in HID_BUTTON)) throw new DaemonError(CODE.INVALID_REQUEST, `Unknown button ${request.name}.`);
    return { op, name };
  }
  if (op === "key") {
    const code = finiteNumber(request.code, "code");
    if (!Number.isInteger(code)) throw new DaemonError(CODE.INVALID_REQUEST, "code must be an integer key code.");
    return { op, code };
  }
  throw new DaemonError(CODE.INVALID_REQUEST, `input op must be one of ${INPUT_OPS.join(", ")}.`);
}

/** The companions idb's state file lists (fb-idb `json_data_companions`), or []. */
export function readIdbState(path) {
  try {
    const payload = JSON.parse(readFileSync(path, "utf8"));
    return Array.isArray(payload) ? payload.filter((entry) => entry && typeof entry === "object") : [];
  } catch {
    return [];
  }
}

export function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

/** The live local companion idb lists for `udid`: `{ pid, endpoint }`, or null. */
export function liveCompanionEntry(entries, udid) {
  for (const entry of entries) {
    if (entry.udid !== udid || entry.is_local !== true || !pidAlive(entry.pid)) continue;
    if (typeof entry.path === "string" && entry.path) return { pid: entry.pid, endpoint: { socketPath: entry.path } };
    if (entry.host && entry.port) return { pid: entry.pid, endpoint: { host: String(entry.host), port: Number(entry.port) } };
  }
  return null;
}

function which(name, env = process.env) {
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    const candidate = join(directory, name);
    try {
      if (statSync(candidate).isFile()) return candidate;
    } catch {
      // Not here.
    }
  }
  return null;
}

function run(file, args, { env, timeoutMs = TOOL_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    nodeExecFile(file, args, { env, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
      resolve({ code: error ? (typeof error.code === "number" ? error.code : -1) : 0, error, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** `promise` raced against `ms`: `{ value, timedOut: false }` or `{ timedOut: true }`. */
function within(promise, ms) {
  let timer;
  return Promise.race([
    promise.then((value) => ({ value, timedOut: false })),
    new Promise((resolve) => {
      timer = setTimeout(() => resolve({ timedOut: true }), Math.max(0, ms));
    }),
  ]).finally(() => clearTimeout(timer));
}

/** A sleep that `abort.stop()` ends at once (an input given up at its deadline). */
function abortableSleep(ms, abort) {
  if (abort.stopped) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      abort.wakers.delete(done);
      resolve();
    }
    abort.wakers.add(done);
  });
}

function plistValue(path, key) {
  try {
    const text = readFileSync(path, "utf8");
    const match = new RegExp(`<key>${key}</key>\\s*<string>([^<]+)</string>`).exec(text);
    return match ? match[1].trim() : null;
  } catch {
    return null;
  }
}

/** Socket connectable within `timeoutMs`. */
export function socketConnectable(path, timeoutMs = 500) {
  return new Promise((resolve) => {
    if (!path) return resolve(false);
    const socket = netConnect(path);
    const done = (value) => {
      clearTimeout(timer);
      socket.destroy();
      resolve(value);
    };
    const timer = setTimeout(() => done(false), timeoutMs);
    socket.once("connect", () => done(true));
    socket.once("error", () => done(false));
  });
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function writeJsonAtomic(path, value) {
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  renameSync(temporary, path);
}

/** Lock paths this process holds right now (several callers in one process share its pid). */
const heldLocks = new Set();

/**
 * O_EXCL lock holding our pid; a lock whose pid is gone is removed and taken. It is not
 * re-entrant: a lock another caller in this process holds is taken, while one that names
 * our pid but that this process does not hold (a reused pid) is stale.
 */
function tryLock(path) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const fd = openSync(path, "wx", 0o600);
      writeSync(fd, String(process.pid));
      closeSync(fd);
      heldLocks.add(path);
      return { taken: true, holder: process.pid };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let holder;
      let ageMs = Infinity;
      try {
        holder = Number.parseInt(readFileSync(path, "utf8").trim(), 10);
        ageMs = Date.now() - statSync(path).mtimeMs;
      } catch {
        continue; // Released while we looked: try again.
      }
      // A lock created a moment ago may not hold its pid yet: it is taken, not stale.
      if (!Number.isInteger(holder) && ageMs < 2000) return { taken: false, holder: null };
      if (holder === process.pid) {
        if (heldLocks.has(path)) return { taken: false, holder };
      } else if (pidAlive(holder)) {
        return { taken: false, holder };
      }
      try {
        unlinkSync(path);
      } catch {
        // Someone else removed it first.
      }
    }
  }
  return { taken: false, holder: null };
}

function releaseLock(path) {
  if (!heldLocks.delete(path)) return;
  try {
    if (Number.parseInt(readFileSync(path, "utf8").trim(), 10) === process.pid) unlinkSync(path);
  } catch {
    // Already gone.
  }
}

/** A registry whose daemon pid is alive and whose socket answers, or null. */
export async function liveRegistry(stateBase, udid) {
  const registry = readJson(daemonPaths(stateBase, udid).registry);
  if (!registry || registry.schema !== IOSD_SCHEMA || !pidAlive(registry.pid)) return null;
  if (!(await socketConnectable(registry.socket))) return null;
  return registry;
}

// ---------------------------------------------------------------------------
// Client side (tests, the Canvas): find or start the daemon, talk to it.
// ---------------------------------------------------------------------------

/** One newline-delimited JSON connection to a daemon socket. */
export class DaemonConnection {
  #socket;
  #pending = new Map();
  #buffer = "";
  #next = 0;
  replies = [];
  /** The daemon's closing line once it arrived, else null. */
  closing = null;
  closed;

  constructor(socket) {
    this.#socket = socket;
    socket.setEncoding("utf8");
    socket.on("data", (chunk) => {
      this.#buffer += chunk;
      let index;
      while ((index = this.#buffer.indexOf("\n")) >= 0) {
        const line = this.#buffer.slice(0, index);
        this.#buffer = this.#buffer.slice(index + 1);
        let reply;
        try {
          reply = JSON.parse(line);
        } catch {
          continue;
        }
        if (reply?.closing === true && (reply.id === undefined || reply.id === null)) {
          this.#onClosing(reply);
          continue;
        }
        this.replies.push(reply);
        const key = reply?.id === undefined || reply.id === null ? null : String(reply.id);
        const waiter = key !== null ? this.#pending.get(key) : this.#pending.get("__next_null__");
        if (waiter) {
          this.#pending.delete(key !== null ? key : "__next_null__");
          waiter.resolve(reply);
        }
      }
    });
    this.closed = new Promise((resolve) => {
      socket.once("close", () => {
        for (const waiter of this.#pending.values()) waiter.reject(new DaemonError(CODE.DAEMON_UNREACHABLE, "The daemon closed the connection.", { stage: STAGE.AFTER_SEND }));
        this.#pending.clear();
        resolve();
      });
    });
    socket.on("error", () => {});
  }

  /** The daemon's closing line: it runs nothing still waiting, so those never reached the device. */
  #onClosing(reply) {
    this.closing = reply;
    const message = reply.error?.message ?? "The daemon is stopping.";
    for (const waiter of this.#pending.values()) waiter.reject(new DaemonError(CODE.DAEMON_UNREACHABLE, message, { stage: STAGE.BEFORE_SEND }));
    this.#pending.clear();
  }

  static connect(path, { timeoutMs = 2000 } = {}) {
    return new Promise((resolve, reject) => {
      const socket = netConnect(path);
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new DaemonError(CODE.DAEMON_UNREACHABLE, `No daemon answered at ${path}.`));
      }, timeoutMs);
      socket.once("connect", () => {
        clearTimeout(timer);
        resolve(new DaemonConnection(socket));
      });
      socket.once("error", (error) => {
        clearTimeout(timer);
        reject(new DaemonError(CODE.DAEMON_UNREACHABLE, `No daemon answered at ${path}: ${error.message}`));
      });
    });
  }

  get open() {
    return !this.#socket.destroyed;
  }

  /** Sends `request` (an id is added when missing) and resolves with its reply. */
  request(request, { timeoutMs = 30_000 } = {}) {
    const id = request.id ?? `c${++this.#next}`;
    const message = { ...request, id };
    if (this.closing) {
      return Promise.reject(new DaemonError(CODE.DAEMON_UNREACHABLE, "The daemon closed this connection; the request was not sent.", { stage: STAGE.BEFORE_SEND }));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(String(id));
        reject(new DaemonError(CODE.DAEMON_UNREACHABLE, `No reply to ${request.op} within ${timeoutMs} ms.`, { stage: STAGE.AFTER_SEND }));
      }, timeoutMs);
      this.#pending.set(String(id), {
        resolve: (reply) => { clearTimeout(timer); resolve(reply); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      this.#socket.write(`${JSON.stringify(message)}\n`);
    });
  }

  /** Writes a raw line (tests of malformed input); resolves with the next reply without an id. */
  sendRaw(line, { timeoutMs = 5000 } = {}) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete("__next_null__");
        reject(new Error("No reply to the raw line."));
      }, timeoutMs);
      this.#pending.set("__next_null__", {
        resolve: (reply) => { clearTimeout(timer); resolve(reply); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      this.#socket.write(line.endsWith("\n") ? line : `${line}\n`);
    });
  }

  close() {
    this.#socket.end();
    this.#socket.destroy();
  }
}

/** One request on a fresh connection. */
export async function requestDaemon(socketPath, request, { timeoutMs = 30_000 } = {}) {
  const connection = await DaemonConnection.connect(socketPath);
  try {
    return await connection.request(request, { timeoutMs });
  } finally {
    connection.close();
  }
}

/**
 * The daemon of `udid`: a live one from the registry, else one started under the
 * clients' start lock `<UDID>.lock`. Resolves with the daemon's status line
 * (`status: "serving"` or `"unsupported"`, plus the registry fields); rejects with a
 * DaemonError `daemon_start_failed` when it could not start.
 */
export async function ensureDaemon(options = {}) {
  const { udid, stateBase = defaultStateBase() } = options;
  if (!UDID_PATTERN.test(udid ?? "")) throw new TypeError("A valid simulator UDID is required.");
  // Concurrent callers in one process (the Canvas server) share one start.
  const key = `${resolvePath(stateBase)}\0${udid}`;
  let start = startsInFlight.get(key);
  if (!start) {
    start = ensureDaemonOnce({ ...options, stateBase }).finally(() => startsInFlight.delete(key));
    startsInFlight.set(key, start);
  }
  return start;
}

const startsInFlight = new Map();

async function ensureDaemonOnce({
  udid, stateBase, env = process.env, idleMs, nodePath = process.execPath, timeoutMs = START_TIMEOUT_MS,
}) {
  const base = resolvePath(stateBase);
  const live = await liveRegistry(base, udid);
  if (live) return { ...live, status: live.state };
  const paths = daemonPaths(base, udid);
  mkdirSync(paths.dir, { recursive: true, mode: 0o700 });
  const deadline = performance.now() + timeoutMs;
  while (!tryLock(paths.startLock).taken) {
    if (performance.now() > deadline) throw new DaemonError(CODE.DAEMON_START_FAILED, `The start lock ${paths.startLock} stayed taken.`);
    await sleep(25);
    const started = await liveRegistry(base, udid);
    if (started) return { ...started, status: started.state };
  }
  try {
    const again = await liveRegistry(base, udid);
    if (again) return { ...again, status: again.state };
    const args = [DAEMON_SCRIPT, "--udid", udid, "--state-base", base];
    if (idleMs !== undefined) args.push("--idle-ms", String(idleMs));
    const child = nodeSpawn(nodePath, args, { env, detached: true, stdio: ["ignore", "pipe", "ignore"] });
    const line = await new Promise((resolve, reject) => {
      let text = "";
      const timer = setTimeout(() => finish(new DaemonError(CODE.DAEMON_START_FAILED, `The daemon did not report within ${timeoutMs} ms.`)), timeoutMs);
      const finish = (error, value) => {
        clearTimeout(timer);
        child.stdout.removeAllListeners("data");
        child.removeListener("exit", onExit);
        if (error) reject(error);
        else resolve(value);
      };
      const onExit = (code) => finish(new DaemonError(CODE.DAEMON_START_FAILED, `The daemon exited (code ${code}) before it reported.`));
      child.once("exit", onExit);
      child.once("error", (error) => finish(new DaemonError(CODE.DAEMON_START_FAILED, `The daemon could not start: ${error.message}`)));
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        text += chunk;
        const index = text.indexOf("\n");
        if (index >= 0) finish(null, text.slice(0, index));
      });
    });
    child.stdout.destroy();
    child.unref();
    let status;
    try {
      status = JSON.parse(line);
    } catch {
      throw new DaemonError(CODE.DAEMON_START_FAILED, "The daemon printed no status line.");
    }
    if (status.status === "already_running") return { ...status, status: status.state };
    if (status.status === "failed") {
      throw new DaemonError(status.error?.code ?? CODE.DAEMON_START_FAILED, status.error?.message ?? "The daemon failed to start.", {
        reason: status.error?.reason ?? null,
      });
    }
    return status;
  } finally {
    releaseLock(paths.startLock);
  }
}

// ---------------------------------------------------------------------------
// The daemon.
// ---------------------------------------------------------------------------

function classifyReadError(error) {
  const text = `${error?.details ?? ""} ${error?.message ?? ""}`;
  if (UNLIT_MESSAGE.test(text)) return "unlit";
  if (NO_TRANSLATION_MESSAGE.test(text)) return "no_translation";
  return "other";
}

/** A BUSY report with QUIET_STUCK_SIGNAL pending: what a stuck companion sends last (see QUIET_STUCK_MS). */
function stuckReport(event) {
  return event?.type === "state_changed" && String(event.state).toUpperCase() === "BUSY"
    && (event.busySignals ?? []).includes(QUIET_STUCK_SIGNAL);
}

/**
 * The silent probe answers in a row on a suspicion (see STUCK_SILENT_PROBES) that still
 * count at `at` (performance.now() ms): the last answer's run while it is silent and
 * younger than STUCK_PROBE_FRESH_MS, else 0. An `ok` answer ends a run.
 */
export function freshSilentRun(evidence, at) {
  const probe = evidence?.probe;
  if (probe?.result !== "silent" || !(at - probe.at < STUCK_PROBE_FRESH_MS)) return 0;
  return Math.max(1, Number(probe.silentRun) || 1);
}

function unreachable(error) {
  const code = error?.code;
  return code === "ENOENT" || code === "ECONNREFUSED" || code === "ERR_HTTP2_SESSION_ERROR"
    || /ENOENT|ECONNREFUSED|connect/i.test(error?.message ?? "");
}

export class IosDeviceDaemon {
  udid;
  stateBase;
  paths;
  socket;
  idleMs;
  state = "starting";
  #env;
  #tools;
  #server = null;
  /** Open client sockets → { inflight, closing, noticed, attachChain }. */
  #connections = new Map();
  #attachments = new Map();
  #inflight = 0;
  #idleTimer = null;
  #pollTimer = null;
  #inputChain = Promise.resolve();
  #client = null;
  #hid = null;
  #primed = false;
  #quietStream = null;
  #quietLoop = null;
  #quietOpenedAt = null;
  #quietFirstLogged = false;
  /** Resolves once the current quiet stream delivered its first event (see QUIET_WARM_MS). */
  #quietArm = null;
  /** The stream reported a new target (target_changed or target_exited) no touch was written to yet. */
  #targetUntouched = false;
  /** When the current stream last reported a new target that has sent no other event since. */
  #targetChangedAt = null;
  /** The last touch whose confirmation window ran out: a late report of it is logged (touch_late). */
  #lastExpired = null;
  /** Fires QUIET_STUCK_MS after a BUSY event with QUIET_STUCK_SIGNAL pending, unless a state or touch report came. */
  #stuckTimer = null;
  /**
   * The current companion's last state or touch report was BUSY with QUIET_STUCK_SIGNAL
   * pending: { at, signals, probe, noted }. Target changes leave it as it is; any other
   * report of the current stream clears it, and so does a new companion connection.
   * `probe` is the last probe's answer for it ({ result, at, ms }), `noted` the checks
   * already logged as skipped (once each).
   */
  #stuckEvidence = null;
  #stuckRestartAt = -Infinity;
  /**
   * Stuck replacements in a row with no sign in between that the companion was healthy
   * (see STUCK_BACKOFF_MAX_FACTOR); the next one needs stuckRestartGap(interval, this).
   */
  #stuckStreak = 0;
  /** When the idle check last probed (see QUIET_STUCK_PROBE_MS): at most once per interval. */
  #stuckIdleProbeAt = -Infinity;
  /** The probe in flight, shared by the idle check and a touch: { evidence, promise }. */
  #stuckProbe = null;
  /** Called when the open stream's report clears the evidence (a probe in flight ends: ok). */
  #stuckCleared = null;
  #companion = null; // { pid, endpoint, owned, startedAt }
  #inherited = null;
  #restarting = null;
  #serving = null;
  #servingResolve = null;
  #stopping = null;
  #unsupported = null;
  #versions = { xcode_path: null, xcode: null, coresimulator: null, companion: null };
  #startedAt = new Date().toISOString();
  #startedMono = performance.now();
  #requests = 0;
  #byOp = {};
  #timings = [];
  #restarts = 0;
  quiet = { state: null, busySignals: [], pid: null, touches: 0, events: 0, lastEventAt: null };
  #watchers = new Set();
  /** Touch inputs written and not yet confirmed, oldest first (see #expectTouch). */
  #touchQueue = [];
  #starting = null;
  #lockHeld = false;
  #listening = false;
  #onExit = null;
  #backoff = IDB_RETRY_MIN_MS;
  #stoppedResolve;
  /** Resolves once stop() finished (idle, a stop request, a signal). */
  stopped = new Promise((resolve) => {
    this.#stoppedResolve = resolve;
  });

  constructor({ udid, stateBase, idleMs, env = process.env } = {}) {
    if (!UDID_PATTERN.test(udid ?? "")) throw new TypeError("A valid simulator UDID is required.");
    this.udid = udid;
    this.stateBase = resolvePath(stateBase ?? defaultStateBase(env));
    this.paths = daemonPaths(this.stateBase, udid);
    this.socket = socketPathFor(this.stateBase, udid);
    this.idleMs = idleLimit({ flag: idleMs, env });
    this.#env = env;
    this.#tools = {
      idb: env.AUTONOM_IDB || which("idb", env),
      companion: env.AUTONOM_IDB_COMPANION_BIN || which("idb_companion", env),
      xcrun: env.AUTONOM_SIMCTL || which("xcrun", env),
      ps: which("ps", env) ?? "/bin/ps",
      stateFile: env.AUTONOM_IDB_STATE_FILE || IDB_STATE_FILE,
    };
    this.#serving = new Promise((resolve) => {
      this.#servingResolve = resolve;
    });
  }

  log(event, fields = {}) {
    try {
      appendFileSync(this.paths.log, `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, event, ...fields })}\n`, { mode: 0o600 });
    } catch {
      // The log is best effort.
    }
  }

  /** Starts serving; resolves with the status line the process prints. */
  start() {
    this.#starting ??= this.#start();
    return this.#starting;
  }

  /** A stop (a signal) arrived while starting: give up before the next step. */
  #checkStopped() {
    if (!this.#stopping) return;
    this.#server?.close();
    throw new DaemonError(CODE.DAEMON_START_FAILED, "The daemon was stopped while it started.", { reason: "stopped" });
  }

  async #start() {
    if (!this.socket.fits) {
      throw new DaemonError(CODE.DAEMON_START_FAILED, `Socket path is longer than ${MAX_SOCKET_PATH_BYTES} bytes: ${this.socket.path}`, {
        reason: "socket_path_too_long", bytes: this.socket.bytes,
      });
    }
    mkdirSync(this.paths.dir, { recursive: true, mode: 0o700 });
    try {
      if (statSync(this.paths.log).size > LOG_MAX_BYTES) truncateSync(this.paths.log, 0);
    } catch {
      // No log yet.
    }
    const lock = await this.#takeDaemonLock();
    if (!lock.taken) return lock.status;
    this.#installExitHook();
    this.#checkStopped();
    this.#clearStaleRegistry();
    await this.#listen();
    this.#checkStopped();
    this.log("start", { idle_ms: this.idleMs, socket_bytes: this.socket.bytes });
    await this.#readVersions();
    this.#checkStopped();
    const known = versionAtLeast(this.#versions.companion);
    if (known === false) {
      this.#markUnsupported(`idb_companion ${this.#versions.companion} is older than ${MIN_COMPANION_VERSION.join(".")}`);
    } else {
      await this.#acquireCompanion({ initial: true });
      if (!this.#unsupported) {
        this.state = "serving";
        this.#startPolling();
      }
    }
    this.#checkStopped();
    this.#writeRegistry();
    this.#servingResolve();
    this.#armIdle();
    this.log("ready", { state: this.state, owned: this.#companion?.owned ?? null, ms: Math.round(performance.now() - this.#startedMono) });
    return { status: this.state, ...this.registry() };
  }

  async #takeDaemonLock() {
    const deadline = performance.now() + LOCK_TIMEOUT_MS;
    for (;;) {
      const lock = tryLock(this.paths.daemonLock);
      if (lock.taken) {
        this.#lockHeld = true;
        return { taken: true };
      }
      // Another daemon owns this UDID: report it once its registry is up.
      const registry = await liveRegistry(this.stateBase, this.udid);
      if (registry) return { taken: false, status: { ...registry, status: "already_running", state: registry.state } };
      if (performance.now() > deadline) {
        throw new DaemonError(CODE.DAEMON_START_FAILED, `Daemon ${lock.holder} holds ${this.paths.daemonLock} but never served.`, { reason: "lock_held" });
      }
      await sleep(50);
      this.#checkStopped();
    }
  }

  #clearStaleRegistry() {
    const stale = readJson(this.paths.registry);
    if (!stale) return;
    if (stale.pid !== process.pid && pidAlive(stale.pid) && stale.schema === IOSD_SCHEMA) {
      // Holding the daemon lock, a live pid here is not a daemon of ours any more (pid reuse).
      this.log("stale_registry_pid_reused", { pid: stale.pid });
    }
    if (stale.companion?.owned && Number.isInteger(stale.companion.pid)) {
      this.#inherited = { pid: stale.companion.pid };
    }
    if (typeof stale.socket === "string" && stale.socket !== this.socket.path) {
      try {
        if (lstatSync(stale.socket).isSocket()) unlinkSync(stale.socket);
        const dir = dirname(stale.socket);
        if (basename(dir).startsWith("autonom-iosd-")) rmSync(dir, { recursive: true, force: true });
      } catch {
        // Already gone.
      }
    }
    try {
      unlinkSync(this.paths.registry);
    } catch {
      // Already gone.
    }
    this.log("stale_registry_removed", { pid: stale.pid ?? null });
  }

  async #listen() {
    const { dir, path } = this.socket;
    try {
      const info = lstatSync(dir);
      if (!info.isDirectory() || info.isSymbolicLink()) throw new DaemonError(CODE.DAEMON_START_FAILED, `${dir} is not a directory.`, { reason: "socket_dir_invalid" });
      if (typeof process.getuid === "function" && info.uid !== process.getuid()) {
        throw new DaemonError(CODE.DAEMON_START_FAILED, `${dir} belongs to another user.`, { reason: "socket_dir_invalid" });
      }
      chmodSync(dir, 0o700);
    } catch (error) {
      if (error instanceof DaemonError) throw error;
      mkdirSync(dir, { mode: 0o700 });
      chmodSync(dir, 0o700);
    }
    if (existsSync(path)) {
      if (await socketConnectable(path)) {
        throw new DaemonError(CODE.DAEMON_START_FAILED, `Another process serves ${path}.`, { reason: "socket_in_use" });
      }
      unlinkSync(path);
    }
    this.#server = netCreateServer((socket) => this.#onConnection(socket));
    await new Promise((resolve, reject) => {
      this.#server.once("error", reject);
      this.#server.listen(path, () => {
        this.#server.off("error", reject);
        resolve();
      });
    });
    this.#listening = true;
    chmodSync(path, 0o600);
  }

  async #readVersions() {
    const env = this.#env;
    let developer = env.DEVELOPER_DIR || null;
    if (!developer) {
      const selected = await run("xcode-select", ["-p"], { env: companionEnvironment(env), timeoutMs: 3000 });
      developer = selected.code === 0 ? selected.stdout.trim() || null : null;
    }
    if (developer) {
      const contents = dirname(developer);
      this.#versions.xcode_path = basename(developer) === "Developer" && basename(contents) === "Contents" ? dirname(contents) : developer;
      this.#versions.xcode = plistValue(join(contents, "version.plist"), "CFBundleShortVersionString");
    }
    this.#versions.coresimulator = plistValue(CORESIMULATOR_PLIST, "CFBundleVersion");
    const binary = this.#tools.companion;
    if (binary) {
      const result = await run(binary, ["--version"], { env: companionEnvironment(env), timeoutMs: 10_000 });
      let real = null;
      try {
        real = realpathSync(binary);
      } catch {
        // Not resolvable.
      }
      this.#versions.companion = companionVersionFrom(result.code === 0 ? result.stdout : "", real);
    }
  }

  #markUnsupported(message) {
    this.state = "unsupported";
    this.#unsupported = { message };
    this.log("unsupported", { companion_version: this.#versions.companion });
  }

  #unsupportedError() {
    return new DaemonError(CODE.COMPANION_TOO_OLD, this.#unsupported.message, { version: this.#versions.companion });
  }

  /**
   * Adopts the live companion idb lists for the UDID, else starts one with `idb connect`
   * (owned), then opens the gRPC session, the quiet stream and the primed HID stream.
   */
  async #acquireCompanion({ initial = false } = {}) {
    let entry = liveCompanionEntry(readIdbState(this.#tools.stateFile), this.udid);
    let owned = false;
    if (entry) {
      // A companion this UDID's previous daemon started and left behind stays ours.
      owned = Boolean(this.#inherited && this.#inherited.pid === entry.pid) || Boolean(this.#companion?.owned && this.#companion.pid === entry.pid);
      this.log(owned ? "companion_inherited" : "companion_adopted", { pid: entry.pid });
    } else {
      if (!this.#tools.idb) throw new DaemonError(CODE.DAEMON_START_FAILED, "The idb client was not found (AUTONOM_IDB or PATH).", { reason: "idb_missing" });
      const t0 = performance.now();
      const result = await run(this.#tools.idb, ["connect", this.udid], { env: companionEnvironment(this.#env), timeoutMs: IDB_CONNECT_TIMEOUT_MS });
      entry = liveCompanionEntry(readIdbState(this.#tools.stateFile), this.udid);
      this.log("idb_connect", { code: result.code, ms: Math.round(performance.now() - t0), registered: Boolean(entry) });
      if (!entry) {
        throw new DaemonError(CODE.DAEMON_START_FAILED, `idb connect ${this.udid} registered no live companion (exit ${result.code}).`, { reason: "companion_start_failed" });
      }
      owned = true;
    }
    this.#inherited = null;
    this.#companion = { pid: entry.pid, endpoint: entry.endpoint, owned, startedAt: performance.now() };
    // A stop that arrived during idb connect: the companion is recorded (owned or not),
    // so stop() releases it as usual instead of leaving it to the next daemon.
    if (initial) this.#checkStopped();
    this.#client = new IdbCompanionClient(entry.endpoint, { hidCloseWaitMs: hidWaitLimit(this.#env) });
    const supported = await this.#openQuietStream({ probe: initial });
    if (!supported) {
      this.#markUnsupported(`idb_companion ${this.#versions.companion ?? "(unknown version)"} does not support accessibility_quiescence`);
      await this.#releaseCompanion();
      return;
    }
    this.#hid = this.#client.hid();
    this.#hid.on("drop", ({ error }) => this.log("hid_drop", { code: error?.code ?? null }));
    this.#hid.on("lost", ({ events, error }) => this.log("hid_lost", { events, code: error?.code ?? null }));
    // Opened ahead of the first input so a broken input path shows at start; the call then
    // ends after the idle window like every other (see IdbHidStream).
    await this.#hid.open();
    this.#primed = false;
    await this.#prime().catch((error) => this.log("prime_failed", { message: error.message }));
    await this.#warm();
    if (!initial) this.#writeRegistry();
  }

  async #prime() {
    const t0 = performance.now();
    await this.#hid.keyUp(PRIMING_KEYCODE);
    this.#primed = true;
    this.log("hid_primed", { keycode: PRIMING_KEYCODE, ms: Math.round(performance.now() - t0) });
  }

  /** Opens the kept-open quiet stream; with `probe`, false when the companion lacks it. */
  async #openQuietStream({ probe = false } = {}) {
    const client = this.#client;
    this.#newQuietArm();
    const stream = await client.accessibilityQuiescence({ quietWindowMs: QUIET_WINDOW_MS });
    this.#quietStream = stream;
    if (probe) {
      let timer;
      const outcome = await Promise.race([
        stream.started.then(() => "ok", (error) => error),
        new Promise((resolve) => { timer = setTimeout(() => resolve("silent"), QUIET_PROBE_MS); }),
      ]).finally(() => clearTimeout(timer));
      if (outcome instanceof GrpcError && outcome.code === 12) return false;
      if (outcome instanceof Error) throw new DaemonError(CODE.DAEMON_START_FAILED, `The quiet stream failed: ${outcome.message}`, { reason: "quiet_stream_failed" });
    }
    this.#quietOpenedAt = performance.now();
    this.#quietFirstLogged = false;
    this.#quietLoop = this.#readQuiet(stream, client);
    return true;
  }

  /** A new quiet stream is about to open: it is cold until its first event. */
  #newQuietArm() {
    this.#quietArm?.cancel();
    const arm = { armed: false, touched: false, gaveUp: false, cancelled: false };
    arm.promise = new Promise((resolve) => {
      arm.resolve = () => {
        arm.armed = true;
        resolve(true);
      };
      // The stream is gone (dropped, replaced, stopping): a wait for it ends at once.
      arm.cancel = () => {
        if (arm.armed) return;
        arm.cancelled = true;
        resolve(false);
      };
    });
    this.#quietArm = arm;
    return arm;
  }

  /**
   * The quiet stream's first event, up to `ms`: true once it came, false at the limit. A
   * stream that let one wait run out is not waited for again (a target that sends no
   * event must not slow every touch); its touches may then go unconfirmed.
   */
  async #quietWarm(ms) {
    const until = performance.now() + ms;
    for (;;) {
      const arm = this.#quietArm;
      if (!arm || arm.armed) return true;
      if (arm.gaveUp || arm.cancelled || this.#stopping) return false;
      const outcome = await within(arm.promise, until - performance.now());
      if (outcome.value === true) return true;
      // Only a full wait that ran out gives the stream up; one cut short by a deadline does not.
      if (outcome.timedOut) {
        if (ms >= QUIET_WARM_MS && arm === this.#quietArm && !arm.armed) arm.gaveUp = true;
        return false;
      }
      // Cancelled: the stream was replaced by a new one (wait for that) or dropped (stop).
      if (this.#quietArm === arm || !this.#client || performance.now() >= until) return false;
    }
  }

  /**
   * A new companion: wait (in parallel, up to QUIET_WARM_MS) for the quiet stream's first
   * event and for the companion's status on the priming call, so the first request does
   * not meet a cold stream (a touch it would not report) or a first hid call still being
   * set up. Neither is required: past the limit the daemon serves as before.
   */
  async #warm() {
    const t0 = performance.now();
    let hidMs = null;
    const hid = this.#hid;
    const primed = this.#primed && hid
      ? hid.flush().then(() => { hidMs = Math.round(performance.now() - t0); }, (error) => this.log("prime_unconfirmed", { message: error.message }))
      : Promise.resolve();
    const [quiet] = await Promise.all([this.#quietWarm(QUIET_WARM_MS), within(primed, QUIET_WARM_MS)]);
    this.log("warm", { quiet_armed: quiet, quiet_ms: quiet ? Math.round(performance.now() - t0) : null, hid_ms: hidMs });
  }

  /** Reads the quiet stream for as long as this companion connection lives, reopening it when it ends. */
  async #readQuiet(stream, client) {
    let current = stream;
    const gone = () => this.#stopping || this.#client !== client || this.state === "unsupported" || this.#restarting;
    for (;;) {
      const openedAt = performance.now();
      if (current) {
        try {
          for await (const event of current) this.#onQuietEvent(event, current);
        } catch (error) {
          this.log("quiet_stream_error", { code: error?.code ?? null, message: String(error?.details || error?.message || "").slice(0, 200), ms: Math.round(performance.now() - openedAt) });
        }
      }
      if (gone()) return;
      // The stream ended (target exited, dropped): reopen it while the companion lives;
      // one that ends at once is retried slowly rather than in a tight loop.
      await sleep(performance.now() - openedAt < 1000 ? 1000 : 200);
      if (gone()) return;
      if (!pidAlive(this.#companion?.pid)) {
        this.#companionLost("quiet_stream_ended");
        return;
      }
      this.quiet.state = null;
      try {
        this.#newQuietArm();
        current = await client.accessibilityQuiescence({ quietWindowMs: QUIET_WINDOW_MS });
        this.#quietStream = current;
        this.log("quiet_reopened");
      } catch (error) {
        this.log("quiet_reopen_failed", { message: error.message });
        current = null;
      }
    }
  }

  #onQuietEvent(event, stream = this.#quietStream) {
    const quiet = this.quiet;
    quiet.events += 1;
    quiet.lastEventAt = performance.now();
    // Only the current stream's own event warms it (not one left over from a cancelled one).
    if (stream === this.#quietStream && this.#quietArm && !this.#quietArm.armed) this.#quietArm.resolve();
    if (!this.#quietFirstLogged) {
      this.#quietFirstLogged = true;
      this.log("quiet_first_event", { type: event.type, state: event.state ?? null, ms: Math.round(quiet.lastEventAt - (this.#quietOpenedAt ?? quiet.lastEventAt)) });
    }
    if (event.pid !== undefined) quiet.pid = event.pid;
    const targetEvent = event.type === "target_changed" || event.type === "target_exited";
    if (!targetEvent && stream === this.#quietStream && this.#targetChangedAt !== null) {
      this.log("quiet_target_first_event", { type: event.type, state: event.state ?? null, ms: Math.round(quiet.lastEventAt - this.#targetChangedAt) });
      this.#targetChangedAt = null;
    }
    // A stuck companion still reports target changes (REV-043): they neither end the
    // silence since its last BUSY report nor clear that report's evidence.
    // Another BUSY report with QUIET_STUCK_SIGNAL still pending is no progress either (a
    // stuck companion sends one for each app launched on it): the evidence, its silence and
    // its probe's answer stand.
    if (stream === this.#quietStream && !targetEvent && !(this.#stuckEvidence && stuckReport(event))) {
      // A report other than that BUSY from a companion that serves: it is not stuck.
      if (!stuckReport(event) && this.state === "serving") this.#stuckHealthy(event.type === "touches_completed" ? "touch" : "report");
      clearTimeout(this.#stuckTimer);
      this.#stuckTimer = null;
      if (this.#stuckEvidence) this.#stuckCleared?.();
      this.#stuckEvidence = null;
      if (stuckReport(event)) {
        this.#stuckEvidence = { at: quiet.lastEventAt, signals: [...event.busySignals] };
        this.#stuckTimer = setTimeout(() => this.#onQuietStuck(), QUIET_STUCK_MS);
        this.#stuckTimer.unref?.();
      }
    }
    if (event.type === "state_changed") {
      quiet.state = typeof event.state === "string" ? event.state.toLowerCase() : event.state;
      quiet.busySignals = event.busySignals ?? [];
    } else if (event.type === "touches_completed") {
      quiet.touches += 1;
      // The oldest touch whose finger already went up owns this completion. One still
      // being written cannot have completed: the event is a late one of an expired touch.
      const oldest = this.#touchQueue[0];
      if (oldest?.up) oldest.settle(true);
      else if (this.#lastExpired) {
        // Never counted for a later touch; logged so a window too short for this app shows.
        const expired = this.#lastExpired;
        this.#lastExpired = null;
        this.log("touch_late", { ms: Math.round(quiet.lastEventAt - expired.windowAt), window_ms: expired.windowMs });
      }
    } else if (event.type === "target_changed" || event.type === "target_exited") {
      quiet.state = null;
      // The first touch on the new target is reported late: it gets FIRST_TOUCH_CONFIRM_MS.
      if (stream === this.#quietStream) {
        this.#targetUntouched = true;
        this.#targetChangedAt = quiet.lastEventAt;
      }
      this.log("quiet_target", { type: event.type, pid: event.pid ?? null });
    }
    for (const watcher of this.#watchers) watcher(event);
  }

  /**
   * The last state or touch report was BUSY with QUIET_STUCK_SIGNAL pending: ms since it
   * (target changes do not count), else null.
   */
  #stuckSilenceMs() {
    const evidence = this.#stuckEvidence;
    if (!this.#quietStream || !this.#client || !evidence) return null;
    return performance.now() - evidence.at;
  }

  /** The stream is stuck (see QUIET_STUCK_MS): BUSY with QUIET_STUCK_SIGNAL pending, no state or touch report since. */
  #quietStuck() {
    const silent = this.#stuckSilenceMs();
    return silent !== null && silent >= QUIET_STUCK_MS;
  }

  /**
   * The stuck timer, once nothing runs (a request in flight is let finish): a suspicion
   * is first probed at most once per interval (and only when a replacement could follow,
   * for a companion the daemon owns); a silent probe is followed by the next at once, and
   * STUCK_SILENT_PROBES silent ones in a row get the companion replaced. While the
   * silence lasts the check comes back after the interval.
   */
  #onQuietStuck() {
    this.#stuckTimer = null;
    const evidence = this.#stuckEvidence;
    const silent = this.#stuckSilenceMs();
    if (silent === null || this.#stopping || this.#restarting || this.state !== "serving") return;
    const busy = this.#inflight > 0 || this.#touchQueue.length > 0 || this.#watchers.size > 0;
    // A timer may fire a little before the silence is complete: wait out the rest.
    if (busy || silent < QUIET_STUCK_MS) {
      this.#armStuckTimer(busy ? 500 : QUIET_STUCK_MS - silent + 5);
      return;
    }
    // A probe in flight (a touch's) decides; its answer brings the check back.
    if (this.#stuckProbe) return;
    const interval = stuckRestartInterval(this.#env);
    // An overloaded host: neither probed nor replaced; checked again later.
    const load = hostLoad(this.#env);
    if (load.overloaded) {
      this.#noteStuckSkip(evidence, "idle", "host_load", load);
      this.#armStuckTimer(Math.min(interval, HOST_LOAD_RECHECK_MS));
      return;
    }
    const now = performance.now();
    const silentRun = freshSilentRun(evidence, now);
    if (silentRun >= STUCK_SILENT_PROBES) {
      this.#replaceStuckCompanion("idle");
      return;
    }
    const owned = Boolean(this.#companion?.owned);
    const gap = this.#stuckGap();
    // The next probe of a fresh silent one is not held by the idle probes' interval.
    const sinceRestart = owned ? gap - (now - this.#stuckRestartAt) : 0;
    const wait = silentRun > 0 ? sinceRestart : Math.max(interval - (now - this.#stuckIdleProbeAt), sinceRestart);
    if (wait > 0) {
      this.#noteStuckSkip(evidence, "idle", "too_soon", { gap_ms: gap });
      this.#armStuckTimer(wait + 5);
      return;
    }
    if (silentRun === 0) this.#stuckIdleProbeAt = now;
    this.#probeStuck("idle", QUIET_STUCK_PROBE_MS).then((probe) => {
      if (this.#stuckEvidence !== evidence) return;
      if (probe.result !== "silent") {
        this.#armStuckTimer(interval);
        return;
      }
      // A request that came during the probe is let finish (a touch decides for itself);
      // otherwise the check comes back at once: the next probe, or the replacement.
      const busyNow = this.#inflight > 0 || this.#touchQueue.length > 0 || this.#watchers.size > 0;
      this.#armStuckTimer(busyNow ? 500 : 0);
    }).catch((error) => this.log("quiet_probe_failed", { message: String(error?.message ?? error).slice(0, 200) }));
  }

  #armStuckTimer(ms) {
    clearTimeout(this.#stuckTimer);
    this.#stuckTimer = setTimeout(() => this.#onQuietStuck(), Math.max(0, ms));
    this.#stuckTimer.unref?.();
  }

  /**
   * A suspicion a check left unprobed (too soon, not owned, host load, deadline): logged
   * once per check and reason, with `extra` (the gap, or the load and the CPU count).
   */
  #noteStuckSkip(evidence, when, reason, extra = {}) {
    evidence.noted ??= new Set();
    const key = `${when}:${reason}`;
    if (evidence.noted.has(key)) return;
    evidence.noted.add(key);
    const { overloaded: _overloaded, ...fields } = extra;
    this.log("quiet_suspect", {
      when, silent_ms: Math.round(this.#stuckSilenceMs() ?? 0), signals: evidence.signals, owned: this.#companion?.owned ?? null, reason,
      ...fields,
    });
  }

  /** The least gap before the next stuck replacement (see STUCK_BACKOFF_MAX_FACTOR). */
  #stuckGap() {
    return stuckRestartGap(stuckRestartInterval(this.#env), this.#stuckStreak);
  }

  /** A sign that the current companion is healthy: the stuck backoff starts over. */
  #stuckHealthy(why) {
    if (this.#stuckStreak === 0) return;
    this.log("quiet_backoff_reset", { why, streak: this.#stuckStreak });
    this.#stuckStreak = 0;
  }

  /**
   * Probes a suspected companion (see QUIET_STUCK_PROBE_MS): opens a new quiet stream on
   * the same session and waits up to `ms` for its first state or touch report. Resolves
   * { result, ms } with result `ok` (the probe stream reported), `cleared` (the open
   * stream reported meanwhile), `silent` (nothing within `ms`: one step of the proof,
   * see STUCK_SILENT_PROBES), or `gone` (the connection changed, the daemon is stopping,
   * or the probe call failed: no proof either way). `silent_run` counts the silent
   * answers in a row on this suspicion, this one included (0 unless silent). One probe
   * runs at a time; a second caller shares it.
   */
  #probeStuck(when, ms) {
    if (this.#stuckProbe) return this.#stuckProbe.promise;
    const client = this.#client;
    const evidence = this.#stuckEvidence;
    const probe = { evidence };
    probe.promise = (async () => {
      const t0 = performance.now();
      let stream = null;
      let timer = null;
      let first = null;
      let skipped = 0;
      let message = null;
      let result;
      let settled = false;
      try {
        const cleared = new Promise((resolve) => { this.#stuckCleared = () => resolve("cleared"); });
        const silent = new Promise((resolve) => { timer = setTimeout(() => resolve("silent"), Math.max(0, ms)); });
        const reported = (async () => {
          const opened = await client.accessibilityQuiescence({ quietWindowMs: QUIET_WINDOW_MS });
          // Opened after the probe was decided: closed at once, never left open.
          if (settled) {
            opened.cancel();
            return "gone";
          }
          stream = opened;
          // A stuck companion still sends target changes, and a new stream of it may send
          // one BUSY with QUIET_STUCK_SIGNAL pending: neither counts, any other report does.
          for await (const event of stream) {
            if (event.type === "target_changed" || event.type === "target_exited") continue;
            if (stuckReport(event)) {
              skipped += 1;
              continue;
            }
            first = { type: event.type, state: event.state ?? null, busy_signals: event.busySignals ?? [] };
            return "ok";
          }
          return "gone";
        })().catch((error) => {
          message = String(error?.details || error?.message || "").slice(0, 200);
          return "gone";
        });
        result = await Promise.race([reported, cleared, silent]);
      } finally {
        settled = true;
        clearTimeout(timer);
        this.#stuckCleared = null;
        stream?.cancel();
      }
      if (this.#client !== client || this.#stopping) result = "gone";
      const elapsed = Math.round(performance.now() - t0);
      // Recorded on the suspicion it answers (a newer one is not answered by it). A silent
      // answer extends the run only of a silent one still fresh when this probe began.
      const silentRun = result === "silent" ? freshSilentRun(evidence, t0) + 1 : 0;
      if (result === "ok" || result === "silent") evidence.probe = { result, at: performance.now(), ms: elapsed, silentRun };
      if (result === "ok") this.#stuckHealthy("probe");
      this.log("quiet_probe", {
        when, result, ms: elapsed, limit_ms: Math.round(ms), silent_ms: Math.round(performance.now() - evidence.at),
        ...(result === "silent" ? { silent_run: silentRun } : {}),
        ...(first ? { first } : {}), ...(skipped ? { busy_reports: skipped } : {}), ...(message ? { message } : {}),
      });
      return { result, ms: elapsed, silent_run: silentRun };
    })().finally(() => {
      if (this.#stuckProbe === probe) this.#stuckProbe = null;
    });
    this.#stuckProbe = probe;
    return probe.promise;
  }

  /**
   * Replaces a companion this daemon owns whose quiet stream is stuck: `idb disconnect`,
   * SIGTERM (SIGKILL after 3 s), then `idb connect` and the usual start of the streams.
   * Returns the restart in progress, or null when it is not done (not owned, too soon
   * after the last one, stopping, or already restarting).
   */
  #replaceStuckCompanion(when) {
    const companion = this.#companion;
    const evidence = this.#stuckEvidence;
    const silentMs = Math.round(this.#stuckSilenceMs() ?? 0);
    const sinceLast = performance.now() - this.#stuckRestartAt;
    const gap = this.#stuckGap();
    const replace = Boolean(companion?.owned) && sinceLast >= gap
      && !this.#stopping && !this.#restarting && this.state === "serving";
    this.log("quiet_stuck", {
      when, silent_ms: silentMs, signals: evidence?.signals ?? [], owned: companion?.owned ?? null,
      probe_ms: evidence?.probe?.result === "silent" ? evidence.probe.ms : null,
      silent_probes: evidence?.probe?.result === "silent" ? evidence.probe.silentRun : 0,
      gap_ms: gap, streak: this.#stuckStreak,
      replace, ...(replace ? {} : { reason: !companion?.owned ? "not_owned" : sinceLast < gap ? "too_soon" : "busy" }),
    });
    if (!replace) return null;
    this.#stuckRestartAt = performance.now();
    // Cleared by the first sign that the new companion is healthy.
    this.#stuckStreak += 1;
    this.state = "companion_restarting";
    this.#writeRegistry();
    this.#serving = new Promise((resolve) => {
      this.#servingResolve = resolve;
    });
    this.#restarting = (async () => {
      await this.#releaseCompanion();
      if (this.#stopping) return;
      await this.#acquireCompanion();
      if (this.state === "unsupported") return;
      this.#restarts += 1;
      this.state = "serving";
      this.log("companion_restarted", { pid: this.#companion.pid, owned: this.#companion.owned, reason: "quiet_stuck" });
    })().catch((error) => {
      this.log("companion_restart_failed", { message: error.message, reason: "quiet_stuck" });
    }).finally(() => {
      this.#restarting = null;
      const resolve = this.#servingResolve;
      if (this.state !== "serving" && this.state !== "unsupported" && !this.#stopping) {
        // The new companion did not come up: the usual replacement with backoff takes over.
        this.#companionLost("quiet_stuck_restart_failed");
      } else {
        this.#writeRegistry();
      }
      resolve();
    });
    return this.#restarting;
  }

  /** Records quiet events from now on; `until(predicate, ms)` resolves with a match or null. */
  #watch() {
    const seen = [];
    const at = [];
    const waiters = new Set();
    const listener = (event) => {
      seen.push(event);
      at.push(performance.now());
      for (const waiter of waiters) {
        if (waiter.predicate(event)) waiter.done(event);
      }
    };
    this.#watchers.add(listener);
    return {
      seen,
      at,
      until: (predicate, ms) => {
        const hit = seen.find(predicate);
        if (hit) return Promise.resolve(hit);
        if (ms <= 0) return Promise.resolve(null);
        return new Promise((resolve) => {
          const waiter = {
            predicate,
            done: (value) => {
              clearTimeout(waiter.timer);
              waiters.delete(waiter);
              resolve(value);
            },
          };
          waiter.timer = setTimeout(() => waiter.done(null), ms);
          waiters.add(waiter);
        });
      },
      close: () => {
        this.#watchers.delete(listener);
        for (const waiter of waiters) waiter.done(null);
      },
    };
  }

  #startPolling() {
    clearInterval(this.#pollTimer);
    this.#pollTimer = setInterval(() => {
      if (this.state === "serving" && this.#companion && !pidAlive(this.#companion.pid)) this.#companionLost("companion_exited");
    }, COMPANION_POLL_MS);
    this.#pollTimer.unref?.();
  }

  /** The companion is gone: replace it through `idb connect` with backoff. */
  #companionLost(reason) {
    if (this.#stopping || this.#restarting || this.state === "unsupported") return this.#restarting;
    const ranMs = performance.now() - (this.#companion?.startedAt ?? performance.now());
    this.state = "companion_restarting";
    this.log("companion_lost", { reason, pid: this.#companion?.pid ?? null, ran_ms: Math.round(ranMs) });
    this.#writeRegistry();
    this.#serving = new Promise((resolve) => {
      this.#servingResolve = resolve;
    });
    // The Canvas's rule: 0.5 s doubling to 5 s; a companion that ran 10 s resets it.
    if (ranMs >= IDB_STABLE_MS) this.#backoff = IDB_RETRY_MIN_MS;
    this.#restarting = (async () => {
      await this.#dropConnection();
      while (!this.#stopping) {
        const backoff = this.#backoff;
        await sleep(backoff);
        this.#backoff = Math.min(IDB_RETRY_MAX_MS, backoff * 2);
        if (this.#stopping) return;
        try {
          await this.#acquireCompanion();
          if (this.state === "unsupported") break;
          this.#restarts += 1;
          this.state = "serving";
          this.log("companion_restarted", { pid: this.#companion.pid, owned: this.#companion.owned });
          break;
        } catch (error) {
          this.log("companion_restart_failed", { message: error.message, backoff_ms: backoff });
          await this.#dropConnection();
        }
      }
    })().finally(() => {
      this.#restarting = null;
      this.#writeRegistry();
      this.#servingResolve();
    });
    return this.#restarting;
  }

  async #dropConnection() {
    const client = this.#client;
    this.#quietArm?.cancel();
    this.#client = null;
    this.#hid = null;
    this.#primed = false;
    this.#quietStream?.cancel();
    this.#quietStream = null;
    this.quiet.state = null;
    // What a stuck companion showed says nothing of the next connection.
    clearTimeout(this.#stuckTimer);
    this.#stuckTimer = null;
    this.#stuckEvidence = null;
    this.#stuckCleared?.();
    for (const entry of [...this.#touchQueue]) entry.settle(false);
    await client?.close({ timeoutMs: 200 }).catch(() => {});
  }

  /** Stops an owned companion: `idb disconnect`, SIGTERM, SIGKILL after 3 s. */
  async #releaseCompanion() {
    const companion = this.#companion;
    await this.#dropConnection();
    this.#companion = null;
    if (!companion?.owned) {
      if (companion) this.log("companion_left_running", { pid: companion.pid });
      return;
    }
    if (this.#tools.idb) {
      const result = await run(this.#tools.idb, ["disconnect", this.udid], { env: companionEnvironment(this.#env), timeoutMs: IDB_DISCONNECT_TIMEOUT_MS });
      this.log("idb_disconnect", { code: result.code });
    }
    const { pid } = companion;
    if (!pidAlive(pid)) return;
    signalCompanion(pid, "SIGTERM");
    const deadline = performance.now() + COMPANION_STOP_MS;
    while (pidAlive(pid) && performance.now() < deadline) await sleep(50);
    if (pidAlive(pid)) signalCompanion(pid, "SIGKILL");
    this.log("companion_stopped", { pid });
  }

  /** The waiting rule while the companion is replaced: up to `maxMs` (RESTART_WAIT_MS, 6 s), then daemon_unreachable. */
  async #ready(maxMs = RESTART_WAIT_MS) {
    if (this.#unsupported) throw this.#unsupportedError();
    if (this.#stopping) throw new DaemonError(CODE.DAEMON_UNREACHABLE, "The daemon is stopping.");
    if (this.state === "serving" && this.#client) return;
    let timer;
    const timedOut = await Promise.race([
      this.#serving.then(() => false),
      new Promise((resolve) => { timer = setTimeout(() => resolve(true), Math.max(0, maxMs)); }),
    ]).finally(() => clearTimeout(timer));
    if (this.#unsupported) throw this.#unsupportedError();
    if (timedOut || this.state !== "serving" || !this.#client) {
      throw new DaemonError(CODE.DAEMON_UNREACHABLE, `The companion of ${this.udid} is being restarted.`);
    }
  }

  registry() {
    return {
      schema: IOSD_SCHEMA,
      udid: this.udid,
      pid: process.pid,
      socket: this.socket.path,
      state: this.state,
      ...(this.#unsupported ? { reason: CODE.COMPANION_TOO_OLD, companion_version: this.#versions.companion } : {}),
      companion: this.#companion
        ? { pid: this.#companion.pid, socket: this.#companion.endpoint.socketPath ?? null, owned: this.#companion.owned }
        : null,
      versions: { ...this.#versions },
      started_at: this.#startedAt,
      idle_ms: this.idleMs,
      log: this.paths.log,
    };
  }

  #writeRegistry() {
    if (this.#stopping) return;
    try {
      writeJsonAtomic(this.paths.registry, this.registry());
    } catch (error) {
      this.log("registry_write_failed", { message: error.message });
    }
  }

  // --- connections and requests ---------------------------------------------

  #onConnection(socket) {
    this.#connections.set(socket, { inflight: 0, closing: false, noticed: false, attachChain: Promise.resolve() });
    const tooLong = () => new DaemonError(CODE.INVALID_REQUEST, `A request line is longer than ${MAX_LINE_BYTES} bytes.`).toJSON();
    let buffer = Buffer.alloc(0);
    let discarding = false;
    socket.on("data", (chunk) => {
      buffer = buffer.length ? Buffer.concat([buffer, chunk]) : chunk;
      let index;
      while ((index = buffer.indexOf(0x0a)) >= 0) {
        const line = buffer.subarray(0, index);
        buffer = buffer.subarray(index + 1);
        if (discarding) {
          discarding = false;
          continue;
        }
        if (line.length > MAX_LINE_BYTES) {
          this.#reply(socket, { id: null, ok: false, error: tooLong() });
          continue;
        }
        this.#onLine(socket, line.toString("utf8"));
      }
      if (buffer.length > MAX_LINE_BYTES) {
        buffer = Buffer.alloc(0);
        if (!discarding) this.#reply(socket, { id: null, ok: false, error: tooLong() });
        discarding = true;
      }
    });
    socket.on("error", () => {});
    socket.on("close", () => {
      this.#connections.delete(socket);
      if (this.#attachments.delete(socket)) {
        this.log("detach", { reason: "connection_closed", attached: this.#attachedCount() });
        this.#armIdle();
      }
    });
  }

  #attachedCount() {
    let count = 0;
    for (const value of this.#attachments.values()) count += value;
    return count;
  }

  #reply(socket, reply) {
    if (socket.destroyed || !socket.writable) return;
    socket.write(`${JSON.stringify(reply)}\n`);
  }

  #onLine(socket, line) {
    // After the closing line nothing on this connection is run (see #closeConnection).
    if (this.#connections.get(socket)?.noticed) return;
    if (!line.trim()) return;
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      this.#reply(socket, { id: null, ok: false, error: new DaemonError(CODE.INVALID_REQUEST, "The request line is not JSON.").toJSON() });
      return;
    }
    const id = request && typeof request === "object" && !Array.isArray(request) ? request.id ?? null : null;
    if (id === null || (typeof id !== "string" && typeof id !== "number")) {
      this.#reply(socket, { id: null, ok: false, error: new DaemonError(CODE.INVALID_REQUEST, "A request needs a string or number id.").toJSON() });
      return;
    }
    if (!OPS.includes(request.op)) {
      this.#reply(socket, { id, ok: false, error: new DaemonError(CODE.INVALID_REQUEST, `Unknown op ${JSON.stringify(request.op)}; expected one of ${OPS.join(", ")}.`).toJSON() });
      return;
    }
    if (this.#stopping) {
      // Nothing of it reaches the device: the client may safely retry or fall back.
      this.log("request", { op: request.op, ms: 0, ok: false, code: CODE.DAEMON_UNREACHABLE, stage: STAGE.BEFORE_SEND });
      this.#reply(socket, { id, ok: false, error: new DaemonError(CODE.DAEMON_UNREACHABLE, "The daemon is stopping.").toJSON() });
      return;
    }
    this.#handle(socket, id, request);
  }

  /**
   * Ends a connection once it has no request in flight: one closing line, then end().
   * Every request read on it was answered before that line, and none read after it runs.
   */
  #closeConnection(socket, connection) {
    if (connection.noticed) return;
    connection.noticed = true;
    const error = new DaemonError(CODE.DAEMON_UNREACHABLE, "The daemon is stopping; it runs no request sent after this line.");
    this.#reply(socket, { id: null, ok: false, closing: true, error: error.toJSON() });
    socket.end();
    setTimeout(() => socket.destroy(), 200).unref?.();
  }

  async #handle(socket, id, request) {
    const { op } = request;
    const t0 = performance.now();
    const connection = this.#connections.get(socket);
    if (connection) connection.inflight += 1;
    this.#inflight += 1;
    this.#requests += 1;
    this.#byOp[op] = (this.#byOp[op] ?? 0) + 1;
    clearTimeout(this.#idleTimer);
    let reply;
    try {
      const result = await this.#dispatch(socket, op, request);
      reply = { id, ok: true, ...result };
    } catch (error) {
      const failure = error instanceof DaemonError
        ? error
        : new DaemonError(CODE.BACKEND_FAILED, error?.message ?? String(error), { stage: STAGE.AFTER_SEND });
      reply = { id, ok: false, ...(failure.extra?.reply ?? {}), error: { ...failure.toJSON(), reply: undefined } };
    }
    const ms = Math.round(performance.now() - t0);
    this.#timings.push({ op, ms, ok: reply.ok });
    if (this.#timings.length > TIMINGS_KEPT) this.#timings.shift();
    this.log("request", { op, ms, ok: reply.ok, ...(reply.ok ? {} : { code: reply.error.code, stage: reply.error.stage }) });
    this.#inflight -= 1;
    if (connection) connection.inflight -= 1;
    this.#reply(socket, reply);
    if (connection?.closing && connection.inflight === 0) this.#closeConnection(socket, connection);
    if (op === "stop") {
      this.stop("stop_request");
      return;
    }
    this.#armIdle();
  }

  async #dispatch(socket, op, request) {
    // ID-04: an unsupported daemon answers status and stop only, before any validation.
    if (this.#unsupported && op !== "status" && op !== "stop") throw this.#unsupportedError();
    switch (op) {
      case "status":
        return this.#status();
      case "stop":
        return { stopping: true };
      case "attach":
      case "detach":
        return this.#attachStep(socket, op);
      case "companion":
        await this.#ready();
        return this.#companionInfo();
      case "tree":
        await this.#ready();
        return this.#tree(request);
      case "quiet":
        await this.#ready();
        return this.#quietWait(request);
      case "act":
        return this.#act(request);
      default: {
        const input = validateInput(request);
        // The deadline counts from arrival: see DEFAULT_HID_WAIT_MS.
        const deadline = performance.now() + hidWaitLimit(this.#env) + inputDurationMs(input);
        await this.#ready();
        await this.#queueInput(input, { attempted: false, deadline });
        return { sent: true };
      }
    }
  }

  /**
   * attach/detach of one connection run in arrival order: a detach sent after an attach
   * that still waits for the companion must not run first and leave the count at 1.
   */
  #attachStep(socket, op) {
    const step = async () => {
      if (op === "detach") {
        const count = this.#attachments.get(socket) ?? 0;
        if (count <= 1) this.#attachments.delete(socket);
        else this.#attachments.set(socket, count - 1);
        this.log("detach", { attached: this.#attachedCount() });
        return { attached: this.#attachedCount() };
      }
      await this.#ready();
      // A connection that closed while it waited holds nothing: its close already ran.
      if (this.#connections.has(socket)) {
        this.#attachments.set(socket, (this.#attachments.get(socket) ?? 0) + 1);
        clearTimeout(this.#idleTimer);
        this.log("attach", { attached: this.#attachedCount() });
      }
      return { attached: this.#attachedCount(), ...this.#companionInfo() };
    };
    const connection = this.#connections.get(socket);
    if (!connection) return step();
    const result = connection.attachChain.then(step);
    connection.attachChain = result.catch(() => {});
    return result;
  }

  #companionInfo() {
    const companion = this.#companion;
    return { socket: companion?.endpoint.socketPath ?? null, pid: companion?.pid ?? null, owned: companion?.owned ?? null };
  }

  #status() {
    return {
      ...this.registry(),
      status: this.state,
      uptime_ms: Math.round(performance.now() - this.#startedMono),
      requests: this.#requests,
      requests_by_op: { ...this.#byOp },
      timings: [...this.#timings],
      attached: this.#attachedCount(),
      restarts: this.#restarts,
      hid_primed: this.#primed,
      hid_calls: this.#hid?.calls ?? 0,
      hid_idle_closes: this.#hid?.idleCloses ?? 0,
      hid_lost: this.#hid?.lostEvents ?? 0,
      hid_open: this.#hid?.isOpen ?? false,
      app_state: this.quiet.state,
    };
  }

  // --- reads ------------------------------------------------------------------

  async #readOnce(options, counter) {
    const client = this.#client;
    if (!client) throw new DaemonError(CODE.DAEMON_UNREACHABLE, "No companion connection.");
    counter.reads += 1;
    return client.accessibilityInfo(options);
  }

  /** One read with the ID-13 rule: an unlit display is retried once, then reported. */
  async #read(options, counter) {
    try {
      return await this.#readOnce(options, counter);
    } catch (error) {
      if (classifyReadError(error) !== "unlit") throw error;
      await sleep(UNLIT_RETRY_MS);
      try {
        return await this.#readOnce(options, counter);
      } catch (again) {
        if (classifyReadError(again) === "unlit") {
          throw new DaemonError(CODE.DISPLAY_NOT_LIT, "The accessibility read hit a display that is not lit yet.", { stage: STAGE.AFTER_SEND, reads: counter.reads });
        }
        throw again;
      }
    }
  }

  async #tree(request = {}) {
    const choice = request.ax_backend ?? "auto";
    if (!["auto", "ax", "axbridge"].includes(choice)) throw new DaemonError(CODE.INVALID_REQUEST, "ax_backend must be auto, ax or axbridge.");
    const filter = request.filter ?? null;
    if (filter !== null && !["all", "interactable"].includes(filter)) throw new DaemonError(CODE.INVALID_REQUEST, "filter must be all or interactable.");
    const counter = { reads: 0 };
    try {
      if (choice === "axbridge") {
        const json = await this.#read({ backend: "AXBRIDGE", filter: (filter ?? "all").toUpperCase() }, counter);
        return { json, ax_backend: "axbridge", coverage: null, reads: counter.reads };
      }
      let json;
      try {
        json = await this.#read({ backend: "AX", filter: (filter ?? "all").toUpperCase() }, counter);
      } catch (error) {
        if (choice === "auto" && classifyReadError(error) === "no_translation") {
          const bridged = await this.#read({ backend: "AXBRIDGE", filter: "INTERACTABLE" }, counter);
          return { json: bridged, ax_backend: "axbridge", ax_reason: "no_translation_object", coverage: null, reads: counter.reads };
        }
        throw error;
      }
      let elements = null;
      try {
        elements = JSON.parse(json);
      } catch {
        // Not a legacy array: keep the ax read.
      }
      const decision = chooseReadBackend(elements);
      if (choice === "auto" && decision.axbridge) {
        const bridged = await this.#read({ backend: "AXBRIDGE", filter: "INTERACTABLE" }, counter);
        return { json: bridged, ax_backend: "axbridge", ax_reason: decision.reason, coverage: decision.coverage, reads: counter.reads };
      }
      return { json, ax_backend: "ax", coverage: decision.coverage, reads: counter.reads };
    } catch (error) {
      if (error instanceof DaemonError) throw error;
      const stage = unreachable(error) ? STAGE.BEFORE_SEND : STAGE.AFTER_SEND;
      throw new DaemonError(CODE.BACKEND_FAILED, `accessibility_info failed: ${error?.details || error?.message}`, {
        stage, grpc_status: error?.code ?? null, reads: counter.reads,
      });
    }
  }

  /** `ui wait --settled` on the open quiet stream. */
  async #quietWait(request = {}) {
    const timeoutMs = optionalMs(request.timeout_ms, "timeout_ms", OBSERVE_TIMEOUT_MS);
    const quietMs = optionalMs(request.quiet_ms, "quiet_ms", QUIET_WINDOW_MS);
    const t0 = performance.now();
    const watcher = this.#watch();
    try {
      const busy = (state) => state === "busy" || state === "settling";
      let state = this.quiet.state;
      let turned = null;
      if (!busy(state)) {
        // Quiet or unknown: settled unless the app turns busy within the quiet window.
        turned = await watcher.until((e) => e.type === "state_changed" && busy(String(e.state).toLowerCase()), Math.min(quietMs, timeoutMs));
        if (!turned) {
          return { state: state ?? "quiet", busy_signals: [], waited_ms: Math.round(performance.now() - t0), timed_out: false, method: "quiet-stream" };
        }
      }
      const remaining = Math.max(0, timeoutMs - (performance.now() - t0));
      // Only a quiet event after the busy one that started the wait ends it.
      const after = turned ? watcher.seen.indexOf(turned) : -1;
      const quiet = await watcher.until((e) => e.type === "state_changed" && String(e.state).toLowerCase() === "quiet"
        && watcher.seen.indexOf(e) > after, remaining);
      state = quiet ? "quiet" : this.quiet.state ?? "busy";
      return {
        state, busy_signals: quiet ? [] : [...this.quiet.busySignals], waited_ms: Math.round(performance.now() - t0), timed_out: !quiet, method: "quiet-stream",
      };
    } finally {
      watcher.close();
    }
  }

  // --- input ------------------------------------------------------------------

  /**
   * One expected touches_completed. Completions are matched to written touches in order,
   * so one caused by an earlier input (queued by another client, or sent just before)
   * never confirms a later one. The confirmation window opens when the write ends.
   */
  #expectTouch(confirmMs) {
    this.#lastExpired = null;
    const entry = { up: false, settled: false, timer: null };
    entry.promise = new Promise((resolve) => {
      entry.settle = (value) => {
        if (entry.settled) return;
        entry.settled = true;
        clearTimeout(entry.timer);
        const index = this.#touchQueue.indexOf(entry);
        if (index >= 0) this.#touchQueue.splice(index, 1);
        resolve(value);
      };
    });
    entry.openWindow = () => {
      if (entry.settled) return;
      const windowAt = performance.now();
      entry.timer = setTimeout(() => {
        if (!entry.settled) this.#lastExpired = { windowAt, windowMs: confirmMs };
        entry.settle(false);
      }, confirmMs);
    };
    this.#touchQueue.push(entry);
    return entry;
  }

  /**
   * A touch found the stream suspected stuck (see QUIET_STUCK_MS) before anything of it
   * was written: probe the companion until STUCK_SILENT_PROBES silent answers in a row
   * prove it stuck, then replace it and wait for the new one, so the touch is reported.
   * The probes still needed run only when they and a replacement fit in what the input's
   * deadline leaves (its duration and ARM_WAIT_SPARE_MS kept): otherwise the touch goes
   * to the current companion (logged `quiet_suspect`, reason `deadline`). A suspicion
   * nothing could follow (a companion the daemon does not own, a replacement too soon
   * after the last) is not probed; an answer younger than STUCK_PROBE_FRESH_MS is used
   * as it is, and an `ok` one keeps the companion.
   */
  async #checkStuckBeforeTouch(input, deadline) {
    const evidence = this.#stuckEvidence;
    const spare = () => deadline - performance.now() - inputDurationMs(input) - ARM_WAIT_SPARE_MS;
    const owned = Boolean(this.#companion?.owned);
    const gap = this.#stuckGap();
    if (!owned || performance.now() - this.#stuckRestartAt < gap) {
      this.#noteStuckSkip(evidence, "touch", owned ? "too_soon" : "not_owned", owned ? { gap_ms: gap } : {});
      return;
    }
    // An overloaded host: the touch goes to the current companion, unprobed.
    const load = hostLoad(this.#env);
    if (load.overloaded) {
      this.#noteStuckSkip(evidence, "touch", "host_load", load);
      return;
    }
    for (;;) {
      if (this.#stuckEvidence !== evidence) return;
      const now = performance.now();
      const fresh = evidence.probe && now - evidence.probe.at < STUCK_PROBE_FRESH_MS ? evidence.probe : null;
      if (fresh && fresh.result !== "silent") return;
      const silentRun = freshSilentRun(evidence, now);
      if (silentRun >= STUCK_SILENT_PROBES) break;
      if (spare() < (STUCK_SILENT_PROBES - silentRun) * QUIET_STUCK_PROBE_MS + RESTART_WAIT_MS) {
        this.#noteStuckSkip(evidence, "touch", "deadline");
        return;
      }
      // One probe at a time: one the idle check started is shared (it ends within its own limit).
      const outcome = await within(this.#probeStuck("touch", QUIET_STUCK_PROBE_MS), QUIET_STUCK_PROBE_MS + 50);
      if (outcome.timedOut || outcome.value.result !== "silent") return;
    }
    // The load may have risen during the probes: still no replacement then.
    const after = hostLoad(this.#env);
    if (after.overloaded) {
      this.#noteStuckSkip(evidence, "touch", "host_load", after);
      return;
    }
    const restart = this.#replaceStuckCompanion("touch");
    if (restart) await within(restart, spare());
  }

  /**
   * Inputs one at a time in arrival order. `track.attempted` turns true, and
   * `track.onBegin()` runs, the moment this input's HID write begins; `track.confirmation`
   * then resolves true once the app reported this touch, false after `track.confirmMs`.
   */
  #queueInput(input, track = { attempted: false }) {
    const run = this.#inputChain.then(() => this.#sendInput(input, track));
    this.#inputChain = run.catch(() => {});
    return run;
  }

  /**
   * Sends one input and waits for the companion to confirm it, all before
   * `track.deadline` (performance.now() ms; default: the limit from now). At the deadline
   * an input whose write has not begun is refused before_send; one already written has
   * its calls cancelled (`IdbHidStream.cancel()`, so the companion never performs it
   * later) and fails with input_unconfirmed, stage after_send.
   */
  async #sendInput(input, track) {
    const deadline = track.deadline ?? performance.now() + hidWaitLimit(this.#env) + inputDurationMs(input);
    if ((input.op === "tap" || input.op === "swipe") && this.#quietStuck()) await this.#checkStuckBeforeTouch(input, deadline);
    // A companion being replaced is waited for within the deadline only (it holds the
    // stuck check's probe and replacement too): past it the input is refused unsent.
    await this.#ready(Math.min(RESTART_WAIT_MS, deadline - performance.now() - inputDurationMs(input)));
    const hid = this.#hid;
    if (!hid) throw new DaemonError(CODE.DAEMON_UNREACHABLE, "No input stream.");
    if (!this.#primed) {
      try {
        await this.#prime();
      } catch (error) {
        throw new DaemonError(CODE.BACKEND_FAILED, `The input stream could not be opened: ${error.message}`);
      }
    }
    // What went before (priming) is settled first, so a loss there is never blamed on this
    // input; the wait is in order anyway, as this input's call would queue behind it. It
    // too ends at the deadline: what is still unconfirmed then is cancelled.
    const before = hid.flush();
    const earlier = await within(before, deadline - performance.now());
    if (earlier.timedOut) await hid.cancel("the next input's deadline passed");
    await before.catch((error) => this.log("hid_unconfirmed", { events: error?.events ?? null, before: input.op }));
    const isTouch = input.op === "tap" || input.op === "swipe";
    if (isTouch && this.#quietArm && !this.#quietArm.armed && !this.#quietArm.gaveUp) {
      // A quiet stream just (re)opened reports no touch until its first event: wait for it
      // (within the deadline) so this touch can be confirmed; past the limit it goes anyway.
      // At least ARM_WAIT_SPARE_MS of the deadline is left for the input itself.
      const budget = Math.min(QUIET_WARM_MS, deadline - performance.now() - inputDurationMs(input) - ARM_WAIT_SPARE_MS);
      if (budget > 0) {
        const t0 = performance.now();
        const armed = await this.#quietWarm(budget);
        this.log("quiet_arm_wait", { armed, ms: Math.round(performance.now() - t0), op: input.op });
        // Nothing was written yet: a companion lost or a stop during the wait refuses it unsent.
        if (this.#stopping || this.#hid !== hid || !this.#client) {
          throw new DaemonError(CODE.DAEMON_UNREACHABLE, `The ${input.op} was not sent: the companion connection closed while it waited for the quiet stream.`, { stage: STAGE.BEFORE_SEND });
        }
      }
    }
    const left = deadline - performance.now();
    if (left <= inputDurationMs(input)) {
      throw new DaemonError(CODE.BACKEND_FAILED,
        `The ${input.op} was not sent: it waited ${Math.round(hidWaitLimit(this.#env) + inputDurationMs(input) - left)} ms `
        + "for earlier input, the companion or another client's hid call, past its deadline.", { stage: STAGE.BEFORE_SEND });
    }
    let confirmMs = track.confirmMs ?? CONFIRM_MS;
    // The first touch written on this quiet stream, or on the target it last reported
    // (marked once its events are out).
    const firstArm = isTouch && this.#quietArm && !this.#quietArm.touched ? this.#quietArm : null;
    const firstOnTarget = isTouch && this.#targetUntouched;
    if ((firstArm || firstOnTarget) && !track.confirmMsSet) confirmMs = Math.max(confirmMs, FIRST_TOUCH_CONFIRM_MS);
    track.windowMs = confirmMs;
    const touch = isTouch ? this.#expectTouch(confirmMs) : null;
    track.confirmation = touch ? touch.promise : Promise.resolve(true);
    track.attempted = true;
    track.beganAt = performance.now();
    track.onBegin?.();
    const fingerUp = () => {
      if (touch) touch.up = true;
    };
    // Set at the deadline: no further event is sent and a sleep in progress ends.
    const abort = { stopped: false, wakers: new Set() };
    const send = (event) => {
      if (abort.stopped) throw new Error("the input's deadline passed before its next event was sent");
      return event();
    };
    const work = (async () => {
      if (input.op === "tap") {
        await send(() => hid.touchDown(input.x, input.y));
        if (input.duration) await abortableSleep(input.duration * 1000, abort);
        fingerUp();
        await send(() => hid.touchUp(input.x, input.y));
        track.upAt = performance.now();
      } else if (input.op === "swipe") {
        const steps = Math.max(1, Math.min(300, Math.round(input.duration * 60)));
        await send(() => hid.touchDown(input.x1, input.y1));
        for (let i = 1; i < steps; i += 1) {
          await abortableSleep((input.duration * 1000) / steps, abort);
          const share = i / steps;
          await send(() => hid.touchMove(input.x1 + (input.x2 - input.x1) * share, input.y1 + (input.y2 - input.y1) * share));
        }
        await abortableSleep((input.duration * 1000) / steps, abort);
        fingerUp();
        await send(() => hid.touchUp(input.x2, input.y2));
      } else if (input.op === "button") {
        await send(() => hid.pressButton(input.name));
      } else {
        await send(() => hid.keyDown(input.code));
        await send(() => hid.keyUp(input.code));
      }
      track.writtenAt = performance.now();
      if (firstArm) firstArm.touched = true;
      if (firstOnTarget) this.#targetUntouched = false;
      track.onWritten?.();
      // The call ends now, and the reply waits for the companion's status: only then were
      // the events read and performed. While another client holds the companion's hid
      // call this waits (the next input queues behind it, in order, sent once) up to the
      // deadline; events the companion never confirmed make the reply an error, never ok.
      await hid.flush();
    })();
    work.catch(() => {});
    try {
      const outcome = await within(work, deadline - performance.now());
      if (outcome.timedOut) {
        abort.stopped = true;
        for (const wake of [...abort.wakers]) wake();
        await hid.cancel("the input's deadline passed");
        // Rejects unless it settled in the same instant it was given up (confirmed after all).
        await work;
      }
      track.flushedAt = performance.now();
      touch?.openWindow();
    } catch (error) {
      touch?.settle(false);
      if (error instanceof HidUnconfirmedError) {
        // Lost events not counted by the failure that stopped the work (a send refused at
        // the deadline) are collected here, so the count covers this input's events.
        let events = error.events;
        await hid.flush().catch((rest) => {
          if (rest instanceof HidUnconfirmedError) events += rest.events;
        });
        this.log("hid_unconfirmed", { events, op: input.op });
        throw new DaemonError(CODE.INPUT_UNCONFIRMED, `The input was sent but not confirmed: ${error.message}`, {
          stage: STAGE.AFTER_SEND, hint: HID_UNCONFIRMED_HINT, unconfirmed_events: events,
        });
      }
      if (abort.stopped) {
        // A send refused by cancel() at the deadline: what went out before is collected.
        let events = 0;
        await hid.flush().catch((rest) => {
          if (rest instanceof HidUnconfirmedError) events += rest.events;
        });
        this.log("hid_unconfirmed", { events, op: input.op });
        throw new DaemonError(CODE.INPUT_UNCONFIRMED, `The input was sent but not confirmed before its deadline: ${error.message}`, {
          stage: STAGE.AFTER_SEND, hint: HID_UNCONFIRMED_HINT, unconfirmed_events: events,
        });
      }
      throw new DaemonError(CODE.BACKEND_FAILED, `The input failed after it was sent: ${error.message}`, { stage: STAGE.AFTER_SEND });
    }
  }

  async #readCause() {
    const xcrun = this.#tools.xcrun;
    let flag = null;
    if (xcrun) {
      const result = await run(xcrun, ["simctl", "spawn", this.udid, "notifyutil", "-g", DTUHIDD_FLAG], { env: this.#env });
      flag = result.code === 0 ? parseNotifyValue(result.stdout) : null;
    }
    const listing = await run(this.#tools.ps, ["-axo", "comm="], { env: this.#env });
    const deviceHub = listing.code === 0 ? deviceHubIn(listing.stdout) : null;
    this.log("input_cause", { dtuhidd_active: flag, device_hub: deviceHub });
    return inputWarning({ flag, deviceHub });
  }

  /**
   * ID-10: send the input once, wait up to confirm_ms for touches_completed, give the app
   * grace_ms to turn busy (then wait for quiet within timeout_ms), and read once.
   *
   * The confirmation and grace windows start once the input's events are written, while
   * the companion's status on the hid call (the flush) is still on its way; the read, and
   * the reply, wait for that status as well. A touch the app already reported is counted
   * at once, the confirmation window still opens only when the flush ended, and an input
   * the companion did not confirm fails as before (stage after_send), the moment that is
   * known.
   */
  async #act(request) {
    const input = validateInput(request.input);
    const confirmMs = optionalMs(request.confirm_ms, "confirm_ms", CONFIRM_MS);
    const graceMs = optionalMs(request.grace_ms, "grace_ms", GRACE_MS);
    const timeoutMs = optionalMs(request.timeout_ms, "timeout_ms", OBSERVE_TIMEOUT_MS);
    const read = request.read !== false;
    // From arrival: the input's deadline (DEFAULT_HID_WAIT_MS) and the whole reply's.
    const arrived = performance.now();
    const deadline = arrived + hidWaitLimit(this.#env) + inputDurationMs(input);
    const replyBy = arrived + timeoutMs + inputDurationMs(input) + ACT_REPLY_MARGIN_MS;
    await this.#ready();
    const t0 = performance.now();
    // Quiet events count only from the moment this input's own write begins, never
    // while it waits in the queue behind another client's input.
    let watcher = null;
    let timing = null;
    let written;
    const wrote = new Promise((resolve) => { written = resolve; });
    const track = {
      attempted: false, confirmMs, confirmMsSet: request.confirm_ms !== undefined && request.confirm_ms !== null, deadline,
      onBegin: () => { watcher = this.#watch(); }, onWritten: () => written(),
    };
    try {
      const sending = this.#queueInput(input, track);
      // Settles only when the send fails: every wait below ends at once then.
      const failed = sending.then(() => new Promise(() => {}));
      failed.catch(() => {});
      const unlessFailed = (promise) => Promise.race([promise, failed]);
      await Promise.race([wrote, sending]);
      const sentAt = performance.now();
      const touch = input.op === "tap" || input.op === "swipe";
      let confirmed = true;
      let confirmedBy = "hid_write";
      if (touch) {
        confirmed = Boolean(await unlessFailed(track.confirmation));
        confirmedBy = confirmed ? "touches_completed" : null;
      }
      const confirmedAt = performance.now();
      const busy = (e) => e.type === "state_changed" && ["busy", "settling"].includes(String(e.state).toLowerCase());
      const turnedBusy = await unlessFailed(watcher.until(busy, graceMs));
      const graceAt = performance.now();
      let settled = true;
      if (turnedBusy) {
        const remaining = Math.max(0, timeoutMs - (performance.now() - t0));
        const quietAfter = (e) => e.type === "state_changed" && String(e.state).toLowerCase() === "quiet"
          && watcher.seen.indexOf(e) > watcher.seen.indexOf(turnedBusy);
        settled = Boolean(await unlessFailed(watcher.until(quietAfter, remaining)));
      }
      // The companion's status: only an input it confirmed gets a reply that is ok.
      await sending;
      const settledAt = performance.now();
      const warnings = [];
      if (!confirmed) {
        // The cause check runs two tools; past the reply's deadline the warning stays plain.
        const cause = await within(this.#readCause(), replyBy - performance.now());
        warnings.push(cause.timedOut ? inputWarning({ flag: null, deviceHub: null }) : cause.value);
      }
      const rel = (value) => (value === undefined || value === null ? null : Math.round(value - t0));
      timing = {
        op: input.op, began: rel(track.beganAt), up: rel(track.upAt), written: rel(track.writtenAt), flushed: rel(track.flushedAt), sent: rel(sentAt),
        confirmed: rel(confirmedAt), grace: rel(graceAt), settled: rel(settledAt), cause: rel(performance.now()),
        window_ms: track.windowMs ?? null, input_confirmed: confirmed, companion_age_ms: Math.round(t0 - (this.#companion?.startedAt ?? t0)),
        quiet_events: watcher.seen.slice(0, 24).map((e, i) => [rel(watcher.at[i]), e.type === "state_changed" ? String(e.state).toLowerCase() : e.type]),
      };
      const result = {
        input_confirmed: confirmed, confirmed_by: confirmedBy, method: "quiet-stream", settled,
        waited_ms: Math.round(performance.now() - sentAt), warnings,
      };
      if (!read) return result;
      try {
        const tree = this.#tree({});
        tree.catch(() => {});
        const done = await within(tree, replyBy - performance.now());
        timing.read = rel(performance.now());
        if (done.timedOut) {
          throw new DaemonError(CODE.BACKEND_FAILED,
            `The read after the input did not finish within the act's reply limit (${timeoutMs + ACT_REPLY_MARGIN_MS} ms).`);
        }
        return { ...result, ...done.value };
      } catch (error) {
        const failure = error instanceof DaemonError ? error : new DaemonError(CODE.BACKEND_FAILED, error.message);
        failure.stage = STAGE.AFTER_SEND;
        failure.extra = { ...failure.extra, reply: result };
        throw failure;
      }
    } catch (error) {
      if (error instanceof DaemonError && track.attempted) error.stage = STAGE.AFTER_SEND;
      throw error;
    } finally {
      watcher?.close();
      if (timing) this.log("act_timing", timing);
    }
  }

  // --- idle and stop ------------------------------------------------------------

  #armIdle() {
    clearTimeout(this.#idleTimer);
    if (this.#stopping || this.#inflight > 0 || this.#attachedCount() > 0) return;
    this.#idleTimer = setTimeout(() => this.stop("idle"), this.idleMs);
  }

  /** Stops the companion it started, closes everything and removes its files. */
  stop(reason = "stop") {
    this.#stopping ??= (async () => {
      this.#quietArm?.cancel();
      this.log("stopping", { reason });
      clearTimeout(this.#idleTimer);
      clearTimeout(this.#stuckTimer);
      clearInterval(this.#pollTimer);
      this.#server?.close();
      // A connection with nothing in flight closes now; one with a request in flight
      // closes after that reply. Requests arriving meanwhile get a before_send reply.
      for (const [socket, connection] of this.#connections) {
        connection.closing = true;
        if (connection.inflight === 0) this.#closeConnection(socket, connection);
      }
      // A stop during start waits for the step in flight (idb connect, bounded by its
      // timeout), so a companion it just started is known and stopped below.
      await this.#starting?.catch(() => {});
      await this.#restarting?.catch(() => {});
      await this.#releaseCompanion().catch((error) => this.log("release_failed", { message: error.message }));
      await this.#cleanupFiles();
      // A request still in flight here gets no reply: end without the closing line, so
      // its client cannot take it for one that was never run.
      for (const [socket, connection] of this.#connections) {
        if (connection.noticed) continue;
        connection.noticed = true;
        socket.end();
        setTimeout(() => socket.destroy(), 200).unref?.();
      }
      this.log("stopped", { reason });
    })().finally(() => this.#stoppedResolve());
    return this.#stopping;
  }

  async #cleanupFiles() {
    try {
      const registry = readJson(this.paths.registry);
      if (registry?.pid === process.pid) unlinkSync(this.paths.registry);
    } catch {
      // Already gone.
    }
    // Only a socket this process listened on: a refused start leaves another daemon's alone.
    if (this.#listening) {
      try {
        rmSync(this.socket.dir, { recursive: true, force: true });
      } catch {
        // Already gone.
      }
      this.#listening = false;
    }
    if (this.#lockHeld) releaseLock(this.paths.daemonLock);
    this.#lockHeld = false;
    if (this.#onExit) process.removeListener("exit", this.#onExit);
  }

  #installExitHook() {
    // An exit without stop() (a crash) still leaves no files and no owned companion behind.
    this.#onExit = () => {
      const companion = this.#companion;
      if (companion?.owned && pidAlive(companion.pid)) signalCompanion(companion.pid, "SIGKILL");
      try {
        if (readJson(this.paths.registry)?.pid === process.pid) unlinkSync(this.paths.registry);
      } catch {
        // Best effort.
      }
      if (this.#listening) {
        try {
          rmSync(this.socket.dir, { recursive: true, force: true });
        } catch {
          // Best effort.
        }
      }
      if (this.#lockHeld) releaseLock(this.paths.daemonLock);
    };
    process.once("exit", this.#onExit);
  }
}

/** SIGTERM/SIGKILL to the companion's process group (idb starts it as a group leader), else to the pid. */
function signalCompanion(pid, signal) {
  try {
    process.kill(-pid, signal);
    return;
  } catch {
    // Not a group leader.
  }
  try {
    process.kill(pid, signal);
  } catch {
    // Gone.
  }
}

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = () => {
      if (i + 1 >= argv.length) throw new DaemonError(CODE.DAEMON_START_FAILED, `${arg} needs a value.`, { reason: "bad_arguments" });
      i += 1;
      return argv[i];
    };
    if (arg === "--udid") options.udid = value();
    else if (arg === "--state-base") options.stateBase = value();
    else if (arg === "--idle-ms") options.idleMs = value();
    else throw new DaemonError(CODE.DAEMON_START_FAILED, `Unknown argument ${arg}.`, { reason: "bad_arguments" });
  }
  if (!UDID_PATTERN.test(options.udid ?? "")) throw new DaemonError(CODE.DAEMON_START_FAILED, "--udid needs a simulator UDID.", { reason: "bad_arguments" });
  return options;
}

function printLine(value) {
  try {
    process.stdout.write(`${JSON.stringify(value)}\n`);
  } catch {
    // The starter went away.
  }
}

export async function main(argv = process.argv.slice(2)) {
  process.stdout.on("error", () => {});
  let daemon = null;
  try {
    const options = parseArgs(argv);
    daemon = new IosDeviceDaemon(options);
    // Before start(): a signal while idb connect runs must still clean up, not kill the
    // process with files and an owned companion left behind.
    const instance = daemon;
    for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) process.once(signal, () => instance.stop(signal));
    const status = await daemon.start();
    printLine(status);
    if (status.status === "already_running") {
      process.exit(0);
      return;
    }
    let code = 0;
    process.on("uncaughtException", (error) => {
      daemon.log("uncaught", { message: error?.message ?? String(error) });
      code = 1;
      daemon.stop("uncaught");
    });
    await daemon.stopped;
    process.exit(code);
  } catch (error) {
    const failure = error instanceof DaemonError
      ? error
      : new DaemonError(CODE.DAEMON_START_FAILED, error?.message ?? String(error), { reason: "start_error" });
    daemon?.log("start_failed", { code: failure.code, reason: failure.extra?.reason ?? null });
    if (daemon) await daemon.stop("start_failed").catch(() => {});
    printLine({ schema: IOSD_SCHEMA, status: "failed", error: { ...failure.toJSON(), reason: failure.extra?.reason ?? null } });
    process.exit(2);
  }
}

if (process.argv[1] && resolvePath(process.argv[1]) === DAEMON_SCRIPT) {
  main();
}
