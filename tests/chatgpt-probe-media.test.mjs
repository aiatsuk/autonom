// ChatGPT probe media endpoint (contract 3.1-3.4, 3.6): the spawned CLI with a temp token and
// --port 0, the security gate, HTTP routes, the WebSocket protocol, shutdown, and the device
// source hook through an injected fake factory. No device commands run here.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter, once } from "node:events";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { request } from "node:http";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test, { after, before, describe } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { FIXTURE_CODEC, configAnnexB, encodeIdrFrame, renderPattern } from "../integrations/chatgpt/autonom-chatgpt-probe/media/h264-pcm.mjs";
import {
  createBackpressure,
  createRateLimiter,
  createRefusedOriginLog,
  errorBody,
  gateRequest,
  parseAllowOrigin,
  parseArgs,
  parseFrameHeader,
} from "../integrations/chatgpt/autonom-chatgpt-probe/media/media-lib.mjs";
import { startProbeMedia } from "../integrations/chatgpt/autonom-chatgpt-probe/media/probe-media.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const MEDIA_DIR = join(ROOT, "integrations", "chatgpt", "autonom-chatgpt-probe", "media");
const SCRIPT = join(MEDIA_DIR, "probe-media.mjs");
const TOKEN = randomBytes(32).toString("base64url");
const ORIGIN = "https://web-sandbox.oaiusercontent.com";
const WILDCARD = "https://*.sandbox.example.com";
const SUB = "autonom-probe.v1";
const SCHEMA = "autonom.chatgpt-probe.media/v1";
const ALLOWED_ANDROID = "android:emulator-5584";
const ALLOWED_IOS = "ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5";

function sleep(ms) {
  return new Promise((done) => setTimeout(done, ms));
}

// ---------- spawned endpoint ----------

function spawnMedia(t, { args = [], env = {}, token = TOKEN, expectReady = true } = {}) {
  const home = mkdtempSync(join(tmpdir(), "probe-media-home-"));
  const childEnv = { PATH: process.env.PATH, HOME: home, ...env };
  if (token !== null) childEnv.AUTONOM_PROBE_TOKEN = token;
  const child = spawn(process.execPath, [SCRIPT, "--port", "0", "--autonom-root", ROOT, ...args], {
    env: childEnv,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const out = { stdout: "", stderr: "", child, home };
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    out.stdout += chunk;
  });
  child.stderr.on("data", (chunk) => {
    out.stderr += chunk;
  });
  out.exited = new Promise((done) => child.on("exit", (code, signal) => done({ code, signal, at: Date.now() })));
  t?.after?.(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    await out.exited;
    rmSync(home, { recursive: true, force: true });
  });
  out.ready = expectReady
    ? new Promise((done, fail) => {
        const timer = setTimeout(() => fail(new Error(`no ready line: ${out.stdout} ${out.stderr}`)), 5000);
        const check = () => {
          const line = out.stdout.split("\n")[0];
          if (out.stdout.includes("\n")) {
            clearTimeout(timer);
            child.stdout.off("data", check);
            done(JSON.parse(line));
          }
        };
        child.stdout.on("data", check);
        child.on("exit", () => {
          clearTimeout(timer);
          fail(new Error(`exited before ready: ${out.stdout} ${out.stderr}`));
        });
      })
    : null;
  return out;
}

function httpGet(port, path, { method = "GET", host, headers = {} } = {}) {
  return new Promise((done, fail) => {
    const req = request({ host: "127.0.0.1", port, path, method, agent: false, headers: { Host: host ?? `127.0.0.1:${port}`, ...headers } });
    req.on("response", (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        text += chunk;
      });
      res.on("end", () => {
        let json = null;
        try {
          json = text ? JSON.parse(text) : null;
        } catch {
          json = null;
        }
        done({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on("error", fail);
    req.end();
  });
}

const auth = (origin = ORIGIN) => ({ Origin: origin, Authorization: `Bearer ${TOKEN}` });

// ---------- minimal WebSocket client (masked frames, unfragmented) ----------

class TestWs {
  constructor(socket, head, response) {
    this.socket = socket;
    this.response = response;
    this.buffer = Buffer.alloc(0);
    this.queue = [];
    this.closed = null;
    this.listeners = new Set();
    socket.on("data", (chunk) => this.#data(chunk));
    socket.on("error", () => {});
    socket.on("close", () => {
      if (!this.closed) this.closed = { code: 1006, reason: "", at: Date.now() };
      this.#wake();
    });
    if (head?.length) this.#data(head);
  }

  #wake() {
    for (const listener of [...this.listeners]) listener();
  }

  #data(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      if (this.buffer.length < 2) return;
      let length = this.buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (this.buffer.length < 4) return;
        length = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (this.buffer.length < 10) return;
        length = Number(this.buffer.readBigUInt64BE(2));
        offset = 10;
      }
      if (this.buffer.length < offset + length) return;
      const opcode = this.buffer[0] & 0x0f;
      const payload = this.buffer.subarray(offset, offset + length);
      this.buffer = this.buffer.subarray(offset + length);
      if (opcode === 1) this.queue.push({ kind: "text", data: JSON.parse(payload.toString("utf8")), at: Date.now() });
      else if (opcode === 2) this.queue.push({ kind: "binary", data: Buffer.from(payload), at: Date.now() });
      else if (opcode === 8) {
        this.closed = { code: payload.length >= 2 ? payload.readUInt16BE(0) : 1005, reason: payload.subarray(2).toString("utf8"), at: Date.now() };
        if (!this.socket.destroyed) this.socket.end(TestWs.frame(8, payload.subarray(0, 2)));
      }
      this.#wake();
    }
  }

  static frame(opcode, payload) {
    const mask = randomBytes(4);
    const length = payload.length;
    let header;
    if (length < 126) header = Buffer.from([0x80 | opcode, 0x80 | length]);
    else if (length <= 0xffff) {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 126;
      header.writeUInt16BE(length, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x80 | opcode;
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(length), 2);
    }
    const masked = Buffer.from(payload);
    for (let i = 0; i < masked.length; i += 1) masked[i] ^= mask[i & 3];
    return Buffer.concat([header, mask, masked]);
  }

  sendText(message) {
    this.socket.write(TestWs.frame(1, Buffer.from(typeof message === "string" ? message : JSON.stringify(message))));
  }

  sendBinary(bytes) {
    this.socket.write(TestWs.frame(2, Buffer.from(bytes)));
  }

  sendRaw(buffer) {
    this.socket.write(buffer);
  }

  close() {
    if (!this.socket.destroyed) this.socket.write(TestWs.frame(8, Buffer.from([0x03, 0xe8])));
  }

  /** Remove and return the first queued message matching `predicate`, waiting up to timeoutMs. */
  async next(predicate = () => true, timeoutMs = 3000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const index = this.queue.findIndex(predicate);
      if (index >= 0) return this.queue.splice(index, 1)[0];
      if (this.closed) throw new Error(`socket closed (${this.closed.code}) while waiting`);
      const left = deadline - Date.now();
      if (left <= 0) throw new Error("timed out waiting for a WebSocket message");
      await new Promise((done) => {
        const finish = () => {
          clearTimeout(timer);
          this.listeners.delete(finish);
          done();
        };
        const timer = setTimeout(finish, left);
        this.listeners.add(finish);
      });
    }
  }

  async text(type, timeoutMs) {
    return (await this.next((m) => m.kind === "text" && m.data.type === type, timeoutMs)).data;
  }

  async binary(timeoutMs) {
    return parseFrameHeader((await this.next((m) => m.kind === "binary", timeoutMs)).data);
  }

  async waitClose(timeoutMs = 3000) {
    const deadline = Date.now() + timeoutMs;
    while (!this.closed && Date.now() < deadline) await sleep(5);
    return this.closed;
  }

  destroy() {
    this.socket.destroy();
  }
}

function wsOpen(port, { path = "/probe/ws?source=fixture", origin = ORIGIN, protocols = [SUB, `bearer.${TOKEN}`], host } = {}) {
  return new Promise((done, fail) => {
    const headers = {
      Host: host ?? `127.0.0.1:${port}`,
      Connection: "Upgrade",
      Upgrade: "websocket",
      "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": randomBytes(16).toString("base64"),
    };
    if (origin !== null) headers.Origin = origin;
    if (protocols) headers["Sec-WebSocket-Protocol"] = protocols.join(", ");
    const req = request({ host: "127.0.0.1", port, path, headers, agent: false });
    req.on("upgrade", (res, socket, head) => done({ status: 101, headers: res.headers, ws: new TestWs(socket, head, res) }));
    req.on("response", (res) => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        text += chunk;
      });
      res.on("end", () => {
        let json = null;
        try {
          json = JSON.parse(text);
        } catch {
          json = null;
        }
        done({ status: res.statusCode, headers: res.headers, text, json });
      });
    });
    req.on("error", fail);
    req.end();
  });
}

async function openOk(port, options) {
  const answer = await wsOpen(port, options);
  assert.equal(answer.status, 101, `upgrade refused: ${answer.status} ${answer.text ?? ""}`);
  return answer.ws;
}

// ---------- pure helpers ----------

test("gate order is Host, then Origin, then token, and refusals carry error payloads", () => {
  const allowOrigins = [parseAllowOrigin(ORIGIN), parseAllowOrigin(WILDCARD)];
  const refused = createRefusedOriginLog();
  const base = { method: "GET", path: "/probe/ping", port: 4000, token: TOKEN, allowOrigins, refused };
  const bad = gateRequest({ ...base, headers: { host: "evil.example:4000", origin: "https://evil.example" } });
  assert.deepEqual([bad.status, bad.code], [421, "host_not_allowed"]);
  assert.equal(refused.list().length, 0, "a Host refusal does not reach the Origin check");
  const upgrade = gateRequest({ ...base, upgrade: true, headers: { host: "evil.example:4000" } });
  assert.deepEqual([upgrade.status, upgrade.code], [403, "host_not_allowed"]);
  const origin = gateRequest({ ...base, headers: { host: "127.0.0.1:4000", origin: "https://evil.example" } });
  assert.deepEqual([origin.status, origin.code, origin.cors], [403, "origin_not_allowed", null]);
  const token = gateRequest({ ...base, headers: { host: "localhost:4000", origin: ORIGIN, authorization: "Bearer nope" } });
  assert.deepEqual([token.status, token.code, token.cors], [401, "unauthorized", ORIGIN]);
  const ok = gateRequest({ ...base, headers: { host: "127.0.0.1:4000", origin: ORIGIN, authorization: `Bearer ${TOKEN}` } });
  assert.deepEqual(ok, { ok: true, origin: ORIGIN });
  for (const code of ["host_not_allowed", "origin_not_allowed", "origin_required", "unauthorized", "bad_nonce", "bad_query", "bad_source", "device_not_allowed", "too_many_sockets", "relay_unavailable", "not_found", "method_not_allowed", "preflight_not_allowed", "bad_message", "rate_limited", "not_supported", "server_stopping", "internal_error", "upgrade_required"]) {
    const body = errorBody(code);
    assert.equal(body.ok, false);
    assert.equal(body.error_code, code);
    assert.equal(typeof body.error, "string");
    assert.notEqual(body.error, code, `${code} has a readable message`);
  }
});

test("argument parsing refuses bad values and emulator-5554", () => {
  assert.throws(() => parseArgs(["--port", "x", "--autonom-root", "/r"]), (error) => error.code === "invalid_arguments" && error.extras.field === "--port");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--fps", "61"]), (error) => error.extras.field === "--fps");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--allow-origin", "null"]), (error) => error.extras.field === "--allow-origin");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--allow-origin", "https://*.com"]), (error) => error.code === "invalid_arguments");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--allow-host", "nohost"]), (error) => error.extras.field === "--allow-host");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--allow-device", "android:emulator-5554"]), (error) => error.code === "device_not_allowed");
  assert.throws(() => parseArgs(["--port", "1", "--autonom-root", "/r", "--allow-device", "ios:short"]), (error) => error.extras.field === "--allow-device");
  assert.throws(() => parseArgs(["--autonom-root", "/r"]), (error) => error.extras.field === "--port");
  assert.throws(() => parseArgs(["--port", "1"]), (error) => error.extras.field === "--autonom-root");
  assert.throws(() => parseArgs(["--bogus"]), (error) => error.extras.problem === "unknown argument");
  const parsed = parseArgs(["--port", "0", "--autonom-root", "/r", "--allow-origin", "HTTPS://A.Example", "--allow-host", "127.0.0.1:47812", "--allow-device", ALLOWED_ANDROID]);
  assert.deepEqual(parsed.allowOrigins, ["https://a.example"]);
  assert.equal(parsed.fps, 30);
});

test("rate limiter, backpressure and refused-origin log", () => {
  let t = 0;
  const limiter = createRateLimiter({ now: () => t });
  const verdicts = Array.from({ length: 205 }, () => limiter.check());
  assert.equal(verdicts.filter((v) => v === "ok").length, 200);
  assert.equal(verdicts.filter((v) => v === "notify").length, 1);
  t = 1000;
  assert.equal(limiter.check(), "ok");
  const bp = createBackpressure({ high: 100, low: 10 });
  assert.equal(bp.admit(50, false), true);
  assert.equal(bp.admit(101, true), false, "over the high mark every frame is skipped");
  assert.equal(bp.admit(50, true), false, "still congested until below the low mark");
  assert.equal(bp.admit(5, false), false, "after draining a delta frame waits for a key frame");
  assert.equal(bp.admit(5, true), true);
  assert.equal(bp.admit(5, false), true);
  const log = createRefusedOriginLog({ now: () => new Date("2026-10-10T14:22:33.456Z") });
  log.record("x".repeat(300));
  assert.equal(log.list()[0].origin.length, 200);
  assert.equal(log.list()[0].last_at, "2026-10-10T14:22:33Z");
});

// ---------- spawned CLI ----------

describe("spawned probe-media.mjs", () => {
  let media;
  let port;

  before(async () => {
    media = spawnMedia(null, {
      args: ["--allow-origin", ORIGIN, "--allow-origin", WILDCARD, "--allow-host", "127.0.0.1:47999", "--allow-device", ALLOWED_ANDROID],
    });
    const ready = await media.ready;
    port = ready.port;
  });

  after(async () => {
    if (media.child.exitCode === null) media.child.kill("SIGKILL");
    await media.exited;
    rmSync(media.home, { recursive: true, force: true });
  });

  test("ready line has the contract shape and is the only stdout line", async () => {
    const lines = media.stdout.trim().split("\n");
    assert.equal(lines.length, 1);
    const ready = JSON.parse(lines[0]);
    assert.deepEqual(Object.keys(ready).sort(), ["event", "pid", "port", "version"]);
    assert.equal(ready.event, "ready");
    assert.equal(ready.version, "0.1.0");
    assert.equal(ready.pid, media.child.pid);
    assert.ok(Number.isInteger(ready.port) && ready.port > 0);
  });

  test("Host refusal (421) and DNS rebinding Host; loopback names and --allow-host pass", async () => {
    const rebinding = await httpGet(port, "/probe/ping?nonce=a", { host: `attacker.example:${port}`, headers: auth() });
    assert.equal(rebinding.status, 421);
    assert.deepEqual(rebinding.json, errorBody("host_not_allowed"));
    assert.equal(rebinding.headers["access-control-allow-origin"], undefined);
    const otherPort = await httpGet(port, "/probe/ping?nonce=a", { host: `127.0.0.1:${port + 1}`, headers: auth() });
    assert.equal(otherPort.status, 421);
    for (const host of [`localhost:${port}`, `LOCALHOST:${port}`, "127.0.0.1:47999"]) {
      const okay = await httpGet(port, "/probe/ping?nonce=a", { host, headers: auth() });
      assert.equal(okay.status, 200, host);
    }
  });

  test("Origin allowlist: exact, case, wildcard one label only, null and absent", async () => {
    assert.equal((await httpGet(port, "/probe/ping?nonce=a", { headers: auth("HTTPS://WEB-SANDBOX.OAIUSERCONTENT.COM") })).status, 200);
    assert.equal((await httpGet(port, "/probe/ping?nonce=a", { headers: auth("https://abc.sandbox.example.com") })).status, 200);
    for (const origin of ["https://a.b.sandbox.example.com", "http://abc.sandbox.example.com", "https://sandbox.example.com", "null", "file://", `${ORIGIN}:8443`, "http://web-sandbox.oaiusercontent.com"]) {
      const answer = await httpGet(port, "/probe/ping?nonce=a", { headers: auth(origin) });
      assert.equal(answer.status, 403, origin);
      assert.equal(answer.json.error_code, "origin_not_allowed", origin);
      assert.equal(answer.headers["access-control-allow-origin"], undefined, origin);
    }
    const absent = await httpGet(port, "/probe/ping?nonce=a", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(absent.status, 403);
    assert.deepEqual(absent.json, errorBody("origin_required"));
    const status = await httpGet(port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(status.status, 200);
    const frames = await httpGet(port, "/probe/frames?count=1", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(frames.status, 200);
    const nullEntry = status.json.refused_origins.find((entry) => entry.origin === "null");
    assert.equal(nullEntry.count, 1);
  });

  test("wrong origin is recorded with counts; the list caps at 20", async () => {
    await httpGet(port, "/probe/ping?nonce=a", { headers: auth("https://evil.example") });
    let status = await httpGet(port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
    const evil = status.json.refused_origins.find((entry) => entry.origin === "https://evil.example");
    assert.equal(evil.count, 1);
    assert.match(evil.last_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    await httpGet(port, "/probe/ping?nonce=a", { headers: auth("https://evil.example") });
    for (let i = 0; i < 25; i += 1) await httpGet(port, "/probe/status", { headers: { Origin: `https://n${i}.example`, Authorization: `Bearer ${TOKEN}` } });
    status = await httpGet(port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(status.json.refused_origins.length, 20);
    assert.equal(status.json.refused_origins.find((entry) => entry.origin === "https://evil.example").count, 2);
  });

  test("Bearer missing or wrong → 401 with CORS for the allowed origin", async () => {
    const missing = await httpGet(port, "/probe/ping?nonce=a", { headers: { Origin: ORIGIN } });
    assert.equal(missing.status, 401);
    assert.deepEqual(missing.json, errorBody("unauthorized"));
    assert.equal(missing.headers["access-control-allow-origin"], ORIGIN);
    const wrong = await httpGet(port, "/probe/ping?nonce=a", { headers: { Origin: ORIGIN, Authorization: `Bearer ${TOKEN.slice(1)}x` } });
    assert.equal(wrong.status, 401);
    const statusNoToken = await httpGet(port, "/probe/status");
    assert.equal(statusNoToken.status, 401);
    const basic = await httpGet(port, "/probe/status", { headers: { Authorization: `Basic ${TOKEN}` } });
    assert.equal(basic.status, 401);
  });

  test("preflight with Private Network Access, and refused preflights", async () => {
    const pre = await httpGet(port, "/probe/ping?nonce=a", {
      method: "OPTIONS",
      headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization", "Access-Control-Request-Private-Network": "true" },
    });
    assert.equal(pre.status, 204);
    assert.equal(pre.headers["access-control-allow-origin"], ORIGIN);
    assert.equal(pre.headers["access-control-allow-methods"], "GET");
    assert.equal(pre.headers["access-control-allow-headers"], "Authorization");
    assert.equal(pre.headers["access-control-max-age"], "60");
    assert.equal(pre.headers["access-control-allow-private-network"], "true");
    assert.equal(pre.headers["access-control-allow-credentials"], undefined);
    const noPna = await httpGet(port, "/probe/frames", { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET" } });
    assert.equal(noPna.status, 204);
    assert.equal(noPna.headers["access-control-allow-private-network"], undefined);
    const post = await httpGet(port, "/probe/ping", { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "POST" } });
    assert.deepEqual([post.status, post.json.error_code], [403, "preflight_not_allowed"]);
    const header = await httpGet(port, "/probe/ping", { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization, x-extra" } });
    assert.deepEqual([header.status, header.json.error_code], [403, "preflight_not_allowed"]);
    const foreign = await httpGet(port, "/probe/ping", { method: "OPTIONS", headers: { Origin: "https://evil.example", "Access-Control-Request-Method": "GET" } });
    assert.deepEqual([foreign.status, foreign.json.error_code], [403, "origin_not_allowed"]);
    assert.equal(foreign.headers["access-control-allow-origin"], undefined);
    const noOrigin = await httpGet(port, "/probe/ping", { method: "OPTIONS", headers: { "Access-Control-Request-Method": "GET" } });
    assert.deepEqual([noOrigin.status, noOrigin.json.error_code], [403, "origin_required"]);
    const elsewhere = await httpGet(port, "/other", { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "GET" } });
    assert.deepEqual([elsewhere.status, elsewhere.json.error_code], [404, "not_found"]);
  });

  test("ping echoes the nonce with JSON headers; bad nonce, unknown path and other methods", async () => {
    const startedAt = Date.now();
    const ping = await httpGet(port, "/probe/ping?nonce=Ab_-9", { headers: auth() });
    assert.equal(ping.status, 200);
    assert.equal(ping.headers["content-type"], "application/json; charset=utf-8");
    assert.equal(ping.headers["cache-control"], "no-store");
    assert.equal(ping.headers["x-content-type-options"], "nosniff");
    assert.equal(ping.headers["access-control-allow-origin"], ORIGIN);
    assert.equal(ping.headers.vary, "Origin");
    assert.equal(ping.json.ok, true);
    assert.equal(ping.json.schema_version, SCHEMA);
    assert.equal(ping.json.nonce, "Ab_-9");
    assert.equal(ping.json.origin_seen, ORIGIN);
    assert.equal(ping.json.host_seen, `127.0.0.1:${port}`);
    assert.ok(ping.json.server_time_ms >= startedAt - 1000 && ping.json.server_time_ms <= Date.now() + 1000);
    for (const path of ["/probe/ping", "/probe/ping?nonce=", "/probe/ping?nonce=a%20b", `/probe/ping?nonce=${"a".repeat(65)}`]) {
      const bad = await httpGet(port, path, { headers: auth() });
      assert.deepEqual([bad.status, bad.json], [400, errorBody("bad_nonce")], path);
    }
    const missing = await httpGet(port, "/probe/nothing", { headers: auth() });
    assert.deepEqual([missing.status, missing.json], [404, errorBody("not_found")]);
    const post = await httpGet(port, "/probe/ping?nonce=a", { method: "POST", headers: auth() });
    assert.deepEqual([post.status, post.json.error_code], [405, "method_not_allowed"]);
    assert.equal(post.headers.allow, "GET, OPTIONS");
    const plainWs = await httpGet(port, "/probe/ws?source=fixture", { headers: auth() });
    assert.deepEqual([plainWs.status, plainWs.json.error_code], [426, "upgrade_required"]);
  });

  test("frames route shape and after_seq", async () => {
    const answer = await httpGet(port, "/probe/frames?count=3&after_seq=-1", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(answer.status, 200);
    const body = answer.json;
    assert.equal(body.ok, true);
    assert.equal(body.schema_version, SCHEMA);
    assert.equal(body.codec, "avc1.42C01E");
    assert.equal(body.width, 96);
    assert.equal(body.height, 208);
    const config = Buffer.from(body.config_b64, "base64");
    assert.ok(config.equals(configAnnexB()));
    assert.deepEqual(
      body.frames.map((frame) => frame.seq),
      [0, 1, 2],
    );
    for (const frame of body.frames) {
      assert.deepEqual(Object.keys(frame).sort(), ["data_b64", "key", "seq", "t_server_ms"]);
      assert.equal(frame.key, true);
      const data = Buffer.from(frame.data_b64, "base64");
      assert.ok(data.subarray(0, config.length).equals(config), "each frame carries SPS and PPS");
      assert.equal(typeof frame.t_server_ms, "number");
    }
    const later = await httpGet(port, "/probe/frames?count=2&after_seq=10", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.deepEqual(
      later.json.frames.map((frame) => frame.seq),
      [11, 12],
    );
    const defaults = await httpGet(port, "/probe/frames", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(defaults.json.frames.length, 10);
    for (const query of ["count=0", "count=31", "count=1.5", "after_seq=-2", "count=x", "after_seq=1e3"]) {
      const bad = await httpGet(port, `/probe/frames?${query}`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      assert.deepEqual([bad.status, bad.json], [400, errorBody("bad_query")], query);
    }
  });

  test("WebSocket upgrade refusals: token, subprotocol, origin, host, source, devices", async () => {
    const cases = [
      [{ protocols: [SUB, "bearer.wrong-token-wrong-token-wrong-token"] }, 401, "unauthorized"],
      [{ protocols: [SUB] }, 401, "unauthorized"],
      [{ protocols: [`bearer.${TOKEN}`] }, 401, "unauthorized"],
      [{ protocols: null }, 401, "unauthorized"],
      [{ origin: "https://evil.example" }, 403, "origin_not_allowed"],
      [{ origin: null }, 403, "origin_required"],
      [{ origin: "null" }, 403, "origin_not_allowed"],
      [{ host: `attacker.example:${port}` }, 403, "host_not_allowed"],
      [{ path: "/probe/ws?source=camera" }, 400, "bad_source"],
      [{ path: "/probe/ws" }, 400, "bad_source"],
      [{ path: "/probe/ws?source=android:emulator-5590" }, 403, "device_not_allowed"],
      [{ path: "/probe/ws?source=android:emulator-5554" }, 403, "device_not_allowed"],
      [{ path: "/probe/ws?source=ios:45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5" }, 403, "device_not_allowed"],
      [{ path: "/probe/other" }, 404, "not_found"],
    ];
    for (const [options, status, code] of cases) {
      const answer = await wsOpen(port, options);
      assert.equal(answer.status, status, JSON.stringify(options));
      assert.equal(answer.json?.error_code, code, JSON.stringify(options));
      assert.equal(answer.json?.ok, false);
    }
  });

  test("fixture stream: hello, config, then >= 10 binary frames with valid headers and increasing seq", async () => {
    const answer = await wsOpen(port);
    assert.equal(answer.status, 101);
    assert.equal(answer.headers["sec-websocket-protocol"], SUB, "only autonom-probe.v1 is selected");
    const { ws } = answer;
    const hello = await ws.text("hello");
    assert.deepEqual(hello.source, { kind: "fixture", id: "fixture" });
    assert.equal(hello.schema_version, SCHEMA);
    assert.equal(typeof hello.server_time_ms, "number");
    const next = await ws.next();
    assert.equal(next.kind, "text", "config comes before the first frame");
    const config = next.data;
    assert.equal(config.type, "config");
    assert.deepEqual([config.codec, config.width, config.height], [FIXTURE_CODEC, 96, 208]);
    assert.ok(Number.isInteger(config.seq));
    let last = -1;
    for (let i = 0; i < 10; i += 1) {
      const frame = await ws.binary();
      assert.equal(frame.key, true);
      assert.equal(frame.config, true);
      assert.ok(frame.seq > last, "seq increases");
      if (i === 0) assert.equal(frame.seq, config.seq, "config names the next frame");
      last = frame.seq;
      assert.ok(Math.abs(frame.tServerMs - Date.now()) < 5000);
      assert.ok(frame.payload.subarray(0, configAnnexB().length).equals(configAnnexB()));
    }
    ws.close();
    await ws.waitClose();
  });

  test("echo order, ping → pong, fixture tap → not_supported, binary and malformed → bad_message", async () => {
    const ws = await openOk(port);
    await ws.text("hello");
    for (const payload of ["one", "two", "three"]) ws.sendText({ type: "echo", payload });
    const echoes = [await ws.text("echo"), await ws.text("echo"), await ws.text("echo")];
    assert.deepEqual(
      echoes.map((m) => [m.n, m.payload]),
      [
        [1, "one"],
        [2, "two"],
        [3, "three"],
      ],
    );
    const sentAt = Date.now();
    ws.sendText({ type: "ping", seq: 7, t_client: 123.5 });
    const pong = await ws.text("pong");
    assert.equal(pong.seq, 7);
    assert.equal(pong.t_client, 123.5);
    assert.ok(pong.t_server >= sentAt - 1000 && pong.t_server <= Date.now() + 1000);
    ws.sendText({ type: "tap", seq: 1, x: 0.5, y: 0.5 });
    const notSupported = await ws.text("error");
    assert.equal(notSupported.error_code, "not_supported");
    assert.equal(typeof notSupported.error, "string");
    ws.sendText({ type: "start", fps: 0 });
    ws.sendText("not json");
    ws.sendText({ type: "echo", payload: "x".repeat(257) });
    ws.sendText({ type: "teleport" });
    ws.sendBinary([1, 2, 3]);
    for (let i = 0; i < 5; i += 1) assert.equal((await ws.text("error")).error_code, "bad_message");
    ws.close();
    await ws.waitClose();
  });

  test("start retimes and stop pauses the fixture stream", async () => {
    const ws = await openOk(port);
    await ws.binary();
    ws.sendText({ type: "stop" });
    await new Promise((done) => setTimeout(done, 150));
    ws.queue.length = 0;
    await new Promise((done) => setTimeout(done, 200));
    assert.equal(ws.queue.filter((m) => m.kind === "binary").length, 0, "no frames while stopped");
    ws.sendText({ type: "start", fps: 5 });
    await ws.binary();
    ws.close();
    await ws.waitClose();
  });

  test("drop closes with 4001 quickly and a reconnect continues with a higher seq", async () => {
    const ws = await openOk(port);
    const beforeDrop = await ws.binary();
    const sentAt = Date.now();
    ws.sendText({ type: "drop" });
    const closed = await ws.waitClose(2000);
    assert.equal(closed.code, 4001);
    assert.equal(closed.reason, "probe drop");
    assert.ok(closed.at - sentAt < 500, `close took ${closed.at - sentAt} ms`);
    const again = await openOk(port);
    await again.text("hello");
    await again.text("config");
    const afterDrop = await again.binary();
    assert.ok(afterDrop.seq > beforeDrop.seq, "seq keeps increasing across sockets");
    again.close();
    await again.waitClose();
  });

  test("more than 200 messages per second are answered once with rate_limited and dropped", async () => {
    const ws = await openOk(port);
    await ws.text("hello");
    const burst = [];
    for (let i = 0; i < 250; i += 1) burst.push(TestWs.frame(1, Buffer.from(JSON.stringify({ type: "ping", seq: i, t_client: i }))));
    ws.sendRaw(Buffer.concat(burst));
    await ws.text("error").then((m) => assert.equal(m.error_code, "rate_limited"));
    await new Promise((done) => setTimeout(done, 300));
    const pongs = ws.queue.filter((m) => m.kind === "text" && m.data.type === "pong").length;
    const limited = ws.queue.filter((m) => m.kind === "text" && m.data.type === "error" && m.data.error_code === "rate_limited").length;
    assert.equal(pongs, 200);
    assert.equal(limited, 0, "rate_limited is sent once per second");
    ws.close();
    await ws.waitClose();
  });

  test("at most 8 sockets: the 9th upgrade gets 503; status counts clients", async () => {
    // Earlier sockets may still be finishing their close handshake.
    for (let i = 0; i < 50; i += 1) {
      const status = await httpGet(port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
      if (status.json.clients === 0) break;
      await new Promise((done) => setTimeout(done, 20));
    }
    const sockets = [];
    for (let i = 0; i < 8; i += 1) sockets.push(await openOk(port));
    const ninth = await wsOpen(port);
    assert.equal(ninth.status, 503);
    assert.deepEqual(ninth.json, errorBody("too_many_sockets"));
    const status = await httpGet(port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(status.json.ok, true);
    assert.equal(status.json.schema_version, SCHEMA);
    assert.equal(status.json.clients, 8);
    assert.deepEqual(status.json.sources[0], { id: "fixture", subscribers: 8 });
    assert.ok(Number.isInteger(status.json.uptime_ms) && status.json.uptime_ms >= 0);
    for (const ws of sockets) ws.close();
    await Promise.all(sockets.map((ws) => ws.waitClose()));
  });

  test(
    "an allowed device without the relay module answers 501 relay_unavailable",
    { skip: existsSync(join(MEDIA_DIR, "device-source.mjs")) ? "device-source.mjs is present; the relay test covers devices" : false },
    async () => {
      const answer = await wsOpen(port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
      assert.equal(answer.status, 501);
      assert.deepEqual(answer.json, errorBody("relay_unavailable"));
    },
  );

  test("port in use is reported on stdout with exit 3", async () => {
    const busy = spawn(process.execPath, [SCRIPT, "--port", String(port), "--autonom-root", ROOT], {
      env: { PATH: process.env.PATH, AUTONOM_PROBE_TOKEN: TOKEN },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    busy.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    const [code] = await once(busy, "exit");
    assert.equal(code, 3);
    assert.deepEqual(JSON.parse(stdout.trim()), { event: "error", error_code: "media_port_in_use" });
  });

  test("the token never appears on stdout or stderr", () => {
    assert.ok(!media.stdout.includes(TOKEN));
    assert.ok(!media.stderr.includes(TOKEN));
  });
});

describe("process lifecycle", () => {
  test("token missing or short → exit 2 with token_missing on stderr", async (t) => {
    for (const token of [null, "Zq9Zq9", `${"a".repeat(31)}`, `${"a".repeat(40)}!`]) {
      const run = spawnMedia(t, { token, expectReady: false });
      const exit = await run.exited;
      assert.equal(exit.code, 2, String(token));
      const error = JSON.parse(run.stderr.trim());
      assert.equal(error.ok, false);
      assert.equal(error.error_code, "token_missing");
      assert.equal(typeof error.error, "string");
      assert.equal(typeof error.hint, "string");
      assert.equal(run.stdout, "");
      if (token) assert.ok(!run.stderr.includes(token));
    }
  });

  test("missing Autonom root → exit 2 autonom_root_not_found; bad arguments → exit 2", async (t) => {
    const dir = mkdtempSync(join(tmpdir(), "probe-media-root-"));
    t.after(() => rmSync(dir, { recursive: true, force: true }));
    const run = spawn(process.execPath, [SCRIPT, "--port", "0", "--autonom-root", dir], { env: { PATH: process.env.PATH, AUTONOM_PROBE_TOKEN: TOKEN }, stdio: ["pipe", "pipe", "pipe"] });
    let stderr = "";
    run.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    const [code] = await once(run, "exit");
    assert.equal(code, 2);
    const error = JSON.parse(stderr.trim());
    assert.equal(error.error_code, "autonom_root_not_found");
    assert.equal(typeof error.hint, "string");
    for (const [args, expected] of [
      [["--port", "nope", "--autonom-root", ROOT], "invalid_arguments"],
      [["--port", "0", "--autonom-root", ROOT, "--allow-device", "android:emulator-5554"], "device_not_allowed"],
    ]) {
      const bad = spawn(process.execPath, [SCRIPT, ...args], { env: { PATH: process.env.PATH, AUTONOM_PROBE_TOKEN: TOKEN }, stdio: ["pipe", "pipe", "pipe"] });
      let text = "";
      bad.stderr.on("data", (chunk) => {
        text += chunk;
      });
      const [badCode] = await once(bad, "exit");
      assert.equal(badCode, 2);
      assert.equal(JSON.parse(text.trim()).error_code, expected);
    }
  });

  test("SIGTERM exits 0 within 2 s and closes sockets with 1001", async (t) => {
    const run = spawnMedia(t, { args: ["--allow-origin", ORIGIN] });
    const { port } = await run.ready;
    const ws = await openOk(port);
    await ws.binary();
    const sentAt = Date.now();
    run.child.kill("SIGTERM");
    const exit = await run.exited;
    assert.equal(exit.code, 0);
    assert.ok(exit.at - sentAt < 2000, `exit took ${exit.at - sentAt} ms`);
    const closed = await ws.waitClose(1000);
    assert.equal(closed.code, 1001);
    ws.destroy();
  });

  test("SIGINT and stdin EOF also exit 0 within 2 s", async (t) => {
    const interrupted = spawnMedia(t);
    await interrupted.ready;
    let sentAt = Date.now();
    interrupted.child.kill("SIGINT");
    let exit = await interrupted.exited;
    assert.equal(exit.code, 0, `SIGINT exit ${JSON.stringify(exit)} stderr ${interrupted.stderr}`);
    assert.ok(exit.at - sentAt < 2000);
    const eof = spawnMedia(t);
    await eof.ready;
    sentAt = Date.now();
    eof.child.stdin.end();
    exit = await eof.exited;
    assert.equal(exit.code, 0);
    assert.ok(exit.at - sentAt < 2000);
  });

  test("--self-test exits 0 without listening and without a token", async () => {
    const run = spawn(process.execPath, [SCRIPT, "--self-test"], { env: { PATH: process.env.PATH }, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    run.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    const [code] = await once(run, "exit");
    assert.equal(code, 0);
    assert.deepEqual(JSON.parse(stdout.trim()), { ok: true, self_test: "passed", failures: [] });
  });

  test("importing probe-media.mjs starts nothing", async (t) => {
    const dir = mkdtempSync(join(tmpdir(), "probe-media-import-"));
    t.after(() => rmSync(dir, { recursive: true, force: true }));
    const importer = join(dir, "importer.mjs");
    writeFileSync(importer, `const m = await import(${JSON.stringify(pathToFileURL(SCRIPT).href)});\nconsole.log(typeof m.startProbeMedia, typeof m.main);\n`);
    const run = spawn(process.execPath, [importer], { env: { PATH: process.env.PATH, AUTONOM_PROBE_TOKEN: TOKEN }, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    run.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    const timer = setTimeout(() => run.kill("SIGKILL"), 5000);
    const [code, signal] = await once(run, "exit");
    clearTimeout(timer);
    assert.equal(signal, null, "the importer exited on its own (nothing listening, stdin not held)");
    assert.equal(code, 0);
    assert.equal(stdout.trim(), "function function");
  });
});

// ---------- in-process: device source hook (contract 3.6) ----------

const DEVICE_CONFIG = configAnnexB();
const DEVICE_IDR = Buffer.concat([Buffer.from([0, 0, 0, 1]), encodeIdrFrame({ width: 96, height: 208, ...renderPattern({ frame: 1 }), idrPicId: 0 })]);
const DEVICE_DELTA = Buffer.from([0, 0, 0, 1, 0x41, 0x9a, 0x02, 0x03]);

function fakeDeviceFactory(log, behaviour = {}) {
  return ({ spec, adbPath, scriptsDir }) => {
    log.created.push({ spec, adbPath, scriptsDir });
    const source = new EventEmitter();
    let timer = null;
    source.start = async () => {
      log.started.push(spec);
      if (behaviour.failStart) throw Object.assign(new Error("fake start failed"), { code: "device_not_connected" });
      setImmediate(() => {
        source.emit("state", "streaming");
        source.emit("frame", { key: false, data: DEVICE_DELTA, tMs: Date.now() });
        source.emit("config", { codec: "avc1.640028", width: 1080, height: 2400, annexB: DEVICE_CONFIG });
        let n = 0;
        timer = setInterval(() => {
          source.emit("frame", { key: n % 4 === 0, data: n % 4 === 0 ? DEVICE_IDR : DEVICE_DELTA, tMs: Date.now() });
          n += 1;
          if (behaviour.errorAfter && n === behaviour.errorAfter) source.emit("error", Object.assign(new Error("fake stream broke"), { code: "device_stream_failed" }));
        }, 10);
      });
    };
    source.stop = async () => {
      clearInterval(timer);
      log.stopped.push(spec);
    };
    source.tap = async ({ x, y }) => {
      log.taps.push({ spec, x, y });
      if (behaviour.tapThrows) throw new Error("fake tap exploded");
      return behaviour.tapResult ?? { ok: true };
    };
    return source;
  };
}

function newLog() {
  return { created: [], started: [], stopped: [], taps: [] };
}

async function startInProcess(t, deps, extra = {}) {
  const handle = await startProbeMedia(
    { port: 0, token: TOKEN, autonomRoot: ROOT, allowOrigins: [ORIGIN], allowDevices: [ALLOWED_ANDROID, ALLOWED_IOS, "android:emulator-5554"], adbPath: "/fake/adb", ...extra },
    deps,
  );
  t.after(() => handle.close());
  return handle;
}

describe("device source hook (in process, injected fake)", () => {
  test("device source streams config and frames, taps are acknowledged, sources are shared", async (t) => {
    const log = newLog();
    const handle = await startInProcess(t, { createDeviceSource: fakeDeviceFactory(log), deviceStopDelayMs: 50 });
    const ws = await openOk(handle.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    const hello = await ws.text("hello");
    assert.deepEqual(hello.source, { kind: "android", id: ALLOWED_ANDROID });
    const config = await ws.text("config");
    assert.deepEqual([config.codec, config.width, config.height], ["avc1.640028", 1080, 2400]);
    const first = await ws.binary();
    assert.equal(first.key, true, "a subscriber starts at a key frame");
    assert.equal(first.config, true);
    assert.ok(first.payload.subarray(0, DEVICE_CONFIG.length).equals(DEVICE_CONFIG), "SPS/PPS are in band in key frames");
    assert.ok(first.payload.subarray(DEVICE_CONFIG.length).equals(DEVICE_IDR));
    const delta = await ws.binary();
    assert.equal(delta.key, false);
    assert.equal(delta.config, false);
    assert.ok(delta.payload.equals(DEVICE_DELTA));
    assert.ok(delta.seq > first.seq);
    assert.deepEqual(log.created, [{ spec: ALLOWED_ANDROID, adbPath: "/fake/adb", scriptsDir: join(ROOT, "plugins", "autonom", "skills", "android-emulator-browser", "scripts") }]);

    ws.sendText({ type: "tap", seq: 9, x: 0.05, y: 0.5 });
    assert.deepEqual(await ws.text("tap_ack"), { type: "tap_ack", seq: 9, ok: true });
    assert.deepEqual(log.taps, [{ spec: ALLOWED_ANDROID, x: 0.05, y: 0.5 }]);
    ws.sendText({ type: "start", fps: 10 });
    assert.equal((await ws.text("error")).error_code, "not_supported");

    const second = await openOk(handle.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    await second.text("hello");
    await second.text("config");
    assert.equal((await second.binary()).key, true);
    assert.equal(log.created.length, 1, "one source per spec");
    const status = await httpGet(handle.port, "/probe/status", { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.deepEqual(status.json.sources.find((source) => source.id === ALLOWED_ANDROID), { id: ALLOWED_ANDROID, state: "streaming", subscribers: 2 });

    ws.close();
    second.close();
    await Promise.all([ws.waitClose(), second.waitClose()]);
    for (let i = 0; i < 100 && !log.stopped.length; i += 1) await sleep(10);
    assert.deepEqual(log.stopped, [ALLOWED_ANDROID], "the source stops after its last subscriber leaves");
  });

  test("tap failures and source errors reach the client", async (t) => {
    const log = newLog();
    const handle = await startInProcess(t, { createDeviceSource: fakeDeviceFactory(log, { tapResult: { ok: false, error_code: "device_not_connected", error: "send failed" }, errorAfter: 3 }) });
    const ws = await openOk(handle.port, { path: `/probe/ws?source=${ALLOWED_IOS}` });
    assert.deepEqual((await ws.text("hello")).source, { kind: "ios", id: ALLOWED_IOS });
    ws.sendText({ type: "tap", seq: 2, x: 1, y: 0 });
    assert.deepEqual(await ws.text("tap_ack"), { type: "tap_ack", seq: 2, ok: false, error_code: "device_not_connected", error: "send failed" });
    const error = await ws.text("error");
    assert.deepEqual([error.error_code, error.error], ["device_stream_failed", "fake stream broke"]);
    ws.sendText({ type: "tap", seq: 3, x: 2, y: 0 });
    assert.equal((await ws.text("error")).error_code, "bad_message", "out-of-range tap is refused");
    ws.close();
    await ws.waitClose();

    const throwing = await startInProcess(t, { createDeviceSource: fakeDeviceFactory(newLog(), { tapThrows: true }) });
    const other = await openOk(throwing.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    other.sendText({ type: "tap", seq: 4, x: 0.5, y: 0.5 });
    const ack = await other.text("tap_ack");
    assert.deepEqual([ack.seq, ack.ok, ack.error_code, ack.error], [4, false, "tap_failed", "fake tap exploded"]);
    other.close();
    await other.waitClose();
  });

  test("a source that fails to start sends an error and closes 1011", async (t) => {
    const log = newLog();
    const handle = await startInProcess(t, { createDeviceSource: fakeDeviceFactory(log, { failStart: true }) });
    const ws = await openOk(handle.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    await ws.text("hello");
    const error = await ws.text("error");
    assert.deepEqual([error.error_code, error.error], ["device_not_connected", "fake start failed"]);
    const closed = await ws.waitClose();
    assert.equal(closed.code, 1011);
  });

  test("emulator-5554 is refused even when allowlisted; a missing relay module is 501", async (t) => {
    const log = newLog();
    const handle = await startInProcess(t, { createDeviceSource: fakeDeviceFactory(log) });
    const refused = await wsOpen(handle.port, { path: "/probe/ws?source=android:emulator-5554" });
    assert.deepEqual([refused.status, refused.json], [403, errorBody("device_not_allowed")]);
    assert.equal(log.created.length, 0);
    const missing = await startInProcess(t, {
      loadDeviceSource: async () => {
        throw new Error("Cannot find module device-source.mjs");
      },
    });
    const answer = await wsOpen(missing.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    assert.deepEqual([answer.status, answer.json], [501, errorBody("relay_unavailable")]);
  });

  test("an upgrade still loading the relay when the endpoint stops gets 503 server_stopping", async () => {
    let release;
    const gate = new Promise((done) => {
      release = done;
    });
    let loaderCalled = false;
    const handle = await startProbeMedia(
      { port: 0, token: TOKEN, autonomRoot: ROOT, allowOrigins: [ORIGIN], allowDevices: [ALLOWED_ANDROID] },
      {
        loadDeviceSource: async () => {
          loaderCalled = true;
          await gate;
          return fakeDeviceFactory(newLog());
        },
      },
    );
    const pending = wsOpen(handle.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    for (let i = 0; i < 200 && !loaderCalled; i += 1) await sleep(5);
    assert.ok(loaderCalled);
    const closing = handle.close();
    release();
    const answer = await pending;
    assert.deepEqual([answer.status, answer.json], [503, errorBody("server_stopping")]);
    await closing;
  });

  test("close() stops device sources and closes sockets with 1001", async () => {
    const log = newLog();
    const handle = await startProbeMedia({ port: 0, token: TOKEN, autonomRoot: ROOT, allowOrigins: [ORIGIN], allowDevices: [ALLOWED_ANDROID] }, { createDeviceSource: fakeDeviceFactory(log) });
    const ws = await openOk(handle.port, { path: `/probe/ws?source=${ALLOWED_ANDROID}` });
    await ws.binary();
    await handle.close();
    assert.deepEqual(log.stopped, [ALLOWED_ANDROID]);
    assert.equal((await ws.waitClose()).code, 1001);
    ws.destroy();
  });

  test("startProbeMedia refuses a bad token, a bad root and a busy port", async (t) => {
    await assert.rejects(startProbeMedia({ port: 0, token: "short", autonomRoot: ROOT }), (error) => error.code === "token_missing");
    const dir = mkdtempSync(join(tmpdir(), "probe-media-root-"));
    t.after(() => rmSync(dir, { recursive: true, force: true }));
    await assert.rejects(startProbeMedia({ port: 0, token: TOKEN, autonomRoot: dir }), (error) => error.code === "autonom_root_not_found");
    const blocker = createNetServer();
    await new Promise((done) => blocker.listen(0, "127.0.0.1", done));
    t.after(() => blocker.close());
    await assert.rejects(startProbeMedia({ port: blocker.address().port, token: TOKEN, autonomRoot: ROOT }), (error) => error.code === "media_port_in_use");
  });
});
