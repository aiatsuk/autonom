/**
 * One scrcpy-server v4.1 on an Android target, driven over an adb forward tunnel.
 * Pushes the server, starts it through `adb shell app_process`, reads the video
 * socket (device meta, codec id, 12-byte packet headers) and owns the control
 * socket. Restarts with backoff after an unexpected exit; a stopped session never
 * restarts. Also resolves which server file the Canvas may use.
 */
import { execFile as nodeExecFile, spawn as nodeSpawn } from "node:child_process";
import { randomInt } from "node:crypto";
import { EventEmitter } from "node:events";
import { realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { connect as nodeConnect } from "node:net";
import { basename, dirname, join, resolve } from "node:path";
import { promisify } from "node:util";

import {
  CONTROL_MESSAGE_TYPE,
  DEVICE_NAME_FIELD_LENGTH,
  DeviceMessageParser,
  SCRCPY_PROTOCOL_VERSION,
  VIDEO_CODEC_ID_H264,
  VIDEO_HEADER_LENGTH,
  encodeEmpty,
  parseScrcpyVersion,
  parseVideoHeader,
} from "./scrcpy-lib.mjs";

export const SCRCPY_CAPABILITY = "canvas.scrcpy";
export const SCRCPY_INSTALL_HINT =
  `Install scrcpy ${SCRCPY_PROTOCOL_VERSION}: brew install scrcpy (macOS) or sudo apt-get install scrcpy ` +
  `(Linux); or pass --scrcpy-server PATH, or point AUTONOM_SCRCPY_SERVER at a ` +
  `scrcpy-server-v${SCRCPY_PROTOCOL_VERSION} file. Autonom never downloads it.`;
export const REMOTE_SERVER_PATH = `/data/local/tmp/autonom-scrcpy-${SCRCPY_PROTOCOL_VERSION}.jar`;
export const SERVER_CLASS = "com.genymobile.scrcpy.Server";

export const CONNECT_TIMEOUT_MS = 10_000;
export const RESTART_MIN_MS = 1000;
export const RESTART_MAX_MS = 10_000;
export const HEALTHY_STREAM_MS = 30_000;
export const KEY_FRAME_REQUEST_INTERVAL_MS = 1000;

const CONNECT_RETRY_MS = 100;
const CONNECT_ATTEMPT_MS = 2000;
// Before killing it, give the adb shell child time to exit by itself once its sockets close.
const CHILD_EXIT_GRACE_MS = 1000;
const ADB_COMMAND_TIMEOUT_MS = 2000;
const PUSH_TIMEOUT_MS = 30_000;
// A media packet larger than this is a broken stream, not a frame.
const MAX_PACKET_BYTES = 64 * 1024 * 1024;
const LOG_TAIL_CHARS = 4000;
const SERVER_VARIABLES = ["AUTONOM_SCRCPY_SERVER", "SCRCPY_SERVER_PATH"];
const EMPTY = Buffer.alloc(0);

const execFileAsync = promisify(nodeExecFile);

function defaultExec(file, args, options) {
  return execFileAsync(file, args, { encoding: "utf8", maxBuffer: 4 * 1024 * 1024, ...options });
}

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

function expandHome(path) {
  return path === "~" || path.startsWith("~/") ? join(homedir(), path.slice(1)) : path;
}

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

/**
 * Which scrcpy-server the Canvas may push, in DEC-002 order: --scrcpy-server (with
 * --scrcpy-version when the file name does not say), AUTONOM_SCRCPY_SERVER,
 * SCRCPY_SERVER_PATH, then the server an installed `scrcpy` ships beside itself.
 * The first source that is set decides; a broken one is reported, not skipped.
 * Returns {ok, serverPath, version, source} or {ok: false, reason, source}.
 */
export async function resolveScrcpyServer({
  serverPath,
  version,
  env = process.env,
  findExecutable,
  exec = defaultExec,
}) {
  let source;
  let path;
  let found;
  if (serverPath) {
    source = "--scrcpy-server";
    path = resolve(expandHome(serverPath));
    found = version ?? parseScrcpyVersion(basename(path));
  } else {
    const variable = SERVER_VARIABLES.find((name) => env[name]);
    if (variable) {
      source = variable;
      path = resolve(expandHome(env[variable]));
      // A file named by a variable may come from any release, so only its name says which.
      found = parseScrcpyVersion(basename(path));
    } else {
      const binary = findExecutable ? await findExecutable("scrcpy").catch(() => null) : null;
      if (!binary) {
        return {
          ok: false,
          source: null,
          reason: "no scrcpy-server found: no --scrcpy-server, AUTONOM_SCRCPY_SERVER, " +
            "SCRCPY_SERVER_PATH or scrcpy on PATH",
        };
      }
      source = "scrcpy";
      // Homebrew puts a symlink on PATH; the server sits under the real prefix.
      const real = await realpath(binary).catch(() => binary);
      path = join(dirname(dirname(real)), "share", "scrcpy", "scrcpy-server");
      try {
        const { stdout } = await exec(binary, ["--version"], { timeout: 5000 });
        found = parseScrcpyVersion(String(stdout));
      } catch (error) {
        found = parseScrcpyVersion(String(error.stdout ?? ""));
      }
    }
  }
  if (!(await isFile(path))) {
    return { ok: false, source, serverPath: path, reason: `no scrcpy-server file at ${path} (from ${source})` };
  }
  if (!found) {
    return {
      ok: false,
      source,
      serverPath: path,
      reason: `cannot tell which scrcpy version ${path} is (from ${source}); name it ` +
        `scrcpy-server-v${SCRCPY_PROTOCOL_VERSION} or pass --scrcpy-version`,
    };
  }
  if (found !== SCRCPY_PROTOCOL_VERSION) {
    return {
      ok: false,
      source,
      serverPath: path,
      version: found,
      reason: `scrcpy-server ${found} found (from ${source}); the Canvas speaks scrcpy ` +
        `${SCRCPY_PROTOCOL_VERSION} only`,
    };
  }
  return { ok: true, source, serverPath: path, version: found };
}

/**
 * Restart policy after an unexpected exit: 1 s doubling up to 10 s, back to 1 s once
 * the server had streamed for 30 s. Returns this delay and the backoff after it.
 */
export function restartDelay(backoff, streamedMs) {
  const delay = streamedMs >= HEALTHY_STREAM_MS ? RESTART_MIN_MS : backoff;
  return { delay, next: Math.min(delay * 2, RESTART_MAX_MS) };
}

/** Bytes from a socket, taken back out in exact sizes without quadratic copying. */
class ByteQueue {
  #chunks = [];
  #length = 0;

  get length() {
    return this.#length;
  }

  push(chunk) {
    if (!chunk.length) return;
    this.#chunks.push(chunk);
    this.#length += chunk.length;
  }

  /** Exactly `count` bytes (count <= length) in a buffer of their own. */
  take(count) {
    if (count === 0) return EMPTY;
    const first = this.#chunks[0];
    if (first.length === count) {
      this.#chunks.shift();
      this.#length -= count;
      return first;
    }
    // Copy, so a cached packet never pins the larger socket chunk it arrived in.
    const out = Buffer.allocUnsafe(count);
    let offset = 0;
    while (offset < count) {
      const chunk = this.#chunks[0];
      const used = Math.min(chunk.length, count - offset);
      chunk.copy(out, offset, 0, used);
      offset += used;
      if (used === chunk.length) this.#chunks.shift();
      else this.#chunks[0] = chunk.subarray(used);
    }
    this.#length -= count;
    return out;
  }
}

function serverCommand({ version, scid, maxSize, bitRate, maxFps }) {
  return [
    `CLASSPATH=${REMOTE_SERVER_PATH}`, "app_process", "/", SERVER_CLASS, version,
    `scid=${scid}`, "log_level=info", "tunnel_forward=true", "audio=false", "control=true",
    "cleanup=true", `max_size=${maxSize}`, `video_bit_rate=${bitRate}`, `max_fps=${maxFps}`,
    "video_codec=h264", "clipboard_autosync=false",
  ].join(" ");
}

function codecProblem(codec) {
  // Streamer.writeDisableStream: 0 disables the stream, 1 reports a configuration error.
  if (codec === 0) return "scrcpy-server disabled the video stream";
  if (codec === 1) return "scrcpy-server reported a video configuration error";
  return `scrcpy-server sent codec 0x${codec.toString(16).padStart(8, "0")}, not h264`;
}

/**
 * States: idle → starting → streaming ↔ restarting → stopped.
 * Events: state(name), session({width, height}), config(Buffer),
 * packet({keyFrame, pts, data}), device-message(message), control-drain(),
 * error(Error), exit({code, signal}).
 */
export class ScrcpySession extends EventEmitter {
  #options;
  #exec;
  #spawn;
  #connect;
  #state = "idle";
  #attempt = null;
  #attemptCount = 0;
  #stopped = false;
  #stopPromise = null;
  #restartTimer = null;
  #backoff = RESTART_MIN_MS;
  #streamingSince = 0;
  #everStreamed = false;
  #keyFrameSentAt = -Infinity;
  #keyFrameWantedAt = 0;
  #keyFrameReceivedAt = -Infinity;
  #keyFrameTimer = null;
  #stats = { packets: 0, bytes: 0, keyFrames: 0, restarts: 0 };
  #size = null;
  #deviceName = null;

  constructor({
    adbPath,
    serial,
    serverPath,
    version = SCRCPY_PROTOCOL_VERSION,
    maxSize,
    bitRate,
    maxFps,
    exec = defaultExec,
    spawn = nodeSpawn,
    connect = nodeConnect,
  }) {
    super();
    this.#options = { adbPath, serial, serverPath, version, maxSize, bitRate, maxFps };
    this.#exec = exec;
    this.#spawn = spawn;
    this.#connect = connect;
  }

  get state() {
    return this.#state;
  }

  get everStreamed() {
    return this.#everStreamed;
  }

  get connected() {
    const control = this.#attempt?.control;
    return this.#state === "streaming" && Boolean(control) && !control.destroyed;
  }

  get stats() {
    return {
      state: this.#state,
      width: this.#size?.width ?? null,
      height: this.#size?.height ?? null,
      deviceName: this.#deviceName,
      ...this.#stats,
    };
  }

  /**
   * Start streaming. Resolves when the first attempt streams and rejects with its
   * error; later restarts only report through events.
   */
  start() {
    if (this.#stopped) return Promise.reject(new Error("scrcpy session is stopped"));
    if (this.#attempt) return Promise.reject(new Error("scrcpy session already started"));
    this.#setState("starting");
    return this.#run();
  }

  /**
   * Control bytes still held in Canvas memory because the device has not read them.
   * send() never refuses, so callers check this before adding new input and wait for
   * `control-drain` once it is past their bound.
   */
  get controlBacklog() {
    return this.connected ? this.#attempt.control.writableLength : 0;
  }

  /**
   * Write one encoded control message; false when the control socket is not usable.
   * `onWritten` runs once the bytes have left Canvas memory for the socket.
   */
  send(buffer, onWritten = null) {
    if (!this.connected) return false;
    this.#attempt.control.write(buffer, onWritten ? (error) => { if (!error) onWritten(); } : undefined);
    return true;
  }

  /** Ask the encoder for a key frame (RESET_VIDEO), at most once per second. */
  requestKeyFrame() {
    const now = performance.now();
    this.#keyFrameWantedAt = now;
    if (!this.connected) return;
    const wait = this.#keyFrameSentAt + KEY_FRAME_REQUEST_INTERVAL_MS - now;
    if (wait <= 0) {
      this.#sendKeyFrameRequest();
      return;
    }
    if (this.#keyFrameTimer) return;
    // A throttled request is kept: a key frame that already arrived before it was
    // asked for does not help the client that asked.
    this.#keyFrameTimer = setTimeout(() => {
      this.#keyFrameTimer = null;
      if (this.#keyFrameReceivedAt < this.#keyFrameWantedAt && this.connected) {
        this.#sendKeyFrameRequest();
      }
    }, wait);
    this.#keyFrameTimer.unref?.();
  }

  /** Stop for good: close sockets, end the server, remove the forward. Idempotent. */
  stop() {
    if (this.#stopPromise) return this.#stopPromise;
    this.#stopped = true;
    clearTimeout(this.#restartTimer);
    clearTimeout(this.#keyFrameTimer);
    this.#restartTimer = null;
    this.#keyFrameTimer = null;
    this.#setState("stopped");
    this.#stopPromise = Promise.resolve(this.#teardown(this.#attempt));
    return this.#stopPromise;
  }

  #setState(state) {
    if (this.#state === state) return;
    this.#state = state;
    this.emit("state", state);
  }

  #sendKeyFrameRequest() {
    this.#keyFrameSentAt = performance.now();
    this.#attempt.control.write(encodeEmpty(CONTROL_MESSAGE_TYPE.RESET_VIDEO));
  }

  #adb(args, timeout) {
    const { adbPath, serial } = this.#options;
    return this.#exec(adbPath, ["-s", serial, ...args], { timeout });
  }

  async #run() {
    const attempt = {
      id: ++this.#attemptCount,
      scid: randomInt(0, 0x7fff_ffff).toString(16).padStart(8, "0"),
      port: null,
      forwarding: null,
      child: null,
      childExit: null,
      video: null,
      control: null,
      log: "",
      done: false,
      failed: false,
    };
    this.#attempt = attempt;
    try {
      await this.#open(attempt);
    } catch (error) {
      this.#fail(attempt, error);
      throw error;
    }
    return true;
  }

  async #open(attempt) {
    const { serverPath, version, maxSize, bitRate, maxFps } = this.#options;
    await this.#adb(["push", serverPath, REMOTE_SERVER_PATH], PUSH_TIMEOUT_MS);
    this.#checkAttempt(attempt);
    // A teardown that starts while adb is still answering waits for this to remove the forward.
    attempt.forwarding = this.#forward(attempt);
    await attempt.forwarding;
    this.#checkAttempt(attempt);

    const { adbPath, serial } = this.#options;
    const child = this.#spawn(adbPath, [
      "-s", serial, "shell", serverCommand({ version, scid: attempt.scid, maxSize, bitRate, maxFps }),
    ], { stdio: ["ignore", "pipe", "pipe"] });
    attempt.child = child;
    const keepLog = (chunk) => {
      attempt.log = (attempt.log + chunk.toString()).slice(-LOG_TAIL_CHARS);
    };
    child.stdout?.on("data", keepLog);
    child.stderr?.on("data", keepLog);
    attempt.childExit = new Promise((resolvePromise) => {
      child.once("exit", (code, signal) => {
        attempt.exited = { code, signal };
        this.emit("exit", { code, signal });
        resolvePromise();
        if (!attempt.done) {
          const tail = attempt.log.trim();
          this.#fail(attempt, new Error(
            `scrcpy-server exited (${signal ?? `code ${code}`})${tail ? `: ${tail}` : ""}`));
        }
      });
    });
    child.once("error", (error) => {
      attempt.exited ??= { code: null, signal: null };
      this.#fail(attempt, error);
    });

    const { socket: video, rest } = await this.#connectVideo(attempt);
    attempt.video = this.#adopt(attempt, video);
    // The server accepts the control socket right after the video one, then sends meta.
    attempt.control = this.#adopt(attempt, await this.#connectSocket(attempt, CONNECT_ATTEMPT_MS));
    this.#readControl(attempt);
    await this.#readVideo(attempt, rest);
  }

  async #forward(attempt) {
    const { stdout } = await this.#adb(
      ["forward", "tcp:0", `localabstract:scrcpy_${attempt.scid}`], ADB_COMMAND_TIMEOUT_MS);
    const port = Number(String(stdout).trim());
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`adb forward did not report a port: ${String(stdout).trim()}`);
    }
    attempt.port = port;
    return port;
  }

  /**
   * Ports of this attempt's forwards that adb lists. Used when `forward tcp:0` failed
   * or timed out: adb may have created the forward without the port reaching us.
   */
  async #listedForwards(attempt) {
    try {
      const { stdout } = await this.#adb(["forward", "--list"], ADB_COMMAND_TIMEOUT_MS);
      const remote = `localabstract:scrcpy_${attempt.scid}`;
      return String(stdout).split(/\r?\n/)
        .map((line) => line.trim().split(/\s+/))
        .filter((parts) => parts.length >= 3 && parts[2] === remote && /^tcp:\d+$/.test(parts[1]))
        .map((parts) => parts[1]);
    } catch {
      return [];
    }
  }

  /** A socket that connected after its attempt was torn down would leak: close it. */
  #adopt(attempt, socket) {
    if (attempt.done) {
      socket.destroy();
      this.#checkAttempt(attempt);
      throw new Error("scrcpy attempt ended");
    }
    this.#checkAttempt(attempt);
    return socket;
  }

  #checkAttempt(attempt) {
    if (this.#stopped) throw new Error("scrcpy session stopped");
    if (attempt.failed) throw attempt.error ?? new Error("scrcpy-server failed");
  }

  #connectSocket(attempt, timeoutMs) {
    return new Promise((resolvePromise, reject) => {
      const socket = this.#connect({ host: "127.0.0.1", port: attempt.port });
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new Error("connection to scrcpy-server timed out"));
      }, timeoutMs);
      socket.once("connect", () => {
        clearTimeout(timer);
        socket.setNoDelay?.(true);
        resolvePromise(socket);
      });
      socket.once("error", (error) => {
        clearTimeout(timer);
        socket.destroy();
        reject(error);
      });
    });
  }

  /**
   * adb accepts a forwarded connection even before the server listens and then
   * closes it, so only the server's dummy byte proves the tunnel is up.
   */
  async #connectVideo(attempt) {
    const deadline = performance.now() + CONNECT_TIMEOUT_MS;
    let lastError = null;
    while (performance.now() < deadline) {
      this.#checkAttempt(attempt);
      try {
        return await this.#tryVideo(attempt, Math.min(CONNECT_ATTEMPT_MS, deadline - performance.now()));
      } catch (error) {
        lastError = error;
      }
      this.#checkAttempt(attempt);
      await sleep(CONNECT_RETRY_MS);
    }
    const tail = attempt.log.trim();
    throw new Error(`scrcpy-server did not accept a connection within ${CONNECT_TIMEOUT_MS / 1000} s` +
      `${tail ? `: ${tail}` : lastError ? `: ${lastError.message}` : ""}`);
  }

  async #tryVideo(attempt, timeoutMs) {
    const socket = await this.#connectSocket(attempt, timeoutMs);
    return await new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => finish(new Error("no dummy byte from scrcpy-server")), timeoutMs);
      const onData = (chunk) => finish(null, chunk);
      const onClose = () => finish(new Error("scrcpy-server is not listening yet"));
      function finish(error, chunk) {
        clearTimeout(timer);
        socket.off("data", onData);
        socket.off("close", onClose);
        socket.off("error", onClose);
        if (error) {
          socket.destroy();
          reject(error);
          return;
        }
        socket.pause();
        resolvePromise({ socket, rest: chunk.subarray(1) });
      }
      socket.on("data", onData);
      socket.once("close", onClose);
      socket.once("error", onClose);
    });
  }

  #readControl(attempt) {
    const parser = new DeviceMessageParser();
    const control = attempt.control;
    control.on("data", (chunk) => {
      let messages;
      try {
        messages = parser.push(chunk);
      } catch (error) {
        this.#fail(attempt, error);
        return;
      }
      for (const message of messages) this.emit("device-message", message);
    });
    // The device read everything it was sent: callers that stopped adding input may go on.
    control.on("drain", () => {
      if (attempt === this.#attempt) this.emit("control-drain");
    });
    control.on("error", () => {});
    control.on("close", () => this.#fail(attempt, new Error("scrcpy control socket closed")));
  }

  /** Resolves once the codec id is checked, then keeps parsing packets from `data` events. */
  #readVideo(attempt, rest) {
    const video = attempt.video;
    const queue = new ByteQueue();
    let phase = "meta";
    let header = null;
    return new Promise((resolvePromise, reject) => {
      const parse = () => {
        for (;;) {
          if (phase === "meta") {
            if (queue.length < DEVICE_NAME_FIELD_LENGTH + 4) return;
            const name = queue.take(DEVICE_NAME_FIELD_LENGTH).toString("utf8");
            this.#deviceName = name.replace(/\0.*$/s, "");
            const codec = queue.take(4).readUInt32BE(0);
            if (codec !== VIDEO_CODEC_ID_H264) throw new Error(codecProblem(codec));
            phase = "header";
            this.#streaming();
            resolvePromise();
          } else if (phase === "header") {
            if (queue.length < VIDEO_HEADER_LENGTH) return;
            header = parseVideoHeader(queue.take(VIDEO_HEADER_LENGTH));
            if (header.kind === "session") {
              this.#size = { width: header.width, height: header.height };
              this.emit("session", { ...this.#size });
            } else if (header.size > MAX_PACKET_BYTES) {
              throw new Error(`scrcpy packet of ${header.size} bytes is not plausible`);
            } else {
              phase = "payload";
            }
          } else {
            if (queue.length < header.size) return;
            const data = queue.take(header.size);
            phase = "header";
            this.#packet(header, data);
          }
        }
      };
      const onData = (chunk) => {
        queue.push(chunk);
        try {
          parse();
        } catch (error) {
          reject(error);
          this.#fail(attempt, error);
        }
      };
      video.on("data", onData);
      video.on("error", () => {});
      video.on("close", () => {
        const error = new Error("scrcpy video socket closed");
        reject(error);
        this.#fail(attempt, error);
      });
      if (rest.length) onData(rest);
      video.resume();
    });
  }

  #streaming() {
    this.#streamingSince = performance.now();
    this.#everStreamed = true;
    this.#setState("streaming");
  }

  #packet(header, data) {
    if (header.config) {
      this.emit("config", data);
      return;
    }
    this.#stats.packets += 1;
    this.#stats.bytes += data.length;
    if (header.keyFrame) {
      this.#stats.keyFrames += 1;
      this.#keyFrameReceivedAt = performance.now();
    }
    this.emit("packet", { keyFrame: header.keyFrame, pts: header.pts, data });
  }

  /** The current attempt ended badly: clean it up, then restart with backoff. */
  #fail(attempt, error) {
    if (attempt.failed || attempt !== this.#attempt) return;
    attempt.failed = true;
    attempt.error = error;
    if (this.#stopped) return;
    if (this.listenerCount("error")) this.emit("error", error);
    const streamedMs = this.#streamingSince ? performance.now() - this.#streamingSince : 0;
    const { delay, next } = restartDelay(this.#backoff, streamedMs);
    this.#backoff = next;
    this.#streamingSince = 0;
    this.#setState("restarting");
    this.#teardown(attempt).then(() => {
      if (this.#stopped || attempt !== this.#attempt) return;
      this.#restartTimer = setTimeout(() => {
        this.#restartTimer = null;
        if (this.#stopped) return;
        this.#stats.restarts += 1;
        this.#run().catch(() => {});
      }, delay);
    });
  }

  /** Close one attempt's sockets, end its adb shell child, then drop forward and server. */
  async #teardown(attempt) {
    if (!attempt || attempt.torndown) return attempt?.torndown;
    attempt.done = true;
    attempt.torndown = (async () => {
      attempt.video?.destroy();
      attempt.control?.destroy();
      const child = attempt.child;
      if (child && !attempt.exited) {
        // The server ends by itself once its sockets close; the adb child follows.
        if (!(await waitFor(attempt.childExit, CHILD_EXIT_GRACE_MS))) {
          child.kill("SIGTERM");
          if (!(await waitFor(attempt.childExit, CHILD_EXIT_GRACE_MS))) child.kill("SIGKILL");
        }
      }
      let forwards = [];
      if (attempt.forwarding) {
        // `adb forward tcp:0` may still be running: its port is only known once it answers.
        const port = await attempt.forwarding.catch(() => null);
        forwards = port ? [`tcp:${port}`] : await this.#listedForwards(attempt);
      }
      const cleanups = forwards.map((local) =>
        this.#adb(["forward", "--remove", local], ADB_COMMAND_TIMEOUT_MS));
      if (child) {
        // A server that never saw its sockets connect waits in accept() forever. The
        // bracket keeps this pattern from matching the shell that runs pkill itself.
        const pattern = `scid=[${attempt.scid[0]}]${attempt.scid.slice(1)}`;
        cleanups.push(this.#adb(["shell", `pkill -f '${pattern}'`], ADB_COMMAND_TIMEOUT_MS));
      }
      await Promise.allSettled(cleanups);
    })();
    return attempt.torndown;
  }
}

async function waitFor(promise, milliseconds) {
  if (!promise) return true;
  let timer;
  const timeout = new Promise((resolvePromise) => {
    timer = setTimeout(() => resolvePromise(false), milliseconds);
  });
  try {
    return await Promise.race([promise.then(() => true), timeout]);
  } finally {
    clearTimeout(timer);
  }
}
