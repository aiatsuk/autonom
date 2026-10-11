/**
 * Device relay of the probe media endpoint (contract section 4, DEC-011).
 *
 * createDeviceSource({spec, adbPath, scriptsDir, factories}) returns one EventEmitter source for an
 * explicit, allowlisted test device:
 * - `android:<serial>` streams through the Canvas ScrcpySession and taps with two scrcpy touch
 *   messages (DOWN then UP);
 * - `ios:<udid>` streams through the Canvas IdbCompanion video stream and taps through its HID stream.
 *
 * Interface (contract 3.6): start(), stop(), tap({x, y}) → {ok, error_code?, error?};
 * events config({codec, width, height, annexB}), frame({key, data, tMs}), error(Error), state(name).
 *
 * The relay never starts an Autonom session, never reads or writes the session store and never
 * retries input. `android:emulator-5554` is refused even when allowlisted. The Canvas modules are
 * imported read-only from `scriptsDir` at run time; `factories` replaces ScrcpySession,
 * IdbCompanion, resolveScrcpyServer and resolveIdbCompanionBinary in tests.
 */

import { EventEmitter } from "node:events";
import { access, constants } from "node:fs/promises";
import { delimiter, join } from "node:path";
import { pathToFileURL } from "node:url";

import { FORBIDDEN_DEVICE, ProbeError, parseDeviceSpec } from "./media-lib.mjs";

export const ANDROID_MAX_SIZE = 1280;
export const ANDROID_BIT_RATE = 8_000_000;
export const ANDROID_MAX_FPS = 30;
export const IOS_FPS = 30;
export const DESCRIBE_TIMEOUT_MS = 3000;

const SCRCPY_SESSION_MODULE = "scrcpy-session.mjs";
const SCRCPY_LIB_MODULE = "scrcpy-lib.mjs";
const IDB_MODULE = "ios-idb-companion.mjs";

/** One relay refusal or failure, with the text the WebSocket error message carries. */
function relayError(code, message, extras = {}) {
  return new ProbeError(code, message, extras);
}

function tapRefusal(code, error) {
  return { ok: false, error_code: code, error };
}

/** `name` on PATH, the way the Canvas finds `scrcpy` (android-emulator-browser.mjs findExecutable). */
async function findExecutable(name) {
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    const candidate = join(directory, name);
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Not here; try the next directory.
    }
  }
  throw new Error(`${name} not found on PATH`);
}

function validFraction(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

/** Scale a 0-1 fraction to a pixel or point inside `[0, size - 1]`. */
function scale(fraction, size) {
  return Math.min(Math.max(Math.round(fraction * size), 0), Math.max(size - 1, 0));
}

/**
 * Create the source for one device spec. Throws `bad_source` for a malformed spec,
 * `device_not_allowed` for `android:emulator-5554` and `relay_unavailable` without a scripts dir.
 */
export function createDeviceSource({ spec, adbPath = null, scriptsDir, factories = {} } = {}) {
  const parsed = parseDeviceSpec(spec);
  if (!parsed) {
    throw relayError("bad_source", "The device spec must be android:<serial> or ios:<udid>.");
  }
  if (parsed.id === FORBIDDEN_DEVICE) {
    throw relayError("device_not_allowed", "android:emulator-5554 is never relayed by the probe.");
  }
  if (typeof scriptsDir !== "string" || !scriptsDir) {
    throw relayError("relay_unavailable", "The relay needs the Canvas scripts directory.");
  }
  const modules = new Map();
  const load = async (name) => {
    if (!modules.has(name)) modules.set(name, import(pathToFileURL(join(scriptsDir, name)).href));
    try {
      return await modules.get(name);
    } catch (error) {
      modules.delete(name);
      throw relayError("relay_unavailable", `The Canvas module ${name} could not be loaded: ${error.message}`);
    }
  };
  const options = { spec: parsed.id, adbPath, factories, load };
  return parsed.kind === "android"
    ? new AndroidDeviceSource({ ...options, serial: parsed.serial })
    : new IosDeviceSource({ ...options, udid: parsed.udid });
}

class RelaySource extends EventEmitter {
  spec;
  state = "idle";
  #startPromise = null;
  #stopPromise = null;
  stopped = false;

  constructor(spec) {
    super();
    this.spec = spec;
  }

  setState(name) {
    if (this.state === name) return;
    this.state = name;
    this.emit("state", name);
  }

  /** Report a stream failure; never throws when nobody listens. */
  fail(code, error) {
    if (this.stopped) return;
    const failure = error instanceof ProbeError
      ? error
      : relayError(code, String(error?.message ?? error ?? "The device stream failed.").slice(0, 300));
    if (this.listenerCount("error")) this.emit("error", failure);
  }

  start() {
    if (this.stopped) return Promise.reject(relayError("source_failed", "The device source is stopped."));
    this.#startPromise ??= (async () => {
      this.setState("starting");
      try {
        await this.open();
      } catch (error) {
        await this.close().catch(() => {});
        if (!this.stopped) this.setState("failed");
        throw error instanceof ProbeError
          ? error
          : relayError("device_start_failed", String(error?.message ?? error).slice(0, 300));
      }
      if (this.stopped) {
        await this.close().catch(() => {});
        throw relayError("source_failed", "The device source was stopped while it started.");
      }
      this.setState("streaming");
    })();
    return this.#startPromise;
  }

  stop() {
    this.#stopPromise ??= (async () => {
      this.stopped = true;
      await this.#startPromise?.catch(() => {});
      await this.close();
      this.setState("stopped");
    })();
    return this.#stopPromise;
  }
}

class AndroidDeviceSource extends RelaySource {
  #serial;
  #adbPath;
  #factories;
  #load;
  #lib = null;
  #session = null;
  #size = null;

  constructor({ spec, serial, adbPath, factories, load }) {
    super(spec);
    this.#serial = serial;
    this.#adbPath = adbPath;
    this.#factories = factories;
    this.#load = load;
  }

  async open() {
    this.#lib = await this.#load(SCRCPY_LIB_MODULE);
    const needSession = !this.#factories.ScrcpySession || !this.#factories.resolveScrcpyServer;
    const sessionModule = needSession ? await this.#load(SCRCPY_SESSION_MODULE) : {};
    const resolveScrcpyServer = this.#factories.resolveScrcpyServer ?? sessionModule.resolveScrcpyServer;
    const ScrcpySession = this.#factories.ScrcpySession ?? sessionModule.ScrcpySession;
    const found = await resolveScrcpyServer({ env: process.env, findExecutable });
    if (!found?.ok) {
      throw relayError("scrcpy_unavailable", String(found?.reason ?? "No usable scrcpy-server was found.").slice(0, 300), {
        hint: "Set AUTONOM_SCRCPY_SERVER to a scrcpy-server 4.1 file or install scrcpy 4.1.",
      });
    }
    if (this.stopped) return;
    const session = new ScrcpySession({
      adbPath: this.#adbPath ?? "adb",
      serial: this.#serial,
      serverPath: found.serverPath,
      version: found.version,
      maxSize: ANDROID_MAX_SIZE,
      bitRate: ANDROID_BIT_RATE,
      maxFps: ANDROID_MAX_FPS,
    });
    this.#session = session;
    session.on("session", ({ width, height } = {}) => {
      if (Number.isInteger(width) && Number.isInteger(height) && width > 0 && height > 0) this.#size = { width, height };
    });
    session.on("config", (buffer) => {
      const annexB = Buffer.from(buffer);
      let codec = null;
      try {
        codec = this.#lib.h264CodecString(annexB);
      } catch {
        codec = null;
      }
      this.emit("config", { codec, width: this.#size?.width ?? null, height: this.#size?.height ?? null, annexB });
    });
    session.on("packet", ({ keyFrame, data } = {}) => {
      if (!data) return;
      this.emit("frame", { key: Boolean(keyFrame), data, tMs: Date.now() });
    });
    session.on("error", (error) => this.fail("device_stream_failed", error));
    session.on("state", (name) => {
      if (name === "restarting") this.setState("restarting");
      else if (name === "streaming" && this.state === "restarting") this.setState("streaming");
    });
    await session.start();
  }

  async close() {
    const session = this.#session;
    this.#session = null;
    if (session) await session.stop();
  }

  async tap({ x, y } = {}) {
    if (!validFraction(x) || !validFraction(y)) return tapRefusal("bad_message", "x and y must be numbers from 0 to 1.");
    const session = this.#session;
    const size = this.#size;
    if (this.stopped || !session || !this.#lib) return tapRefusal("device_not_ready", "The device stream is not running.");
    if (!size) return tapRefusal("device_not_ready", "The device screen size is not known yet.");
    const { encodeTouch, MOTION_ACTION, POINTER_ID } = this.#lib;
    const message = (action, pressure) => encodeTouch({
      action,
      pointerId: POINTER_ID.GENERIC_FINGER,
      x: scale(x, size.width),
      y: scale(y, size.height),
      width: size.width,
      height: size.height,
      pressure,
    });
    // One DOWN and one UP; a refused send is reported, never repeated.
    if (!session.send(message(MOTION_ACTION.DOWN, 1))) {
      return tapRefusal("device_not_connected", "The scrcpy control socket is not connected.");
    }
    if (!session.send(message(MOTION_ACTION.UP, 0))) {
      return tapRefusal("device_not_connected", "The scrcpy control socket closed before the touch ended.");
    }
    return { ok: true };
  }
}

class IosDeviceSource extends RelaySource {
  #udid;
  #factories;
  #load;
  #codecString = null;
  #companion = null;
  #client = null;
  #stream = null;
  #reader = null;
  #lastConfig = null;
  #points = null;

  constructor({ spec, udid, factories, load }) {
    super(spec);
    this.#udid = udid;
    this.#factories = factories;
    this.#load = load;
  }

  async open() {
    this.#codecString = (await this.#load(SCRCPY_LIB_MODULE)).h264CodecString;
    const idb = await this.#load(IDB_MODULE);
    const resolveIdbCompanionBinary = this.#factories.resolveIdbCompanionBinary ?? idb.resolveIdbCompanionBinary;
    const IdbCompanion = this.#factories.IdbCompanion ?? idb.IdbCompanion;
    const found = await resolveIdbCompanionBinary({ env: process.env });
    if (!found?.available) {
      throw relayError("idb_companion_unavailable", String(found?.reason ?? "idb_companion was not found.").slice(0, 300), {
        hint: "Install idb_companion or set AUTONOM_IDB_COMPANION_BIN.",
      });
    }
    if (this.stopped) return;
    const companion = new IdbCompanion({ udid: this.#udid, binary: found.path });
    this.#companion = companion;
    companion.on("exit", ({ expected } = {}) => {
      if (!expected) this.fail("idb_companion_exited", new Error("idb_companion exited."));
    });
    const client = await companion.start();
    this.#client = client;
    if (this.stopped) return;
    const stream = await client.openVideoStream({ ...idb.DEFAULT_VIDEO_OPTIONS, fps: IOS_FPS });
    this.#stream = stream;
    if (this.stopped) return;
    this.#reader = this.#read(stream);
  }

  async #read(stream) {
    try {
      for await (const frame of stream) {
        if (this.stopped) break;
        this.#onFrame(frame);
      }
      if (!this.stopped) this.fail("device_stream_ended", new Error("The iOS video stream ended."));
    } catch (error) {
      this.fail("device_stream_failed", error);
    }
  }

  #onFrame(frame) {
    if (!frame?.data) return;
    if (frame.config && (!this.#lastConfig || !Buffer.from(frame.config).equals(this.#lastConfig))) {
      this.#lastConfig = Buffer.from(frame.config);
      let codec = typeof frame.codec === "string" && frame.codec ? frame.codec : null;
      if (!codec) {
        try {
          codec = this.#codecString(this.#lastConfig);
        } catch {
          codec = null;
        }
      }
      this.emit("config", { codec, width: frame.width ?? null, height: frame.height ?? null, annexB: this.#lastConfig });
    }
    this.emit("frame", { key: Boolean(frame.key), data: frame.data, tMs: Date.now() });
  }

  async close() {
    const stream = this.#stream;
    const companion = this.#companion;
    this.#stream = null;
    this.#client = null;
    this.#companion = null;
    if (stream) await stream.stop().catch(() => {});
    await this.#reader?.catch(() => {});
    if (companion) await companion.stop();
  }

  async #screenPoints(client) {
    if (this.#points) return this.#points;
    const { screen } = (await client.describe({ timeoutMs: DESCRIBE_TIMEOUT_MS })) ?? {};
    if (screen?.widthPoints && screen?.heightPoints) {
      this.#points = { width: screen.widthPoints, height: screen.heightPoints };
    } else if (screen?.width && screen?.height && screen?.density) {
      this.#points = { width: Math.round(screen.width / screen.density), height: Math.round(screen.height / screen.density) };
    }
    return this.#points;
  }

  async tap({ x, y } = {}) {
    if (!validFraction(x) || !validFraction(y)) return tapRefusal("bad_message", "x and y must be numbers from 0 to 1.");
    const client = this.#client;
    if (this.stopped || !client) return tapRefusal("device_not_ready", "The device stream is not running.");
    let points;
    try {
      points = await this.#screenPoints(client);
    } catch (error) {
      return tapRefusal("device_describe_failed", `idb describe failed: ${String(error?.message ?? error).slice(0, 200)}`);
    }
    if (!points) return tapRefusal("device_describe_failed", "idb describe gave no screen size.");
    try {
      // One tap (touch down and up); a failed tap is reported, never repeated.
      await client.hid().tap(scale(x, points.width), scale(y, points.height));
    } catch (error) {
      return tapRefusal("tap_failed", `idb tap failed: ${String(error?.message ?? error).slice(0, 200)}`);
    }
    return { ok: true };
  }
}
