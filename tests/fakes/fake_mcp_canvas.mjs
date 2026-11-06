#!/usr/bin/env node
/**
 * Fake workspace Canvas for the Autonom MCP adapter media tests (contract 5.6).
 *
 * Built on the real Canvas WebSocket server side (`ws.mjs`, `acceptWebSocket`,
 * imported read-only). The fake CLI runs it through FAKE_MCP_CANVAS_CMD as
 * `<cmd> canvas <verb> ...`:
 *
 *   canvas serve --workspace W --ephemeral --bootable-only --port 0 --control-idle N
 *       serves 127.0.0.1, writes `<state base>/canvas/<port>.json` (schema
 *       autonom-canvas/v1, mode workspace) with a fresh token, and prints its URL
 *       with `#token=` on stdout as the real Canvas does;
 *   canvas attach|detach --workspace W --platform P --target T
 *   canvas stop --workspace W
 *       client verbs: they call the running fake as the CLI calls the Canvas
 *       (Bearer token, explicit `X-Autonom-Origin: agent`) and print the CLI's
 *       JSON answers (errors on stderr, exit 2).
 *
 * Routes: GET/POST /api/devices, POST /api/devices/<id>/detach, POST /api/stop,
 * GET /d/<id>/status, POST /d/<id>/{tap,swipe,key,text,control}, and the upgrades
 * /d/<id>/ws/video (a scripted session, config and packet sequence) and
 * /d/<id>/ws/control (every message and the origin recorded; a key-frame request
 * makes the next packet a key frame). Origins fail closed as in the Canvas: a
 * missing or unknown X-Autonom-Origin is recorded and counted as `agent`.
 *
 * Behaviour comes from the JSON file FAKE_MCP_CANVAS_CONFIG, read again on
 * every request, so a test can change it while the fake runs:
 *   bind_session   session id every attached device is bound to (else the live
 *                  session of the target in FAKE_MCP_STATE/sessions.json)
 *   preattach      [{platform, target}] attached at start
 *   display        {width, height} of /status (default 1080x2400)
 *   script         {route: [answer, ...]}: the n-th request of a route (tap, swipe,
 *                  key, text, control) gets the n-th answer: "200", "hang", "409",
 *                  "409:<error_code>" or any other status code
 *   video          {width, height, packets (null: endless), packet_bytes, key_every,
 *                  key_interval_ms, interval_ms, pause_after, pause_ms (a pause after
 *                  that many packets), new_session_at, new_width, new_height,
 *                  unknown_first (an unknown message type before the packets)}
 * Every event is appended as one JSON line to FAKE_MCP_CANVAS_LOG (never the token).
 */
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { appendFileSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { join } from "node:path";

import { acceptWebSocket, READY_STATE, rejectUpgrade } from "../../plugins/autonom/skills/android-emulator-browser/scripts/ws.mjs";

const DISCOVERY_SCHEMA = "autonom-canvas/v1";
const ORIGINS = ["human", "agent", "replay", "system"];
const ROUTES = ["tap", "swipe", "key", "text", "control"];
// A real H.264 SPS and PPS as x264 writes them (High profile, level 3.1, 1280x720), with the
// emulation prevention bytes (00 00 03) of a real stream; its codec string is avc1.64001F.
const CONFIG_FIXTURE = Buffer.from(
  "000000016764001facd9405005bb011000000300100000030320f1831960" + "0000000168ebe3cb22c0", "hex");

// ----- shared helpers -------------------------------------------------------------------

function stateBase(env = process.env) {
  if (env.AUTONOM_HOME) return env.AUTONOM_HOME;
  if (env.XDG_STATE_HOME) return join(env.XDG_STATE_HOME, "autonom");
  return join(env.HOME ?? ".", ".local", "state", "autonom");
}

function log(event) {
  const path = process.env.FAKE_MCP_CANVAS_LOG;
  if (!path) return;
  try {
    appendFileSync(path, `${JSON.stringify({ at: Date.now() / 1000, pid: process.pid, ...event })}\n`);
  } catch {
    // a log that cannot be written never stops the fake
  }
}

function readConfig() {
  const path = process.env.FAKE_MCP_CANVAS_CONFIG;
  if (!path) return {};
  try {
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function flags(args) {
  const out = {};
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (!token.startsWith("--")) continue;
    const [name, inline] = token.slice(2).split("=", 2);
    if (inline !== undefined) out[name] = inline;
    else if (index + 1 < args.length && !args[index + 1].startsWith("--")) out[name] = args[++index];
    else out[name] = true;
  }
  return out;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** A live pid that is not a zombie (as the CLI's pid_alive): a parent may not have reaped it yet. */
function pidAlive(pid) {
  try {
    process.kill(pid, 0);
  } catch (error) {
    if (error.code !== "EPERM") return false;
  }
  const out = spawnSync("ps", ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" });
  if (out.error) return true;
  const stat = String(out.stdout ?? "").trim();
  return Boolean(stat) && !stat.startsWith("Z");
}

function deviceId(platform, target) {
  return `${platform}~${target}`;
}

// ----- serve ------------------------------------------------------------------------------

function liveSessionOf(platform, target) {
  const root = process.env.FAKE_MCP_STATE;
  if (!root) return null;
  try {
    const table = JSON.parse(readFileSync(join(root, "sessions.json"), "utf8"));
    for (const record of Object.values(table)) {
      if (record.platform === platform && record.target_id === target && !record.stopped_at) return record.session_id;
    }
  } catch {
    // no sessions yet
  }
  return null;
}

async function serve(args) {
  const options = flags(args);
  const workspace = typeof options.workspace === "string" ? options.workspace : "default";
  const token = randomBytes(24).toString("base64url");
  const devices = new Map();
  const counters = new Map();
  const hanging = new Set();
  const sockets = new Set();
  let port = 0;
  let discoveryPath = null;
  let stopping = false;
  const holder = () => ({ kind: "human", id: `canvas:${port}:${process.pid}`, label: `Canvas ${port}` });

  function bindingFor(platform, target) {
    const config = readConfig();
    if (typeof config.bind_session === "string") return config.bind_session;
    return liveSessionOf(platform, target);
  }

  function addDevice(platform, target) {
    const id = deviceId(platform, target);
    const sessionId = bindingFor(platform, target);
    const entry = {
      id, platform, target, sessionId, attachedAt: new Date().toISOString(), videoClients: new Set(),
      controlClients: new Set(), keyframePending: false,
      lease: { state: "none", holder: null, generation: 0, expires_at: null, took_over_from: null },
      controlOwner: "shared",
    };
    devices.set(id, entry);
    return entry;
  }

  function leaseView(entry) {
    const lease = entry.lease;
    return { state: lease.state, holder: lease.holder ? { ...lease.holder } : null,
      generation: lease.generation || null, expires_at: lease.expires_at,
      mine: lease.state === "held" && lease.holder?.id === holder().id,
      took_over_from: lease.took_over_from, ended_by: null, idle_s: 120 };
  }

  function summary(entry) {
    return { id: entry.id, platform: entry.platform, target: entry.target, name: entry.target, state: "live",
      tab: null, slot: null, session: entry.sessionId ? { id: entry.sessionId, started_by_canvas: false, reused: true } : null,
      attached_at: entry.attachedAt, lease: leaseView(entry), session_stopped: false };
  }

  function send(response, status, body) {
    if (response.writableEnded) return;
    const data = JSON.stringify(body);
    response.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
    response.end(data);
  }

  function shutdown() {
    if (stopping) return;
    stopping = true;
    log({ t: "stop" });
    if (discoveryPath) rmSync(discoveryPath, { force: true });
    for (const response of hanging) response.destroy();
    for (const socket of sockets) socket.destroy();
    server.close();
    setTimeout(() => process.exit(0), 20);
  }

  function nextAnswer(route) {
    const count = counters.get(route) ?? 0;
    counters.set(route, count + 1);
    const script = readConfig().script ?? {};
    const answers = Array.isArray(script[route]) ? script[route] : [];
    return String(answers[count] ?? "200");
  }

  function applyControl(entry, mode, origin) {
    const lease = entry.lease;
    if (origin === "human" && mode === "takeover") {
      const own = lease.state === "held" && lease.holder?.id === holder().id;
      if (!own) {
        lease.took_over_from = lease.state === "held" ? lease.holder : null;
        lease.generation += 1;
      }
      lease.state = "held";
      lease.holder = holder();
      lease.expires_at = new Date(Date.now() + 30000).toISOString();
    } else if (origin === "human" && mode === "release" && lease.state === "held" && lease.holder?.id === holder().id) {
      lease.state = "free";
      lease.holder = null;
      lease.expires_at = null;
      lease.took_over_from = null;
    }
    if (mode === "takeover") entry.controlOwner = origin;
    else if (mode === "release") entry.controlOwner = "shared";
  }

  async function readBody(request) {
    const chunks = [];
    let size = 0;
    for await (const chunk of request) {
      size += chunk.length;
      if (size > 1024 * 1024) throw new Error("body too large");
      chunks.push(chunk);
    }
    if (!chunks.length) return {};
    try {
      const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      return value && typeof value === "object" ? value : {};
    } catch {
      return { __invalid__: true };
    }
  }

  async function handle(request, response) {
    const url = new URL(request.url, "http://127.0.0.1");
    const raw = request.headers["x-autonom-origin"];
    const originHeader = raw === undefined ? null : String(raw);
    const origin = ORIGINS.includes(originHeader) ? originHeader : "agent";
    const auth = request.headers.authorization === `Bearer ${token}`;
    const body = request.method === "POST" ? await readBody(request) : null;
    log({ t: "http", method: request.method, path: url.pathname, origin_header: originHeader, origin, auth, body });
    if (!auth) {
      send(response, 401, { error: "Unauthorized" });
      return;
    }
    const path = url.pathname;
    if (path === "/api/stop" && request.method === "POST") {
      send(response, 200, { ok: true, stopping: true });
      setTimeout(shutdown, 30);
      return;
    }
    if (path === "/api/devices" && request.method === "GET") {
      send(response, 200, { ok: true, devices: [...devices.values()].map(summary) });
      return;
    }
    if (path === "/api/devices" && request.method === "POST") {
      const platform = body?.platform;
      const target = body?.target;
      if (!["android", "ios"].includes(platform) || typeof target !== "string" || !target) {
        send(response, 400, { ok: false, error: "platform and target are required", error_code: "invalid_value" });
        return;
      }
      const id = deviceId(platform, target);
      const present = devices.get(id);
      const entry = present ?? addDevice(platform, target);
      log({ t: "attach", device: id, attached: !present, session: entry.sessionId });
      send(response, 200, { ok: true, attached: !present, device: summary(entry) });
      return;
    }
    const detach = path.match(/^\/api\/devices\/([^/]+)\/detach$/);
    if (detach && request.method === "POST") {
      const id = decodeURIComponent(detach[1]);
      const entry = devices.get(id);
      if (!entry) {
        send(response, 404, { ok: false, error: `No device ${id} is attached`, error_code: "device_not_found" });
        return;
      }
      for (const conn of [...entry.videoClients, ...entry.controlClients]) conn.close(1001, "detached");
      devices.delete(id);
      log({ t: "detach", device: id });
      send(response, 200, { ok: true, detached: id, session_stopped: false });
      return;
    }
    const route = path.match(/^\/d\/([^/]+)\/([a-z]+)$/);
    if (!route) {
      send(response, 404, { ok: false, error: "Not found", error_code: "not_found" });
      return;
    }
    const entry = devices.get(decodeURIComponent(route[1]));
    if (!entry) {
      send(response, 404, { error: "No such device" });
      return;
    }
    const name = route[2];
    if (name === "status" && request.method === "GET") {
      const display = readConfig().display ?? { width: 1080, height: 2400 };
      send(response, 200, { platform: entry.platform, display, control_owner: entry.controlOwner, input_paused: false,
        lease: leaseView(entry), session_stopped: false });
      return;
    }
    if (!ROUTES.includes(name) || request.method !== "POST") {
      send(response, 404, { error: "Not found" });
      return;
    }
    const answer = nextAnswer(name);
    log({ t: "answer", route: name, answer });
    if (answer === "hang") {
      hanging.add(response);
      response.on("close", () => hanging.delete(response));
      return;
    }
    if (answer === "200") {
      if (name === "control") {
        applyControl(entry, String(body?.mode ?? ""), origin);
        send(response, 200, { ok: true, control_owner: entry.controlOwner, input_paused: false,
          lease: leaseView(entry), session_stopped: false });
      } else {
        send(response, 200, { ok: true });
      }
      return;
    }
    const [status, code] = answer.split(":", 2);
    if (code) send(response, Number(status), { ok: false, error: `fake refusal ${code}`, error_code: code });
    else if (status === "409") send(response, 409, { error: "Canvas input is paused" });
    else send(response, Number(status) || 500, { error: `fake answer ${status}` });
  }

  const server = createServer((request, response) => {
    handle(request, response).catch((error) => send(response, 500, { error: String(error?.message ?? error) }));
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  server.on("upgrade", (request, socket, head) => {
    const url = new URL(request.url, "http://127.0.0.1");
    const match = url.pathname.match(/^\/d\/([^/]+)\/ws\/(video|control)$/);
    const auth = request.headers.authorization === `Bearer ${token}` || url.searchParams.get("token") === token;
    const originParam = url.searchParams.get("origin");
    log({ t: "upgrade", path: url.pathname, kind: match?.[2] ?? null, origin_param: originParam,
      origin_header: "origin" in request.headers, auth, query_token: url.searchParams.has("token") });
    if (!auth) {
      rejectUpgrade(socket, 401, "Unauthorized");
      return;
    }
    const entry = match ? devices.get(decodeURIComponent(match[1])) : null;
    if (!entry) {
      rejectUpgrade(socket, 404, "No such device");
      return;
    }
    if (originParam !== null && !ORIGINS.includes(originParam)) {
      rejectUpgrade(socket, 400, "origin must be human, agent, replay, or system");
      return;
    }
    const origin = originParam ?? "agent";
    const conn = acceptWebSocket(request, socket, head, { maxMessageBytes: 64 * 1024 });
    if (!conn) return;
    const kind = match[2];
    conn.on("close", (code) => {
      entry.videoClients.delete(conn);
      entry.controlClients.delete(conn);
      log({ t: "ws_close", kind, device: entry.id, origin, code });
    });
    if (kind === "control") {
      entry.controlClients.add(conn);
      conn.on("message", (data, binary) => {
        const text = binary ? null : String(data);
        let message = null;
        try { message = text === null ? null : JSON.parse(text); } catch { message = null; }
        const keyframe = Boolean(message && message.t === "system" && message.op === "keyframe");
        log({ t: "control_message", device: entry.id, origin, binary, data: text, keyframe });
        if (keyframe) entry.keyframePending = true;
      });
      conn.send(JSON.stringify({ t: "state", owner: entry.controlOwner, paused: false }));
      return;
    }
    entry.videoClients.add(conn);
    streamVideo(entry, conn).catch((error) => log({ t: "video_error", error: String(error?.message ?? error) }));
  });

  async function streamVideo(entry, conn) {
    const video = { width: 1080, height: 2400, packets: 300, packet_bytes: 20480, key_every: 30, key_interval_ms: 0,
      interval_ms: 0, new_session_at: null, new_width: 720, new_height: 1280, unknown_first: true,
      pause_after: null, pause_ms: 0, ...(readConfig().video ?? {}) };
    const open = () => conn.readyState === READY_STATE.OPEN;
    const session = (width, height) => {
      const message = Buffer.alloc(9);
      message[0] = 1;
      message.writeUInt32BE(width, 1);
      message.writeUInt32BE(height, 5);
      return message;
    };
    const config = Buffer.concat([Buffer.from([2]), CONFIG_FIXTURE]);
    conn.send(session(video.width, video.height));
    conn.send(config);
    if (video.unknown_first) conn.send(Buffer.from([9, 1, 2, 3]));
    const deltaBody = Buffer.alloc(Math.max(5, video.packet_bytes), 0xab);
    Buffer.from([0, 0, 0, 1, 0x41]).copy(deltaBody);
    const keyBody = Buffer.alloc(Math.max(5, video.packet_bytes), 0xcd);
    Buffer.from([0, 0, 0, 1, 0x65]).copy(keyBody);
    let lastKey = -Infinity;
    let forceKey = false;
    let sentBytes = 0;
    let index = 0;
    for (; video.packets === null || index < video.packets; index += 1) {
      if (!open()) break;
      if (video.new_session_at !== null && index === video.new_session_at) {
        conn.send(session(video.new_width, video.new_height));
        conn.send(config);
        forceKey = true;
        log({ t: "video_session", device: entry.id, index });
      }
      const now = performance.now();
      const requested = entry.keyframePending;
      const key = forceKey || requested || (video.key_every > 0 && index % video.key_every === 0) ||
        (video.key_interval_ms > 0 && now - lastKey >= video.key_interval_ms);
      if (key) {
        lastKey = now;
        forceKey = false;
        if (requested) {
          entry.keyframePending = false;
          log({ t: "keyframe_sent", device: entry.id, index });
        }
      }
      const body = key ? keyBody : deltaBody;
      const header = Buffer.alloc(10);
      header[0] = 3;
      header[1] = key ? 1 : 0;
      header.writeBigUInt64BE(BigInt(index) * 33333n, 2);
      conn.send(Buffer.concat([header, body]));
      sentBytes += body.length;
      while (open() && conn.writableNeedDrain) await sleep(1);
      if (video.pause_after !== null && index + 1 === video.pause_after) await sleep(video.pause_ms);
      if (video.interval_ms > 0) await sleep(video.interval_ms);
      else if (index % 32 === 31) await new Promise((resolve) => setImmediate(resolve));
    }
    log({ t: "video_done", device: entry.id, packets: index, bytes: sentBytes, open: open() });
  }

  server.listen(Number(options.port ?? 0) || 0, "127.0.0.1", () => {
    port = server.address().port;
    const directory = join(stateBase(), "canvas");
    mkdirSync(directory, { recursive: true });
    discoveryPath = join(directory, `${port}.json`);
    for (const item of readConfig().preattach ?? []) addDevice(item.platform, item.target);
    const document = { schema: DISCOVERY_SCHEMA, pid: process.pid, port, url: `http://127.0.0.1:${port}/`, token,
      started_at: new Date().toISOString(), mode: "workspace", workspace, tabs: [], devices: [], booted: [] };
    const temporary = `${discoveryPath}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify(document), { mode: 0o600 });
    renameSync(temporary, discoveryPath);
    log({ t: "serve", argv: args, port, workspace });
    // As the real Canvas: the URL with its token goes to stdout (the adapter sends it to /dev/null).
    process.stdout.write(`Autonom Canvas: http://127.0.0.1:${port}/#token=${token}\n`);
  });
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

// ----- client verbs (as the CLI runs them) -------------------------------------------------

function findCanvas(workspace) {
  const directory = join(stateBase(), "canvas");
  let names = [];
  try {
    names = readdirSync(directory).filter((name) => name.endsWith(".json"));
  } catch {
    return null;
  }
  for (const name of names.sort()) {
    try {
      const document = JSON.parse(readFileSync(join(directory, name), "utf8"));
      if (document.schema === DISCOVERY_SCHEMA && document.mode === "workspace" && document.workspace === workspace &&
          pidAlive(document.pid)) return document;
    } catch {
      // a half-written or foreign file
    }
  }
  return null;
}

function call(document, method, path, body) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const headers = { Accept: "application/json", "X-Autonom-Origin": "agent", Authorization: `Bearer ${document.token}` };
    if (data) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = data.length;
    }
    const request = httpRequest({ host: "127.0.0.1", port: document.port, method, path, headers, timeout: 20000 }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        let payload = {};
        try { payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); } catch { payload = {}; }
        resolve({ status: response.statusCode, payload });
      });
    });
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", reject);
    if (data) request.write(data);
    request.end();
  });
}

function fail(code, message) {
  process.stderr.write(`${JSON.stringify({ ok: false, error_code: code, error: message })}\n`);
  process.exit(2);
}

async function client(verb, args) {
  const options = flags(args);
  const workspace = typeof options.workspace === "string" ? options.workspace : "default";
  const document = findCanvas(workspace);
  if (!document) fail("canvas_not_found", `no Canvas is running for workspace ${workspace}`);
  if (verb === "stop") {
    await call(document, "POST", "/api/stop", {}).catch(() => null);
    const deadline = Date.now() + 10000;
    while (pidAlive(document.pid) && Date.now() < deadline) await sleep(50);
    const item = { port: document.port, pid: document.pid, workspace };
    if (pidAlive(document.pid)) item.still_running = true;
    process.stdout.write(`${JSON.stringify({ ok: true, stopped: [item] })}\n`);
    return;
  }
  const platform = options.platform;
  const target = options.target;
  if (typeof platform !== "string" || typeof target !== "string") fail("usage_error", "name the device");
  let answer;
  try {
    answer = verb === "attach"
      ? await call(document, "POST", "/api/devices", { platform, target })
      : await call(document, "POST", `/api/devices/${encodeURIComponent(deviceId(platform, target))}/detach`, {});
  } catch (error) {
    fail("backend_failed", `the Canvas on port ${document.port} did not answer: ${error.message}`);
  }
  if (answer.status !== 200) fail(answer.payload.error_code || "backend_failed", answer.payload.error || `HTTP ${answer.status}`);
  const out = verb === "attach"
    ? { ok: true, port: document.port, device: answer.payload.device ?? {}, attached: Boolean(answer.payload.attached), tab: null }
    : { ok: true, port: document.port, detached: answer.payload.detached, session_stopped: false };
  process.stdout.write(`${JSON.stringify(out, null, 2)}\n`);
}

const argv = process.argv.slice(2);
if (argv[0] !== "canvas" || !["serve", "attach", "detach", "stop"].includes(argv[1])) {
  fail("usage_error", "usage: fake_mcp_canvas.mjs canvas serve|attach|detach|stop ...");
}
if (argv[1] === "serve") await serve(argv.slice(2));
else await client(argv[1], argv.slice(2));
