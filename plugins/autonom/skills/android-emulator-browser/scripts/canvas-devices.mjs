// The devices a Canvas shows: their ids and URL paths, the stream profile of a focused or
// background device, the registry of attached devices with their states, and the focus
// controller that plans profile restarts. Pure apart from the callbacks it is given, so
// every rule here is unit-tested without a device.

export const MAX_DEVICES = 8;
export const DEVICE_ID_PATTERN = /^(android|ios)~[A-Za-z0-9._:-]{1,128}$/;
export const PROFILE = Object.freeze({
  BACKGROUND_FPS: 30,
  BACKGROUND_ANDROID_MAX_SIZE: 1024,
  BACKGROUND_IOS_SCALE: 0.5,
  DEMOTE_AFTER_MS: 2000,
  RESTART_MIN_INTERVAL_MS: 3000,
});
// How long a planned profile restart waits while a pointer or key is down.
export const RESTART_INPUT_WAIT_MS = 1000;
const RESTART_INPUT_POLL_MS = 100;
// Attaches that may run their create step at once.
export const ATTACH_CONCURRENCY = 2;
export const DEVICE_STATES = Object.freeze(["attaching", "live", "offline", "failed", "detaching"]);
const FOCUSED_FPS = 60;

/** A refusal of the Canvas API: an HTTP status, a stable code, a hint and extra fields. */
export class CanvasError extends Error {
  constructor(status, code, message, hint = null, extra = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
    this.extra = extra;
  }

  /** The API error body: {ok: false, error, error_code, hint, ...extra}. */
  body() {
    return { ok: false, error: this.message, error_code: this.code, hint: this.hint, ...this.extra };
  }
}

/** `<platform>~<target>`; refuses a platform or target the id rule does not allow. */
export function deviceIdFor(platform, target) {
  const id = `${platform}~${target}`;
  if (typeof platform !== "string" || typeof target !== "string" || !DEVICE_ID_PATTERN.test(id)) {
    throw new CanvasError(400, "invalid_value",
      "platform must be android or ios and target 1-128 characters of letters, digits, . _ : -");
  }
  return id;
}

/** {platform, target} of a valid device id, else null. */
export function parseDeviceId(id) {
  if (typeof id !== "string" || !DEVICE_ID_PATTERN.test(id)) return null;
  const at = id.indexOf("~");
  return { platform: id.slice(0, at), target: id.slice(at + 1) };
}

export function devicePath(id) {
  return `/d/${encodeURIComponent(id)}`;
}

/**
 * "/d/<enc>/<rest>" -> {id, rest: "/<rest>"}; "/d/<enc>" -> {id, rest: null}; anything else
 * (or a segment that does not decode) -> null. The id itself is not validated here.
 */
export function splitDevicePath(pathname) {
  if (typeof pathname !== "string" || !pathname.startsWith("/d/")) return null;
  const after = pathname.slice(3);
  const slash = after.indexOf("/");
  const encoded = slash < 0 ? after : after.slice(0, slash);
  if (!encoded) return null;
  let id;
  try {
    id = decodeURIComponent(encoded);
  } catch {
    return null;
  }
  return { id, rest: slash < 0 ? null : after.slice(slash) };
}

/**
 * The stream settings of a device. Focused: the configured frame rate (60 unless --fps),
 * the automatic size, full scale. Background: at most 30 fps, Android at most 1024 px on
 * the long side (or the explicit --max-size when smaller; 0 = native counts as unbounded),
 * iOS at half scale.
 */
export function profileVideo({ platform, focused, options = {} }) {
  const focusedFps = options.fpsExplicit ? options.fps : FOCUSED_FPS;
  if (focused) {
    return { profile: "focused", fps: focusedFps, maxSize: undefined, scaleFactor: 1 };
  }
  const fps = Math.min(PROFILE.BACKGROUND_FPS, focusedFps);
  if (platform === "ios") {
    return { profile: "background", fps, maxSize: undefined, scaleFactor: PROFILE.BACKGROUND_IOS_SCALE };
  }
  const auto = options.maxSizeExplicit ? options.maxSize : undefined;
  const effective = auto === undefined || auto === 0 ? Infinity : auto;
  return {
    profile: "background", fps, maxSize: Math.min(effective, PROFILE.BACKGROUND_ANDROID_MAX_SIZE), scaleFactor: 1,
  };
}

/**
 * The attached devices of one Canvas in attach order. `create` builds a device context and
 * `close` ends one; at most `max` devices and ATTACH_CONCURRENCY creates at once.
 */
export class DeviceRegistry {
  #entries = new Map();
  #pending = new Map();
  #max;
  #create;
  #close;
  #onChange;
  #now;
  #running = 0;
  #waiting = [];

  constructor({ max = MAX_DEVICES, create, close, onChange = () => {}, now = () => performance.now() }) {
    this.#max = max;
    this.#create = create;
    this.#close = close;
    this.#onChange = onChange;
    this.#now = now;
  }

  get size() {
    return this.#entries.size;
  }

  get max() {
    return this.#max;
  }

  get(id) {
    return this.#entries.get(id) ?? null;
  }

  list() {
    return [...this.#entries.values()];
  }

  /** The first device in attach order: the target of the root aliases. */
  primary() {
    return this.#entries.values().next().value ?? null;
  }

  /** Attach a device; one already present (or attaching) is returned with attached false. */
  attach({ platform, target, name = null, bootedByCanvas = false }) {
    const id = deviceIdFor(platform, target);
    const pending = this.#pending.get(id);
    if (pending) return pending.then(({ entry }) => ({ entry, attached: false }));
    const existing = this.#entries.get(id);
    if (existing) return Promise.resolve({ entry: existing, attached: false });
    if (this.#entries.size >= this.#max) {
      return Promise.reject(new CanvasError(409, "device_limit",
        `A Canvas shows at most ${this.#max} devices`, "Detach a device first."));
    }
    const entry = {
      id, platform, target, name: name ?? target, state: "attaching", context: null,
      attachedAt: new Date().toISOString(), attachedMs: this.#now(), bootedByCanvas: Boolean(bootedByCanvas),
      error: null, failures: [], session: null,
    };
    this.#entries.set(id, entry);
    this.#changed();
    const run = this.#limited(async () => {
      try {
        const context = await this.#create({ id, platform, target, name: entry.name });
        if (this.#entries.get(id) !== entry || entry.state === "detaching") {
          // Detached while it was being created: end what was made.
          await this.#close(context, "detach");
          throw new CanvasError(409, "device_detached", `Device ${id} was detached while it attached`);
        }
        entry.context = context;
        entry.state = "live";
        this.#changed();
        return { entry, attached: true };
      } catch (error) {
        if (this.#entries.get(id) === entry) {
          this.#entries.delete(id);
          this.#changed();
        }
        if (error instanceof CanvasError) throw error;
        throw new CanvasError(502, "backend_failed", `Could not attach ${id}: ${error?.message ?? error}`);
      }
    });
    const shared = run.finally(() => this.#pending.delete(id));
    this.#pending.set(id, shared);
    return shared;
  }

  /** Detach a device: its state becomes detaching, `close` runs, then it is removed. */
  async detach(id, { reason = "detach" } = {}) {
    const entry = this.#entries.get(id);
    if (!entry) throw new CanvasError(404, "device_not_found", `No device ${id} is attached`);
    if (entry.detaching) return entry.detaching;
    entry.state = "detaching";
    this.#changed();
    entry.detaching = (async () => {
      try {
        if (entry.context) await this.#close(entry.context, reason);
      } finally {
        if (this.#entries.get(id) === entry) this.#entries.delete(id);
        this.#changed();
      }
    })();
    return entry.detaching;
  }

  setState(id, state, { error = null } = {}) {
    const entry = this.#entries.get(id);
    if (!entry) return null;
    if (!DEVICE_STATES.includes(state)) throw new RangeError(`Unknown device state ${state}`);
    if (entry.state === "detaching") return entry;
    entry.state = state;
    entry.error = error;
    this.#changed();
    return entry;
  }

  /** Swap the context of a restored device (crash isolation) without changing its place. */
  replaceContext(id, context) {
    const entry = this.#entries.get(id);
    if (!entry) return null;
    entry.context = context;
    this.#changed();
    return entry;
  }

  #changed() {
    try {
      this.#onChange(this);
    } catch {}
  }

  #limited(task) {
    return new Promise((resolvePromise, reject) => {
      const start = () => {
        this.#running += 1;
        Promise.resolve().then(task).then(resolvePromise, reject).finally(() => {
          this.#running -= 1;
          this.#waiting.shift()?.();
        });
      };
      if (this.#running < ATTACH_CONCURRENCY) start();
      else this.#waiting.push(start);
    });
  }
}

/**
 * Which device is focused and which stream profile each device should run. Promotion is
 * planned at once, demotion of the previous device after DEMOTE_AFTER_MS; each device
 * restarts at most once per RESTART_MIN_INTERVAL_MS (the latest wish wins) and waits up to
 * RESTART_INPUT_WAIT_MS while `busy(id)` says a pointer or key is down. `apply(id, profile)`
 * performs a restart. With no device focused every device runs the focused profile.
 * `appliedOf(id)` names the profile the stream really runs: it changes only once `apply`
 * finished without throwing, rejecting or answering `false` (a failed restart keeps the
 * previous applied profile), and a restart overtaken by a newer one is not recorded.
 */
export class FocusController {
  #focus = null;
  #wanted = new Map();
  #applied = new Map();
  #confirmed = new Map();
  #applySeq = new Map();
  #lastRestart = new Map();
  #timers = new Map();
  #demoteTimers = new Map();
  #apply;
  #busy;
  #now;
  #setTimer;
  #clearTimer;
  #demoteAfterMs;
  #minIntervalMs;

  constructor({
    apply, busy = () => false, now = () => performance.now(),
    setTimer = (fn, ms) => { const timer = setTimeout(fn, ms); timer.unref?.(); return timer; },
    clearTimer = (timer) => clearTimeout(timer),
    demoteAfterMs = PROFILE.DEMOTE_AFTER_MS, minIntervalMs = PROFILE.RESTART_MIN_INTERVAL_MS,
  }) {
    this.#apply = apply;
    this.#busy = busy;
    this.#now = now;
    this.#setTimer = setTimer;
    this.#clearTimer = clearTimer;
    this.#demoteAfterMs = demoteAfterMs;
    this.#minIntervalMs = minIntervalMs;
  }

  get focus() {
    return this.#focus;
  }

  /** The profile a device runs now: "focused" or "background". */
  profileOf(id) {
    return this.#wanted.get(id) ?? (this.#focus === null || this.#focus === id ? "focused" : "background");
  }

  /**
   * The profile the device's stream runs, confirmed by a finished `apply` (or the one it was
   * added with): `{profile, at, applied_at}` with `at` on the `now` clock and `applied_at` an
   * ISO time; null for an unknown device.
   */
  appliedOf(id) {
    return this.#confirmed.get(id) ?? null;
  }

  /** A device joined: it streams focused when it is the focus or nothing is focused. */
  add(id) {
    const profile = this.#focus === null || this.#focus === id ? "focused" : "background";
    this.#wanted.set(id, profile);
    this.#applied.set(id, profile);
    this.#confirm(id, profile);
    return profile;
  }

  #confirm(id, profile) {
    this.#confirmed.set(id, { profile, at: this.#now(), applied_at: new Date().toISOString() });
  }

  /** A device left: its timers end. A focused device that leaves clears the focus. */
  forget(id) {
    this.#clearTimer(this.#timers.get(id));
    this.#clearTimer(this.#demoteTimers.get(id));
    this.#timers.delete(id);
    this.#demoteTimers.delete(id);
    this.#wanted.delete(id);
    this.#applied.delete(id);
    this.#confirmed.delete(id);
    this.#applySeq.delete(id);
    this.#lastRestart.delete(id);
    if (this.#focus === id) this.#focus = null;
  }

  /** Focus `id` (or nobody with null) among `ids`, the devices attached now. */
  setFocus(id, ids) {
    const previous = this.#focus;
    this.#focus = id;
    for (const other of ids) {
      if (other === id) {
        this.#clearTimer(this.#demoteTimers.get(other));
        this.#demoteTimers.delete(other);
        this.#want(other, "focused");
      } else if (id === null) {
        this.#want(other, "focused");
      } else if (this.profileOf(other) === "focused" && !this.#demoteTimers.has(other)) {
        // The previous focus keeps its profile a little longer: a quick switch back costs nothing.
        const delay = other === previous ? this.#demoteAfterMs : 0;
        if (delay === 0) {
          this.#want(other, "background");
          continue;
        }
        this.#demoteTimers.set(other, this.#setTimer(() => {
          this.#demoteTimers.delete(other);
          if (this.#focus !== other && this.#focus !== null) this.#want(other, "background");
        }, delay));
      }
    }
    return this.#focus;
  }

  #want(id, profile) {
    this.#wanted.set(id, profile);
    this.#schedule(id);
  }

  #schedule(id, waitedMs = 0) {
    this.#clearTimer(this.#timers.get(id));
    this.#timers.delete(id);
    const wanted = this.#wanted.get(id);
    if (wanted === undefined || this.#applied.get(id) === wanted) return;
    const now = this.#now();
    const earliest = (this.#lastRestart.get(id) ?? -Infinity) + this.#minIntervalMs;
    if (now < earliest) {
      this.#timers.set(id, this.#setTimer(() => this.#schedule(id), earliest - now));
      return;
    }
    if (waitedMs < RESTART_INPUT_WAIT_MS && this.#busy(id)) {
      this.#timers.set(id, this.#setTimer(() => this.#schedule(id, waitedMs + RESTART_INPUT_POLL_MS),
        RESTART_INPUT_POLL_MS));
      return;
    }
    this.#lastRestart.set(id, now);
    this.#applied.set(id, wanted);
    const seq = (this.#applySeq.get(id) ?? 0) + 1;
    this.#applySeq.set(id, seq);
    // Only the latest restart of a device that is still attached may confirm its profile.
    const settle = (result) => {
      if (result !== false && this.#applySeq.get(id) === seq) this.#confirm(id, wanted);
    };
    try {
      const done = this.#apply(id, wanted);
      if (typeof done?.then === "function") done.then(settle, () => {});
      else settle(done);
    } catch {}
  }
}
