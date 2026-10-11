#!/usr/bin/env node
/**
 * Autonom ChatGPT probe media endpoint (stdlib only).
 *
 * Listens on 127.0.0.1, gates every request on Host, Origin and the per-start token, and
 * serves /probe/ping, /probe/status, /probe/frames and the /probe/ws WebSocket that streams a
 * generated H.264 pattern (or, through the optional relay module, one allowlisted device).
 *
 *   node probe-media.mjs --port N --autonom-root DIR [--allow-origin O]... [--allow-host H:P]...
 *                        [--allow-device SPEC]... [--adb PATH] [--fps N] [--self-test]
 *
 * The token comes only from the AUTONOM_PROBE_TOKEN environment variable and is never printed.
 * The WebSocket code (ws.mjs) is imported at run time from the Autonom root.
 */
import { realpathSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

import {
  DEFAULT_FPS,
  DROP_CLOSE_CODE,
  DROP_CLOSE_REASON,
  MAX_MESSAGE_BYTES,
  MAX_SOCKETS,
  MEDIA_SCHEMA,
  PROBE_VERSION,
  ProbeError,
  SUBPROTOCOL,
  createBackpressure,
  createRateLimiter,
  createRefusedOriginLog,
  deviceAllowed,
  encodeFrameHeader,
  errorBody,
  gateRequest,
  jsonHeaders,
  parseAllowHost,
  parseAllowOrigin,
  parseArgs,
  parseClientMessage,
  parseFramesQuery,
  parseSource,
  selfTest as libSelfTest,
  validNonce,
  validToken,
} from "./media-lib.mjs";
import {
  DEFAULT_HEIGHT,
  DEFAULT_WIDTH,
  FIXTURE_CODEC,
  codecString,
  configAnnexB,
  encodeSps,
  makeFrame,
} from "./h264-pcm.mjs";

export const SCRIPTS_SUBPATH = Object.freeze(["plugins", "autonom", "skills", "android-emulator-browser", "scripts"]);
export const DEVICE_STOP_DELAY_MS = 10_000;
const SHUTDOWN_GRACE_MS = 300;

export function scriptsDirFor(autonomRoot) {
  return join(autonomRoot, ...SCRIPTS_SUBPATH);
}

/** Import ws.mjs from the Autonom root; throws ProbeError(autonom_root_not_found). */
export async function loadWsModule(autonomRoot) {
  if (typeof autonomRoot !== "string" || !autonomRoot) {
    throw new ProbeError("autonom_root_not_found", "No Autonom root was given.");
  }
  let mod;
  try {
    mod = await import(pathToFileURL(join(scriptsDirFor(autonomRoot), "ws.mjs")).href);
  } catch {
    throw new ProbeError("autonom_root_not_found", "ws.mjs was not found under the Autonom root.");
  }
  if (typeof mod.acceptWebSocket !== "function" || typeof mod.rejectUpgrade !== "function") {
    throw new ProbeError("autonom_root_not_found", "ws.mjs under the Autonom root lacks acceptWebSocket or rejectUpgrade.");
  }
  return mod;
}

async function defaultDeviceSourceLoader() {
  const mod = await import("./device-source.mjs");
  if (typeof mod.createDeviceSource !== "function") throw new Error("device-source.mjs has no createDeviceSource");
  return mod.createDeviceSource;
}

function startsWithSps(data) {
  if (!data || data.length < 5) return false;
  if (data[0] === 0 && data[1] === 0 && data[2] === 0 && data[3] === 1) return (data[4] & 0x1f) === 7;
  if (data[0] === 0 && data[1] === 0 && data[2] === 1) return (data[3] & 0x1f) === 7;
  return false;
}

function safeUrl(rawUrl) {
  try {
    return new URL(String(rawUrl ?? "/"), "http://127.0.0.1");
  } catch {
    return null;
  }
}

/**
 * Start the endpoint in this process.
 * deps: createDeviceSource (injected factory), loadDeviceSource (async loader of the factory),
 * loadWs (async loader of ws.mjs), deviceStopDelayMs, now.
 */
export async function startProbeMedia(options, deps = {}) {
  const {
    port = 0,
    token,
    autonomRoot,
    allowOrigins = [],
    allowHosts = [],
    allowDevices = [],
    adbPath = null,
    fps = DEFAULT_FPS,
  } = options ?? {};
  if (!validToken(token)) throw new ProbeError("token_missing", "The probe token is missing or shorter than 32 characters.");
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new ProbeError("invalid_arguments", "port must be 0-65535", { field: "port" });
  if (!Number.isInteger(fps) || fps < 1 || fps > 60) throw new ProbeError("invalid_arguments", "fps must be 1-60", { field: "fps" });
  const originList = allowOrigins.map((origin) => parseAllowOrigin(origin));
  const hostList = allowHosts.map((host) => parseAllowHost(host));
  const deviceList = [...allowDevices];
  const ws = await (deps.loadWs ?? loadWsModule)(autonomRoot);
  const scriptsDir = scriptsDirFor(autonomRoot);
  const stopDelayMs = deps.deviceStopDelayMs ?? DEVICE_STOP_DELAY_MS;
  const now = deps.now ?? (() => Date.now());
  const startedAt = now();
  const refused = createRefusedOriginLog();
  const clients = new Set();
  const devices = new Map();
  let reserved = 0;
  let fixtureSeq = 0;
  let listenPort = port;
  let closing = false;
  let closePromise = null;
  let deviceFactory = deps.createDeviceSource ?? null;

  async function getDeviceFactory() {
    if (deviceFactory) return deviceFactory;
    try {
      deviceFactory = await (deps.loadDeviceSource ?? defaultDeviceSourceLoader)();
      return deviceFactory;
    } catch {
      return null;
    }
  }

  function nextFixtureSeq() {
    const seq = fixtureSeq;
    fixtureSeq = (fixtureSeq + 1) >>> 0;
    return seq;
  }

  function sendJson(res, status, body, corsOrigin = null, extraHeaders = {}) {
    const text = JSON.stringify(body);
    res.writeHead(status, { ...jsonHeaders(corsOrigin, extraHeaders), "Content-Length": Buffer.byteLength(text) });
    res.end(text);
  }

  function reject(socket, status, code) {
    ws.rejectUpgrade(socket, status, JSON.stringify(errorBody(code)));
  }

  // ---- device sources (shared per spec, contract 3.6) ----

  function deviceEntry(spec, factory) {
    let entry = devices.get(spec);
    if (entry) return entry;
    entry = { spec, subscribers: new Set(), state: "starting", config: null, seq: 0, stopTimer: null, source: null, ready: null };
    devices.set(spec, entry);
    const current = entry;
    current.ready = (async () => {
      const source = await factory({ spec, adbPath, scriptsDir });
      current.source = source;
      source.on("config", (config) => onDeviceConfig(current, config));
      source.on("frame", (frame) => onDeviceFrame(current, frame));
      source.on("error", (error) => onDeviceError(current, error));
      source.on("state", (name) => {
        current.state = String(name).slice(0, 40);
      });
      await source.start();
      if (current.state === "starting") current.state = "streaming";
    })();
    current.ready.catch((error) => {
      current.state = "failed";
      if (devices.get(spec) === current) devices.delete(spec);
      for (const client of current.subscribers) client.fail(error);
      current.source?.stop?.().catch(() => {});
    });
    return current;
  }

  function onDeviceConfig(entry, config) {
    if (!config || !config.annexB) return;
    const annexB = Buffer.from(config.annexB);
    let codec = typeof config.codec === "string" && config.codec ? config.codec : null;
    if (!codec) {
      try {
        codec = codecString(annexB);
      } catch {
        codec = "avc1.42C01E";
      }
    }
    entry.config = { codec, width: config.width, height: config.height, annexB };
    for (const client of entry.subscribers) client.sendConfig(entry.config, entry.seq);
  }

  function onDeviceFrame(entry, frame) {
    if (!frame || !frame.data) return;
    const seq = entry.seq;
    entry.seq = (entry.seq + 1) >>> 0;
    const data = Buffer.from(frame.data);
    const key = Boolean(frame.key);
    let payload = data;
    let config = false;
    if (key && entry.config) {
      config = true;
      if (!startsWithSps(data)) payload = Buffer.concat([entry.config.annexB, data]);
    }
    for (const client of entry.subscribers) client.deliver({ key, config, seq, payload });
  }

  function onDeviceError(entry, error) {
    for (const client of entry.subscribers) client.sendError(error?.code ?? "source_failed", String(error?.message ?? "source error").slice(0, 300));
  }

  function releaseDevice(entry, client) {
    entry.subscribers.delete(client);
    if (entry.subscribers.size || entry.stopTimer || devices.get(entry.spec) !== entry) return;
    entry.stopTimer = setTimeout(() => stopDevice(entry), stopDelayMs);
    entry.stopTimer.unref?.();
  }

  async function stopDevice(entry) {
    clearTimeout(entry.stopTimer);
    entry.stopTimer = null;
    if (devices.get(entry.spec) === entry) devices.delete(entry.spec);
    entry.state = "stopped";
    await entry.ready?.catch(() => {});
    try {
      await entry.source?.stop();
    } catch {
      // A source that fails to stop is dropped; nothing else holds it.
    }
  }

  // ---- WebSocket clients ----

  function attachClient(conn, socket, source, factory) {
    const limiter = createRateLimiter();
    const backpressure = createBackpressure();
    let echoCount = 0;
    let timer = null;
    let paused = false;
    let configSent = false;
    let waitingKey = true;
    let entry = null;
    let closed = false;

    const send = (message) => {
      if (conn.readyState === 1) conn.send(JSON.stringify(message));
    };
    const client = {
      source,
      socket,
      conn,
      sendError(code, message) {
        send({ type: "error", error_code: code, error: message ?? errorBody(code).error });
      },
      sendConfig(config, seq) {
        send({ type: "config", codec: config.codec, width: config.width, height: config.height, seq });
        configSent = true;
        // A new or changed configuration is always followed by a key frame.
        waitingKey = true;
      },
      deliver({ key, config, seq, payload }) {
        if (paused || !configSent || conn.readyState !== 1) return;
        if (waitingKey && !key) return;
        if (!backpressure.admit(conn.bufferedAmount, key)) return;
        waitingKey = false;
        conn.send(Buffer.concat([encodeFrameHeader({ key, config, seq, tServerMs: Date.now() }), payload]));
      },
      fail(error) {
        client.sendError(error?.code ?? "source_failed", String(error?.message ?? "source failed").slice(0, 300));
        if (conn.readyState === 1) conn.close(1011, "source failed");
      },
      shutdown(code, reason) {
        if (conn.readyState === 1) conn.close(code, reason);
      },
    };

    const fixtureTick = () => {
      if (paused || conn.readyState !== 1) return;
      const seq = nextFixtureSeq();
      if (!configSent) client.sendConfig({ codec: FIXTURE_CODEC, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT }, seq);
      if (!backpressure.admit(conn.bufferedAmount, true)) return;
      const { annexB } = makeFrame({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, frame: seq, timeMs: Date.now() });
      conn.send(Buffer.concat([encodeFrameHeader({ key: true, config: true, seq, tServerMs: Date.now() }), annexB]));
    };
    const startFixture = (rate) => {
      clearInterval(timer);
      paused = false;
      timer = setInterval(fixtureTick, Math.max(1, Math.round(1000 / rate)));
      setImmediate(fixtureTick);
    };

    const handle = (message) => {
      switch (message.type) {
        case "start":
          if (source.kind !== "fixture") return client.sendError("not_supported");
          return startFixture(message.fps);
        case "stop":
          paused = true;
          clearInterval(timer);
          timer = null;
          return undefined;
        case "echo":
          echoCount += 1;
          return send({ type: "echo", n: echoCount, payload: message.payload });
        case "ping":
          return send({ type: "pong", seq: message.seq, t_client: message.t_client, t_server: Date.now() });
        case "drop":
          return conn.close(DROP_CLOSE_CODE, DROP_CLOSE_REASON);
        case "tap":
          if (source.kind === "fixture") return client.sendError("not_supported");
          return tap(message);
        default:
          return client.sendError("bad_message");
      }
    };

    const tap = async ({ seq, x, y }) => {
      let answer;
      try {
        await entry.ready;
        const result = await entry.source.tap({ x, y });
        answer = result?.ok
          ? { type: "tap_ack", seq, ok: true }
          : {
              type: "tap_ack",
              seq,
              ok: false,
              error_code: String(result?.error_code ?? "tap_failed"),
              error: String(result?.error ?? "The tap was not delivered.").slice(0, 300),
            };
      } catch (error) {
        answer = { type: "tap_ack", seq, ok: false, error_code: String(error?.code ?? "tap_failed"), error: String(error?.message ?? "tap failed").slice(0, 300) };
      }
      send(answer);
    };

    conn.on("message", (data, isBinary) => {
      const verdict = limiter.check();
      if (verdict === "drop") return;
      if (verdict === "notify") return client.sendError("rate_limited");
      if (isBinary) return client.sendError("bad_message");
      const parsed = parseClientMessage(String(data));
      if (!parsed.ok) return client.sendError("bad_message");
      return handle(parsed.message);
    });
    conn.on("close", () => {
      if (closed) return;
      closed = true;
      clearInterval(timer);
      clients.delete(client);
      if (entry) releaseDevice(entry, client);
    });
    if (socket.destroyed) {
      // The peer left before the accept finished; the connection reports close on its own.
      return;
    }
    clients.add(client);

    send({ type: "hello", schema_version: MEDIA_SCHEMA, source: { kind: source.kind, id: source.id }, server_time_ms: Date.now() });
    if (source.kind === "fixture") {
      startFixture(fps);
    } else {
      entry = deviceEntry(source.id, factory);
      clearTimeout(entry.stopTimer);
      entry.stopTimer = null;
      entry.subscribers.add(client);
      if (entry.config) client.sendConfig(entry.config, entry.seq);
    }
  }

  async function handleUpgrade(request, socket, head) {
    socket.on("error", () => {});
    if (closing) return reject(socket, 503, "server_stopping");
    const url = safeUrl(request.url);
    const path = url?.pathname ?? "";
    const gate = gateRequest({
      method: request.method,
      path,
      headers: request.headers,
      upgrade: true,
      port: listenPort,
      token,
      allowHosts: hostList,
      allowOrigins: originList,
      refused,
    });
    if (!gate.ok) return reject(socket, gate.status, gate.code);
    if (path !== "/probe/ws") return reject(socket, 404, "not_found");
    const source = parseSource(url.searchParams.get("source"));
    if (!source) return reject(socket, 400, "bad_source");
    if (source.kind !== "fixture" && !deviceAllowed(source.id, deviceList)) return reject(socket, 403, "device_not_allowed");
    if (clients.size + reserved >= MAX_SOCKETS) return reject(socket, 503, "too_many_sockets");
    reserved += 1;
    try {
      let factory = null;
      if (source.kind !== "fixture") {
        factory = await getDeviceFactory();
        if (!factory) return reject(socket, 501, "relay_unavailable");
      }
      if (socket.destroyed) return undefined;
      if (closing) return reject(socket, 503, "server_stopping");
      const conn = ws.acceptWebSocket(request, socket, head, { maxMessageBytes: MAX_MESSAGE_BYTES, protocols: [SUBPROTOCOL] });
      if (!conn) return undefined;
      attachClient(conn, socket, source, factory);
      return undefined;
    } finally {
      reserved -= 1;
    }
  }

  // ---- HTTP routes ----

  function handleRequest(req, res) {
    const url = safeUrl(req.url);
    const path = url?.pathname ?? "";
    const gate = gateRequest({
      method: req.method,
      path,
      headers: req.headers,
      port: listenPort,
      token,
      allowHosts: hostList,
      allowOrigins: originList,
      refused,
    });
    if (!gate.ok) return sendJson(res, gate.status, errorBody(gate.code), gate.cors);
    if (gate.preflight) {
      res.writeHead(204, { ...gate.headers, "Cache-Control": "no-store", "Content-Length": 0 });
      return res.end();
    }
    const origin = gate.origin;
    if (req.method !== "GET") return sendJson(res, 405, errorBody("method_not_allowed"), origin, { Allow: "GET, OPTIONS" });
    switch (path) {
      case "/probe/ping": {
        const nonce = url.searchParams.get("nonce");
        if (!validNonce(nonce)) return sendJson(res, 400, errorBody("bad_nonce"), origin);
        return sendJson(
          res,
          200,
          { ok: true, schema_version: MEDIA_SCHEMA, nonce, server_time_ms: Date.now(), origin_seen: origin, host_seen: String(req.headers.host ?? "") },
          origin,
        );
      }
      case "/probe/status":
        return sendJson(res, 200, statusBody(), origin);
      case "/probe/frames": {
        const query = parseFramesQuery(url.searchParams);
        if (!query) return sendJson(res, 400, errorBody("bad_query"), origin);
        return sendJson(res, 200, framesBody(query), origin);
      }
      case "/probe/ws":
        return sendJson(res, 426, errorBody("upgrade_required"), origin, { Upgrade: "websocket" });
      default:
        return sendJson(res, 404, errorBody("not_found"), origin);
    }
  }

  function statusBody() {
    let fixtureSubscribers = 0;
    for (const client of clients) if (client.source.kind === "fixture") fixtureSubscribers += 1;
    const sources = [{ id: "fixture", subscribers: fixtureSubscribers }];
    for (const entry of devices.values()) sources.push({ id: entry.spec, state: entry.state, subscribers: entry.subscribers.size });
    return {
      ok: true,
      schema_version: MEDIA_SCHEMA,
      uptime_ms: Math.max(0, now() - startedAt),
      clients: clients.size,
      sources,
      refused_origins: refused.list(),
    };
  }

  function framesBody({ count, afterSeq }) {
    const frames = [];
    for (let i = 1; i <= count; i += 1) {
      const seq = afterSeq + i;
      const timeMs = Date.now();
      const { annexB, key } = makeFrame({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, frame: seq, timeMs });
      frames.push({ seq, key, t_server_ms: timeMs, data_b64: annexB.toString("base64") });
    }
    return {
      ok: true,
      schema_version: MEDIA_SCHEMA,
      codec: codecString(encodeSps({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT })),
      width: DEFAULT_WIDTH,
      height: DEFAULT_HEIGHT,
      config_b64: configAnnexB({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT }).toString("base64"),
      frames,
    };
  }

  const server = createServer((req, res) => {
    try {
      handleRequest(req, res);
    } catch {
      if (!res.headersSent) sendJson(res, 500, errorBody("internal_error"));
      else res.destroy();
    }
  });
  server.on("upgrade", (request, socket, head) => {
    handleUpgrade(request, socket, head).catch(() => {
      if (!socket.destroyed) reject(socket, 500, "internal_error");
    });
  });
  server.on("clientError", (_error, socket) => {
    if (!socket.destroyed) socket.destroy();
  });

  await new Promise((resolve, rejectListen) => {
    const onError = (error) => {
      server.off("listening", onListening);
      if (error?.code === "EADDRINUSE") rejectListen(new ProbeError("media_port_in_use", `Port ${port} is in use.`));
      else rejectListen(new ProbeError("media_start_failed", String(error?.message ?? error)));
    };
    const onListening = () => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, "127.0.0.1");
  });
  listenPort = server.address().port;

  async function close() {
    if (closePromise) return closePromise;
    closing = true;
    closePromise = (async () => {
      const sockets = [];
      for (const client of clients) {
        sockets.push(client.socket);
        try {
          client.shutdown(1001, "probe stopping");
        } catch {
          // Already closing.
        }
      }
      const stops = [...devices.values()].map((entry) => stopDevice(entry));
      server.close();
      server.closeAllConnections?.();
      await Promise.race([Promise.allSettled(stops), new Promise((resolve) => setTimeout(resolve, 1000))]);
      await new Promise((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS));
      for (const socket of sockets) socket.destroy();
      clients.clear();
    })();
    return closePromise;
  }

  return { port: listenPort, close };
}

/** `--self-test`: helper and bitstream assertions without listening. Returns failed check names. */
export function runSelfTest() {
  const failures = libSelfTest();
  const check = (name, condition) => {
    if (!condition) failures.push(name);
  };
  try {
    const sps = encodeSps({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT });
    check("codec string", codecString(sps) === "avc1.42C01E" && FIXTURE_CODEC === "avc1.42C01E");
    const config = configAnnexB({ width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT });
    check("config starts with SPS", startsWithSps(config));
    const a = makeFrame({ frame: 3, timeMs: 1234 }).annexB;
    const b = makeFrame({ frame: 3, timeMs: 1234 }).annexB;
    const c = makeFrame({ frame: 4, timeMs: 1234 }).annexB;
    check("frame deterministic", a.equals(b));
    check("frame differs", !a.equals(c));
    check("frame starts with config", a.subarray(0, config.length).equals(config));
    check("frame size", a.length > 78 * 384 && a.length < 78 * 400);
  } catch (error) {
    failures.push(`bitstream: ${String(error?.message ?? error).slice(0, 120)}`);
  }
  return failures;
}

function writeErrorLine(code, message, extras = {}) {
  const body = { ok: false, error_code: code, error: message, ...extras };
  if (code === "token_missing") body.hint = "Start the probe from its MCP server, which passes AUTONOM_PROBE_TOKEN (32+ characters of A-Z, a-z, 0-9, _ or -).";
  if (code === "autonom_root_not_found") body.hint = "Pass --autonom-root pointing at an Autonom checkout or install that has the Canvas scripts.";
  if (code === "invalid_arguments") body.hint = "Fix the named argument and run again.";
  if (code === "media_start_failed") body.hint = "See error; run the probe media endpoint by hand to debug.";
  process.stderr.write(`${JSON.stringify(body)}\n`);
}

function exitAfterFlush(code) {
  // Nothing keeps the event loop alive on these paths, so the process ends once the
  // pending stdout/stderr writes are flushed.
  process.exitCode = code;
}

export async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    writeErrorLine(error?.code ?? "invalid_arguments", String(error?.message ?? error), error?.extras ?? {});
    return exitAfterFlush(2);
  }
  if (options.selfTest) {
    const failures = runSelfTest();
    process.stdout.write(`${JSON.stringify({ ok: failures.length === 0, self_test: failures.length ? "failed" : "passed", failures })}\n`);
    return exitAfterFlush(failures.length ? 1 : 0);
  }
  const token = process.env.AUTONOM_PROBE_TOKEN;
  // Child processes (adb, companions) never inherit the token.
  delete process.env.AUTONOM_PROBE_TOKEN;
  if (!validToken(token)) {
    writeErrorLine("token_missing", "AUTONOM_PROBE_TOKEN is missing or shorter than 32 characters of A-Z, a-z, 0-9, _ or -.");
    return exitAfterFlush(2);
  }
  // Signal and stdin handlers go in before anything can be printed: a parent that reads the
  // ready line may stop the endpoint at once, and a signal without a handler kills Node.
  let handle = null;
  let stopping = false;
  const shutdown = () => {
    if (stopping) return;
    stopping = true;
    const hard = setTimeout(() => process.exit(0), 1800);
    hard.unref?.();
    if (!handle) {
      // Still starting: exit as soon as startup settles (or at once when it never listens).
      return;
    }
    handle.close().finally(() => process.exit(0));
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  process.stdin.on("end", shutdown);
  process.stdin.on("error", shutdown);
  process.stdin.resume();
  const detachInput = () => {
    // Startup failed: stop holding stdin so the process can end after its error line.
    process.stdin.off("end", shutdown);
    process.stdin.off("error", shutdown);
    process.stdin.destroy();
  };
  try {
    handle = await startProbeMedia({ ...options, token });
  } catch (error) {
    detachInput();
    if (error?.code === "media_port_in_use") {
      process.stdout.write(`${JSON.stringify({ event: "error", error_code: "media_port_in_use" })}\n`);
      return exitAfterFlush(3);
    }
    if (error?.code === "autonom_root_not_found" || error?.code === "invalid_arguments") {
      writeErrorLine(error.code, String(error.message));
      return exitAfterFlush(2);
    }
    writeErrorLine("media_start_failed", String(error?.message ?? error).slice(0, 300));
    return exitAfterFlush(1);
  }
  if (stopping) {
    // A stop request arrived while the endpoint was starting.
    handle.close().finally(() => process.exit(0));
    return undefined;
  }
  process.stdout.write(`${JSON.stringify({ event: "ready", port: handle.port, pid: process.pid, version: PROBE_VERSION })}\n`);
  return undefined;
}

function isEntry() {
  if (!process.argv[1]) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href || import.meta.url === pathToFileURL(process.argv[1]).href;
  } catch {
    return false;
  }
}

if (isEntry()) {
  main().catch((error) => {
    writeErrorLine("media_start_failed", String(error?.message ?? error).slice(0, 300));
    exitAfterFlush(1);
  });
}
